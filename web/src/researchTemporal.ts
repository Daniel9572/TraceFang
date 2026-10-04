import type { Candle } from "./types";
import type { ResearchPage } from "./researchApi";

// Source text must not pass through the chart's approximate coordinates.
export function researchSourceText(value: unknown, digits = 2): string {
  if (typeof value === "string") return /^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$/.test(value) ? value : "—";
  return typeof value === "number" && Number.isFinite(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "—";
}

export function isResearchLabelOnly(page: Pick<ResearchPage, "authority_snapshot_id" | "authority_unavailable_reason"> | null): boolean {
  return !!page?.authority_unavailable_reason && !page.authority_snapshot_id;
}

// Display pages may extend source labels without creating a tradable authority.
// Same-label conflicting source prices require a fresh range, not silent overwrite.
export function mergeResearchDisplayRows(current: Candle[], incoming: Candle[]): Candle[] {
  const rows = new Map(current.map(row => [row.open_time, row]));
  for (const row of incoming) {
    const previous = rows.get(row.open_time);
    if (previous && ["open", "high", "low", "close", "volume", "open_interest"].some(key => previous[key as keyof Candle] !== row[key as keyof Candle])) {
      throw new Error("来源同一标签价格或数量存在修订，请刷新范围后继续查看。");
    }
    if (!previous) rows.set(row.open_time, row);
  }
  return [...rows.values()].sort((a, b) => a.open_time.localeCompare(b.open_time));
}
