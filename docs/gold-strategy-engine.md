# 黄金策略引擎：分层、数据时点与验证边界

> 状态：设计与当前工作树实现说明；核对日期为 2026-08-11。
>
> 本文不提供收益率或胜率承诺。“置信度”是规则强度的启发式分数，不是经校准的上涨/下跌概率。只有通过按品种、周期和数据源分层的样本外检验后，策略结果才可以被描述为历史验证结果；即使如此，也不能外推为未来表现。

## 1. 目标与非目标

策略引擎的目标是把价格结构、波动状态和外部市场上下文转换为可审计的观察结果。每个结果都应回答：使用了哪些字段、这些字段在决策时是否已经可得、何时失效、缺失时如何降级，以及是否允许进入方向合成或实验回测。

当前系统不是自动下单系统。尤其是黄金期货还缺少完整的可交易连续合约、换月执行、手续费/滑点和交易所约束模型；`AU8888`、`AG8888` 是供应商发布的加权观察值，不是可以直接下单的月份合约。

## 2. 三层架构

| 层 | 职责 | 可以产生什么 | 不允许做什么 |
| --- | --- | --- | --- |
| 一、因果交易策略候选 | 只使用决策时点以前已经可得的 Bar 前缀计算方向、确认或耗竭证据 | 单策略信号、实验性 composite 分数、具备明确进出场规则后的实验回测 | 不得把同柱未来信息、后续摆动点或未发布的外部收盘值写回历史；“已接线”不等于“已验证” |
| 二、系统结构标注 | 标出支撑/压力、趋势线、FVG、POC≈ 等位置和状态，帮助解释风险位置 | 图层、标签、候选/确认/测试/失效状态及原因 | 默认不直接决定仓位；不得把事后质量评分回填成早期信号 |
| 三、非方向市场上下文 | 描述隐含波动、成交活跃度和持仓变化等环境 | 风险状态、数据新鲜度、量仓覆盖度、条件标签 | 不得把 VIX、GVZ 或总持仓量机械翻译为金价方向 |

目录字段的统一语义如下：

- `role` 描述策略在系统中的角色，例如 `direction`、`confirmation`、`exhaustion`、`structure`、`risk-context`。
- `evidenceMode=native` 表示直接由声明字段计算；`conditional` 表示只有外部字段完整时才启用；`proxy` 必须始终显示 `≈`，不能伪装成真实订单流或真实逐价成交分布。
- `compositeEligible` / `backtestEligible` 只表示代码接线资格，不表示样本外验证通过，更不表示生产可交易。
- 当前 `confidence` 是有限范围内的规则强度权重，只用于合成，不是预测概率。

## 3. 全局 as-of 约束

一次分析至少要保留以下时间：

- `decision_as_of`：本次结果允许使用信息的截止时刻。
- `observed_at`：上游数据所描述的市场时刻。
- `published_at`：上游实际发布时刻；上游不提供时必须为 `null`，不能猜测。
- `received_at`：本系统收到数据的时刻。
- K 线的 `open_time`、`state`、`revision` 与 `finalized_at`。

任何输入都必须同时满足 `observed_at <= decision_as_of` 且在当时已经发布并被系统接收。上游不提供 `published_at` 时，以 `received_at` 作为“系统最早确认可得”的保守边界。跨市场日线不能只按日期字符串连接：Cboe 美国收盘值在发布前不能用于更早的上期所或北京时间决策。

当前专家工作区先定位最后一根 `state=final` 的 Candle，并把确认历史与实时指标预览使用不同的 history key。MA/BOLL 结论、Setup 9、智能趋势锚点、composite 与回测都只读取这段确认前缀；尾部 `provisional_quote` / `provisional_authoritative` 仍可驱动价格和实时指标预览，但不会改写确认信号。`normalizedBars` 还保留原始 Candle 前缀到有效 Bar 数量的映射，因此截止点之前存在非法 OHLC 行时，也不会把截止点错误地推进到后面的未完成柱。

- 正式研究只使用当时已经 `final` 的 Bar，或使用保存了当时 revision 的 as-of 快照；
- 上游修订导致 OHLCV 改变时会按 revision 使确认历史重算，不能只保留最终修订后的历史再声称实时可得；
- 实时价格预览与确认策略状态必须在 UI 语义上分开，不能把“正在变化的价格”写成“已确认信号”。

本文不嵌入任何“最新指数值”。数据新鲜度应在运行时由上述时间字段判断，而不是由文档日期判断。

## 4. 第一层：因果交易策略候选

### 4.1 MA20 / MA60 / MA120 / MA250

当前实现使用简单移动平均：

`SMA_n(t) = (C_t + C_{t-1} + ... + C_{t-n+1}) / n`

四条均线只在各自周期内计算，不跨周期借值。完整多头排列是 `MA20 > MA60 > MA120 > MA250`，完整空头排列反向；当前代码在至少两条均线可用时也会给出部分排列，因此 UI 和研究输出必须区分“部分排列”和“四线完整排列”。斜率使用当前均线相对 5 根 Bar 前均线的每 Bar 百分比变化；多头排列还要求收盘高于最短可用均线且所有可用斜率不为负，空头条件反向。

日线 `MA250` 约覆盖一个交易年，通常被市场称为“年线”。这只是日线语境中的约定：周线 `MA250` 是约 250 周，年线 `MA250` 是 250 个年度样本，不能把日线 MA250 的含义搬到其他周期。MA60、MA250 也不是天然支撑；它们只能被定义为待验证的动态支撑/压力候选。

当前动态交互使用 ATR 归一化：

