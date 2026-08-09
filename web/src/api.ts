import type {
  Candle,
  CandleBackfillResult,
  ChartBarPage,
  InstrumentEntry,
  InstrumentSourceSelection,
  QuoteView,
  QuoteSamplePage,
  SourceConnectionTest,
  SourceDescriptor,
  SourceId,
} from "./types";
import { historyWindowBefore, type HistoryWindow } from "./historyLoading.ts";

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
const BACKFILL_TRANSPORT_PAGE_MINUTES = 10_000;

interface CandleRequestOptions {
  time?: number;
  count?: number;
}

interface CandleHistoryBackfill {
  result: CandleBackfillResult;
  candles: Candle[];
}

const candleBackfillRequests = new Map<string, Promise<CandleHistoryBackfill>>();

export function mergeCandleRows(...pages: Candle[][]): Candle[] {
  const rows = new Map<string, Candle>();
  for (const page of pages) {
    for (const candle of page) {
      const current = rows.get(candle.open_time);
      if (!current) {
        rows.set(candle.open_time, candle);
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
      if (incomingWins) rows.set(candle.open_time, candle);
    }
  }
  return [...rows.values()].sort(
    (left, right) => new Date(left.open_time).getTime() - new Date(right.open_time).getTime(),
  );
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
  return request<Candle[]>(`/api/candles/${encodeURIComponent(code)}?${params.toString()}`)
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
): Promise<CandleHistoryBackfill> {
  const cacheKey = `${sourceId}:${code}:${window.start}:${window.count}`;
  const active = candleBackfillRequests.get(cacheKey);
  if (active) return active;
  const promise = (async () => {
    const results: CandleBackfillResult[] = [];
    for (
      let time = window.start;
      time < window.end;
      time += BACKFILL_TRANSPORT_PAGE_MINUTES * 60
    ) {
      const count = Math.min(
        BACKFILL_TRANSPORT_PAGE_MINUTES,
        Math.ceil((window.end - time) / 60),
      );
      const params = new URLSearchParams({ time: String(time), count: String(count) });
      const result = await request<CandleBackfillResult>(
        `/api/candles/${encodeURIComponent(code)}/backfill?${params.toString()}`,
        { method: "POST" },
      );
      if (result.source_id !== sourceId) {
        throw new Error("合约实时数据源已变化，请重新读取 K 线");
      }
      results.push(result);
    }
    const result: CandleBackfillResult = {
      source_id: sourceId,
      state: results.some((item) => item.state === "fetched") ? "fetched" : "cached",
      start: new Date(window.start * 1_000).toISOString(),
      end: new Date(window.end * 1_000).toISOString(),
      row_count: results.reduce((total, item) => total + item.row_count, 0),
    };
    return {
      result,
      candles: [],
    };
  })();
  candleBackfillRequests.set(cacheKey, promise);
  try {
    return await promise;
  } finally {
    if (candleBackfillRequests.get(cacheKey) === promise) {
      candleBackfillRequests.delete(cacheKey);
    }
  }
}

function loadOlderCandleHistory(
  code: string,
  sourceId: SourceId,
  beforeEpochSeconds: number,
  countMinutes: number,
): Promise<CandleHistoryBackfill> {
  return backfillCandleWindow(
    code,
    sourceId,
    historyWindowBefore(beforeEpochSeconds, countMinutes),
  );
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
    before?: number,
  ) => {
    const params = new URLSearchParams({ period: periodId });
    if (before !== undefined) params.set("before", String(before));
    return request<ChartBarPage>(`/api/bars/${encodeURIComponent(code)}?${params}`).then((page) => {
      if (page.items.some((item) => item.source.provider !== sourceId)) {
        throw new Error("合约实时数据源已变化，请重新读取周期 Bar");
      }
      return page;
    });
  },
  olderCandleHistory: loadOlderCandleHistory,
  timelineSamplePage: (code: string, sourceId: SourceId, cursor?: number) => {
    const params = new URLSearchParams();
    if (cursor !== undefined) params.set("cursor", String(cursor));
    return request<QuoteSamplePage>(
      `/api/timeline/${encodeURIComponent(code)}${params.size > 0 ? `?${params}` : ""}`,
    ).then((page) => {
      if (page.items.some((item) => item.source_id !== sourceId)) {
        throw new Error("合约实时数据源已变化，请重新读取分时事件");
      }
      return page;
    });
  },
  openQuoteStream: (code: string) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/api/stream/quotes/${encodeURIComponent(code)}`;
    return new WebSocket(url);
  },
  testSource: (sourceId: SourceId, code: string) =>
    request<SourceConnectionTest>(
      `/api/sources/${sourceId}/test?code=${encodeURIComponent(code)}`,
      { method: "POST" },
    ),
};
