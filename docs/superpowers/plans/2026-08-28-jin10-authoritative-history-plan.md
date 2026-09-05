# 金十权威历史自动回补实施计划

## 目标

让 `jin10_client` 在绑定品种需要更早 Bar 时自动使用金十当前登录会话查询同源历史，将原始 K 线、统一 Bar 状态、已完成覆盖区间和权威水位持久化到 PostgreSQL。相同范围在进程内、重启后都不重复访问上游；重叠范围只获取差集；实时报价和当前 Bar 热路径不得被历史下载或批量落库阻塞。

## 已批准约束

- 页面和合约始终只面对 `jin10_client`，不得重新暴露 `jin10_web` / `jin10_local` 为可选数据源。
- 显式环境变量优先；未配置时自动复用 macOS 金十客户端会话。
- 金十令牌只驻留后端内存，不写 `.env`、数据库、日志、API 或前端状态。
- 图表 GET 只读本地；只有明确的历史需求命令才能访问上游。
- `authoritative_through` 是排他水位；覆盖区间表示“上游已成功检查”，两者不能互相推导。
- 成功的空窗口也缓存；失败绝不推进覆盖、水位或历史边界。
- 普通回补服从覆盖缓存；可见的同交易时段缺口才能执行精确 `revalidate`。
- 当前仍可能追加的尾部不能永久缓存。
- 在当前脏工作树上增量实现，禁止覆盖或提交无关改动。

## 已批准的游标与报价语义修订

以下修订覆盖本文后续任务中仍出现的旧客户端分页描述：

- 普通左缘加载由浏览器发送一次逻辑 `count_back` 命令；浏览器不把周期换算成分钟，也不循环调用 `/backfill` 与 `/prepare`。
- 服务端先读本地页，必要时执行一个最多 10000 分钟的同源传输步骤，增量物化后直接返回目标周期页。
- 页游标由服务端生成，按来源、品种、周期和交易日历隔离；`next_before` 只供诊断，前端不得从时间重建游标。
- `local_status` 与 `source_status` 分开表达；分时和全部 K 线使用同一状态机，1 秒历史能力不足时返回 `unsupported`，不伪造分钟轨迹。
- 报价只按稳定传输身份实现幂等；相同价格、相同来源时间和晚到的不同事件全部保留。推送标记为 `event`，轮询观测标记为 `snapshot`。

## 实施顺序

### 任务 1：建立 macOS 会话解析器

**文件**

- 新增：`src/tracefang/infrastructure/providers/jin10_local/session.py`
- 修改：`src/tracefang/infrastructure/providers/jin10_local/settings.py`
- 修改：`src/tracefang/infrastructure/providers/jin10_local/provider.py`
- 修改：`src/tracefang/infrastructure/providers/jin10_local/__init__.py`
- 修改：`src/tracefang/api.py`
- 新增：`tests/test_jin10_local_session.py`
- 修改：`tests/test_jin10_local_settings.py`

**先写失败测试**

1. 显式 `JIN10_LOCAL_SESSION_TOKEN` 始终覆盖自动发现。
2. 未配置显式凭据时，从 `~/Library/Application Support/com.jin10.desktop/local_storage.json` 的 `ji10_token` 读取 36 位令牌；协议使用零值身份从 token 解析账户，禁止把日志作为凭据来源。
3. 拒绝目录外路径、符号链接、非当前用户拥有的凭据文件、短令牌和缺失会话存储。
4. `repr`、异常、健康状态和日志中不得出现令牌或完整凭据内容。
5. 客户端重新登录并替换令牌后，刷新解析返回新凭据。
6. Windows 与 Linux 使用显式 Token，不保留任何日志兼容路径。

**实现**

- 定义 `Jin10SessionCredentials`（令牌字段 `repr=False`）和 `Jin10SessionResolver`。
- 解析器每次返回一个不可变凭据快照；环境变量为第一优先级，macOS/Windows 客户端存储为回退。
- `Jin10LocalSettings` 只保存端点、频率、超时等非秘密配置，并持有凭据解析接口；保留测试中显式构造凭据的便利入口。
- Provider 首次登录使用当前快照；鉴权失败或重连要求重新登录时强制刷新一次，不无限重试。
- `api.lifespan` 以“解析器可用”判断历史能力已配置，不把内部路径或凭据暴露给来源描述。

**验证**

