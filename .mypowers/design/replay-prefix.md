# 原始回放的事实前缀与指标修订

2026-10-03 总控决定，适用 REQ-0008/0010/0012。storage_sol 负责回放事实和检查点；drawing_sol 提供共享指标恢复接口及消费端；recovery_sol 协助暴露原生 Store 的最小复用接口。当前为实施合同，不是完成证明。

回放在固定 capture epoch、构建/公式版本、来源范围和 cursor 下，维护该原始前缀实际可知的最新事实。完整事实不等于 10,000 根显示窗口，也不能从 live Store 或最终修订历史补齐。行情、指标及其输入 hash 必须指向相同前缀。

使用独立的可再生回放事实库，复用规范 codec、聚合和单 MVCC 扫描。其错误不会修改在线事实库或原始捕获。先建立从 retained 空种子重建的正确性基线；原始缺失前缀保留为 warmup/coverage 缺口。重建期间可以显示 rebuild_required，但完成后必须产出真实计算结果，不能永久停在该状态。

共享 evaluator 是按柱时间追加的状态机。收到过去已确认柱的修订或迟到补充后，记录最早受影响柱，选择此前可信指标检查点，再从当前原始前缀的事实视图流式重算时间序尾部。没有可用检查点就从该前缀可证实的起始事实重新计算。简单恢复检查点后反复喂相同倒序修订不是恢复方案。

指标重算不得改写已发生的原始事件决策或模拟成交。在事件时点语义下，迟到修订只能影响当前和后续决策，不能使用旧开盘价格补成交。现阶段历史模拟如果只支持最终修订序列，则明确采用该模式，不宣称还原原始交易时点。

旧原始帧本次导入的时间叫 imported_at，不作为当时的 accepted_at。旧 QuantBar 的 accepted_at 保持 None；原始 received_at、legacy broker_stored_at 保留独立证据。原生 accepted_at 是提交前采样，durable_confirmed_at 是提交后回执。应用序号不由这些墙钟排序。时间定位、回放逻辑知识时钟和 QuantBar.known_at 必须一致，避免当前 cursor 的柱仅因接收到接受间隔而被全部遗漏；后续 cursor 永远不可见。

旧 PG candles 的 590,826 行没有 finalized_at 列。受控 legacy 导入可以保留 final、finalized_at=None，并明确固定源快照、final_revision_history 和 finalization_time_unknown。最终修订历史分析可使用这些事实；不能因缺失旧确认钟把整表丢弃，也不能伪造确认时间。原始事件回放仍只从所选 capture 前缀重建，不能拿这些最终事实当作旧时点种子；原生实时提交的严格 finality 合同保持独立。

完整事实前缀的加速可评估独立 redb 库的 persistent savepoint，而非每 256 帧复制完整历史。已读本机 redb 4.3.0 的 transactions.rs：创建要求 clean 写事务和 Immediate；存续期间较新的废页不回收；恢复使所有更晚 savepoint 失效。若采用，相关指标检查点必须同时失效，并限制保留页、磁盘增长和会话数。此 API 不得用于回退 live Store。方法仍需速度、重启和取消实测，不能因 API 存在就称完成。

验收覆盖：旧年月 legacy 回放、原生接收与接受有延迟、同戳及回拨、早 seek/晚 seek/再次早 seek、过去 final 修订、晚到旧柱、缺失前缀、构建版本变化、长首次 seek 期间停止/新 seek/断连、取消后旧结果不发布。固定前缀重复结果和 hash 一致，并与从 retained 空种子的独立重建比较。
