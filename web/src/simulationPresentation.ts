const reasons: Record<string,string> = {
  "cancelled":"运行已取消；保留已发布的部分账本，不作为完整结果",
  "requires at least 30 daily close returns":"至少需要 30 个每日收盘收益样本",
  "UTC calendar daily samples have gaps or nonpositive prior equity; no trading-calendar annual factor is inferred":"每日样本存在缺口或前一期权益不为正，无法可靠计算；未猜测交易日年化因子",
  "daily return variance is zero":"每日收益没有波动，Sharpe 分母为零",
  "requires a full 365 elapsed calendar days":"需要覆盖至少 365 个自然日",
  "nonpositive final equity cannot be compounded":"末期权益不为正，无法计算复利年化",
  "compounding exponent exceeds the supported statistics domain":"复利计算超出已验证的统计范围",
  "no profitable closed trades":"尚无盈利的已平仓交易",
  "no losing closed trades":"尚无亏损的已平仓交易",
  "average loss is zero":"平均亏损为零，无法计算此比值",
  "requires both profitable and losing closed trades":"需要同时有盈利与亏损的已平仓交易",
  "no closed trades":"尚无已平仓交易",
  "no losing trades; profit factor has no finite denominator":"没有亏损交易，利润因子的分母为零",
  "strategy had no feasible fill; no comparable entry point":"策略没有可成交记录，缺少可比的基准入场点",
  "a later confirmed decision superseded this intention before a available open":"下一可用开盘前，较新的确认信号替代了该意向",
  "absolute opening notional plus fee exceeds configured equity/initial-capital exposure limit":"开仓名义金额与费用超过本次配置的权益或初始资金敞口限制",
  "no subsequent available bar open in the selected range":"所选范围内没有下一根可用柱的开盘，意向未成交",
};
export function simulationReason(value: string | null | undefined): string {
  return value ? reasons[value] ?? value : "";
}
