import type { ChartBarPage, ChartHistorySourceStatus } from "./types";

export interface HistoryWindow {
  start: number;
  end: number;
  count: number;
}

export interface VisibleLogicalRange {
  from: number;
  to: number;
}

export const TARGET_BARS_PER_BATCH = 240;
export const HISTORY_LOADING_INDICATOR_DELAY_MS = 150;

export interface HistoryDemand {
  visibleBars: number;
  indicatorWarmupBars: number;
  reason: "left-edge" | "initial-fill";
}

export type HistoryLoadOutcome = {
  state: "loaded" | "advanced" | "exhausted" | "busy" | "failed";
  added: number;
  advancedMinutes: number;
  retryAfterMs?: number;
};

export interface HistoryDemandResolution {
  active: boolean;
  emptyAdvanceMinutes: number;
  retryAfterMs?: number;
}

export function nextHistoryDemandEvaluationDelay(
  outcome: HistoryLoadOutcome,
  resolution: HistoryDemandResolution,
): number | null {
  if (!resolution.active || outcome.state === "loaded") return null;
  if (outcome.state === "busy") return resolution.retryAfterMs ?? 100;
  return null;
}

export function canBackfillOlderHistory(
  instrumentBackfillSupported: boolean,
  sourceBackfillConfigured: boolean | null | undefined,
): boolean {
  return instrumentBackfillSupported && sourceBackfillConfigured === true;
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
      // A server-confirmed cursor advance without a new logical Bar completes
      // this browser demand. A later real drag can explicitly request the next
      // bounded step; chart redraws must not form a request waterfall.
      active: false,
      emptyAdvanceMinutes: nextEmptyAdvance,
      retryAfterMs: outcome.retryAfterMs,
    };
  }
  if (outcome.state === "busy") {
    return {
      active: true,
      emptyAdvanceMinutes,
      retryAfterMs: outcome.retryAfterMs,
    };
  }
  return { active: false, emptyAdvanceMinutes };
}

export function historyDemandBars(
  visibleBars = 0,
  indicatorWarmupBars = 0,
): number {
  const requestedBars = Math.max(
    TARGET_BARS_PER_BATCH,
    Math.ceil(Math.max(0, visibleBars)),
    Math.ceil(Math.max(0, indicatorWarmupBars)),
  );
  return Math.min(10_000, Math.max(1, requestedBars));
}

export function enabledIndicatorWarmupBars(
  indicatorIds: readonly ("rsi" | "kdj" | "macd")[],
): number {
  const warmups = { rsi: 14, kdj: 9, macd: 35 } as const;
  return indicatorIds.reduce((maximum, indicatorId) => (
    Math.max(maximum, warmups[indicatorId])
  ), 0);
}

export function enabledStrategyWarmupBars(strategyIds: readonly string[]): number {
  const warmups: Readonly<Record<string, number>> = {
    "price-structure": 160,
    "ma-structure": 250,
    macd: 35,
    kdj: 9,
    rsi: 14,
    bollinger: 120,
    "nine-count": 13,
    momentum: 120,
    "trend-slope": 30,
    "volume-price": 30,
    "smart-money": 160,
  };
  return strategyIds.reduce((maximum, strategyId) => (
    Math.max(maximum, warmups[strategyId] ?? 0)
  ), 0);
}

function epochSeconds(value: string | null | undefined): number | null {
  if (!value) return null;
  const seconds = Math.floor(Date.parse(value) / 1_000);
  return Number.isFinite(seconds) ? seconds : null;
}

function retryDelayMilliseconds(
  retryAfter: string | null | undefined,
  nowMilliseconds: number,
): number {
  if (!retryAfter) return 100;
  const retryAt = Date.parse(retryAfter);
  if (!Number.isFinite(retryAt)) return 100;
  return Math.max(0, retryAt - nowMilliseconds);
}

export interface HistoryPageCursor {
  token: string;
  before: number;
}

