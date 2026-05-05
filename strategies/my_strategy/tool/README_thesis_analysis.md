# TD Thesis Analysis Tool

`thesis_analysis_tool.py` 是 TD 9-13 策略的离线论文分析脚本，不依赖 PTrade 环境。它读取策略运行后生成的元数据文件，输出论文第五章需要的表格和图表。

## 输入文件

目标运行目录需要包含：


| 文件                      | 来源            | 说明                               |
| ----------------------- | ------------- | -------------------------------- |
| `td_events.csv`         | `backtest.py` | TD Setup / Countdown 完成与取消等事件元数据 |
| `event_outcomes.csv`    | `backtest.py` | 事件窗口成熟后写出的方向收益、MFE、MAE           |
| `trade.csv`             | `backtest.py` | 全部标的交易生命周期汇总                     |
| `position_snapshot.csv` | `backtest.py` | 开仓和平仓时的轻量仓位快照，当前工具预留读取口径         |


工具只读取策略输出目录中的元数据文件，不读取 `data/` 目录、不读取 parquet，也不依赖 PTrade。若缺少 `event_outcomes.csv`，说明策略运行时尚未生成事件窗口结果，需要重新运行策略或扩大回测区间让事件窗口成熟。

## 配置方式

打开 `thesis_analysis_tool.py`，在文件开头修改：

```python
TARGET_RUN_FOLDER = "../reacher_path/2016-01-01"
OUTPUT_FOLDER_NAME = "thesis_analysis"
INITIAL_CAPITAL = 1_000_000.0
EVENT_WINDOWS = [5, 20, 60, 120]
SINGLE_STOCK_EVENT_MIN_COUNT = 20
SINGLE_STOCK_TRADE_MIN_COUNT = 10
```

也可以通过命令行覆盖运行目录：

```bash
python strategies/my_strategy/tool/thesis_analysis_tool.py ../reacher_path/2021-01-01
```

在项目虚拟环境中运行：

```bash
poetry run python strategies/my_strategy/tool/thesis_analysis_tool.py ../reacher_path/2021-01-01
```

## 输出文件

脚本会在目标运行目录下创建：

```text
thesis_analysis/
├── event_study_summary.csv
├── trade_performance_summary.csv
├── stratified_summary.csv
├── single_stock_applicability.csv
├── trade_performance_by_quality.png
└── single_stock_applicability.png
```

## 输出指标说明

本工具输出的比例类指标一般使用小数表示，例如 `0.05` 表示 `5%`，`-0.03` 表示 `-3%`。金额类指标使用回测账户币种，通常为人民币。下文的参考范围是论文分析时的经验解读区间，不是硬性交易阈值；最终结论应结合样本数、市场阶段、交易成本和分层稳定性判断。

### `event_study_summary.csv`

该文件用于回答“TD 事件发生后，价格是否在指定观察窗口内沿信号方向运动”。事件窗口结果来自策略输出的 `event_outcomes.csv`，工具不读取行情源数据。当前观察窗口为 `5`、`20`、`60`、`120` 个交易日，其中 `20` 日是主观察窗口。

事件组 `event_group` 的含义：


| 字段值                       | 含义                                     | 主要用途                            |
| ------------------------- | -------------------------------------- | ------------------------------- |
| `Buy Setup`               | 买方向 Setup 完成事件                         | 观察 Setup 是否只是早期提示，或是否已经具备短期反弹特征 |
| `Sell Setup`              | 卖方向 Setup 完成事件                         | 观察上涨衰竭类事件后的下行或回落特征              |
| `Buy Countdown Complete`  | 买方向 Countdown 13 完成事件                  | 交易策略的核心候选入场事件                   |
| `Sell Countdown Complete` | 卖方向 Countdown 13 完成事件                  | 主要用于解释反向信号和止盈辅助，不作为默认卖出入场       |
| `Countdown Cancel`        | Countdown 被 TDST、同向 Setup 或反向 Setup 取消 | 用于分析信号失效和规则边界                   |


字段解释：


