# 数据来源管理

## 路由规则

- 每个来源声明能力：`quote / candles / catalog / news / calendar`。
- 每个报价来源声明稳定的服务等级 `quote_service_tier`：`institutional / enhanced / standard / reference`。`institutional` 保留给有明确低延迟 SLA 的机构专业数据；当前金十官网高速行情属于 `enhanced`，定位是适合常规分析的优质分析级数据，不等同于机构级行情。
- 实时路由粒度是“合约”：每个合约只选择一个实时产品/渠道。逻辑组合产品可以声明多个原始子通道，但字段归属必须固定并对外展示。
- 实时调用方必须显式指定来源，`auto` 会被拒绝；失败直接返回错误，不尝试其他实时来源。
- 历史 K 线不使用合约路由。所有合约和订阅者只读取 PostgreSQL `standard_candles`。
- 历史数据不足时，后台只允许 `unmetered` 免费通道自动补缺。当前没有任何 `limited / metered` 活动数据源或校对候选。
- 连续结构化推送可以从各自的原始报价事件生成候选分钟 OHLC。候选必须经过结构、时序和多源价格一致性校验；接受/拒绝结果写入 `candle_validation_results`，只有接受记录进入 `standard_candles`。
- 冻结来源不允许启用、连接、测试、选择或进入任何历史候选。
- 启停与优先级原子写入 `data/sources.json`。
- 图表工具栏中的“实时来源”下拉框就是完整控制台：直接完成启停、连接测试、合约切换、额度与运行条件查看，不再跳转到独立管理页面。

当前来源：

1. `jin10_client`（金十客户端组合行情）：逻辑产品。`jin10_web` 固定拥有 `last/change/change_percent`，`jin10_local` 固定拥有 `open/high/low/volume`；
2. `jin10_web`（金十官网高速原始通道）：公开、无需登录口令的结构化价格变化推送；
3. `jin10_local`（金十桌面会话原始通道）：本机金十客户端会话鉴权的结构化 WebSocket，提供日内统计、买卖价等补充字段；
4. `jin10_mcp`（冻结归档）：能力代码与配置字段保留，但当前不实例化客户端，不属于可选数据源，也不参与实时、历史、补缺或校对。

`jin10_client` 的组合只发生在查询/展示层。`quote_events` 与 `latest_quotes` 中不会出现伪造的 `jin10_client` 原始记录。`jin10_web` 失败时不使用 `jin10_local.last` 顶替；`jin10_local` 失败时保留价格通道并显式返回补充字段缺失/过期状态。

合约实时来源路由保存在 PostgreSQL；来源启停和页面顺序保存在 `data/sources.json`。历史来源优先级是独立策略，不随合约实时源切换。

启动时会把遗留的 `jin10_mcp` 合约路由迁移为 `jin10_client`，并强制把来源配置持久化为禁用。历史遗留的 MCP 原始记录保留作审计，但所有含 MCP 候选证据的标准 K 线和校验结果会被删除；历史查询还会按允许来源再次过滤。

## 本地 API

- `GET /api/sources?refresh=true|false`：来源、能力、连接策略、工具级额度和实时健康状态；
- `PATCH /api/sources/{source_id}`：更新活动来源的 `enabled` 和/或 `priority`；对冻结 MCP 的修改会被拒绝；
- `POST /api/sources/{source_id}/test`：主动验证活动来源的连接和当前帧；冻结 MCP 不可测试；
- `GET /api/instruments/{code}/source`：读取该合约唯一行情源；
- `PUT /api/instruments/{code}/source`：切换该合约的实时行情；
- `GET /api/quotes/{code}?source=jin10_client|jin10_local|jin10_web`：只读本地最新帧；组合源返回 `price / supplement / field_sources / missing_channels / stale_channels`。传入 `jin10_mcp` 会返回冻结错误；
- `GET /api/candles/{code}`：只从全局本地标准历史读取分钟 K 线；旧版 `source` 参数暂时兼容但被忽略；
- `WS /api/stream/quotes/{code}?source=...`：持续报价与状态事件。

## 成本、限额与连接策略

每个来源均声明 `access_model`：

- `unmetered`：无调用计费或本应用不需要管理上游额度；
- `limited`：存在固定周期请求上限；
- `metered`：按调用量计费。

当前没有启用的限额或付费来源。金十 MCP 不是“等待用户连接”，而是明确冻结：启动时不创建 MCP 客户端，页面开关、合约选择和测试按钮均锁定，后端即使收到直接请求也会拒绝。

额度按工具返回 `used / limit / reserve / available / usage_percent / resets_at`。当前 `used` 是本应用进程内计数，不包含同一账号在其他客户端产生的调用，因此 UI 明确标注为本应用记录；上游返回的限流错误始终具有最终权威性。

## 金十 MCP 冻结策略

历史协议实现和额度配置仍保留，方便未来重新评审，但当前运行规则为：

- `JIN10_MCP_ENABLED=false` 为默认冻结开关；未显式改变项目策略前不得修改；
- 不执行 `initialize`、`tools/list`、`resources/list` 或 `tools/call`；
- 不采集报价或 K 线，不提供资讯/日历，不进入实时路由、历史补缺或主动校对列表；
- 历史 MCP 原始记录仅作审计归档，不能成为 `standard_candles` 的主来源或候选证据；
- 本地报价通过单一长连接接收，实测通常约每 3～4 秒一帧，不消耗 MCP 工具额度；
- 网页极速报价使用独立公开长连接和变化驱动档；2026-08-06 实测黄金 30 秒 70 帧，平均约 3.68 帧/秒，不消耗 MCP 工具额度；
- 同次采样中，本机后端接收至网页采样器平均 1.27ms、中位 0.96ms、P95 2.52ms；上游源时间只有整秒精度，不能据此伪造精确的公网单向延迟；
- 新页面和新 WebSocket 订阅者从内存或 `latest_quotes` 回放最新帧；最新分钟 K 线从 `standard_candles` 读取，缺口只尝试免费通道。

`.env.example` 中的 Token、额度与协议配置仅为冻结归档，不代表当前可用来源。页面显示 MCP 为“已冻结”，不会展示为待连接。

WebSocket 解决浏览器到本机后端的持续传递，不能把低频上游变成逐笔行情。

页面用全局标准历史的最近一分钟 K 线作为基线，并只用所选产品的价格角色更新当前柱。对于 `jin10_client`，当前柱严格使用 `jin10_web` 帧；`jin10_local` 只更新日内统计，不进入价格时间线。同一源时间秒内的多个价格事件以本地接收时间排序，全部参与当前柱高低收计算；收盘倒计时完全在浏览器本地计算。
