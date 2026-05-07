# -*- coding: utf-8 -*-
"""
策略名: TD_9_13_Sequential_Signal
功能:   基于 TD 9-13 Sequential 的信号识别 + 简单交易执行
环境:   PTrade 回测 / 交易引擎

模块结构（单文件，按层划分；除 config.json 外不依赖任何其它本地文件）:
    [1] 常量与入口路径         CONFIG_REL_PATH 等
    [2] 路径/配置/输出目录层   _join_research_path / load_config / prepare_output_dir
    [3] 日志层                StrategyLogger
    [4] 数据层                Bar / MarketDataFetcher
    [5] 信号层                SetupMachine / CountdownMachine / TDSignalProcessor
    [6] 元数据与交易输出层    MetadataRecorder / TradeRecordRecorder
    [7] 交易执行层            TradeExecutor
    [8] PTrade 策略钩子层     initialize / before_trading_start / handle_data / after_trading_end

设计要点:
    * 数据层与信号层完全解耦：信号层只接受 Bar 序列，不感知数据来源；数据层只负责
      从 PTrade API 获取并清洗 Bar 序列，不感知任何信号语义。
    * 不再使用任何持久化缓存；按 README 推荐采用 "前复权 + 每日全量重算"。
    * 跳过停牌日：只要 K 线 volume<=0 即视为停牌，从源头过滤；同时调用
      get_stock_status('HALT') 进一步确认当日是否停牌，停牌则当日不产生信号。
    * 仅在 PTrade 接口允许的钩子内调用对应接口（如 get_history / get_price 不能
      在 initialize 中调用，所有数据获取放在 before_trading_start / handle_data）。
    * 周期统一：本策略不支持混合频率，统一使用 config.data.frequency。
    * 信号/交易时序：
        - 数据层采用 include=False，即 handle_data 中拿到的"最后一根 K 线" = 上一根已收盘 K 线
          （日线策略下即昨日）。
        - 信号层基于该"最后一根 K 线"发出信号。
        - 交易层基于这些"昨日信号"在今日盘中下单（order_value 当日成交），与交易员
          视角一致：今天拿昨天收盘价跑完 TD，今天入场。
    * 简单交易策略：昨日出现 Countdown 完成信号时，按总资产的 1/10 执行对应方向交易。
"""

import json
from datetime import datetime
# 注意：PTrade 禁止 import os / import sys。所有文件/目录操作必须通过
# 内置 open()、PTrade 注入的 create_dir / get_research_path 完成。


# =============================================================================
# [1] 常量与入口路径
# =============================================================================

# 配置文件在 PTrade 研究目录下的相对路径。全局只在此处维护：
# 如需迁移目录，仅修改此常量即可；代码其它地方一律通过 CONFIG_REL_PATH 引用。
CONFIG_REL_PATH = "TD913_xiongrui/config.json"

# 本地 SimTradeLab 回测兼容路径。PTrade 实盘/云回测仍优先使用 CONFIG_REL_PATH；
# 如果研究目录下找不到配置，再按这些路径依次尝试。
LOCAL_CONFIG_FALLBACK_PATHS = [
    "strategies/my_strategy/research_path/config.json",
    "./strategies/my_strategy/research_path/config.json",
    "config.json",
]

# load_config 会记录最终实际读取到的配置路径。initialize 创建输出目录时会用它，
# 这样本地回测也能把 output 放到 config 同级目录下。
LOADED_CONFIG_PATH = CONFIG_REL_PATH

# 日志/CSV 中的日期时间统一格式（字符串类型）。
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"
EVENT_OUTCOME_WINDOWS = (5, 20, 60, 120)

# 配置 schema 中要求必填的顶层段。缺失即视为非法配置。
_REQUIRED_CONFIG_SECTIONS = (
    "universe", "data", "backtest", "setup", "countdown", "trade", "log",
)
_REQUIRED_CONFIG_KEYS = {
    "universe":    ("securities", "benchmark"),
    "data":        ("frequency", "fq", "lookback_count", "min_required_bars"),
    "backtest":    ("slippage", "limit_mode", "commission_ratio", "min_commission"),
    "setup":       ("require_perfect_for_signal",),
    "countdown":   ("enabled", "tdst_cancel_rule"),
    "trade":       (
        "enabled", "buy_fraction_of_portfolio", "min_trade_value", "min_cash_for_buy",
        "take_profit_on_sell_countdown", "stop_loss_enabled", "profit_target_r_multiple",
        "stop_loss_range_multiple", "require_perfect_setup_for_buy",
        "max_holding_days_enabled", "max_holding_days",
    ),
    "log":         ("level",),
}


# =============================================================================
# [2] 路径 / 配置 / 输出目录层
# =============================================================================

def _join_research_path(rel_path):
    """
    将相对路径拼接到 PTrade 研究目录根路径下。
    本地调试若无 get_research_path，退化为 './'。
    """
    base = "./"
    try:
        base = get_research_path()  # noqa: F821 - PTrade 注入
    except Exception:
        base = "./"
    if not base.endswith("/") and not base.endswith("\\"):
        base = base + "/"
    return base + rel_path


def _is_local_project_path(path):
    """
    判断是否为本地 SimTradeLab 项目相对路径。
    PTrade 研究目录路径仍通过 _join_research_path 处理；本地 fallback 路径则直接使用。
    """
    p = str(path)
    return p.startswith("strategies/") or p.startswith("./strategies/")


def _split_parent_rel(rel_path):
    """
    返回 (parent_rel, file_name)。若无分隔符则 parent_rel = ""。
    例: "TD913_xiongrui/config.json" -> ("TD913_xiongrui", "config.json")
    """
    if "/" in rel_path:
        parent, name = rel_path.rsplit("/", 1)
        return parent, name
    return "", rel_path


def _validate_config_schema(cfg):
    """对 config 做 schema 校验，发现缺失 / 类型错误就抛异常。"""
    if not isinstance(cfg, dict):
        raise ValueError("config 根对象必须是 JSON object / dict")
    for section in _REQUIRED_CONFIG_SECTIONS:
        if section not in cfg:
            raise ValueError("config 缺少顶层段: [{}]".format(section))
        if not isinstance(cfg[section], dict):
            raise ValueError("config 的 [{}] 段必须是对象".format(section))
        for key in _REQUIRED_CONFIG_KEYS.get(section, ()):
            if key not in cfg[section]:
                raise ValueError("config 缺少字段: [{}].{}".format(section, key))
    # 特殊结构校验
    sec_list = cfg["universe"].get("securities")
    if not isinstance(sec_list, list) or len(sec_list) == 0:
        raise ValueError("config.universe.securities 必须是非空列表")


def _file_exists(abs_path):
    """
    PTrade 禁用 os 模块，这里通过尝试 open 来判定目标文件是否存在且可读。
    仅用于文件，不用于目录。
    """
    try:
        f = open(abs_path, "r", encoding="utf-8")
        f.close()
        return True
    except Exception:
        return False


def load_config(config_rel_path):
    """
    强制读取并校验 config.json。文件不存在或内容非法时直接抛异常，不再使用
    内置默认值兜底（避免"看似成功实则走错"）。
    """
    global LOADED_CONFIG_PATH
    full_path = _join_research_path(config_rel_path)
    f = None
    # PTrade 主路径加载成功时记录原始相对路径；后续 create_dir 用相对路径，
    # open 写文件时再用 get_research_path() 拼成完整路径。
    # 本地 fallback 才记录本地项目路径，避免把 get_research_path() 拼接两次。
    read_path = config_rel_path
    errors = []
    try:
        f = open(full_path, "r", encoding="utf-8")
    except Exception as e:
        errors.append("{} | err={}".format(full_path, e))
        # 本地 SimTradeLab 回测通常从项目根目录运行，配置文件保留在策略目录下。
        for fallback_path in LOCAL_CONFIG_FALLBACK_PATHS:
            try:
                f = open(fallback_path, "r", encoding="utf-8")
                read_path = fallback_path
                break
            except Exception as fallback_err:
                errors.append("{} | err={}".format(fallback_path, fallback_err))
        if f is None:
            raise FileNotFoundError("配置文件不存在或不可读，已尝试: {}".format(" ; ".join(errors)))
    try:
        cfg = json.load(f)
    finally:
        f.close()

    # 剥离以下划线开头的注释/预留说明字段，保持对策略透明
    cfg = _strip_underscore_keys(cfg)
    _validate_config_schema(cfg)
    LOADED_CONFIG_PATH = read_path
    try:
        log.info("[CONFIG] 配置加载完成 | path={}".format(read_path))  # noqa: F821
    except Exception:
        pass
    return cfg


def _strip_underscore_keys(obj):
    """递归去除所有以 '_' 开头的键（视作注释 / 预留）。"""
    if isinstance(obj, dict):
        return {k: _strip_underscore_keys(v) for k, v in obj.items() if not k.startswith("_")}
    if isinstance(obj, list):
        return [_strip_underscore_keys(v) for v in obj]
    return obj


# 本次运行创建的输出目录内会写入此标记文件，后续启动可凭它识别"目录已被占用"。
_RUN_MARKER_NAME = ".initialized"
# 最多尝试的递增后缀数量，避免极端情况下死循环。
_MAX_DIR_SUFFIX_TRIES = 1000


def prepare_output_dir(start_date_str, config_rel_path):
    """
    在 config 同级目录下，基于"策略启动日期"创建一个全新的输出目录，用于存放
    本次运行的 .log 与 .csv。若同名目录已存在（以标记文件判定），依次追加
    _1、_2、... 直到得到一个全新的目录名。

    由于 PTrade 禁用 os 模块，这里仅使用 PTrade 的 create_dir + 内置 open：
        * 是否占用：尝试读取目录下的 .initialized 标记文件；读到即视为已占用
        * 创建目录：调用 PTrade 注入的 create_dir(rel)
        * 占用目录：写入 .initialized，下次其它 run 就能看到

    返回:
        (rel_dir, write_dir) 二元组。rel_dir 相对研究目录，传给 create_dir；
        write_dir 为实际传给 open() 的目录。PTrade 下是 get_research_path()
        拼出的完整路径，本地 fallback 下是本地项目路径。
    """
    parent_rel, _ = _split_parent_rel(config_rel_path)
    base_name = start_date_str
    is_local_path = _is_local_project_path(config_rel_path)
    if is_local_path:
        parent_write = parent_rel.rstrip("/\\")
    else:
        parent_write = _join_research_path(parent_rel).rstrip("/\\") if parent_rel else _join_research_path("").rstrip("/\\")

    # 先尝试确保父目录存在（与 config.json 同级）。如果本来就存在，
    # PTrade 的 create_dir 一般也会静默返回。
    if parent_rel:
        try:
            if not is_local_path:
                create_dir(parent_rel)  # noqa: F821 - PTrade 注入
        except Exception:
            pass

    for suffix in range(0, _MAX_DIR_SUFFIX_TRIES):
        candidate = base_name if suffix == 0 else "{}_{}".format(base_name, suffix)
        candidate_rel = (parent_rel + "/" + candidate) if parent_rel else candidate
        candidate_write = (parent_write + "/" + candidate) if parent_write else candidate

        # 目录中已有 .initialized 标记 → 说明是之前某一次运行留下来的
        if _file_exists(candidate_write + "/" + _RUN_MARKER_NAME):
            continue

        # 试着创建目录（若已存在且为空，PTrade 的 create_dir 一般不会抛错；
        # 若抛错则视为创建失败，尝试下一个后缀）。
        try:
            if is_local_path:
                # 本地 SimTradeLab 环境允许使用 pathlib / os，但策略文件为了兼容 PTrade
                # 不 import os；这里用 open marker 的父目录创建能力不可用，因此退化为
                # Python 内置 __import__ 动态导入 pathlib，只在本地路径分支执行。
                pathlib = __import__("pathlib")
                pathlib.Path(candidate_write).mkdir(parents=True, exist_ok=False)
            else:
                create_dir(candidate_rel)  # noqa: F821 - PTrade 注入
        except Exception:
            # 创建失败一般意味着该名字已被占用但没有 .initialized —— 跳过
            continue

        # 标记这个目录为"本次运行占用"。marker 写入失败不致命，最多下次同日运行覆盖。
        try:
            mf = open(candidate_write + "/" + _RUN_MARKER_NAME, "w", encoding="utf-8")
            try:
                mf.write("run_start={}\nsuffix={}\n".format(start_date_str, suffix))
            finally:
                mf.close()
        except Exception:
            pass

        return candidate_rel, candidate_write

    # 兜底：超出尝试上限仍未成功，退化为不带后缀，交由 PTrade 决定
    fallback_rel = (parent_rel + "/" + base_name) if parent_rel else base_name
    fallback_write = (parent_write + "/" + base_name) if parent_write else base_name
    return fallback_rel, fallback_write


# =============================================================================
# [3] 日志层
# =============================================================================

class StrategyLogger:
    """
    日志包装器：统一前缀 [TD]，在每条日志中带上 security/frequency/datetime 等上下文，
    便于在多标的、多周期场景下定位问题。
    底层依赖 PTrade 注入的全局 log 对象。
    """

    LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

    def __init__(self, level="INFO", verbose_state_transition=False, verbose_countdown_step=True):
        self.level = self.LEVEL_ORDER.get(str(level).upper(), 20)
        self.verbose_state_transition = bool(verbose_state_transition)
        self.verbose_countdown_step = bool(verbose_countdown_step)
        self.log_file_path = None

    def _enabled(self, level_name):
        return self.LEVEL_ORDER[level_name] >= self.level

    @staticmethod
    def _fmt_kv(kvs):
        """格式化键值对为字符串，便于日志输出"""
        if not kvs:
            return ""
        return " | " + " ".join("{}={}".format(k, v) for k, v in kvs.items())

    def _emit(self, level_name, message, security=None, frequency=None, datetime_=None, **kvs):
        """发出日志，根据级别与开关筛选"""
        if not self._enabled(level_name):
            return
        ctx = ""
        if security is not None:
            ctx += "[sec={}]".format(security)
        if frequency is not None:
            ctx += "[freq={}]".format(frequency)
        if datetime_ is not None:
            ctx += "[dt={}]".format(datetime_)
        line = "[TD]{} {}{}".format(ctx, message, self._fmt_kv(kvs))
        method = getattr(log, level_name.lower(), log.info)  # noqa: F821
        method(line)
        self._append_to_file(line)

    def set_log_file(self, log_file_path):
        """绑定日志文件路径；之后所有日志会在控制台与文件双写。"""
        self.log_file_path = log_file_path

    def _append_to_file(self, line):
        """将单行日志追加写入 .log 文件。失败不影响主流程。"""
        if not self.log_file_path:
            return
        try:
            f = open(self.log_file_path, "a", encoding="utf-8")
            try:
                f.write(line + "\n")
            finally:
                f.close()
        except Exception:
            # 文件日志失败时静默，避免日志系统反向影响策略执行
            return

    def debug(self, message, **kw):
        self._emit("DEBUG", message, **kw)

    def info(self, message, **kw):
        self._emit("INFO", message, **kw)

    def warning(self, message, **kw):
        self._emit("WARNING", message, **kw)

    def error(self, message, **kw):
        self._emit("ERROR", message, **kw)

    def state_transition(self, message, **kw):
        if self.verbose_state_transition:
            self._emit("DEBUG", message, **kw)

    def countdown_step(self, message, **kw):
        if self.verbose_countdown_step:
            self._emit("DEBUG", message, **kw)


# =============================================================================
# [4] 数据层
# =============================================================================

class Bar:
    """
    单根 K 线数据对象。仅承载数据，不含任何信号语义。
    """
    __slots__ = ("security", "frequency", "datetime", "open", "high", "low", "close", "volume", "amount")

    def __init__(self, security, frequency, datetime_, open_, high, low, close, volume, amount=None):
        self.security = security
        self.frequency = frequency
        self.datetime = datetime_
        self.open = float(open_)
        self.high = float(high)
        self.low = float(low)
        self.close = float(close)
        self.volume = float(volume)
        self.amount = None if amount is None else float(amount)

    def __repr__(self):
        """返回 Bar 对象的字符串表示。"""
        return "<Bar {} O:{:.2f} H:{:.2f} L:{:.2f} C:{:.2f} V:{:.0f}>".format(
            self.datetime, self.open, self.high, self.low, self.close, self.volume
        )


class MarketDataFetcher:
    """
    数据层：负责从 PTrade API 获取行情，并清洗为按时间升序的 List[Bar]。
    职责单一：只关心行情数据获取与停牌过滤；不感知任何信号逻辑。

    注意 PTrade API 使用范围：
      * get_history / get_price 不能在 initialize 中调用；
      * 本类的方法仅可在 before_trading_start / handle_data / after_trading_end / run_daily 中调用。
    """

    def __init__(self, frequency, fq, lookback_count, logger):
        self.frequency = frequency
        self.fq = fq
        self.lookback_count = int(lookback_count)
        self.logger = logger

    def fetch_recent_bars(self, security):
        """
        拉取该标的最近 lookback_count 根"已收盘" K 线，按时间升序返回。

        采用 include=False：返回的最后一根 K 线为"上一根已收盘"的 K 线（日线策略
        下即昨日），与 TD 序列"需要等 K 线收盘才分析"的要求一致。
        在 handle_data 中这意味着：
            今天盘中 → 基于昨日收盘后的 TD 结果 → 今日下单执行交易。

        同时会过滤掉 volume<=0 的停牌 K 线（历史停牌日不参与 TD 计数）。

        返回:
            List[Bar]，可能为空。
        """
        try:
            df = get_history(  # noqa: F821 - PTrade 注入
                count=self.lookback_count,
                frequency=self.frequency,
                field=["open", "high", "low", "close", "volume", "money"],
                security_list=security,
                fq=self.fq,
                include=False,
            )
        except Exception as e:
            self.logger.error("get_history 调用异常", security=security, frequency=self.frequency, err=e)
            return []

        if df is None or len(df) == 0:
            self.logger.warning("get_history 返回空数据", security=security, frequency=self.frequency)
            return []

        bars = []
        skipped = 0
        for dt, row in df.iterrows():
            try:
                volume = float(row["volume"])
            except Exception:
                skipped += 1
                continue
            if volume <= 0:
                skipped += 1
                continue
            try:
                bar = Bar(
                    security=security,
                    frequency=self.frequency,
                    datetime_=self._format_datetime(dt),
                    open_=row["open"],
                    high=row["high"],
                    low=row["low"],
                    close=row["close"],
                    volume=volume,
                    amount=row.get("money", None) if hasattr(row, "get") else None,
                )
            except Exception as e:
                self.logger.error("构造 Bar 失败", security=security, dt=dt, err=e)
                skipped += 1
                continue
            bars.append(bar)

        self.logger.debug(
            "拉取行情完成", security=security, frequency=self.frequency,
            total=len(df), valid=len(bars), skipped_halt=skipped,
        )
        return bars

    @staticmethod
    def _format_datetime(dt):
        """
        将 pandas Timestamp / datetime 统一格式化为 'YYYY-MM-DD HH:MM:SS' 字符串。
        类型仍为字符串，只是形态更利于阅读与 Excel 打开查看。
        """
        try:
            return dt.strftime(DATETIME_FMT)
        except Exception:
            return str(dt)

    @staticmethod
    def is_halt_today(security, query_date_yyyymmdd=None):
        """
        通过 get_stock_status 查询当日停牌状态。失败时保守返回 False（即不视为停牌）。
        参数:
            query_date_yyyymmdd: 'YYYYmmdd' 格式字符串；None 表示当前周期。
        """
        try:
            status = get_stock_status([security], "HALT", query_date_yyyymmdd)  # noqa: F821
            if status is None:
                return False
            return bool(status.get(security, False))
        except Exception:
            return False


