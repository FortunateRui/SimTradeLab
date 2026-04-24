# -*- coding: utf-8 -*-
"""
策略名: TD_9_13_Sequential_Signal
功能:   基于 TD 9-13 Sequential 的信号识别（仅识别 Setup / Countdown 信号，不下单交易）
环境:   PTrade 回测 / 交易引擎

模块结构（单文件，按层划分；除 config.json 外不依赖任何其它本地文件）:
    [1] 常量与默认配置        DEFAULT_CONFIG
    [2] 路径与配置加载层      _join_research_path / load_config
    [3] 日志层                StrategyLogger
    [4] 数据层                Bar / MarketDataFetcher
    [5] 信号层                SetupMachine / CountdownMachine / TDSignalProcessor
    [6] 信号输出层            SignalRecorder
    [7] PTrade 策略钩子层     initialize / before_trading_start / handle_data / after_trading_end

设计要点:
    * 数据层与信号层完全解耦：信号层只接受 Bar 序列，不感知数据来源；数据层只负责
      从 PTrade API 获取并清洗 Bar 序列，不感知任何信号语义。
    * 不再使用任何持久化缓存；按 README 推荐采用 "前复权 + 每日全量重算"。
    * 跳过停牌日：只要 K 线 volume<=0 即视为停牌，从源头过滤；同时调用
      get_stock_status('HALT') 进一步确认当日是否停牌，停牌则当日不产生信号。
    * 仅在 PTrade 接口允许的钩子内调用对应接口（如 get_history / get_price 不能
      在 initialize 中调用，所有数据获取放在 before_trading_start / handle_data）。
    * 周期统一：本策略不支持混合频率，统一使用 config.data.frequency。
"""

import json


# =============================================================================
# [1] 常量与默认配置
# =============================================================================

DEFAULT_CONFIG = {
    "universe": {
        "securities": ["600519.SS"],
        "benchmark": "000300.SS",
    },
    "data": {
        "frequency": "1d",
        "fq": "pre",
        "lookback_count": 300,
        "min_required_bars": 20,
    },
    "backtest": {
        "slippage": 0.001,
        "limit_mode": "UNLIMITED",
    },
    "setup": {
        "enabled": True,
        "require_perfect_for_signal": False,
        "extend_record_max": 18,
    },
    "intersection": {
        "enabled": False,  # 预留，后续更新
    },
    "countdown": {
        "enabled": True,
        "type": "sequence",          # 预留 combo
        "require_perfect": False,
        "tdst_cancel_rule": 4,       # 1~5
        "cancel_on_opposite_setup": True,
        "cancel_on_same_setup": True,
    },
    "log": {
        "level": "INFO",
        "verbose_state_transition": False,
        "verbose_countdown_step": True,
        "csv_output": True,
        "output_dir": "TDSignal",
    },
}


# =============================================================================
# [2] 路径与配置加载层
# =============================================================================