```bash
uv run python -m unittest tests.test_jin10_local_session tests.test_jin10_local_settings -v
uv run ruff check src/tracefang/infrastructure/providers/jin10_local tests/test_jin10_local_session.py tests/test_jin10_local_settings.py
```

### 任务 2：让历史提供方返回权威元数据

**文件**

- 修改：`src/tracefang/application/realtime_bars.py`
- 修改：`src/tracefang/infrastructure/providers/jin10_local/provider.py`
- 修改：`src/tracefang/infrastructure/providers/tonghuashun_futures/provider.py`
- 修改：`tests/test_jin10_local_provider.py`
- 修改：`tests/test_tonghuashun_futures_provider.py`
- 修改：`tests/test_realtime_bars.py`

**先写失败测试**

1. 历史提供方返回 `candles`、实际检查范围、`authoritative_through`、证据版本和可选的 `history_floor`。
2. `authoritative_through` 必须按分钟对齐且不得超过请求结束时间。
3. 金十 manifest 版本由文件名、声明条数和结束时间形成稳定摘要，不包含令牌或下载查询参数。
4. 当前未完成分钟不会进入永久权威范围。
5. 空结果只有在成功完成协议和文件检查后才算有效批次。
6. 没有明确上游证据时 `history_floor` 保持 `None`，不能用一个周末空窗口猜测历史结束。

**实现**

- 在应用层定义 provider-neutral 的 `HistoricalBarBatch`，替换只返回 `tuple[Candle, ...]` 的历史协议。
- 金十 Provider 在分页 manifest 时累计证据版本与最后可确认边界；继续校验文件声明区间和追加式尾部。
- 同花顺历史 Provider 适配同一批次契约，不能在通用服务中增加供应商分支。
- 保持历史 Bar 以真实内部证据通道落库，对外投影仍使用完整实时数据源 ID。

**验证**

```bash
uv run python -m unittest tests.test_jin10_local_provider tests.test_tonghuashun_futures_provider tests.test_realtime_bars -v
uv run ruff check src/tracefang/application/realtime_bars.py src/tracefang/infrastructure/providers
```

### 任务 3：持久化序列权威水位并原子提交历史批次

**文件**

- 修改：`src/tracefang/infrastructure/postgres/schema.py`
- 修改：`src/tracefang/infrastructure/postgres/store.py`
- 修改：`src/tracefang/application/realtime_bars.py`
- 修改：`tests/test_postgres_quote_events.py`
- 修改：`tests/test_realtime_bars.py`
- 修改：`tests/test_realtime_klines.py`

**数据库结构**

新增 `realtime_bar_series_state`，主键为：

```text
(realtime_source_id, instrument_symbol, interval_seconds)
```

字段至少包含：

```text
upstream_channel_id
provider_symbol
latest_authoritative_open_time
authoritative_through
history_floor
tail_checked_through
tail_checked_at
evidence_version
updated_at
```

约束：水位和历史边界必须带时区、按周期对齐；`history_floor <= authoritative_through`；更新水位默认单调前进。

**先写失败测试**

1. Schema 可在已有数据库上幂等执行。
2. 序列水位按来源、品种和基础周期隔离。
3. 一次历史提交在同一事务内写入 `candles`、`realtime_bars`、覆盖区间和序列状态。
4. 任一步失败时四类记录全部回滚。
5. 相邻或重叠覆盖段合并，查询差集不会随重复查看不断增长。
6. 合并覆盖段后的 `row_count` 由数据库中的去重 Bar 重新计算，不能简单累加重叠请求。
7. 成功零行仍产生覆盖；失败零行不产生覆盖。
8. 服务重启后能恢复权威水位和覆盖区间。

**实现**

- 给 `SourceBarStore` 增加读取序列状态和原子提交历史批次的接口。
- PostgreSQL 提交方法在短事务内完成原始证据、统一 Bar、覆盖段压缩和序列状态 upsert。
- 网络请求与文件解析必须在事务之外完成；事务只包围校验后的数据写入。
- 历史状态先在临时结构中归约，数据库提交成功后才发布到内存 Bar 缓存，避免落库失败但页面短暂看到未持久化历史。
- 覆盖区间仍使用 `[start, end)`；只有批次水位之前的部分进入永久覆盖，尾部只更新软检查字段。

**验证**

```bash
uv run python -m unittest tests.test_postgres_quote_events tests.test_realtime_bars tests.test_realtime_klines -v
uv run ruff check src/tracefang/infrastructure/postgres src/tracefang/application/realtime_bars.py
```

