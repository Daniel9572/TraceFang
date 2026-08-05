export type SourceId = "auto" | "jin10_mcp" | "jin10_desktop";

export interface SourceMetadata {
  provider: string;
  provider_symbol: string;
  observed_at: string;
  received_at: string;
}

export interface Instrument {
  symbol: string;
  asset_class: string;
  base: string | null;
  quote: string | null;
  venue: string | null;
}

export interface InstrumentEntry {
  provider: string;
  provider_code: string;
  name: string;
  instrument: Instrument | null;
}

export interface QuoteSnapshot {
  instrument: Instrument;
  last: number | string;
  open: number | string | null;
  high: number | string | null;
  low: number | string | null;
  volume: number | string | null;
  change: number | string | null;
  change_percent: number | string | null;
  source: SourceMetadata;
}

export interface Candle {
  instrument: Instrument;
  interval: number | string;
  open_time: string;
  open: number | string;
  high: number | string;
  low: number | string;
  close: number | string;
  volume: number | string | null;
  source: SourceMetadata;
}

export interface SourceDescriptor {
  source_id: Exclude<SourceId, "auto">;
  display_name: string;
  description: string;
  capabilities: string[];
  enabled: boolean;
  priority: number;
  delayed: boolean;
  requires_running_app: boolean;
  health: "healthy" | "degraded" | "unavailable" | "unconfigured" | "unknown";
  state: string;
  error: string | null;
  checked_at: string | null;
  last_success_at: string | null;
}

export interface ComparisonItem {
  source_id: Exclude<SourceId, "auto">;
  quote: QuoteSnapshot | null;
  error: string | null;
  request_latency_ms: number;
  sample_age_seconds: number | null;
  deviation: number | string | null;
  deviation_percent: number | string | null;
}

export interface QuoteComparison {
  code: string;
  reference_source: string | null;
  items: ComparisonItem[];
}

export interface HoverCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}
