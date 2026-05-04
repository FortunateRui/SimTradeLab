# 基于TD序列的策略说明

## 导航
- [TD序列的定义](#TD序列的定义)
- [代码的设计与实现](doc/Code_Design.md)
- [使用说明](#使用说明)
- [回测说明](#回测说明)
- [回测结果](#回测结果)

## TD序列的定义
Demark Sequential包含三个主要部分：Set up , Intersection 和 Count down。以下尽可能详细的介绍整个流程中采用的全部规则。

### 1. Price Flip(价格反转):  
要求至少有六根K线，其中  
- **第五根K线的收盘价比第一根K线的收盘价高，第六根K线的收盘价，比第二根K线的收盘价低**，那么就形成了熊市价格反转（Price Flip）。这第六根K线，同时也是所谓的TD买入结构的第一根K线。  
- **第五根K线的收盘价比第一根K线的收盘价低，第六根K线的收盘价，比第二根K线的收盘价高**，那么就形成了牛市价格反转（Price Flip）。这第六根K线，同时也是所谓的TD卖出结构的第一根K线。  

### 2. Setup(TD结构):
- Buy Setup（买入结构）: 当连续出现九根K线，并且这些K线的**收盘价都比各自前面的第四根K线的收盘价低**时，我们就说，形成了TD买入结构。必须是连续九根K线。如果出现中断，就必须重新开始构建。也就是重新寻找熊市价格反转，以及其后的TD买入结构。  
- Sell Setup（卖出结构）: 当连续出现九根K线，并且这些K线的**收盘价都比各自前面的第四根K线的收盘价高**时，我们就说，形成了TD卖出结构。必须是连续九根K线。如果出现中断，就必须重新开始构建。也就是重新寻找牛市价格反转，以及其后的TD卖出结构。  

TD买入结构并不限定只有九根K线，只要满足上述条件，可以一直延续。但countdown从第九根完成后就可以开始计算了。结构完成之前，如果有某个收盘价等于先期第四天的收盘价，取消结构重新来过。即需要**严格**小于或大于。一旦setup达到9根K线，就发出Buy Setup或Sell Setup信号。  

```python
# Buy Setup Condition
  # Price Flip(价格反转):
  BAR[t-1].Close >= BAR[t-5].Close
  # Setup从t开始计数，要求至少连续 9 根
  BAR[t].Close < BAR[t-4].Close
```

```python
# Sell Setup Condition
  # Price Flip(价格反转):
  BAR[t-1].Close <= BAR[t-5].Close
  # Setup从t开始计数，要求至少连续 9 根
  BAR[t].Close > BAR[t-4].Close
```
### Setup Perfection(完美Setup) ：  

完美setup是setup的理想状态，对普通Setup可靠性、成熟度、极值特征的附加确认。可通过配置参数控制是否仅在完美setup时执行后续策略。

- 买入结构中，第8**或**第9个交易日的最低价小于等于第6**和**第7个交易日的最低价。  
- 卖出结构中，第8**或**第9个交易日的最高价大于等于第6**和**第7个交易日的最高价。  

```python
# Buy Setup Perfection Condition
( Setup[8].Low <= Setup[6].Low and Setup[8].Low <= Setup[7].Low )
or
( Setup[9].Low <= Setup[6].Low and Setup[9].Low <= Setup[7].Low )
```

```python
# Sell Setup Perfection Condition
( Setup[8].High >= Setup[6].High and Setup[8].High >= Setup[7].High )
or
( Setup[9].High >= Setup[6].High and Setup[9].High >= Setup[7].High )
```

### Intersection(TD交叉):

- [ ] 暂定作为配置参数。后续更新中加入此功能。

### Countdown(TD计数):
首个Setup完成，并满足交叉（可选配置）后开始对应方向的Countdown：  

Countdown分为**序列计数**和**组合计数**。序列型和组合型拥有相同的Setup序列。“序列型”策略在趋势市场和盘整市场中都能做出有效反应，而“组合型”策略在趋势市场中反应更为灵敏，在盘整时期则表现较为迟缓。  
因此，我们常说“组合型”能识别出行情的最高点或最低点，而“序列型”则能识别出次级的测试点。当两者配合使用时，效果会更加显著。
项目中将默认使用**序列计数**。


#### 序列型计数（Sequence Countdown）：  

- 每当某日收盘价小于等于其两天前的最低价时买入计数增加1（可以不连续），直到计数增加到13。  
- 每当某日收盘价大于等于其两天前的最高价时卖出计数增加1（可以不连续），直到计数增加到13。    
 
```python
# Buy Countdown Condition
BAR[t].Close <= BAR[t-2].Low
```

```python
# Sell Countdown Condition
BAR[t].Close >= BAR[t-2].High
```

#### 组合型计数（Combo Countdown）：  

- [ ] 组合计数作为可选配置功能，后续更新中加入此功能。

### Cancel Countdown(取消计数)  
1. 相反的setup：在TD计数过程中，出现一个相反的Set up，则取消计数。  
2. 同向setup：在TD计数过程中，出现一个新的同方向的Set up，则取消计数。  
3. TDST（TD Setup Trend，TD 结构趋势）突破，通过配置参数控制启用哪一条规则：   
  - 以下五选一（Buy Countdown）：
    1. 有一根k线的最高价高于Setup中最高的收盘价  
    2. 有一根k线的最高价高于Setup中最高的最高价  
    3. 有一根k线的收盘价高于Setup中最高的收盘价  
    4. 有一根k线的收盘价高于Setup中最高的最高价（默认配置）  
    5. 有一根k线的收盘价高于Setup中最高的[实际最高价](#note1 "当前K线的最高价与其前一根K线的收盘价两者之间较高的价格")  
  - 以下五选一（Sell Countdown）：
    1. 有一根k线的最低价低于Setup中最低的收盘价  
    2. 有一根k线的最低价低于Setup中最低的最低价  
    3. 有一根k线的收盘价低于Setup中最低的收盘价  
    4. 有一根k线的收盘价低于Setup中最低的最低价（默认配置）  
    5. 有一根k线的收盘价低于Setup中最低的[实际最低价](#note1 "当前K线的最低价与其前一根K线的收盘价两者之间较低的价格")  
    
<a id="note1"></a>
> 注：  
> ***实际最低价*** :指当前K线的最低价与其前一根K线的收盘价两者之间较低的价格。  
> ***实际最高价*** :指当前K线的最高价与其前一根K线的收盘价两者之间较高的价格。    

### Perfect Countdown(完美计数)  
为了防范、避免不利价格形态与关系影响 TD 序列计数的理想进场点，可通过配置参数控制是否仅在完美计数时执行后续策略。启用时，当TD买入计数达到12时，如果第13根K线只满足一般计数条件，但是不满足完美countdown的条件，那么这根K线就不能计数为13，在显示时只显示一个“+”。

- 买进**计数第13天**的收盘价必须低于或等于**计数第8天**的收盘价。  
- 卖出**计数第13天**的收盘价必须高于或等于**计数第8天**的收盘价。

```python
# Buy Perfect Countdown Condition
Countdown[13].Close <= Countdown[8].Close
```

```python
# Sell Perfect Countdown Condition
Countdown[13].Close >= Countdown[8].Close
```
## 使用说明

## 回测说明

## 回测结果


# 待整理

### 状态机每次是全量更新还是增量更新？  

暂定新增一个配置参数：  
1.基于前复权的每日全量更新。因为采用前复权，每天的历史数据会变化，所以每天都要从头计算一遍状态机。  
2.基于动态复权的每日增量更新。动态复权就是为了增量更新，所以每天只需要输入最新的数据更新状态机即可。  

由于当前场景下，全量更新的成本很低。且日线级别的更新基本没有性能要求，所以推荐且默认使用前复权全量更新

# TODO：
- [ ] 删除注释中的noqa。  
- [ ] 整理说明文档至README.md，并剔除ai生成的内容。

- [x] 把setup countdown 到交易的记录记为1行
- [ ] 对所有股票都进行回测，分别分析整个市场的胜率和对每只股票的胜率以及收益
- [x] tick级别的止损交易策略，止损的设置

- [x] 前复权定义整理至论文中，讲解全量更新
- [x] 寻找回测方法相关论文，引用方法

- [ ] 抽取5支股票进行5年的回测，验证simtradelab和PTrade的一致性，并在论文中做相关的补充
- [ ] 稳健性检测


- [ ] 第一章command+f搜索“预留”字段，修改文本
- [ ] 最后只写展望，不要写不足
- [ ] 摘要和目录的分节符重新整理，使目录页码正确
- [ ] 表格和图片的格式重新整理，不要跨页，不要大段空白
- [ ] 章节开头在奇数页
- [ ] 删除参考文献的反向定位符

