# 原生数据层接入设计

2026-10-03，recovery_sol 接入草案；引擎选择以 storage-selection.md 为准。本文不声明已经安装或已经完成运行时验收。

## 文件与责任

recovery_sol 负责 Store、精确编码、层级范围索引、Market prepare/commit、pages/history、ingestion/main/API 的水位与关停编排、quant_input 适配器。storage_sol 负责 Capture/replay 和隔离 legacy 导入工具，不改 main/market/store/api；后续通过 canonical_snapshot 对接 Parquet/Arrow。drawing_sol 负责分析/模拟及前端。公共 DTO 放 persistence_contract.rs；所有持久位置使用 epoch、u64 sequence 和原始记录 digest，legacy 游标保留独立命名空间。

正式持久数据默认放 macOS Application Support/TraceFang/native、Windows LOCALAPPDATA/TraceFang/native，Linux XDG_DATA_HOME/tracefang/native（未配置时使用用户数据目录）。可通过专用数据路径配置覆盖。Library/Caches 只用于可再生编译/基准数据；不把正式数据库放入 Caches、临时目录或 repo/iCloud 默认目录。

## 在线事实与精确值

一份事实 redb 持有分钟事实、规范报价事件、最新报价/系列状态、来源配置、自选、研究/AI/run 元数据、层级聚合、应用版本和提交游标。内部键按来源、品种、周期和 signed i64 纳秒排序，时间的 sign bit 翻转用于字节排序；事件以 epoch/u64 应用顺序为主，源连接序号与上游交易所序号分开。公开 JSON 的十进制、纳秒、修订和 u64 身份用字符串，不能先在浏览器 JSON.parse 时损失精度。

值编码保留任意宽有符号系数与 scale、显式 NULL、真实源时间精度、接收时刻、证据身份和算法/模式版本。有限加乘使用共享宽精确运算；受检快速路径必须验证没有重缩放舍入，失败转宽路径。比例、开方和黄金换算的最终舍入须有单独政策和版本。MAX+1e-28 必须保存其真实和，或明确拒绝且保留原文，不能返回 MAX 作为精确和。

每个事实先按 revision/源版本门控。相同身份重复且 hash 相同不产生新修订；相同身份不同原文隔离为冲突。旧 high 降低、low 提高、NULL 替换都按新的完整事实重算，不能沿用旧极值或只累加差值。未知完整 volume 为 NULL，known sum/count/total 独立保存。

## 可修订层级范围索引

以连续 UTC 分钟格为索引坐标，每个叶块覆盖 64 分钟，父层每层组合 64 个子块。键包含来源、品种、基础周期、索引 generation/aggregation_version、层级和块起点；负 epoch 使用 Euclidean floor。节点保存首尾可见事实身份与 OHLC、精确 known_volume_sum、known/total count、final/provisional count、revision 摘要、实际时间边界、应用水位和覆盖位图。可为 final-only 与全部可见事实分别保存摘要，不把 provisional 混入 final-only 计算。

更新一分钟时，重读受影响叶块最多 64 个当前事实，再逐层重算最多 64 个子节点。事实、节点、系列状态和提交游标在同一 MVCC 事务提交。日/周/年先沿用既有交易时段、DST、交易日历取得半开窗口，再组合完全包含的索引节点，并读取边缘分钟。该索引是范围运算基础，不预先为每次修订重扫 19 个周期。缺失分钟和非交易时间由覆盖与日历一起解释，不把空范围虚构为完整历史。

首次导入/版本变更用独立 generation 构建并校验，最后一次事务切 active generation。查询校验 schema、aggregation、schedule 版本；不读取半建或旧版本索引。被旧运行引用的 generation 保留到引用释放。300 完整 UTC 日的逐行 redb p95 1511.59ms 是已测慢项，新索引必须独立通过同 oracle、修订/NULL/DST 及真实 API 并发性能验收，不能提前宣称达到基准分页速度。

## 原始帧：默认独立 redb + 追加 actor

建议默认独立 capture.redb 和单独追加 actor；每次有界事务同时写原始帧、身份/hash 去重索引、源序号映射、received_at 时间索引和 capture tail，Immediate commit 返回后才交 DurableReceipt。事实写入者随后按其确切 CapturePosition 处理，不把跨文件更新声称一个数据库事务。单写者竞争被隔离，但两 actor 仍共享磁盘及 fsync 资源，必须测实际小包、混合 32MiB 包和背压。

崩溃边界：raw 未提交不确认 durable；raw 提交但事实未提交从事实游标继续；事实提交后内存/广播前崩溃由 Store 水位重建。事实游标引用的 capture epoch、sequence、digest 必须能验证；缺记录、游标领先、epoch 变化或不连续范围导致明确 degraded/read-only，而不是从 first 跳过后恢复为 healthy。永久保留不使用隐含 7 天/10GiB 淘汰；任何显式保留策略也必须保护未投影输入、活跃回放和 checkpoint 引用。

