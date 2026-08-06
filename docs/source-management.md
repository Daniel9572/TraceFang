# 数据来源管理

## 路由规则

- 每个来源声明能力：`quote / candles / catalog / news / calendar`。
- 实时路由粒度是“合约”：每个合约只选择一个实时源，报价、当前柱、分时、状态与延迟统一使用它。
- 实时调用方必须显式指定来源，`auto` 会被拒绝；失败直接返回错误，不尝试其他实时来源。
- 历史 K 线不使用合约路由。所有合约和订阅者读取同一套 PostgreSQL 本地历史。
- 历史数据不足时由后台按 `unmetered -> limited -> metered` 顺序补缺；同成本级再按来源优先级排序。
- 连续结构化推送可以从本地报价事件生成分钟 OHLC；低频轮询报价只用于实时展示，不能进入历史规范序列。
- 限额来源只有在用户主动连接后才可能进入后台候选，不会为了历史补缺自动握手。
- 启停与优先级原子写入 `data/sources.json`。

当前来源：

1. `jin10_local`：本机会话认证的结构化 WebSocket 报价，并从同一报价流生成分钟 K 线；
2. `jin10_mcp`：官方结构化报价、K 线、目录、资讯与日历。

合约实时来源路由保存在 PostgreSQL；来源启停和页面顺序保存在 `data/sources.json`。历史来源优先级是独立策略，不随合约实时源切换。

## 本地 API

- `GET /api/sources?refresh=true|false`：来源、能力、连接策略、工具级额度和实时健康状态；
- `PATCH /api/sources/{source_id}`：更新 `enabled` 和/或 `priority`；
- `POST /api/sources/{source_id}/test`：绕过报价缓存，主动测试该来源的黄金报价并返回耗时；
- `GET /api/instruments/{code}/source`：读取该合约唯一行情源；
- `PUT /api/instruments/{code}/source`：切换该合约的实时行情；
- `GET /api/quotes/{code}?source=jin10_mcp|jin10_local`：读取报价；
- `GET /api/candles/{code}`：只从全局本地历史库读取分钟 K 线；旧版 `source` 参数暂时兼容但被忽略；
- `WS /api/stream/quotes/{code}?source=...`：持续报价与状态事件。

## 成本、限额与连接策略

每个来源均声明 `access_model`：

- `unmetered`：无调用计费或本应用不需要管理上游额度；
- `limited`：存在固定周期请求上限；
- `metered`：按调用量计费。

限额或付费来源必须由用户主动连接。金十 MCP 在应用启动时保持未连接；用户点击“连接并测试”后才完成 `initialize`、能力与资源发现，并执行一次报价测试。测试只改变连接状态，不会自动把合约切换到该来源。报价、K 线等 `tools/call` 仍受本地预算和上游限流双重保护。

额度按工具返回 `used / limit / reserve / available / usage_percent / resets_at`。当前 `used` 是本应用进程内计数，不包含同一账号在其他客户端产生的调用，因此 UI 明确标注为本应用记录；上游返回的限流错误始终具有最终权威性。

## 金十 MCP 调用额度

官方 MCP 每个用户、每个工具、北京时间自然日最多 1500 次。浏览器不再轮询主报价，后端为相同“来源 + 合约”的订阅者共享一个报价泵：

- 官方 MCP 仅在用户主动连接后完成协议握手；选择为当前行情源后，报价订阅首次立即调用，之后按 60 秒安全额度周期调度；
- 本地报价通过单一长连接接收，实测通常约每 3～4 秒一帧，不消耗 MCP 工具额度；
- 最新分钟 K 线进入页面时立即从本地历史库读取，之后贴着分钟边界刷新；
- 历史不足时请求立即返回已有数据，后台先尝试免费通道；只有已由用户主动连接的限额来源才可能继续补缺；
- 官方来源的“测试连接”只在用户主动点击时调用一次。

可通过 `.env` 配置：`JIN10_MCP_DAILY_TOOL_LIMIT`、`JIN10_MCP_QUOTA_RESERVE`、`JIN10_MCP_QUOTA_TIMEZONE` 与 `JIN10_MCP_QUOTA_WARNING_PERCENT`。页面目前重点展示 `get_quote` 与 `get_kline` 两个实际行情工具的独立额度。

WebSocket 解决浏览器到本机后端的持续传递，不能把低频上游变成逐笔行情。

页面用全局历史的最近一分钟 K 线作为基线，并把当前合约实时源的新报价合并进当前柱。收盘倒计时完全在浏览器本地计算，不消耗 MCP 调用额度；报价刷新速度仍受上述来源采样间隔约束。
