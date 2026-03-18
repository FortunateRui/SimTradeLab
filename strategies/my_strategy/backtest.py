# -*- coding: utf-8 -*-
"""
策略名：TD_9_13_Sequential
作者：熊瑞
功能：基于TD_9_13_Sequential的策略实现
"""

import pickle
import pandas as pd


def _get_setup_log_path():
    """
    获取保存 Setup 列表的 CSV 文件路径（位于研究路径下）。
    注意：在 PTrade 平台上应将 RESEARCH_PATH 替换为 get_research_path() 返回的路径。
    这里不依赖 os 模块，直接基于字符串拼接。
    """
    try:
        base = get_research_path()
    except Exception:
        # 本地调试时若没有该 API，则退化为当前目录
        base = "."
    if not base.endswith("/") and not base.endswith("\\"):
        base = base + "/"
    return base + "DeMarker/td_setup_list.csv"



def initialize(context):
    
    # 基本配置
    set_benchmark('000300.SS')  # 设置基准指数为沪深300
    g.securities = [
        '600519.SS',  # 贵州茅台
        '000858.SZ',  # 五粮液
        '601318.SS',  # 中国平安
        '600036.SS',  # 招商银行
        '000651.SZ',  # 格力电器
    ]
    set_universe(g.securities)    # 设置股票池，handle_data中的data参数会自动订阅股票池中的股票
    g.frequency = "1d"  # 数据频率,如更改应随PTrade软件中的策略周期一同更改
    g.cache_capacity = 300  # 缓存容量

    # 回测参数设置
    set_slippage(slippage=0.1)  # 设置滑点为+-0.05%
    set_limit_mode('UNLIMITED')  # 设置回测中限仓模式为无限制

    # 运行时变量
    g.bars_manager = BarsManager(capacity=g.cache_capacity)  # 环形数组容量；(security, frequency) 在首次 add_new_data 时自动创建
    g.setup_machines_manager = {}  # 存储setup机器的dict，key为(security, frequency)，value为SetupMachine对象



    # 初始化
    context.is_warmup = False
    
    # TODO: 是否有需要持久化的数据？如果有，通过pickle进行持久化，init、handle、after_trading_end中都要有相关的持久化恢复和保存

    
def before_trading_start(context,data):
    if context.is_warmup == False:
        fill_bars_manager(context)

        # for security in g.securities:
        #     _key = (security, g.frequency) 
        #     g.setup_machines_manager[_key] = SetupMachine(security, g.frequency)
        #     for offset in range(0, g.bars_manager.get_size(security, g.frequency)-1):
        #         g.setup_machines_manager[_key].update_state(g.bars_manager.get_data_by_offset(security, g.frequency, g.bars_manager.get_oldest_datetime(security, g.frequency), offset).datetime)
        for security in g.securities:
            _key = (security, g.frequency) 
            g.setup_machines_manager[_key] = SetupMachine(security, g.frequency)
            size = g.bars_manager.get_size(security, g.frequency)
            if size - 5<= 0:
                continue
            start_datetime = g.bars_manager.get_oldest_datetime(security, g.frequency)
            for offset in range(4, size-5):
                bar = g.bars_manager.get_data_by_offset(security, g.frequency, start_datetime, offset)
                g.setup_machines_manager[_key].update_state(bar.datetime)
        context.is_warmup = True

def handle_data(context, data):
    pass

def after_trading_end(context, data):
    pass

class Bar:
    """
    说明：存储单根K线数据的对象
    """

    def __init__(self, security:str, frequency:str, datetime:str, open:float, high:float, low:float, close:float, volume:int, amount:float=None):
        """
        初始化
        :param security: 股票代码
        :param frequency: 频率周期
        :param datetime: 日期时间
        :param open: 开盘价
        :param high: 最高价
        :param low: 最低价
        :param close: 收盘价
        :param volume: 成交量
        :param amount: 成交额（可选）
        """
        self.security = security
        self.frequency = frequency
        self.datetime = datetime
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume
        self.amount = amount

    def __repr__(self):
        return "<Bar {} O:{:.2f} H:{:.2f} L:{:.2f} C:{:.2f} V:{}>".format(
            self.datetime, self.open, self.high, self.low, self.close, self.volume
        )



