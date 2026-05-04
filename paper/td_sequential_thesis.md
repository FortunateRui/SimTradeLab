# 基于 TD 序列的量化交易系统设计与实现

## 摘要

量化交易系统能够将交易规则、数据处理、风险控制和评价流程转化为可重复执行的软件系统，对提高策略研究的规范性和可复现性具有重要意义。TD 序列规则链条较长，包含 Setup、Countdown、完美计数和 TDST 取消等多阶段状态，若直接采用简单条件判断实现，容易出现计数边界不清、信号重复、复权数据不一致和交易记录不可追溯等问题。本文面向 A 股日线数据、PTrade 量化交易环境和 SimTradeLab 本地回测框架，从 TD 序列状态机实现和适用性评价方法两个方面开展研究，主要工作如下。

1. 针对 TD 9-13 Sequential 规则状态依赖强、计数过程易出错的问题，设计并实现了一种基于有限状态机的 TD 序列信号识别方法。该方法将 Setup 的严格连续计数、Countdown 的非连续计数、完美 Setup、完美 Countdown、TDST 取消、同向 Setup 取消和反向 Setup 取消统一建模为事件流；同时采用前复权数据全量重算和停牌过滤机制，解决了 A 股场景下历史价格可比性和信号可追溯性问题。

2. 针对 TD 序列不能仅以收益率和胜率评价的问题，设计并实现了一个兼容 PTrade 接口、可在 SimTradeLab 本地回测框架中运行的量化交易系统，并提出事件研究与事件驱动回测相结合的适用性分析方案。系统实现了配置管理、行情获取、信号输出、交易生命周期记录和统计输出等功能；评价方案从信号密度、完美信号比例、取消原因分布、事件后收益、最大有利波动、最大不利波动和样本外分层表现等角度分析 TD 序列在不同市场状态和股票特征下的适用性。
<br>
**关键词**  TD序列  量化交易  有限状态机  回测方法  适用性分析

# Design and Implementation of Quantitative Trading System Based on TD Sequential

## ABSTRACT

Quantitative trading systems transform trading rules, data processing, risk control, and evaluation procedures into repeatable software systems, which is meaningful for improving the standardization and reproducibility of strategy research. TD Sequential has a long rule chain and contains multi-stage states such as Setup, Countdown, perfected counting, and TDST cancellation. A direct implementation with simple conditional statements may lead to ambiguous counting boundaries, duplicated signals, inconsistent adjusted prices, and untraceable trading records. Taking A-share daily data, the PTrade quantitative trading environment, and the SimTradeLab local backtesting framework as the application context, this thesis studies TD Sequential from two aspects: state-machine-based implementation and applicability evaluation. The main work is as follows.

1. To solve the problems of strong state dependence and error-prone counting in TD 9-13 Sequential, a signal recognition method based on finite state machines is designed and implemented. The method models the strict consecutive counting of Setup, the non-consecutive counting of Countdown, perfected Setup, perfected Countdown, TDST cancellation, same-direction Setup cancellation, and opposite-direction Setup cancellation as a unified event flow. Meanwhile, full recalculation based on pre-adjusted prices and suspension filtering are adopted to address price comparability and signal traceability in the A-share market.

2. To avoid evaluating TD Sequential only by return and win rate, a quantitative trading system compatible with PTrade APIs and runnable on the SimTradeLab local backtesting framework is designed and implemented, and an applicability analysis scheme combining event study and event-driven backtesting is proposed. The system implements configuration management, market data acquisition, signal output, trade lifecycle recording, and statistical output. The evaluation scheme analyzes the applicability of TD Sequential under different market states and stock characteristics from the perspectives of signal density, perfected signal ratio, cancellation reason distribution, post-event return, maximum favorable excursion, maximum adverse excursion, and out-of-sample stratified performance.
<br>
**KEY WORDS**  TD Sequential  Quantitative Trading  Finite State Machine  Backtesting Method  Applicability Analysis

## 目录

## 第一章 绪论

### 1.1 研究背景与意义

随着证券市场电子化程度的提高，交易策略从经验判断逐渐转向规则化、程序化和可验证的系统实现。量化交易的基本思想是将投资假设转化为明确的计算规则，再通过历史数据、实时数据和交易接口完成信号生成、订单执行与结果反馈。与人工主观判断相比，量化系统的优势在于可重复、可回溯、可审计，但其风险也同样明显：若规则定义不清、数据处理不一致或回测方法过度简化，历史结果可能无法反映真实可执行性。

TD 序列是 DeMark 指标体系中应用较广的技术分析方法，原始思想强调以连续价格比较识别趋势耗竭，而不是简单追随趋势。官方说明将 Sequential 描述为由 Setup 和 Countdown 构成的多阶段价格比较过程，其目标是分析趋势强弱及其发生反转的可能性[^1]。DeMark 的技术分析体系强调用客观规则替代主观画线与形态识别[^2]，Perl 对多种 DeMark 指标的交易含义和使用方式进行了系统整理[^3]。从软件实现角度看，TD 序列的价值不仅在于给出买卖提示，更在于它天然具有状态机结构：Setup 要求严格连续，Countdown 允许不连续，取消条件又可能来自 TDST 突破、同向 Setup 或反向 Setup。这使得 TD 序列非常适合作为量化交易系统中“复杂技术指标工程化实现”的研究对象。

对于 A 股市场而言，TD 序列的研究还具有现实意义。A 股存在分红送转、停牌、涨跌停、ST 风险提示、退市整理和较明显的散户交易特征，直接照搬期货、外汇或指数市场中的技术指标规则，可能造成信号失真。尤其是 TD 序列依赖历史收盘价、高低价之间的相对关系，若复权方式不统一，历史序列的可比性会受到影响。因此，在 A 股环境中实现 TD 序列，需要同时处理复权数据、停牌过滤、信号时间对齐和可交易性约束。

本课题的意义主要体现在三个方面。第一，从指标实现角度，将 TD 序列拆解为可验证的状态机与事件流，减少复杂规则在代码中的隐式耦合。第二，从系统设计角度，围绕 PTrade 兼容环境和 SimTradeLab 本地回测框架设计可运行、可配置、可输出完整生命周期记录的量化交易系统。第三，从研究评价角度，不以盈利率和胜率作为唯一目标，而是建立面向适用性分析的回测方法，关注 TD 信号在不同市场状态、流动性水平和股票特征下的表现差异。

### 1.2 国内外研究现状

技术分析的有效性长期存在争议。一方面，有效市场假说认为历史价格信息难以稳定产生超额收益；另一方面，大量实证研究表明，在市场不充分有效、交易者行为偏差较强或制度摩擦较多的阶段，技术交易规则可能具有阶段性预测能力。国内早期研究中，韩杨对中国股市技术分析有效性进行了统计检验，认为早期 A 股市场中短期技术分析更容易体现有效性，但随着市场发展，单纯依靠技术分析获得超额收益的难度提高[^4]。孙碧波和方健雯从技术分析获利能力角度检验中国证券市场弱态有效性，指出部分技术规则在上证指数上具有长期、稳定的超额利润，且交易成本和异步交易不能完全解释该现象[^5]。林玲等使用移动平均线交易规则进行可预测性研究，认为移动平均线策略在样本中获得高于买入持有的平均收益率[^6]。唐雨虹等进一步研究量价配合的技术交易规则，发现交易规则有效性与时间跨度、成交量条件和交易成本有关[^7]。汤光华和邓益民则从技术指标与市场弱式有效的关系出发，说明技术指标的统计关系需要结合市场交易时段和信息反应速度理解[^8]。