- ATR 以 14 根 Bar 计算；MA20/60 接触缓冲为约 `0.28 × ATR14`，MA120/250 为约 `0.38 × ATR14`。
- 收盘在线上且最低价进入缓冲区时标记 `support-test`；反向标记 `resistance-test`。
- 前后收盘越过均线并超过缓冲时标记 `break`。
- 当前 MA 可视化在 ATR 样本不足时会临时使用价格的 0.5% 作为缓冲基准；这只能维持图层稳定，不能替代正式 ATR 支撑确认。

边界：对应窗口不足时该均线必须为 `null/样本不足`，不能缩短窗口冒充；横盘缠绕、跳空、极端波动和换源后的价格尺度变化都会提高假信号率。当前 MA 已进入逐 Bar evaluator、composite 和固定规则实验回测；尚未通过黄金/白银各周期的 walk-forward 与真实成本验证。

### 4.2 布林带 20 / 2

当前口径固定为：

- `Middle = SMA20(close)`；
- `Upper/Lower = Middle ± 2 × Std20(close)`；
- 当前代码的滚动标准差使用总体口径，即除以 `n`；
- `Bandwidth = (Upper - Lower) / Middle`；位置分数用价格相对半带宽的标准化距离表达。

波动状态使用截至当前最多 120 个可计算带宽值：带宽处于自身历史不高于 20% 分位时为 `squeeze`；相对上一根扩张超过 4%，且带宽分位不低于 45% 时为 `expanding`，否则为 `normal`。当前代码在分位样本少于 20 个时用中性分位 0.5 判断扩张，但不会判断压缩；正式验证应先固定最小预热长度，避免短样本扩张状态与完整历史口径混用。只有“收盘在带外 + 带宽扩张”才产生方向确认；压缩只表示低波动状态，不预测突破方向。

John Bollinger 的官方规则明确指出，单次触轨不是买卖信号，趋势中价格可能沿轨运行，带外收盘初始更偏向延续而非自动反转；20/2 也只是默认参数，不是适用于所有市场和周期的常数。当前计算器、overlay、composite 集合和实验回测已有接线，catalog 已固定当前 20/2、120 Bar 带宽窗和扩张阈值；这仍只表示规则版本固定，生产样本外验证尚未完成。

### 4.3 九转计数仅为 Setup 9

当前实现为可审计的 Setup 计数：

- Buy Setup：连续 9 根 `Close_t < Close_{t-4}`；
- Sell Setup：连续 9 根 `Close_t > Close_{t-4}`；
- 任一比较失败即重置对应连续计数；最少需要 13 根连续 Bar；
- 另有基于第 8/9 根与更早极值关系的简化 `perfected` 提示。
- 方向信号只在第 9 个条件刚完成的那一根 Bar 产生一次；若同方向条件继续到第 10 根以后，UI 保留“已完成、等待翻转重置”的状态，但不会重复发出新的耗竭信号。

它不是完整的 DeMARK Sequential。当前没有 13 Countdown、TDST、Countdown cancellation、Risk Level 等完整阶段，也不复制未公开的专有细节。Setup 9 只提示趋势可能过度延伸；强趋势可以在 9 以后继续运行。

当前状态：作为视觉耗竭提示运行，`compositeEligible=false`、`backtestEligible=false`。在未定义确认、进场、止损和退出规则前，不把“出现 9”直接回测成反转交易。

### 4.4 多尺度时序动量

当前实现避免只押注一个窗口：

- 原始回报窗口为 20、60、120 根 Bar；
- 用最近 20 根对数收益的实现波动率归一化；
- `Scaled_n = tanh((C_t / C_{t-n} - 1) / (RV20 × sqrt(n)))`；
- 权重为 0.5 / 0.3 / 0.2，缺少长窗口时对可用权重重新归一化；
- 至少两个可用窗口同为正且综合分超过 `+0.15`，或至少两个同为负且综合分低于 `-0.15`，才给出方向，其余为分歧/中性。

相关实证研究覆盖含商品期货的多资产样本，支持把时序动量作为待检验的基线，但不能直接推出单一黄金品种、任一周期或当前数据源的胜率。当前计算器和实验合成/回测已有接线；窗口、阈值、成本后稳定性和黄金/白银分层 walk-forward 尚未完成。

## 5. 第二层：智能趋势线与结构标注

智能趋势线使用已经被右侧 2 根 Bar 确认的局部高/低点，因此有天然确认延迟，但不会使用尚未出现的未来触碰来生成锚点。catalog 将用户可解释 horizon 标为最近 80–240 根；当前实现为容纳摆动确认边界，候选扫描上限约 260 根，在支撑线和压力线中分别选择质量最高的有效线，并保留一条近期失效线供审计。

| 状态 | 当前判定 | UI |
| --- | --- | --- |
| `candidate` | 两个有效锚点已形成，但质量低于确认阈值且没有第三次触碰 | 低透明度点线 |
| `confirmed` | 尚无第三次触碰，但由跨度、误差与触碰项组成的质量分不低于 0.48 | 较高透明度实线 |
| `tested` | 除两个锚点外至少出现一次 ATR 容差内的有效触碰 | 高透明度、较粗实线 |
| `invalidated` | 连续两根收盘越过趋势线约 `0.28 × ATR` 的缓冲，或单根收盘穿越超过 `0.75 × ATR` | 灰色低透明度虚线，保留失效原因 |

候选线还受锚点间隔、斜率、穿越和 ATR 误差限制。质量分由时间跨度 25%、额外触碰 50%、误差 25% 组成；它是排序分，不是命中概率。当前趋势线图层、状态标签、失效淡化和截止点测试已实现；`auto-trend` 不进入 composite 或交易回测，避免把后续触碰形成的质量错误地解释为早期可交易信息。