# =============================================================================
# [5] 信号层
# =============================================================================

# ---- Setup 状态枚举（仅用于可读日志，不影响计算） ------------------------------------
SETUP_STATE_NAME = {
    (0, 0): "q0_idle",
    (-1, 0): "q1_post_ss_idle",
    (1, 1): "q2_buy_setup_d1",
    (1, 9): "q4_buy_setup_done",
    (1, 0): "q5_post_buy_setup",
    (-1, 1): "q6_sell_setup_d1",
    (-1, 9): "q8_sell_setup_done",
}


class SetupMachine:
    """
    Setup 子状态机
      * 输入仅为 (bar, prev_4_close)。
      * 状态完成时把 setup_bars（最近 9 根 K 线对象）暴露给上层，便于 Countdown 计算 TDST 等。

    判定规则:
        BS (Buy Setup 输入)   : bar.close < prev_4_close
        SS (Sell Setup 输入)  : bar.close > prev_4_close
        EQ (相等)             : 取消任何进行中的 setup（要求严格小于 / 大于）

    完美 Setup（参考 README）:
        Buy : (S[7].low<=S[5].low and S[7].low<=S[6].low) or (S[8].low<=S[5].low and S[8].low<=S[6].low)
        Sell: (S[7].high>=S[5].high and S[7].high>=S[6].high) or (S[8].high>=S[5].high and S[8].high>=S[6].high)
        说明：README 写的是 "第8或第9个交易日"、"第6和第7个交易日"，对应 1-based 的索引；
              本实现内部 setup_bars 为 0-based list，index 5/6/7/8 即对应 README 的第6/7/8/9 根。
    """

    def __init__(self, security, frequency, logger):
        self.security = security
        self.frequency = frequency
        self.logger = logger

        # 状态变量（沿用原项目命名）
        self.sd = 0   # 方向 1=买 -1=卖 0=无
        self.sc = 0   # 计数 0~9
        self.last_datetime = None

        # 最近一次正在累积的 setup 对应的 K 线，最多 9 根
        self.setup_bars = []

    # ----- 输入分类 -----
    @staticmethod
    def classify(bar_close, prev_4_close):
        if bar_close < prev_4_close:
            return "BS"
        if bar_close > prev_4_close:
            return "SS"
        return "EQ"

    # ----- 完美 Setup 判定 -----
    @staticmethod
    def _is_perfect_buy(setup_bars):
        if len(setup_bars) < 9:
            return False
        s6, s7, s8, s9 = setup_bars[5], setup_bars[6], setup_bars[7], setup_bars[8]
        cond_8 = (s8.low <= s6.low) and (s8.low <= s7.low)
        cond_9 = (s9.low <= s6.low) and (s9.low <= s7.low)
        return cond_8 or cond_9

    @staticmethod
    def _is_perfect_sell(setup_bars):
        if len(setup_bars) < 9:
            return False
        s6, s7, s8, s9 = setup_bars[5], setup_bars[6], setup_bars[7], setup_bars[8]
        cond_8 = (s8.high >= s6.high) and (s8.high >= s7.high)
        cond_9 = (s9.high >= s6.high) and (s9.high >= s7.high)
        return cond_8 or cond_9

    # ----- 步进 -----
    def step(self, bar, prev_4_close):
        """
        喂入一根 K 线和它对应的 t-4 收盘价，更新状态。

        返回:
            None 或 dict:
                {
                    "type": "BUY_SETUP" / "SELL_SETUP",
                    "perfect": bool,
                    "setup_bars": [Bar x 9]   (副本)
                }
            仅在 setup 第 9 根完成的当根 K 线返回信号。
        """
        if self.last_datetime is not None and bar.datetime <= self.last_datetime:
            self.logger.error(
                "SetupMachine 时间逆序", security=self.security, frequency=self.frequency,
                datetime_=bar.datetime, last=self.last_datetime,
            )
            return None

        inp = self.classify(bar.close, prev_4_close)

        prev_state = (self.sd, self.sc)
        new_sd, new_sc = self._transit(prev_state, inp)
        self.sd, self.sc = new_sd, new_sc
        self.last_datetime = bar.datetime

        # 维护 setup_bars
        signal = self._apply_state_action(bar)

        self.logger.state_transition(
            "Setup transit", security=self.security, frequency=self.frequency, datetime_=bar.datetime,
            input=inp, prev=SETUP_STATE_NAME.get(prev_state, prev_state),
            curr=SETUP_STATE_NAME.get((self.sd, self.sc), (self.sd, self.sc)),
            sc=self.sc,
        )
        return signal

    def _transit(self, state, inp):
        """
        根据当前状态和输入返回新状态 (sd, sc)。每个 q 状态独立成块，
        每种输入（BS / SS / EQ）显式分支，便于与状态机图直接对照。
        """
        sd, sc = state

        # ===== q0: 初始/重置状态 =====
        if (sd, sc) == (0, 0):
            if inp == "BS":
                return (1, 0)            # → q5（已感知到 BS 方向，但尚未启动 setup 计数）
            if inp == "SS":
                return (-1, 0)           # → q1
            if inp == "EQ":
                return (0, 0)            # 留在 q0

        # ===== q1: 上一根 SS，等待方向反转 =====
        elif (sd, sc) == (-1, 0):
            if inp == "BS":
                return (1, 1)            # → q2 启动 buy setup 第 1 根
            if inp == "SS":
                return (-1, 0)           # 留在 q1
            if inp == "EQ":
                return (0, 0)            # → q0

        # ===== q2: buy setup 第 1 根 =====
        elif (sd, sc) == (1, 1):
            if inp == "BS":
                return (1, 2)            # → q3 buy setup 第 2 根
            if inp == "SS":
                return (-1, 1)           # → q6 转向启动 sell setup
            if inp == "EQ":
                return (0, 0)            # → q0 中断

        # ===== q3: buy setup 进行中（第 2~8 根） =====
        elif sd == 1 and 2 <= sc <= 8:
            if inp == "BS":
                return (1, sc + 1)       # 在 q3 内累加；累加到 9 即进入 q4
            if inp == "SS":
                return (-1, 1)           # → q6 中断当前 buy
            if inp == "EQ":
                return (0, 0)            # → q0 中断

        # ===== q4: buy setup 已完成（sc=9） =====
        elif (sd, sc) == (1, 9):
            if inp == "BS":
                return (1, 0)            # → q5 进入 setup 后余波
            if inp == "SS":
                return (-1, 1)           # → q6 反向启动
            if inp == "EQ":
                return (0, 0)            # → q0

        # ===== q5: buy setup 完成后的余波，等待方向变化 =====
        elif (sd, sc) == (1, 0):
            if inp == "BS":
                return (1, 0)            # 留在 q5（继续 BS 不再产生新 setup）
            if inp == "SS":
                return (-1, 1)           # → q6
            if inp == "EQ":
                return (0, 0)            # → q0

        # ===== q6: sell setup 第 1 根 =====
        elif (sd, sc) == (-1, 1):
            if inp == "BS":
                return (1, 1)            # → q2 转向启动 buy setup
            if inp == "SS":
                return (-1, 2)           # → q7 sell setup 第 2 根
            if inp == "EQ":
                return (0, 0)            # → q0

        # ===== q7: sell setup 进行中（第 2~8 根） =====
        elif sd == -1 and 2 <= sc <= 8:
            if inp == "BS":
                return (1, 1)            # → q2 中断当前 sell
            if inp == "SS":
                return (-1, sc + 1)      # 在 q7 内累加；累加到 9 即进入 q8
            if inp == "EQ":
                return (0, 0)            # → q0

        # ===== q8: sell setup 已完成（sc=9） =====
        elif (sd, sc) == (-1, 9):
            if inp == "BS":
                return (1, 0)            # → q5
            if inp == "SS":
                return (-1, 1)           # → q6 同向重启
            if inp == "EQ":
                return (0, 0)            # → q0

        raise ValueError("invalid setup state: ({}, {}), input={}".format(sd, sc, inp))

    def _apply_state_action(self, bar):
        """
        进入新状态后维护 setup_bars 并在第 9 根完成时返回信号。
        每个 q 状态独立成块，与 _transit 一一对应。
        """
        sd, sc = self.sd, self.sc

        # ===== q0: 重置 =====
        if (sd, sc) == (0, 0):
            self.setup_bars = []
            return None

        # ===== q1: SS 方向待机，清空 setup_bars =====
        if (sd, sc) == (-1, 0):
            self.setup_bars = []
            return None

        # ===== q2: buy setup 第 1 根，开始累积 =====
        if (sd, sc) == (1, 1):
            self.setup_bars = [bar]
            return None

        # ===== q3: buy setup 进行中（第 2~8 根），追加 =====
        if sd == 1 and 2 <= sc <= 8:
            self.setup_bars.append(bar)
            return None

        # ===== q4: buy setup 第 9 根完成，发出 BUY_SETUP 信号 =====
        if (sd, sc) == (1, 9):
            self.setup_bars.append(bar)
            if len(self.setup_bars) != 9:
                self.logger.error(
                    "Buy Setup 完成时 setup_bars 长度异常",
                    security=self.security, datetime_=bar.datetime, length=len(self.setup_bars),
                )
                return None
            return {
                "type": "BUY_SETUP",
                "perfect": self._is_perfect_buy(self.setup_bars),
                "setup_bars": list(self.setup_bars),
            }

        # ===== q5: buy setup 后余波，清空 setup_bars =====
        if (sd, sc) == (1, 0):
            self.setup_bars = []
            return None

        # ===== q6: sell setup 第 1 根，开始累积 =====
        if (sd, sc) == (-1, 1):
            self.setup_bars = [bar]
            return None

        # ===== q7: sell setup 进行中（第 2~8 根），追加 =====
        if sd == -1 and 2 <= sc <= 8:
            self.setup_bars.append(bar)
            return None

        # ===== q8: sell setup 第 9 根完成，发出 SELL_SETUP 信号 =====
        if (sd, sc) == (-1, 9):
            self.setup_bars.append(bar)
            if len(self.setup_bars) != 9:
                self.logger.error(
                    "Sell Setup 完成时 setup_bars 长度异常",
                    security=self.security, datetime_=bar.datetime, length=len(self.setup_bars),
                )
                return None
            return {
                "type": "SELL_SETUP",
                "perfect": self._is_perfect_sell(self.setup_bars),
                "setup_bars": list(self.setup_bars),
            }

        raise ValueError("invalid setup state after transit: ({}, {})".format(sd, sc))


class CountdownMachine:
    """
    Countdown 状态机（序列型 Sequence Countdown）。

    生命周期:
        由 TDSignalProcessor 在 Setup 完成时通过 start() 创建，进行中通过 step() 推进，
        触发完成 / 取消时由处理器丢弃实例。

    序列计数规则（README）:
        Buy  : bar.close <= bar[t-2].low  → count + 1
        Sell : bar.close >= bar[t-2].high → count + 1
        最大 13；可不连续。

    完美 Countdown（README）:
        Buy  : count[13].close <= count[8].close
        Sell : count[13].close >= count[8].close
        当 count==12 后下一根满足一般条件但不满足完美条件时，count 不进位为 13，
        而是输出 "+" 暂记信号；继续等待下一根满足条件 + 完美的 K 线，方进位为 13。

    取消条件:
        a. 出现相反方向 Setup 完成（由处理器外部触发 cancel，原因 'opposite_setup'）
        b. 出现新的同向 Setup 完成（由处理器外部触发 cancel，原因 'same_setup'）
        c. TDST 突破（5 选 1 规则，本类内部 step() 中检测）

    TDST 5 选 1 规则（针对 Buy Countdown，Sell 取反）:
        1. bar.high  >  max(setup_bars.close)
        2. bar.high  >  max(setup_bars.high)
        3. bar.close >  max(setup_bars.close)
        4. bar.close >  max(setup_bars.high)         （默认）
        5. bar.close >  max(true_high(setup_bars))   true_high = max(high, prev_close)
    """

    def __init__(self, direction, setup_bars, config_countdown, logger, security, frequency, setup_info=None):
        """
        direction: 1=Buy, -1=Sell
        setup_bars: 触发本 Countdown 的 9 根 setup K 线（按时间升序）
        setup_info: 可选，记录触发本 Countdown 的 setup 元信息（type / perfect / first_dt / last_dt），
                    仅用于下游事件携带上下文，FSM 逻辑不依赖它。
        """
        self.direction = direction
        self.setup_bars = list(setup_bars)
        self.config = config_countdown
        self.logger = logger
        self.security = security
        self.frequency = frequency

        self.count = 0
        self.bars_at_count = []          # 长度 == self.count，每个元素为对应的 Bar
        self.completed = False
        self.cancelled = False
        self.cancel_reason = None
        # 当 count==12 时，下一根满足一般条件但不满足完美条件 → 输出 "+"
        # 该标志仅用于上层日志/记录
        self.last_plus_dt = None

        # 预计算 TDST 阈值（buy: 取最大；sell: 取最小）
        self._tdst_threshold = self._compute_tdst_threshold()

        # 本 Countdown 的启动上下文（外部只读，便于交易事件串联）
        self.setup_info = dict(setup_info) if setup_info else {}
        self.start_bar_dt = setup_bars[-1].datetime if setup_bars else None

    # ----- TDST 阈值预计算 -----
    def _compute_tdst_threshold(self):
        rule = int(self.config.get("tdst_cancel_rule", 4))
        bars = self.setup_bars
        if not bars:
            return None
        if self.direction == 1:
            if rule == 1:
                return max(b.close for b in bars)
            if rule == 2:
                return max(b.high for b in bars)
            if rule == 3:
                return max(b.close for b in bars)
            if rule == 4:
                return max(b.high for b in bars)
            if rule == 5:
                # true_high 需要 prev_close，这里用 setup_bars 内部前一根；首根 true_high=high
                hi = []
                for i, b in enumerate(bars):
                    if i == 0:
                        hi.append(b.high)
                    else:
                        hi.append(max(b.high, bars[i - 1].close))
                return max(hi)
        else:
            if rule == 1:
                return min(b.close for b in bars)
            if rule == 2:
                return min(b.low for b in bars)
            if rule == 3:
                return min(b.close for b in bars)
            if rule == 4:
                return min(b.low for b in bars)
            if rule == 5:
                lo = []
                for i, b in enumerate(bars):
                    if i == 0:
                        lo.append(b.low)
                    else:
                        lo.append(min(b.low, bars[i - 1].close))
                return min(lo)
        return None

    def _check_tdst_break(self, bar, prev_close):
        rule = int(self.config.get("tdst_cancel_rule", 4))
        thr = self._tdst_threshold
        if thr is None:
            return False
        if self.direction == 1:
            if rule == 1:
                return bar.high > thr
            if rule == 2:
                return bar.high > thr
            if rule == 3:
                return bar.close > thr
            if rule == 4:
                return bar.close > thr
            if rule == 5:
                return bar.close > thr  # rule 5: 直接比较收盘价 > true_high 最大值
        else:
            if rule == 1:
                return bar.low < thr
            if rule == 2:
                return bar.low < thr
            if rule == 3:
                return bar.close < thr
            if rule == 4:
                return bar.close < thr
            if rule == 5:
                return bar.close < thr
        return False

    # ----- 完美 Countdown 判定（针对最终第 13 根） -----
    def _is_perfect_13(self, bar_13):
        """要求第 13 根（候选）相对第 8 根的关系。"""
        if len(self.bars_at_count) < 8:
            return False
        bar_8 = self.bars_at_count[7]  # 0-based
        if self.direction == 1:
            return bar_13.close <= bar_8.close
        return bar_13.close >= bar_8.close

    # ----- 主步进 -----
    def step(self, bar, prev_2_low, prev_2_high, prev_close):
        """
        喂入一根 K 线，处理顺序: TDST 取消 → 计数判定 → 完成判定。

        参数:
            prev_2_low / prev_2_high: 当前 bar 的 t-2 K 线的 low/high；若不足则 None
            prev_close              : 当前 bar 的 t-1 K 线的 close；若不足则 None

        返回:
            事件列表（按发生顺序），每个元素为 dict:
                {"type": "PROGRESS"|"PLUS_TENTATIVE"|"COMPLETE"|"CANCEL", ...}
        """
        events = []

        if self.completed or self.cancelled:
            return events

        # (1) TDST 取消
        if self._check_tdst_break(bar, prev_close):
            self.cancelled = True
            self.cancel_reason = "tdst_break_rule_{}".format(self.config.get("tdst_cancel_rule", 4))
            events.append({
                "type": "CANCEL",
                "reason": self.cancel_reason,
                "bar": bar,
                "count": self.count,
                "direction": self.direction,
            })
            return events

        # (2) 一般计数条件
        meets_general = False
        if prev_2_low is not None and prev_2_high is not None:
            if self.direction == 1:
                meets_general = bar.close <= prev_2_low
            else:
                meets_general = bar.close >= prev_2_high

        if not meets_general:
            return events

        # (3) 完美 Countdown 进位逻辑
        if self.count == 12 and self.config.get("require_perfect", False):
            if self._is_perfect_13(bar):
                self.count = 13
                self.bars_at_count.append(bar)
                self.completed = True
                events.append({
                    "type": "COMPLETE",
                    "bar": bar,
                    "count": 13,
                    "direction": self.direction,
                    "perfect": True,
                })
            else:
                # 不进位，仅记录 "+"
                self.last_plus_dt = bar.datetime
                events.append({
                    "type": "PLUS_TENTATIVE",
                    "bar": bar,
                    "count": 12,
                    "direction": self.direction,
                })
            return events

        # 正常进位
        self.count += 1
        self.bars_at_count.append(bar)
        if self.count >= 13:
            self.completed = True
            events.append({
                "type": "COMPLETE",
                "bar": bar,
                "count": 13,
                "direction": self.direction,
                "perfect": self._is_perfect_13(bar),
            })
        else:
            events.append({
                "type": "PROGRESS",
                "bar": bar,
                "count": self.count,
                "direction": self.direction,
            })
        return events

    # ----- 由外部强制取消（同向 / 反向 setup） -----
    def cancel_by_setup(self, reason, trigger_bar):
        if self.completed or self.cancelled:
            return None
        self.cancelled = True
        self.cancel_reason = reason
        return {
            "type": "CANCEL",
            "reason": reason,
            "bar": trigger_bar,
            "count": self.count,
            "direction": self.direction,
        }


