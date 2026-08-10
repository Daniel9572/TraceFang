# 黄金重要数据与事件系统

## 目标与边界

黄金事件是独立业务事实，不属于任何图层。事件显示、资金主导、回放、回测和 AI
都只能读取同一份事实快照；关闭“数据/事件”图层只隐藏标记，不能删除事实，也不能改变
资金主导策略对美国 08:30 ET 重要数据日的判断。

当前 `gold-events-v1` 是经过来源核验的事件骨架和 2026 官方日历，不声称穷尽历史。
它不启用已经冻结的 Jin10 MCP 日历，也不把金融机构价格预测写成事实。新增动态来源必须通过
独立适配器进入同一契约。

## 分类目录

| 基线级别 | 类型 | 主要渠道 |
|---|---|---|
| S+ | FOMC 声明/SEP、主席发布会、紧急政策 | 实际利率、美元、风险、流动性 |
| S+ | 美国 CPI、就业形势报告 | 实际利率、美元、风险 |
| S+ | 战争升级、储备冻结、系统性金融危机 | 避险、美元、流动性、央行配置 |
| S | PCE、零售销售、ISM、GDP 初值 | 实际利率、美元、风险偏好 |
| S | 主要央行决议、央行购售金、黄金 ETF 流 | 全球利率、官方/投资资金流 |
| S | 主权信用风险、交割和流动性异常 | 避险、美元、市场结构 |
| A | PPI/ECI、JOLTS/ADP/初请、住房/工业数据 | 条件性宏观重定价 |
| A | 欧元区宏观、中国官方/实物需求、印度政策 | 汇率和实物需求 |
| A | COT/OI/期权偏度、供给与市场制度变化 | 仓位、流动性、供需 |

基线级别只决定公布前的关注优先级，不是永久影响分。公布后必须使用真实意外和市场证据
重新评分。

## 权威来源映射

- FOMC：[Federal Reserve FOMC calendars](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
- CPI、就业、PPI、JOLTS、ECI：[U.S. BLS release calendar](https://www.bls.gov/schedule/2026/home.htm)
- GDP、PCE 和零售等：[U.S. BEA release schedule](https://www.bea.gov/news/schedule/full)
- ISM：[ISM Report On Business calendar](https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/)
- 期货仓位：[CFTC Commitments of Traders](https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm)
- 黄金 ETF：[World Gold Council ETF flows](https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows)
- 央行购金：[World Gold Council central-bank demand](https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2024/central-banks)
- 制裁和储备配置：[IMF Gold as International Reserves](https://www.elibrary.imf.org/abstract/journals/001/2023/014/article-A001-en.xml)

来源优先顺序为：官方统计机构 > 央行/交易所 > IMF/WGC 等研究机构 > 金融机构研报 >
新闻或人工核验。低一级来源不能静默覆盖高一级事实，修订必须形成新版本。

主要金融机构研报只用于验证传导渠道和研究优先级：Goldman Sachs 强调央行和边际实物
需求，[J.P. Morgan](https://www.jpmorgan.com/insights/markets-and-economy/markets/is-it-a-golden-era-for-gold)
强调实际利率、美元、供需和情绪，[UBS](https://www.ubs.com/global/en/wealthmanagement/insights/marketnews/article.1893443.html)
强调降息、美元和央行配置。它们的价格目标、线性弹性和主观方向不能进入事实字段，也不能
覆盖事件公布后的真实评分。[Goldman Sachs 研究入口](https://www.goldmansachs.com/insights/articles/gold-predicted-to-climb-higher-than-expected-as-records-shatter)

## 事实时间语义

每条事实分别保存：

- `scheduled_at`：计划公布时间；
- `released_at`：实际公布时间；
- `effective_period_start/end`：数据实际描述或资金流发生的周期；
- `source_published_at`：该版本信息首次对市场可得的时间；
- `ingested_at`：本系统采集时间；
- `revision_vintage`：初值或修订版本；
- `release_cluster_id`：同一时点或同一政策事件簇。

央行“5 月购金”在 7 月公布时，5 月是有效期，7 月是市场可得时间。回测只能在 7 月以后
使用该事实。FOMC 声明和 SEP 记录在 14:00 ET，主席发布会在 14:30 ET 单独记录，二者
共享事件簇但分别评估增量行情。多个 08:30 ET 数据共享 `release_cluster_id`，不能把全部
价格变化归因给其中一个数据。

## 双评分

### 短期冲击分 ShockScore

| 证据 | 权重 |
|---|---:|
| 1 分钟、5 分钟、30 分钟、2 小时异常波动 | 35% |
| 实际值相对预期的标准化意外及修订 | 20% |
| 成交活跃、点差、滑点和流动性变化 | 20% |
| 美元、实际利率、OIS、VIX/信用利差确认 | 15% |
| 现货、COMEX、ETF、伦敦/纽约/上海广度 | 10% |

### 中长期定价分 RegimeScore

| 证据 | 权重 |
|---|---:|
| 4 小时、1 日、5 日、20 日持续性 | 30% |
| ETF、央行吨数、COT/OI 和实物溢价资金流 | 25% |
| 美联储路径、实际利率曲线和美元制度重定价 | 20% |
| 同类事件跨市场环境的历史稳定性 | 15% |
| 制裁、战争和制度改变的不可逆程度 | 10% |

分值按当前已取得证据重新归一化，同时强制显示覆盖率。例如“冲击 88 / 覆盖 35%”表示
黄金价格反应强，但还没有美元、利率、流动性和跨市场证据，不能解释为完整高置信度结论。
成交量只属于活跃度，不属于净资金流。只有 ETF 创建赎回、央行吨数、COT/OI、实物溢价
等来源才能进入资金导向证据。

当前前端评分使用事件前同一 UTC 时点的滚动历史作为稳健基线，采用中位数和 MAD，且只读取
在 `evaluated_at` 前已经完成或已经观测到的 Bar。后续接入纽约本地时段基线时，不得改变
原始事件或 K 线，只能升级评分证据版本。

## 回放和防未来数据

- `source_published_at > replay_cutoff` 的事实不可见，也不可参与资金主导判断；
- 事件后的 30 分钟窗口只有在窗口完整结束后才能评分；
- 修订值只能从修订发布时间之后使用；
- 央行资金流按公布时间进入策略，不能按有效期回填为当时已知；
- 未来计划事件可以在其官方日历已发布后进入预告，但不能携带未来实际值；
- 同一事件簇的价格反应必须标明“不可单因子归因”。

## 当前能力状态

已完成：

- 后端事件类型目录、来源优先级和事实 API；
- 1999 年以来代表性结构/风险事件及 2026 CPI、非农、FOMC 骨架；
- FOMC 声明、发布会和纪要的阶段拆分；
- 事件显示与资金主导策略独立；
- 回放可得时间过滤；
- 基于当前黄金 Bar 的短期/持续性局部评分和证据覆盖率；
- 普通行情和专家模式复用同一事件事实与图层。

尚未伪装为已接通：

- 实时美元指数、实际利率/OIS 和风险资产确认；
- ETF 日流、CFTC COT/OI、LBMA/COMEX/SGE 跨市场证据；
- 数据预期分布和历史修订数据库；
- 央行交易级时间、完整历史新闻事件库；
- 期权隐含事件波动溢价。

这些来源接入后应填充已有证据槽位，而不是增加新的特例事件流程。
