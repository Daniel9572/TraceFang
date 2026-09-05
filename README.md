# TraceFang

面向多市场、多品种和多数据源的交易分析平台。当前目录覆盖现货贵金属、人民币汇率、沪金/沪银加权与具体月份合约、美元指数、布伦特原油连续、上证指数和纳斯达克综合指数，用于验证结构化采集、可信存储、品种观察管理、按合约来源管理和 K 线终端。

## 第一版已经具备

- 自有品牌的桌面行情工作台：品种观察、实时报价、当前 K 线实时生长、收盘倒计时、当前价聚焦、缩放/平移与 1/5/15/30/60 分钟聚合；
- 可持久化的自定义观察列表：侧边栏不再硬编码品种分类，可从完整目录搜索、添加或移除品种，并由列表自动维护实际的上游订阅；
- `USDCNH` 作为独立实时外汇品种；`XAUCNHG` 由 `XAUUSD × USDCNH ÷ 31.1034768` 实时换算为离岸人民币/克，并明确标记为应用换算值；
- `AU8888`（沪金加权）与 `AG8888`（沪银加权）通过同花顺公开结构化接口直接采集，分别以人民币/克和人民币/千克计价；它们是加权指数观察值，不是可下单的具体月份合约；
- `AU2610`、`AG2706`、`USDIND`、`BRN0Y`、`SHCOMP` 与 `IXIC` 通过同一公开源读取实时报价、日内统计和同源分钟 K 线；跨市场 K 线按上海、纽约、伦敦或 UTC 的源时区解析；
- 每个合约独立且仅保存一个实时数据源绑定；完整实时数据源必须同时提供 `quote + kline`，内部通道构成不暴露为合约选项；
- 金十统一行情向页面输出一份来源结果，内部原始通道分别落库且绝不静默互相代替；
- 当前与过去 K 线都服从合约绑定；普通查询只读专属本地缓存，缺失覆盖由后台仅向同一实时数据源补取、落库后再显示，不跨源标准化、补缺或回退；
- 实时请求或订阅只绑定一个明确来源，断线时标记停滞，不自动换源；
- 浏览器通过 WebSocket 接收持续报价事件；报价 REST、WebSocket 首帧和 K 线 GET 都只读本地。页面在发现历史覆盖未记录时调用隔离的普通回补 POST；同一交易时段内已进入视窗的具体 Bar 缺口可执行一次精确同源复核，两者都不阻塞实时链路；
- PostgreSQL 保存合约实时数据源路由、分渠道原始载荷、报价事件、最新报价和分源 K 线证据；旧标准历史表仅作冻结兼容资产；
- 实时数据源管理：合约级单选绑定、完整能力、健康状态、运行要求和连通测试；
- 金十官方 MCP 适配代码仅作归档保留；应用不读取其配置、不注册来源、不初始化，也不用于实时、历史或校对；
- 金十官网高速行情：直接接收金十官网公开结构化 WebSocket 的价格变化推送，无需 Token 或桌面客户端；
- 金十桌面会话行情：使用本机金十客户端会话鉴权并解码 WebSocket 二进制报价，支持心跳、断线重连和源时间；截图与 OCR 已从生产代码中删除；
- 黄金重要数据与事件事实库：统一保存计划/实际/有效期/发布时间与修订版本，事件显示和资金主导策略独立，并以冲击分、趋势分和证据覆盖率评估已发生事件；
- 通用领域模型、能力端口和适配器边界，方便继续接入期货、股票、外汇、加密资产和其他供应商。

## 数据来源

| 实时数据源 | 首版能力 | 优点 | 运行要求 |
| --- | --- | --- | --- |
| 金十统一行情 | 报价事件 + 当前/过去 K 线 + 日内统计 | 结构化、事件驱动、同源历史可缓存 | 实时报价无需登录；macOS 可自动复用已登录客户端会话，其他平台可显式配置 |
| 同花顺公开行情 | 沪金/沪银、美元指数、布伦特原油与中美股票指数的报价 + 同源分钟 K 线 | 公开结构化接口、无需本地期货软件常驻 | 后端按观察品种节流轮询 |

`jin10_web` 与 `jin10_local` 是金十实时数据源内部的采集通道，不出现在数据源选择器中。它们保持独立采集、健康检查和原始落库。XAUUSD、XAGUSD 与 USDCNH 绑定 `jin10_client`；同花顺目录中的八个直连品种绑定 `tonghuashun_futures`。前端只展示当前品种兼容的完整实时数据源，同一品种不能同时绑定两个来源。XAUCNHG 不是原生合约，而是同一来源下 XAUUSD 与 USDCNH 的应用层实时换算观察值，目前不伪造历史合成 OHLC。权威定义见 [docs/data-semantics.md](docs/data-semantics.md)，本地结构化研究路线见 [docs/local-data-acceleration.md](docs/local-data-acceleration.md)。