def _join_research_path(rel_path):
    """
    将相对路径拼接到 PTrade 的研究目录根路径下。
    PTrade 禁用 os 模块，因此手工拼接。
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


def _deep_merge(default, override):
    """
    递归合并配置。override 中的值覆盖 default 中的同名键；override 中不存在的
    键沿用 default。仅对 dict 进行递归，list / 标量直接覆盖。
    """
    if not isinstance(default, dict) or not isinstance(override, dict):
        return override if override is not None else default
    result = dict(default)
    for k, v in override.items():
        if k.startswith("_"):
            # 以下划线开头的键视为注释/预留说明，不参与合并
            continue
        if k in result:
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_config(config_rel_path):
    """
    从研究目录下读取 config.json。文件不存在或解析失败时使用内置默认配置。
    """
    full_path = _join_research_path(config_rel_path)
    user_cfg = None
    try:
        f = open(full_path, "r", encoding="utf-8")
        try:
            user_cfg = json.load(f)
        finally:
            f.close()
    except Exception as e:
        log.warning("[CONFIG] 读取配置文件失败，将使用内置默认配置 | path={}, err={}".format(full_path, e))  # noqa: F821

    cfg = _deep_merge(DEFAULT_CONFIG, user_cfg or {})
    log.info("[CONFIG] 配置加载完成 | path={}".format(full_path))  # noqa: F821
    return cfg


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
        拉取该标的最近 lookback_count 根 K 线（含当前周期），按时间升序返回。
        过滤掉 volume<=0 的停牌 K 线。

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
                include=True,
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
        """统一日期时间格式为字符串，便于跨场景比较与写日志。"""
        try:
            # pandas Timestamp / datetime 均有 strftime
            return dt.strftime("%Y%m%d%H%M%S")
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
        """根据当前状态和输入返回新状态 (sd, sc)。原项目 FSM 的等价实现。"""
        sd, sc = state

        # q0
        if (sd, sc) == (0, 0):
            return (1, 0) if inp == "BS" else (-1, 0) if inp == "SS" else (0, 0)
        # q1
        if (sd, sc) == (-1, 0):
            return (1, 1) if inp == "BS" else (-1, 0) if inp == "SS" else (0, 0)
        # q2
        if (sd, sc) == (1, 1):
            return (1, 2) if inp == "BS" else (-1, 1) if inp == "SS" else (0, 0)
        # q3 (buy setup ongoing 2..8)
        if sd == 1 and 2 <= sc <= 8:
            return (1, sc + 1) if inp == "BS" else (-1, 1) if inp == "SS" else (0, 0)
        # q4 (buy setup done)
        if (sd, sc) == (1, 9):
            return (1, 0) if inp == "BS" else (-1, 1) if inp == "SS" else (0, 0)
        # q5 (post buy setup)
        if (sd, sc) == (1, 0):
            return (1, 0) if inp == "BS" else (-1, 1) if inp == "SS" else (0, 0)
        # q6 (sell setup d1)
        if (sd, sc) == (-1, 1):
            return (1, 1) if inp == "BS" else (-1, 2) if inp == "SS" else (0, 0)
        # q7 (sell setup ongoing 2..8)
        if sd == -1 and 2 <= sc <= 8:
            return (1, 1) if inp == "BS" else (-1, sc + 1) if inp == "SS" else (0, 0)
        # q8 (sell setup done)
        if (sd, sc) == (-1, 9):
            return (1, 0) if inp == "BS" else (-1, 1) if inp == "SS" else (0, 0)

        raise ValueError("invalid setup state: ({}, {})".format(sd, sc))

    def _apply_state_action(self, bar):
        """根据进入的新状态维护 setup_bars，并在第 9 根完成时返回信号。"""
        sd, sc = self.sd, self.sc

        if (sd, sc) in ((0, 0), (-1, 0), (1, 0)):
            self.setup_bars = []
            return None

        if (sd, sc) in ((1, 1), (-1, 1)):
            # 新 setup 第 1 根
            self.setup_bars = [bar]
            return None

        if sd == 1 and 2 <= sc <= 8:
            self.setup_bars.append(bar)
            return None

        if sd == -1 and 2 <= sc <= 8:
            self.setup_bars.append(bar)
            return None

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

        return None


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

    def __init__(self, direction, setup_bars, config_countdown, logger, security, frequency):
        """
        direction: 1=Buy, -1=Sell
        setup_bars: 触发本 Countdown 的 9 根 setup K 线（按时间升序）
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
        record = {
            "datetime": bar.datetime,
            "security": self.security,
            "frequency": self.frequency,
            "category": "SETUP",
            "type": type_name + ("_PERFECT" if setup_signal["perfect"] else ""),
            "direction": 1 if setup_signal["type"] == "BUY_SETUP" else -1,
            "perfect": setup_signal["perfect"],
            "extra": "setup_first_dt={}".format(setup_signal["setup_bars"][0].datetime),
        }
        events_out.append(record)
        self.logger.info(
            "Setup 完成", security=self.security, frequency=self.frequency, datetime_=bar.datetime,
            type=record["type"], perfect=setup_signal["perfect"],
        )

    def _handle_countdown_on_setup_complete(self, setup_signal, bar, events_out):
        """根据 setup 完成情况决定是否取消旧 countdown / 启动新 countdown。"""
        if not self.config["countdown"].get("enabled", True):
            return

        new_dir = 1 if setup_signal["type"] == "BUY_SETUP" else -1

        # 是否需要 require_perfect_for_signal 才启动 countdown？
        # 按 README，require_perfect_for_signal 控制的是 "信号是否输出"；
        # 这里若开启，则非完美 setup 不启动 countdown（信号本身已经在 _emit_setup_signal 输出）。
        # 为避免歧义，单独尊重 setup.require_perfect_for_signal：
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
            self.active_countdown = CountdownMachine(
                direction=new_dir,
                setup_bars=setup_signal["setup_bars"],
                config_countdown=self.config["countdown"],
                logger=self.logger,
                security=self.security,
                frequency=self.frequency,
            )
            self.logger.info(
                "Countdown 启动", security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction="BUY" if new_dir == 1 else "SELL",
                tdst_threshold=self.active_countdown._tdst_threshold,
            )

    def _emit_countdown_event(self, ev, events_out):
        dir_str = "BUY" if ev["direction"] == 1 else "SELL"
        ev_type = ev["type"]
        bar = ev["bar"]
        type_name = "{}_COUNTDOWN_{}".format(dir_str, ev_type)
        if ev_type == "COMPLETE" and ev.get("perfect"):
            type_name = "{}_COUNTDOWN_COMPLETE_PERFECT".format(dir_str)

        record = {
            "datetime": bar.datetime,
            "security": self.security,
            "frequency": self.frequency,
            "category": "COUNTDOWN",
            "type": type_name,
            "direction": ev["direction"],
            "count": ev.get("count"),
            "extra": "reason={}".format(ev.get("reason", "")) if ev_type == "CANCEL" else "",
        }
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
            )
        elif ev_type == "CANCEL":
            self.logger.info(
                "Countdown 取消",
                security=self.security, frequency=self.frequency, datetime_=bar.datetime,
                direction=dir_str, count=ev.get("count"), reason=ev.get("reason"),
            )


