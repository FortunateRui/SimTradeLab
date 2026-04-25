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
    [6] 信号与交易输出层      SignalRecorder / TradeRecordRecorder
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
# 注意：PTrade 禁止 import os / import sys。所有文件/目录操作必须通过
# 内置 open()、PTrade 注入的 create_dir / get_research_path 完成。


# =============================================================================
# [1] 常量与入口路径
# =============================================================================

# 配置文件在 PTrade 研究目录下的相对路径。全局只在此处维护：
# 如需迁移目录，仅修改此常量即可；代码其它地方一律通过 CONFIG_REL_PATH 引用。
CONFIG_REL_PATH = "TD913_xiongrui/config.json"

# 日志/CSV 中的日期时间统一格式（字符串类型）。
DATETIME_FMT = "%Y-%m-%d %H:%M:%S"
DATE_FMT = "%Y-%m-%d"

# 配置 schema 中要求必填的顶层段。缺失即视为非法配置。
_REQUIRED_CONFIG_SECTIONS = (
    "universe", "data", "backtest", "setup", "countdown", "trade", "log",
)
_REQUIRED_CONFIG_KEYS = {
    "universe":    ("securities", "benchmark"),
    "data":        ("frequency", "fq", "lookback_count", "min_required_bars"),
    "backtest":    ("slippage", "limit_mode"),
    "setup":       ("require_perfect_for_signal",),
    "countdown":   ("enabled", "tdst_cancel_rule"),
    "trade":       (
        "enabled", "buy_fraction_of_portfolio", "min_trade_value", "min_cash_for_buy",
        "take_profit_on_sell_countdown", "stop_loss_enabled", "profit_target_r_multiple",
        "require_perfect_setup_for_buy",
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
    full_path = _join_research_path(config_rel_path)
    try:
        f = open(full_path, "r", encoding="utf-8")
    except Exception as e:
        raise FileNotFoundError("配置文件不存在或不可读: {} | err={}".format(full_path, e))
    try:
        cfg = json.load(f)
    finally:
        f.close()

    # 剥离以下划线开头的注释/预留说明字段，保持对策略透明
    cfg = _strip_underscore_keys(cfg)
    _validate_config_schema(cfg)
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
        (rel_dir, abs_dir) 二元组。rel_dir 相对研究目录；abs_dir 为绝对路径。
    """
    parent_rel, _ = _split_parent_rel(config_rel_path)
    base_name = start_date_str
    parent_abs = _join_research_path(parent_rel).rstrip("/\\")

    # 先尝试确保父目录存在（与 config.json 同级）。如果本来就存在，
    # PTrade 的 create_dir 一般也会静默返回。
    if parent_rel:
        try:
            create_dir(parent_rel)  # noqa: F821 - PTrade 注入
        except Exception:
            pass

    for suffix in range(0, _MAX_DIR_SUFFIX_TRIES):
        candidate = base_name if suffix == 0 else "{}_{}".format(base_name, suffix)
        candidate_rel = (parent_rel + "/" + candidate) if parent_rel else candidate
        candidate_abs = parent_abs + "/" + candidate

        # 目录中已有 .initialized 标记 → 说明是之前某一次运行留下来的
        if _file_exists(candidate_abs + "/" + _RUN_MARKER_NAME):
            continue

        # 试着创建目录（若已存在且为空，PTrade 的 create_dir 一般不会抛错；
        # 若抛错则视为创建失败，尝试下一个后缀）。
        try:
            create_dir(candidate_rel)  # noqa: F821 - PTrade 注入
        except Exception:
            # 创建失败一般意味着该名字已被占用但没有 .initialized —— 跳过
            continue

        # 标记这个目录为"本次运行占用"。marker 写入失败不致命，最多下次同日运行覆盖。
        try:
            mf = open(candidate_abs + "/" + _RUN_MARKER_NAME, "w", encoding="utf-8")
            try:
                mf.write("run_start={}\nsuffix={}\n".format(start_date_str, suffix))
            finally:
                mf.close()
        except Exception:
            pass

        return candidate_rel, candidate_abs

    # 兜底：超出尝试上限仍未成功，退化为不带后缀，交由 PTrade 决定
    fallback_rel = (parent_rel + "/" + base_name) if parent_rel else base_name
    return fallback_rel, parent_abs + "/" + base_name


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
            self.logger.info(
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
            self.logger.info(
                "Countdown 暂记 + (满足一般条件但不满足完美)",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime, direction=dir_str,
            )
        elif ev_type == "COMPLETE":
            self.logger.info(
                "Countdown 完成",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction=dir_str, perfect=ev.get("perfect", False),
                setup_first_dt=record["setup_first_dt"],
            )
        elif ev_type == "CANCEL":
            self.logger.info(
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


class SignalRecorder:
    """
    把信号事件写入 **每标的一份** CSV 文件。命名仅用标的代码（剥离 '.'），
    不含日期；所有 CSV 位于本次运行的输出目录下。

    字段顺序（含扩展的 setup/countdown 细节，便于离线分析）：
        datetime, security, frequency, category, type, direction, count,
        perfect, reason,
        setup_type, setup_perfect, setup_first_dt, setup_last_dt,
        tdst_threshold, countdown_start_dt
    """

    HEADER_FIELDS = [
        "datetime", "security", "frequency", "category", "type",
        "direction", "count", "perfect", "reason",
        "setup_type", "setup_perfect", "setup_first_dt", "setup_last_dt",
        "setup_highest_high", "tdst_threshold", "countdown_start_dt",
        "count_1_dt", "count_2_dt", "count_3_dt", "count_4_dt", "count_5_dt",
        "count_6_dt", "count_7_dt", "count_8_dt", "countdown_8_close",
        "count_9_dt", "count_10_dt", "count_11_dt", "count_12_dt", "count_13_dt",
        "countdown_low_dt", "countdown_low", "countdown_low_bar_high",
        "bar_close", "bar_high", "bar_low",
    ]

    def __init__(self, output_dir_abs, logger, enabled=True):
        """
        output_dir_abs: 绝对 / 本地输出目录路径（已经由 prepare_output_dir 创建）。
        """
        self.enabled = bool(enabled)
        self.logger = logger
        self.output_dir_abs = output_dir_abs.rstrip("/\\")
        # 记录每个 security 对应 CSV 文件路径的缓存；也用于避免重复写表头
        self._header_written_paths = set()

    def _csv_path_for(self, security):
        return "{}/{}.csv".format(self.output_dir_abs, _security_to_filename(security))

    def write_events(self, events):
        if not self.enabled or not events:
            return

        # 按 security 分组；单个 CSV 只 open 一次，减少 IO
        grouped = {}
        for ev in events:
            sec = ev.get("security", "unknown")
            grouped.setdefault(sec, []).append(ev)

        for security, sec_events in grouped.items():
            self._write_group(security, sec_events)

    def _write_group(self, security, sec_events):
        path = self._csv_path_for(security)
        try:
            need_header = self._need_header(path)
            f = open(path, "a" if not need_header else "w", encoding="utf-8")
            try:
                if need_header:
                    f.write(",".join(self.HEADER_FIELDS) + "\n")
                for ev in sec_events:
                    row = [self._csv_escape(ev.get(k, "")) for k in self.HEADER_FIELDS]
                    f.write(",".join(row) + "\n")
            finally:
                f.close()
            self._header_written_paths.add(path)
            self.logger.debug(
                "信号已写入 CSV",
                security=security, count=len(sec_events), path=path,
            )
        except Exception as e:
            self.logger.error("写入信号 CSV 失败", security=security, path=path, err=e)

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
    文件按标的分流：trade_record_600519SS.csv。

    只在生命周期终态写入：
      * Buy Countdown 被取消
      * Buy Countdown 完成但交易被取消/跳过
      * 已买入仓位被卖出（止损、止盈、趋势反转、外部中止）
    """

    HEADER_FIELDS = [
        "security",
        "setup_completed_at", "setup_is_perfect", "setup_highest_high",
        "count_1_at", "count_2_at", "count_3_at", "count_4_at", "count_5_at",
        "count_6_at", "count_7_at", "count_8_at", "count_8_close",
        "count_9_at", "count_10_at", "count_11_at", "count_12_at", "count_13_at",
        "countdown_completed_count", "countdown_is_perfect", "countdown_status",
        "bought", "buy_reject_reason", "buy_quantity", "buy_price", "buy_date",
        "stop_loss_price", "take_profit_price",
        "sell_price", "sell_date", "sell_reason", "pnl",
    ]

    def __init__(self, output_dir_abs, logger, enabled=True):
        self.enabled = bool(enabled)
        self.logger = logger
        self.output_dir_abs = output_dir_abs.rstrip("/\\")
        self._header_written_paths = set()

    def _csv_path_for(self, security):
        return "{}/trade_record_{}.csv".format(self.output_dir_abs, _security_to_filename(security))

    def record(self, row):
        if not self.enabled:
            return
        security = row.get("security", "unknown")
        path = self._csv_path_for(security)
        try:
            need_header = self._need_header(path)
            f = open(path, "a" if not need_header else "w", encoding="utf-8")
            try:
                if need_header:
                    f.write(",".join(self.HEADER_FIELDS) + "\n")
                values = [SignalRecorder._csv_escape(row.get(k, "")) for k in self.HEADER_FIELDS]
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

class TradeExecutor:
    """
    基于"上一根已收盘 K 线"产生的信号，在"今天盘中"下单。

    时序（修复 available_cash=0 的根因）:
        1. 数据层 include=False，导致 handle_data 拿到的最后一根 K 线 = 昨日（上一 bar）
        2. 信号层基于昨日 K 线计算 TD，发出的完成信号其 event.datetime = 昨日
        3. 交易层读取 context.portfolio 的当前总资产与可用现金（此时为今日盘中状态）
        4. order_value 以当日市价下单

    关于"available_cash=0.0"的排查:
        * 旧实现通过 `getattr(..., "available_cash", 0.0)` 读取，任何异常 / 缺失属性
          都会直接落到 0.0。现在改为：显式尝试 `portfolio.available_cash` →
          `portfolio.cash` → `portfolio._cash` → `portfolio.starting_cash` →
          `context.capital_base` 多重回退，并在读取到 0.0 时将快照完整写入日志，
          便于定位到底是哪一层返回了 0。
        * 另外，`total_value` 也做类似健壮处理。

    必要检查:
        * 空仓（仓位数量 ≤ 0 且仓位市值 ≤ 0）→ 禁止卖出
        * 满仓（可用现金 < min_cash_for_buy）→ 禁止买入
    """

    def __init__(self, cfg_trade, logger, trade_recorder=None):
        self.enabled = bool(cfg_trade.get("enabled", True))
        self.fraction = float(cfg_trade.get("buy_fraction_of_portfolio", 0.1))
        self.min_trade_value = float(cfg_trade.get("min_trade_value", 1000))
        self.min_cash_for_buy = float(cfg_trade.get("min_cash_for_buy", 1000))
        self.logger = logger
        self.trade_recorder = trade_recorder

    # ----- 对外入口 -----
    def execute_for_events(self, context, security, last_bar_events):
        """
        last_bar_events: 上一根已收盘 K 线产生的事件（= 今日可用于下单的信号）。
        仅处理 *_COUNTDOWN_COMPLETE 事件。
        """
        if not self.enabled or not last_bar_events:
            return

        completion_events = []
        for ev in last_bar_events:
            if ev.get("category") != "COUNTDOWN":
                continue
            ev_type = ev.get("type", "")
            if ev_type.startswith("BUY_COUNTDOWN_COMPLETE") or ev_type.startswith("SELL_COUNTDOWN_COMPLETE"):
                completion_events.append(ev)

        if not completion_events:
            return

        for ev in completion_events:
            self._execute_single_event(context, security, ev)

    # ----- 单事件处理 -----
    def _execute_single_event(self, context, security, ev):
        direction = int(ev.get("direction", 0))
        if direction not in (1, -1):
            self.logger.warning("交易方向非法，跳过", security=security, direction=direction, event_type=ev.get("type"))
            return

        snap = self._portfolio_snapshot(context)
        total_value = snap["total_value"]
        available_cash = snap["available_cash"]
        target_trade_value = total_value * self.fraction

        pos_qty, pos_value = self._get_position_snapshot(security)
        decision_dt = _format_dt(self._current_dt(context))
        base_row = self._make_base_row(decision_dt, security, direction, ev, snap, pos_qty, pos_value, target_trade_value)

        # 目标金额过小（总资产本身就极低或 fraction 设置过小）
        if target_trade_value < self.min_trade_value:
            self.logger.info(
                "交易金额过小，跳过",
                security=security, event_type=ev.get("type"),
                trade_value=round(target_trade_value, 2), min_trade_value=self.min_trade_value,
                total_value=round(total_value, 2),
            )
            self._record(base_row, status="SKIP_MIN_VALUE", actual_value=0.0)
            return

        # 买入前风控：可用现金是否足够
        if direction == 1 and available_cash < self.min_cash_for_buy:
            self.logger.info(
                "满仓或现金不足，禁止买入",
                security=security, event_type=ev.get("type"),
                available_cash=round(available_cash, 2), min_cash_for_buy=self.min_cash_for_buy,
                total_value=round(total_value, 2),
                # 全快照：方便定位是哪一层拿到的 0
                snapshot=snap["debug"],
            )
            self._record(base_row, status="SKIP_FULL_POSITION", actual_value=0.0)
            return

        # 卖出前风控：空仓
        if direction == -1 and pos_qty <= 0 and pos_value <= 0:
            self.logger.info(
                "空仓状态，禁止卖出",
                security=security, event_type=ev.get("type"),
                position_qty=pos_qty, position_value=round(pos_value, 2),
            )
            self._record(base_row, status="SKIP_EMPTY_POSITION", actual_value=0.0)
            return

        # 计算实际下单金额并下单
        if direction == 1:
            actual_value = min(target_trade_value, available_cash)
            if actual_value < self.min_trade_value:
                self.logger.info(
                    "可买金额不足最小门槛，跳过",
                    security=security, event_type=ev.get("type"),
                    actual_value=round(actual_value, 2), min_trade_value=self.min_trade_value,
                )
                self._record(base_row, status="SKIP_MIN_CASH", actual_value=round(actual_value, 2))
                return
            try:
                order_value(security, actual_value)  # noqa: F821 - PTrade 注入
            except Exception as e:
                self.logger.error("order_value 调用失败", security=security, err=e, value=actual_value)
                self._record(base_row, status="ORDER_ERROR", actual_value=round(actual_value, 2))
                return
            self.logger.info(
                "执行买入",
                security=security, event_type=ev.get("type"),
                total_value=round(total_value, 2), available_cash=round(available_cash, 2),
                target_trade_value=round(target_trade_value, 2), actual_trade_value=round(actual_value, 2),
            )
            self._record(base_row, status="EXECUTED", actual_value=round(actual_value, 2))
            return

        # direction == -1: 卖出
        actual_value = min(target_trade_value, max(pos_value, 0.0))
        if actual_value < self.min_trade_value:
            self.logger.info(
                "可卖金额不足最小门槛，跳过",
                security=security, event_type=ev.get("type"),
                actual_value=round(actual_value, 2), min_trade_value=self.min_trade_value,
            )
            self._record(base_row, status="SKIP_NO_POSITION_VALUE", actual_value=round(actual_value, 2))
            return
        try:
            order_value(security, -actual_value)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error("order_value 调用失败", security=security, err=e, value=-actual_value)
            self._record(base_row, status="ORDER_ERROR", actual_value=round(actual_value, 2))
            return
        self.logger.info(
            "执行卖出",
            security=security, event_type=ev.get("type"),
            total_value=round(total_value, 2),
            position_qty=pos_qty, position_value=round(pos_value, 2),
            target_trade_value=round(target_trade_value, 2), actual_trade_value=round(actual_value, 2),
        )
        self._record(base_row, status="EXECUTED", actual_value=round(actual_value, 2))

    # ----- 账户快照（健壮读取，多回退） -----
    def _portfolio_snapshot(self, context):
        portfolio = getattr(context, "portfolio", None)
        debug = {"has_portfolio": portfolio is not None}

        def read_attr(obj, names):
            """依次尝试从 obj 读取 names 中的属性，返回 (value, hit_name)；都失败返回 (None, None)。"""
            if obj is None:
                return None, None
            for name in names:
                try:
                    if hasattr(obj, name):
                        val = getattr(obj, name)
                        # 可调用（方法）则 call
                        if callable(val):
                            val = val()
                        v = self._safe_float(val)
                        return v, name
                except Exception:
                    continue
            return None, None

        # total_value / portfolio_value
        total_value, tv_src = read_attr(portfolio, [
            "total_value", "portfolio_value", "totalValue", "total_asset", "assets",
        ])
        debug["total_value_from"] = tv_src
        if total_value is None:
            total_value = 0.0

        # available_cash 多重回退
        available_cash, ac_src = read_attr(portfolio, [
            "available_cash", "cash", "_cash", "starting_cash", "capital_used_cash",
        ])
        debug["available_cash_from"] = ac_src
        if available_cash is None:
            # 兜底：尝试 context.capital_base
            cb = self._safe_float(getattr(context, "capital_base", 0.0))
            available_cash = cb
            debug["available_cash_from"] = "context.capital_base"

        # 若 total_value 仍为 0，尝试 cash + positions_value 手工求和
        if total_value <= 0 and portfolio is not None:
            pv = self._safe_float(getattr(portfolio, "positions_value", 0.0))
            total_value = available_cash + pv
            debug["total_value_from"] = "cash+positions_value"

        debug["available_cash"] = available_cash
        debug["total_value"] = total_value

        return {
            "total_value": total_value,
            "available_cash": available_cash,
            "debug": debug,
        }

    # ----- 工具 -----
    @staticmethod
    def _current_dt(context):
        """优先从 context.blotter.current_dt 取；退到 context.current_dt。"""
        try:
            return context.blotter.current_dt
        except Exception:
            pass
        return getattr(context, "current_dt", None)

    def _make_base_row(self, decision_dt, security, direction, ev, snap, pos_qty, pos_value, target_value):
        action = "BUY" if direction == 1 else "SELL"
        return {
            "decision_dt": decision_dt,
            "security": security,
            "action": action,
            "signal_type": ev.get("type", ""),
            "signal_bar_dt": ev.get("datetime", ""),
            "count": ev.get("count", ""),
            "perfect": ev.get("perfect", ""),
            "setup_type": ev.get("setup_type", ""),
            "setup_perfect": ev.get("setup_perfect", ""),
            "setup_first_dt": ev.get("setup_first_dt", ""),
            "setup_last_dt": ev.get("setup_last_dt", ""),
            "countdown_start_dt": ev.get("countdown_start_dt", ""),
            "tdst_threshold": ev.get("tdst_threshold", ""),
            "total_value": round(snap["total_value"], 2),
            "available_cash": round(snap["available_cash"], 2),
            "position_qty": pos_qty,
            "position_value": round(pos_value, 2),
            "target_value": round(target_value, 2),
        }

    def _record(self, base_row, status, actual_value):
        if self.trade_recorder is None:
            return
        row = dict(base_row)
        row["status"] = status
        row["actual_value"] = actual_value
        self.trade_recorder.record(row)

    @staticmethod
    def _safe_float(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    def _get_position_snapshot(self, security):
        """
        返回 (仓位数量, 仓位市值)，兼容不同柜台字段命名。
        """
        try:
            pos = get_position(security)  # noqa: F821 - PTrade 注入
        except Exception:
            pos = None
        if pos is None:
            return 0.0, 0.0

        qty_candidates = [
            "current_amount", "total_amount", "enable_amount", "amount", "volume", "qty",
        ]
        value_candidates = [
            "market_value", "position_value", "value", "cost_balance",
        ]
        qty = 0.0
        for k in qty_candidates:
            if hasattr(pos, k):
                qty = self._safe_float(getattr(pos, k, 0.0))
                if qty > 0:
                    break
        pos_value = 0.0
        for k in value_candidates:
            if hasattr(pos, k):
                pos_value = self._safe_float(getattr(pos, k, 0.0))
                if pos_value > 0:
                    break
        return qty, pos_value


# =============================================================================
# [7.1] 交易执行层（当前版本）
# =============================================================================

COUNTDOWN_STATUS_NORMAL = "NORMAL"
COUNTDOWN_STATUS_CANCEL_TDST_PREFIX = "CANCEL_BY_TDST_RULE_"
COUNTDOWN_STATUS_CANCEL_OPPOSITE_SETUP = "CANCEL_BY_OPPOSITE_SETUP"
COUNTDOWN_STATUS_CANCEL_SAME_SETUP = "CANCEL_BY_SAME_SETUP"

BUY_REJECT_TRADE_DISABLED = "TRADE_DISABLED"
BUY_REJECT_NON_PERFECT_SETUP = "NON_PERFECT_SETUP"
BUY_REJECT_ALREADY_OPEN_POSITION = "ALREADY_OPEN_POSITION"
BUY_REJECT_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
BUY_REJECT_MIN_TRADE_VALUE = "MIN_TRADE_VALUE"
BUY_REJECT_ORDER_REJECTED = "ORDER_REJECTED"
BUY_REJECT_ORDER_ERROR = "ORDER_ERROR"

SELL_REASON_STOP_LOSS = "STOP_LOSS"
SELL_REASON_PROFIT_TARGET = "PROFIT_TARGET"
SELL_REASON_TREND_REVERSAL = "TREND_REVERSAL"
SELL_REASON_EXTERNAL_ABORT = "EXTERNAL_ABORT"


class TradeExecutor:
    """
    当前交易策略：
      * 默认只根据 BUY_COUNTDOWN_COMPLETE 买入；
      * 每次买入总资产 buy_fraction_of_portfolio；
      * SELL_COUNTDOWN_COMPLETE 默认只保留信号，不用于卖出；
      * 若 take_profit_on_sell_countdown=true，则出现 SELL_COUNTDOWN_COMPLETE 且仓位盈利时卖出；
      * 买入后设置 stop_loss_price 与 take_profit_price；
      * 实盘环境用 on_trade_response 确认买入成交后建立风控仓位，tick_data 中追踪止盈止损；
      * 回测环境降级为：order_value 返回订单后按当前持仓快照近似确认成交，
        并在后续 handle_data 的上一根已完成 K 线上用 high/low 判断止盈止损。
    """

    def __init__(self, cfg_trade, logger, trade_recorder=None):
        self.enabled = bool(cfg_trade.get("enabled", True))
        self.buy_fraction = float(cfg_trade.get("buy_fraction_of_portfolio", 0.1))
        self.min_trade_value = float(cfg_trade.get("min_trade_value", 1000))
        self.min_cash_for_buy = float(cfg_trade.get("min_cash_for_buy", 1000))
        self.take_profit_on_sell_countdown = bool(cfg_trade.get("take_profit_on_sell_countdown", False))
        self.stop_loss_enabled = bool(cfg_trade.get("stop_loss_enabled", True))
        self.profit_target_r_multiple = float(cfg_trade.get("profit_target_r_multiple", 1.5))
        self.require_perfect_setup_for_buy = bool(cfg_trade.get("require_perfect_setup_for_buy", False))
        self.logger = logger
        self.trade_recorder = trade_recorder

        # 每个标的当前最多跟踪一笔完整交易。后续若需要金字塔/多笔并行，可扩展为 list。
        self.open_trades = {}
        self.pending_buy_orders = {}
        self.pending_sell_orders = {}

    def execute_for_events(self, context, security, last_bar_events):
        if not last_bar_events:
            return

        for ev in last_bar_events:
            if ev.get("category") != "COUNTDOWN":
                continue
            ev_type = ev.get("type", "")
            direction = int(ev.get("direction", 0))

            # Buy countdown 取消：生命周期在"计数取消"处结束，写一行。
            if direction == 1 and ev_type == "BUY_COUNTDOWN_CANCEL":
                self._record_terminal(ev, bought=False, buy_reject_reason="", countdown_status=self._countdown_status(ev))
                continue

            # Buy countdown 完成：尝试买入；若不能买，生命周期在"交易取消"处结束，写一行。
            if direction == 1 and ev_type.startswith("BUY_COUNTDOWN_COMPLETE"):
                self._try_buy_from_signal(context, security, ev)
                continue

            # Sell countdown 完成：默认不交易；配置开启时，仅盈利仓位趋势反转止盈。
            if direction == -1 and ev_type.startswith("SELL_COUNTDOWN_COMPLETE"):
                self._try_sell_on_sell_countdown(context, security, ev)

    def check_backtest_exits(self, context, security, completed_bar):
        """
        回测降级：用上一根已完成 K 线的 high/low 判断止盈止损。
        注意：刚根据该 completed_bar 信号买入的仓位，会把 last_checked_bar_dt 初始化为
        signal datetime，因此不会在同一根历史 K 上立刻被止盈/止损。
        """
        if self._is_live_trade() or security not in self.open_trades:
            return
        trade = self.open_trades[security]
        bar_dt = completed_bar.datetime
        if trade.get("last_checked_bar_dt") and bar_dt <= trade.get("last_checked_bar_dt"):
            return

        stop_loss = self._safe_float(trade.get("stop_loss_price", 0.0))
        take_profit = self._safe_float(trade.get("take_profit_price", 0.0))

        # 同一根 K 线同时触发时，保守按先止损处理。
        if self.stop_loss_enabled and stop_loss > 0 and completed_bar.low <= stop_loss:
            self._sell_open_trade(context, security, stop_loss, completed_bar.datetime, SELL_REASON_STOP_LOSS)
            return
        if take_profit > 0 and completed_bar.high >= take_profit:
            self._sell_open_trade(context, security, take_profit, completed_bar.datetime, SELL_REASON_PROFIT_TARGET)
            return

        trade["last_checked_bar_dt"] = bar_dt

    def check_tick_exits(self, context, tick_data_obj):
        """实盘 tick_data 中追踪止盈止损。回测环境不执行。"""
        if not self._is_live_trade():
            return
        for security in list(self.open_trades.keys()):
            price = self._extract_tick_price(tick_data_obj, security)
            if price <= 0:
                continue
            trade = self.open_trades[security]
            if self.stop_loss_enabled and price <= self._safe_float(trade.get("stop_loss_price", 0.0)):
                self._submit_sell_order(context, security, price, SELL_REASON_STOP_LOSS)
                continue
            if price >= self._safe_float(trade.get("take_profit_price", 0.0)):
                self._submit_sell_order(context, security, price, SELL_REASON_PROFIT_TARGET)

    def on_trade_response(self, context, trade_response):
        """
        实盘成交回报：买入成交后才建立风控仓位；卖出成交后才写终态记录。
        回测环境 is_trade()=False 时不会依赖该回调。
        """
        if not self._is_live_trade():
            return
        order_id = self._read_any(trade_response, ["order_id", "entrust_no", "order_no", "id"])
        if not order_id:
            return
        order_id = str(order_id)

        price = self._safe_float(self._read_any(trade_response, ["price", "business_price", "filled_price", "trade_price"]))
        qty = self._safe_float(self._read_any(trade_response, ["amount", "business_amount", "filled_amount", "trade_amount", "volume"]))
        trade_dt = _format_dt(self._read_any(trade_response, ["datetime", "dt", "trade_time", "business_time"]))

        if order_id in self.pending_buy_orders:
            pending = self.pending_buy_orders.pop(order_id)
            security = pending["security"]
            if price <= 0:
                price = self._safe_float(pending.get("fallback_price"))
            if qty <= 0:
                qty = self._safe_float(pending.get("fallback_qty"))
            self._open_trade_from_fill(security, pending["event"], price, qty, trade_dt or pending["decision_dt"])
            return

        if order_id in self.pending_sell_orders:
            pending = self.pending_sell_orders.pop(order_id)
            security = pending["security"]
            if price <= 0:
                price = self._safe_float(pending.get("fallback_price"))
            self._finalize_sell(security, price, trade_dt or pending["sell_date"], pending["sell_reason"])

    # ----- Buy side -----
    def _try_buy_from_signal(self, context, security, ev):
        if not self.enabled:
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_TRADE_DISABLED)
            return
        if security in self.open_trades:
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_ALREADY_OPEN_POSITION)
            return
        if self.require_perfect_setup_for_buy and not self._boolish(ev.get("setup_perfect")):
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_NON_PERFECT_SETUP)
            return

        snap = self._portfolio_snapshot(context)
        total_value = snap["total_value"]
        available_cash = snap["available_cash"]
        target_value = total_value * self.buy_fraction

        if target_value < self.min_trade_value:
            self.logger.info("买入取消：目标金额低于最小交易金额", security=security, target_value=round(target_value, 2))
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_MIN_TRADE_VALUE)
            return
        if available_cash < self.min_cash_for_buy:
            self.logger.info(
                "买入取消：现金不足",
                security=security, available_cash=round(available_cash, 2), min_cash_for_buy=self.min_cash_for_buy,
                snapshot=snap["debug"],
            )
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_INSUFFICIENT_CASH)
            return

        actual_value = min(target_value, available_cash)
        if actual_value < self.min_trade_value:
            self.logger.info("买入取消：实际可买金额低于最小交易金额", security=security, actual_value=round(actual_value, 2))
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_MIN_TRADE_VALUE)
            return

        decision_dt = _format_dt(self._current_dt(context))
        fallback_price = self._safe_float(ev.get("bar_close"))
        try:
            order_id = order_value(security, actual_value)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error("买入 order_value 调用失败", security=security, err=e, value=actual_value)
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_ORDER_ERROR)
            return
        if not order_id:
            self.logger.info("买入取消：order_value 未返回订单号", security=security, actual_value=round(actual_value, 2))
            self._record_terminal(ev, bought=False, buy_reject_reason=BUY_REJECT_ORDER_REJECTED)
            return

        if self._is_live_trade():
            self.pending_buy_orders[str(order_id)] = {
                "security": security, "event": ev, "decision_dt": decision_dt,
                "fallback_price": fallback_price, "fallback_qty": 0.0,
            }
            self.logger.info("买入委托已提交，等待成交回报", security=security, order_id=order_id, value=round(actual_value, 2))
            return

        # 回测降级：order_value 返回订单号后，读取持仓快照作为成交近似。
        qty, pos_value, entry_price = self._get_position_detail(security)
        if qty <= 0:
            qty = int(actual_value / fallback_price / 100) * 100 if fallback_price > 0 else 0
        if entry_price <= 0:
            entry_price = fallback_price
        self._open_trade_from_fill(security, ev, entry_price, qty, decision_dt)
        self.logger.info(
            "买入完成（回测近似成交）",
            security=security, order_id=order_id, buy_qty=qty, buy_price=round(entry_price, 4),
        )

    def _open_trade_from_fill(self, security, ev, buy_price, buy_qty, buy_date):
        stop_loss, take_profit = self._calc_risk_prices(ev, buy_price)
        row = self._base_trade_record(ev, COUNTDOWN_STATUS_NORMAL)
        row.update({
            "bought": True,
            "buy_reject_reason": "",
            "buy_quantity": buy_qty,
            "buy_price": round(buy_price, 4),
            "buy_date": buy_date,
            "stop_loss_price": round(stop_loss, 4) if stop_loss else "",
            "take_profit_price": round(take_profit, 4) if take_profit else "",
            "sell_price": "",
            "sell_date": "",
            "sell_reason": "",
            "pnl": "",
        })
        self.open_trades[security] = row
        self.open_trades[security]["last_checked_bar_dt"] = ev.get("datetime", "")

    def _calc_risk_prices(self, ev, buy_price):
        low = self._safe_float(ev.get("countdown_low"))
        high = self._safe_float(ev.get("countdown_low_bar_high"))
        if low <= 0 or high <= 0 or high < low:
            return "", ""
        raw_stop_loss = low - (high - low)
        take_profit = buy_price + (buy_price - raw_stop_loss) * self.profit_target_r_multiple
        stop_loss = raw_stop_loss if self.stop_loss_enabled else ""
        return stop_loss, take_profit

    # ----- Sell side / exits -----
    def _try_sell_on_sell_countdown(self, context, security, ev):
        if not self.take_profit_on_sell_countdown or security not in self.open_trades:
            return
        trade = self.open_trades[security]
        ref_price = self._safe_float(ev.get("bar_close"))
        buy_price = self._safe_float(trade.get("buy_price"))
        if ref_price > buy_price:
            self._sell_open_trade(context, security, ref_price, ev.get("datetime", ""), SELL_REASON_TREND_REVERSAL)
        else:
            self.logger.info(
                "Sell Countdown 出现但仓位未盈利，不做趋势反转止盈",
                security=security, ref_price=round(ref_price, 4), buy_price=round(buy_price, 4),
            )

    def _submit_sell_order(self, context, security, fallback_price, sell_reason):
        if security not in self.open_trades or security in self.pending_sell_orders:
            return
        _, pos_value, _ = self._get_position_detail(security)
        if pos_value <= 0:
            pos_value = self._safe_float(self.open_trades[security].get("buy_price")) * self._safe_float(self.open_trades[security].get("buy_quantity"))
        try:
            order_id = order_value(security, -pos_value)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error("卖出 order_value 调用失败", security=security, err=e, value=-pos_value, reason=sell_reason)
            return
        if not order_id:
            self.logger.info("卖出委托未提交", security=security, value=round(pos_value, 2), reason=sell_reason)
            return
        self.pending_sell_orders[str(order_id)] = {
            "security": security,
            "sell_reason": sell_reason,
            "sell_date": _format_dt(self._current_dt(context)),
            "fallback_price": fallback_price,
        }

    def _sell_open_trade(self, context, security, sell_price, sell_date, sell_reason):
        if security not in self.open_trades:
            return
        if self._is_live_trade():
            self._submit_sell_order(context, security, sell_price, sell_reason)
            return
        _, pos_value, _ = self._get_position_detail(security)
        if pos_value <= 0:
            pos_value = self._safe_float(self.open_trades[security].get("buy_price")) * self._safe_float(self.open_trades[security].get("buy_quantity"))
        try:
            order_id = order_value(security, -pos_value)  # noqa: F821 - PTrade 注入
        except Exception as e:
            self.logger.error("卖出 order_value 调用失败", security=security, err=e, value=-pos_value, reason=sell_reason)
            return
        if not order_id:
            self.logger.info("卖出委托未提交", security=security, value=round(pos_value, 2), reason=sell_reason)
            return
        self._finalize_sell(security, sell_price, sell_date, sell_reason)

    def _finalize_sell(self, security, sell_price, sell_date, sell_reason):
        if security not in self.open_trades:
            return
        row = dict(self.open_trades.pop(security))
        buy_price = self._safe_float(row.get("buy_price"))
        qty = self._safe_float(row.get("buy_quantity"))
        pnl = (sell_price - buy_price) * qty
        row.update({
            "sell_price": round(sell_price, 4),
            "sell_date": sell_date,
            "sell_reason": sell_reason,
            "pnl": round(pnl, 2),
        })
        self.trade_recorder.record(row)
        self.logger.info("交易生命周期结束", security=security, sell_reason=sell_reason, pnl=round(pnl, 2))

    # ----- Records -----
    def _record_terminal(self, ev, bought=False, buy_reject_reason="", countdown_status=None):
        if self.trade_recorder is None:
            return
        status = countdown_status or self._countdown_status(ev)
        row = self._base_trade_record(ev, status)
        row.update({
            "bought": bool(bought),
            "buy_reject_reason": buy_reject_reason,
            "buy_quantity": "",
            "buy_price": "",
            "buy_date": "",
            "stop_loss_price": "",
            "take_profit_price": "",
            "sell_price": "",
            "sell_date": "",
            "sell_reason": "",
            "pnl": "",
        })
        self.trade_recorder.record(row)

    def _base_trade_record(self, ev, countdown_status):
        return {
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

    def _countdown_status(self, ev):
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
        try:
            pos = get_position(security)  # noqa: F821 - PTrade 注入
        except Exception:
            pos = None
        if pos is None:
            return 0.0, 0.0, 0.0
        qty = self._first_attr_float(pos, ["current_amount", "total_amount", "enable_amount", "amount", "volume", "qty"])
        value = self._first_attr_float(pos, ["market_value", "position_value", "value", "cost_balance"])
        price = self._first_attr_float(pos, ["last_sale_price", "price", "cost_basis"])
        if price <= 0 and qty > 0 and value > 0:
            price = value / qty
        return qty, value, price

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
    output_rel, output_abs = prepare_output_dir(start_date_str, CONFIG_REL_PATH)
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
    set_benchmark(universe_cfg["benchmark"])  # noqa: F821
    set_universe(g.securities)  # noqa: F821

    bt_cfg = cfg["backtest"]
    set_slippage(slippage=float(bt_cfg["slippage"]))  # noqa: F821
    set_limit_mode(bt_cfg["limit_mode"])  # noqa: F821

    # (5) 数据层
    data_cfg = cfg["data"]
    g.frequency = data_cfg["frequency"]
    g.fq = data_cfg["fq"]
    g.lookback_count = int(data_cfg["lookback_count"])
    g.min_required_bars = int(data_cfg["min_required_bars"])
    g.data_fetcher = MarketDataFetcher(
        frequency=g.frequency, fq=g.fq, lookback_count=g.lookback_count, logger=g.logger,
    )

    # (6) 信号输出层 & 交易生命周期输出层（per-security CSV + trade_record_<SEC>.csv）
    g.recorder = SignalRecorder(
        output_dir_abs=output_abs,
        logger=g.logger,
        enabled=log_cfg.get("csv_output", True),
    )
    g.trade_recorder = TradeRecordRecorder(
        output_dir_abs=output_abs,
        logger=g.logger,
        enabled=log_cfg.get("csv_output", True),
    )

    # (7) 交易执行层
    g.trade_executor = TradeExecutor(
        cfg_trade=cfg["trade"],
        logger=g.logger,
        trade_recorder=g.trade_recorder,
    )

    # (8) 当日有效（非停牌）标的列表，由 before_trading_start 每日刷新
    g.active_securities_today = list(g.securities)

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
        4. 交易层基于这些信号在今日下单；所有事件写入 per-security 信号 CSV
    """
    all_today_events = []

    for security in g.active_securities_today:
        # (1) 二次确认当日是否停牌，避免对今日无法交易的标的下单
        try:
            current_dt = context.blotter.current_dt
            query_date = current_dt.strftime("%Y%m%d")
        except Exception:
            query_date = None

        if MarketDataFetcher.is_halt_today(security, query_date):
            g.logger.info(
                "标的当日停牌，跳过", security=security, frequency=g.frequency, datetime_=query_date,
            )
            continue

        # (2) 数据层
        bars = g.data_fetcher.fetch_recent_bars(security)
        if len(bars) < g.min_required_bars:
            g.logger.warning(
                "有效 K 线数量不足，跳过本周期",
                security=security, frequency=g.frequency, valid=len(bars),
                min_required=g.min_required_bars,
            )
            continue

        last_bar_dt = bars[-1].datetime  # 昨日（或更早的已收盘 K 线）

        # 回测降级：没有 tick_data / on_trade_response 时，用上一根已完成 K 线的 high/low
        # 追踪已有仓位的止盈止损。实盘环境中该逻辑由 tick_data 负责。
        g.trade_executor.check_backtest_exits(context, security, bars[-1])

        # (3) 信号层：每日全量重算
        processor = TDSignalProcessor(
            security=security, frequency=g.frequency, config=g.config, logger=g.logger,
        )
        all_events = processor.run(bars)

        # (4) 仅关注"最后一根 K 线"产生的事件——这些是"今日可执行"的新鲜信号
        last_bar_events = [ev for ev in all_events if ev["datetime"] == last_bar_dt]

        g.logger.debug(
            "本周期事件统计",
            security=security, frequency=g.frequency, signal_bar_dt=last_bar_dt,
            historical_events=len(all_events) - len(last_bar_events),
            last_bar_events=len(last_bar_events),
        )

        if last_bar_events:
            for ev in last_bar_events:
                g.logger.info(
                    "LAST-BAR SIGNAL",
                    security=ev["security"], frequency=ev["frequency"],
                    signal_bar_dt=ev["datetime"],
                    type=ev["type"], direction=ev["direction"], count=ev.get("count"),
                    perfect=ev.get("perfect", ""),
                )
            # (5) 交易层：按"昨日信号 → 今日下单"的时序执行
            g.trade_executor.execute_for_events(
                context=context,
                security=security,
                last_bar_events=last_bar_events,
            )
            all_today_events.extend(last_bar_events)

    # (6) 全量写入信号 CSV（按 security 分流到不同文件）
    if all_today_events:
        g.recorder.write_events(all_today_events)


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
    实盘成交回报：买入成交后建立风控仓位；卖出成交后写 trade_record_*.csv。
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