近年的外文研究更加重视数据探测偏差和样本外稳定性。Wang 等对中国市场中数千种技术交易规则进行 Superior Predictive Ability 检验，指出部分指数阶段性存在技术交易规则盈利能力，但这种能力会随着市场效率提高而减弱[^9]。Jiang、Tong 和 Song 使用大量技术分析信号并控制数据探测偏差，发现中国股票市场中仍存在若干技术规则的可预测证据[^10]。Chuang 等基于 2010 年至 2021 年数据研究中国股票市场技术交易规则，在修正数据探测偏差后发现，仅少数复杂规则在部分阶段可以超过基准，且交易成本会显著削弱样本外收益[^11]。这些研究共同提示：技术指标研究不能只报告最优回测结果，而应关注样本外验证、交易成本、规则数量和稳健性。

在量化交易系统与回测方法方面，Campbell、Lo 和 MacKinlay 系统讨论了金融市场实证研究和事件研究方法，为用事件窗口考察价格反应提供了基础框架[^12]。Bailey 等提出回测过拟合概率问题，强调大量策略与参数组合反复测试会放大历史绩效[^13]。Sharpe 提出的风险调整后收益评价思想为夏普比率等指标提供了理论基础[^14]。国内教材方面，战雪丽和杨庆泉系统介绍了量化交易概念、策略类型、平台、样本内外测试和风险管理[^15]；丁鹏对量化投资策略与技术体系进行了较全面梳理[^16]；王晓华、欧阳鹏程等图书则从 Python 或 vn.py 实践角度介绍了量化交易系统开发流程[^17][^18]。这些成果为本文的系统设计和回测评价提供了参考。

现有研究对 TD 序列本身的系统化实现讨论相对不足。多数资料集中在交易规则解释或指标图形展示，对状态机建模、复权处理、信号生命周期输出和 A 股适配问题讨论较少。本文因此将研究重点放在 TD 9-13 Sequential 的规则形式化与系统实现上，并提出事件研究与事件驱动回测结合的适用性分析框架。

### 1.3 研究内容

本文围绕“TD 序列如何在 A 股量化交易系统中被准确实现并被合理评价”这一问题展开，主要研究内容如下。

第一，梳理 TD 9-13 Sequential 的核心规则。包括 Price Flip、Buy/Sell Setup、Setup Perfection、Sequence Countdown、Perfect Countdown、TDST 取消、同向 Setup 取消、反向 Setup 取消等内容，并明确本文系统已实现与暂不实现的规则边界。

第二，设计 TD 序列状态机模型。将 Setup 的严格连续计数抽象为有限状态机，将 Countdown 的非连续计数抽象为可启动、可推进、可完成、可取消的事件对象，并为每次信号生成保留 Setup 起点、Countdown 计数日、TDST 阈值、完美状态和取消原因。

第三，设计并实现量化交易系统。系统采用 PTrade 兼容接口编写策略逻辑，能够在 PTrade 环境中迁移运行，也能够借助 SimTradeLab 本地回测框架完成大规模历史回测，完成配置读取、行情获取、停牌过滤、K 线清洗、信号计算、交易生命周期记录、统计输出和日志管理。系统强调模块解耦、配置显式化和输出可追溯。

第四，提出专业但不过度复杂的回测与适用性分析方法。该方法将策略实现、回测引擎和评价口径区分开来：策略代码负责按照 TD 规则生成信号并记录生命周期，SimTradeLab 负责在本地高效执行大规模历史回测，评价层采用事件研究和事件驱动交易回测两类口径分析信号适用性。

### 1.4 论文组织结构

全文共分为六章。第一章介绍研究背景、意义、国内外研究现状、研究内容与结构安排。第二章介绍 TD 序列相关理论、A 股数据处理问题、PTrade、SimTradeLab 与数据源基础以及系统需求分析。第三章给出 TD 序列信号模型设计，重点说明 Setup 与 Countdown 的状态机实现。第四章说明量化交易系统的架构、模块设计与关键实现。第五章提出回测方法与适用性分析方案，并预留后续实证结果展示位置。第六章总结全文工作，并讨论后续优化方向。

## 第二章 相关理论与需求分析

### 2.1 TD 序列基本理论

TD Sequential 通常由 Setup 与 Countdown 两个核心阶段构成，并辅以 Price Flip、Intersection、TDST、Recycle 和若干滤网规则。本文实现重点集中在 TD 9-13 的主干流程，即 9 根 Setup 与 13 个 Countdown。由于 Intersection、Combo Countdown 和 Recycle 在不同资料中存在较多细节分歧，且会显著增加实现复杂度，本文将其作为扩展功能保留，不作为当前系统的必要条件。

Price Flip 是 Setup 启动前的方向转换条件。以买入方向为例，若前一根 K 线的收盘价不低于其前四根 K 线的收盘价，而当前 K 线收盘价低于其前四根 K 线的收盘价，则出现由强转弱的价格反转，当前 K 线可作为 Buy Setup 的第一根。卖出方向则相反。Setup 阶段要求连续 9 根 K 线满足同一方向的四日前收盘价比较关系：Buy Setup 要求当前收盘价严格小于四日前收盘价，Sell Setup 要求当前收盘价严格大于四日前收盘价。严格不等号意味着相等会中断 Setup。

Setup 完成后可以启动 Countdown。Sequence Countdown 不要求连续，Buy Countdown 的一般计数条件为当前收盘价小于等于两日前最低价，Sell Countdown 的一般计数条件为当前收盘价大于等于两日前最高价。计数达到 13 时，认为趋势耗竭信号完成。完美 Countdown 进一步要求 Buy Countdown 第 13 个计数日收盘价低于或等于第 8 个计数日收盘价，Sell Countdown 则要求第 13 个计数日收盘价高于或等于第 8 个计数日收盘价。

TDST 用于判断原 Setup 所代表的趋势结构是否被突破。以 Buy Countdown 为例，若价格向上突破 Setup 阶段高点或真实高点阈值，说明原下跌耗竭计数的结构基础可能失效，需要取消当前 Countdown。本文系统提供五种 TDST 取消规则配置，默认采用收盘价突破 Setup 阶段最高价的规则，使取消逻辑在保守性和可执行性之间保持平衡。

### 2.2 A 股市场数据与复权问题

TD 序列以历史价格之间的相对关系作为核心输入，因此数据连续性和可比性直接影响信号质量。A 股上市公司可能发生现金分红、送股、资本公积转增股本、配股和拆细等权益事件。交易所规则中，除权除息日即时行情显示的前收盘价会调整为除权除息参考价，并以该参考价作为当日涨跌幅限制的计算基准[^19]。这说明除权除息不是普通市场波动，而是由公司权益分派引起的价格口径变化。如果直接把未复权价格输入 TD 序列，除权除息日前后可能出现机械跳空，TD 的四日前收盘价比较、两日前高低价比较和 TDST 突破判断都可能受到扭曲。

量化研究中常见的价格口径包括未复权、前复权和后复权。未复权价格保留交易日真实成交价格，适合用于订单执行、成交价格复核、涨跌停判断和资金流水核算；但它不能消除分红送转造成的名义价格断点。前复权以最近交易日价格为基准，将历史价格按复权因子向前调整，使近期价格保持接近真实市场价格，同时让历史 K 线在视觉和数值上连续，较适合技术指标计算、信号识别和历史形态比较。后复权通常以较早日期价格为基准，将后续权益影响累积到之后价格中，适合观察长期持有视角下的累计收益或总回报趋势，但后复权后的当前价格可能显著偏离真实可交易价格，因此不宜直接作为交易执行价格。DolphinDB 关于复权行情计算的资料也将未复权、前复权和后复权区分为不同用途，并指出复权处理的目的在于消除分红、送股、拆股等权息事件对股价走势的影响[^20]。

表2-1  价格复权口径比较表