# =============================================================================
# [6] 信号输出层
# =============================================================================

class SignalRecorder:
    """
    将信号事件追加写入 CSV 文件，以便后续离线分析。
    所有标的共用一份 signals.csv（按行追加），文件路径位于 PTrade 研究目录下的
    config.log.output_dir 子目录中。

    职责单一：仅做持久化，不感知任何信号语义。
    """

    HEADER = "datetime,security,frequency,category,type,direction,count,extra\n"

    def __init__(self, output_dir_rel, logger, enabled=True):
        self.enabled = bool(enabled)
        self.logger = logger
        self.output_dir_rel = output_dir_rel
        self.csv_full_path = _join_research_path(output_dir_rel.rstrip("/") + "/signals.csv")
        self._header_written = False

        if self.enabled:
            try:
                create_dir(output_dir_rel)  # noqa: F821 - PTrade 注入
            except Exception as e:
                self.logger.warning("create_dir 失败，将尝试直接写入", path=output_dir_rel, err=e)

    def write_events(self, events):
        if not self.enabled or not events:
            return
        try:
            need_header = self._need_header()
            mode = "a" if not need_header else "w"
            f = open(self.csv_full_path, mode, encoding="utf-8")
            try:
                if need_header:
                    f.write(self.HEADER)
                for ev in events:
                    line = "{},{},{},{},{},{},{},{}\n".format(
                        ev.get("datetime", ""),
                        ev.get("security", ""),
                        ev.get("frequency", ""),
                        ev.get("category", ""),
                        ev.get("type", ""),
                        ev.get("direction", ""),
                        ev.get("count", "") if ev.get("count") is not None else "",
                        ev.get("extra", ""),
                    )
                    f.write(line)
            finally:
                f.close()
            self.logger.debug("信号已写入 CSV", count=len(events), path=self.csv_full_path)
        except Exception as e:
            self.logger.error("写入信号 CSV 失败", path=self.csv_full_path, err=e)

    def _need_header(self):
        if self._header_written:
            return False
        try:
            f = open(self.csv_full_path, "r", encoding="utf-8")
            try:
                first = f.readline()
            finally:
                f.close()
            if first.startswith("datetime,security,frequency,category,type,direction,count,extra"):
                self._header_written = True
                return False
            return True
        except Exception:
            return True


# =============================================================================
# [7] PTrade 策略钩子
# =============================================================================