class BarsManager:
    """
    说明：存储近 n 根股票 K 线数据的对象，使用环形数组 + 哈希表，避免 pop(0) 导致下标错位。

    self.member:
        self.capacity: int       # 缓存容量，即每个 (security, frequency) 最多存储 capacity 条最新数据
        self.bars: dict          # (security, frequency) -> 环形数组 list[Bar|None]，固定长度 capacity
        self.time_index: dict    # (security, frequency) -> { datetime -> 环形数组物理下标 }
        self._head: dict         # (security, frequency) -> 下一次写入的环形数组下标
        self._size: dict         # (security, frequency) -> 当前已存 Bar 数量
    """
    def __init__(self, capacity: int = 300):
        self.capacity = capacity
        self.bars = {}
        self.time_index = {}
        self._head = {}
        self._size = {}

    def _key(self, security: str, frequency: str):
        """
        根据股票代码和频率周期生成key
        :param security: 股票代码
        :param frequency: 频率周期
        :return: key，即(security, frequency)
        """
        return (security, frequency)

    def _ensure_slot(self, key):
        if key not in self.bars:
            self.bars[key] = [None] * self.capacity
            self.time_index[key] = {}
            self._head[key] = 0
            self._size[key] = 0
    
    def get_size(self, security: str, frequency: str):
        """
        获取当前已存 Bar 数量
        :param security: 股票代码
        :param frequency: 频率周期
        :return: 当前已存 Bar 数量；若该 (security, frequency) 尚无数据，则返回 0
        """
        key = self._key(security, frequency)
        return self._size.get(key, 0)
    def get_latest_datetime(self, security: str, frequency: str):
        """
        获取最新日期时间
        :param security: 股票代码
        :param frequency: 频率周期
        :return: 最新日期时间（str），若无数据返回 None
        """
        key = self._key(security, frequency)
        if key not in self._size or self._size[key] == 0:
            return None

        head = self._head[key]
        size = self._size[key]
        buf = self.bars[key]
        cap = self.capacity

        # 未满时，最新一根在物理下标 size-1；已满时，最新一根在 head 前一位
        if size < cap:
            latest_phys = size - 1
        else:
            latest_phys = (head - 1 + cap) % cap

        latest_bar = buf[latest_phys]
        if latest_bar is None:
            return None
        return latest_bar.datetime
       
        
    def get_oldest_datetime(self, security: str, frequency: str):
        """
        获取最旧日期时间
        :param security: 股票代码
        :param frequency: 频率周期
        :return: 最旧日期时间（str），若无数据返回 None
        """
        key = self._key(security, frequency)
        if key not in self._size or self._size[key] == 0:
            return None

        head = self._head[key]
        size = self._size[key]
        buf = self.bars[key]
        cap = self.capacity

        # 未满时，最旧一根在物理下标 0；已满时，最旧一根在 head 位置
        if size < cap:
            oldest_phys = 0
        else:
            oldest_phys = head

        oldest_bar = buf[oldest_phys]
        if oldest_bar is None:
            return None
        return oldest_bar.datetime

    def get_data_by_datetime(self, security: str, frequency: str, datetime: str):
        """
        根据日期时间获取数据
        :param security: 股票代码
        :param frequency: 频率周期
        :param datetime: 日期时间
        :return: Bar对象，如果该日期时间的数据不存在，则返回 None
        """
        key = self._key(security, frequency)
        if key not in self.time_index or datetime not in self.time_index[key]:
            log.error("datetime not found in time_index | security={}, frequency={}, datetime={}".format(security, frequency, datetime))
            return None
        idx = self.time_index[key][datetime]
        return self.bars[key][idx]

    def get_data_by_offset(self, security: str, frequency: str, datetime: str, offset: int):
        """
        根据日期时间和偏移量获取数据
        :param security: 股票代码
        :param frequency: 频率周期
        :param datetime: 日期时间
        :param offset: 偏移量，-4 代表往前第 4 根 k 线（时间更早）
        :return: 数据，若不存在或越界则返回 None
        """
        key = self._key(security, frequency)
        if key not in self.time_index or datetime not in self.time_index[key]:
            log.error("datetime not found in time_index | security={}, frequency={}, datetime={}".format(security, frequency, datetime))
            return None

        head, size, key_bars = self._head[key], self._size[key], self.bars[key]
        cap = self.capacity
        phys = self.time_index[key][datetime]

        # time_index 存的是物理下标，需转为逻辑下标（0=最旧，size-1=最新）
        if size < cap:
            logical = phys
        else:
            # cap -logical = head - phys
            logical = (phys - head + cap) % cap

        want_logical = logical + offset
        if want_logical < 0 or want_logical >= size:
            log.error("offset out of range: want_logical={}, size={}".format(want_logical, size))
            return None

        # 逻辑下标转回物理下标
        if size < cap:
            phys_want = want_logical
        else:
            phys_want = (head + want_logical) % cap
        return key_bars[phys_want]



    def add_new_data(self, bar: Bar):
        """
        添加新的 K 线数据。使用环形数组覆盖最旧的一条，物理下标不变，time_index 只增删一条映射。
        """
        # 此处为防御性编程，应在上层就确保不会添加交易量为0的K线数据
        if bar.volume <= 0:
            log.error("volume is less than 0: volume={}".format(bar.volume))
            return
        key = self._key(bar.security, bar.frequency)
        self._ensure_slot(key)
        head = self._head[key]
        size = self._size[key]
        key_bars = self.bars[key]
        key_time_index = self.time_index[key]
        if bar.datetime in key_time_index:
            log.error("datetime already exists when adding new data | security={}, frequency={}, datetime={}".format(bar.security, bar.frequency, bar.datetime))
        # 若已满，覆盖最旧位置并删除其 datetime 映射
        if size >= self.capacity:
            old_bar = key_bars[head]
            if old_bar is not None:
                key_time_index.pop(old_bar.datetime, None)
        else:
            size += 1
            self._size[key] = size
        key_bars[head] = bar
        key_time_index[bar.datetime] = head
        self._head[key] = (head + 1) % self.capacity