### 任务 4：完善回补协调器、差集计算和退避

**文件**

- 修改：`src/tracefang/application/realtime_bars.py`
- 修改：`src/tracefang/api.py`
- 修改：`tests/test_realtime_bars.py`
- 修改：`tests/test_realtime_klines.py`
- 修改：`tests/test_realtime_source_api_contract.py`

**先写失败测试**

1. 两个完全相同的并发请求只访问上游一次，第二个调用得到 `joined` 语义。
2. `[0, 60)` 与 `[30, 90)` 并发时，上游总请求等价于 `[0, 60)` 加 `[60, 90)`，不得重复 `[30, 60)`。
3. 锁释放后必须重新读取数据库覆盖，不能使用锁前的过期差集。
4. 每个来源/品种同一时刻最多执行一个历史协议请求；全部历史提供方共享可配置的小型并发信号量，默认 2。
5. 已缓存范围返回 `cached`；成功空差集返回 `advanced`；明确边界返回 `exhausted`。
6. 当前可变尾部在冷却期内返回 `deferred/retry_after`，权威水位或证据版本推进后允许再次请求。
7. 网络、鉴权、协议、校验、取消和数据库失败都不记录完成覆盖。
8. 鉴权失败只刷新客户端会话并重试一次；再次失败进入指数退避。
9. `revalidate` 只绕过目标缺口的粗粒度覆盖，并按来源、品种、范围、证据版本和冷却时间去重。
10. 历史任务取消或失败不停止报价订阅、当前 Bar 生长或 WebSocket 广播。

**实现**

- 将精确请求共享与同序列重叠请求串行化保留在应用层，获取序列锁后重新计算差集。
- 在访问 Provider 前扣除永久覆盖和有效软尾部检查；合并相邻缺失窗口，但仍服从单次 10000 分钟传输上限。
- 将 `BarBackfillResult` 扩展为稳定状态枚举，并返回覆盖范围、新增行数、`authoritative_through`、`history_floor` 与 `retry_after`。
- 指标计数至少包含缓存命中、实际上游调用、joined 调用、写入行数、失败数和当前待处理任务数。

**验证**

```bash
uv run python -m unittest tests.test_realtime_bars tests.test_realtime_klines tests.test_realtime_source_api_contract -v
uv run ruff check src/tracefang/application/realtime_bars.py src/tracefang/api.py
```

### 任务 5：更新统一来源状态和 API 契约

**文件**

- 修改：`src/tracefang/api.py`
- 修改：`src/tracefang/application/sources.py`（仅在需要扩展通用健康字段时）
- 修改：`tests/test_realtime_source_api_contract.py`
- 修改：`tests/test_source_manager.py`
- 修改：`web/src/types.ts`

**先写失败测试**

1. 自动发现会话后 `jin10_client.history_backfill_configured=true` 且来源可达到 `ready`。
2. 客户端退出但已有有效会话时，来源状态准确区分“历史可连接”“正在重连”和“鉴权失效”。
3. 公共 API、来源选择器和健康响应都不出现内部通道选择项或凭据路径。
4. 回补响应状态与时间字段在 OpenAPI 中稳定且带时区。
5. 健康诊断包含权威水位和聚合计数，但不包含用户编号、令牌、文件 URL 查询参数。

**实现**

- 保持 `/api/candles/{code}/backfill` 为显式历史命令；更新响应 DTO，不把上游调用移入 `/api/bars/{code}`。
- 来源能力来自统一契约和会话解析器状态，不再要求用户手写 `.env` 才能启用 macOS 历史能力。
- 在 `/api/health` 或现有来源诊断结构中增加聚合历史指标；前端普通来源设置只显示统一结果。

**验证**

```bash
uv run python -m unittest tests.test_realtime_source_api_contract tests.test_source_manager -v
uv run ruff check src/tracefang/api.py src/tracefang/application/sources.py
```

### 任务 6：让图表按需求平滑续页且不重复触发

**文件**

- 修改：`web/src/types.ts`
- 修改：`web/src/api.ts`
- 修改：`web/src/historyLoading.ts`
- 修改：`web/src/App.tsx`
- 修改：`web/src/MarketChart.tsx`
- 修改：`web/src/ExpertModeWorkspace.tsx`
- 修改：`web/tests/historyLoading.test.ts`
- 修改：`web/tests/barPageCache.test.ts`
- 新增：`web/tests/historyRequestState.test.ts`

**先写失败测试**

