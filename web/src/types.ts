export type SourceId = string;
export type SourceAccessModel = "unmetered" | "limited" | "metered";
export type QuoteServiceTier = "institutional" | "enhanced" | "standard" | "reference";

export interface SourceQuota {
  key: string;
  label: string;
  used: number;
  limit: number;
  reserve: number;
  available: number;
  usage_percent: number;
  warning_percent: number;
  period: "daily" | string;
  resets_at: string;
  scope: "application_process" | string;
}

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

export interface QuoteView {
  source_id: SourceId;
  quote: QuoteSnapshot;
  quality: "complete" | "degraded";
  unavailable_fields: string[];
  stale_fields: string[];
  composed_at: string;
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
  source_id: SourceId;
  display_name: string;
  description: string;
  capabilities: string[];
  selectable: boolean;
  delayed: boolean;
  requires_running_app: boolean;
  structured: boolean;
  quote_poll_interval_seconds: number;
  quote_streaming: boolean;
  quote_service_tier: QuoteServiceTier;
  access_model: SourceAccessModel;
  access_note: string | null;
  manual_connection_required: boolean;
  connection_active: boolean;
  quotas: SourceQuota[];
  health: "healthy" | "degraded" | "unavailable" | "unconfigured" | "frozen" | "unknown";
  state: string;
  error: string | null;
  checked_at: string | null;
  last_success_at: string | null;
}

export interface SourceConnectionTest {
  source_id: SourceId;
  code: string;
  last: number | string;
  observed_at: string;
  latency_ms: number;
  quality: "complete" | "degraded";
  unavailable_fields: string[];
  stale_fields: string[];
}

export interface InstrumentSourceSelection {
  code: string;
  source_id: SourceId;
}

export interface QuoteStreamEvent {
  kind: "quote" | "status";
  state: "connecting" | "live" | "unavailable";
  emitted_at: string;
  quote: QuoteView | null;
  error: string | null;
}

export interface HoverCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface TimelineSample {
  time: number;
  value: number;
  observedTime?: number;
  eventId?: string;
}