## 6. 第三层：外部市场上下文

### 6.1 VIX 与 GVZ 不是同一个信号

| 指数 | 期权标的 | 描述 | 在本系统中的角色 |
| --- | --- | --- | --- |
| VIX | S&P 500 指数（SPX）期权 | 约 30 天股票市场预期波动率 | 跨市场风险背景 |
| GVZ | SPDR Gold Shares（GLD）期权 | 约 30 天黄金 ETF 预期波动率 | 黄金自身隐含波动背景 |

二者都是隐含波动幅度，不是方向指标。VIX 上升不等于金价必涨；GVZ 上升也只表示黄金预期波动扩大，不说明涨跌方向。VIX 与 GVZ 同升可描述为共同波动扩张，GVZ 相对独升可描述为黄金特异波动，但这些标签只能用于风险解释或后续条件收益研究。

当前 `GET /api/expert/context/volatility` 只读取 Cboe 官方 EOD 历史 CSV：分别给出 VIX/GVZ 最新已发布日值、交易日 `as_of`、包含最新值在内最多 252 个已发布日值的经验分位和样本区间。CSV 不提供精确发布时间，因此响应用 `received_at` 记录本服务获取时间。该 endpoint 不注册为可选择行情源，不自动轮询 delayed quote CDN；当前也没有 60 日 z-score、标准化 `GVZ - VIX` spread、composite 或回测。

### 6.2 沪金/沪银量价持仓

必须区分两类数据：

1. `AU8888` / `AG8888` 是供应商发布的沪金/沪银加权价格观察值。应用不自行猜测权重或换月公式，它们也不是具体可交易月份。
2. SHFE 延迟市场数据按真实月份合约提供 `last_price`、`volume`、`open_interest` 以及可能存在的 `open_interest_change`。当前 provider 只接受 AU/AG 的真实月份合约，排除聚合行，再汇总合约数量、成交量和持仓量。

当前 `GET /api/expert/context/shfe-positioning/{product_code}` 支持 `au/ag`，按请求读取官方 `delaymarket_au/ag.dat` 并缓存 60 秒。响应保留：`observed_at`、`received_at`、源 URL、声明延迟 30 分钟、单位 `lots`、`counting_method=single_side` 和 `directional_inference=unavailable`。该 provider 只作为 endpoint 生命周期组件，不注册为可选择行情源或自动轮询源。`open_interest_change` 只有在所有纳入合约都提供该字段时才汇总；部分合约缺失时总变化必须为 `null`，并用 `open_interest_change_contracts` 报告覆盖数，绝不把缺失当 0。

量仓解释仅在同合约、同交易日与同 as-of 下成立：

- 价涨 + 增仓：可标记“上涨伴随新增参与”，但不能由总 OI 识别是谁净做多；
- 价跌 + 增仓：可标记“下跌伴随新增参与”，同样不是持仓方向分解；
- 价涨/价跌 + 减仓：优先描述为持仓退出或去杠杆背景，不能与增仓趋势确认等强处理；
- 异常成交量提高事件显著性，但不单独决定方向。

当前页面把两项观察并列展示，但不融合：一项是 SHFE 真实 AU/AG 合约的延迟总成交量/OI 快照；另一项是当前加权 K 线源自身的 24-Bar `volume-price` 条件 evaluator。后者把最近有效成交量 Bar 分成前后两个窗口，比较价格方向与平均成交量变化；至少需要 12 根正成交量 Bar，量变化绝对比例低于 4% 时按中性带处理。它只解释该 K 线源的量价关系，不读取 SHFE OI，也不能冒充量仓四象限。

当前不能把加权价格与 SHFE 合约汇总量仓直接拼成因果四象限交易策略：两者来源、时间粒度和构造口径不同，而且还没有逐时点历史权重/成分、连续合约换月元数据和可审计历史快照。现阶段 SHFE 数据只属于延迟市场参与度上下文，不进入 composite 或回测；24-Bar `volume-price` 仅在当前 K 线确有可比较 volume 时按 catalog 的条件规则独立贡献实验分数。

### 6.3 换月与加权序列边界

对月份合约的 OI 下降，可能只是资金迁移到远月；对主力连续或加权序列，换月也会造成价格、成交量和 OI 的结构跳变。生产量仓策略至少需要：

- 每个观测时点的成分合约、权重和可交易主力合约；
- 明确的换月判定、切换生效时刻和交易成本；
- 夜盘归属到 SHFE 下一交易日的统一日历；
- 同合约比较，或显式构造且版本化的连续序列；
- `roll_event` 标记，换月窗口内暂停普通 ΔOI 四象限解释。

缺少任一项时，只展示各真实合约及全市场参与度汇总，不生成连续合约方向信号。

## 7. 缺字段与过期数据的降级规则

