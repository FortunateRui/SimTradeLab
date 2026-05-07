# 第四章图表说明

本文件用于集中存放论文 `paper/td_sequential_thesis.md` 第四章使用的全部图示。论文正文以 `![图名](figures/xxx.png)` 形式引用，对应 PNG 文件需统一放置于 `paper/figures/` 子目录。当前论文实际使用 5 张图，与本文件中的 5 个图示小节一一对应。

可选的渲染方式：

1. 使用本文件给出的 Mermaid 代码块，配合 [mermaid.live](https://mermaid.live) 在线导出 PNG 后，按下方指定的文件名覆盖到 `paper/figures/`。
2. 使用 drawio、Visio 或 PlantUML 等工具按 Mermaid 代码所示拓扑手工绘制后，按下方指定的文件名导出 PNG。

为减少视觉信息密度，本文件给出的代码已避免使用颜色、特殊形状与跨节点连线，便于在黑白印刷条件下保持清晰。

---

## 图4-1 系统分层架构图（architecture.png）

展示 TD 9-13 量化交易系统的八层分层结构以及"通用交易层 / TD 适配层"的解耦关系。

```mermaid
flowchart TB
    subgraph hooks [PTrade 钩子层]
        H1[initialize]
        H2[before_trading_start]
        H3[handle_data]
        H4[tick_data / on_trade_response]
        H5[after_trading_end]
    end
    subgraph cfg [配置与日志层]
        C1[CONFIG_REL_PATH / load_config]
        C2[prepare_output_dir]
        C3[StrategyLogger]
    end
    subgraph data [数据层]
        D1[Bar]
        D2[MarketDataFetcher]
    end
    subgraph signal [信号层]
        S1[SetupMachine]
        S2[CountdownMachine]
        S3[TDSignalProcessor]
    end
    subgraph adapter [策略适配层]
        A1[TDStrategyAdapter]
    end
    subgraph executor [通用交易执行层]
        E1[TradeExecutor]
        E2[BuyIntent / SellIntent / FillInfo]
        E3[TradeExecutorCallbacks]
    end
    subgraph output [输出与元数据层]
        O1[MetadataRecorder]
        O2[TradeRecordRecorder]
    end

    hooks --> cfg
    cfg --> data
    data --> signal
    signal --> adapter
    adapter -- "BuyIntent / SellIntent" --> executor
    executor -- "FillInfo / Callbacks" --> adapter
    adapter --> output
    executor --> output
```

绘图要点：分层在视觉上由上至下，箭头表示运行期主调用方向；交易层与适配层之间双向箭头表示通过回调接口完成职责对接。

---

## 图4-2 单周期 handle_data 主流程图（handle-data.png）

将 `handle_data` 七步流程显式绘制，便于复核执行顺序。

```mermaid
flowchart TB
    Begin[周期开始]
    R0["(0) reconcile_pending_orders<br/>结算上周期未结成交"]
    Loop{遍历当日有效标的}
    HaltAgain["(1) 盘中停牌复核"]
    Fetch["(2) 行情获取与清洗"]
    Window["(3) 更新事件窗口<br/>成熟即写 event_outcomes"]
    Div["(4) 分红 / 送股同步"]
    Exit["(5) 风控触发<br/>check_backtest_exits / check_tick_exits"]
    Sig["(6) 信号识别<br/>TDSignalProcessor.run"]
    Adapt["(7) 信号 -> 交易意图<br/>TDStrategyAdapter.execute_for_events"]
    R1["周期末 reconcile_pending_orders"]
    End[周期结束]

    Begin --> R0 --> Loop
    Loop --> HaltAgain --> Fetch --> Window --> Div --> Exit --> Sig --> Adapt --> Loop
    Loop -->|遍历完成| R1 --> End
```

绘图要点：(0) 与周期末两次对账的位置必须保留，体现"成交先落账再做后续判断"的不变量。

---

## 图4-3 信号下单成交时序图（signal-order-sequence.png）

体现"昨日信号、今日入场、真实成交回报为准"的时序约定。

```mermaid
sequenceDiagram
    autonumber
    participant Bar as 数据层
    participant Sig as 信号层
    participant Adp as 策略适配层
    participant Exe as 交易执行层
    participant PT as PTrade / SimTradeLab

    Note over Bar: T-1 收盘后<br/>get_history(include=False)
    Bar->>Sig: 输入完整 Bar 序列
    Sig->>Sig: 全量重算 TD 状态机
    Sig-->>Adp: 取 last_bar_dt = T-1 的事件
    Adp->>Exe: 提交 BuyIntent / SellIntent
    Exe->>PT: order_value / order (T 日盘中)
    PT-->>Exe: get_order / get_trades 真实回报
    Exe-->>Adp: on_buy_filled / on_sell_filled
    Adp->>Adp: 写入 trade.csv 终态行
```

绘图要点：信号产生与下单执行分别处于 T-1 与 T；trade.csv 的写入位置必须在收到真实成交回报之后，而不是在下单瞬间。

---

## 图4-4 TD 信号生命周期状态图（signal-lifecycle.png）

合并 Setup 与 Countdown 两阶段状态。Setup 部分严格连续；Countdown 部分允许不连续，并叠加完美计数与多种取消规则。

```mermaid
stateDiagram-v2
    [*] --> SetupIdle
    SetupIdle --> SetupCounting: 满足 Price Flip 与首根 BS/SS
    SetupCounting --> SetupCounting: 同向 BS/SS 累计
    SetupCounting --> SetupIdle: 收盘价相等 (EQ) 中断
    SetupCounting --> SetupIdle: 反向输入触发重启
    SetupCounting --> SetupComplete: 达到第 9 根
    SetupComplete --> CountdownCounting: 启动同向 Countdown
    CountdownCounting --> CountdownCounting: 满足一般计数条件
    CountdownCounting --> CountdownPlus: count=12 且要求完美但未满足
    CountdownPlus --> CountdownComplete: 后续 K 线满足完美 + 一般条件
    CountdownPlus --> CountdownCancel: TDST / 反向 / 同向 Setup
    CountdownCounting --> CountdownComplete: count=13
    CountdownCounting --> CountdownCancel: TDST 突破
    CountdownCounting --> CountdownCancel: 反向 Setup 完成
    CountdownCounting --> CountdownCancel: 同向新 Setup 完成
    CountdownComplete --> [*]
    CountdownCancel --> [*]
```

手绘补充：可保留官方 q0–q8 命名作为辅助说明（即 `q0_idle / q1_post_ss_idle / q2_buy_setup_d1 / q3_buy_setup_2_8 / q4_buy_setup_done / q5_post_buy_setup / q6_sell_setup_d1 / q7_sell_setup_2_8 / q8_sell_setup_done`），但出版稿可使用上方简化命名以提高可读性。

---

## 图4-5 订单生命周期状态图（order-lifecycle.png）

订单从提交到终结的状态转换，含软超时与硬超时分支。

```mermaid
stateDiagram-v2
    [*] --> Submitted: submit_buy / submit_sell
    Submitted --> Pending: 入 pending_*_orders
    Pending --> PartialFilled: get_order 返回部分成交
    Pending --> Filled: get_order 返回全部成交
    Pending --> CancelledByEngine: 引擎主动撤单
    Pending --> SoftTimeout: 达到 pending_order_timeout_days
    SoftTimeout --> Pending: cancel_order 已发出
    SoftTimeout --> HardTimeout: 达到 pending_order_force_drop_days
    PartialFilled --> CancelledByEngine
    PartialFilled --> Filled
    Filled --> [*]: 触发 on_*_filled
    CancelledByEngine --> [*]: 累计成交=0 触发 on_*_rejected(ORDER_REJECTED)
    HardTimeout --> [*]: 强制摘除并触发 on_*_rejected(TIMEOUT)
```

绘图要点：突出软/硬超时的两阶段保护逻辑，避免实盘异常下卖单永久阻塞止损。

---

## 文件与图示对照表

| 论文图编号 | 图题 | 论文中引用文件 |
| ---- | ---- | ---- |
| 图4-1 | 系统分层架构图 | `figures/architecture.png` |
| 图4-2 | 单周期 handle\_data 主流程图 | `figures/handle-data.png` |
| 图4-3 | 信号下单成交时序图 | `figures/signal-order-sequence.png` |
| 图4-4 | TD 信号生命周期状态图 | `figures/signal-lifecycle.png` |
| 图4-5 | 订单生命周期状态图 | `figures/order-lifecycle.png` |
