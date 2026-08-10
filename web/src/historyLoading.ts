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
export const MAX_EMPTY_HISTORY_ADVANCE_MINUTES = 7 * 24 * 60;

export type HistoryLoadOutcome = {
  state: "loaded" | "advanced" | "exhausted" | "busy" | "failed";
  added: number;
  advancedMinutes: number;
};

export interface HistoryDemandResolution {
  active: boolean;
  emptyAdvanceMinutes: number;
}

export function resolveHistoryDemandOutcome(
  emptyAdvanceMinutes: number,
  outcome: HistoryLoadOutcome,
): HistoryDemandResolution {
  if (outcome.state === "loaded") {
    return { active: true, emptyAdvanceMinutes: 0 };
  }
  if (outcome.state === "advanced") {
    const nextEmptyAdvance = emptyAdvanceMinutes + Math.max(0, outcome.advancedMinutes);
    return {
      active: nextEmptyAdvance < MAX_EMPTY_HISTORY_ADVANCE_MINUTES,
      emptyAdvanceMinutes: nextEmptyAdvance,
    };
  }
  if (outcome.state === "busy") {
    return { active: true, emptyAdvanceMinutes };
  }
  return { active: false, emptyAdvanceMinutes };
}

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

export function historyGapWindow(
  firstMissingEpochSeconds: number,
  missingDurationSeconds: number,
): HistoryWindow {
  const start = Math.floor(firstMissingEpochSeconds / 60) * 60;
  const count = Math.max(1, Math.ceil(missingDurationSeconds / 60));
  return {
    start,
    end: start + count * 60,
    count,
  };
}

export function historyCursorEpoch(
  nextBefore: string | null,
  firstOpenTime: string | null | undefined,
): number | null {
  for (const value of [nextBefore, firstOpenTime]) {
    if (!value) continue;
    const seconds = Math.floor(Date.parse(value) / 1_000);
    if (Number.isFinite(seconds)) return seconds;
  }
  return null;
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

function historyEdgeThreshold(range: VisibleLogicalRange): number {
  const visibleBars = Math.max(1, range.to - range.from);
  return Math.max(4, Math.min(24, visibleBars * 0.12));
}

export function isNearOlderHistoryEdge(
  range: VisibleLogicalRange | null,
  dataLength: number,
): boolean {
  return Boolean(range && dataLength > 0 && range.from <= historyEdgeThreshold(range));
}

export function shouldActivateOlderHistoryDemand(
  range: VisibleLogicalRange | null,
  dataLength: number,
  userInitiated: boolean,
): boolean {
  if (!range || !isNearOlderHistoryEdge(range, dataLength)) return false;
  const wholeLoadedSeriesIsVisible = range.to >= dataLength - 1;
  return userInitiated || wholeLoadedSeriesIsVisible;
}

export function shouldRequestOlderHistory(
  range: VisibleLogicalRange | null,
  dataLength: number,
  loading: boolean,
  demandActive: boolean,
): boolean {
  return !loading && demandActive && isNearOlderHistoryEdge(range, dataLength);
}
