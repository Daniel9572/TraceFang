import type { ChartPeriod } from "./chartPeriods";

export interface HistoryWindow {
  start: number;
  end: number;
  count: number;
}

export interface VisibleLogicalRange {
  from: number;
  to: number;
}

const TARGET_BARS_PER_BATCH = 240;

function calendarPeriodMinutes(period: ChartPeriod): number {
  if (period.aggregation.kind !== "calendar") return period.aggregation.minutes;
  switch (period.aggregation.unit) {
    case "day": return 24 * 60;
    case "week": return 7 * 24 * 60;
    case "month": return 31 * 24 * 60;
    case "quarter": return 92 * 24 * 60;
    case "year": return 366 * 24 * 60;
  }
}

export function historyBatchMinutes(period: ChartPeriod): number {
  const periodMinutes = calendarPeriodMinutes(period);
  return Math.max(
    1,
    Math.ceil(
      period.aggregation.kind === "calendar"
        ? periodMinutes
        : periodMinutes * TARGET_BARS_PER_BATCH,
    ),
  );
}

export function historyWindowBefore(
  beforeEpochSeconds: number,
  countMinutes: number,
): HistoryWindow {
  const end = Math.floor(beforeEpochSeconds / 60) * 60;
  const count = Math.max(1, Math.floor(countMinutes));
  return {
    start: end - count * 60,
    end,
    count,
  };
}

export function prependedPointCount(
  previousFirstTime: number | null,
  nextFirstTime: number | null,
  previousIndex: number,
): number {
  if (
    previousFirstTime === null
    || nextFirstTime === null
    || nextFirstTime >= previousFirstTime
    || previousIndex <= 0
  ) return 0;
  return previousIndex;
}

export function shouldRequestOlderHistory(
  range: VisibleLogicalRange | null,
  dataLength: number,
  loading: boolean,
  userInitiated: boolean,
): boolean {
  if (!range || dataLength <= 0 || loading || !userInitiated) return false;
  const visibleBars = Math.max(1, range.to - range.from);
  const edgeThreshold = Math.max(4, Math.min(24, visibleBars * 0.12));
  return range.from <= edgeThreshold;
}