class TDSignalProcessor:
    """
    单标的信号处理器：将一段时间升序的 Bar 序列喂入 Setup → Countdown 流水线，
    输出该序列中产生的所有信号事件。

    本处理器是无状态可重建的：每天 handle_data 都重新构造一个全新的处理器，
    将最近 N 根有效 K 线全量喂入，然后筛出"最后一根（=今日）"产生的信号即可。
    """

    def __init__(self, security, frequency, config, logger):
        self.security = security
        self.frequency = frequency
        self.config = config
        self.logger = logger

        self.setup_machine = SetupMachine(security, frequency, logger)
        self.active_countdown = None  # 当前进行中的 Countdown 实例

    # ----- 主循环 -----
    def run(self, bars):
        """
        bars: 时间升序的 List[Bar]（已剔除停牌/0 量）。
        返回: 该序列中所有信号事件 List[dict]，每个事件附带 'datetime' 字段。
        """
        events = []
        if not bars:
            return events

        n = len(bars)
        if n < 5:
            self.logger.warning(
                "K 线数量不足以驱动 SetupMachine（需至少 5 根）",
                security=self.security, frequency=self.frequency, count=n,
            )
            return events

        # 从 idx=4 开始（确保 t-4 存在）
        for i in range(4, n):
            bar = bars[i]
            prev_4_close = bars[i - 4].close
            prev_close = bars[i - 1].close
            prev_2_low = bars[i - 2].low if i >= 2 else None
            prev_2_high = bars[i - 2].high if i >= 2 else None

            # ---- (a) Setup 步进 ----
            setup_signal = self.setup_machine.step(bar, prev_4_close)
            if setup_signal is not None:
                self._emit_setup_signal(setup_signal, bar, events)
                self._handle_countdown_on_setup_complete(setup_signal, bar, events)

            # ---- (b) Countdown 步进 ----
            #   注意：Setup 完成时同根 K 线已经构造了新的 active_countdown，
            #   按 README "包括构成 TD 买入结构的第九根 K 线"，第 9 根需要立即检查 countdown 条件。
            if self.active_countdown is not None:
                cd_events = self.active_countdown.step(bar, prev_2_low, prev_2_high, prev_close)
                for ev in cd_events:
                    self._emit_countdown_event(ev, events)

                if self.active_countdown.completed or self.active_countdown.cancelled:
                    self.active_countdown = None

        return events

    # ----- 内部辅助 -----
    def _emit_setup_signal(self, setup_signal, bar, events_out):
        type_name = "BUY_SETUP" if setup_signal["type"] == "BUY_SETUP" else "SELL_SETUP"
        setup_bars = setup_signal["setup_bars"]
        record = {
            "datetime": bar.datetime,
            "security": self.security,
            "frequency": self.frequency,
            "category": "SETUP",
            "type": type_name + ("_PERFECT" if setup_signal["perfect"] else ""),
            "direction": 1 if setup_signal["type"] == "BUY_SETUP" else -1,
            "perfect": setup_signal["perfect"],
            "count": 9,
            # 扩展字段，便于 trade CSV 追溯
            "setup_first_dt": setup_bars[0].datetime,
            "setup_last_dt": setup_bars[-1].datetime,
            "setup_type": type_name,
            "setup_perfect": setup_signal["perfect"],
        }
        events_out.append(record)
        self.logger.info(
            "Setup 完成", security=self.security, frequency=self.frequency, datetime_=bar.datetime,
            type=record["type"], perfect=setup_signal["perfect"],
            setup_first_dt=record["setup_first_dt"],
        )

    def _handle_countdown_on_setup_complete(self, setup_signal, bar, events_out):
        """根据 setup 完成情况决定是否取消旧 countdown / 启动新 countdown。"""
        if not self.config["countdown"].get("enabled", True):
            return

        new_dir = 1 if setup_signal["type"] == "BUY_SETUP" else -1

        # 根据配置决定是否需要完美 setup 才启动 countdown
        if self.config["setup"].get("require_perfect_for_signal", False) and not setup_signal["perfect"]:
            return

        # 取消旧 countdown
        if self.active_countdown is not None and not (self.active_countdown.completed or self.active_countdown.cancelled):
            old_dir = self.active_countdown.direction
            cancel_flag = False
            if old_dir != new_dir and self.config["countdown"].get("cancel_on_opposite_setup", True):
                cancel_evt = self.active_countdown.cancel_by_setup("opposite_setup", bar)
                cancel_flag = True
            elif old_dir == new_dir and self.config["countdown"].get("cancel_on_same_setup", True):
                cancel_evt = self.active_countdown.cancel_by_setup("same_setup", bar)
                cancel_flag = True
            else:
                cancel_evt = None

            if cancel_flag and cancel_evt is not None:
                self._emit_countdown_event(cancel_evt, events_out)
                self.active_countdown = None

        # 启动新 countdown
        if self.active_countdown is None:
            setup_info = {
                "setup_type": "BUY_SETUP" if new_dir == 1 else "SELL_SETUP",
                "setup_perfect": setup_signal["perfect"],
                "setup_first_dt": setup_signal["setup_bars"][0].datetime,
                "setup_last_dt": setup_signal["setup_bars"][-1].datetime,
                "setup_highest_high": max(b.high for b in setup_signal["setup_bars"]),
            }
            self.active_countdown = CountdownMachine(
                direction=new_dir,
                setup_bars=setup_signal["setup_bars"],
                config_countdown=self.config["countdown"],
                logger=self.logger,
                security=self.security,
                frequency=self.frequency,
                setup_info=setup_info,
            )
            self.logger.debug(
                "Countdown 启动", security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction="BUY" if new_dir == 1 else "SELL",
                tdst_threshold=self.active_countdown._tdst_threshold,
                setup_first_dt=setup_info["setup_first_dt"],
                setup_perfect=setup_info["setup_perfect"],
            )

    def _emit_countdown_event(self, ev, events_out):
        dir_str = "BUY" if ev["direction"] == 1 else "SELL"
        ev_type = ev["type"]
        bar = ev["bar"]
        type_name = "{}_COUNTDOWN_{}".format(dir_str, ev_type)
        if ev_type == "COMPLETE" and ev.get("perfect"):
            type_name = "{}_COUNTDOWN_COMPLETE_PERFECT".format(dir_str)

        # 当前事件所属的 countdown 实例（可能已是刚被 cancel/complete 的那一个）
        cd = self.active_countdown
        setup_info = cd.setup_info if cd is not None else {}
        tdst_threshold = cd._tdst_threshold if cd is not None else None
        countdown_start_dt = cd.start_bar_dt if cd is not None else None
        count_bars = cd.bars_at_count if cd is not None else []
        lowest_bar = self._lowest_low_bar(count_bars)

        record = {
            "datetime": bar.datetime,
            "security": self.security,
            "frequency": self.frequency,
            "category": "COUNTDOWN",
            "type": type_name,
            "direction": ev["direction"],
            "count": ev.get("count"),
            "perfect": ev.get("perfect", False) if ev_type == "COMPLETE" else "",
            "reason": ev.get("reason", "") if ev_type == "CANCEL" else "",
            # 扩展字段：回溯到 setup 起点 + TDST 阈值 + countdown 起点
            "setup_type": setup_info.get("setup_type", ""),
            "setup_perfect": setup_info.get("setup_perfect", ""),
            "setup_first_dt": setup_info.get("setup_first_dt", ""),
            "setup_last_dt": setup_info.get("setup_last_dt", ""),
            "setup_highest_high": setup_info.get("setup_highest_high", ""),
            "tdst_threshold": tdst_threshold if tdst_threshold is not None else "",
            "countdown_start_dt": countdown_start_dt or "",
            "countdown_8_close": count_bars[7].close if len(count_bars) >= 8 else "",
            "countdown_low_dt": lowest_bar.datetime if lowest_bar is not None else "",
            "countdown_low": lowest_bar.low if lowest_bar is not None else "",
            "countdown_low_bar_high": lowest_bar.high if lowest_bar is not None else "",
            "bar_close": bar.close,
            "bar_high": bar.high,
            "bar_low": bar.low,
        }
        for i in range(1, 14):
            record["count_{}_dt".format(i)] = count_bars[i - 1].datetime if len(count_bars) >= i else ""
        events_out.append(record)

        if ev_type == "PROGRESS":
            self.logger.countdown_step(
                "Countdown 进位",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction=dir_str, count=ev["count"],
            )
        elif ev_type == "PLUS_TENTATIVE":
            self.logger.debug(
                "Countdown 暂记 + (满足一般条件但不满足完美)",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime, direction=dir_str,
            )
        elif ev_type == "COMPLETE":
            self.logger.debug(
                "Countdown 完成",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction=dir_str, perfect=ev.get("perfect", False),
                setup_first_dt=record["setup_first_dt"],
            )
        elif ev_type == "CANCEL":
            self.logger.debug(
                "Countdown 取消",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction=dir_str, count=ev.get("count"), reason=ev.get("reason"),
            )

    @staticmethod
    def _lowest_low_bar(bars):
        if not bars:
            return None
        lowest = bars[0]
        for b in bars[1:]:
            if b.low < lowest.low:
                lowest = b
        return lowest


# =============================================================================
# [6] 信号与交易输出层
# =============================================================================

def _security_to_filename(security):
    """
    将标的代码转为文件名友好形式，剥离 '.' 等特殊字符。
    例: '600519.SS' -> '600519SS'
    """
    return str(security).replace(".", "").replace("/", "_")


def prepare_security_output_dirs(output_dir_rel, securities):
    """
    为每个标的创建独立输出目录：
        <run_dir>/<SEC>/
    其中 <SEC> 为剥离 '.' 后的股票代码。PTrade 禁用 os，因此使用 create_dir。
    """
    base_rel = output_dir_rel.rstrip("/\\")
    for security in securities:
        try:
            sec_rel = base_rel + "/" + _security_to_filename(security)
            if _is_local_project_path(output_dir_rel):
                pathlib = __import__("pathlib")
                pathlib.Path(sec_rel).mkdir(parents=True, exist_ok=True)
            else:
                create_dir(sec_rel)  # noqa: F821
        except Exception:
            # 目录存在或 create_dir 不可用时不阻断策略，后续写文件失败会在 recorder 中记录。
            pass


class MetadataRecorder:
    """
    轻量论文元数据输出。

    策略端只记录事件研究和交易复盘所需的原始事件，不在策略内做周期级统计。
    事件统一写到运行根目录 td_events.csv，避免大规模回测时生成大量中间 CSV。
    """

    HEADER_FIELDS = [
        "event_id", "datetime", "security", "frequency", "category", "type",
        "direction", "count", "perfect", "reason",
        "setup_type", "setup_perfect", "setup_first_dt", "setup_last_dt",
        "setup_highest_high", "tdst_threshold", "countdown_start_dt",
        "count_1_dt", "count_2_dt", "count_3_dt", "count_4_dt", "count_5_dt",
        "count_6_dt", "count_7_dt", "count_8_dt", "countdown_8_close",
        "count_9_dt", "count_10_dt", "count_11_dt", "count_12_dt", "count_13_dt",
        "countdown_low_dt", "countdown_low", "countdown_low_bar_high",
        "bar_close", "bar_high", "bar_low",
        "pre20_avg_amount", "pre20_volatility", "pre20_return",
    ]
    POSITION_HEADER_FIELDS = [
        "datetime", "event", "security", "trade_id",
        "open_trade_count", "buy_order_id", "buy_quantity", "buy_price",
        "buy_commission", "entry_value", "stop_loss_price", "take_profit_price",
        "sell_order_id", "sell_price", "sell_commission", "sell_reason", "pnl",
        "total_value", "available_cash",
    ]
    OUTCOME_HEADER_FIELDS = [
        "event_id", "event_datetime", "security", "type", "direction", "window",
        "matured_datetime", "base_close", "end_close",
        "directional_return", "mfe", "mae",
    ]

    def __init__(self, output_dir_abs, logger, enabled=True):
        """
        output_dir_abs: 绝对 / 本地输出目录路径（已经由 prepare_output_dir 创建）。
        """
        self.enabled = bool(enabled)
        self.logger = logger
        self.output_dir_abs = output_dir_abs.rstrip("/\\")
        self._header_written_paths = set()

    def _events_csv_path(self):
        return "{}/td_events.csv".format(self.output_dir_abs)

    def _position_csv_path(self):
        return "{}/position_snapshot.csv".format(self.output_dir_abs)

    def _outcomes_csv_path(self):
        return "{}/event_outcomes.csv".format(self.output_dir_abs)

    def write_events(self, events):
        if not self.enabled or not events:
            return
        self._write_rows(self._events_csv_path(), self.HEADER_FIELDS, events, "TD 事件元数据")

    def record_position(self, row):
        if not self.enabled:
            return
        self._write_rows(self._position_csv_path(), self.POSITION_HEADER_FIELDS, [row], "仓位元数据")

    def write_event_outcomes(self, rows):
        if not self.enabled or not rows:
            return
        self._write_rows(self._outcomes_csv_path(), self.OUTCOME_HEADER_FIELDS, rows, "事件窗口结果元数据")

    def _write_rows(self, path, header_fields, rows, label):
        try:
            need_header = self._need_header(path, header_fields)
            f = open(path, "a" if not need_header else "w", encoding="utf-8")
            try:
                if need_header:
                    f.write(",".join(header_fields) + "\n")
                for row in rows:
                    values = [self._csv_escape(row.get(k, "")) for k in header_fields]
                    f.write(",".join(values) + "\n")
            finally:
                f.close()
            self._header_written_paths.add(path)
            self.logger.debug("{}已写入".format(label), count=len(rows), path=path)
        except Exception as e:
            self.logger.error("写入{}失败".format(label), path=path, err=e)

    def _need_header(self, path, header_fields=None):
        if path in self._header_written_paths:
            return False
        fields = header_fields or self.HEADER_FIELDS
        try:
            f = open(path, "r", encoding="utf-8")
            try:
                first = f.readline()
            finally:
                f.close()
            if first.startswith(",".join(fields[:3])):
                self._header_written_paths.add(path)
                return False
            return True
        except Exception:
            return True

    @staticmethod
    def _csv_escape(val):
        """对可能含逗号的字段做简单转义（整体加双引号）。"""
        if val is None:
            return ""
        s = str(val)
        if "," in s or "\"" in s or "\n" in s:
            s = s.replace("\"", "\"\"")
            return "\"{}\"".format(s)
        return s


class TradeRecordRecorder:
    """
    记录一笔完整的"Buy Setup -> Buy Countdown -> 买入/未买入 -> 卖出/取消"生命周期。
    文件同时写入：
      * 按标的分流：600519SS/trade.csv
      * 本次运行汇总：trade.csv

    只在生命周期终态写入：
      * Buy Countdown 被取消
      * Buy Countdown 完成但交易被取消/跳过
      * 已买入仓位被卖出（止损、止盈、趋势反转、外部中止）
    """

    HEADER_FIELDS = [
        "datetime", "security",
        "setup_completed_at", "setup_is_perfect", "setup_highest_high",
        "count_1_at", "count_2_at", "count_3_at", "count_4_at", "count_5_at",
        "count_6_at", "count_7_at", "count_8_at", "count_8_close",
        "count_9_at", "count_10_at", "count_11_at", "count_12_at", "count_13_at",
        "countdown_completed_count", "countdown_is_perfect", "countdown_status",
        "bought", "buy_reject_reason",
        "buy_order_id", "buy_quantity", "buy_price", "buy_commission", "buy_date",
        "entry_value", "stop_loss_price", "take_profit_price",
        "sell_order_id", "sell_price", "sell_commission", "sell_date", "sell_reason",
        "price_pnl", "dividend_income", "pnl",
    ]

    def __init__(self, output_dir_abs, logger, enabled=True):
        self.enabled = bool(enabled)
        self.logger = logger
        self.output_dir_abs = output_dir_abs.rstrip("/\\")
        self._header_written_paths = set()

    def _csv_path_for(self, security):
        return "{}/{}/trade.csv".format(self.output_dir_abs, _security_to_filename(security))

    def _total_csv_path(self):
        return "{}/trade.csv".format(self.output_dir_abs)

    def record(self, row):
        if not self.enabled:
            return
        security = row.get("security", "unknown")
        security_path = self._csv_path_for(security)
        total_path = self._total_csv_path()

        self._write_row(row, security_path, security)
        self._write_row(row, total_path, security)

    def _write_row(self, row, path, security):
        try:
            need_header = self._need_header(path)
            f = open(path, "a" if not need_header else "w", encoding="utf-8")
            try:
                if need_header:
                    f.write(",".join(self.HEADER_FIELDS) + "\n")
                values = [MetadataRecorder._csv_escape(row.get(k, "")) for k in self.HEADER_FIELDS]
                f.write(",".join(values) + "\n")
            finally:
                f.close()
            self._header_written_paths.add(path)
            self.logger.debug("交易生命周期记录已写入", security=security, path=path)
        except Exception as e:
            self.logger.error("写入交易生命周期 CSV 失败", security=security, path=path, err=e)

    def _need_header(self, path):
        if path in self._header_written_paths:
            return False
        try:
            f = open(path, "r", encoding="utf-8")
            try:
                first = f.readline()
            finally:
                f.close()
            if first.startswith(",".join(self.HEADER_FIELDS[:3])):
                self._header_written_paths.add(path)
                return False
            return True
        except Exception:
            return True