| 价格口径 | 处理方式 | 主要适用场景 | 本文使用方式 |
| ---- | ---- | ---- | ---- |
| 未复权价格 | 保留交易当日实际成交价格，不消除权息事件造成的名义跳变 | 成交复核、涨跌停判断、资金流水和真实交易价格分析 | 用于交易执行口径和可交易性校验 |
| 前复权价格 | 以最近价格为基准，向前调整历史价格，使历史序列连续 | 技术指标计算、历史形态比较、信号识别 | 用于 TD Setup 与 Countdown 信号计算 |
| 后复权价格 | 以较早价格为基准，向后累积权息影响 | 长期持有收益展示、总回报趋势观察 | 本文不作为信号输入和执行价格 |

本文在系统实现中严格区分“信号价格口径”和“交易执行口径”。信号识别层采用前复权日线数据，目的是使 TD 序列比较的收盘价、高价和低价处于同一可比口径，避免把除权除息造成的名义价格变化误判为趋势耗竭或趋势突破。交易评价层则关注信号出现后是否真正能够成交，因此需要结合未复权或实际可交易价格、涨跌停价格、停牌状态和手续费税费进行判断。也就是说，前复权主要解决“信号能不能正确识别”的问题，未复权或实际交易价格主要解决“交易能不能真实执行”的问题，二者不能混用。

前复权还带来一个重要工程问题：每当新的分红、送转或配股发生时，历史复权因子可能变化，过去已经缓存的前复权 K 线和 TD 状态也可能随之改变。若系统采用长期增量缓存，只在前一日状态上继续推进，可能出现缓存状态与当前前复权序列不一致的问题。因此，本文系统采用“前复权 + 每日全量重算”的设计，即每个交易周期重新获取足够长度的历史 K 线，从头运行 Setup 与 Countdown 状态机，再筛选最后一根已完成 K 线产生的新信号。该方法牺牲少量计算效率，但日线级别下计算成本较低，能够保证 TD 状态始终来自同一复权口径下的价格序列。

在回测研究中，还需要注意前复权数据的时间视角。若直接使用样本期末一次性生成的前复权全历史数据，未来发生的权息事件会被反映到更早的历史价格中。对于只依赖同一权息区间内相对大小比较的 TD 规则而言，这通常不会改变同一区间内价格比较的方向；但为了保持研究口径严谨，本文在事件生成逻辑上采用滚动日视角：每个事件日只使用截至该日已经发生的行情和权息信息，事件日之后发生的权息变化只影响后续重新计算，不倒灌为事件日前的交易依据。这样可以减少复权处理引入未来信息的风险。

除复权外，A 股还需要处理停牌、涨跌停、ST 和退市风险。信号计算层需要过滤成交量为零或停牌的 K 线，交易评价层需要考虑信号出现后的可交易性。如果某只股票在事件后的下一交易日停牌、涨停无法买入或跌停无法卖出，回测应顺延到下一可交易日或记录为不可执行事件，而不能默认按理想价格成交。

### 2.3 PTrade、SimTradeLab 与数据源基础

本文策略代码按照 PTrade 量化交易平台的接口风格实现。PTrade 提供策略生命周期函数、股票池设置、行情获取、委托下单、持仓查询和交易回报等接口，支持研究、回测和交易模块中的策略开发[^21]。系统使用 `initialize` 完成配置读取、输出目录准备和对象构造；使用 `before_trading_start` 刷新当日可交易股票池；使用 `handle_data` 获取历史数据、计算信号并执行交易逻辑；在交易环境中使用 `tick_data` 和 `on_trade_response` 跟踪逐笔成交与止盈止损。

为提高大规模回测效率，本文采用 GitHub 开源项目 SimTradeLab 作为本地模拟回测环境。SimTradeLab 是一个受 PTrade 事件驱动架构启发的本地回测框架，提供 PTrade API 模拟层，并在项目说明中标明策略可在 SimTradeLab 与 PTrade 之间无代码迁移；其 README 还给出了相较 PTrade 平台 100-160 倍的性能提升说明[^22]。本文回测数据来自 SimTradeLab 配套数据项目 SimTradeData。该项目提供 A 股日线 OHLCV、涨跌停价格、估值、除权除息、交易日历、指数成分、ST/停牌状态等数据，并支持导出为 Parquet 文件供 SimTradeLab 使用[^23]。

### 2.4 系统需求分析

根据毕业设计任务要求与 TD 序列研究目标，系统需求可分为功能需求和非功能需求。功能需求面向策略运行流程，非功能需求面向系统稳定性、可追溯性和可扩展性。

表2-2  系统功能需求表

| 需求编号 | 需求名称 | 需求说明 |
| ---- | ---- | ---- |
| FR1 | 配置管理 | 通过独立 JSON 配置股票池、频率、复权方式、TDST 规则、交易参数和日志参数 |
| FR2 | 行情获取 | 在 PTrade 兼容生命周期内获取历史 K 线，并过滤停牌和成交量异常数据 |
| FR3 | Setup 识别 | 识别 Buy/Sell Setup、完美 Setup 和 Setup 起止日期 |
| FR4 | Countdown 识别 | 识别非连续 Sequence Countdown、完美 Countdown、暂记加号和 13 完成信号 |
| FR5 | 取消规则 | 支持 TDST、同向 Setup、反向 Setup 导致的 Countdown 取消 |
| FR6 | 信号输出 | 按股票输出 Setup 与 Countdown 信号事件，保留计数日期和关键价格 |
| FR7 | 交易生命周期输出 | 从 Buy Setup 到 Countdown 取消、未买入或卖出终态形成完整记录 |
| FR8 | 统计输出 | 输出周期级统计数据，为后续适用性分析提供数据基础 |

表2-3  系统非功能需求表

| 需求编号 | 需求名称 | 需求说明 |
| ---- | ---- | ---- |
| NFR1 | 可追溯性 | 每个信号和交易事件均可回溯到 Setup、Countdown 和取消原因 |
| NFR2 | 可配置性 | TDST 规则、完美信号要求、仓位比例和风控参数均通过配置控制 |
| NFR3 | 稳定性 | 配置缺失、数据不足、停牌、接口异常等情况应记录日志并可定位 |
| NFR4 | 低耦合 | 数据层、信号层、输出层和交易层职责清晰，便于独立测试和替换 |
| NFR5 | 可扩展性 | 预留 Intersection、Combo Countdown、Recycle 和多频率联动扩展位置 |

### 2.5 本章小结

本章介绍了 TD 序列的核心理论、A 股数据处理问题、PTrade、SimTradeLab 与数据源基础以及系统需求。TD 序列的实现难点在于状态依赖和事件生命周期，而 A 股环境又要求对复权、停牌和可交易性进行处理。因此，后续章节将围绕状态机建模和系统分层实现展开。

## 第三章 TD 序列信号模型设计

### 3.1 信号模型总体思路

TD 序列信号模型将原本描述性的交易规则转化为确定性的事件流。输入是一段按时间升序排列的有效 K 线序列，输出是 Setup 事件、Countdown 进位事件、Countdown 暂记事件、Countdown 完成事件和 Countdown 取消事件。每个事件均带有股票代码、周期、发生日期、方向、计数、完美状态、关联 Setup 信息、TDST 阈值和关键价格。

该模型采用两个子状态机组合实现。SetupMachine 负责严格连续的 9 根 Setup 识别，它只关心当前收盘价与四日前收盘价的关系。CountdownMachine 由 Setup 完成事件启动，负责非连续的 13 计数、完美 Countdown 判断和 TDST 取消。TDSignalProcessor 作为编排层，按时间顺序将 K 线输入两个状态机，并处理 Setup 完成时对当前 Countdown 的取消或重启。

### 3.2 Setup 状态机设计

