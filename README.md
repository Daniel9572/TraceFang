# Market Analysis Platform

面向多市场、多品种和多数据源的交易分析平台。第一阶段用现货黄金与现货白银验证采集、来源管理和 K 线终端；系统不把分析逻辑绑定到金十、MCP、桌面 OCR 或具体品种。

## 第一版已经具备

- 自有品牌的桌面行情工作台：品种观察、实时报价、K 线缩放/平移、1/5/15/30/60 分钟聚合；
- 两套独立行情来源，可自动回退、手动强制和并排比较；
- 来源管理：启停、优先级、能力、健康状态、运行要求和连通测试；
- 金十官方 MCP：品种目录、报价、分钟 K 线、快讯、文章和财经日历；
- 本地金十软件：通过窗口截图和 Windows OCR 读取黄金、白银报价；
- 通用领域模型、能力端口和适配器边界，方便继续接入期货、股票、外汇、加密资产和其他供应商。

## 两套来源

| 来源 | 首版能力 | 优点 | 运行要求 |
| --- | --- | --- | --- |
| 金十官方 MCP | 报价、分钟 K 线、目录、资讯、日历 | 结构化、字段完整、可验证 | 配置 Bearer Token；不需要启动金十软件 |
| 本地金十软件 | XAUUSD/XAGUSD 实时报价 | 成本低，普通分析够用 | Windows 上启动金十软件，打开行情页，窗口不能最小化；被其他窗口遮挡不影响 |

本地来源不会拦截 HTTPS、安装证书、绕过鉴权或逆向私有协议，也不会从蜡烛图像素反推 K 线。详细边界见 [docs/jin10-desktop.md](docs/jin10-desktop.md)。

## 快速开始

首次运行，双击 `setup.cmd` 安装并构建。只使用本地软件源时，之后可直接双击 `start.cmd`。

如需官方 MCP 与 K 线，请在 PowerShell 中设置 Token，并在**同一个窗口**启动：

```powershell
$env:JIN10_MCP_BEARER_TOKEN = "<your-token>"
.\start.cmd
```

浏览器会打开 `http://127.0.0.1:8000`。也可以完全从终端安装和启动：

```powershell
uv sync --python 3.13
corepack pnpm -C web install --frozen-lockfile
corepack pnpm -C web build
uv run market-analysis-server
```

真实 Token 不得写入 `.env` 以外的非忽略文件、代码、测试、日志或 Git 历史。若 Token 曾粘贴到聊天或其他可留存位置，建议在服务端重新生成。

## 数据源使用方式

- `自动选择`：按来源管理中的优先级调用；只有明确的来源错误才会回退。
- `强制来源`：只调用选中的来源，失败时直接报告，不暗中换源。
- `双源对比`：分别读取两套报价，展示采样时间、请求耗时、绝对偏差和百分比偏差。
- K 线首版仅来自官方 MCP。接口最多返回 100 根一分钟 K 线，页面的 5/15/30/60 分钟由真实分钟数据聚合；不会伪造更长历史。

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
