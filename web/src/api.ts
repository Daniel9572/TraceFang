import type {
  Candle,
  InstrumentEntry,
  InstrumentSourceSelection,
  QuoteView,
  SourceConnectionTest,
  SourceDescriptor,
  SourceId,
} from "./types";

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

const HISTORY_LOOKBACK_MINUTES = 23 * 60;
const HISTORY_PAGE_SIZE = 100;
const HISTORY_CACHE_MILLISECONDS = 30 * 60 * 1_000;

interface CandleRequestOptions {
  time?: number;
  count?: number;
}

interface CandleHistoryCacheEntry {
  expiresAt: number;
  promise: Promise<Candle[]>;
}

const candleHistoryCache = new Map<string, CandleHistoryCacheEntry>();

export function mergeCandleRows(...pages: Candle[][]): Candle[] {
  const rows = new Map<string, Candle>();
  for (const page of pages) {
    for (const candle of page) rows.set(candle.open_time, candle);
  }
  return [...rows.values()].sort(
    (left, right) => new Date(left.open_time).getTime() - new Date(right.open_time).getTime(),
  );
}

function fetchCandles(
  code: string,
  options: CandleRequestOptions = {},
): Promise<Candle[]> {
  const params = new URLSearchParams({
    count: String(options.count ?? HISTORY_PAGE_SIZE),
  });
  if (options.time !== undefined) params.set("time", String(options.time));
  return request<Candle[]>(`/api/candles/${encodeURIComponent(code)}?${params.toString()}`);
}

async function fetchCandleHistory(code: string): Promise<Candle[]> {
  const cacheKey = code;
  const now = Date.now();
  const cached = candleHistoryCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.promise;

  const promise = (async () => {
    const endMinute = Math.floor(now / 60_000) * 60;
    const recentWindowStart = endMinute - HISTORY_PAGE_SIZE * 60;
    const historyStart = endMinute - HISTORY_LOOKBACK_MINUTES * 60;
    const requests: Array<{ time: number; count: number }> = [];

    for (let time = historyStart; time < recentWindowStart; time += HISTORY_PAGE_SIZE * 60) {
      requests.push({
        time,
        count: Math.min(HISTORY_PAGE_SIZE, Math.ceil((recentWindowStart - time) / 60)),
      });
    }

    const pages = await Promise.all(
      requests.map((options) => fetchCandles(code, options)),
    );
    return mergeCandleRows(...pages);
  })();

  candleHistoryCache.set(cacheKey, {
    expiresAt: now + HISTORY_CACHE_MILLISECONDS,
    promise,
  });
  try {
    return await promise;
  } catch (error) {
    if (candleHistoryCache.get(cacheKey)?.promise === promise) {
      candleHistoryCache.delete(cacheKey);
    }
    throw error;
  }
}

export const marketApi = {
  instruments: () => request<InstrumentEntry[]>("/api/instruments"),
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
  candles: fetchCandles,
  candleHistory: fetchCandleHistory,
  openQuoteStream: (code: string) => {
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${protocol}//${window.location.host}/api/stream/quotes/${encodeURIComponent(code)}`;
    return new WebSocket(url);
  },
  testSource: (sourceId: SourceId) =>
    request<SourceConnectionTest>(`/api/sources/${sourceId}/test`, { method: "POST" }),
};