Setup 的核心是“连续”。若直接用循环变量计数，容易忽略 Price Flip、相等中断、同向延续和反向重启之间的差异。本文将 Setup 抽象为有限状态机，状态由方向 `sd` 和计数 `sc` 组成，其中 `sd=1` 表示买入方向，`sd=-1` 表示卖出方向，`sd=0` 表示空闲；`sc` 表示 Setup 已累计根数。

SetupMachine 对每根 K 线先进行输入分类。若当前收盘价小于四日前收盘价，输入记为 `BS`；若大于四日前收盘价，输入记为 `SS`；若相等，输入记为 `EQ`。状态机根据当前状态和输入决定是否启动、累计、反向切换或重置。Buy Setup 在第 9 根完成时输出 `BUY_SETUP` 或 `BUY_SETUP_PERFECT`，Sell Setup 在第 9 根完成时输出 `SELL_SETUP` 或 `SELL_SETUP_PERFECT`。

![Setup状态转移图](setup状态转移图.png)

图3-1  Setup状态转移图

表3-1  Setup输入分类表

| 输入 | 判定条件 | 含义 |
| ---- | ---- | ---- |
| BS | `close_t < close_{t-4}` | 满足买入 Setup 方向的价格关系 |
| SS | `close_t > close_{t-4}` | 满足卖出 Setup 方向的价格关系 |
| EQ | `close_t = close_{t-4}` | 严格条件中断，清空正在进行的 Setup |

![Setup状态转移表](状态转移表.png)

图3-2  Setup状态转移表图

完美 Setup 是对普通 Setup 的附加确认。Buy Setup 中，第 8 或第 9 根 K 线的最低价需要小于等于第 6 和第 7 根 K 线的最低价；Sell Setup 中，第 8 或第 9 根 K 线的最高价需要大于等于第 6 和第 7 根 K 线的最高价。系统在 Setup 完成时直接基于 9 根 `setup_bars` 计算完美状态，并将结果写入信号事件。

### 3.3 Countdown 状态机设计

Countdown 与 Setup 的最大差异在于不要求连续。Setup 完成后，Countdown 从同一根 K 线开始检查计数条件，因为 TD 序列规则允许 Setup 第 9 根同时成为 Countdown 的第 1 个计数日。Buy Countdown 的进位条件为当前收盘价小于等于两日前最低价，Sell Countdown 的进位条件为当前收盘价大于等于两日前最高价。每满足一次条件，计数加一，直到达到 13。

CountdownMachine 保存当前方向、已计数数量、各计数日对应 K 线、启动它的 Setup 信息、TDST 阈值、完成状态和取消状态。该状态机的事件类型包括 `PROGRESS`、`PLUS_TENTATIVE`、`COMPLETE` 和 `CANCEL`。当配置要求完美 Countdown 且当前计数已经达到 12 时，若某根 K 线满足一般计数条件但不满足完美第 13 条件，则不进位，只输出暂记加号事件，等待后续更理想的第 13 根。

表3-2  Countdown事件类型表

| 事件类型 | 触发条件 | 输出含义 |
| ---- | ---- | ---- |
| PROGRESS | 满足一般计数条件且计数未达到 13 | Countdown 正常进位 |
| PLUS_TENTATIVE | 计数为 12，满足一般条件但不满足完美第 13 条件 | 暂记加号，不完成 |
| COMPLETE | 计数达到 13 | Countdown 完成 |
| CANCEL | TDST 突破、同向 Setup 或反向 Setup 触发取消 | 当前 Countdown 终止 |

### 3.4 TDST 与取消规则设计

Countdown 的取消规则决定了一个 TD 信号生命周期是否仍然有效。本文系统支持三类取消来源。第一类是 TDST 突破，即价格突破由 Setup 阶段推导出的关键阈值。第二类是反向 Setup 完成，表示市场结构发生相反方向变化。第三类是同向 Setup 完成，表示新的同方向结构出现，系统可根据配置取消旧 Countdown 并以新 Setup 启动新的 Countdown。

TDST 阈值提供五种规则配置。以 Buy Countdown 为例，规则可选择 Setup 阶段最高收盘价、最高最高价、收盘价突破最高收盘价、收盘价突破最高最高价或收盘价突破真实最高价。Sell Countdown 对应使用最低价方向。不同规则的保守程度不同，阈值越容易突破，Countdown 被取消的频率越高。系统默认采用“收盘价突破 Setup 阶段最高最高价”的规则，以减少盘中噪声对取消判断的影响。

### 3.5 信号输出字段设计

为了支持后续适用性分析，信号输出不仅记录事件类型，还记录事件上下文。`signnal.csv` 按股票分流保存，字段包括事件日期、股票代码、周期、事件类别、事件类型、方向、计数、完美状态、取消原因、Setup 起止日期、Setup 阶段最高价、TDST 阈值、Countdown 起始日期、各计数日日期、计数第 8 日收盘价、Countdown 阶段最低价和事件 K 线价格。

表3-3  关键信号字段表

| 字段 | 说明 | 适用分析 |
| ---- | ---- | ---- |
| setup_first_dt | Setup 第一根日期 | 计算 Setup 持续区间 |
| setup_last_dt | Setup 第九根完成日期 | 定位生命周期起点 |
| setup_perfect | 是否完美 Setup | 比较完美与非完美信号差异 |
| count_1_dt 至 count_13_dt | Countdown 各计数日期 | 分析计数节奏和时间跨度 |
| countdown_8_close | 第 8 个计数日收盘价 | 判断完美 Countdown |
| countdown_low | Countdown 阶段最低价 | 计算 TD 风险价位 |
| reason | 取消原因 | 分析失效模式 |

### 3.6 本章小结

本章将 TD 序列信号识别拆解为 SetupMachine、CountdownMachine 和 TDSignalProcessor 三部分。SetupMachine 解决严格连续计数问题，CountdownMachine 解决非连续计数和取消问题，处理器负责事件编排。通过结构化事件输出，系统为后续回测和适用性分析保留了完整上下文。

## 第四章 量化交易系统设计与实现

### 4.1 系统总体架构

系统采用分层架构设计，避免将行情接口、信号计算、交易执行和文件输出混写在同一逻辑块中。软件工程教材强调需求分析、模块划分和接口设计对于系统可维护性的重要作用[^24]，设计模式思想也强调通过清晰对象职责提高代码复用性和可扩展性[^25]。本文系统虽然以单文件策略形式编写，但内部按照配置层、日志层、数据层、信号层、输出层、交易层和策略钩子层进行组织；运行环境上保持 PTrade 接口兼容，同时在本地使用 SimTradeLab 完成批量回测。

表4-1  系统分层架构表

| 层次 | 主要对象 | 职责 |
| ---- | ---- | ---- |
| 配置层 | `load_config`、配置 schema | 读取 JSON 配置并强制校验必填字段 |
| 日志层 | `StrategyLogger` | 控制台与文件双写，记录状态转移和异常 |
| 数据层 | `Bar`、`MarketDataFetcher` | 获取历史行情，转换为内部 K 线并过滤停牌 |
| 信号层 | `SetupMachine`、`CountdownMachine`、`TDSignalProcessor` | 生成 TD Setup 与 Countdown 事件 |
| 输出层 | `SignalRecorder`、`TradeRecordRecorder`、`StatisticsRecorder` | 输出信号、交易生命周期和统计 CSV |
| 交易层 | `TradeExecutor` | 将 Buy Countdown 完成信号接入基础仓位与风控 |
| 钩子层 | `initialize`、`before_trading_start`、`handle_data` 等 | 连接 PTrade/SimTradeLab 兼容生命周期 |

### 4.2 配置与运行目录设计

系统将全部可调参数集中在 `config.json` 中，包括股票池、基准、数据频率、复权方式、历史回看长度、滑点、涨跌停成交模式、Setup 完美要求、Countdown 启用状态、TDST 规则、交易仓位、止盈止损、日志级别和统计输出开关。策略启动时若配置缺失或格式错误，系统直接抛出异常终止运行，避免在错误默认值下生成看似有效的回测结果。