## 快速开始

首次运行，双击 `setup.cmd` 安装并构建，之后双击 `start.cmd` 启动。

`setup.cmd` 会在被 Git 忽略的 `.env.local` 中生成本机 PostgreSQL 随机凭据；`start.cmd` 会在 Docker Desktop 已运行时启动项目专用 PostgreSQL 容器。

复制本机环境文件模板后即可启动。macOS 标准安装若已登录金十客户端，历史能力会自动复用该本机会话；需要覆盖自动发现或在其他平台运行时，再填写桌面会话配置。MCP 配置已从模板和应用运行时移除：

```powershell
Copy-Item .env.example .env
# 按需填写本地会话覆盖值；不要把真实 Token 写回 .env.example
.\start.cmd
```

金十统一行情的实时报价无需 Token；历史 K 线回补与日内补充复用桌面会话。显式 `JIN10_LOCAL_SESSION_TOKEN` 优先，否则 macOS 从当前用户的 `~/Library/Application Support/com.jin10.desktop/local_storage.json` 读取 `ji10_token`；Windows、Linux 或非标准安装只需显式提供 Token。行情协议以 `user_id=0` 让服务端从会话解析账户，不读取客户端日志，也不保存用户编号。`LOCAL/WEB` 只是内部适配器配置前缀，不是数据源名称。

配置优先级为：进程环境变量 > `.env.local` > `.env`。`.env.local` 保存本机生成的数据库凭据，`.env` 保存人工维护的 Token，二者都被 Git 忽略。

专家工作台的 AI 分析复用本机 Codex 登录，不要求另存 OpenAI API Key。服务优先读取
`TRACEFANG_CODEX_CLI_PATH`，其次从 `PATH` 查找；macOS 标准安装还会自动识别
`/Applications/ChatGPT.app/Contents/Resources/codex`。非标准安装可在 `.env.local` 中填写
可执行文件完整路径；登录状态以 `codex login status` 为准。

浏览器会打开 `http://127.0.0.1:8000`。也可以完全从终端安装和启动：

```powershell
uv sync --python 3.13
cd web
corepack pnpm install --frozen-lockfile
corepack pnpm build
cd ..
uv run tracefang-server
```

macOS 日常运行可直接双击 `start.command`。它先构建网页，再由同一个 FastAPI
进程在 `http://127.0.0.1:8000` 提供页面与 API，因此不存在 5173 前端仍在、8000
后端已经退出的分裂生命周期。开发时双击 `dev.command`，或运行
`python3 scripts/run-local.py --dev`；开发入口会统一管理 Vite 与 FastAPI，任一进程
退出都会停止另一项，避免留下只能显示旧页面、却无法连接实时服务的孤儿进程。

页面把两类故障明确分开：浏览器到 TraceFang 的 WebSocket 关闭显示“本机实时服务”
状态；WebSocket 仍连接但数据源报告不可用时，显示具体“上游行情”恢复状态。临时
断线采用 0.5 秒起步、最长 15 秒的指数退避，协议拒绝不会无限重试。

客户端会话是本机敏感凭据：真实 Token 不得提交、复制到文档/聊天、写入测试、日志、数据库、API 响应或 Git 历史。解析器只在后端内存保存凭据快照，并拒绝符号链接、目录外或非当前用户拥有的自动发现文件。macOS 客户端重新登录并轮换 Token 后，下一次鉴权失败只刷新会话并重试一次；仍失败则进入明确的鉴权错误和退避，不推进任何历史覆盖。显式环境值变更后应重启后端。若 Token 曾暴露到可留存位置，建议立即轮换。

## 数据源使用方式

