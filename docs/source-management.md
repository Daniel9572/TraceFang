# 数据来源管理

## 路由规则

- 每个来源声明能力：`quote / candles / catalog / news / calendar`。
- `auto` 只考虑已启用且具有目标能力的来源，按数值较小的优先级先调用。
- 只有 `ProviderError` 类的预期提供方错误会触发回退；代码错误会直接暴露。
- 显式指定来源时不回退。
- 启停与优先级原子写入 `data/sources.json`。

默认顺序：

1. `jin10_mcp`，优先级 `10`；
2. `jin10_desktop`，优先级 `20`。

可在页面的“数据来源管理”中把本地软件源设为自动优先，以节约官方调用额度。

## 本地 API

- `GET /api/sources`：来源、能力、配置和实时健康状态；
- `PATCH /api/sources/{source_id}`：更新 `enabled` 和/或 `priority`；
- `GET /api/quotes/{code}?source=auto|jin10_mcp|jin10_desktop`：读取报价；
- `GET /api/quotes/{code}/compare`：独立调用启用的报价源并计算偏差；
- `GET /api/candles/{code}?source=auto|jin10_mcp`：读取分钟 K 线。

比较结果中的 `sample_age_seconds` 是可观测时间的新鲜度；桌面 OCR 的可观测时间是截图采样时间，不能表示行情供应商内部延迟。

## 调用额度

官方 MCP 每个用户、每个工具、北京时间自然日最多 1500 次。页面默认：

- 官方报价约每 65 秒刷新一次；
- 本地桌面报价约每 5 秒刷新一次；
- 分钟 K 线约每 65 秒刷新一次；
- 双源比较只在用户打开或手动刷新时调用。

这些间隔让单个活跃页面保持在每日工具额度内；多开页面会分别产生调用，应通过后端共享缓存或后续的集中调度器进一步约束。

页面用最近一分钟 K 线作为基线，并把所选报价源的新报价合并进当前柱。收盘倒计时完全在浏览器本地计算，不消耗 MCP 调用额度；报价刷新速度仍受上述来源采样间隔约束。