def _format_dt(dt, fmt=DATETIME_FMT):
    """将任意 datetime / pandas.Timestamp 以文本形式统一格式化。失败则回退 str()。"""
    try:
        return dt.strftime(fmt)
    except Exception:
        return str(dt) if dt is not None else ""


# =============================================================================
# [7] 交易执行层
# =============================================================================
#
# 本层只负责"按意图下单 → 用 PTrade 真实成交回报回填记录 → 跟踪止盈止损 / 持仓事件"，
# **完全不知道 TD 9-13 / Setup / Countdown 等信号语义**。
#
# 上层（TDStrategyAdapter）通过 BuyIntent / SellIntent 与本层通讯，并以
# TradeExecutorCallbacks 接收成交、拒单、平仓回调，由它负责把这些事件翻译成
# trade.csv 中的完整 TD 生命周期记录。
# =============================================================================

SELL_REASON_STOP_LOSS = "STOP_LOSS"
SELL_REASON_PROFIT_TARGET = "PROFIT_TARGET"
SELL_REASON_TREND_REVERSAL = "TREND_REVERSAL"
SELL_REASON_EXTERNAL_ABORT = "EXTERNAL_ABORT"
SELL_REASON_MAX_HOLDING_DAYS = "MAX_HOLDING_DAYS"

# 通用买入拒绝原因（与具体策略无关，由交易层主动给出）。策略层可以在
# 自己的预检中再加更多原因（例如 NON_PERFECT_SETUP 等）。
BUY_REJECT_TRADE_DISABLED = "TRADE_DISABLED"
BUY_REJECT_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
BUY_REJECT_MIN_TRADE_VALUE = "MIN_TRADE_VALUE"
BUY_REJECT_ORDER_REJECTED = "ORDER_REJECTED"
BUY_REJECT_ORDER_ERROR = "ORDER_ERROR"
BUY_REJECT_TIMEOUT = "TIMEOUT"

# 卖出拒绝/异常原因
SELL_REJECT_TIMEOUT = "TIMEOUT"
SELL_REJECT_TERMINAL_NO_FILL = "TERMINAL_NO_FILL"
SELL_REJECT_INVALID_QTY = "INVALID_QTY"
SELL_REJECT_ORDER_REJECTED = "ORDER_REJECTED"
SELL_REJECT_ORDER_ERROR = "ORDER_ERROR"

DIVIDEND_TAX_RATE = 0.20

# PTrade 引擎硬编码的两类附加费率（见 set_commission 文档）：经手费按所有交易计、
# 印花税仅卖出时计。策略自算手续费时复用同一份常量，使 trade.csv 中的
# `*_commission` 与 PTrade 引擎实际扣款保持一致量级。
PTRADE_TRANSFER_FEE_RATIO = 0.0000487
PTRADE_STAMP_TAX_RATIO = 0.001


class BuyIntent:
    """策略层 → 交易层的买入意图。`metadata` 是不透明字典，交易层不解析。"""
    __slots__ = ("security", "target_value", "decision_dt", "fallback_price", "metadata")

    def __init__(self, security, target_value, decision_dt, fallback_price, metadata=None):
        self.security = security
        self.target_value = float(target_value)
        self.decision_dt = decision_dt
        self.fallback_price = float(fallback_price) if fallback_price else 0.0
        self.metadata = metadata or {}


class SellIntent:
    """策略层 → 交易层的卖出意图。`trade` 必须是交易层维护的某条 open_trade 引用。"""
    __slots__ = ("security", "trade", "sell_reason", "fallback_price", "metadata")

    def __init__(self, security, trade, sell_reason, fallback_price, metadata=None):
        self.security = security
        self.trade = trade
        self.sell_reason = sell_reason
        self.fallback_price = float(fallback_price) if fallback_price else 0.0
        self.metadata = metadata or {}


class FillInfo:
    """交易层 → 策略层：一笔成交的真实回报。"""
    __slots__ = ("order_id", "security", "side", "quantity", "price", "commission", "fill_dt")

    def __init__(self, order_id, security, side, quantity, price, commission, fill_dt):
        self.order_id = str(order_id) if order_id else ""
        self.security = security
        self.side = side  # "buy" or "sell"
        self.quantity = int(quantity)
        self.price = float(price) if price else 0.0
        self.commission = float(commission) if commission else 0.0
        self.fill_dt = fill_dt


class TradeExecutorCallbacks:
    """
    策略层实现该接口监听交易层事件。默认实现全部为 no-op，
    交易层不依赖任何返回值——回调里抛异常会被交易层日志记录但不会中断流程。
    """
    def on_buy_rejected(self, intent, reason): pass
    def on_buy_filled(self, intent, trade, fill): pass
    def on_sell_rejected(self, intent, reason): pass
    def on_sell_filled(self, intent, trade, fill, pnl_breakdown): pass
    def on_position_adjusted(self, security, trade, dt_text, ratio, reason): pass