| 字段                  | 含义                | 值的含义                   | 参考范围和解读                                                 |
| ------------------- | ----------------- | ---------------------- | ------------------------------------------------------- |
| `event_group`       | 事件类型分组            | 字符串分类                  | 重点比较 `Buy Countdown Complete` 与其他事件组                    |
| `event_count`       | 该组事件数量            | 非负整数                   | 小于 30 时结论偏弱；100 以上更适合做分位数解释；全市场样本越大越稳健                  |
| `ret_{h}d_count`    | 有效窗口样本数           | 非负整数，`h` 为 5、20、60、120 | 可能小于 `event_count`，因为临近回测末尾的事件尚未成熟                      |
| `ret_{h}d_mean`     | `h` 日方向收益均值       | 正数表示平均方向正确，负数表示平均方向错误  | 容易受极端值影响；建议和中位数一起看                                      |
| `ret_{h}d_median`   | `h` 日方向收益中位数      | 正数表示超过一半事件方向收益为正       | 主参考指标；`>0` 说明典型样本方向正确，`>0.02` 可视为有一定经济意义，`<0` 表示信号方向不稳定 |
| `ret_{h}d_q25`      | `h` 日方向收益 25% 分位数 | 四分之一位置的方向收益            | 若明显小于 0，说明左尾风险较重；若接近 0 或为正，说明下行风险较温和                    |
| `ret_{h}d_q75`      | `h` 日方向收益 75% 分位数 | 四分之三位置的方向收益            | 衡量机会空间；越高说明右尾收益越明显                                      |
| `ret_{h}d_win_rate` | `h` 日方向胜率         | 方向收益 `>0` 的比例          | `0.50` 附近表示接近随机；`0.55` 以上较好；`0.60` 以上通常需要重点检查样本偏差和交易成本  |
| `mfe_{h}d_median`   | `h` 日最大有利波动中位数    | 窗口内沿信号方向最有利涨跌幅的中位数     | 对 Buy 事件越高越好；若明显高于 `abs(mae)`，说明信号具有可交易机会空间             |
| `mae_{h}d_median`   | `h` 日最大不利波动中位数    | 窗口内沿信号方向最不利涨跌幅的中位数     | 通常为负数，越接近 0 越好；若小于 `-0.08` 或 `-0.10`，说明持有过程风险较大         |


方向收益率定义为 `direction * (future_close / event_close - 1)`。Buy 方向 `direction=1`，上涨为正；Sell 方向 `direction=-1`，下跌为正。因此正值统一表示“事件方向正确”，负值统一表示“事件方向错误”。

MFE 和 MAE 的解释：

- `MFE` 表示 Maximum Favorable Excursion，即观察窗口内最大有利波动。它衡量事件出现后曾经给过多少顺向机会。
- `MAE` 表示 Maximum Adverse Excursion，即观察窗口内最大不利波动。它衡量事件出现后为了等到机会可能承受的最大反向波动。
- 若 `mfe_20d_median` 明显大于 `abs(mae_20d_median)`，通常说明风险收益结构较好。
- 若 `ret_20d_median` 为正但 `mae_20d_median` 很低，说明最终方向可能正确，但持有路径风险较大。

### `trade_performance_summary.csv`

该文件用于回答“把 Buy Countdown 完成事件转化为实际交易后，是否形成可执行的收益结果”。工具只统计完整交易，即 `bought=true`、`sell_date` 非空且 `pnl` 可解析的记录。

信号质量口径 `scenario` 的含义：


| 字段值                         | 含义                          | 解读重点                    |
| --------------------------- | --------------------------- | ----------------------- |
| `All Buy Countdown`         | 所有完成买入流程的 Buy Countdown 样本  | 策略总体交易表现                |
| `Perfect Setup`             | 只要求关联 Setup 为完美 Setup       | 检验 Setup 质量是否改善结果       |
| `Perfect Countdown`         | 只要求 Countdown 为完美 Countdown | 检验 Countdown 完成质量是否改善结果 |
| `Perfect Setup + Countdown` | 同时要求完美 Setup 和完美 Countdown  | 最高质量信号，样本通常最少           |


字段解释：


| 字段                      | 含义     | 值的含义                           | 参考范围和解读                                              |
| ----------------------- | ------ | ------------------------------ | ---------------------------------------------------- |
| `scenario`              | 信号质量口径 | 字符串分类                          | 用于比较不同信号筛选条件                                         |
| `trade_count`           | 完整交易数量 | 非负整数                           | 小于 30 时统计不稳定；筛选越严格样本越少，要防止只看高质量小样本                   |
| `win_rate`              | 交易胜率   | 盈利交易数 / 完整交易数                  | `0.50` 不是绝对标准，需结合盈亏比；趋势反转策略若盈亏比高，`0.40` 也可能可接受       |
| `avg_profit_loss_ratio` | 平均盈亏比  | 平均盈利金额 / 平均亏损金额                | `>1` 表示单笔盈利大于单笔亏损；`>1.5` 较好；`inf` 表示样本中没有亏损交易，需检查样本数 |
| `total_pnl`             | 总实现盈亏  | 所有完整交易 `pnl` 求和                | 正数为盈利，负数为亏损；应结合初始资金和样本规模解读                           |
| `total_return`          | 总收益率   | `total_pnl / INITIAL_CAPITAL`  | `0.10` 表示相对初始资金盈利 10%；当前不含未平仓浮盈浮亏                    |
| `max_drawdown`          | 最大回撤   | 按交易盈亏序列构造净值后的最大回撤比例            | 越低越好；`<0.10` 较温和，`0.10~0.30` 需结合收益评估，`>0.30` 风险较高    |
| `sharpe`                | 年化夏普比率 | 单笔交易收益序列均值 / 标准差，再按 252 个交易日年化 | `>0` 表示风险调整后收益为正；`>1` 较好；样本少或交易间隔不均时仅作辅助             |


