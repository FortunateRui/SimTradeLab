# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2025 Kay
#
# This file is part of SimTradeLab, dual-licensed under AGPL-3.0 and a
# commercial license. See LICENSE-COMMERCIAL.md or contact kayou@duck.com
#
"""
本地回测入口 - 配置与启动

简化的入口文件，仅保留配置参数
"""


import sys

# 确保控制台 UTF-8 编码和实时输出（兼容 Windows）
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
sys.stderr.reconfigure(encoding='utf-8')

from simtradelab.backtest.runner import BacktestRunner
from simtradelab.backtest.config import BacktestConfig


if __name__ == '__main__':

    # ==================== 启动回测 ====================

    # 创建配置
    config = BacktestConfig(
    strategy_name='my_strategy',
    # strategy_name='5mv',
    start_date='2025-01-01',
    end_date='2026-01-01',
    initial_capital=1000000.0,  # 初始资金
    frequency='1d',       # '1d' 日线回测（默认），'1m' 分钟回测
    benchmark_code='000300.SS',  # 基准指数（默认沪深300）
    enable_logging=True,  # 生成 .log 日志文件（默认开启）
    enable_charts=True,   # 生成 .png 可视化图表（默认开启）
    enable_export=True,  # 生成 .csv 每日统计和持仓快照（默认关闭）
    sandbox=True,         # PTrade 兼容模式，限制 f-string 等语法（默认开启）
    )


    # 运行回测
    runner = BacktestRunner()
    report = runner.run(config=config)