| 条件 | 必须降级为 | 禁止行为 |
| --- | --- | --- |
| OHLC 非有限、次序非法或 Bar 缺口未解释 | 跳过该 Bar；相关指标显示 unavailable | 修补成看似连续的价格 |
| MA/BOLL/动量窗口不足 | 对应值 `null/insufficient`；只展示真实可用窗口 | 缩短窗口后仍标原参数 |
| 最新 Bar 不是 `final` | 价格与实时指标可预览；确认策略、趋势锚点和回测停在最后一根 `final` Bar | 把未收盘柱写成已确认信号，或把最终修订历史冒充当时可得历史 |
| ATR14 不足 | 当前图层可用明确标记的显示回退；正式支撑/趋势线确认降级 | 把价格百分比回退标为真实 ATR |
| `volume` 缺失或不可比较 | `volume-price=unavailable`；POC≈/FLOW≈若等权退化必须保留代理标识 | 把 tick volume、交易所手数和无量现货混用 |
| `open_interest` 缺失 | 量仓上下文 unavailable | 从成交量反推 OI |
| 仅部分合约有 `open_interest_change` | 汇总 ΔOI 为 `null`，同时显示覆盖合约数 | 缺失补 0 或给出四象限方向 |
| VIX 或 GVZ 任一读取失败 | 整组 EOD 上下文显示 unavailable，或保留上一组值及其原 as-of；不计算 spread | 前向填充为“实时”或由另一个指数替代 |
| 合约成分/换月元数据缺失 | 量仓只作快照上下文 | 对连续/加权序列回测 ΔOI 策略 |
| 任一外部源失败 | 显示 stale/unavailable，保留最后有效值及其原 as-of | 静默切换来源或更新时间戳 |

## 8. 回测与研究控制

### 8.1 当前实验回测是什么

当前前端实验回测与实时信号共用逐 Bar evaluator，按信号柱收盘换仓，并计入 0.02% 单边固定摩擦。交易数和胜率只统计已平仓持仓；期末持仓按末价计入收益，但不计入胜负。它尚未包含真实点差、市场冲击、动态滑点、手续费档位、换月、涨跌停、夜盘可成交性和 as-of 修订快照。

同一信号柱收盘成交是一项偏乐观假设。生产研究至少应把执行推迟到下一可成交报价/Bar，并分别报告理想收盘、下一开盘和带点差滑点三种结果。

### 8.2 Walk-forward

禁止随机打乱时间序列。每个品种、周期和数据源应按时间执行滚动或扩展窗口：

1. 训练窗口只用于选择有限参数集和阈值；
2. 参数冻结后，在紧邻且完全未见的验证/测试窗口运行；
3. 窗口向前滚动，保留每个窗口的正负结果，不只汇报最佳区间；
4. 阈值、成本假设或策略组合再次调优时，需要新的外层样本外窗口；
5. AU/AG、现货金、不同分钟/日/周周期分别报告，不能合并成一个“黄金胜率”。

### 8.3 成本和基准

净结果至少扣除双边手续费、买卖价差、与成交量/波动相关的滑点、换月价差和货币换算成本。与简单基准比较：不持仓、买入持有、单一长期动量或同风险波动目标；报告净收益、最大回撤、换手、暴露、交易数和各市场状态结果，而不是只报告胜率。

### 8.4 数据窥探控制

- 在测试集前冻结策略版本、参数搜索空间、缺失值规则和成本模型。
- 保存所有试验而非只保存最好结果；同一数据上尝试大量 MA、带宽、趋势线或组合阈值时，使用 Reality Check、Deflated Sharpe Ratio 或等价的多重检验控制。
- 训练期计算的分位数、标准化参数和合约权重只能向后应用，不能用全样本统计量。
- 企业/供应商历史数据修订、幸存者式品种选择和只保留成功策略都要进入审计记录。
- “先进”应指更严格的因果特征、校准和验证流程，而不是更复杂的公式。多尺度时序动量可作为研究基线；真实利率、美元和波动上下文只能在可审计 as-of 数据到位后作为条件变量研究，且其关系具有状态依赖性。

## 9. 当前实现与尚未实现

| 能力 | 当前工作树 | 尚未实现/未完成 |
| --- | --- | --- |
| 策略目录与详情 | 17 个策略定义、角色、公式、字段、边界、失效、参考和 eligibility；详情 UI 已接线 | 将版本、验证报告和数据集摘要持久化 |
| RSI14 | Wilder 递推、30/70 状态、极端区回收事件、图层与实验 evaluator/backtest | 黄金分周期 walk-forward、成本与阈值稳定性报告；背离尚未实现 |
| W/M/2B | 因果摆动、颈线/失败突破确认、失效保留与特殊结构印记 | 2B/sweep 事件簇去重和黄金分周期样本外统计 |
| 跨周期趋势 | 1h/1d/1w final-only as-of 摘要、aligned/divergent/not-comparable 与机会候选 | 多序列历史复演和组合回测；当前有意不进 composite/backtest |
| 流动性与结构代理 | OHLC Sweep/BOS/CHOCH、失效淡化及 8 Bar 同向共振 | 真实订单流接入、代理效果验证与事件簇去重；当前有意不进 composite/backtest |
| MA20/60/120/250 | 计算、图层、排列、ATR 交互、逐 Bar evaluator 与实验回测已接线 | 分周期 walk-forward、真实交易成本、完整/部分排列 UI 强区分 |
| BOLL 20/2 | 计算、图层、压缩/扩张状态、实验 composite/backtest 接线已存在 | 冻结训练窗分位、样本外阈值和生产验证 |
| Setup 9 | 连续计数和简化 perfected 视觉提示 | 完整 Sequential；当前明确不计划用不完整实现冒充完整指标 |
| 多尺度动量 | 20/60/120 波动归一化得分和实验接线 | 黄金/白银分层样本外与参数稳定性报告 |
| 智能趋势线 | 因果摆动锚点、四状态、失效原因、淡化 UI 和截止点测试 | 进入方向合成/交易回测；当前有意禁止 |
| VIX/GVZ | `/api/expert/context/volatility` 只读 Cboe EOD CSV 的最新已发布值、as-of 与含最新值的最多 252 日经验分位 | 自动 delayed quote、60 日 z-score/spread、跨时区条件研究、composite/backtest |
| SHFE 量仓 | `/api/expert/context/shfe-positioning/{au,ag}` 按请求读取真实合约延迟快照，聚合并保护缺失 ΔOI；与加权 K 线 24-Bar 量价观察并列展示 | 二者的因果历史融合、四象限方向、换月元数据、连续序列和量仓回测 |
| 期货自动交易 | 无 | 可交易合约选择、订单执行、风控、交易所约束、换月与对账全链路 |

