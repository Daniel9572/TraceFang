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

const MIN_HISTORY_BATCH_MINUTES = 48 * 60;
export const MAX_HISTORY_BATCH_MINUTES = 10_000;
const TARGET_BARS_PER_BATCH = 240;

export function historyBatchMinutes(period: ChartPeriod): number {
  if (period.aggregation.kind === "calendar") return MAX_HISTORY_BATCH_MINUTES;
  return Math.min(
    MAX_HISTORY_BATCH_MINUTES,
    Math.max(
      MIN_HISTORY_BATCH_MINUTES,
      Math.ceil(period.aggregation.minutes * TARGET_BARS_PER_BATCH),
    ),
  );
}

export function historyWindowBefore(
  beforeEpochSeconds: number,
  countMinutes: number,
): HistoryWindow {
  const end = Math.floor(beforeEpochSeconds / 60) * 60;
  const count = Math.min(
    MAX_HISTORY_BATCH_MINUTES,
    Math.max(1, Math.floor(countMinutes)),
  );
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