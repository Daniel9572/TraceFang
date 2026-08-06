# 金十 MCP 接入

## 协议

- 服务地址：`https://mcp.jin10.com/mcp`
- 协议版本：`2025-11-25`
- 认证：`Authorization: Bearer <token>`
- 传输：Streamable HTTP，客户端同时接受 `application/json` 与 `text/event-stream`
- 工具结果：只从 `result.structuredContent` 解析业务数据；`result.content` 仅保留为可读补充

客户端严格执行：

1. `initialize`
2. 保存服务端返回的 `MCP-Session-Id`
3. `notifications/initialized`
4. `tools/list` 与 `resources/list`
5. `resources/read` 或 `tools/call`

初始化后的请求携带协商后的 `MCP-Protocol-Version` 和会话 ID。会话返回 404 时重新握手并仅重试一次。

## 品种与数据

先读取 `quote://codes`，再把提供方代码映射到领域品种。首批映射：

| 领域品种 | 金十代码 | 数据性质 |
|---|---|---|
| XAU/USD | `XAUUSD` | 现货黄金 |
| XAG/USD | `XAGUSD` | 现货白银 |

K线接口当前为分钟级，单次 `count` 范围是 1–100，`time` 为可选 Unix 秒级时间戳且查询范围为 24 小时内。适配器会把返回结果统一为时间升序。

## 分页

`list_flash`、`list_news` 和 `search_news` 的请求使用 `cursor`；结构化响应读取 `data.next_cursor` 与 `data.has_more`。不得使用旧式 `offset`。

## 配额

每个用户、每个工具每天最多 1500 次，按北京时间自然日重置。进程内额度保护会预留少量余额，但它无法感知其他机器或进程的调用，因此服务端仍是最终权威。

1500 次/日不适合多品种秒级轮询。例如两个品种全天轮询时，平均每个品种约每 115 秒才能调用一次 `get_quote`。若需要逐笔或亚秒级数据，应新增推送型数据源，而不是提高 MCP 轮询频率。

## 密钥

程序启动时先读取项目根目录 `.env`，再从进程环境获取 `JIN10_MCP_BEARER_TOKEN`。加载使用 `override=False`，因此 PowerShell、系统服务或容器显式注入的同名变量优先于 `.env`。

`.env` 和其他 `.env.*` 本机文件默认被 Git 忽略，仓库只保留无密钥的 `.env.example`。真实 Token 不得写入示例、代码、测试、日志或 Git 历史。