1. 本地页有数据时绝不提交上游历史命令。
2. 左缘需求在锁占用期间保持，完成后自动继续评估。
3. `cached/joined/deferred` 不会错误前移游标；`advanced` 使用后端确认范围前移；`exhausted` 结束当前需求。
4. 可见范围连续变化时，同一历史需求只派发一次。
5. 批次大小至少覆盖可见 Bar 数、默认 240 根和已启用指标最大预热长度，并继续按 10000 分钟传输页切片。
6. 前插后逻辑可见区保持不变；实时跟随状态不被历史结果重置。
7. 实际上游等待少于 150ms 时不显示加载闪烁，超时后只显示轻量非阻断状态。
8. 正常缓存边界和正常耗尽不显示错误或额外边界提示。
9. 来源/品种/周期切换会取消旧请求，旧结果无法合并到新图表。
10. 精确缺口复核按证据版本和冷却去重。

**实现**

- 将图表历史回调参数扩展为明确的 `HistoryDemand`，至少携带可见 Bar 数和需求原因。
- 由 `historyLoading.ts` 纯函数决定窗口、批次、响应状态和下一游标，React 组件只负责调度与合并。
- `api.ts` 聚合多个传输页时保留最严格状态和最早确认边界，不再把所有非 `fetched` 结果折叠成 `cached`。
- 保留现有 `historyDemandActive` latch 和视窗平移逻辑；增加延迟加载指示器，避免缓存命中闪烁。

**验证**

```bash
cd web
pnpm test
pnpm typecheck
pnpm build
```

### 任务 7：文档和安全检查

**文件**

- 修改：`.env.example`
- 修改：`README.md`
- 修改：`docs/data-semantics.md`
- 修改：`docs/kline-history-loading.md`
- 修改：`docs/local-data-acceleration.md`
- 修改：`docs/source-management.md`

**内容**

- 说明 macOS/Windows 自动会话发现、显式配置优先级和客户端重新登录行为。
- 明确定义 `authoritative_through`、永久覆盖、软尾部检查和 `history_floor`。
- 记录普通回补、精确复核、空窗口、失败和退避状态机。
- 删除“macOS 必须显式填写用户编号”等已经过时的说明。
- 明确客户端会话属于本机敏感凭据，禁止提交、复制或输出。

**验证**

```bash
rg -n "JIN10_LOCAL_SESSION_TOKEN|ji10_token|userId|authoritative_through|history_floor" README.md docs .env.example
git diff --check
```

人工检查所有示例只包含占位值，不包含本机真实用户编号、令牌或日志内容。

### 任务 8：全量验证与真实会话验收

**自动验证**

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
cd web && pnpm test && pnpm typecheck && pnpm build
```

若全量后端测试仍仅因仓库既有的历史包 fixture 缺失失败，必须单独记录该已知问题，同时保证本计划涉及的测试全部通过；不得为了变绿伪造 fixture。

**真实金十会话验收**

1. 保持金十客户端已登录，重启 TraceFang 后端。
2. 确认 `jin10_client` 显示历史能力已配置，日志不含凭据。
3. 打开 `XAUUSD / 1分`，记录 PostgreSQL 当前 Bar 数、覆盖段和权威水位。
4. 将图表拖到左边缘；确认只出现一次实际上游历史请求，数据库 Bar 数增加，视窗不跳动，实时价格仍持续更新。
5. 再次查看相同范围；确认响应为本地命中，实际上游请求计数不增加。
6. 请求一个部分重叠范围；确认只补未覆盖差集。
7. 重启后端后第三次查看相同范围；确认仍为数据库命中。
8. 在当前可变尾部验证软检查：冷却内不重复请求，权威水位推进后允许补齐。
9. 临时模拟无效会话；确认仅刷新并重试一次，失败不推进覆盖，恢复登录后可继续。
10. 使用浏览器复核 1分、15分、1小时和日K 前插及指标预热，不出现“更早行情加载失败”的旧错误。

**完成标准**

- 首次缺失范围：访问一次同源上游并原子落库。
- 同范围再次查看及服务重启后：零上游请求。
- 重叠范围：只请求时间差集。
- 当前尾部：不过度缓存，也不在冷却期重复请求。
- 历史下载期间：实时报价、当前 Bar 和 WebSocket 广播不中断。
- 数据库可查询每个来源/品种的最后权威时刻和已完成历史覆盖。
- 用户界面始终只呈现“金十统一行情”。