运行输出目录由配置文件所在目录和策略启动日期共同确定。若同日多次启动，目录自动追加 `_1`、`_2` 等后缀，避免覆盖历史结果。每个股票拥有独立子目录，用于保存信号、交易生命周期和统计文件；运行根目录保存汇总交易记录、总统计和策略日志。该设计使不同运行批次之间相互隔离，便于复现实验。

### 4.3 数据获取与清洗实现

数据层通过 PTrade 兼容的历史行情接口获取最近 `lookback_count` 根 K 线，并使用 `include=False` 获取已完成 K 线。日线策略下，`handle_data` 中最后一根 K 线通常是上一交易日 K 线，信号基于昨日收盘后可确认的信息生成，交易层在今日执行。这种时序避免使用未来数据。在 PTrade 环境中，该接口由平台提供；在本地回测中，该接口由 SimTradeLab 的 API 模拟层提供，底层读取 SimTradeData 导出的 Parquet 数据。

MarketDataFetcher 将外部行情数据转换为内部 `Bar` 对象，统一字段包括日期、开盘价、最高价、最低价、收盘价和成交量。成交量小于等于零的 K 线被视为无效交易日并过滤。盘前阶段使用 PTrade 兼容状态过滤接口剔除停牌和退市标的，盘中处理时再进行二次确认，减少对不可交易股票下单的可能。

### 4.4 信号处理流程实现

信号处理在每个周期对每只有效股票独立执行。系统每日构造新的 TDSignalProcessor，将最近 N 根有效 K 线全量输入状态机，得到完整历史事件流，再筛选最后一根 K 线产生的新事件。该方式虽然不是最高效的增量更新，但能适配前复权价格历史变动，避免长期缓存状态与当前复权序列不一致。

信号处理流程如下：首先，SetupMachine 从第 5 根 K 线开始运行，保证四日前收盘价可用；其次，若 Setup 完成，则输出 Setup 事件，并根据配置决定是否启动 Countdown；再次，若已有 Countdown 且新 Setup 与其方向相反或相同，则根据取消配置输出取消事件并启动新 Countdown；最后，CountdownMachine 在每根 K 线上检查 TDST 取消和计数条件，输出进位、暂记、完成或取消事件。

### 4.5 交易生命周期记录实现

本文系统包含交易执行层，其作用是将 TD 信号接入仓位、止盈止损和结果记录流程，形成可执行、可追溯的交易生命周期。论文后续实证评价在此基础上按照第五章统一的事件研究和事件驱动回测口径统计，而不是只报告某一次运行中的单一收益结果。交易生命周期从 Buy Setup 完成开始，可能在 Buy Countdown 取消、Buy Countdown 完成但未买入、买入后止损、买入后止盈或趋势反转卖出时结束。每个终态写入一行 `trade.csv`，避免中间事件重复写入导致统计口径混乱。

表4-2  交易生命周期字段表

| 字段类别 | 代表字段 | 说明 |
| ---- | ---- | ---- |
| Setup 信息 | `setup_completed_at`、`setup_is_perfect`、`setup_highest_high` | 记录生命周期起点 |
| Countdown 信息 | `count_1_at` 至 `count_13_at`、`count_8_close` | 记录计数路径 |
| 终态信息 | `countdown_status`、`countdown_completed_count` | 标记正常完成或取消原因 |
| 买入信息 | `bought`、`buy_reject_reason`、`buy_quantity`、`buy_price` | 记录买入与未买入原因 |
| 风控信息 | `stop_loss_price`、`take_profit_price` | 记录 TD 风险价位和止盈目标 |
| 卖出信息 | `sell_price`、`sell_date`、`sell_reason`、`pnl` | 记录终态交易结果 |

### 4.6 交易与风控规则实现

交易层默认只根据 Buy Countdown 完成信号进行买入，Sell Countdown 默认保留为信号，不直接用于卖出。若配置开启 `take_profit_on_sell_countdown`，则 Sell Countdown 完成且当前仓位盈利时可作为趋势反转止盈信号。每次买入使用总资产固定比例，默认 10%，并设置最小交易金额和最小现金要求，防止过小订单和近似满仓状态下继续下单。

止损价基于 TD 序列建议的风险价位计算。系统在 Countdown 阶段找到最低价所在 K 线，将该 K 线最低价减去其高低价差的一定倍数作为止损价；止盈价采用 1.5R 规则，即买入价与止损价之间的风险距离乘以 1.5 后加到买入价上。实盘环境中，成交价和数量以 `on_trade_response` 的成交回报为准，止盈止损在 `tick_data` 中逐 tick 追踪；回测环境中则降级为在后续已完成 K 线上使用最高价和最低价判断触发。

### 4.7 本地模拟回测与效率优化

TD 序列需要在每个交易日对每只股票重新计算较长历史窗口，若研究对象扩展到 5000 多只 A 股并覆盖约 10 年日线样本，直接在 PTrade 平台逐标的运行会面临明显的时间成本。按本课题前期运行耗时折算，若完全使用 PTrade 环境完成全市场长周期回测，总耗时可能超过 1000 小时，难以及时支持参数检查、日志排错和分层统计。

为解决该问题，本文采用 SimTradeLab 作为本地模拟回测引擎。根据项目 README，SimTradeLab 提供 PTrade API 模拟、数据常驻内存、多级缓存、按需数据加载和生命周期控制等能力，并标称本地回测速度比 PTrade 平台提升 100-160 倍[^22]。在本课题的全市场长周期回测场景中，使用 SimTradeLab 后回测任务可在 12 小时以内完成。由于策略代码遵循 PTrade 接口形式，信号识别与交易生命周期逻辑可以在 SimTradeLab 中高效验证，并在需要时迁移到 PTrade 环境运行，从而兼顾研究效率和平台兼容性。

### 4.8 异常处理与可扩展性

系统在配置缺失、文件不可读、行情数据不足、停牌、下单失败和 CSV 写入失败等情况下写入日志。对于 PTrade 禁用部分标准库的限制，系统尽量使用平台允许的文件和目录接口，并将 SimTradeLab 作为本地回测兼容路径。输出文件使用固定表头和按股票分流方式，便于后续使用 Python、Excel 或数据库进行统计分析。

在可扩展性方面，系统已为 Intersection、Combo Countdown、Recycle、Setup 延伸记录和多频率联动保留配置位置。后续若加入这些规则，可在信号层扩展新的状态或事件类型，同时保持输出层和适用性分析层的字段结构稳定。

### 4.9 本章小结

本章说明了基于 TD 序列的量化交易系统架构和关键实现。系统通过配置显式化、分层职责划分、状态机信号识别和生命周期记录，实现了 TD 9-13 Sequential 在 PTrade 兼容环境中的可运行闭环；同时借助 SimTradeLab 将大规模回测任务从平台端迁移到本地端，提高了研究迭代效率。

## 第五章 回测方法与适用性分析方案

### 5.1 回测研究目标

本文已经围绕 TD 序列完成两类回测分析。第一类是信号事件分析，目标是判断 TD Setup、TD Countdown 和 Countdown 取消事件在 A 股样本中是否具有稳定的价格行为特征；第二类是事件驱动交易回测，目标是在加入可交易性约束、交易成本和基础风控后，检验 TD 买入信号是否能够形成完整交易闭环。事件研究方法常用于检验特定事件前后证券价格反应，Fama 等对股票价格对新信息的调整进行了经典事件研究，Brown 和 Warner 进一步讨论了日收益率数据下事件研究方法的统计特征，Kothari 和 Warner 对事件研究方法的模型设定、样本特征和短窗口可靠性进行了系统综述[^26][^27][^28]。本文借鉴该思路，将 TD 信号视为技术分析事件，而不是公司公告事件。两类分析共同服务于“适用性”判断，而不是单纯证明策略必须取得较高收益率或胜率。

