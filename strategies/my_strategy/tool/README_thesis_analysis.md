# TD Thesis Analysis Tool

`thesis_analysis_tool.py` 是 TD 9-13 策略的离线论文分析脚本，不依赖 PTrade 环境。它读取策略运行后生成的元数据文件，输出论文第五章需要的表格和图表。

## 输入文件

目标运行目录需要包含：

| 文件 | 来源 | 说明 |
| --- | --- | --- |
| `td_events.csv` | `backtest.py` | TD Setup / Countdown 完成与取消等事件元数据 |
| `event_outcomes.csv` | `backtest.py` | 事件窗口成熟后写出的方向收益、MFE、MAE |
| `trade.csv` | `backtest.py` | 全部标的交易生命周期汇总 |
| `position_snapshot.csv` | `backtest.py` | 开仓和平仓时的轻量仓位快照，当前工具预留读取口径 |

工具只读取策略输出目录中的元数据文件，不读取 `data/` 目录、不读取 parquet，也不依赖 PTrade。若缺少 `event_outcomes.csv`，说明策略运行时尚未生成事件窗口结果，需要重新运行策略或扩大回测区间让事件窗口成熟。

## 配置方式

打开 `thesis_analysis_tool.py`，在文件开头修改：

```python
TARGET_RUN_FOLDER = "../reacher_path/2021-01-01"
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

## 统计口径

### 事件研究

`event_study_summary.csv` 按事件组统计：

- `Buy Setup`
- `Sell Setup`
- `Buy Countdown Complete`
- `Sell Countdown Complete`
- `Countdown Cancel`

事件窗口结果来自策略输出的 `event_outcomes.csv`，工具不再读取行情源数据。对每个事件窗口统计：

- 方向收益率：`direction * (future_close / event_close - 1)`
- 方向胜率：方向收益率大于 0 的比例
- 均值、中位数、25% 分位数、75% 分位数
- MFE：事件窗口内方向上最大有利波动
- MAE：事件窗口内方向上最大不利波动

### 交易回测

`trade_performance_summary.csv` 只统计完整交易，即：

- `bought=true`
- `sell_date` 非空
- `pnl` 可解析

输出四种信号质量口径：

- `All Buy Countdown`
- `Perfect Setup`
- `Perfect Countdown`
- `Perfect Setup + Countdown`

核心指标包括交易次数、胜率、平均盈亏比、总盈亏、总收益率、最大回撤和夏普比率。

### 分层分析

`stratified_summary.csv` 按以下维度生成摘要：

- `Market State`：根据 `pre20_return` 粗分为 `Uptrend` / `Downtrend` / `Sideways`
- `Liquidity`：根据 `pre20_avg_amount` 粗分高、中、低流动性
- `Volatility`：根据 `pre20_volatility` 粗分高、中、低波动率
- `Signal Quality`：根据 Setup / Countdown 是否完美分组

这些阈值是论文初稿分析口径，可在脚本中的 bucket 函数里调整。

### 单股适用性分析

`single_stock_applicability.csv` 对每只股票单独汇总 Buy Countdown 完成事件和完整交易记录。默认样本过滤：

- Buy Countdown 完成事件数不少于 `SINGLE_STOCK_EVENT_MIN_COUNT`，默认 20。
- 完整交易数不少于 `SINGLE_STOCK_TRADE_MIN_COUNT`，默认 10。

核心指标包括：

- `ret_20d_median`：20 日方向收益中位数。
- `mfe_20d_median`：20 日最大有利波动中位数。
- `mae_20d_median`：20 日最大不利波动中位数。
- `trade_win_rate`：完整交易胜率。
- `avg_profit_loss_ratio`：平均盈利 / 平均亏损。
- `max_drawdown`：按该股票交易盈亏序列计算的最大回撤。
- `execution_rate`：完整交易数 / Buy Countdown 完成事件数。
- `split_consistent`：事件样本和交易样本前后两个子区间是否都保持正向表现。
- `selected`：是否入选潜在适用股票。

入选逻辑与论文第五章口径一致：样本数达标，20 日方向收益、最大不利波动、交易胜率和平均盈亏比同时优于全市场中位数，并且前后子区间表现方向一致。`applicability_note` 会给出未入选原因，便于人工复核。

## 注意事项

- 该工具只读取策略输出元数据，不会读取 `data/` 行情目录，也不会修改原始回测结果。
- 图表使用 `matplotlib` 的 `Agg` 后端，适合在终端或服务器环境生成 PNG。
- 进度条优先使用 `tqdm`；如果当前 Python 环境没有安装 `tqdm`，会自动降级为普通阶段提示。
- 如果本机 `matplotlib` 不可用，优先保证 CSV 表格输出；缺失的图表可在依赖安装后重新运行生成。
