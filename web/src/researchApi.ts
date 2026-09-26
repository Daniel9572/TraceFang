import type { Candle } from "./types";

export type ResearchAsset = "equity" | "etf" | "index" | "future" | "option";
export type ResearchSourceId =
  | "eastmoney"
  | "sina"
  | "tushare"
  | "alpaca"
  | "tencent";
export interface ResearchInstrument {
  source: ResearchSourceId;
  symbol: string;
  name: string;
  asset: ResearchAsset;
  currency: string;
  expiry?: string | null;
}
export interface ResearchSource {
  id: ResearchSourceId;
  name: string;
  assets: ResearchAsset[];
  periods: string[];
  credentials: string[];
  configured: boolean;
  market: string;
  note: string;
  url: string;
  mode: string;
  diagnostic: { state: string; checked_at: string; detail: string } | null;
}
export interface ResearchQuery {
  source: ResearchSourceId;
  symbol: string;
  asset: ResearchAsset;
  period: string;
  adjustment: "raw" | "forward" | "backward";
  limit?: number;
  before?: string | null;
}
export interface ResearchPage {
  query: ResearchQuery;
  items: Candle[];
  next_before: string | null;
  cache_state: "fresh" | "cached" | "stale";
  data_as_of: string | null;
  fetched_at: string;
  warnings: string[];
  rejected_rows: number;
  volume_unit: string;
  currency: string;
  feed: string;
  frequency: string;
  empty_reason: string | null;
}
export interface ResearchJob {
  id: string;
  state: "loading" | "analyzing" | "completed" | "failed" | "cancelled";
  stage: string;
  query: ResearchQuery;
  snapshot_id?: string;
  data_as_of?: string;
  result: {
    analysis: string | null;
    detail: string;
    generated_at: string;
    bar_count: number;
  } | null;
  error: string | null;
  evidence: Record<string, unknown> | null;
}
export interface ChainContract {
  symbol: string;
  underlying: string;
  expiry: string;
  kind: "call" | "put";
  strike: number;
  bid: number | null;
  ask: number | null;
  last: number | null;
  observed_at: string | null;
  iv: number | null;
  greeks: Record<string, number | null>;
}
export interface OptionChain {
  source: string;
  feed: string;
  underlying: string;
  fetched_at: string;
  truncated: boolean;
  contracts: ChainContract[];
  note: string;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/research${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
  let value;
  try {
    value = await response.json();
  } catch {
    throw new Error("服务未返回有效数据，请检查后端连接。");
  }
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string"
        ? value.detail
        : `请求失败 (${response.status})，请检查输入。`,
    );
  return value as T;
}
export const researchApi = {
  catalog: () => request<ResearchInstrument[]>("/catalog"),
  sources: () => request<ResearchSource[]>("/sources"),
  bars: (query: ResearchQuery, signal?: AbortSignal, refresh = false) =>
    request<ResearchPage>(`/bars?refresh=${refresh}`, {
      method: "POST",
      body: JSON.stringify(query),
      signal,
    }),
  contracts: (asset: ResearchAsset, exchange: string) =>
    request<ResearchInstrument[]>(
      `/contracts?asset=${asset}&exchange=${exchange}`,
    ),
  chain: (symbol: string, expiry?: string, signal?: AbortSignal) =>
    request<OptionChain>(
      `/options/${encodeURIComponent(symbol)}${expiry ? `?expiry=${expiry}` : ""}`,
      { signal },
    ),
  analyze: (
    query: ResearchQuery,
    question: string,
    model?: string,
    reasoning_effort?: string,
  ) =>
    request<ResearchJob>("/analysis", {
      method: "POST",
      body: JSON.stringify({ query, question, model, reasoning_effort }),
    }),
  job: (id: string, signal?: AbortSignal) =>
    request<ResearchJob>(`/analysis/${id}`, { signal }),
  cancel: (id: string) =>
    request<ResearchJob>(`/analysis/${id}`, { method: "DELETE" }),
};

export function downloadText(
  filename: string,
  text: string,
  type = "text/plain",
) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
