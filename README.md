# Market Analysis Platform

面向多市场、多品种和多数据源的交易分析平台。第一阶段用现货黄金与现货白银验证结构化采集、可信存储、按合约来源管理和 K 线终端。

## 第一版已经具备

- 自有品牌的桌面行情工作台：品种观察、实时报价、当前 K 线实时生长、收盘倒计时、当前价聚焦、缩放/平移与 1/5/15/30/60 分钟聚合；
- 每个合约独立保存一个实时行情源；报价、当前柱、分时、状态与延迟始终来自它；
- 全局历史 K 线统一从 PostgreSQL 读取，缺口在后台优先使用免费通道补齐；
- 实时请求或订阅只绑定一个明确来源，断线时标记停滞，不自动换源；
- 浏览器通过 WebSocket 接收持续报价事件，不再定时重复请求报价接口；
- PostgreSQL 保存合约来源路由、结构化原始载荷、报价事件、最新报价和 K 线；
- 来源管理：启停、优先级、能力、健康状态、运行要求和连通测试；
- 金十官方 MCP：品种目录、报价、分钟 K 线、快讯、文章和财经日历；
- 金十本地行情：读取本机会话并解码 WebSocket 二进制报价，支持心跳、断线重连和源时间；截图与 OCR 已从生产代码中删除；
- 通用领域模型、能力端口和适配器边界，方便继续接入期货、股票、外汇、加密资产和其他供应商。

## 数据来源

| 来源 | 首版能力 | 优点 | 运行要求 |
| --- | --- | --- | --- |
| 金十官方 MCP | 报价、分钟 K 线、目录、资讯、日历 | 结构化、字段完整、可验证 | 配置 Bearer Token；不需要启动金十软件 |
| 金十本地行情 | 现货黄金、现货白银报价与本地分钟 K 线 | 结构化推送，不消耗 MCP 工具额度 | 首次需登录金十软件生成本机会话；连接建立后软件无需保持运行 |

XAUUSD 可以使用本地结构化实时源，同时 XAGUSD 使用官方 MCP。实时来源失效只影响绑定它的合约；两者的历史 K 线仍读取同一套全局本地数据。本地结构化研究路线见 [docs/local-data-acceleration.md](docs/local-data-acceleration.md)。

## 快速开始

首次运行，双击 `setup.cmd` 安装并构建，之后双击 `start.cmd` 启动。

`setup.cmd` 会在被 Git 忽略的 `.env.local` 中生成本机 PostgreSQL 随机凭据；`start.cmd` 会在 Docker Desktop 已运行时启动项目专用 PostgreSQL 容器。

如需使用官方 MCP，推荐复制本机环境文件模板，填写 `JIN10_MCP_BEARER_TOKEN` 后再启动：

```powershell
Copy-Item .env.example .env
# 用文本编辑器填写 .env；不要把真实 Token 写回 .env.example
.\start.cmd
```

本地结构化报价使用 `JIN10_LOCAL_SESSION_TOKEN`。令牌写入 `.env` 后，用户 ID 默认从最新金十登录日志自动识别；也可通过 `JIN10_LOCAL_USER_ID` 显式配置。两种 Token 都不得写入代码、测试或可提交文件。

配置优先级为：进程环境变量 > `.env.local` > `.env`。`.env.local` 保存本机生成的数据库凭据，`.env` 保存人工维护的 Token，二者都被 Git 忽略。

浏览器会打开 `http://127.0.0.1:8000`。也可以完全从终端安装和启动：

```powershell
uv sync --python 3.13
corepack pnpm -C web install --frozen-lockfile
corepack pnpm -C web build
uv run market-analysis-server
```

真实 Token 不得写入 `.env` 以外的非忽略文件、代码、测试、日志或 Git 历史。若 Token 曾粘贴到聊天或其他可留存位置，建议在服务端重新生成。

## 数据源使用方式

- 实时路由粒度是“合约”。切换合约时恢复该合约自己的实时行情源。
- 所有实时来源选择都是强制来源，失败时直接报告，不暗中换源。
- `连接并测试`：限额或付费来源默认不连接；用户主动点击后才完成协议握手并测试报价。测试不会自动切换当前合约，测试官方源会明确消耗一次官方报价调用。
- 历史 K 线只从本地 PostgreSQL 读取，与合约实时源无关。缺口补齐在请求返回后静默执行，免费源优先；官方 MCP 只有在用户已主动连接且免费数据仍不足时才可能调用。
- 当前柱在浏览器中用“全局历史基线 + 当前合约实时源报价”合成：同一周期内只更新真实报价触达后的收盘/最高/最低，跨周期后由下一笔报价开启新柱。倒计时每秒更新，不额外调用数据接口。
- 浏览器通过 WebSocket 持续接收事件；官方 MCP 是请求式接口，报价按 60 秒安全额度周期发起，最新 K 线在进入页面和每个分钟边界立即请求，因此不是逐笔成交行情。
- 本地金十源由上游事件驱动，当前实测通常约 3～4 秒收到一帧，不做 0/5/10 秒固定轮询，也不会在断线时混入官方源或截图数据。

合约的实时行情源保存在 PostgreSQL 的 `instrument_source_routes` 兼容表 `quote` 行中；旧 `candles` 行不再参与历史读取。来源启停和页面顺序保存在 `data/sources.json`。接口契约见 [docs/source-management.md](docs/source-management.md)。

## PostgreSQL

本地容器只绑定 `127.0.0.1:54329`。报价先进入内存事件通道并推送页面，再由有界队列异步写入 PostgreSQL，不把数据库延迟放进实时报价热路径。核心表为 `quote_events`、`latest_quotes`、`candles`、`instrument_source_routes`、`instruments` 和 `market_sources`。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
corepack pnpm -C web typecheck
corepack pnpm -C web build
```

实时 MCP 契约测试默认关闭，显式启用会消耗少量工具额度：

```powershell
$env:RUN_JIN10_LIVE = "1"
uv run python -m unittest tests.live.test_jin10_contract -v
```

## 重要资产边界

`XAUUSD` 和 `XAGUSD` 是现货黄金、现货白银，不是黄金/白银期货。它们可以作为黄金期货分析的宏观和跨市场输入，但不能替代 COMEX、上期所等具体期货合约的价格、基差、期限结构、持仓量与换月数据。

总体设计见 [docs/architecture.md](docs/architecture.md)，MCP 合同见 [docs/jin10-mcp.md](docs/jin10-mcp.md)。
