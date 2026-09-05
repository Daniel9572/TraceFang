import type {
  Candle,
  CandleBackfillResult,
  ChartBarPage,
  ChartHistoryResponse,
  InstrumentEntry,
  InstrumentSourceSelection,
  QuoteView,
  ReplayFrameBounds,
  ReplayFrameCursor,
  SourceConnectionTest,
  SourceDescriptor,
  SourceId,
} from "./types";
import type {
  ExpertAiAnalysis,
  ExpertAiStatus,
  ExpertGoldEventCatalogSnapshot,
  ExpertMultiTimeframeContext,
  ExpertOptionsStatus,
  ExpertShfePositioningContext,
  ExpertVolatilityContext,
} from "./expertTypes";
import type { BarPeriodId } from "./chartPeriods";
import { BoundedBarPageCache } from "./barPageCache.ts";
import { sameCandleVersion } from "./chartModel.ts";
import type { HistoryWindow } from "./historyLoading.ts";
import { replayStreamQuery, type ReplayStreamOptions } from "./expertReplay.ts";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    cache: init?.cache ?? "no-store",
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      // Keep the HTTP fallback when an upstream proxy returns non-JSON text.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

const HISTORY_PAGE_SIZE = 1_000;
export const BACKFILL_TRANSPORT_PAGE_MINUTES = 10_000;

interface CandleRequestOptions {
  time?: number;
  count?: number;
  signal?: AbortSignal;
}

interface CandleHistoryBackfill {
  result: CandleBackfillResult;
  candles: Candle[];
  page: ChartBarPage | null;
}

function isoBoundary(
  values: readonly (string | null)[],
  selector: (left: number, right: number) => number,
): string | null {
  const timestamps = values
    .filter((value): value is string => value !== null)
    .map((value) => Date.parse(value))
    .filter(Number.isFinite);
  if (timestamps.length === 0) return null;
  return new Date(timestamps.reduce((left, right) => selector(left, right))).toISOString();
}

const BACKFILL_STATE_PRIORITY: Record<CandleBackfillResult["state"], number> = {
  cached: 0,
  joined: 1,
  advanced: 2,
  fetched: 3,
  exhausted: 4,
  deferred: 5,
};

export function aggregateBackfillResults(
  sourceId: SourceId,
  window: HistoryWindow,
  results: readonly CandleBackfillResult[],
): CandleBackfillResult {
  if (results.length === 0) {
    throw new Error("history transport returned no result");
  }
  const state = results.reduce((strictest, item) => (
    BACKFILL_STATE_PRIORITY[item.state] > BACKFILL_STATE_PRIORITY[strictest]
      ? item.state
      : strictest
  ), results[0].state);
  const evidenceVersions = [...new Set(
    results
      .map((item) => item.evidence_version)
      .filter((value): value is string => Boolean(value)),
  )].sort();
  return {
    source_id: sourceId,
    state,
    start: new Date(window.start * 1_000).toISOString(),
    end: new Date(window.end * 1_000).toISOString(),
    row_count: results.reduce((total, item) => total + item.row_count, 0),
    covered_start: isoBoundary(results.map((item) => item.covered_start), Math.min),
    covered_end: isoBoundary(results.map((item) => item.covered_end), Math.max),
    authoritative_through: isoBoundary(
      results.map((item) => item.authoritative_through),
      Math.max,
    ),
    history_floor: isoBoundary(results.map((item) => item.history_floor), Math.min),
    retry_after: isoBoundary(results.map((item) => item.retry_after), Math.max),
    evidence_version: evidenceVersions.length > 0 ? evidenceVersions.join("|") : null,
  };
}

export function backfillTransportWindows(window: HistoryWindow): HistoryWindow[] {
  const windows: HistoryWindow[] = [];
  for (
    let time = window.start;
    time < window.end;
    time += BACKFILL_TRANSPORT_PAGE_MINUTES * 60
  ) {
    const count = Math.min(
      BACKFILL_TRANSPORT_PAGE_MINUTES,
      Math.ceil((window.end - time) / 60),
    );
    windows.push({ start: time, end: Math.min(window.end, time + count * 60), count });
  }
  return windows;
}

const barPageCache = new BoundedBarPageCache();

function candleTimeKey(candle: Candle): string {
  const milliseconds = Date.parse(candle.open_time);
  return Number.isFinite(milliseconds)
    ? String(Math.floor(milliseconds / 1_000))
    : candle.open_time;
}

interface BarPageRequest {
  cursor?: string;
  before?: number;
  pageSize?: number;
  signal?: AbortSignal;
  cache?: "default" | "reload";
}

