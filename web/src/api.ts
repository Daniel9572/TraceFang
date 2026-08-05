import type {
  Candle,
  InstrumentEntry,
  QuoteComparison,
  QuoteSnapshot,
  SourceDescriptor,
  SourceId,
} from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
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

export const marketApi = {
  instruments: () => request<InstrumentEntry[]>("/api/instruments"),
  sources: () => request<SourceDescriptor[]>("/api/sources"),
  quote: (code: string, source: SourceId) =>
    request<QuoteSnapshot>(`/api/quotes/${code}?source=${encodeURIComponent(source)}`),
  candles: (code: string) => request<Candle[]>(`/api/candles/${code}?source=auto&count=100`),
  compare: (code: string) => request<QuoteComparison>(`/api/quotes/${code}/compare`),
  updateSource: (
    sourceId: Exclude<SourceId, "auto">,
    update: { enabled?: boolean; priority?: number },
  ) =>
    request<SourceDescriptor>(`/api/sources/${sourceId}`, {
      method: "PATCH",
      body: JSON.stringify(update),
    }),
};