具体而言，本文将 TD 序列适用性拆分为四个可观测问题：第一，信号能否在全市场样本中稳定生成，Setup 到 Countdown 的转化比例如何；第二，Buy Countdown 完成后，事件窗口内是否存在可观察的反弹、跌幅收敛或风险收益结构变化；第三，完美 Setup、完美 Countdown、不同取消原因和不同市场状态是否会造成信号表现差异；第四，在下一可交易日入场、固定仓位、TD 风险价位止损和 1.5R 止盈规则下，信号能否形成可执行的交易流程。

### 5.2 数据样本与预处理方法

本文使用 SimTradeData 提供的 A 股日线数据，并通过 SimTradeLab 在本地完成回测。回测区间设定为 2016 年 1 月 1 日至 2025 年 12 月 31 日，覆盖主板、创业板和科创板中具备有效日线数据的股票。

信号识别层使用前复权日线价格，字段包括开盘价、最高价、最低价、收盘价和成交量；该处理与第二章所述复权口径一致，目的是保证 TD 价格比较关系连续[^20]。交易执行层使用事件日之后的下一可交易日实际可交易价格口径，并根据涨跌停、停牌和 ST 状态判断能否执行。本文所用 SimTradeData 提供日线行情、涨跌停价格、除权除息、交易日历、指数成分和 ST/停牌状态等字段，可支撑信号识别和交易约束的分层处理[^23]。为避免新股上市初期价格行为对 TD 序列造成明显干扰，股票上市后前 120 个交易日不纳入事件统计。若某股票在事件日或入场日处于停牌、退市整理或缺少必要价格字段状态，则该事件记为不可执行，但仍保留在信号统计中。

表5-1  回测数据样本表

| 项目 | 取值 |
| ---- | ---- |
| 回测区间 | 2016-01-01 至 2025-12-31 |
| 市场范围 | A 股主板、创业板、科创板 |
| 数据频率 | 日线 |
| 数据来源 | SimTradeData 导出数据 |
| 回测引擎 | SimTradeLab |
| 回测基准 | 沪深300指数（收盘价） |
| 样本股票数量 | 5510支 |
| 有效交易日数量 | 2430天 |
| 最终可分析事件数量 | 119094条 |

表5-2  数据预处理规则表

| 项目 | 处理方法 | 目的 |
| ---- | ---- | ---- |
| 复权方式 | 信号识别使用前复权价格 | 保证 TD 价格比较关系连续 |
| 执行价格 | 入场、退出和交易成本使用实际可交易价格口径 | 避免用复权价格替代真实成交价格 |
| 停牌处理 | 成交量为零或状态为停牌的 K 线不推进信号 | 避免无交易日扭曲连续计数 |
| 涨跌停处理 | 涨停日不可买入，跌停日不可卖出 | 保证交易回测可执行 |
| ST/退市处理 | 事件保留标记，交易执行中单独过滤 | 分析风险股票影响 |
| 新股处理 | 上市后前 120 个交易日不纳入统计 | 降低新股异常波动影响 |

### 5.3 TD 信号事件研究方法

本文将 TD 信号输出记录转换为事件样本，事件日记为 `t`。事件类型包括 Buy Setup 完成、Sell Setup 完成、Buy Countdown 完成、Sell Countdown 完成、Buy Countdown 取消和 Sell Countdown 取消。其中，Buy Countdown 完成是交易回测的主要入场事件，Setup 完成和 Countdown 取消主要用于解释 TD 序列状态变化和适用性边界。与公司公告事件不同，TD 信号可能在市场下跌或上涨阶段集中出现，因此本文不只报告平均收益，还同时报告中位数、分位数和分层结果，以减少极端样本和事件聚集对结论的影响。该处理也符合事件研究文献中对日频样本特征和样本分层问题的关注[^27][^28]。

事件研究采用 5、20、60、120 个交易日作为观察窗口，其中 20 个交易日作为主观察窗口，5 个交易日用于观察短期反应，60 个和 120 个交易日用于观察信号效果是否延续或衰减。对于 Buy 方向事件，未来收益率定义为事件日后第 `h` 个交易日收盘价相对事件日收盘价的涨跌幅；对于 Sell 方向事件，为了保持“方向正确为正”的解释口径，采用相反数处理。方向收益率公式如下：

$$
R_{i,t,h}^{dir}=D_i \times \left(\frac{P_{i,t+h}}{P_{i,t}}-1\right) % 式（5-1）
$$

其中，`D_i=1` 表示 Buy 方向事件，`D_i=-1` 表示 Sell 方向事件。相对基准方向收益率使用个股方向收益率减去同期沪深 300 指数方向收益率。最大有利波动和最大不利波动分别用于衡量事件窗口内最有利价格变化和最不利价格变化。对于 Buy 方向，最大有利波动取窗口最高价相对事件日收盘价的最大涨幅，最大不利波动取窗口最低价相对事件日收盘价的最大跌幅；Sell 方向按方向取反。

表5-3  事件研究指标表

| 指标 | 计算口径 | 说明 |
| ---- | ---- | ---- |
| 事件数量 | 各类 TD 事件的样本数 | 衡量信号密度 |
| 方向收益率 | `D × (P_{t+h}/P_t - 1)` | 衡量信号方向上的价格变化 |
| 相对基准收益 | 个股方向收益率减同期指数方向收益率 | 剔除市场整体影响 |
| 最大有利波动 | 事件窗口内方向上最有利价格变化 | 衡量潜在机会空间 |
| 最大不利波动 | 事件窗口内方向上最不利价格变化 | 衡量信号风险暴露 |
| Setup 转化率 | Setup 后 120 日内形成同向 Countdown 13 的比例 | 衡量规则链条完成能力 |
| Countdown 取消率 | Countdown 被 TDST、同向或反向 Setup 取消的比例 | 衡量信号失效模式 |

本文对事件收益分布计算均值、中位数、25%分位数、75%分位数、方向胜率和 Bootstrap 置信区间。为避免只依赖单一均值结论，本文优先使用中位数和分位数描述 TD 信号的典型表现，并结合最大不利波动判断信号风险。若方向收益中位数为正、最大有利波动明显高于最大不利波动，且结论在不同市场状态和流动性分组中保持一致，则认为该类 TD 信号具有较好的适用性；若结果只出现在少数极端样本或低流动性样本中，则仅作为现象记录，不作为稳健结论。

表5-4  TD信号事件研究结果表

| 事件类型 | 样本数 | 5日方向收益中位数 | 20日方向收益中位数 | 20日最大有利波动中位数 | 20日最大不利波动中位数 | 阶段性结论 |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| Buy Setup 完成 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| Buy Countdown 完成 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| Sell Setup 完成 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| Sell Countdown 完成 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| Countdown 取消 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |

根据表5-4，本文得到的事件研究结论为：【待补充】。

### 5.4 事件驱动交易回测方法

交易回测只使用 Buy Countdown 完成事件作为入场信号。由于 A 股普通股票不能直接做空，Sell Countdown 不作为开仓信号，仅作为事件研究对象和可选趋势反转退出信号。事件驱动回测按照“信号出现、下一可交易日执行、持仓期间按规则退出”的顺序推进，避免在信号日收盘后又假设以同日收盘价成交的未来函数问题。入场日为信号事件日后的下一可交易日，入场价格采用下一可交易日开盘价；若下一交易日停牌或开盘涨停，则最多向后顺延 5 个交易日，仍无法买入则记为不可执行事件。