export function mergeCandleRows(...pages: Candle[][]): Candle[] {
  const rows = new Map<string, Candle>();
  for (const page of pages) {
    for (const candle of page) {
      const timeKey = candleTimeKey(candle);
      const current = rows.get(timeKey);
      if (!current) {
        rows.set(timeKey, candle);
        continue;
      }
      const stateRank = { provisional_quote: 0, provisional_authoritative: 1, final: 2 };
      const incomingWins = candle.revision > current.revision
        || (
          candle.revision === current.revision
          && stateRank[candle.state] > stateRank[current.state]
        )
        || (
          candle.revision === current.revision
          && candle.state === current.state
          && Date.parse(candle.source.received_at) > Date.parse(current.source.received_at)
        );
      if (incomingWins) rows.set(timeKey, candle);
    }
  }
  const merged = [...rows.values()].sort(
    (left, right) => new Date(left.open_time).getTime() - new Date(right.open_time).getTime(),
  );
  for (const candidate of pages) {
    if (
      candidate.length === merged.length
      && candidate.every((candle, candleIndex) => candle === merged[candleIndex])
    ) {
      return candidate;
    }
  }
  for (let index = pages.length - 1; index >= 0; index -= 1) {
    const candidate = pages[index];
    if (
      candidate.length === merged.length
      && candidate.every((candle, candleIndex) => sameCandleVersion(candle, merged[candleIndex]))
    ) {
      return candidate;
    }
  }
  return merged;
}

function fetchCandles(
  code: string,
  expectedSourceId: SourceId,
  options: CandleRequestOptions = {},
): Promise<Candle[]> {
  const params = new URLSearchParams({
    count: String(options.count ?? HISTORY_PAGE_SIZE),
  });
  if (options.time !== undefined) params.set("time", String(options.time));
  return request<Candle[]>(
    `/api/candles/${encodeURIComponent(code)}?${params.toString()}`,
    { signal: options.signal },
  )
    .then((rows) => {
      if (rows.some((row) => row.source.provider !== expectedSourceId)) {
        throw new Error("合约实时数据源已变化，请重新读取 K 线");
      }
      return rows;
    });
}

async function backfillCandleWindow(
  code: string,
  sourceId: SourceId,
  window: HistoryWindow,
  options: { revalidate?: boolean; signal?: AbortSignal } = {},
): Promise<CandleHistoryBackfill> {
  const revalidate = options.revalidate === true;
  const results: CandleBackfillResult[] = [];
  for (const transportWindow of backfillTransportWindows(window)) {
    options.signal?.throwIfAborted();
    const params = new URLSearchParams({
      time: String(transportWindow.start),
      count: String(transportWindow.count),
    });
    if (revalidate) params.set("revalidate", "true");
    const result = await request<CandleBackfillResult>(
      `/api/candles/${encodeURIComponent(code)}/backfill?${params.toString()}`,
      { method: "POST", signal: options.signal },
    );
    if (result.source_id !== sourceId) {
      throw new Error("合约实时数据源已变化，请重新读取 K 线");
    }
    results.push(result);
  }
  const result = aggregateBackfillResults(sourceId, window, results);
  return { result, candles: [], page: null };
}

async function loadOlderCandleHistory(
  code: string,
  sourceId: SourceId,
  periodId: BarPeriodId,
  cursor: string,
  countBack: number,
  signal?: AbortSignal,
): Promise<ChartHistoryResponse> {
  const params = new URLSearchParams({
    period: periodId,
    cursor,
    count_back: String(countBack),
  });
  const response = await request<ChartHistoryResponse>(
    `/api/bars/${encodeURIComponent(code)}/history?${params}`,
    { method: "POST", signal },
  );
  if (
    response.source_id !== sourceId
    || response.period_id !== periodId
    || response.page.period_id !== periodId
    || response.page.items.some((item) => item.source.provider !== sourceId)
  ) {
    throw new Error("合约实时数据源已变化，请重新读取周期 Bar");
  }
  if (response.next_cursor !== response.page.next_cursor) {
    throw new Error("历史响应包含不一致的服务端游标");
  }
  return response;
}

function revalidateCandleHistory(
  code: string,
  sourceId: SourceId,
  window: HistoryWindow,
  signal?: AbortSignal,
): Promise<CandleHistoryBackfill> {
  return backfillCandleWindow(code, sourceId, window, { revalidate: true, signal });
}

export interface ExpertAiAnalysisRequest {
  code: string;
  period: string;
  enabled_strategies: string[];
}