胜率和盈亏比要组合解读。例如胜率 `0.45` 但盈亏比 `2.0`，策略仍可能盈利；胜率 `0.65` 但盈亏比 `0.6`，可能因为亏损单较大而总体不佳。

### `stratified_summary.csv`

该文件用于回答“TD 信号是否只在某些市场状态、流动性或波动率环境下有效”。它按事件元数据中的 `pre20` 上下文字段分组，然后汇总对应交易表现。

分层维度 `dimension` 和分组 `bucket` 的含义：


| `dimension`      | `bucket`                    | 划分规则                              | 解读重点                 |
| ---------------- | --------------------------- | --------------------------------- | -------------------- |
| `Market State`   | `Uptrend`                   | `pre20_return > 0.05`             | 事件前约 20 日上涨超过 5%     |
| `Market State`   | `Downtrend`                 | `pre20_return < -0.05`            | 事件前约 20 日下跌超过 5%     |
| `Market State`   | `Sideways`                  | `-0.05 <= pre20_return <= 0.05`   | 事件前处于震荡区间            |
| `Liquidity`      | `High Liquidity`            | `pre20_avg_amount >= 300,000,000` | 20 日平均成交额较高，可交易性较好   |
| `Liquidity`      | `Mid Liquidity`             | 介于高低阈值之间                          | 普通流动性样本              |
| `Liquidity`      | `Low Liquidity`             | `pre20_avg_amount <= 50,000,000`  | 低流动性，结果易受冲击成本和成交限制影响 |
| `Volatility`     | `High Volatility`           | `pre20_volatility >= 0.035`       | 日收益波动较高，风险和机会都更大     |
| `Volatility`     | `Mid Volatility`            | 介于高低阈值之间                          | 普通波动环境               |
| `Volatility`     | `Low Volatility`            | `pre20_volatility <= 0.015`       | 波动较低，信号空间可能较小        |
| `Signal Quality` | `Perfect Setup + Countdown` | Setup 和 Countdown 都完美             | 高质量信号组               |
| `Signal Quality` | `Perfect Setup`             | 仅 Setup 完美                        | 检验 Setup 条件贡献        |
| `Signal Quality` | `Perfect Countdown`         | 仅 Countdown 完美                    | 检验 Countdown 条件贡献    |
| `Signal Quality` | `Non-perfect`               | 两者都不满足完美条件                        | 普通信号对照组              |
| 任意维度             | `Unknown`                   | 元数据缺失或无法解析                        | 不宜作为主要结论             |


字段解释：


| 字段                   | 含义        | 值的含义              | 参考范围和解读                      |
| -------------------- | --------- | ----------------- | ---------------------------- |
| `dimension`          | 分层维度      | 字符串分类             | 说明当前行按什么维度分组                 |
| `bucket`             | 分层组别      | 字符串分类             | 说明当前行所属环境或信号质量               |
| `event_count`        | 当前分组事件数   | 非负整数              | 小样本分组容易失真，建议至少 30 个事件再做方向性判断 |
| `trade_count`        | 当前分组完整交易数 | 非负整数              | 小于 10 的交易分组只适合描述，不适合下结论      |
| `trade_win_rate`     | 当前分组交易胜率  | 盈利交易数 / 完整交易数     | 用于比较不同环境下信号是否更容易获利           |
| `trade_total_pnl`    | 当前分组总盈亏   | 当前分组完整交易 `pnl` 求和 | 正负方向比绝对大小更重要，因为各组样本数可能不同     |
| `trade_max_drawdown` | 当前分组交易回撤  | 按该分组交易盈亏序列计算      | 用于识别某些环境下是否风险明显更高            |


分层分析的重点不是寻找单个最高收益组，而是判断结论是否稳定。若只有低流动性或极高波动组表现好，而高流动性组无效，需要在论文中谨慎表述。

### `single_stock_applicability.csv`

该文件用于回答“哪些单只股票更适合应用 TD 9-13 Buy Countdown 交易逻辑”。它对每只股票单独汇总 Buy Countdown 完成事件和完整交易记录。默认样本过滤为 Buy Countdown 完成事件数不少于 `SINGLE_STOCK_EVENT_MIN_COUNT`（默认 20），完整交易数不少于 `SINGLE_STOCK_TRADE_MIN_COUNT`（默认 10）。

字段解释：


