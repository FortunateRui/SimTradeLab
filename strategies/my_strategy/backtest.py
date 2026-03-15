# -*- coding: utf-8 -*-
"""
策略名：TD_9_13_Sequential
作者：熊瑞
功能：基于TD_9_13_Sequential的策略实现
"""

import pickle
RESEARCH_PATH = get_research_path()

def initialize(context):
    
    # 基本配置
    set_benchmark('000300.SS')  # 设置基准指数为沪深300
    
    g.stock_frequency_pairs = [
        ('600519.SS', "1d"),  # 贵州茅台
        ('000858.SZ', "1d"),  # 五粮液
        ('601318.SS', "1d"),  # 中国平安
        ('600036.SS', "1d"),  # 招商银行
        ('000651.SZ', "1d"),  # 格力电器
    ]

    # 回测参数设置
    set_slippage(slippage=0.1)  # 设置滑点为+-0.05%
    set_limit_mode('UNLIMITED')  # 设置回测中限仓模式为无限制

    # 运行变量
    g.bars_manager = BarsManager(capacity=300)  # 环形数组容量；(security, frequency) 在首次 add_new_data 时自动创建




    # 初始化

    # TODO: 是否有需要持久化的数据？如果有，通过pickle进行持久化，init、handle、after_trading_end中都要有相关的持久化恢复和保存

    


def handle_data(context, data):
    pass
    # # 获取所有股票近6日收盘价（含今日用于计算均线）
    # hist = get_history(6, '1d', 'close', context.stocks, include=True)

    # # 过滤掉数据不足的股票
    # valid = [s for s in context.stocks if s in hist.columns and hist[s].count() >= 5]

    # ma5 = {s: hist[s].iloc[-5:].mean() for s in valid}
    # price = {s: hist[s].iloc[-1] for s in valid}

    # positions = context.portfolio.positions

    # # 卖出：持仓中收盘价跌破5日均线的
    # for stock, pos in list(positions.items()):
    #     if pos.amount > 0 and stock in ma5 and price[stock] < ma5[stock]:
    #         order_target(stock, 0)
    #         log.info("卖出 {} price={:.2f} ma5={:.2f}".format(stock, price[stock], ma5[stock]))

    # # 买入：价格高于5日均线且未持仓，按持仓上限等比买入
    # held = [s for s, p in context.portfolio.positions.items() if p.amount > 0]
    # slots = context.max_position - len(held)
    # if slots <= 0:
    #     return

    # candidates = [
    #     s for s in valid
    #     if s not in held and price[s] > ma5[s]
    # ]
    # # 按超越均线幅度降序，优先买入强势股
    # candidates.sort(key=lambda s: price[s] / ma5[s], reverse=True)

    # per_slot = context.portfolio.cash / slots
    # for stock in candidates[:slots]:
    #     order_value(stock, per_slot)
    #     log.info("买入 {} price={:.2f} ma5={:.2f}".format(stock, price[stock], ma5[stock]))


def after_trading_end(context, data):
    pass
    # positions = context.portfolio.positions
    # held = [(s, p) for s, p in positions.items() if p.amount > 0]
    # log.info("日终 | 总资产: {:.2f} | 持仓: {} 只 | 现金: {:.2f}".format(
    #     context.portfolio.portfolio_value, len(held), context.portfolio.cash))


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
    member:
        capacity: int       # 缓存容量，即每个 (security, frequency) 最多存储 capacity 条最新数据
        bars: dict          # (security, frequency) -> 环形数组 list[Bar|None]，固定长度 capacity
        time_index: dict    # (security, frequency) -> { datetime -> 环形数组物理下标 }
        _head: dict         # (security, frequency) -> 下一次写入的环形数组下标
        _size: dict         # (security, frequency) -> 当前已存 Bar 数量
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
        key = self._key(bar.security, bar.frequency)
        self._ensure_slot(key)
        head = self._head[key]
        size = self._size[key]
        key_bars = self.bars[key]
        key_time_index = self.time_index[key]
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


class SetupMachine: 

    # 存储该状态机分析的股票信息，应该在初始化完成后就不再变化
    security: str = None    # 股票代码
    frequency: str = "1d"    # 数据频率

    # 运行时维护的变量
    current_date: str = None    # 记录当前状态对应的日期时间
    sd:int = 0                 # 表示状态的变量1，实际含义是当前setup的方向，1代表买入setup，-1代表卖出setup，0代表没有方向
    sc:int = 0                 # 表示状态的变量2，实际含义是当前setup的计数，0代表没有计数
    # 构造函数
    def __init__(self):
        pass

    