- 实时路由粒度是“合约”。切换合约时恢复该合约唯一的实时数据源绑定。
- 来源菜单是单选列表；选择新实时数据源会原子替换旧绑定，不存在多个来源同时绑定、优先级回退或 `auto`。
- `测试连接` 面向完整实时数据源，同时验证报价和 K 线输出；内部 Web/桌面通道不作为两个可选虚拟源展示。
- 实时采集由后端合约路由常驻管理，与浏览器订阅数量无关。实时数据源把实际需求映射到内部推送通道；启动时会把遗留的物理通道或 MCP 合约绑定迁移到默认实时数据源。
- 实时 REST/WebSocket 首帧只读内存或 PostgreSQL `latest_quotes`，不会调用任何上游。用户主动“测试连接”是明确的例外，并会把所得原始帧按真实渠道缓存、落库。
- K 线 GET 先解析合约绑定，再只读取该实时数据源的专属内存/数据库缓存，并返回服务端不透明排他游标。页面先显示最新 500 根；每次左缘动作只提交一个以逻辑 Bar 表达的 `countBack` 历史命令，服务端先读本地、必要时执行一个最多 10000 分钟的同源传输步骤、增量物化并直接返回周期页。浏览器不再换算分钟或串联多个回补/准备请求。后端只请求当前绑定源自己的缺失差集，把原始 K 线、统一 Bar、覆盖与权威状态原子写入 PostgreSQL；普通 GET 保持只读，实时尾部只重算受影响桶。前插保持用户当前视窗与实时跟随状态。同一交易时段内的可见缺口可以只复核精确子区间，跨会话休市不会触发。
- 当前柱由该实时数据源的同源 K 线状态与报价事件共同生长：同秒到达的多帧全部参与开高低收；跨周期后的第一笔报价独立开启新柱，不沿用上一柱收盘价。倒计时每秒更新，不额外调用数据接口。
- 浏览器通过 WebSocket 持续接收本地事件；REST/WebSocket 都不接受来源参数，只服从合约绑定。MCP 服务不会创建客户端、握手、轮询或执行任何工具调用。
- 金十统一行情由上游事件驱动，不做 0/5/10 秒固定轮询，也不会在任一内部采集适配器断线时跨来源补值。
- 金十统一行情归类为“优质分析级”。其实时报价使用公开网页行情协议的变化驱动档；2026-08-06 实测黄金 30 秒收到 70 帧，平均约 3.68 帧/秒；本机后端接收至网页采样器平均 1.27ms。它适合常规分析，但不等同于有明确低延迟 SLA 的机构专业级数据。
- 图表工具栏的来源下拉框同时承担管理控制台：可测试、查看运行状态，并为当前合约单选实时数据源，不再打开独立管理页面。

合约的实时数据源绑定保存在 PostgreSQL 的 `instrument_source_routes` 表中，路由类型为 `realtime`。`instrument_symbol` 唯一索引从数据库层保证每个合约至多一个绑定。观察列表保存在 `watchlists` 与 `watchlist_items`，品种目录通过 `GET /api/instruments` 读取，观察列表通过 `GET/POST/DELETE /api/watchlist` 管理。接口契约见 [docs/source-management.md](docs/source-management.md)，图表历史状态机见 [docs/kline-history-loading.md](docs/kline-history-loading.md)。

## PostgreSQL

本地容器默认只绑定 `127.0.0.1:15432`，使用低于 Windows 默认动态端口范围的本机专用端口；可在被 Git 忽略的 `.env.local` 中修改。原始报价先在同一事件循环轮次更新内存报价/K 线并推送页面，再由有界队列异步写入 PostgreSQL，不把数据库延迟放进实时热路径。`quote_events`、`latest_quotes` 与 `candles` 是分渠道证据层；`realtime_candle_cache_ranges` 记录同一实时数据源已完成的永久历史检查范围，包括成功但零行的休市区间；`realtime_bar_series_state` 保存排他 `authoritative_through`、可选 `history_floor`、可变尾部软检查和证据版本。二者均在历史批次的同一短事务中更新，数据库失败不会先把未持久化历史发布到内存。`derived_period_bars` 保存派生周期页，`period_bar_materializations` 保存对应事实游标和物化边界，使重复读取无需全量聚合。覆盖表示“上游已成功检查”，不保证区间内每分钟都有 Bar；同一交易时段内的具体可见缺口允许冷却受控的精确同源复核。`candle_validation_results` 与 `standard_candles` 暂时冻结，不参与查询。

## 验证

```powershell
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
corepack pnpm -C web typecheck
corepack pnpm -C web build
```

实时 MCP 契约测试仅作为归档能力保留，不属于当前应用测试和数据源链路。

## 重要资产边界

`XAUUSD` 和 `XAGUSD` 是现货黄金、现货白银；`AU8888` 和 `AG8888` 是加权指数观察值；`AU2610` 和 `AG2706` 才是明确月份合约。`BRN0Y` 是供应商发布的布伦特连续序列，不是交易所可下单合约；`USDIND`、`SHCOMP` 与 `IXIC` 都是指数观察值。

总体设计见 [docs/architecture.md](docs/architecture.md)，黄金事件契约与评分口径见
[docs/gold-event-system.md](docs/gold-event-system.md)，冻结的 MCP 历史合同见
[docs/jin10-mcp.md](docs/jin10-mcp.md)。