| 字段                      | 含义                  | 值的含义                          | 参考范围和解读                              |
| ----------------------- | ------------------- | ----------------------------- | ------------------------------------ |
| `security`              | 股票代码                | 例如 `600519.SS`、`000001.SZ`    | 用于定位单股                               |
| `event_count`           | Buy Countdown 完成事件数 | 非负整数                          | 小于 20 默认样本不足；越高越能说明该股 TD 信号稳定出现      |
| `completed_trade_count` | 完整交易数               | 非负整数                          | 小于 10 默认样本不足；太少时胜率和盈亏比不稳定            |
| `ret_20d_median`        | 20 日方向收益中位数         | 正数表示典型 Buy Countdown 后 20 日上涨 | `>0` 是基础要求；明显高于全市场中位数说明相对适用性更好       |
| `mfe_20d_median`        | 20 日最大有利波动中位数       | Buy 信号后 20 日内最大反弹空间           | 越高说明机会空间越大；需和 `mae_20d_median` 一起看   |
| `mae_20d_median`        | 20 日最大不利波动中位数       | Buy 信号后 20 日内最大回撤压力           | 通常为负数，越接近 0 越好；工具当前用“高于全市场中位数”判断是否更优 |
| `trade_win_rate`        | 该股完整交易胜率            | 盈利交易数 / 完整交易数                 | 高于全市场中位数说明该股交易闭环更稳定                  |
| `avg_profit_loss_ratio` | 该股平均盈亏比             | 平均盈利 / 平均亏损                   | `>1` 较好；`inf` 表示样本中没有亏损交易，需要结合交易数复核  |
| `max_drawdown`          | 该股交易最大回撤            | 按该股交易盈亏序列计算                   | 越低越好；若收益高但回撤也高，应谨慎入选                 |
| `execution_rate`        | 交易执行率               | 完整交易数 / Buy Countdown 完成事件数   | 过低可能说明大量信号未买入或未完整结束；`0.30` 以下需检查原因   |
| `split_consistent`      | 前后子区间一致性            | `True` 表示事件收益和交易收益前后两段都为正     | `True` 更适合作为论文中“稳定适用”的证据             |
| `selected`              | 是否入选候选适用股票          | `True` / `False`              | `True` 表示满足样本数、相对中位数和一致性条件           |
| `applicability_note`    | 入选或未入选原因            | `candidate` 或英文原因列表           | 用于人工复核未入选的主要短板                       |


`selected=True` 的判定逻辑：

- 样本数达标：`event_count >= 20` 且 `completed_trade_count >= 10`。
- `ret_20d_median` 高于达标股票的全市场中位数。
- `mae_20d_median` 高于达标股票的全市场中位数，即不利波动相对更温和。
- `trade_win_rate` 高于达标股票的全市场中位数。
- `avg_profit_loss_ratio` 高于达标股票的全市场中位数。
- `split_consistent=True`。

`applicability_note` 常见值：


| 值                                            | 含义                | 处理建议                 |
| -------------------------------------------- | ----------------- | -------------------- |
| `candidate`                                  | 入选候选适用股票          | 可作为论文单股案例或后续参数复核对象   |
| `insufficient sample`                        | 事件数或交易数不足         | 不宜下结论，可扩大样本区间或仅作个案观察 |
| `event return not above market median`       | 20 日事件收益没有超过市场中位数 | 说明信号方向优势不足           |
| `adverse move not better than market median` | 最大不利波动没有优于市场中位数   | 说明持有路径风险偏高           |
| `trade quality not above market median`      | 胜率或盈亏比没有超过市场中位数   | 说明转化为交易后质量不足         |
| `subperiod consistency failed`               | 前后子区间表现不一致        | 说明结果可能依赖特定阶段         |


### 图表文件

`trade_performance_by_quality.png` 展示不同信号质量口径下的交易表现。柱状图为总盈亏，折线为胜率。它适合快速观察完美 Setup、完美 Countdown 是否改善交易结果。若高质量组总盈亏更好但交易数很少，论文中应说明样本约束。

`single_stock_applicability.png` 展示入选股票，若没有入选股票，则展示排序靠前的样本。柱状图为 `ret_20d_median`，折线为 `trade_win_rate`。它适合辅助挑选单股案例，但最终应以 `single_stock_applicability.csv` 的完整字段为准。

## 注意事项

- 该工具只读取策略输出元数据，不会读取 `data/` 行情目录，也不会修改原始回测结果。
- 图表使用 `matplotlib` 的 `Agg` 后端，适合在终端或服务器环境生成 PNG。
- 进度条优先使用 `tqdm`；如果当前 Python 环境没有安装 `tqdm`，会自动降级为普通阶段提示。
- 如果本机 `matplotlib` 不可用，优先保证 CSV 表格输出；缺失的图表可在依赖安装后重新运行生成。

