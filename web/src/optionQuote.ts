/** Source values remain decimal text; only scenario inputs cross the model's float boundary. */
export function optionSourceText(value: string | number | null | undefined): string {
  return value == null ? "—" : String(value);
}
export function positiveOptionSource(value: string | number | null | undefined): boolean {
  if (typeof value === "number") return Number.isFinite(value) && value > 0;
  return typeof value === "string" && /^\+?\d+(?:\.\d+)?$/.test(value) && /[1-9]/.test(value);
}
export function optionScenarioNumber(value: string | number | null | undefined): number | null {
  if (value == null || (typeof value === "string" && !/^[+-]?\d+(?:\.\d+)?$/.test(value))) return null;
  const approximate = Number(value);
  return Number.isFinite(approximate) && approximate >= 0 ? approximate : null;
}

export interface OptionDayDisplay {
  price_semantics?: "official_daily_close";
  daily_close?: string | null;
  settlement?: string | null;
  last?: string | number | null;
  source_date?: string;
  source_received_at?: string;
}
export function optionDayPriceText(value: OptionDayDisplay): string {
  return value.price_semantics === "official_daily_close"
    ? `日收盘 ${optionSourceText(value.daily_close)} / 日结算 ${optionSourceText(value.settlement)}`
    : `最后 ${optionSourceText(value.last)}`;
}
export function optionDayClockText(value: OptionDayDisplay): string | null {
  return value.price_semantics === "official_daily_close"
    ? `历史报告日期 ${value.source_date ?? "未知"}（日精度） · 来源获取 ${value.source_received_at ?? "未知"}；不是成交时刻`
    : null;
}