仓位控制采用固定比例法。初始资金设为 1,000,000 元，每次买入金额为组合当日总资产的 1%，单只股票同一时间可以持有多笔未平仓交易，组合最多同时持有 100 只股票。若可用现金不足或买入金额低于最小交易金额，则该信号记为交易取消。交易成本统一设置为买卖双边佣金万分之三、卖出印花税千分之一、买卖双边滑点万分之二。交易成本是回测与实盘差异的重要来源，相关研究将交易成本划分为显性成本和隐性成本，并强调最优执行和成本估计对策略评价的影响[^29]；国内量化投资教材也通常将手续费、印花税、滑点和资金管理作为回测评价中的必要约束[^15][^16]。本文采用相对简单但明确的固定成本口径，目的不是精确模拟每一笔订单簿撮合，而是在各类 TD 信号之间建立统一可比较的交易评价基准。

退出规则按优先级执行。第一，若持仓期间价格触及 TD 风险价位，则以止损价退出；第二，若价格触及 1.5R 止盈目标，则以止盈价退出；第三，若出现 Sell Countdown 完成且该笔持仓处于盈利状态，则以该日收盘价退出；同一根 K 线同时触发止损和止盈时，按保守原则优先认定止损。

表5-5  事件驱动回测规则表

| 模块 | 本文设定 |
| ---- | ---- |
| 入场信号 | Buy Countdown 完成 |
| 入场时间 | 事件日后下一可交易日 |
| 入场价格 | 下一可交易日开盘价 |
| 顺延规则 | 停牌或涨停最多顺延 5 个交易日 |
| 初始资金 | 1,000,000 元 |
| 单笔仓位 | 总资产 1% |
| 最大持仓数量 | 100 只股票 |
| 止损规则 | TD 风险价位 |
| 止盈规则 | 1.5R 目标价 |
| 趋势反转退出 | 盈利状态下 Sell Countdown 完成 |
| 交易成本 | 佣金万分之三、印花税千分之一、滑点万分之二 |

表5-6  事件驱动交易回测结果表

| 策略口径 | 交易次数 | 胜率 | 平均盈亏比 | 年化收益 | 最大回撤 | 夏普比率 | 结论 |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 全部 Buy Countdown | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 完美 Setup 过滤 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 完美 Countdown 过滤 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 完美 Setup + 完美 Countdown | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |

根据表5-6，本文得到的交易回测结论为：【待补充】。

### 5.5 适用性分层分析方法

本文在全样本交易回测后进一步进行了适用性分层分析。分层分析不改变 TD 序列规则和交易规则，只按照事件发生时的市场状态、股票特征、信号质量和个股维度对样本重新分组，并比较各组信号密度、事件收益、最大不利波动、交易胜率和最大回撤。Kothari 和 Warner 指出事件研究方法的性质可能受样本公司特征和波动水平影响[^28]，因此本文不把全市场平均结果视为唯一结论，而是重点观察 TD 序列在不同市场环境和股票特征下是否存在适用边界。

市场状态使用沪深 300 指数定义。若指数收盘价高于 120 日均线且近 20 日收益率为正，则定义为上涨阶段；若指数收盘价低于 120 日均线且近 20 日收益率为负，则定义为下跌阶段；其余情况定义为震荡阶段。流动性分组使用事件日前 20 个交易日平均成交额，将股票分为高、中、低三组。波动率分组使用事件日前 20 个交易日日收益率标准差，将股票分为高、中、低三组。信号质量分组按照是否完美 Setup、是否完美 Countdown、Countdown 完成用时和取消原因划分。

为进一步回答 TD 序列是否对某几支股票具有特别适用性，本文增加单股票适用性分析。具体方法为：首先，按股票代码汇总 Buy Countdown 完成事件和完整交易记录，剔除事件数少于 20 次或完整交易数少于 10 次的股票，避免由小样本偶然结果造成误判；其次，对每只股票分别计算 20 日方向收益中位数、20 日最大有利波动中位数、20 日最大不利波动中位数、交易胜率、平均盈亏比、最大回撤和信号可执行率；再次，将单股票指标与全市场中位数进行比较，只有当该股票在方向收益、最大不利波动和交易回测指标上同时优于全市场基准，并且在前后两个子区间中表现方向一致时，才将其列为 TD 序列的潜在适用股票；最后，对入选股票补充人工复核，检查其行业属性、波动率水平、流动性和典型 K 线走势，判断该适用性是否具有可解释性。

表5-7  适用性分层分析结果表

| 分层维度 | 分组 | 事件数量 | 20日方向收益中位数 | 交易胜率 | 最大回撤 | 分层结论 |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 市场状态 | 上涨阶段 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 市场状态 | 下跌阶段 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 市场状态 | 震荡阶段 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 流动性 | 高成交额组 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 流动性 | 低成交额组 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 信号质量 | 完美信号组 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 信号质量 | 非完美信号组 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 个股适用性 | 候选股票组 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |

根据表5-7，本文得到的适用性分层结论为：【待补充】。

表5-8  个股适用性候选结果表

| 股票代码 | 事件数量 | 完整交易数量 | 20日方向收益中位数 | 交易胜率 | 平均盈亏比 | 最大回撤 | 是否入选 | 适用性解释 |
| ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- | ---- |
| 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |
| 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 | 【待补充】 |

根据表5-8，本文得到的个股适用性结论为：【待补充】。若存在入选股票，说明 TD 序列可能更适合具有特定波动结构或交易节奏的个股；若不存在稳定入选股票，则说明 TD 序列更适合从市场状态或信号质量角度使用，而不宜将其固化为少数个股的专用策略。

### 5.6 稳健性检验

为验证结论不依赖单一参数，本文进行了三组稳健性检验。第一，将事件窗口从 20 个交易日扩展到 10 个和 60 个交易日，观察事件收益方向是否保持一致。第二，将止盈目标从 1.5R 调整为 1.0R 和 2.0R，观察交易结果是否对止盈倍数过度敏感。第三，将低流动性股票剔除后重新计算事件研究和交易回测结果，观察 TD 信号是否主要来自不可交易或成交困难的股票。回测研究容易受到参数选择、重复试验和样本内优化影响，Bailey 等提出的回测过拟合概率问题以及 López de Prado 对金融机器学习回测偏差的讨论均提示，策略评价应通过样本分层、参数扰动和约束一致性降低偶然结果的影响[^13][^30]。近年关于技术交易规则可预测性、中国市场形态交易规则和含交易成本最优交易的研究也表明，技术信号评价需要同时考虑市场差异、规则复杂度和成本约束[^31][^32][^33]。

表5-9  稳健性检验结果表

| 检验项目 | 对照口径 | 结果摘要 | 是否支持主结论 |
| ---- | ---- | ---- | ---- |
| 事件窗口 | 10日、20日、60日 | 【待补充】 | 【待补充】 |
| 止盈倍数 | 1.0R、1.5R、2.0R | 【待补充】 | 【待补充】 |
| 流动性过滤 | 全样本、高流动性样本 | 【待补充】 | 【待补充】 |

根据表5-9，本文得到的稳健性检验结论为：【待补充】。

### 5.7 本章小结

本章给出了本文实际采用的回测分析方法。本文先对 TD 信号进行事件研究，再在固定交易规则下进行事件驱动交易回测，并进一步从市场状态、流动性、信号质量和单股票维度进行分层分析。通过上述流程，本文形成了关于 TD 序列实现效果与适用边界的综合判断。综合本章各项结果，最终结论为：【待补充】。

## 第六章 总结与展望

### 6.1 研究工作总结

本文围绕 TD 9-13 Sequential 的工程化实现与适用性评价展开研究，完成了 TD 序列规则梳理、状态机模型设计、PTrade 量化交易系统实现和回测方法设计。与单纯追求策略收益不同，本文重点关注复杂技术指标在真实量化交易环境中的可解释、可复现和可追溯实现。