class TradeExecutor:
    """
    通用交易执行层。职责：
      1. 接收 BuyIntent / SellIntent；
      2. 调用 PTrade `order_value` / `order` 提交委托并挂入 pending 队列；
      3. 每周期通过 `reconcile_pending_orders` 用 PTrade 真实成交回报落账；
      4. 跟踪每一笔未平仓交易的止损价 / 止盈价 / 持仓天数 / 归属分红；
      5. 在回测中 `check_backtest_exits` 用上一根 K 线高低判断风控触发；
         在实盘中 `check_tick_exits` 逐 tick 触发；
      6. 处理除权送股、现金分红同步；
      7. 通过 TradeExecutorCallbacks 把成交 / 拒单 / 平仓事件回调给策略层。

    本类**不依赖任何信号定义**（不知道 TD / Setup / Countdown），所有策略相关
    的 trade.csv 行写入工作由策略层（TDStrategyAdapter）完成。
    """

    def __init__(
        self, cfg_trade, logger, callbacks=None, metadata_recorder=None,
        commission_ratio=0.0003, min_commission=5.0,
        transfer_fee_ratio=PTRADE_TRANSFER_FEE_RATIO,
        stamp_tax_ratio=PTRADE_STAMP_TAX_RATIO,
    ):
        self.enabled = bool(cfg_trade.get("enabled", True))
        self.buy_fraction = float(cfg_trade.get("buy_fraction_of_portfolio", 0.1))
        self.min_trade_value = float(cfg_trade.get("min_trade_value", 1000))
        self.min_cash_for_buy = float(cfg_trade.get("min_cash_for_buy", 1000))
        self.stop_loss_enabled = bool(cfg_trade.get("stop_loss_enabled", True))
        self.max_holding_days_enabled = bool(cfg_trade.get("max_holding_days_enabled", True))
        self.max_holding_days = int(cfg_trade.get("max_holding_days", 400))
        # ----- 手续费配置（与 PTrade set_commission 共用同一份费率） -----
        # PTrade Python API 不暴露 commission 字段，策略需要按这份配置自算，
        # 让 trade.csv 中的 buy_commission / sell_commission 与 PTrade 引擎扣款一致。
        # `transfer_fee_ratio`、`stamp_tax_ratio` 默认硬编码为 PTrade 文档中的固定值，
        # 留参数主要为了离线工具或单元测试场景下能注入不同值。
        self.commission_ratio = float(commission_ratio)
        self.min_commission = float(min_commission)
        self.transfer_fee_ratio = float(transfer_fee_ratio)
        self.stamp_tax_ratio = float(stamp_tax_ratio)
        # ----- 超时未成交订单保护 -----
        # 同一笔订单挂在 pending 队列里 N 个自然日仍未拿到 PTrade 终态时：
        #   1. 第一次到达 `pending_order_timeout_days` 时主动 cancel_order，
        #      并把 timeout_cancel_attempted 标记上，下一周期 reconcile 看到
        #      撤单后的终态会走正常的清理路径；
        #   2. 若到达 `pending_order_force_drop_days` 仍未消化（说明撤单也没生效），
        #      强制摘除 pending 项并触发 on_*_rejected(TIMEOUT)，避免后续止损
        #      因为 _has_pending_sell_for_trade 而被永久阻塞。
        # 任意一个值 <= 0 即视为关闭对应保护。
        self.pending_order_timeout_days = int(cfg_trade.get("pending_order_timeout_days", 5))
        self.pending_order_force_drop_days = int(cfg_trade.get(
            "pending_order_force_drop_days",
            max(self.pending_order_timeout_days * 2, self.pending_order_timeout_days + 1),
        ))
        self.logger = logger
        self.callbacks = callbacks or TradeExecutorCallbacks()
        self.metadata_recorder = metadata_recorder

        # 每个标的可同时跟踪多笔完整交易；每笔买入独立计算止损/止盈。
        self.open_trades = {}
        self.pending_buy_orders = {}
        self.pending_sell_orders = {}
        self._processed_dividend_keys = set()
        self._processed_share_adjustment_keys = set()
        self._next_trade_id = 1
        self._last_portfolio_snapshot = None

    def set_callbacks(self, callbacks):
        """运行时绑定策略层回调（initialize() 中常用：先建 executor，再建 adapter 注册）。"""
        self.callbacks = callbacks or TradeExecutorCallbacks()

    def _safe_callback(self, name, *args, **kwargs):
        """统一的回调调用入口；任何回调异常都不会传染到交易层。"""
        cb = getattr(self.callbacks, name, None)
        if cb is None:
            return
        try:
            cb(*args, **kwargs)
        except Exception as e:
            self.logger.error("策略层回调异常", callback=name, err=e)

    def check_backtest_exits(self, context, security, completed_bar):
        """
        回测降级：用上一根已完成 K 线的 high/low 判断止盈止损。
        注意：刚根据该 completed_bar 信号买入的仓位，会把 last_checked_bar_dt 初始化为
        signal datetime，因此不会在同一根历史 K 上立刻被止盈/止损。

        卖出价 / 卖出时间一律以 PTrade 真实成交回报为准记录；止损 / 止盈阈值仅
        作为触发条件，不再作为 trade.csv 中的成交价。
        """
        if self._is_live_trade() or not self._open_trade_list(security):
            return
        bar_dt = completed_bar.datetime
        for trade in list(self._open_trade_list(security)):
            if trade.get("last_checked_bar_dt") and bar_dt <= trade.get("last_checked_bar_dt"):
                continue

            stop_loss = self._safe_float(trade.get("stop_loss_price", 0.0))
            take_profit = self._safe_float(trade.get("take_profit_price", 0.0))
            holding_days = self._holding_days(trade, bar_dt)

            if self._should_exit_by_holding_days(holding_days):
                self.submit_sell(context, SellIntent(
                    security=security, trade=trade,
                    sell_reason=SELL_REASON_MAX_HOLDING_DAYS,
                    fallback_price=completed_bar.close,
                ))
                continue

            # 同一根 K 线同时触发时，保守按先止损处理。
            if self.stop_loss_enabled and stop_loss > 0 and completed_bar.low <= stop_loss:
                self.submit_sell(context, SellIntent(
                    security=security, trade=trade,
                    sell_reason=SELL_REASON_STOP_LOSS,
                    fallback_price=stop_loss,
                ))
                continue
            if take_profit > 0 and completed_bar.high >= take_profit:
                self.submit_sell(context, SellIntent(
                    security=security, trade=trade,
                    sell_reason=SELL_REASON_PROFIT_TARGET,
                    fallback_price=take_profit,
                ))
                continue

            trade["last_checked_bar_dt"] = bar_dt

    def accrue_dividends(self, security, dt_text):
        """把当天现金分红按逐笔未平仓数量累计到 open trades。"""
        trades = self._open_trade_list(security)
        if not trades:
            return
        date_key = self._date_key(dt_text)
        if not date_key:
            return
        processed_key = "{}|{}".format(security, date_key)
        if processed_key in self._processed_dividend_keys:
            return
        bonus_ps = self._get_bonus_ps(security, date_key)
        self._processed_dividend_keys.add(processed_key)
        if bonus_ps <= 0:
            return

        after_tax_bonus = bonus_ps * (1.0 - DIVIDEND_TAX_RATE)
        total_income = 0.0
        for trade in trades:
            qty = self._safe_float(trade.get("buy_quantity"))
            if qty <= 0:
                continue
            income = qty * after_tax_bonus
            trade["dividend_income"] = round(self._safe_float(trade.get("dividend_income")) + income, 2)
            total_income += income
        if total_income > 0:
            self.logger.info(
                "现金分红已计入逐笔交易",
                security=security,
                date=date_key,
                bonus_ps=round(bonus_ps, 6),
                after_tax_bonus=round(after_tax_bonus, 6),
                income=round(total_income, 2),
            )

    def reconcile_position_adjustments(self, context, security, dt_text):
        """
        同步除权送股后的逐笔交易数量和风控价格。

        优先使用 get_stock_exrights 的 allotted_ps 明确处理送股/转增；
        账户真实持仓数量只作为兜底校验，避免接口缺失或字段异常时残留仓位。
        """
        trades = self._open_trade_list(security)
        if not trades:
            return
        if self._has_pending_sell_for_security(security):
            return

        date_key = self._date_key(dt_text)
        if date_key:
            processed_key = "{}|{}".format(security, date_key)
            if processed_key not in self._processed_share_adjustment_keys:
                ratio = self._get_share_adjustment_ratio(security, date_key)
                self._processed_share_adjustment_keys.add(processed_key)
                if ratio > 1.0:
                    self._apply_position_adjustment(
                        context=context,
                        security=security,
                        dt_text=dt_text,
                        ratio=ratio,
                        reason="EXRIGHTS_ALLOTTED_PS",
                    )

        # 兜底：若账户股数仍显著大于内部逐笔仓位，说明还有未显式处理的分股类调整。
        account_qty, _, _ = self._get_position_detail(security)
        internal_qty = self._open_quantity(security)
        if account_qty <= 0 or internal_qty <= 0:
            return
        diff = account_qty - internal_qty
        if diff <= max(1.0, internal_qty * 0.001):
            return
        ratio = account_qty / internal_qty
        if ratio <= 1.0:
            return
        self._apply_position_adjustment(
            context=context,
            security=security,
            dt_text=dt_text,
            ratio=ratio,
            reason="ACCOUNT_QTY_RECONCILE",
            account_qty=account_qty,
            before_internal_qty=internal_qty,
            warning=True,
        )

    def _apply_position_adjustment(
        self, context, security, dt_text, ratio, reason, account_qty="", before_internal_qty="", warning=False,
    ):
        trades = self._open_trade_list(security)
        if not trades or ratio <= 1.0:
            return
        self._last_portfolio_snapshot = self._portfolio_snapshot(context)
        adjusted_rows = []
        for trade in trades:
            before_qty = self._safe_float(trade.get("buy_quantity"))
            if before_qty <= 0:
                continue
            before_price = self._safe_float(trade.get("buy_price"))
            before_stop = self._safe_float(trade.get("stop_loss_price"))
            before_take = self._safe_float(trade.get("take_profit_price"))
            before_entry = self._safe_float(trade.get("entry_value"))

            after_qty = before_qty * ratio
            after_price = before_price / ratio if before_price > 0 else before_price
            after_stop = before_stop / ratio if before_stop > 0 else before_stop
            after_take = before_take / ratio if before_take > 0 else before_take
            entry_value = before_entry if before_entry > 0 else after_qty * after_price

            trade["buy_quantity"] = int(round(after_qty))
            trade["buy_price"] = round(after_price, 4) if after_price > 0 else trade.get("buy_price", "")
            trade["entry_value"] = round(entry_value, 2)
            if before_stop > 0:
                trade["stop_loss_price"] = round(after_stop, 4)
            if before_take > 0:
                trade["take_profit_price"] = round(after_take, 4)
            self._record_position_snapshot("ADJUST", security, trade, dt_text, sell_price="", sell_reason="", pnl="")
            self._safe_callback("on_position_adjusted", security, trade, dt_text, ratio, reason)
            adjusted_rows.append((trade.get("_trade_id"), before_qty, trade["buy_quantity"]))

        if adjusted_rows:
            log_kwargs = {
                "security": security,
                "reason": reason,
                "ratio": round(ratio, 6),
                "lots": len(adjusted_rows),
            }
            if account_qty != "":
                log_kwargs["account_qty"] = round(account_qty, 4)
            if before_internal_qty != "":
                log_kwargs["before_internal_qty"] = round(before_internal_qty, 4)
            if warning:
                self.logger.warning("通过账户持仓差额兜底同步逐笔仓位，请检查 get_stock_exrights 是否漏掉分股事件", **log_kwargs)
            else:
                self.logger.info("同步除权送股后的逐笔仓位", **log_kwargs)

    def check_tick_exits(self, context, tick_data_obj):
        """实盘 tick_data 中追踪止盈止损。回测环境不执行。"""
        if not self._is_live_trade():
            return
        for security in list(self.open_trades.keys()):
            self.reconcile_position_adjustments(context, security, _format_dt(self._current_dt(context)))
            price = self._extract_tick_price(tick_data_obj, security)
            if price <= 0:
                continue
            for trade in list(self._open_trade_list(security)):
                holding_days = self._holding_days(trade, self._current_dt(context))
                if self._should_exit_by_holding_days(holding_days):
                    self.submit_sell(context, SellIntent(
                        security=security, trade=trade,
                        sell_reason=SELL_REASON_MAX_HOLDING_DAYS, fallback_price=price,
                    ))
                    continue
                if self.stop_loss_enabled and price <= self._safe_float(trade.get("stop_loss_price", 0.0)):
                    self.submit_sell(context, SellIntent(
                        security=security, trade=trade,
                        sell_reason=SELL_REASON_STOP_LOSS, fallback_price=price,
                    ))
                    continue
                if price >= self._safe_float(trade.get("take_profit_price", 0.0)):
                    self.submit_sell(context, SellIntent(
                        security=security, trade=trade,
                        sell_reason=SELL_REASON_PROFIT_TARGET, fallback_price=price,
                    ))

    def on_trade_response(self, context, trade_response):
        """
        实盘成交回报：直接走 reconcile_pending_orders，复用同一套"以 PTrade
        get_order / get_trades 真实成交结果为准"的写入逻辑，避免实盘 / 回测
        两条不同的写入路径让 trade.csv 出现差异。
        """
        if not self._is_live_trade():
            return
        self.reconcile_pending_orders(context)

    # ----- Pending order reconciliation -----
    def reconcile_pending_orders(self, context):
        """
        核心对账入口：扫描所有挂起的买 / 卖订单，使用 PTrade 真实成交记录
        （`get_order` / `get_trades`）写入 trade.csv 与 position_snapshot.csv。

        - 同一笔订单可能跨多个 handle_data 周期才完全成交（PTrade 异步撮合），
          这里维护"已记录数量 / 已记录金额 / 已记录手续费"的累计游标，
          每周期只把"新增成交"那部分追加为新的开仓 / 平仓事件。
        - 订单进入终态（全部成交 / 部分成交后撤单 / 完全撤单）时再从 pending 摘除；
          若一直没拿到 get_order 数据就保持挂起，下一个周期继续轮询。
        - 超时未成交时由 `_handle_pending_timeouts` 主动 cancel_order 并兜底摘除。
        """
        if not self.pending_buy_orders and not self.pending_sell_orders:
            return
        for order_id in list(self.pending_buy_orders.keys()):
            try:
                self._settle_pending_buy(context, order_id)
            except Exception as e:
                self.logger.error("结算挂起买单异常", order_id=order_id, err=e)
        for order_id in list(self.pending_sell_orders.keys()):
            try:
                self._settle_pending_sell(context, order_id)
            except Exception as e:
                self.logger.error("结算挂起卖单异常", order_id=order_id, err=e)
        # 仅对仍存在的 pending 进行超时处理（已经被 settle 摘除的不再扫描）。
        self._handle_pending_timeouts(context)

    # ----- Pending order timeout protection -----
    def _handle_pending_timeouts(self, context):
        """
        分两阶段处理超时挂单：
          1. 软超时（`pending_order_timeout_days`）：第一次到达时主动 cancel_order
             并 `timeout_cancel_attempted=True`，期望下一周期 reconcile 看到撤单
             终态后走正常清理路径；
          2. 硬超时（`pending_order_force_drop_days`）：撤单仍没生效或 PTrade
             根本没暴露终态——强制摘除 pending 项，并触发 on_*_rejected(TIMEOUT)，
             否则后续 _has_pending_sell_for_trade 会把同一笔仓位的止损永久封死。
        """
        if self.pending_order_timeout_days <= 0 and self.pending_order_force_drop_days <= 0:
            return
        if not self.pending_buy_orders and not self.pending_sell_orders:
            return
        now_dt = self._parse_dt(self._current_dt(context))
        if now_dt is None:
            return

        for order_id in list(self.pending_buy_orders.keys()):
            self._maybe_timeout_pending(context, "buy", order_id, now_dt)
        for order_id in list(self.pending_sell_orders.keys()):
            self._maybe_timeout_pending(context, "sell", order_id, now_dt)

    def _maybe_timeout_pending(self, context, side, order_id, now_dt):
        bucket = self.pending_buy_orders if side == "buy" else self.pending_sell_orders
        pending = bucket.get(order_id)
        if pending is None:
            return
        elapsed_days = self._pending_elapsed_days(pending, now_dt)
        if elapsed_days is None:
            return

        # 硬超时：直接强制摘除，并触发 timeout 拒绝回调。
        if (self.pending_order_force_drop_days > 0
                and elapsed_days >= self.pending_order_force_drop_days):
            self._force_drop_pending(side, order_id, pending, elapsed_days)
            return

        # 软超时：发起一次撤单，等下一周期 reconcile 收尾。
        if (self.pending_order_timeout_days > 0
                and elapsed_days >= self.pending_order_timeout_days
                and not pending.get("timeout_cancel_attempted")):
            self._cancel_timeout_pending(side, order_id, pending, elapsed_days)

    def _pending_elapsed_days(self, pending, now_dt):
        submit_dt = self._parse_dt(pending.get("submit_dt"))
        if submit_dt is None:
            return None
        try:
            return max(0, (now_dt.date() - submit_dt.date()).days)
        except Exception:
            return None

    def _try_cancel_order(self, order_id):
        """
        发起一次撤单。兼容两种 cancel_order 签名：
          - PTrade：`cancel_order(order_id_str)`
          - SimTradeLab：`cancel_order(order_obj)`
        任一签名成功即视为撤单已发出（最终是否真的撤掉由下个周期 reconcile 验证）。
        """
        try:
            cancel_order(str(order_id))  # noqa: F821 - PTrade 注入
            return True
        except Exception:
            pass
        try:
            ord_obj = get_order(str(order_id))  # noqa: F821 - PTrade 注入
            if ord_obj is not None:
                cancel_order(ord_obj)  # noqa: F821 - PTrade 注入
                return True
        except Exception as e:
            self.logger.warning("尝试 cancel_order 失败（已尝试两种签名）", order_id=order_id, err=e)
        return False

    def _cancel_timeout_pending(self, side, order_id, pending, elapsed_days):
        intent = pending["intent"]
        filled_recorded = self._safe_float(pending.get("filled_qty_recorded", 0.0))
        ok = self._try_cancel_order(order_id)
        pending["timeout_cancel_attempted"] = True
        self.logger.warning(
            "委托超时未成交，已发起撤单等待下一周期收尾",
            side=side, security=intent.security, order_id=order_id,
            submit_dt=pending.get("submit_dt"),
            elapsed_days=elapsed_days,
            timeout_days=self.pending_order_timeout_days,
            filled_qty_recorded=filled_recorded,
            cancel_ok=ok,
            sell_reason=getattr(intent, "sell_reason", None) if side == "sell" else None,
        )

    def _force_drop_pending(self, side, order_id, pending, elapsed_days):
        intent = pending["intent"]
        filled_recorded = self._safe_float(pending.get("filled_qty_recorded", 0.0))
        # 再尝试一次撤单——尽管很可能已经撤过了，反复 cancel 是幂等的。
        self._try_cancel_order(order_id)
        self.logger.error(
            "委托长时间无终态，强制摘除挂单追踪；后续 PTrade 若再回成交将无法落账，请人工对账",
            side=side, security=intent.security, order_id=order_id,
            submit_dt=pending.get("submit_dt"),
            elapsed_days=elapsed_days,
            force_drop_days=self.pending_order_force_drop_days,
            filled_qty_recorded=filled_recorded,
            sell_reason=getattr(intent, "sell_reason", None) if side == "sell" else None,
        )
        if side == "buy":
            if filled_recorded <= 0:
                self._safe_callback("on_buy_rejected", intent, BUY_REJECT_TIMEOUT)
            self.pending_buy_orders.pop(order_id, None)
        else:
            if filled_recorded <= 0:
                self._safe_callback("on_sell_rejected", intent, SELL_REJECT_TIMEOUT)
            # 卖单 force-drop 时仓位仍在 open_trades 中——下个周期止损/止盈
            # 触发器会重新评估并发起新的卖出意图。
            self.pending_sell_orders.pop(order_id, None)

    def _settle_pending_buy(self, context, order_id):
        pending = self.pending_buy_orders.get(order_id)
        if pending is None:
            return
        intent = pending["intent"]
        state = self._fetch_order_state(context, order_id, intent.security)
        if state is None:
            return  # 还没拿到订单信息，下一周期再试

        recorded_qty = self._safe_float(pending.get("filled_qty_recorded", 0.0))
        new_filled = state["filled_qty"] - recorded_qty

        if new_filled > 0:
            recorded_gross = self._safe_float(pending.get("recorded_gross", 0.0))
            recorded_commission = self._safe_float(pending.get("recorded_commission", 0.0))
            delta_gross = max(0.0, state["gross_value"] - recorded_gross)
            # 计算单股价格：优先用本次新增成交的金额平均；兜底依次为
            # Order.limit/PTrade get_trades 价格 → 提交时的 fallback_price（昨收）。
            avg_price = 0.0
            if new_filled > 0 and delta_gross > 0:
                avg_price = delta_gross / new_filled
            if avg_price <= 0:
                avg_price = self._safe_float(state.get("price_hint"))
            if avg_price <= 0:
                avg_price = self._safe_float(pending.get("fallback_price"))
            if delta_gross <= 0 and avg_price > 0:
                delta_gross = avg_price * new_filled
            # 手续费：PTrade Python API 不暴露，state["commission"] 可能为 0；
            # 此时按 _estimate_commission 自算，使 trade.csv 与 PTrade 引擎对齐。
            delta_commission = max(0.0, state["commission"] - recorded_commission)
            if delta_commission <= 0 and delta_gross > 0:
                delta_commission = self._estimate_commission("buy", delta_gross)
            fill_dt = (
                _format_dt(state["fill_dt"]) or pending.get("submit_dt") or
                _format_dt(self._current_dt(context))
            )

            self._open_trade_from_fill(
                intent, avg_price, int(new_filled), fill_dt,
                buy_order_id=order_id, buy_commission=delta_commission,
            )
            self.logger.info(
                "买入成交回报已记录",
                security=intent.security, order_id=order_id,
                fill_qty=int(new_filled), fill_price=round(avg_price, 4),
                fill_commission=round(delta_commission, 4), fill_dt=fill_dt,
            )

            # 累计游标用"max(实际累计, 推算累计)"——PTrade 没暴露 gross/commission
            # 时 state 给的是 0，避免下次 reconcile 把已记账的 delta 又算一次。
            pending["filled_qty_recorded"] = state["filled_qty"]
            pending["recorded_gross"] = max(
                self._safe_float(pending.get("recorded_gross", 0.0)) + delta_gross,
                state["gross_value"],
            )
            pending["recorded_commission"] = max(
                self._safe_float(pending.get("recorded_commission", 0.0)) + delta_commission,
                state["commission"],
            )

        if state["is_terminal"]:
            if self._safe_float(pending.get("filled_qty_recorded", 0.0)) <= 0:
                self.logger.info(
                    "买入委托终态未成交",
                    security=intent.security, order_id=order_id,
                )
                self._safe_callback("on_buy_rejected", intent, BUY_REJECT_ORDER_REJECTED)
            self.pending_buy_orders.pop(order_id, None)

    def _settle_pending_sell(self, context, order_id):
        pending = self.pending_sell_orders.get(order_id)
        if pending is None:
            return
        intent = pending["intent"]
        state = self._fetch_order_state(context, order_id, intent.security)
        if state is None:
            return

        recorded_qty = self._safe_float(pending.get("filled_qty_recorded", 0.0))
        new_filled = state["filled_qty"] - recorded_qty

        if new_filled > 0:
            recorded_gross = self._safe_float(pending.get("recorded_gross", 0.0))
            recorded_commission = self._safe_float(pending.get("recorded_commission", 0.0))
            delta_gross = max(0.0, state["gross_value"] - recorded_gross)
            avg_price = 0.0
            if new_filled > 0 and delta_gross > 0:
                avg_price = delta_gross / new_filled
            if avg_price <= 0:
                avg_price = self._safe_float(state.get("price_hint"))
            if avg_price <= 0:
                avg_price = self._safe_float(pending.get("fallback_price"))
            if delta_gross <= 0 and avg_price > 0:
                delta_gross = avg_price * new_filled
            delta_commission = max(0.0, state["commission"] - recorded_commission)
            if delta_commission <= 0 and delta_gross > 0:
                delta_commission = self._estimate_commission("sell", delta_gross)
            fill_dt = (
                _format_dt(state["fill_dt"]) or pending.get("submit_dt") or
                _format_dt(self._current_dt(context))
            )

            self._last_portfolio_snapshot = self._portfolio_snapshot(context)
            self._finalize_sell(
                intent, avg_price, fill_dt,
                sell_qty=int(new_filled), sell_order_id=order_id,
                sell_commission=delta_commission,
            )
            self.logger.info(
                "卖出成交回报已记录",
                security=intent.security, order_id=order_id,
                fill_qty=int(new_filled), fill_price=round(avg_price, 4),
                fill_commission=round(delta_commission, 4), fill_dt=fill_dt,
            )

            pending["filled_qty_recorded"] = state["filled_qty"]
            pending["recorded_gross"] = max(
                self._safe_float(pending.get("recorded_gross", 0.0)) + delta_gross,
                state["gross_value"],
            )
            pending["recorded_commission"] = max(
                self._safe_float(pending.get("recorded_commission", 0.0)) + delta_commission,
                state["commission"],
            )

        if state["is_terminal"]:
            if self._safe_float(pending.get("filled_qty_recorded", 0.0)) <= 0:
                self.logger.warning(
                    "卖出委托终态未成交，仓位维持开仓状态",
                    security=intent.security, order_id=order_id,
                    sell_reason=intent.sell_reason,
                )
                self._safe_callback("on_sell_rejected", intent, SELL_REJECT_TERMINAL_NO_FILL)
            self.pending_sell_orders.pop(order_id, None)

    def _fetch_order_state(self, context, order_id, security):
        """
        从 PTrade / SimTradeLab 拉取一笔订单的当前状态。
        返回 dict（含 filled_qty / total_amount / gross_value / commission /
        fill_dt / is_terminal / status / price_hint）；拿不到任何信息时返回 None。

        关键设计：
          * **PTrade `get_order(order_id)` 返回的是 `list[Order]`**（见官方文档 5742 行），
            字段为 `id / dt / limit / symbol / amount / filled / status / entrust_no`，
            **没有** business_amount / business_price / business_balance / commission；
            SimTradeLab 返回的是单个 Order 对象，字段名也不一定相同。
            这里统一展开成同名 key 后再读，避免 isinstance(list) 时被当成无属性对象。
          * **PTrade 不暴露真实成交价 / 手续费**：成交价用 `limit` 兜底，手续费由
            `_estimate_commission` 按同步的费率公式自算（见 _settle_pending_buy/sell 用法）。
          * `get_trades` 的返回结构两边完全不同，由 `_aggregate_trades_for_order` 处理。
        """
        ord_obj = None
        try:
            ord_obj = get_order(str(order_id))  # noqa: F821 - PTrade 注入
        except Exception:
            ord_obj = None

        ord_records = self._normalize_order_objects(ord_obj)

        filled_qty = 0.0
        total_amount = 0.0
        gross_value = 0.0
        commission = 0.0
        fill_dt = None
        status_text = ""
        price_hint = 0.0
        found = False

        for rec in ord_records:
            found = True
            # PTrade Order: filled / amount / status / entrust_no / limit / dt
            # SimTradeLab Order: 字段名兼容多套（business_amount/filled/filled_amount...）
            filled = abs(self._safe_float(self._read_any(
                rec, ["filled", "business_amount", "filled_amount", "trade_amount"],
            )))
            total = abs(self._safe_float(self._read_any(
                rec, ["amount", "entrust_amount", "volume"],
            )))
            limit_price = self._safe_float(self._read_any(
                rec, ["business_price", "filled_price", "trade_price", "avg_price", "limit_price", "limit"],
            ))
            balance = self._safe_float(self._read_any(
                rec, ["business_balance", "trade_balance"],
            ))
            comm = self._safe_float(self._read_any(
                rec, ["commission", "fee", "fees", "transfer_fee"],
            )) + self._safe_float(self._read_any(rec, ["stamp_tax", "tax"]))
            dt_value = self._read_any(
                rec, ["business_time", "trade_time", "filled_time", "dt", "datetime", "created"],
            )
            status_value = self._read_any(rec, ["status", "order_status", "state"])

            filled_qty = max(filled_qty, filled)
            if total > total_amount:
                total_amount = total
            if balance > gross_value:
                gross_value = balance
            elif filled > 0 and limit_price > 0:
                gross_value = max(gross_value, filled * limit_price)
            if comm > commission:
                commission = comm
            if dt_value is not None:
                fill_dt = dt_value
            if status_value:
                status_text = str(status_value)
            if limit_price > 0:
                price_hint = limit_price

        # 用 get_trades 的逐笔成交补齐——尤其是 PTrade，需要从这里拿真实成交价。
        trades_qty, trades_value, trades_comm, trades_dt, trades_price = self._aggregate_trades_for_order(
            security, order_id,
        )
        if trades_qty > filled_qty:
            found = True
            filled_qty = trades_qty
        if trades_value > gross_value:
            gross_value = trades_value
        if trades_comm > commission:
            commission = trades_comm
        if trades_dt is not None:
            fill_dt = trades_dt
        if trades_price > 0:
            price_hint = trades_price

        if not found:
            return None

        if total_amount <= 0:
            total_amount = filled_qty

        return {
            "filled_qty": filled_qty,
            "total_amount": total_amount,
            "gross_value": gross_value,
            "commission": commission,
            "fill_dt": fill_dt,
            "price_hint": price_hint,
            "is_terminal": self._is_order_terminal(status_text, filled_qty, total_amount),
            "status": status_text,
        }

    @staticmethod
    def _normalize_order_objects(raw):
        """
        统一把 `get_order` / `get_orders` 的返回值规约为 `list[record]` 形式：
          * PTrade：`list[Order]` -> 直接返回
          * SimTradeLab：单 Order 对象 -> 包成 list
          * dict（极少见但兼容）：包成 list
          * None / 空 -> []
        """
        if raw is None:
            return []
        if isinstance(raw, (list, tuple)):
            return [r for r in raw if r is not None]
        return [raw]

    def _aggregate_trades_for_order(self, security, order_id):
        """
        按 entrust_no 把 `get_trades()` 的成交流水按订单聚合。
        返回 (sum_qty, sum_value, sum_commission, last_dt, avg_price)。

        两套引擎返回结构不同：
          * **PTrade `get_trades()`**（无参）返回 `dict{order_id: list[list]}`，
            list 字段位置式：`[成交编号, 委托编号, 标的代码, 买卖类型, 成交数量,
            成交价格, 成交金额, 成交时间]`，**没有 commission / 手续费字段**。
          * **SimTradeLab `get_trades(security)`** 返回 `list[dict]`，dict 字段
            包含 business_amount / business_price / business_balance / commission 等。

        手续费两套都不一定有真实值——PTrade 完全没有，SimTradeLab 视版本而定。
        统一让 `_settle_pending_*` 在 commission <= 0 时用 `_estimate_commission` 兜底。
        """
        target = str(order_id)
        # 先尝试无参（PTrade 唯一签名），再回退带 security 的形式（SimTradeLab）。
        trades = None
        try:
            trades = get_trades()  # noqa: F821 - PTrade 注入
        except Exception:
            trades = None
        if trades is None or self._is_empty_trades(trades):
            try:
                trades = get_trades(security)  # noqa: F821 - PTrade 注入
            except Exception:
                trades = None

        if trades is None or self._is_empty_trades(trades):
            return 0.0, 0.0, 0.0, None, 0.0

        sum_qty = 0.0
        sum_value = 0.0
        sum_commission = 0.0
        last_dt = None

        if isinstance(trades, dict):
            # PTrade：dict[order_id] = list[list(8字段)]
            entries = trades.get(target) or trades.get(str(target))
            # 有些 PTrade 版本会用证券代码而非 order_id 当 key——退化为对所有
            # value 检查 entrust_no（位置 1）。
            if entries is None:
                for _, value in trades.items():
                    for row in (value or []):
                        if self._ptrade_trade_entrust_no(row) == target:
                            qty, price, value_amt, dt_val = self._ptrade_trade_fields(row)
                            sum_qty += qty
                            if value_amt > 0:
                                sum_value += value_amt
                            elif qty > 0 and price > 0:
                                sum_value += qty * price
                            if dt_val is not None:
                                last_dt = dt_val
                # PTrade 不暴露 commission，留 0 由策略自算。
            else:
                for row in entries:
                    qty, price, value_amt, dt_val = self._ptrade_trade_fields(row)
                    sum_qty += qty
                    if value_amt > 0:
                        sum_value += value_amt
                    elif qty > 0 and price > 0:
                        sum_value += qty * price
                    if dt_val is not None:
                        last_dt = dt_val
        else:
            # SimTradeLab：list[dict]
            for t in trades:
                t_id = self._read_any(t, ["entrust_no", "order_id", "id"])
                if not t_id or str(t_id) != target:
                    continue
                qty = abs(self._safe_float(self._read_any(
                    t, ["business_amount", "amount", "trade_amount", "filled"],
                )))
                price = self._safe_float(self._read_any(
                    t, ["business_price", "trade_price", "filled_price", "price", "limit"],
                ))
                balance = self._safe_float(self._read_any(
                    t, ["business_balance", "trade_balance"],
                ))
                comm = self._safe_float(self._read_any(
                    t, ["commission", "fee", "fees", "transfer_fee"],
                )) + self._safe_float(self._read_any(t, ["stamp_tax", "tax"]))
                dt_val = self._read_any(t, ["business_time", "trade_time", "filled_time", "dt", "datetime"])

                sum_qty += qty
                if balance > 0:
                    sum_value += balance
                elif qty > 0 and price > 0:
                    sum_value += qty * price
                sum_commission += comm
                if dt_val is not None:
                    last_dt = dt_val

        avg_price = sum_value / sum_qty if sum_qty > 0 and sum_value > 0 else 0.0
        return sum_qty, sum_value, sum_commission, last_dt, avg_price

    @staticmethod
    def _is_empty_trades(trades):
        try:
            if isinstance(trades, dict):
                return len(trades) == 0
            return not trades
        except Exception:
            return False

    @staticmethod
    def _ptrade_trade_entrust_no(row):
        """PTrade `get_trades()` value 中每一行 list 的 [1] 位置是委托编号。"""
        try:
            return str(row[1]) if row is not None and len(row) >= 2 else ""
        except Exception:
            return ""

    @staticmethod
    def _ptrade_trade_fields(row):
        """
        解析 PTrade `get_trades()` 中位置式 list：
            [成交编号, 委托编号, 标的代码, 买卖类型, 成交数量, 成交价格, 成交金额, 成交时间]
        """
        try:
            qty = abs(float(row[4])) if len(row) > 4 else 0.0
        except Exception:
            qty = 0.0
        try:
            price = float(row[5]) if len(row) > 5 else 0.0
        except Exception:
            price = 0.0
        try:
            value = abs(float(row[6])) if len(row) > 6 else 0.0
        except Exception:
            value = 0.0
        dt_val = row[7] if len(row) > 7 else None
        return qty, price, value, dt_val

    def _estimate_commission(self, side, gross_value):
        """
        按 PTrade `set_commission` 同款公式自算手续费。
            * 佣金费 = max(commission_ratio * gross, min_commission)
            * 经手费 = transfer_fee_ratio * gross
            * 印花税 = stamp_tax_ratio * gross （仅卖出）
        side: "buy" / "sell"。
        gross_value: 成交毛额（数量 × 价格）。
        """
        gross = max(0.0, self._safe_float(gross_value))
        if gross <= 0:
            return 0.0
        commission = max(self.commission_ratio * gross, self.min_commission)
        transfer_fee = self.transfer_fee_ratio * gross
        stamp_tax = self.stamp_tax_ratio * gross if str(side).lower() == "sell" else 0.0
        return commission + transfer_fee + stamp_tax

    @staticmethod
    def _is_order_terminal(status_text, filled_qty, total_amount):
        """
        判断订单是否进入终态。

        PTrade 状态码（见官方文档"数据字典 > status"段）：
          * "0" 未报 / "1" 待报 / "2" 已报 / "3" 已报待撤 / "4" 部成待撤
            / "+" 已受理 / "-" 已确认 / "C" 正报 / "V" 已确认  →  非终态
          * "5" 部撤 / "6" 已撤 / "7" 部成 / "8" 已成 / "9" 废单         →  终态
        SimTradeLab 也用同一套数字状态码（部分版本），加上 "filled/cancelled" 等
        英文文本——这里两条路径都做兼容。
        """
        if total_amount > 0 and filled_qty >= total_amount:
            return True
        s = (status_text or "").strip().lower()
        terminal_tokens = (
            "filled", "fully", "all_filled", "alldealt", "all_dealt", "全部成交", "已成",
            "cancel", "cancelled", "canceled", "撤单", "已撤", "部撤",
            "rejected", "废单", "reject",
            "expired", "已过期",
        )
        if any(tok in s for tok in terminal_tokens):
            return True
        # 注意：PTrade "2" 是 "已报"（非终态），不能纳入；"7"（部成）严格说也非终态，
        # 但实际 PTrade 回测下 7 通常意味着不会再继续撮合（日终），保留为终态兼容。
        if s in {"5", "6", "7", "8", "9"}:
            return True
        return False

    # ----- Buy side -----
    def submit_buy(self, context, intent):
        """
        提交一笔买入意图。本方法只负责通用校验（开关、最小金额、可用资金）和
        实际下单 + 挂入 pending；所有策略相关的预检（如完美 Setup 校验）
        必须在 BuyIntent 抵达本层之前就完成。

        - 通用拒单：触发 `callbacks.on_buy_rejected(intent, reason)` 让策略层决定
          如何写 trade.csv 终态行；
        - 真实成交后：在 `_settle_pending_buy` 中调用 `_open_trade_from_fill`，
          后者会回调 `callbacks.on_buy_filled(intent, trade, fill)`。
        """
        security = intent.security

        if not self.enabled:
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_TRADE_DISABLED)
            return

        snap = self._portfolio_snapshot(context)
        total_value = snap["total_value"]
        available_cash = snap["available_cash"]
        target_value = total_value * self.buy_fraction

        if target_value < self.min_trade_value:
            self.logger.info(
                "买入取消：目标金额低于最小交易金额",
                security=security, target_value=round(target_value, 2),
            )
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_MIN_TRADE_VALUE)
            return
        if available_cash < self.min_cash_for_buy:
            self.logger.info(
                "买入取消：现金不足",
                security=security, available_cash=round(available_cash, 2),
                min_cash_for_buy=self.min_cash_for_buy, snapshot=snap["debug"],
            )
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_INSUFFICIENT_CASH)
            return

        actual_value = min(target_value, available_cash)
        if actual_value < self.min_trade_value:
            self.logger.info(
                "买入取消：实际可买金额低于最小交易金额",
                security=security, actual_value=round(actual_value, 2),
            )
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_MIN_TRADE_VALUE)
            return

        submit_dt = _format_dt(self._current_dt(context))
        fallback_price = intent.fallback_price

        try:
            order_id = order_value(security, actual_value)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error(
                "买入 order_value 调用失败",
                security=security, err=e, value=actual_value,
            )
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_ORDER_ERROR)
            return
        if not order_id:
            self.logger.info(
                "买入取消：order_value 未返回订单号",
                security=security, actual_value=round(actual_value, 2),
            )
            self._safe_callback("on_buy_rejected", intent, BUY_REJECT_ORDER_REJECTED)
            return

        order_id = str(order_id)
        self.pending_buy_orders[order_id] = {
            "intent": intent,
            "security": security,
            "submit_dt": submit_dt,
            "fallback_price": fallback_price,
            "filled_qty_recorded": 0.0,
            "recorded_gross": 0.0,
            "recorded_commission": 0.0,
        }
        self.logger.info(
            "买入委托已提交，等待真实成交回报",
            security=security, order_id=order_id, value=round(actual_value, 2),
        )

    def _open_trade_from_fill(self, intent, buy_price, buy_qty, buy_date,
                              buy_order_id="", buy_commission=0.0):
        """
        根据真实成交回报新建一笔 open_trade（**完全通用、不含 TD 字段**）。
        策略层在 `on_buy_filled` 回调里再补充 stop_loss_price / take_profit_price
        与策略私有上下文（如 TD 信号字段，统一塞到 `trade["_strategy_metadata"]`）。
        """
        security = intent.security
        entry_value = buy_price * buy_qty if buy_price and buy_qty else 0.0
        trade = {
            "_trade_id": self._next_trade_id,
            "_strategy_metadata": {},
            "security": security,
            "buy_order_id": str(buy_order_id) if buy_order_id else "",
            "buy_quantity": buy_qty,
            "buy_price": round(buy_price, 4),
            "buy_commission": round(self._safe_float(buy_commission), 4),
            "buy_date": buy_date,
            "entry_value": round(entry_value, 2),
            "stop_loss_price": "",
            "take_profit_price": "",
            "dividend_income": 0.0,
            # 用真实成交日期作为基准——避免下个 cycle 立刻拿成交当天的 K 线高低点去
            # 触发刚开仓的止盈止损（毕竟买入是收盘成交，bar 内的高低点早就过去了）。
            "last_checked_bar_dt": buy_date or "",
        }
        self._next_trade_id += 1
        self.open_trades.setdefault(security, []).append(trade)

        fill = FillInfo(
            order_id=buy_order_id, security=security, side="buy",
            quantity=buy_qty, price=buy_price,
            commission=self._safe_float(buy_commission), fill_dt=buy_date,
        )
        # 让策略层在快照写入前完成 SL / TP / 私有上下文的填充——确保
        # position_snapshot.csv 的 OPEN 行能拿到完整字段。
        self._safe_callback("on_buy_filled", intent, trade, fill)

        self._record_position_snapshot(
            "OPEN", security, trade, buy_date,
            sell_price="", sell_reason="", pnl="",
        )

    # ----- Sell side / exits -----
    def submit_sell(self, context, intent):
        """
        提交一笔卖出意图。本方法只下单 + 挂 pending；卖出价 / 数量 /
        成交时间 / 手续费等真实成交结果由 `reconcile_pending_orders` 落账。
        """
        security = intent.security
        trade = intent.trade

        if not self._trade_is_open(security, trade):
            return
        if self._has_pending_sell_for_trade(trade):
            return
        sell_qty = self._sell_quantity(trade)
        if sell_qty <= 0:
            self.logger.info(
                "卖出委托未提交：该笔仓位数量无效",
                security=security, trade_id=trade.get("_trade_id"),
                reason=intent.sell_reason,
            )
            self._safe_callback("on_sell_rejected", intent, SELL_REJECT_INVALID_QTY)
            return

        submit_dt = _format_dt(self._current_dt(context))
        try:
            order_id = order(security, -sell_qty)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error(
                "卖出 order 调用失败",
                security=security, err=e, quantity=-sell_qty, reason=intent.sell_reason,
            )
            self._safe_callback("on_sell_rejected", intent, SELL_REJECT_ORDER_ERROR)
            return
        if not order_id:
            self.logger.info(
                "卖出委托未提交：order 未返回订单号",
                security=security, quantity=sell_qty, reason=intent.sell_reason,
            )
            self._safe_callback("on_sell_rejected", intent, SELL_REJECT_ORDER_REJECTED)
            return

        order_id = str(order_id)
        self.pending_sell_orders[order_id] = {
            "intent": intent,
            "security": security,
            "trade": trade,
            "sell_reason": intent.sell_reason,
            "submit_dt": submit_dt,
            "fallback_price": intent.fallback_price,
            "fallback_qty": sell_qty,
            "filled_qty_recorded": 0.0,
            "recorded_gross": 0.0,
            "recorded_commission": 0.0,
        }
        self.logger.info(
            "卖出委托已提交，等待真实成交回报",
            security=security, order_id=order_id, qty=sell_qty,
            reason=intent.sell_reason,
        )

    def _finalize_sell(self, intent, sell_price, sell_date, sell_qty=None,
                       sell_order_id="", sell_commission=0.0):
        """
        根据真实成交回报落账一笔卖出（部分 / 完全平仓）。
        - 计算 PnL 各组成部分并构造 `pnl_breakdown` dict；
        - 写 position_snapshot CLOSE（通用快照，不含策略私有字段）；
        - 通过 `callbacks.on_sell_filled` 把完整 PnL 数据交还策略层，
          由策略层决定如何写 `<SEC>/trade.csv` / 根目录 `trade.csv` 等终态行。
        """
        security = intent.security
        trade = intent.trade
        if not self._trade_is_open(security, trade):
            return

        buy_price = self._safe_float(trade.get("buy_price"))
        original_qty = self._safe_float(trade.get("buy_quantity"))
        qty = self._safe_float(sell_qty) if sell_qty is not None else original_qty
        if qty <= 0:
            qty = original_qty
        if original_qty > 0 and qty > original_qty:
            qty = original_qty

        price_pnl = (sell_price - buy_price) * qty
        dividend_total = self._safe_float(trade.get("dividend_income"))
        dividend_income = dividend_total * qty / original_qty if original_qty > 0 else 0.0
        buy_commission_total = self._safe_float(trade.get("buy_commission"))
        buy_commission_for_qty = (
            buy_commission_total * qty / original_qty if original_qty > 0 else 0.0
        )
        sell_commission_value = self._safe_float(sell_commission)
        net_pnl = price_pnl + dividend_income - buy_commission_for_qty - sell_commission_value

        is_terminal_close = qty >= original_qty
        # 在变更 trade 状态前，先抓一份"本次平仓对应"的快照值给回调使用，
        # 避免回调读到的 buy_quantity 是部分平仓后的剩余数量。
        snapshot_for_close = {
            "_trade_id": trade.get("_trade_id"),
            "buy_order_id": trade.get("buy_order_id", ""),
            "buy_quantity": qty,
            "buy_price": trade.get("buy_price", ""),
            "buy_commission": round(buy_commission_for_qty, 4),
            "buy_date": trade.get("buy_date", ""),
            "entry_value": round(buy_price * qty, 2),
            "stop_loss_price": trade.get("stop_loss_price", ""),
            "take_profit_price": trade.get("take_profit_price", ""),
            "sell_order_id": str(sell_order_id) if sell_order_id else "",
            "sell_price": round(sell_price, 4),
            "sell_commission": round(sell_commission_value, 4),
        }

        if not is_terminal_close:
            remaining_qty = original_qty - qty
            remaining_buy_commission = max(0.0, buy_commission_total - buy_commission_for_qty)
            trade["buy_quantity"] = remaining_qty
            trade["entry_value"] = round(buy_price * remaining_qty, 2)
            trade["dividend_income"] = round(max(0.0, dividend_total - dividend_income), 2)
            trade["buy_commission"] = round(remaining_buy_commission, 4)
        else:
            self._remove_open_trade(security, trade)

        # 通用 position_snapshot：CLOSE 行只描述本次平仓的数量 / 价格 / pnl。
        self._record_position_snapshot(
            "CLOSE", security, snapshot_for_close, sell_date,
            sell_price=sell_price, sell_reason=intent.sell_reason, pnl=net_pnl,
        )

        fill = FillInfo(
            order_id=sell_order_id, security=security, side="sell",
            quantity=int(qty), price=sell_price,
            commission=sell_commission_value, fill_dt=sell_date,
        )
        pnl_breakdown = {
            "sold_qty": int(qty),
            "remaining_qty": int(max(0.0, original_qty - qty)),
            "is_terminal_close": is_terminal_close,
            "buy_price": round(buy_price, 4),
            "buy_commission_for_qty": round(buy_commission_for_qty, 4),
            "entry_value": round(buy_price * qty, 2),
            "price_pnl": round(price_pnl, 2),
            "dividend_income": round(dividend_income, 2),
            "sell_commission": round(sell_commission_value, 4),
            "net_pnl": round(net_pnl, 2),
        }
        self._safe_callback("on_sell_filled", intent, trade, fill, pnl_breakdown)

        if is_terminal_close:
            self.logger.info(
                "交易生命周期结束",
                security=security, sell_reason=intent.sell_reason,
                sell_qty=int(qty), pnl=round(net_pnl, 2),
            )
        else:
            self.logger.info(
                "交易部分平仓",
                security=security, sell_reason=intent.sell_reason,
                sell_qty=int(qty),
                remaining_qty=int(original_qty - qty), pnl=round(net_pnl, 2),
            )

    def _record_position_snapshot(self, event, security, trade, dt_text, sell_price="", sell_reason="", pnl=""):
        if self.metadata_recorder is None:
            return
        snap = {
            "datetime": dt_text,
            "event": event,
            "security": security,
            "trade_id": trade.get("_trade_id", ""),
            "open_trade_count": len(self._open_trade_list(security)),
            "buy_order_id": trade.get("buy_order_id", ""),
            "buy_quantity": trade.get("buy_quantity", ""),
            "buy_price": trade.get("buy_price", ""),
            "buy_commission": trade.get("buy_commission", ""),
            "entry_value": trade.get("entry_value", ""),
            "stop_loss_price": trade.get("stop_loss_price", ""),
            "take_profit_price": trade.get("take_profit_price", ""),
            "sell_order_id": trade.get("sell_order_id", ""),
            "sell_price": round(sell_price, 4) if sell_price != "" else "",
            "sell_commission": trade.get("sell_commission", ""),
            "sell_reason": sell_reason,
            "pnl": round(pnl, 2) if pnl != "" else "",
        }
        latest = self._last_portfolio_snapshot or {}
        port = {
            "total_value": latest.get("total_value", ""),
            "available_cash": latest.get("available_cash", ""),
        }
        snap.update(port)
        self.metadata_recorder.record_position(snap)

    def _open_trade_list(self, security):
        return self.open_trades.get(security, [])

    def _trade_is_open(self, security, trade):
        return trade in self._open_trade_list(security)

    def _remove_open_trade(self, security, trade):
        trades = self._open_trade_list(security)
        if trade in trades:
            trades.remove(trade)
        if not trades and security in self.open_trades:
            del self.open_trades[security]

    def _has_pending_sell_for_trade(self, trade):
        trade_id = trade.get("_trade_id")
        for pending in self.pending_sell_orders.values():
            pending_trade = pending.get("trade")
            if pending_trade is trade:
                return True
            if pending_trade is not None and pending_trade.get("_trade_id") == trade_id:
                return True
        return False

    def _has_pending_sell_for_security(self, security):
        for pending in self.pending_sell_orders.values():
            if pending.get("security") == security:
                return True
        return False

    def _open_quantity(self, security):
        total = 0.0
        for trade in self._open_trade_list(security):
            total += self._safe_float(trade.get("buy_quantity"))
        return total

    def _sell_quantity(self, trade):
        qty = self._safe_float(trade.get("buy_quantity"))
        if qty <= 0:
            return 0
        return int(qty)

    def _should_exit_by_holding_days(self, holding_days):
        return (
            self.max_holding_days_enabled
            and self.max_holding_days > 0
            and holding_days is not None
            and holding_days >= self.max_holding_days
        )

    def _holding_days(self, trade, current_dt):
        buy_dt = self._parse_dt(trade.get("buy_date"))
        cur_dt = self._parse_dt(current_dt)
        if buy_dt is None or cur_dt is None:
            return None
        return max(0, (cur_dt.date() - buy_dt.date()).days)

    def _get_bonus_ps(self, security, date_key):
        row = self._get_exrights_row(security, date_key)
        if row is None:
            return 0.0
        return self._read_row_float(row, ["bonus_ps", "bonus", "cash_bonus", "dividend", "dividend_ps"])

    def _get_share_adjustment_ratio(self, security, date_key):
        row = self._get_exrights_row(security, date_key)
        if row is None:
            return 1.0
        allotted_ps = self._read_row_float(row, ["allotted_ps", "allotted", "stock_bonus", "bonus_share"])
        if allotted_ps <= 0:
            return 1.0
        return 1.0 + allotted_ps

    def _get_exrights_row(self, security, date_key):
        try:
            exrights = get_stock_exrights(security)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.debug("读取除权除息数据失败", security=security, date=date_key, err=e)
            return None
        return self._pick_exrights_row(exrights, date_key)

    def _pick_exrights_row(self, exrights, date_key):
        if exrights is None:
            return None
        try:
            rows_iter = exrights.iterrows()
        except Exception:
            rows_iter = None
        if rows_iter is not None:
            for idx, row in rows_iter:
                if self._date_key(idx) == date_key:
                    return row
            return None
        try:
            if self._date_key(exrights.get("date", "")) == date_key:
                return exrights
        except Exception:
            return None
        return None

    def _read_row_float(self, row, names):
        for name in names:
            try:
                if isinstance(row, dict) and name in row:
                    return self._safe_float(row.get(name))
                if hasattr(row, "get"):
                    value = row.get(name)
                    if value is not None:
                        return self._safe_float(value)
                if hasattr(row, name):
                    return self._safe_float(getattr(row, name))
            except Exception:
                continue
        return 0.0

    @staticmethod
    def _date_key(value):
        text = _format_dt(value, DATE_FMT) if not isinstance(value, str) else value
        digits = "".join([c for c in str(text) if c.isdigit()])
        return digits[:8] if len(digits) >= 8 else ""

    @staticmethod
    def _parse_dt(value):
        if value is None:
            return None
        if hasattr(value, "date"):
            return value
        text = str(value).strip()
        if not text:
            return None
        for fmt in (DATETIME_FMT, DATE_FMT, "%Y%m%d"):
            try:
                if fmt == DATETIME_FMT:
                    candidate = text[:19]
                elif fmt == DATE_FMT:
                    candidate = text[:10]
                else:
                    candidate = "".join([c for c in text if c.isdigit()])[:8]
                return datetime.strptime(candidate, fmt)
            except Exception:
                continue
        return None

    # ----- Helpers -----
    def _portfolio_snapshot(self, context):
        portfolio = getattr(context, "portfolio", None)
        debug = {"has_portfolio": portfolio is not None}

        def read_attr(obj, names):
            if obj is None:
                return None, None
            for name in names:
                try:
                    if hasattr(obj, name):
                        val = getattr(obj, name)
                        if callable(val):
                            val = val()
                        return self._safe_float(val), name
                except Exception:
                    continue
            return None, None

        total_value, tv_src = read_attr(portfolio, ["total_value", "portfolio_value", "totalValue", "total_asset", "assets"])
        available_cash, ac_src = read_attr(portfolio, ["available_cash", "cash", "_cash", "starting_cash"])
        debug["total_value_from"] = tv_src
        debug["available_cash_from"] = ac_src
        if available_cash is None:
            available_cash = self._safe_float(getattr(context, "capital_base", 0.0))
            debug["available_cash_from"] = "context.capital_base"
        if total_value is None or total_value <= 0:
            positions_value = self._safe_float(getattr(portfolio, "positions_value", 0.0)) if portfolio else 0.0
            total_value = available_cash + positions_value
            debug["total_value_from"] = "cash+positions_value"
        debug["available_cash"] = available_cash
        debug["total_value"] = total_value
        return {"total_value": total_value, "available_cash": available_cash, "debug": debug}

    def _get_position_detail(self, security):
        """
        返回 (qty, market_value, cost_basis_per_share)。

        cost_basis_per_share 优先取 cost_basis（PTrade / SimTradeLab 中即"建仓时的
        含佣金均价"），buy 路径需要据此推断"本次单笔买入"的真实成本，因此一定要
        优先取 cost_basis 而不是 last_sale_price——last_sale_price 在 portfolio_value
        被查询过一次后会被引擎回写为当日收盘价，不能反映本次成交的真实成本。
        """
        try:
            pos = get_position(security)  # noqa: F821 - PTrade 注入
        except Exception:
            pos = None
        if pos is None:
            return 0.0, 0.0, 0.0
        qty = self._first_attr_float(pos, ["current_amount", "total_amount", "enable_amount", "amount", "volume", "qty"])
        value = self._first_attr_float(pos, ["market_value", "position_value", "value", "cost_balance"])
        price = self._first_attr_float(pos, ["cost_basis", "cost_price", "avg_cost", "last_sale_price", "price"])
        if price <= 0 and qty > 0 and value > 0:
            price = value / qty
        return qty, value, price

    def _available_cash(self, context):
        """返回账户当前可用现金。计算 buy / sell 真实成交价的现金端依据。"""
        portfolio = getattr(context, "portfolio", None)
        if portfolio is None:
            return 0.0
        for name in ("available_cash", "cash", "_cash"):
            try:
                if hasattr(portfolio, name):
                    val = getattr(portfolio, name)
                    if callable(val):
                        val = val()
                    return self._safe_float(val)
            except Exception:
                continue
        return 0.0

    def _first_attr_float(self, obj, names):
        for k in names:
            if hasattr(obj, k):
                v = self._safe_float(getattr(obj, k, 0.0))
                if v > 0:
                    return v
        return 0.0

    @staticmethod
    def _current_dt(context):
        try:
            return context.blotter.current_dt
        except Exception:
            pass
        return getattr(context, "current_dt", None)

    @staticmethod
    def _safe_float(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    @staticmethod
    def _boolish(x):
        return x is True or str(x).lower() in ("true", "1", "yes")

    @staticmethod
    def _read_any(obj, names):
        if obj is None:
            return None
        for name in names:
            try:
                if isinstance(obj, dict) and name in obj:
                    return obj.get(name)
                if hasattr(obj, name):
                    return getattr(obj, name)
            except Exception:
                continue
        return None

    def _extract_tick_price(self, tick_data_obj, security):
        tick = None
        try:
            if isinstance(tick_data_obj, dict):
                tick = tick_data_obj.get(security)
        except Exception:
            tick = None
        if tick is None:
            tick = tick_data_obj
            tick_sec = self._read_any(tick, ["security", "symbol", "stock_code", "code"])
            if tick_sec and str(tick_sec) != str(security):
                return 0.0
        return self._safe_float(self._read_any(tick, [
            "last_price", "current_price", "price", "last", "close", "business_price",
        ]))

    @staticmethod
    def _is_live_trade():
        try:
            return bool(is_trade())  # noqa: F821 - PTrade 注入
        except Exception:
            return False


# =============================================================================
# [7.5] 策略适配层：TD 信号 ↔ 交易意图
# =============================================================================
#
# 本层是 TD 9-13 信号与通用 TradeExecutor 之间的桥梁：
#   1. 把 TD 事件流（COUNTDOWN_COMPLETE / COUNTDOWN_CANCEL / SELL_COUNTDOWN_COMPLETE）
#      翻译成 BuyIntent / SellIntent，调用 executor.submit_buy / submit_sell；
#   2. 接收 executor 的成交 / 拒单回调，把它们翻译成 trade.csv 中的 TD 生命周期行；
#   3. 维护 TD 特有的预检（require_perfect_setup_for_buy）和风控参数
#      （stop_loss_range_multiple、profit_target_r_multiple）。
#
# 交易层（TradeExecutor）完全不依赖本层；理论上只要实现 TradeExecutorCallbacks
# 接口，任何其他信号体系都能直接复用 TradeExecutor。
# =============================================================================

COUNTDOWN_STATUS_NORMAL = "NORMAL"
COUNTDOWN_STATUS_CANCEL_TDST_PREFIX = "CANCEL_BY_TDST_RULE_"
COUNTDOWN_STATUS_CANCEL_OPPOSITE_SETUP = "CANCEL_BY_OPPOSITE_SETUP"
COUNTDOWN_STATUS_CANCEL_SAME_SETUP = "CANCEL_BY_SAME_SETUP"

# TD 特有的买入拒绝原因，仅由本适配层使用。通用拒绝原因
# （TRADE_DISABLED / INSUFFICIENT_CASH / MIN_TRADE_VALUE / ORDER_REJECTED / ORDER_ERROR）
# 由 TradeExecutor 给出。
BUY_REJECT_NON_PERFECT_SETUP = "NON_PERFECT_SETUP"


class TDStrategyAdapter(TradeExecutorCallbacks):
    """
    TD 9-13 与 TradeExecutor 之间的适配层。
    `executor` 持有所有交易执行 / 风控逻辑，本类只负责"信号 ↔ 意图 ↔ 终态记录"
    的翻译。注册到 executor.callbacks 后，所有写 trade.csv 的工作都集中在本类。
    """

    def __init__(self, executor, trade_recorder, cfg_trade, logger):
        self.executor = executor
        self.trade_recorder = trade_recorder
        self.logger = logger
        # TD 特有风控参数；通用风控参数（stop_loss_enabled / max_holding_days*）留在 executor
        self.profit_target_r_multiple = float(cfg_trade.get("profit_target_r_multiple", 1.5))
        self.stop_loss_range_multiple = float(cfg_trade.get("stop_loss_range_multiple", 1.0))
        self.require_perfect_setup_for_buy = bool(cfg_trade.get("require_perfect_setup_for_buy", False))
        self.take_profit_on_sell_countdown = bool(cfg_trade.get("take_profit_on_sell_countdown", False))
        # 注册回调
        executor.set_callbacks(self)

    # ---------- 信号 → 意图 ----------
    def execute_for_events(self, context, security, last_bar_events):
        if not last_bar_events:
            return
        for ev in last_bar_events:
            if ev.get("category") != "COUNTDOWN":
                continue
            ev_type = ev.get("type", "")
            direction = int(ev.get("direction", 0))

            # Buy countdown 取消：生命周期在"计数取消"处结束，写一行 trade.csv。
            if direction == 1 and ev_type == "BUY_COUNTDOWN_CANCEL":
                self._write_terminal_row(
                    ev, bought=False, buy_reject_reason="",
                    countdown_status=self._countdown_status(ev),
                )
                continue

            # Buy countdown 完成：经预检后构造 BuyIntent 投递交易层。
            if direction == 1 and ev_type.startswith("BUY_COUNTDOWN_COMPLETE"):
                self._dispatch_buy(context, security, ev)
                continue

            # Sell countdown 完成：仅当配置启用且仓位盈利时主动趋势反转止盈。
            if direction == -1 and ev_type.startswith("SELL_COUNTDOWN_COMPLETE"):
                self._dispatch_sell_countdown(context, security, ev)

    def _dispatch_buy(self, context, security, ev):
        # TD 特有预检放在交易层之外
        if self.require_perfect_setup_for_buy and not _boolish(ev.get("setup_perfect")):
            self._write_terminal_row(
                ev, bought=False, buy_reject_reason=BUY_REJECT_NON_PERFECT_SETUP,
            )
            return
        intent = BuyIntent(
            security=security,
            target_value=0.0,  # 实际目标金额由 executor 按 buy_fraction 计算
            decision_dt=_format_dt(self.executor._current_dt(context)),
            fallback_price=_safe_float(ev.get("bar_close")),
            metadata={"td_event": ev},
        )
        self.executor.submit_buy(context, intent)

    def _dispatch_sell_countdown(self, context, security, ev):
        if not self.take_profit_on_sell_countdown:
            return
        trades = self.executor.open_trades.get(security, [])
        if not trades:
            return
        ref_price = _safe_float(ev.get("bar_close"))
        for trade in list(trades):
            buy_price = _safe_float(trade.get("buy_price"))
            if ref_price <= buy_price:
                self.logger.info(
                    "Sell Countdown 出现但该笔仓位未盈利，不做趋势反转止盈",
                    security=security, trade_id=trade.get("_trade_id"),
                    ref_price=round(ref_price, 4), buy_price=round(buy_price, 4),
                )
                continue
            self.executor.submit_sell(context, SellIntent(
                security=security, trade=trade,
                sell_reason=SELL_REASON_TREND_REVERSAL,
                fallback_price=ref_price,
                metadata={"td_event": ev},
            ))

    # ---------- 交易层 → trade.csv 终态行 ----------
    def on_buy_rejected(self, intent, reason):
        ev = intent.metadata.get("td_event", {})
        self._write_terminal_row(ev, bought=False, buy_reject_reason=reason)

    def on_buy_filled(self, intent, trade, fill):
        """根据真实成交价反算止损 / 止盈，并把 TD 信号上下文存入 trade 对象。"""
        ev = intent.metadata.get("td_event", {})
        stop_loss, take_profit = self._calc_td_risk_prices(ev, fill.price)
        trade["stop_loss_price"] = round(stop_loss, 4) if stop_loss else ""
        trade["take_profit_price"] = round(take_profit, 4) if take_profit else ""
        # 把生成 trade.csv 终态行所需的 TD 字段全部缓存下来；
        # 交易层完全不会读取 _strategy_metadata。
        trade["_strategy_metadata"] = {
            "td_event": dict(ev),
        }

    def on_sell_filled(self, intent, trade, fill, pnl_breakdown):
        td_ctx = (trade.get("_strategy_metadata") or {}).get("td_event", {})
        row = self._base_lifecycle_row(td_ctx, COUNTDOWN_STATUS_NORMAL)
        row.update({
            "datetime": fill.fill_dt,
            "bought": True,
            "buy_reject_reason": "",
            "buy_order_id": trade.get("buy_order_id", ""),
            "buy_quantity": pnl_breakdown["sold_qty"],
            "buy_price": pnl_breakdown["buy_price"],
            "buy_commission": pnl_breakdown["buy_commission_for_qty"],
            "buy_date": trade.get("buy_date", ""),
            "entry_value": pnl_breakdown["entry_value"],
            "stop_loss_price": trade.get("stop_loss_price", ""),
            "take_profit_price": trade.get("take_profit_price", ""),
            "sell_order_id": fill.order_id,
            "sell_price": round(fill.price, 4),
            "sell_commission": pnl_breakdown["sell_commission"],
            "sell_date": fill.fill_dt,
            "sell_reason": intent.sell_reason,
            "price_pnl": pnl_breakdown["price_pnl"],
            "dividend_income": pnl_breakdown["dividend_income"],
            "pnl": pnl_breakdown["net_pnl"],
        })
        if self.trade_recorder is not None:
            self.trade_recorder.record(row)

    def on_sell_rejected(self, intent, reason):
        # 卖出未达成不写 trade.csv 终态——仓位继续保留，下一周期由风控再尝试。
        # 此处只记一条复盘日志，方便事后定位"为什么 TD 信号触发了卖单却没成交"。
        self.logger.info(
            "卖出意图被交易层拒绝",
            security=intent.security, sell_reason=intent.sell_reason, reason=reason,
        )

    def on_position_adjusted(self, security, trade, dt_text, ratio, reason):
        # 由 executor 处理日志和 position_snapshot ADJUST 行，本层无需额外动作。
        pass

    # ---------- TD-specific 内部工具 ----------
    def _calc_td_risk_prices(self, ev, buy_price):
        low = _safe_float(ev.get("countdown_low"))
        high = _safe_float(ev.get("countdown_low_bar_high"))
        if low <= 0 or high <= 0 or high < low:
            return "", ""
        raw_stop_loss = low - (high - low) * self.stop_loss_range_multiple
        take_profit = buy_price + (buy_price - raw_stop_loss) * self.profit_target_r_multiple
        stop_loss = raw_stop_loss if self.executor.stop_loss_enabled else ""
        return stop_loss, take_profit

    def _write_terminal_row(self, ev, bought=False, buy_reject_reason="", countdown_status=None):
        if self.trade_recorder is None:
            return
        status = countdown_status or self._countdown_status(ev)
        row = self._base_lifecycle_row(ev, status)
        row.update({
            "datetime": ev.get("datetime", ""),
            "bought": bool(bought),
            "buy_reject_reason": buy_reject_reason,
            "buy_order_id": "", "buy_quantity": "", "buy_price": "",
            "buy_commission": "", "buy_date": "",
            "entry_value": "", "stop_loss_price": "", "take_profit_price": "",
            "sell_order_id": "", "sell_price": "", "sell_commission": "",
            "sell_date": "", "sell_reason": "",
            "price_pnl": "", "dividend_income": "", "pnl": "",
        })
        self.trade_recorder.record(row)

    @staticmethod
    def _base_lifecycle_row(ev, countdown_status):
        return {
            "datetime": ev.get("datetime", ""),
            "security": ev.get("security", ""),
            "setup_completed_at": ev.get("setup_last_dt", ""),
            "setup_is_perfect": ev.get("setup_perfect", ""),
            "setup_highest_high": ev.get("setup_highest_high", ""),
            "count_1_at": ev.get("count_1_dt", ""),
            "count_2_at": ev.get("count_2_dt", ""),
            "count_3_at": ev.get("count_3_dt", ""),
            "count_4_at": ev.get("count_4_dt", ""),
            "count_5_at": ev.get("count_5_dt", ""),
            "count_6_at": ev.get("count_6_dt", ""),
            "count_7_at": ev.get("count_7_dt", ""),
            "count_8_at": ev.get("count_8_dt", ""),
            "count_8_close": ev.get("countdown_8_close", ""),
            "count_9_at": ev.get("count_9_dt", ""),
            "count_10_at": ev.get("count_10_dt", ""),
            "count_11_at": ev.get("count_11_dt", ""),
            "count_12_at": ev.get("count_12_dt", ""),
            "count_13_at": ev.get("count_13_dt", ""),
            "countdown_completed_count": ev.get("count", ""),
            "countdown_is_perfect": ev.get("perfect", ""),
            "countdown_status": countdown_status,
        }

    @staticmethod
    def _countdown_status(ev):
        ev_type = ev.get("type", "")
        if "CANCEL" not in ev_type:
            return COUNTDOWN_STATUS_NORMAL
        reason = ev.get("reason", "")
        if reason.startswith("tdst_break_rule_"):
            return COUNTDOWN_STATUS_CANCEL_TDST_PREFIX + reason.replace("tdst_break_rule_", "")
        if reason == "opposite_setup":
            return COUNTDOWN_STATUS_CANCEL_OPPOSITE_SETUP
        if reason == "same_setup":
            return COUNTDOWN_STATUS_CANCEL_SAME_SETUP
        return "CANCEL_BY_{}".format(str(reason).upper() or "UNKNOWN")


def _safe_float(value):
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _boolish(x):
    if isinstance(x, bool):
        return x
    return x is True or str(x).lower() in ("true", "1", "yes")


# =============================================================================
# [8] PTrade 策略钩子
# =============================================================================

def _strategy_start_date_str(context):
    """
    返回策略启动当天的日期字符串，格式 YYYY-MM-DD。
    """
    dt = None
    try:
        dt = context.blotter.current_dt
    except Exception:
        dt = getattr(context, "current_dt", None)
    if dt is None:
        try:
            dt = get_trading_day(0)  # noqa: F821 - PTrade 注入
        except Exception:
            dt = None
    return _format_dt(dt, DATE_FMT) if dt is not None else "unknown_start_date"


def _enrich_events_with_bar_context(events, bars):
    """
    为论文离线分析补充事件日前 20 日的轻量上下文字段。
    这些字段只做原始元数据记录，分层统计和图表生成放到离线工具中完成。
    """
    if not events or not bars:
        return
    window = bars[-20:] if len(bars) >= 20 else list(bars)
    amounts = []
    closes = []
    for b in window:
        try:
            if b.amount is not None:
                amounts.append(float(b.amount))
        except Exception:
            pass
        try:
            closes.append(float(b.close))
        except Exception:
            pass

    avg_amount = sum(amounts) / len(amounts) if amounts else ""
    pre20_return = ""
    volatility = ""
    if len(closes) >= 2 and closes[0] > 0:
        pre20_return = closes[-1] / closes[0] - 1.0
        returns = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                returns.append(closes[i] / closes[i - 1] - 1.0)
        if returns:
            mean_ret = sum(returns) / len(returns)
            variance = sum((r - mean_ret) * (r - mean_ret) for r in returns) / len(returns)
            volatility = variance ** 0.5

    for ev in events:
        if not ev.get("event_id"):
            ev["event_id"] = _make_event_id(ev)
        ev["pre20_avg_amount"] = round(avg_amount, 4) if avg_amount != "" else ""
        ev["pre20_return"] = round(pre20_return, 6) if pre20_return != "" else ""
        ev["pre20_volatility"] = round(volatility, 6) if volatility != "" else ""


def _make_event_id(ev):
    raw = "{}|{}|{}|{}|{}|{}".format(
        ev.get("security", ""),
        ev.get("datetime", ""),
        ev.get("type", ""),
        ev.get("direction", ""),
        ev.get("setup_last_dt", ""),
        ev.get("count", ""),
    )
    return raw.replace(" ", "T").replace(":", "").replace("|", "_").replace(".", "")


def _update_pending_event_outcomes(pending_events, security, bars, recorder):
    if not pending_events or not bars or recorder is None:
        return
    security_events = pending_events.get(security, [])
    if not security_events:
        return
    index_by_dt = {}
    for idx, bar in enumerate(bars):
        index_by_dt[bar.datetime] = idx
    outcome_rows = []
    still_pending = []
    for ev in security_events:
        event_dt = ev.get("datetime", "")
        event_idx = index_by_dt.get(event_dt)
        if event_idx is None:
            # lookback 窗口已无法覆盖该事件，保守丢弃，避免无限增长。
            continue
        completed_windows = ev.setdefault("_completed_windows", {})
        for window in EVENT_OUTCOME_WINDOWS:
            if completed_windows.get(window):
                continue
            matured_idx = event_idx + int(window)
            if matured_idx >= len(bars):
                continue
            row = _make_event_outcome_row(ev, bars, event_idx, matured_idx, window)
            if row is not None:
                outcome_rows.append(row)
                completed_windows[window] = True
        if len(completed_windows) < len(EVENT_OUTCOME_WINDOWS):
            still_pending.append(ev)
    if outcome_rows:
        recorder.write_event_outcomes(outcome_rows)
    if still_pending:
        pending_events[security] = still_pending
    elif security in pending_events:
        del pending_events[security]


def _should_track_event_outcome(ev):
    typ = ev.get("type", "")
    if _safe_int(ev.get("direction", 0)) == 0:
        return False
    if "PROGRESS" in typ or "TENTATIVE" in typ:
        return False
    return typ.startswith("BUY_SETUP") or typ.startswith("SELL_SETUP") or "COUNTDOWN_COMPLETE" in typ or "COUNTDOWN_CANCEL" in typ


def _make_event_outcome_row(ev, bars, event_idx, matured_idx, window):
    direction = _safe_int(ev.get("direction", 0))
    if direction == 0:
        return None
    base_close = _safe_float_value(ev.get("bar_close"))
    if base_close <= 0:
        base_close = bars[event_idx].close
    if base_close <= 0:
        return None
    end_bar = bars[matured_idx]
    end_close = end_bar.close
    directional_return = direction * (end_close / base_close - 1.0)
    window_bars = bars[event_idx + 1:matured_idx + 1]
    if not window_bars:
        return None
    if direction > 0:
        mfe = max(b.high / base_close - 1.0 for b in window_bars)
        mae = min(b.low / base_close - 1.0 for b in window_bars)
    else:
        mfe = max(1.0 - b.low / base_close for b in window_bars)
        mae = min(1.0 - b.high / base_close for b in window_bars)
    return {
        "event_id": ev.get("event_id", _make_event_id(ev)),
        "event_datetime": ev.get("datetime", ""),
        "security": ev.get("security", ""),
        "type": ev.get("type", ""),
        "direction": direction,
        "window": window,
        "matured_datetime": end_bar.datetime,
        "base_close": round(base_close, 6),
        "end_close": round(end_close, 6),
        "directional_return": round(directional_return, 8),
        "mfe": round(mfe, 8),
        "mae": round(mae, 8),
    }


def _safe_float_value(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def _safe_int(value):
    try:
        return int(float(value))
    except Exception:
        return 0


def initialize(context):
    """
    PTrade 在策略启动时调用一次。此函数中不可调用 get_history / get_price /
    get_stock_status 等行情接口。本函数只负责：配置加载 → 输出目录 → 日志 →
    数据层对象构造 → 交易层对象构造。
    """
    # (1) 配置：强制存在、强制合法，否则直接抛错中止策略
    cfg = load_config(CONFIG_REL_PATH)
    g.config = cfg

    # (2) 输出目录：和 config.json 同级，按策略启动日期命名，存在则追加 _1 递增
    start_date_str = _strategy_start_date_str(context)
    output_rel, output_abs = prepare_output_dir(start_date_str, LOADED_CONFIG_PATH)
    g.output_dir_rel = output_rel
    g.output_dir_abs = output_abs
    g.run_tag = start_date_str

    # (3) 日志层：同时写入控制台与 .log 文件
    log_cfg = cfg["log"]
    g.logger = StrategyLogger(
        level=log_cfg.get("level", "INFO"),
        verbose_state_transition=log_cfg.get("verbose_state_transition", False),
        verbose_countdown_step=log_cfg.get("verbose_countdown_step", True),
    )
    g.log_file_path = "{}/strategy.log".format(output_abs)
    g.logger.set_log_file(g.log_file_path)

    # (4) 标的、基准、滑点等
    universe_cfg = cfg["universe"]
    g.securities = list(universe_cfg["securities"])
    prepare_security_output_dirs(output_rel, g.securities)
    set_benchmark(universe_cfg["benchmark"])  # noqa: F821
    set_universe(g.securities)  # noqa: F821

    bt_cfg = cfg["backtest"]
    set_slippage(slippage=float(bt_cfg["slippage"]))  # noqa: F821
    set_limit_mode(bt_cfg["limit_mode"])  # noqa: F821
    # 同步把 commission 配置交给 PTrade 引擎；TradeExecutor 自算时会读同一份配置，
    # 这样 trade.csv 中的 buy_commission/sell_commission 与 PTrade 实际扣款保持一致。
    commission_ratio_cfg = float(bt_cfg["commission_ratio"])
    min_commission_cfg = float(bt_cfg["min_commission"])
    try:
        set_commission(  # noqa: F821 - PTrade 注入；SimTradeLab 也提供等价实现
            commission_ratio=commission_ratio_cfg,
            min_commission=min_commission_cfg,
            type="STOCK",
        )
    except Exception as exc:
        # 个别 SimTradeLab 版本不支持该 API，不视为致命：策略内部仍按配置自算。
        g.logger.warning("set_commission 调用失败，已使用配置自算手续费", err=str(exc))

    # (5) 数据层
    data_cfg = cfg["data"]
    g.frequency = data_cfg["frequency"]
    g.fq = data_cfg["fq"]
    g.lookback_count = int(data_cfg["lookback_count"])
    g.min_required_bars = int(data_cfg["min_required_bars"])
    g.data_fetcher = MarketDataFetcher(
        frequency=g.frequency, fq=g.fq, lookback_count=g.lookback_count, logger=g.logger,
    )

    # (6) 论文元数据输出层 & 交易生命周期输出层（td_events.csv + trade.csv）
    g.recorder = MetadataRecorder(
        output_dir_abs=output_abs,
        logger=g.logger,
        enabled=log_cfg.get("csv_output", True),
    )
    g.trade_recorder = TradeRecordRecorder(
        output_dir_abs=output_abs,
        logger=g.logger,
        enabled=log_cfg.get("csv_output", True),
    )
    # (7) 交易执行层（通用，不感知 TD 信号）
    g.trade_executor = TradeExecutor(
        cfg_trade=cfg["trade"],
        logger=g.logger,
        metadata_recorder=g.recorder,
        commission_ratio=commission_ratio_cfg,
        min_commission=min_commission_cfg,
    )
    # (7.1) 策略适配层：把 TD 信号 ↔ 通用 Buy/Sell 意图，并独占 trade.csv 写入
    g.trade_adapter = TDStrategyAdapter(
        executor=g.trade_executor,
        trade_recorder=g.trade_recorder,
        cfg_trade=cfg["trade"],
        logger=g.logger,
    )

    # (8) 当日有效（非停牌）标的列表，由 before_trading_start 每日刷新
    g.active_securities_today = list(g.securities)
    g.pending_event_outcomes = {}

    g.logger.info(
        "策略初始化完成",
        securities=len(g.securities), frequency=g.frequency, fq=g.fq,
        lookback=g.lookback_count,
        setup_perfect_only=cfg["setup"].get("require_perfect_for_signal"),
        countdown_perfect=cfg["countdown"].get("require_perfect"),
        tdst_rule=cfg["countdown"].get("tdst_cancel_rule"),
        trade_enabled=cfg["trade"].get("enabled", True),
        buy_fraction=cfg["trade"].get("buy_fraction_of_portfolio", 0.1),
        take_profit_on_sell_countdown=cfg["trade"].get("take_profit_on_sell_countdown", False),
        stop_loss_enabled=cfg["trade"].get("stop_loss_enabled", True),
        metadata_output=log_cfg.get("csv_output", True),
        start_date=start_date_str,
        output_dir=output_abs,
        log_file=g.log_file_path,
    )


def before_trading_start(context, data):
    """
    每日盘前刷新当日有效标的（剔除当日停牌、退市等异常标的）。
    根据 PTrade 文档，filter_stock_by_status 仅可在 before_trading_start 内调用。
    """
    try:
        trading_dt = context.blotter.current_dt
    except Exception:
        trading_dt = getattr(context, "current_dt", None)
    g.logger.info("盘前开始", trading_date=_format_dt(trading_dt, DATE_FMT))

    try:
        active = filter_stock_by_status(g.securities, ["HALT", "DELISTING"])  # noqa: F821
        if active is None:
            active = list(g.securities)
    except Exception as e:
        g.logger.warning("filter_stock_by_status 调用失败，今日不剔除停牌/退市标的", err=e)
        active = list(g.securities)

    g.active_securities_today = list(active)
    skipped = [s for s in g.securities if s not in active]
    g.logger.info(
        "盘前刷新有效标的",
        active=len(g.active_securities_today), skipped=len(skipped),
        skipped_list=",".join(skipped) if skipped else "-",
    )


def handle_data(context, data):
    """
    每个周期执行一次（日线策略下每日 15:00 一次）。
    对每只当日非停牌标的：
        1. 数据层拉取近 N 根有效 K 线（include=False → 最后一根 = 昨日已收盘 K 线）
        2. 构造全新 TDSignalProcessor，从头跑完整 TD 流水线
        3. 筛出"最后一根 K 线（= 昨日）"产生的信号事件——这些是今天可以执行的信号
        4. 交易层基于这些信号在今日下单；事件写入根目录 td_events.csv
    """
    # (0) 在当周期任何下单 / 风控逻辑前，先把"上周期下出去、PTrade 后续才撮合成交"
    # 的订单按真实成交回报落账到 trade.csv / position_snapshot.csv，
    # 这样后续 check_backtest_exits / accrue_dividends 看到的是与 PTrade 完全
    # 一致的持仓 / 成本 / 风控价。
    g.trade_executor.reconcile_pending_orders(context)

    all_today_events = []
    skipped_halt = 0
    skipped_data = 0

    for security in g.active_securities_today:
        # (1) 二次确认当日是否停牌，避免对今日无法交易的标的下单
        try:
            current_dt = context.blotter.current_dt
            query_date = current_dt.strftime("%Y%m%d")
        except Exception:
            current_dt = getattr(context, "current_dt", None)
            query_date = None

        if MarketDataFetcher.is_halt_today(security, query_date):
            skipped_halt += 1
            continue

        # (2) 数据层
        bars = g.data_fetcher.fetch_recent_bars(security)
        if len(bars) < g.min_required_bars:
            skipped_data += 1
            continue

        last_bar_dt = bars[-1].datetime  # 昨日（或更早的已收盘 K 线）
        _update_pending_event_outcomes(g.pending_event_outcomes, security, bars, g.recorder)

        # 回测降级：没有 tick_data / on_trade_response 时，用上一根已完成 K 线的 high/low
        # 追踪已有仓位的止盈止损。实盘环境中该逻辑由 tick_data 负责。
        current_dt_text = _format_dt(current_dt)
        g.trade_executor.accrue_dividends(
            security=security,
            dt_text=current_dt_text,
        )
        g.trade_executor.reconcile_position_adjustments(
            context=context,
            security=security,
            dt_text=current_dt_text,
        )
        g.trade_executor.check_backtest_exits(context, security, bars[-1])

        # (3) 信号层：每日全量重算
        processor = TDSignalProcessor(
            security=security, frequency=g.frequency, config=g.config, logger=g.logger,
        )
        all_events = processor.run(bars)

        # (4) 仅关注"最后一根 K 线"产生的事件——这些是"今日可执行"的新鲜信号
        last_bar_events = [ev for ev in all_events if ev["datetime"] == last_bar_dt]

        if last_bar_events:
            _enrich_events_with_bar_context(last_bar_events, bars)
            for ev in last_bar_events:
                if _should_track_event_outcome(ev):
                    g.pending_event_outcomes.setdefault(security, []).append(dict(ev))
            # (5) 策略适配层：把 TD 事件翻译为通用 Buy/Sell 意图投递给交易层
            g.trade_adapter.execute_for_events(
                context=context,
                security=security,
                last_bar_events=last_bar_events,
            )
            all_today_events.extend(last_bar_events)

    # (6) 写入根目录 TD 事件元数据
    if all_today_events:
        g.recorder.write_events(all_today_events)
        g.logger.info("本周期 TD 事件", count=len(all_today_events))
    if skipped_halt or skipped_data:
        g.logger.debug("本周期跳过标的", halt=skipped_halt, insufficient_data=skipped_data)

    # (7) 周期末再 reconcile 一次，捕获 SimTradeLab 等同步撮合引擎在本 cycle
    # 内即时成交的订单，使 trade.csv 不会被推迟一周期。对于真正异步的 PTrade
    # 引擎，这次 reconcile 通常只会看到"未成交"或"刚成交"的同一份状态——幂等。
    g.trade_executor.reconcile_pending_orders(context)


def tick_data(context, data):
    """
    实盘主推回调：仅在 is_trade() 为 True 时用于逐 tick 追踪止盈止损。
    回测环境下 is_trade() 通常为 False，本函数会直接返回；回测止盈止损由
    handle_data 中的 check_backtest_exits 降级处理。
    """
    try:
        if not is_trade():  # noqa: F821 - PTrade 注入
            return
    except Exception:
        return
    g.trade_executor.check_tick_exits(context, data)


def on_trade_response(context, trade_response):
    """
    实盘成交回报：买入成交后建立风控仓位；卖出成交后写 <SEC>/trade.csv。
    该回调仅交易环境可用，回测环境不依赖它。
    """
    try:
        if not is_trade():  # noqa: F821 - PTrade 注入
            return
    except Exception:
        return
    g.trade_executor.on_trade_response(context, trade_response)


def after_trading_end(context, data):
    """盘后汇总占位。后续可在此追加每日统计。"""
    g.logger.debug("盘后处理完毕")