同库 raw 的优势是可以把原文、事实和游标放同一事务；劣势是大包写入、删除/压缩和事实/索引写入争一个 writer。独立 raw 的优势是事实写入与历史/大载荷生命周期解耦；劣势是需要明确跨库恢复协议、每批两个持久屏障。分段追加日志可以减少大载荷 B+tree 写放大，但增加记录/段校验、截断尾部扫描、段目录/manifest 同步、时间/身份索引、淘汰引用与恢复实现；未通过同样本定向测试前不声称更快、更可靠。

定向实测只比较项目实录小帧与一份明确标注合成的 32MiB 载荷，单帧/有界批次，以及混合事实更新时 raw receipt 和事实 commit 的 p50/p95、积压、磁盘增长。测同库 raw 和独立 raw actor，错误/重复/SIGKILL 前后逐帧核验。如果追加日志进入比较也使用相同 envelope 和强持久屏障。测量结果决定 raw 最终格式，不再扩通用数据库排名。

## 应用、水位与退出

单一有序应用 actor 先完整验证输入，生成受影响状态的暂存结果和持久 effects，再提交；commit 成功后才换入可发布内存快照并广播。失败不修改已接纳 reducer/cache；decode 错误也以带来源/序号/范围的持久 quarantine 记录，不把隔离范围称为完整。大帧不使用 sequence=0 的伪水位：第一版以完整帧为提交原子边界，事务内部可分编码块但游标只在末尾前进，并使用有界帧/队列背压。若混合大帧验收显示此边界阻塞实时路径，再引入显式 frame/part staging，未激活的部分不可被 canonical 查询看到。

received、durable captured、prepared/applied、committed 和 broadcast 分开命名。需持久化的 history/backfill/API 等待 committed receipt；只读快照携带一次 MVCC commit ID、capture position、目录/日历/路由/计算版本。不以数据库时钟充当输入版本。

关停分阶段：停止新 HTTP 工作/新历史任务，停止并等待 providers，关闭输入队列并排空已接受原始帧，投影追到 durable capture tail，等待 facts/index/cursor 提交，最后结束 writer/读任务/文件句柄。独立期限与存储失败返回不完整和未完成水位；不能 abort 尚未保存的队列后标 clean。强制退出恢复与正常排空分别测试。

## 量化输入和原始回放

quant_input::read(AppState, QuantInputRequest) 只接服务器范围参数，按一次 MVCC 读得到规范 bars、quote、源能力/时点和提交版本，适配 drawing_sol 的 QuantInput/SnapshotToken。不能把 live cache 填进历史输入。final-only canonical 查询明确是当前已修订历史事实，不能冒充当时可见事实；original replay 必须使用输入帧/checkpoint 的 as-of sequence，从不晚于目标的 checkpoint 重放到目标，保留原始接收/接受/应用顺序。checkpoint 绑定输入 epoch/hash、完整 reducer/报价/指标状态及所有配置/算法版本，不兼容就重新重放；缺初始证据准确说明预热范围。

## 非破坏迁移清单与切换

PG 表、NATS 原始流和旧安装目录只作为隔离导入来源，在线新 Store 不继续写 PG。导入 manifest 记录旧实例/流身份、读取边界、模式版本、行数和 hash；PG 旧游标与新 capture sequence 绝不等同。先导入保留原始帧和完整原始 headers/seq，建立映射，再重建可恢复事实；缺失原始范围的旧事实以 legacy lineage 导入并明确证据限制。旧 NUMERIC(38,18)/微秒时刻已经丢掉的精度无法凭迁移恢复；有原文的重新解析并保留差异报告。

核对范围包括 instruments/market_sources/source routes/watchlist、latest_quotes/quote_events、实时 bars/series state/history/backfill 完成范围、配置/source 文件与源开关、研究缓存及清单、AI 请求/响应与模型/提示版本、模拟 runs/参数/输入版本/结果、回放 checkpoint/会话配置、日历和目录版本、错误隔离记录和旧投影游标。大型缓存文件记录内容 hash 与引用，先清点再迁移，不能只迁移四张行情表。旧绘图与前端本地数据由 drawing_sol 的清单对齐。

候选库独立建立和校验；shadow 增量追平时旧服务继续保留。切换前停止旧采集并验证接受尾部已导入，核对规范事实/配置/原始身份和源差异，重启原生候选并核验 build/source/lock/frontend hashes，才切正式入口。回退也要处理切换后新增输入的归属，不能开启旧服务后静默产生两份相互分叉的数据。当前阶段不执行生产切换。
