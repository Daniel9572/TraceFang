# 历史数据治理

## 当前边界

- 历史数据的首要粒度是渠道实际交付的每一次报价事件。
- `QUOTE_EVENT` 与 `BAR_OBSERVATION` 分开管理。K 线不能反推、插值或伪造成逐报价事件。
- 每个数据集只属于一个明确渠道：`provider_family + channel_id + feed_id`。
- 这里的“渠道”是历史证据的物理来源身份，不是用户可选择的逻辑数据源。逻辑源可以消费多个渠道，但历史记录仍按渠道分别登记和校验。
- 不在不同渠道之间拼字段、补字段、平均价格或生成“组合行情”。
- 历史查询只读取本地 PostgreSQL 可信视图，不触发数据提供方请求。
- 实时采集路径不参与历史导入、批量校验或交叉比对。
- MCP 当前完全排除，不作为实时、历史、补缺或校验数据源。

## 数据身份与精度

每个历史数据集必须记录以下身份：

- 标准品种、标的、计价币种、市场类型、场所和合约代码；
- 供应商家族、具体渠道、Feed 和独立性分组；
- 记录粒度；
- 报价字段或 K 线价格口径；
- 原始时区、UTC 标准化、时间语义和时间精度；
- 文件存储小数位和实际观测价格量子；
- 原始工件校验和、解析器版本和来源地址。

“同一供应商的两个渠道”仍是两个不同渠道，数据不得混合；但它们属于同一独立性分组，不能互相充当独立信源确认。

## 准入流程

```text
原始文件或原始事件
  -> 校验文件长度和 SHA-256
  -> 解析为单渠道候选记录
  -> 校验结构、时间、价格精度、顺序和重复
  -> 判断信源语义是否可比
  -> 进行独立、同语义交叉校验
  -> 形成有证据编号的准入决定
  -> 选择一个完整数据集作为某时间段的唯一来源
  -> 本地可信视图
```

校验分为两类：

1. `EXACT`：品种、市场、合约、记录粒度、价格口径和时间语义一致，可用于独立确认。
2. `SANITY_ONLY`：例如现货 Bid 与期货 Close、逐报价与分钟 K 线，只能检查大方向和数量级，不能用于可信准入。

默认准入策略至少要求一个来自不同独立性分组的 `EXACT` 通过证据。单一来源即使文件完整、OHLC 正确，也只能进入 `validated_candidate`。

## PostgreSQL 隔离结构

历史治理使用独立的 `history` schema：

- `datasets`：数据集身份、精度和状态；
- `artifacts`：文件长度、SHA-256 和发布者认证信息；
- `quote_events`：逐次交付的渠道报价，使用 `event_index` 保留同一来源秒内的多帧；
- `bar_observations`：外部 K 线候选，不与逐报价表混放；
- `validation_runs` / `validation_findings`：多步骤校验结果；
- `cross_validations`：同语义或合理性比对证据；
- `admission_decisions`：可审计准入结果；
- `canonical_segments`：某一时间段完整引用一个已准入数据集，不复制或融合字段；
- `trusted_quote_events` / `trusted_bar_observations`：仅包含可信时间段的本地只读视图。

状态流转为：

```text
registered -> ingested -> validated_candidate -> trusted
                                    |
                                    -> quarantined
```

## 已登记的 HistData 2026-07 候选

| 品种 | 真实语义 | 记录数 | 时间精度 | CSV 小数位 | 观测价格量子 | 当前状态 |
|---|---|---:|---:|---:|---:|---|
| XAUUSD | OTC 现货 Bid M1 OHLC | 31,138 | 60 秒 | 6 | 0.001 | `validated_candidate` |
| XAGUSD | OTC 现货 Bid M1 OHLC | 31,040 | 60 秒 | 6 | 0.001 | `validated_candidate` |

说明：

- 原始数据按固定 EST（UTC-05:00，不随夏令时变化）解释，标准化文件为 UTC。
- 文件长度和 manifest 中 SHA-256 已通过；CSV 结构、OHLC、排序、重复、时间对齐和价格量子已通过。
- manifest 没有发布者数字签名，因此校验和只能证明本地数据包一致，不能单独证明发布者身份。
- 黄金覆盖率约 99.232%，白银约 98.920%；缺口被保留，没有生成合成 K 线。
- Yahoo COMEX 期货仅属于不同市场、不同价格口径的合理性参考，不构成同语义确认。
- 目前可信报价视图、可信 K 线视图和标准时间段均为空；需要独立的同语义数据后才可准入。

## 离线导入命令

仅校验，不写数据库：

```powershell
uv run python scripts/import_histdata_history.py
```

将通过离线校验的数据写为候选：

```powershell
uv run python scripts/import_histdata_history.py --apply
```

重复执行不会覆盖已有不可变数据集。