export interface ChartHistoryStepResolution {
  nextCursor: HistoryPageCursor | null;
  outcome: HistoryLoadOutcome;
}

export function historyPageCursor(
  page: Pick<ChartBarPage, "next_before" | "next_cursor">,
): HistoryPageCursor | null {
  if (!page.next_cursor) return null;
  const before = epochSeconds(page.next_before);
  return before === null ? null : { token: page.next_cursor, before };
}

export function resolveChartHistoryStep({
  currentCursor,
  page,
  localAdded,
  sourceStatus,
  retryAfter,
  nowMilliseconds = Date.now(),
}: {
  currentCursor: HistoryPageCursor;
  page: Pick<ChartBarPage, "next_before" | "next_cursor">;
  localAdded: number;
  sourceStatus: ChartHistorySourceStatus;
  retryAfter: string | null;
  nowMilliseconds?: number;
}): ChartHistoryStepResolution {
  const nextCursor = historyPageCursor(page);
  if (
    nextCursor !== null
    && (
      nextCursor.token === currentCursor.token
      || nextCursor.before >= currentCursor.before
    )
  ) {
    throw new Error("服务端周期 Bar 游标未前进");
  }
  const advancedMinutes = nextCursor === null
    ? 0
    : Math.max(0, Math.ceil((currentCursor.before - nextCursor.before) / 60));

  if (localAdded > 0) {
    return {
      nextCursor,
      outcome: {
        state: "loaded",
        added: Math.max(0, localAdded),
        advancedMinutes,
      },
    };
  }

  if (sourceStatus === "exhausted" || sourceStatus === "unsupported") {
    return {
      nextCursor: null,
      outcome: { state: "exhausted", added: 0, advancedMinutes: 0 },
    };
  }
  if (sourceStatus === "deferred") {
    return {
      nextCursor: currentCursor,
      outcome: {
        state: "busy",
        added: 0,
        advancedMinutes: 0,
        retryAfterMs: retryDelayMilliseconds(retryAfter, nowMilliseconds),
      },
    };
  }

  if (nextCursor !== null) {
    return {
      nextCursor,
      outcome: {
        state: "advanced",
        added: 0,
        advancedMinutes,
      },
    };
  }

  return {
    nextCursor: currentCursor,
    outcome: {
      state: "busy",
      added: 0,
      advancedMinutes: 0,
      retryAfterMs: retryDelayMilliseconds(retryAfter, nowMilliseconds),
    },
  };
}

export function shouldShowHistoryLoading(
  startedAtMilliseconds: number,
  nowMilliseconds: number,
): boolean {
  return nowMilliseconds - startedAtMilliseconds >= HISTORY_LOADING_INDICATOR_DELAY_MS;
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
  confirmedAdvanceMinutes = 0,
): boolean {
  if (!range || !isNearOlderHistoryEdge(range, dataLength)) return false;
  const wholeLoadedSeriesIsVisible = range.to >= dataLength - 1;
  return userInitiated || (wholeLoadedSeriesIsVisible && confirmedAdvanceMinutes <= 0);
}

export function historyDemandFor(
  range: VisibleLogicalRange,
  dataLength: number,
  userInitiated: boolean,
  indicatorWarmupBars: number,
): HistoryDemand {
  const visibleBars = Math.max(1, Math.ceil(range.to - range.from + 1));
  const wholeLoadedSeriesIsVisible = range.to >= dataLength - 1;
  return {
    visibleBars,
    indicatorWarmupBars: Math.max(0, Math.ceil(indicatorWarmupBars)),
    reason: !userInitiated && wholeLoadedSeriesIsVisible ? "initial-fill" : "left-edge",
  };
}

export function shouldRequestOlderHistory(
  range: VisibleLogicalRange | null,
  dataLength: number,
  loading: boolean,
  demandActive: boolean,
): boolean {
  return !loading && demandActive && isNearOlderHistoryEdge(range, dataLength);
}