在规则建模方面，本文将 Setup 的严格连续计数和 Countdown 的非连续计数拆分为两个状态机，分别处理价格关系判断、完美信号、TDST 取消、同向与反向 Setup 取消。该设计使 TD 序列不再是散落在代码中的条件判断，而是可以通过状态、输入和事件清晰描述的模型。

在系统实现方面，本文设计了配置管理、日志、数据获取、信号处理、输出记录、交易执行和统计输出等模块。系统能够在 PTrade 兼容环境下运行，支持前复权全量重算、停牌过滤、按股票输出信号 CSV、记录完整交易生命周期和周期统计，并借助 SimTradeLab 完成大规模本地回测。该实现体现了软件工程中模块化、低耦合、配置显式化和异常可定位的原则。

在回测方法方面，本文提出事件研究和事件驱动交易回测结合的方案，能够更系统地分析 TD 序列在 A 股市场中的适用性。后续补充结果时，应围绕信号密度、完美信号比例、取消原因、事件后收益分布、样本外稳定性和分层表现展开，而不是只报告收益率和胜率。

### 6.2 不足与展望

本文仍存在若干不足。第一，当前系统主要实现 Sequence Countdown，Intersection、Combo Countdown、Recycle 和多种 TD 过滤器尚未完整实现。第二，当前实现以日线级别为主，尚未验证分钟线或多周期联动下的信号一致性。第三，交易执行层主要用于形成闭环，后续若用于实盘，应进一步完善订单状态、部分成交、撤单、涨跌停排队和持久化恢复。第四，适用性分析结果仍需在后续补入，并应尽量采用全市场历史股票池以降低幸存者偏差。

后续工作可以从三个方向展开。首先，完善 TD 序列规则族，实现 Intersection、Combo Countdown、Recycle 和更细粒度的 TDST 有效突破确认。其次，建立独立的研究型回测模块，将事件研究、样本外滚动检验、Bootstrap 置信区间和分层统计自动化。最后，将 TD 序列与成交量、波动率、市场状态或基本面过滤条件结合，通过消融实验分析不同模块对结果的贡献，避免简单叠加指标后无法判断 TD 序列本身作用的问题。

## 参考文献

[^1]: DeMARK Analytics. Sequential Indicator[EB/OL]. [2026-05-02]. https://demark.com/sequential-indicator/.

[^2]: DeMark T R. The New Science of Technical Analysis[M]. New York: John Wiley & Sons, 1994.

[^3]: Perl J. DeMark Indicators[M]. New York: Bloomberg Press, 2008.

[^4]: 韩杨. 对技术分析在中国股市的有效性研究[J]. 经济科学, 2001, 23(03):49-57.

[^5]: 孙碧波, 方健雯. 对中国证券市场弱态有效性的检验——基于技术分析获利能力的实证研究[J]. 上海财经大学学报, 2004, 6(6):53-58.

[^6]: 林玲, 曾勇, 唐小我. 移动平均线交易规则检验[J]. 电子科技大学学报, 2000, 29(6):647-650.

[^7]: 唐雨虹, 曾勇, 唐小我. 量价配合的技术分析交易规则有效性研究[J]. 电子科技大学学报, 2005, 34(5):720-723.

[^8]: 汤光华, 邓益民. 技术指标与市场弱式有效的实证研究[J]. 统计研究, 2004(12):31-34.

[^9]: Wang S, Jiang Z Q, Li S P, 等. Testing the performance of technical trading rules in the Chinese markets based on superior predictive test[J]. Physica A: Statistical Mechanics and its Applications, 2015, 439:114-123.

[^10]: Jiang F, Tong G, Song G. Technical analysis profitability without data snooping bias: Evidence from Chinese stock market[J]. International Review of Finance, 2019, 19(1):191-206.

[^11]: Chuang O C, Chuang H C, Wang Z, 等. Profitability of technical trading rules in the Chinese stock market[J]. Pacific-Basin Finance Journal, 2024, 84:102278.

[^12]: Campbell J Y, Lo A W, MacKinlay A C. The Econometrics of Financial Markets[M]. Princeton: Princeton University Press, 1997.

[^13]: Bailey D H, Borwein J M, López de Prado M, 等. The probability of backtest overfitting[J]. Journal of Computational Finance, 2016, 20(4):39-69.

[^14]: Sharpe W F. The Sharpe Ratio[J]. Journal of Portfolio Management, 1994, 21(1):49-58.

[^15]: 战雪丽, 杨庆泉. 量化投资实务[M]. 北京: 清华大学出版社, 2022.

[^16]: 丁鹏. 量化投资: 策略与技术[M]. 北京: 电子工业出版社, 2016.

[^17]: 王晓华. Python量化交易实战[M]. 北京: 清华大学出版社, 2019.

[^18]: 欧阳鹏程. Python量化交易实战: 使用vn.py构建交易系统[M]. 北京: 清华大学出版社, 2023.

[^19]: 上海证券交易所. 关于修改《上海证券交易所交易规则》及《上海证券交易所参与者交易业务单元实施细则》涉及交易参与人若干条款的通知[EB/OL]. [2026-05-04]. https://big5.sse.com.cn/site/cht/www.sse.com.cn/lawandrules/sselawsrules/trade/universal/c/c_20210128_5312084.shtml.

[^20]: DolphinDB. 股票复权因子和复权行情计算[EB/OL]. [2026-05-04]. https://docs.dolphindb.cn/zh/tutorials/market_condition_adjustments.html.

[^21]: PTrade. PTrade量化交易API接口文档[EB/OL]. [2026-05-02]. https://ptradeapi.com/.

[^22]: kay-ou. SimTradeLab: 轻量级量化回测框架 - PTrade API本地实现[EB/OL]. [2026-05-02]. https://github.com/kay-ou/SimTradeLab.

[^23]: kay-ou. SimTradeData: Stock Market Data Downloader and Processor[EB/OL]. [2026-05-02]. https://github.com/kay-ou/SimTradeData.

[^24]: 张海藩, 牟永敏. 软件工程导论: 第6版[M]. 北京: 清华大学出版社, 2013.

[^25]: Gamma E, Helm R, Johnson R, 等. 设计模式: 可复用面向对象软件的基础[M]. 李英军, 马晓星, 蔡敏, 等译. 北京: 机械工业出版社, 2007.

[^26]: Fama E F, Fisher L, Jensen M C, 等. The Adjustment of Stock Prices to New Information[J]. International Economic Review, 1969, 10(1):1-21.

[^27]: Brown S J, Warner J B. Using daily stock returns: The case of event studies[J]. Journal of Financial Economics, 1985, 14(1):3-31.

[^28]: Kothari S P, Warner J B. Econometrics of event studies[M]//Eckbo B E. Handbook of Empirical Corporate Finance. Amsterdam: Elsevier, 2007:3-36.

[^29]: Kissell R, Glantz M. A practical framework for estimating transaction costs and developing optimal trading strategies to achieve best execution[J]. Finance Research Letters, 2004, 1(1):35-46.

[^30]: López de Prado M. Advances in Financial Machine Learning[M]. Hoboken: Wiley, 2018.

[^31]: Rink K. The predictive ability of technical trading rules: an empirical analysis of developed and emerging equity markets[J]. Financial Markets and Portfolio Management, 2023, 37:403-456.

[^32]: Lobão J, Pacheco L, Fernandes A. Trading rule discovery using technical analysis and a template matching technique for pattern recognition: Evidence from the Chinese stock market[J]. International Studies of Economics, 2024, 19(2):168-185.

[^33]: Murthy S, Wald J K. Optimal trading with transaction costs and short-term predictability[J]. Quantitative Finance, 2023, 23(7-8):1115-1127.

## 致谢

2022年12月1日，ChatGPT横空出世，感谢大语言模型（Large Language Models, LLMs）。