## 10. RSI、反转形态、跨周期与市场结构代理

> 本节同时记录已经落地的机械规则和仍处于研究阶段的扩展。每一小节的“当前状态”才是准入依据；研究建议不能视为已实现功能。

本节所有新增阈值均是工程起点，必须先冻结策略版本和参数，再按第 8 节做 walk-forward、交易成本和数据窥探控制；不得在同一评估样本上反复调参后报告为样本外结果。

### 10.1 证据分级与命名纪律

| 级别 | 本节对象 | 可以声称什么 | 不能声称什么 |
| --- | --- | --- | --- |
| 公式或原作者规则 | Wilder RSI、Sperandeo 2B | 公式/原规则有可追溯出处，可以机械复现 | 原作者的定性评价不是本系统的黄金胜率 |
| 学术直接或邻近证据 | 自动形态识别、公开支撑压力、止损单聚集、真实订单簿 OFI、时序动量 | 这些现象在论文所研究的市场、时期和数据上有统计结果 | 不能直接外推到黄金、当前周期或 OHLC 代理 |
| 行业术语 | BOS、CHOCH、liquidity sweep、FVG、order block、“聪明钱” | 可以给出本系统自己的可证伪机械定义 | 不能据 OHLC 断言机构持仓、隐藏订单、止损位置或操纵行为 |

本轮未找到对 BOS/CHOCH、ICT/SMC FVG 或 order block 给出统一定义并直接验证黄金样本外预测力的一手同行评审研究。因此代码和 UI 必须使用 `structure-proxy`、`liquidity-sweep-proxy`、`order-block-proxy` 等名称；学术上的订单流和止损聚集研究只作为边界依据，不能替行业叙事背书。

### 10.2 Wilder RSI14（已实现 `rsi-wilder-14-v1`）

