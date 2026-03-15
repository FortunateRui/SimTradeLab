# -*- coding: utf-8 -*-
"""
策略名：TD_9_13_Sequential
作者：熊瑞
功能：基于TD_9_13_Sequential的策略实现
"""
def initialize(context):
    
    set_benchmark('000300.SS')  # 设置基准指数为沪深300

    context.stocks = [
        '600519.SS',  # 贵州茅台
        '000858.SZ',  # 五粮液
        '601318.SS',  # 中国平安
        '600036.SS',  # 招商银行
        '000651.SZ',  # 格力电器
    ]

    context.max_position = 2  # 最多同时持仓2只

    context.bars = {}   #存储交易量不为0的k线数据
    context.time_index = {}   #存储交易量不为0的k线数据的时间索引
    # 回测参数设置
    set_slippage(slippage=0.1)  # 设置滑点为+-0.05%
    set_limit_mode('UNLIMITED')  # 设置回测中限仓模式为无限制


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




"""
类名：DataManager
功能：数据管理类，用于管理股票的K线数据
作者：熊瑞
"""
class DataManager:

    
    def __init__(self, stocks: list[str], frequency: str = "1d"):
        

    def get_data(self, security: str, frequency: str):
        pass

    def get_data_by_date(self, security: str, frequency: str, date: str):
        pass

"""
类名：SetupMachine
功能：setup 子自动机的实现
作者：熊瑞
"""
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

    