def initialize(context):
    """
    PTrade 在策略启动时调用一次。注意此函数中不可调用 get_history / get_price /
    get_stock_status 等行情接口，因此所有数据获取均推迟到 before_trading_start 与
    handle_data 中执行。本函数只做配置加载与设置类调用。
    """
    cfg = load_config("TDSignal/config.json")
    g.config = cfg

    log_cfg = cfg.get("log", {})
    g.logger = StrategyLogger(
        level=log_cfg.get("level", "INFO"),
        verbose_state_transition=log_cfg.get("verbose_state_transition", False),
        verbose_countdown_step=log_cfg.get("verbose_countdown_step", True),
    )

    universe_cfg = cfg["universe"]
    g.securities = list(universe_cfg.get("securities", []))
    set_benchmark(universe_cfg.get("benchmark", "000300.SS"))
    set_universe(g.securities)

    bt_cfg = cfg["backtest"]
    set_slippage(slippage=float(bt_cfg.get("slippage", 0.001)))
    set_limit_mode(bt_cfg.get("limit_mode", "UNLIMITED"))

    data_cfg = cfg["data"]
    g.frequency = data_cfg.get("frequency", "1d")
    g.fq = data_cfg.get("fq", "pre")
    g.lookback_count = int(data_cfg.get("lookback_count", 300))
    g.min_required_bars = int(data_cfg.get("min_required_bars", 20))

    # 数据层与信号输出层（无状态对象，可在 init 中构造）
    g.data_fetcher = MarketDataFetcher(
        frequency=g.frequency, fq=g.fq, lookback_count=g.lookback_count, logger=g.logger,
    )
    g.recorder = SignalRecorder(
        output_dir_rel=log_cfg.get("output_dir", "TDSignal"),
        logger=g.logger,
        enabled=log_cfg.get("csv_output", True),
    )

    # 当日有效（非停牌）标的列表，由 before_trading_start 每日刷新
    g.active_securities_today = list(g.securities)

    g.logger.info(
        "策略初始化完成",
        securities=len(g.securities), frequency=g.frequency, fq=g.fq,
        lookback=g.lookback_count, setup_perfect_only=cfg["setup"].get("require_perfect_for_signal"),
        countdown_perfect=cfg["countdown"].get("require_perfect"),
        tdst_rule=cfg["countdown"].get("tdst_cancel_rule"),
    )


def before_trading_start(context, data):
    """
    每日盘前刷新当日有效标的（剔除当日停牌、退市等异常标的）。
    根据 PTrade 文档，filter_stock_by_status 仅可在 before_trading_start 内调用。
    """
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
        1. 通过数据层拉取最近 N 根有效 K 线（前复权，跳过 volume<=0 的停牌日）
        2. 构造一个全新的 TDSignalProcessor，从头跑完整流水线
        3. 筛出"最后一根 K 线（即今日）"产生的信号事件
        4. 写日志 + 持久化到 CSV
    """
    today_events_all = []

    for security in g.active_securities_today:
        # 二次确认当日是否停牌（保守策略）
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

        # 数据层：拉取近 N 根有效 K 线
        bars = g.data_fetcher.fetch_recent_bars(security)
        if len(bars) < g.min_required_bars:
            g.logger.warning(
                "有效 K 线数量不足，跳过本周期",
                security=security, frequency=g.frequency, valid=len(bars),
                min_required=g.min_required_bars,
            )
            continue

        last_dt = bars[-1].datetime

        # 信号层：每日全量重算
        processor = TDSignalProcessor(
            security=security, frequency=g.frequency, config=g.config, logger=g.logger,
        )
        all_events = processor.run(bars)

        # 仅关注今日产生的信号事件（最后一根 K 线 datetime）
        today_events = [ev for ev in all_events if ev["datetime"] == last_dt]

        g.logger.debug(
            "本周期事件统计",
            security=security, frequency=g.frequency, datetime_=last_dt,
            historical_events=len(all_events) - len(today_events),
            today_events=len(today_events),
        )

        if today_events:
            for ev in today_events:
                g.logger.info(
                    "TODAY SIGNAL",
                    security=ev["security"], frequency=ev["frequency"], datetime_=ev["datetime"],
                    type=ev["type"], direction=ev["direction"], count=ev.get("count"),
                    extra=ev.get("extra", ""),
                )
            today_events_all.extend(today_events)

    if today_events_all:
        g.recorder.write_events(today_events_all)


def after_trading_end(context, data):
    """盘后留作汇总日志。当前不做交易，仅打印当日产生的信号数量。"""
    g.logger.debug("盘后处理完毕")