[TA-Lib 官方 RSI 说明](https://ta-lib.org/functions/rsi.html)给出 Wilder 平滑、默认 14 期和 70/30 常用阈值。建议严格固定以下口径：

`U_t = max(C_t - C_{t-1}, 0)`，`D_t = max(C_{t-1} - C_t, 0)`

第一个平均涨/跌幅使用 14 个变化量的简单平均，之后递推：

`AvgU_t = (13 × AvgU_{t-1} + U_t) / 14`，`AvgD_t = (13 × AvgD_{t-1} + D_t) / 14`

`RSI_t = 100 × AvgU_t / (AvgU_t + AvgD_t)`

| 项目 | 当前实现 |
| --- | --- |
| 数据 | 同一品种、同一周期至少 15 根连续且已完成的 close |
| 原始状态 | `RSI > 70` 为高位动量/过热上下文，`RSI < 30` 为低位动量/过冷上下文；两者都不是反转指令 |
| 因果候选 | 上一根 `RSI <= 30`、当前收回 30 上方时偏多；上一根 `RSI >= 70`、当前回落 70 下方时偏空 |
| 极端钝化 | RSI 仍在 30 以下或 70 以上只显示状态，方向置信度为 0，不把“超卖/超买”直接计为反转 |
| 失效 | 回收后重新进入原极端区且价格沿原方向延续，或同周期价格结构给出反向确认 |
| 降级 | `AvgU=AvgD=0` 时按系统约定输出中性 50，并标记 flat-input；只有一侧为 0 时分别输出 100 或 0 |

参数 14 和 70/30 是待 walk-forward 的预注册起点，不是黄金最优参数。强趋势中 RSI 可以长时间处于极端区；阈值回收是低置信度动量候选，不能解释为反转概率。RSI 背离和“价格突破摆动位后二次确认”仍是研究项，当前详情页明确不声称已经实现。

当前状态：Wilder 递推、种子预热、flat-input、历史修订、RSI 图层和 strategy catalog 已实现；阈值回收进入逐 Bar evaluator、composite 与实验回测。尚缺黄金分品种/周期 walk-forward、交易成本和参数稳定区间报告。

### 10.3 W 底 / M 顶（已实现于 `structure-wm-2b-v2`）

[Lo、Mamaysky 与 Wang](https://www.nber.org/papers/w7613)表明主观图形可以被算法化并比较条件收益分布，但其样本是美国股票，不是黄金；论文结论也不等于下面工程阈值已被验证。

当前实现把 W/M 作为对称的因果事件，并与旧版仅 M 顶口径区分版本：

| 项目 | W 底 | M 顶 |
| --- | --- | --- |
| 结构 | 三个连续确认摆动 `L1 - N - L2` | 三个连续确认摆动 `H1 - N - H2` |
| 摆动确认 | 半径 2，即一个极值只有在右侧 2 根 Bar 完成后才可用 | 同左 |
| 两端相似 | 差值不超过 `max(0.4% × 两端均价, 0.5 ATR14)` | 同左 |
| 间隔 | 两端相隔 5–80 Bar，整个扫描窗口最多 160 Bar | 同左 |
| 最小深度 | `N - mean(L1,L2) >= 1.0 ATR14` | `mean(H1,H2) - N >= 1.0 ATR14` |
| 因果确认 | 第二低点已完成右侧确认后的 40 Bar 内，收盘 `> N + 0.05 ATR14` | 第二高点已完成右侧确认后的 40 Bar 内，收盘 `< N - 0.05 ATR14` |
| 失效 | 确认前先收盘越过失效位则候选作废；确认后收盘 `< min(L1,L2) - 0.25 ATR14` 则淡化 | 确认前镜像作废；确认后收盘 `> max(H1,H2) + 0.25 ATR14` 则淡化 |

形态高度只能解释结构规模，不是止盈概率。当前只创建 `confirmed` 和 `invalidated` 事件，不展示 forming 猜测；相同锚点和确认柱按稳定 ID 去重，失效后保留原锚点并淡化，不得回看式删除。当前版本不强制先验趋势，避免把未实现条件写进详情页；若将来加入趋势门控必须另升版本。volume 若存在且口径稳定，可作为独立确认字段，不能把现货缺失量补成期货量。

当前状态：W/M 检测、确认/失效生命周期、图层特殊印记、因果截止和逐 Bar evaluator 已实现，并可进入当前实验回测。正式统计必须保留策略版本，不能用 v2 阈值覆盖旧 `structure-v1` 结果。

### 10.4 Victor Sperandeo 2B 失败突破（已实现于 `structure-wm-2b-v2`）

Sperandeo 在 Wiley 的[《Trader Vic on Commodities》第 4 章](https://onlinelibrary.wiley.com/doi/10.1002/9781119196792.ch4)将 2B 描述为：上升趋势创新高但不能延续，随后价格回到前高下方；下降趋势镜像处理。原文说明它可用于短、中、长期图表，但文案中的概率评价不能继承为本系统结论。

为消除“不能延续”“随后”的主观性，当前使用以下机械版本：

1. 参考位必须是半径 2 已确认的前摆动高 `H1` 或前摆动低 `L1`；当前版本不额外要求此前趋势方向。
2. 在参考摆动完成右侧确认后的最多 30 根 Bar 中，寻找越过旧极值 `0.05–1.25 ATR14` 的探测；过小视为噪声，过大不再归为本 2B 版本。
3. 看空 2B：探测 Bar 的 high 高于 `H1`，并在探测当根或随后最多 3 根已完成 Bar 内出现 close `< H1 - 0.05 ATR14`；看多镜像处理。
4. 回收柱收盘就是本版本的因果确认，事件只在该柱完成后产生；若回收前先收盘越过探测极值外 `0.25 ATR14`，候选立即作废。确认后再越过该失效位则标记 `invalidated` 并保留印记。
5. 穿越后 3 根内没有回收，或参考摆动后 30 根内没有合格探测，不创建形态事件；UI 不提前显示会产生幸存者偏差的“forming 2B”。

数据需求：连续完成 OHLC、ATR14、已确认 swing 和周期 ID。当前没有 tick size 输入，因此不猜测最小报价单位。volume/OI 不是 2B 原规则的必要字段，只能作为另一个来源一致时的旁证。2B 与 liquidity sweep proxy 可能描述同一组 Bar；在事件簇去重完成前，smart-money 代理不进入 composite，从制度上避免重复加分。

当前状态：2B 顶/底、确认与失效状态、图层印记、因果截止和逐 Bar实验回测已实现。原始定义有一手出处，但 ATR 阈值与窗口是工程默认值，尚无黄金样本外效果统计。

### 10.5 长期与短期周期差异（已实现 `multi-timeframe-trend-v1`）

[Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)在含商品期货的多资产月频研究中发现 1–12 个月收益延续、再长周期部分反转；论文还提示更高频结果可能受市场微观结构影响。它支持“期限必须分开验证”，不支持把月频结论直接套到 1 分钟黄金。

当前 profile 固定为 `swing-1h-1d-1w-v1`：短周期 1h、中周期 1d、长周期 1w，每档都要求 20 根合格 final Bar。

| 状态 | 机械定义 | UI/策略含义 |
| --- | --- | --- |
| `up` | `Close > SMA5 > SMA20` 且 20-Bar return > 0 | 当前档趋势摘要向上，不是上涨概率 |
| `down` | `Close < SMA5 < SMA20` 且 20-Bar return < 0 | 当前档趋势摘要向下，不是下跌概率 |
| `mixed` | 有完整样本但不满足 up/down | 不强行归类 |
| `aligned` | 1h/1d/1w 全为 up 或全为 down | 同向背景，只作风险上下文 |
| `divergent` | 三档中同时存在 up 和 down | 展示周期张力及差异对 |
| `not_comparable` | 任一档少于 20 根合格 Bar 或不可用 | 不生成跨周期结论 |

因果连接规则：

- 三档分别读取同一 `instrument_symbol`、同一 `source_id`。共同 `decision_as_of` 只纳入 `state=final`，且 `bucket_end`、`finalized_at`、`observed_at`、`received_at` 都不晚于截止时刻的 Bar；正在形成的日/周 Bar 不会提前用于低周期判断。
- 长周期 up、短周期 down 时只标记“多头回撤候选”；长周期 down、短周期 up 时只标记“空头反弹候选”。候选必须等待短周期 RSI 回收、W/M、2B 或 BOS/CHOCH 确认，不能直接下单。
- 高周期与短周期同向时显示 aligned 背景；mixed 或样本不足不被强行加权。跨周期上下文当前完全不进入方向 composite。
- 每档最后一根 Bar 自然结束于不同时间；`decision_as_of` 是共同信息边界，不声称三个 bucket 同时结束。
- 服务不保存每一次历史修订快照。若修订在 `decision_as_of` 之后才收到，会保守排除而不是假装重建当时版本。

当前状态：final-only as-of join、1h/1d/1w 趋势摘要、差异明细、不可比降级、API 和“周期张力”UI 已实现。它是候选优势上下文，不是入场保证；现有单序列回测不能复演三档历史，故 `compositeEligible=false`、`backtestEligible=false`。

### 10.6 “聪明钱”概念的可证伪代理

#### 10.6.1 已实现的 BOS 与 CHOCH 代理

当前 `smart-money-structure-proxy-v1` 以 swing close-break 统一术语，不采用无法审计的肉眼结构：

- swing 使用半径 2，并在右侧 2 根完成后确认；扫描窗口最多 220 Bar。
- `bullish structure`：最近两个确认 swing high 和 swing low 都抬高；`bearish structure` 镜像，否则为 `mixed`。当前版本不使用 protected high/low 的行业流派定义。
- 前一收盘没有越过阈值、当前收盘首次超过最近摆动高 `+0.12 ATR14` 时生成 bullish break；跌破最近摆动低 `-0.12 ATR14` 时镜像。
- 突破前若处于相反结构，事件标为 `CHOCH`；否则标为 `BOS`。CHOCH 只描述机械结构切换，不证明趋势一定反转。
- 事件后若多方收盘低于参考位 `-0.50 ATR14`，或空方收盘高于参考位 `+0.50 ATR14`，状态改为 `invalidated`；原锚点保留并在图上淡化。

数据需求是连续完成 OHLC、ATR14 和 Bar revision。BOS/CHOCH 已实现并展示，但单独事件不进入 composite；只有与同方向 sweep proxy 在规定窗口内共振才产生候选，整个策略目前仍禁止计分和回测交易。

#### 10.6.2 已实现的 liquidity sweep proxy

[纽约联储关于止损单与价格瀑布的研究](https://www.newyorkfed.org/research/staff_reports/sr150.html)在高频外汇数据中发现止损单会在特定价位聚集并可能放大短期价格运动；[公开支撑压力研究](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf)也只验证了特定外汇样本中的趋势中断。两者都没有证明某根黄金影线就是“机构扫流动性”。

当前 OHLC 回收代理为：

1. 参考位只使用半径 2 已确认的最近 swing high/low。
2. 影线必须越过参考位 `0.08–1.50 ATR14`；过小视为噪声，过大不归为本版本 sweep。
3. 穿越当根必须收盘回到参考位内侧至少 `0.02 ATR14`，随即创建 `SWEEP` 结构印记。
4. 单独 sweep 不产生策略方向候选；只有同方向仍有效 sweep 后最多 8 根 Bar 内，最新 Bar 再确认 BOS/CHOCH 才产生低置信度共振。
5. 事件后若多方收盘跌破参考位 `-0.50 ATR14`、空方收盘突破参考位 `+0.50 ATR14`，标记 `invalidated` 并保留历史。

只有 OHLC 时 UI 和 strategy catalog 强制写“流动性/扫位代理”，不能写“已扫止损”“机构进场”或“获取流动性”。若未来取得 L2/order message、主动买卖方向和盘口深度，真实 liquidity/OFI 分析必须作为另一个 native evidence 接入，不能回填旧代理历史。当前 2B 与 sweep 可能覆盖相近 Bar，未完成事件簇去重前 `smart-money` 保持 `compositeEligible=false`、`backtestEligible=false`。

#### 10.6.3 FVG 的边界与状态（计划版本 `fvg-state-v2`）

当前 `fair-value` 只实现三 Bar OHLC 缺口：bullish 为 `Low_t > High_{t-2}`，bearish 为 `High_t < Low_{t-2}`；它已明确不是订单簿不平衡，也不保证回补。

若作为 SMC 结构代理扩展，建议保留原始 FVG，同时新增而不覆盖 v2：

- 第三根 Bar 完成后才创建区域；bullish 区域为 `[High_{t-2}, Low_t]`，bearish 镜像。
- `displacement-qualified` 另要求中间 Bar 实体 `>= 1.0 ATR14` 且 gap 宽度 `>= 0.1 ATR14`；阈值是工程默认值。
- 状态为 `open -> tested -> fully-filled`：后续价格首次进入区域为 tested，触达远端边界为 fully-filled。fill 只描述覆盖，不等于反转或盈利。
- 原结构 swing 被收盘有效突破或出现反向 BOS 时为 `invalidated`；状态和首次发生时刻永久保留。
- 进场还需区域内产生拒绝收盘和同向低周期 BOS，不允许“见 gap 即挂单”。

[Cont、Kukanov 与 Stoikov](https://doi.org/10.1093/jjfinec/nbt003)研究的是真实限价簿事件和 OFI，且样本为美国股票；其结果不能用来证明 OHLC FVG 是真实供需失衡或必然回补。

#### 10.6.4 Order block proxy（计划版本 `order-block-proxy-v1`）

“最后一根反向 K 线就是机构订单区”不可由 OHLC 证实。可实现版本只能定义为 BOS 前置 K 线区域：

- 只有出现 `displacement-qualified` 且最终造成 BOS 时才搜索；在 BOS 前最多 5 根 Bar 中选择最后一根反向实体 K 线。
- 区域固定为该 Bar 的完整 `[low, high]`，不得在事后从实体/影线中挑表现最好的边界。
- BOS 收盘后才创建 `fresh` 区域；后续首次价格重叠为 `tested`。
- 必须在区域内出现拒绝收盘并随后同向 BOS，才成为方向候选；单次触区不产生交易。
- 多头区域若收盘 `< low - 0.1 ATR14`、空头区域若收盘 `> high + 0.1 ATR14`，或出现反向 BOS，则标记 `invalidated`；100 根 Bar 未测试则标记 `expired`。
- 同一 displacement 同时生成 FVG 与 order-block proxy 时属于一个事件簇，composite 最多计一次结构证据。

真实 order block/机构行为需要订单簿、订单所有者或至少可审计的订单流数据。当前系统没有这些字段，也没有 order-block proxy 实现。

### 10.7 增量实现状态与准入顺序

| 项目 | 当前状态 | 首次允许进入 composite/backtest 的前提 |
| --- | --- | --- |
| RSI14 | 已实现计算、图层、evaluator 与实验回测 | 已满足公式/flat-input/无未来函数基础测试；正式准入仍需黄金 walk-forward 与成本报告 |
| W/M | v2 已实现对称确认、失效与特殊印记 | 当前可进实验 composite/backtest；正式准入需分周期样本外、去重和成本统计 |
| 2B | 已实现参考摆动、穿越/回收与失效 | 当前可进实验 composite/backtest；正式准入需黄金样本外与 2B/sweep 事件簇去重统计 |
| 跨周期上下文 | 已实现 final-only as-of join、1h/1d/1w 摘要与冲突降级 | 多序列历史复演、交易日历/换月验证完成前不进 composite/backtest |
| BOS/CHOCH | 已实现 OHLC close-break 代理与失效淡化 | protected swing 等扩展需另升版本；黄金样本外完成前不计分 |
| sweep proxy | 已实现单柱回收代理，并要求 8 Bar 内同向 BOS/CHOCH 共振 | UI 已强制 proxy；与 2B 去重、真实订单流边界与样本外完成前不计分/回测 |
| FVG | v1 原始三 Bar 区域已实现 | v2 displacement 与区域生命周期独立版本化；不能用 OFI 论文冒充验证 |
| order-block proxy | 未实现 | BOS 后因果创建、固定边界、反应确认、过期/失效及事件簇去重 |

已完成顺序为：RSI 公式与图层 → W/M 和 2B 因果事件 → BOS/CHOCH 与 sweep 代理 → final-only 多周期上下文。下一阶段应优先做 2B/sweep 事件簇去重、多序列历史复演和黄金分周期 walk-forward，而不是继续增加未经验证的形态。任何策略只有精确进出场、成本和样本外检验完成后，才能从“实验回测”升级为正式交易准入。

## 11. 一手/权威依据

- Cboe，[VIX FAQ：SPX 期权隐含的恒定 30 日预期波动](https://www.cboe.com/tradable_products/vix/faqs)。
- Cboe Global Indices，[VIX/GVZ 等波动率指数方法论](https://cdn.cboe.com/api/global/us_indices/governance/Volatility_Index_Methodology_Selected_Broad_Based_Index_Equity_and_ETF_Volatility_Indices.pdf)。
- Cboe 官方历史文件：[VIX History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv)、[GVZ History CSV](https://cdn.cboe.com/api/global/us_indices/daily_prices/GVZ_History.csv)。
- John Bollinger，[Bollinger Band Rules](https://www.bollingerbands.com/bollinger-band-rules)。
- DeMARK Analytics，[9-13：Setup 9、Perfected Setup 与 Countdown 13 的阶段边界](https://demark.com/9-13/)。
- Moskowitz, Ooi, Pedersen，[Time Series Momentum（作者稿）](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)。
- Lo, Mamaysky, Wang，[Foundations of Technical Analysis](https://www.nber.org/papers/w7613)。
- TA-Lib，[Wilder Relative Strength Index：公式、14 期默认值与实现入口](https://ta-lib.org/functions/rsi.html)。
- Victor Sperandeo / Wiley，[2B or Not 2B: A Classic Rule Revisited](https://onlinelibrary.wiley.com/doi/10.1002/9781119196792.ch4)。
- Federal Reserve Bank of New York / Carol Osler，[Support for Resistance: Technical Analysis and Intraday Exchange Rates](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf)。
- Federal Reserve Bank of New York / Carol Osler，[Stop-Loss Orders and Price Cascades in Currency Markets](https://www.newyorkfed.org/research/staff_reports/sr150.html)。
- Cont, Kukanov, Stoikov，[The Price Impact of Order Book Events](https://doi.org/10.1093/jjfinec/nbt003)。
- Brock, Lakonishok, LeBaron，[Simple Technical Trading Rules and the Stochastic Properties of Stock Returns](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1992.tb04681.x)。
- Sullivan, Timmermann, White，[Data-Snooping, Technical Trading Rule Performance, and the Bootstrap](https://doi.org/10.1111/0022-1082.00163)。
- Bailey 与 López de Prado，[The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)。
- 上海期货交易所，[第二代行情发布平台接口规范：延时与单双边统计模式](https://www.shfe.com.cn/upload/20181207/1544168978740.pdf)。
- 上海期货交易所官方延迟数据：[沪金 AU](https://www.shfe.com.cn/data/tradedata/future/delaymarket/delaymarket_au.dat)、[沪银 AG](https://www.shfe.com.cn/data/tradedata/future/delaymarket/delaymarket_ag.dat)。
- CFTC，[Commitments of Traders Explanatory Notes：持仓量定义及报告边界](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ExplanatoryNotes/index.htm)。
- IMF，[Gold Returns in the Presence of Real Yields and Dollar Regimes（跨市场关系的状态依赖研究）](https://www.elibrary.imf.org/view/journals/068/2026/007/article-A001-en.xml)。

这些资料用于定义指标、数据边界和验证方法，不构成当前系统在黄金上的预测率证据。任何策略详情页引用都应同时显示“研究对象是否为黄金”“是否为样本外结果”“当前实现与原方法的差异”。
