# Market Analysis Platform

面向多市场、多品种和多数据源的交易分析平台。第一阶段用现货黄金与现货白银验证采集、来源管理和 K 线终端；系统不把分析逻辑绑定到金十、MCP、桌面 OCR 或具体品种。

## 第一版已经具备

- 自有品牌的桌面行情工作台：品种观察、实时报价、当前 K 线实时生长、收盘倒计时、当前价聚焦、缩放/平移与 1/5/15/30/60 分钟聚合；
- 两套独立行情来源，默认本地优先，可自动回退或手动强制；
- 来源管理：启停、优先级、能力、健康状态、运行要求和连通测试；
- 金十官方 MCP：品种目录、报价、分钟 K 线、快讯、文章和财经日历；
- 本地金十软件：通过窗口截图和 Windows OCR 读取黄金、白银报价；
- 通用领域模型、能力端口和适配器边界，方便继续接入期货、股票、外汇、加密资产和其他供应商。

## 两套来源

| 来源 | 首版能力 | 优点 | 运行要求 |
| --- | --- | --- | --- |
| 金十官方 MCP | 报价、分钟 K 线、目录、资讯、日历 | 结构化、字段完整、可验证 | 配置 Bearer Token；不需要启动金十软件 |
| 本地金十软件 | XAUUSD/XAGUSD 实时报价 | 成本低，普通分析够用 | Windows 上启动金十软件，打开行情页，窗口不能最小化；被其他窗口遮挡不影响 |

当前本地适配器只做窗口采样，不拦截 HTTPS、不安装证书、不绕过鉴权，也不会从蜡烛图像素反推 K 线。后续加速路线会作为独立适配器验证，见 [docs/local-data-acceleration.md](docs/local-data-acceleration.md)。

## 快速开始

首次运行，双击 `setup.cmd` 安装并构建。只使用本地软件源时，之后可直接双击 `start.cmd`。

如需官方 MCP 与 K 线，推荐复制本机环境文件模板，填写 `JIN10_MCP_BEARER_TOKEN` 后再启动：

```powershell
Copy-Item .env.example .env
# 用文本编辑器填写 .env；不要把真实 Token 写回 .env.example
.\start.cmd
```

程序启动时自动读取项目根目录 `.env`；PowerShell 或系统中已经存在的同名环境变量优先，不会被 `.env` 覆盖。若只想让 Token 在当前 PowerShell 会话临时生效，也可以不创建 `.env`，继续使用 `$env:JIN10_MCP_BEARER_TOKEN = "<your-token>"`。

浏览器会打开 `http://127.0.0.1:8000`。也可以完全从终端安装和启动：

```powershell
uv sync --python 3.13
corepack pnpm -C web install --frozen-lockfile
corepack pnpm -C web build
uv run market-analysis-server
```

真实 Token 不得写入 `.env` 以外的非忽略文件、代码、测试、日志或 Git 历史。若 Token 曾粘贴到聊天或其他可留存位置，建议在服务端重新生成。

## 数据源使用方式

- `本地优先（自动回退）`：默认先读本地金十软件；只有明确的来源错误才会回退到官方 MCP。
- `强制来源`：只调用选中的来源，失败时直接报告，不暗中换源。
- `测试连接`：在来源管理中逐个主动测试，绕过报价缓存并显示价格、耗时和采样时间；测试官方源会明确消耗一次官方报价调用。
- K 线首版仅来自官方 MCP。接口最多返回 100 根一分钟 K 线，页面的 5/15/30/60 分钟由真实分钟数据聚合；不会伪造更长历史。
- 当前柱在浏览器中用“最近一分钟 K 线 + 当前报价”合成：同一周期内只更新真实报价触达后的收盘/最高/最低，跨周期后由下一笔报价开启新柱。倒计时每秒更新，不额外调用数据接口。
- 本地软件报价约 5 秒采样一次；官方 MCP 报价约 65 秒采样一次。因此“实时绘制”表示按所选来源的实际采样节奏更新，不表示逐笔成交行情。

来源配置保存在 `data/sources.json`，不会进入 Git。接口契约见 [docs/source-management.md](docs/source-management.md)。

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