def fill_bars_manager(context):
    """
    使用 get_price 获取当前日（或当前周期）之前的 K 线，按 (security, frequency) 填入 g.bars_manager 缓存。
    应在 before_trading_start 或 handle_data 中调用，且已设置 g.securities、g.frequency、g.bars_manager、g.cache_capacity。
    """
    assert hasattr(g, "bars_manager") and hasattr(g, "securities") and hasattr(g, "frequency") and hasattr(g, "cache_capacity"), "bars_manager, securities, frequency, or cache_capacity not set"
    bm = g.bars_manager
    frequency = g.frequency
    securities = g.securities

    # if frequency in ['1d']:
    #     end_date = (context.blotter.current_dt - pd.Timedelta(days=1)).strftime("%Y%m%d")     # 日线及更高周期，日期格式为YYYYMMDD
    # else:
    #     end_date = (context.blotter.current_dt).strftime("%Y%m%d%H%M") # 分钟线格式为YYYYMMDDHHMM, -1天

    fields = ["open", "high", "low", "close", "volume", "money"]
    _count = int(g.cache_capacity * 1.5 + 0.9999)    # 向上取整，确保至少有1.5倍缓存容量的数据，避免因停牌等导致数据不足
    for security in securities:

        df = get_price(security=security, frequency=frequency, fields=fields, count=_count,fq="pre")
        if df is None or df.empty:
            log.warning("get_price 返回空结果 | security={}, frequency={}, fields={}, count={}".format(security, frequency, fields, _count))
            continue
        # df 是 pandas.DataFrame, 行索引为 datetime.datetime, 列索引为行情字段名(str)
        for dt, row in df.iterrows():
            if row["volume"] <= 0:
                continue    # 跳过成交量为0的bar
            bar = Bar(security, frequency, dt.strftime("%Y%m%d%H%M%S"), row["open"], row["high"], row["low"], row["close"], row["volume"], row.get("money", None))
            bm.add_new_data(bar)


