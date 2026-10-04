import type { Candle } from "./types";

export type ResearchAsset = "equity" | "etf" | "index" | "future" | "option";
export type ResearchSourceId =
  | "eastmoney"
  | "sina"
  | "tushare"
  | "alpaca"
  | "akshare"
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
  asset_periods?: Partial<Record<ResearchAsset, string[]>>;
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
  authority_snapshot_id?:string|null;authority_manifest?:ResearchAuthorityManifest|null;authority_unavailable_reason?:string|null;source_response_evidence?:unknown;
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
export interface ResearchAuthorityManifest{row_count:number;first_open:string|null;last_open:string|null;warmup_complete:boolean;coverage_reason:string;precision_policy:string;fetched_at:string}
export interface ResearchJob {
  id: string;
  state: "loading" | "analyzing" | "completed" | "failed" | "cancelled";
  stage: string;
  query: ResearchQuery;
  snapshot_id?: string;
  input_hash?:string;
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
  strike: string | number;
  bid: string | number | null;
  ask: string | number | null;
  last: string | number | null;
  observed_at: string | null;
  iv: number | null;
  greeks: Record<string, number | null>;
  multiplier?: string | number;
  currency?: string;
  volume?: string | null;
  open_interest?: string | null;
  source_quote?: Record<string, unknown>;
  daily_close?: string | null;
  settlement?: string | null;
  price_semantics?: "official_daily_close";
  source_date?: string;
  source_received_at?: string;
  source_clock_label?: string;
  source_clock_qualification?: string;
  quantity_units?: {volume?: string | null; open_interest?: string | null; turnover?: string | null};
}
export interface OptionChain {
  source: string;
  feed: string;
  underlying: string;
  fetched_at: string;
  truncated: boolean;
  contracts: ChainContract[];
  note: string;
  cache_state?: "fresh" | "cached" | "stale";
  warnings?: string[];
  pricing_model?: "black76" | "black-scholes";
  reference_spot?: string | number | null;
  reference_observed_at?: string | null;
  reference_date?: string | null;
  reference_precision?: string;
  reference_source_label?: string | null;
  reference_received_at?: string | null;
  reference_clock_qualification?: string;
  precision_policy?: string;
  model_numeric_policy?: string;
  price_semantics?: "official_daily_close";
  source_date?: string;
  source_family?: string;
  metadata_contract_count?: string;
  quoted_contract_count?: string;
  positive_close_count?: string;
  source_availability?: {availability: string; attempted_product_request: boolean};
}
export interface OptionUnderlying {
  symbol: string;
  name: string;
  category: "etf" | "index" | "future";
  quote_source?: "czce-option-daily" | "gfex-option-daily" | "catalog-only" | null;
  daily_date?: string | null;
}
export interface OptionMonths {
  symbol: string;
  months: Array<{ month: string; label: string; expiry: string }>;
  fetched_at: string;
}
export function sourcePeriods(
  source: ResearchSource | undefined,
  asset: ResearchAsset,
): string[] {
  return source?.asset_periods?.[asset] ?? source?.periods ?? ["1d"];
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
  mergeAuthority:(base_snapshot_id:string,additional_snapshot_id:string,signal?:AbortSignal)=>request<{authority_snapshot_id:string;authority_manifest:ResearchAuthorityManifest}>("/authority/merge",{method:"POST",body:JSON.stringify({base_snapshot_id,additional_snapshot_id}),signal}),
  catalog: () => request<ResearchInstrument[]>("/catalog"),
  sources: () => request<ResearchSource[]>("/sources"),
  bars: (query: ResearchQuery, signal?: AbortSignal, refresh = false) =>
    request<ResearchPage>(`/bars?refresh=${refresh}`, {
      method: "POST",
      body: JSON.stringify(query),
      signal,
    }),
  contracts: (
    asset: ResearchAsset,
    exchange: string,
    source: ResearchSourceId = "tushare",
  ) =>
    request<ResearchInstrument[]>(
      `/contracts?asset=${asset}&exchange=${exchange}&source=${source}`,
    ),
  optionUnderlyings: (signal?: AbortSignal) =>
    request<OptionUnderlying[]>("/option-underlyings", { signal }),
  optionMonths: (symbol: string, signal?: AbortSignal) =>
    request<OptionMonths>(`/option-months/${encodeURIComponent(symbol)}`, {
      signal,
    }),
  chain: (
    symbol: string,
    expiry?: string,
    signal?: AbortSignal,
    source: "alpaca" | "akshare" = "alpaca",
    month?: string,
    reportDate?: string,
  ) =>
    request<OptionChain>(
      `/options/${encodeURIComponent(symbol)}?source=${source}${expiry ? `&expiry=${expiry}` : ""}${month ? `&month=${month}` : ""}${reportDate ? `&report_date=${encodeURIComponent(reportDate)}` : ""}`,
      { signal },
    ),
  analyze: (
    query: ResearchQuery,
    question: string,
    model?: string,
    reasoning_effort?: string,
    evidence?:{research_snapshot_id:string;expected_input_hash:string;parameters:import("./quantTypes").QuantParameters},
  ) =>
    request<ResearchJob>("/analysis", {
      method: "POST",
      body: JSON.stringify({ query, question, model, reasoning_effort,...evidence }),
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