export const marketApi = {
  instruments: () => request<InstrumentEntry[]>("/api/instruments"),
  watchlist: () => request<InstrumentEntry[]>("/api/watchlist"),
  addToWatchlist: (code: string) =>
    request<InstrumentEntry[]>(`/api/watchlist/${encodeURIComponent(code)}`, {
      method: "POST",
    }),
  removeFromWatchlist: (code: string) =>
    request<InstrumentEntry[]>(`/api/watchlist/${encodeURIComponent(code)}`, {
      method: "DELETE",
    }),
  instrumentSource: (code: string) =>
    request<InstrumentSourceSelection>(`/api/instruments/${encodeURIComponent(code)}/source`),
  updateInstrumentSource: (code: string, sourceId: SourceId) =>
    request<InstrumentSourceSelection>(`/api/instruments/${encodeURIComponent(code)}/source`, {
      method: "PUT",
      body: JSON.stringify({ source_id: sourceId }),
    }),
  sources: (refresh = true) =>
    request<SourceDescriptor[]>(`/api/sources?refresh=${refresh ? "true" : "false"}`),
  quote: (code: string) =>
    request<QuoteView>(`/api/quotes/${encodeURIComponent(code)}`),
  lastQuote: (code: string) =>
    request<QuoteView>(`/api/quotes/${encodeURIComponent(code)}/last`),
  candles: (code: string, sourceId: SourceId, options: CandleRequestOptions = {}) =>
    fetchCandles(code, sourceId, options),
  barPage: (
    code: string,
    sourceId: SourceId,
    periodId: string,
    options: BarPageRequest = {},
  ) => {
    const { before, cursor, signal } = options;
    if (before !== undefined && cursor !== undefined) {
      return Promise.reject(new Error("周期 Bar 请求不能同时提供 cursor 和 before"));
    }
    const pageSize = options.pageSize ?? 500;
    signal?.throwIfAborted();
    const boundary = cursor !== undefined
      ? `cursor:${cursor}`
      : before !== undefined
        ? `before:${before}`
        : null;
    const cacheKey = boundary === null ? null : {
      code,
      sourceId,
      periodId,
      boundary,
      pageSize,
    };
    const cached = cacheKey && options.cache !== "reload"
      ? barPageCache.get(cacheKey)
      : undefined;
    if (cached) return Promise.resolve(cached);
    const params = new URLSearchParams({ period: periodId, page_size: String(pageSize) });
    if (cursor !== undefined) params.set("cursor", cursor);
    if (before !== undefined) params.set("before", String(before));
    return request<ChartBarPage>(
      `/api/bars/${encodeURIComponent(code)}?${params}`,
      { signal },
    ).then((page) => {
      if (page.items.some((item) => item.source.provider !== sourceId)) {
        throw new Error("合约实时数据源已变化，请重新读取周期 Bar");
      }
      if (cacheKey && page.items.length > 0) barPageCache.set(cacheKey, page);
      return page;
    });
  },
  olderCandleHistory: loadOlderCandleHistory,
  revalidateCandleHistory,
  openQuoteStream: (code: string, period: BarPeriodId = "1m") => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = new URLSearchParams({ period });
    const url = `${protocol}//${window.location.host}/api/stream/quotes/${encodeURIComponent(code)}?${params}`;
    return new WebSocket(url);
  },
  replayFrameBounds: () => request<ReplayFrameBounds>("/api/replay/frames"),
  replayFrameCursor: (sequence: number, signal?: AbortSignal) => request<ReplayFrameCursor>(
    `/api/replay/cursor?sequence=${encodeURIComponent(String(sequence))}`,
    { signal },
  ),
  openReplayStream: (
    code: string,
    options: ReplayStreamOptions,
  ) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const params = replayStreamQuery(options);
    const url = `${protocol}//${window.location.host}/api/replay/stream/${encodeURIComponent(code)}?${params}`;
    return new WebSocket(url);
  },
  testSource: (sourceId: SourceId, code: string) =>
    request<SourceConnectionTest>(
      `/api/sources/${sourceId}/test?code=${encodeURIComponent(code)}`,
      { method: "POST" },
    ),
  expertAiStatus: () => request<ExpertAiStatus>("/api/expert/ai/status"),
  expertAiAnalyze: (payload: ExpertAiAnalysisRequest) =>
    request<ExpertAiAnalysis>("/api/expert/ai/analyze", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  expertGoldEvents: () => request<ExpertGoldEventCatalogSnapshot>("/api/expert/events/gold"),
  expertGoldOptions: () => request<ExpertOptionsStatus>("/api/expert/options/gold"),
  expertVolatilityContext: () => request<ExpertVolatilityContext>("/api/expert/context/volatility"),
  expertMultiTimeframe: (code: string, asOf?: string) => {
    const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
    return request<ExpertMultiTimeframeContext>(
      `/api/expert/context/multi-timeframe/${encodeURIComponent(code)}${query}`,
    );
  },
  expertShfePositioning: (productCode: "au" | "ag") => request<ExpertShfePositioningContext>(
    `/api/expert/context/shfe-positioning/${productCode}`,
  ),
};