class SetupMachine: 

    # 构造函数
    def __init__(self, security: str, frequency: str):
        assert security is not None and frequency is not None, "security and frequency must be set in SetupMachine"
        assert hasattr(g, "bars_manager"), "bars_manager must be set before initializing SetupMachine"
        self.security = security    # 股票代码
        self.frequency = frequency  # 数据频率
        self.last_datetime = None   # 记录当前状态对应的日期时间，初始化为None
        self.sd = 0                 # 表示状态的变量1，实际含义是当前setup的方向，1代表买入setup，-1代表卖出setup，0代表没有方向，初始化为0
        self.sc = 0                 # 表示状态的变量2，实际含义是当前setup的计数，0代表没有计数，初始化为0
        self.setup_list = []        # 存储当前的setup列表，每个元素为datetime字符串，初始化为空列表
        self.bm = g.bars_manager    # 存储股票K线数据的环形数组

    def _append_current_setup_to_csv(self, signal_type: str):
        """
        将当前 setup_list 及对应的 Bar 详情追加写入 CSV 文件。
        - 首次写入文件时，先写入表头行。
        - 每个 setup_list 记录结束后追加一行空行。
        CSV 列：security,frequency,signal_type,datetime,open,high,low,close,volume,amount
        """
        if not self.setup_list:
            return
        path = _get_setup_log_path()
        # 判断是否需要写入表头：
        # - 文件不存在或为空 -> 写表头
        # - 文件存在但首行不是表头（历史文件）-> 覆盖重建并写表头
        write_header = False
        overwrite_existing = False
        try:
            f_check = open(path, "r", encoding="utf-8")
            first_line = f_check.readline()
            f_check.close()
            if not first_line:
                write_header = True
            elif not first_line.startswith("security,frequency,signal_type,datetime,open,high,low,close,volume,amount"):
                write_header = True
                overwrite_existing = True
        except Exception:
            write_header = True

        try:
            mode = "w" if overwrite_existing else "a"
            f = open(path, mode, encoding="utf-8")
        except Exception:
            return
        try:
            if write_header:
                f.write("security,frequency,signal_type,datetime,open,high,low,close,volume,amount\n")
            for dt in self.setup_list:
                bar = self.bm.get_data_by_datetime(self.security, self.frequency, dt)
                if bar is None:
                    continue
                amount_str = ""
                if bar.amount is not None:
                    amount_str = "{:.4f}".format(bar.amount)
                line = "{},{},{},{},{:.4f},{:.4f},{:.4f},{:.4f},{},{}\n".format(
                    self.security,
                    self.frequency,
                    signal_type,
                    bar.datetime,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    amount_str,
                )
                f.write(line)
            # 一个 setup_list 结束后空一行
            f.write("\n")
        finally:
            f.close()

    def judge_input(self, datetime: str):
        """
        判断输入信号
        :param datetime: 日期时间（应按时间递增调用）
        :return: 输入信号，BS代表买入结构方向，SS代表卖出结构方向，EQ代表无变化
        """
        if self.last_datetime is not None and datetime <= self.last_datetime:
            raise ValueError("datetime is less than last_datetime | datetime={}, last_datetime={}".format(datetime, self.last_datetime))
        bar = self.bm.get_data_by_datetime(self.security, self.frequency, datetime)
        bar_pre_4 = self.bm.get_data_by_offset(self.security, self.frequency, datetime, -4)
        if bar is None or bar_pre_4 is None:
            raise ValueError("bar or bar_pre_4 is None | security={}, frequency={}, datetime={}".format(self.security, self.frequency, datetime))
        if bar.close < bar_pre_4.close:
            return "BS"
        elif bar.close > bar_pre_4.close:
            return "SS"
        else:
            return "EQ"

    def update_state(self, datetime: str): 
        """
        更新状态：根据输入信号和当前状态,进行状态转移,更新last_datetime,执行当前状态对应的逻辑
        状态转移图详见文档
        :param datetime: 日期时间
        """
        input = self.judge_input(datetime)
        match (self.sd, self.sc):
            case (0,0):
                # q0状态
                if input == "BS":
                    # 转移至q5状态
                    self.sd = 1
                    self.sc = 0
                elif input == "SS":
                    # 转移至q1状态
                    self.sd = -1
                    self.sc = 0
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (-1,0):
                # q1状态
                if input == "BS":
                    # 转移至q2状态
                    self.sd = 1
                    self.sc = 1
                elif input == "SS":
                    # 转移至q1状态
                    self.sd = -1
                    self.sc = 0
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (1,1):
                # q2状态
                if input == "BS":
                    # 转移至q3状态
                    self.sd = 1
                    self.sc += 1
                elif input == "SS":
                    # 转移至q6状态
                    self.sd = -1
                    self.sc = 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (1,c) if c >= 2 and c <= 8:
                # q3状态
                if input == "BS":
                    # 转移至q3/q4状态
                    self.sd = 1
                    self.sc += 1
                elif input == "SS":
                    # 转移至q6状态
                    self.sd = -1
                    self.sc = 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (1,9):
                # q4状态
                if input == "BS":
                    # 转移至q5状态
                    self.sd = 1
                    self.sc = 0
                elif input == "SS":
                    # 转移至q6状态
                    self.sd = -1
                    self.sc = 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (1,0):
                # q5状态
                if input == "BS":
                    # 转移至q5状态
                    self.sd = 1
                    self.sc = 0
                elif input == "SS":
                    # 转移至q6状态
                    self.sd = -1
                    self.sc = 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (-1,1):
                # q6状态
                if input == "BS":
                    # 转移至q2状态
                    self.sd = 1
                    self.sc = 1
                elif input == "SS":
                    # 转移至q7状态
                    self.sd = -1
                    self.sc += 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (-1,c) if c >= 2 and c <= 8:
                # q7状态
                if input == "BS":
                    # 转移至q2状态
                    self.sd = 1
                    self.sc = 1
                elif input == "SS":
                    # 转移至q7状态
                    self.sd = -1
                    self.sc += 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case (-1,9):
                # q8状态
                if input == "BS":
                    # 转移至q5状态
                    self.sd = 1
                    self.sc = 0
                elif input == "SS":
                    # 转移至q6状态
                    self.sd = -1
                    self.sc = 1
                elif input == "EQ":
                    # 转移至q0状态
                    self.sd = 0
                    self.sc = 0
                else:
                    raise ValueError("invalid input | input={}".format(input))
                self.last_datetime = datetime
            case _:
                raise ValueError("invalid state | sd={}, sc={}".format(self.sd, self.sc))
        self.execute_current_state()

    def execute_current_state(self):
        """
        执行当前状态对应的逻辑
        """
        match (self.sd, self.sc):
            case (0,0):
                # q0状态
                self.setup_list.clear()
            case (-1,0):
                # q1状态
                self.setup_list.clear()
            case (1,1):
                # q2状态
                self.setup_list.clear()
                self.setup_list.append(self.last_datetime)
            case (1,c) if c >= 2 and c <= 8:
                # q3状态
                self.setup_list.append(self.last_datetime)
            case (1,9):
                # q4状态
                self.setup_list.append(self.last_datetime)
                if self.setup_list.__len__() != 9:
                    raise ValueError("setup_list length is not 9 | setup_list={},check setup machine logic".format(self.setup_list))
                # TODO: 判断是否是完美setup，并输出Buy Setup或完美Buy Setup信号
                # 将 Buy Setup 及其对应的 Bar 详情写入 CSV
                self._append_current_setup_to_csv("BUYSETUP")
            case (1,0):
                # q5状态
                self.setup_list.clear()
            case (-1,1):
                # q6状态
                self.setup_list.clear()
                self.setup_list.append(self.last_datetime)
            case (-1,c) if c >= 2 and c <= 8:
                # q7状态
                self.setup_list.append(self.last_datetime)
            case (-1,9):
                # q8状态
                self.setup_list.append(self.last_datetime)
                if self.setup_list.__len__() != 9:
                    raise ValueError("setup_list length is not 9 | setup_list={},check setup machine logic".format(self.setup_list))
                # TODO: 判断是否是完美setup，并输出Sell Setup或完美Sell Setup信号
                # 将 Sell Setup 及其对应的 Bar 详情写入 CSV
                self._append_current_setup_to_csv("SELLSETUP")
            case _:
                raise ValueError("invalid state | sd={}, sc={}".format(self.sd, self.sc))
    