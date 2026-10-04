import type { BarPeriodId } from "./chartPeriods";

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
  raw_payload?: Record<string, unknown> | null;
}

export interface Instrument {
  symbol: string;
  asset_class: string;
  base: string | null;
  quote: string | null;
  venue: string | null;
}

export interface TradingSessionWindow {
  weekday: 0 | 1 | 2 | 3 | 4 | 5 | 6;
  open: string;
  close: string;
  close_day_offset: number;
}

export interface MarketSchedule {
  time_zone: string;
  sessions: TradingSessionWindow[];
  reference: string;
  trading_day_rule?: "session_start" | "session_end" | "shfe";
}

export type MarketPhase = "open" | "closed" | "unknown";

export interface InstrumentEntry {
  provider: string;
  provider_code: string;
  name: string;
  instrument: Instrument | null;
  price_unit: string;
  price_digits: number;
  quote_kind: "direct" | "derived";
  history_backfill_supported: boolean;
  source_ids: SourceId[];
  source_period_reference?: SourcePeriodReferenceMapping | null;
  dependencies: string[];
  market_schedule?: MarketSchedule | null;
}

export interface SourcePeriodReferenceMapping { source_id: string; period: "min_5"; market: string; code: string }
export interface SourcePeriodReferenceScope { code: string; mapping: SourcePeriodReferenceMapping }
export interface SourcePeriodPriceRow {
  row_index: number;
  source_label: string;
  label_utc_display: string;
  source_fields: Record<string, unknown>;
  field_presence: Record<string, boolean>;
  open: string | null; high: string | null; low: string | null; close: string | null;
  volume: string | null; turnover: string | null;
  finality: "unknown";
}
export interface SourcePeriodPriceReference {
  code: string; name: string; source_id: string;
  source_instrument: { market: string; code: string };
  requested_source_period: "min_5"; adjust_type: "actual";
  source_response_state: "rows" | "empty";
  delivery_mode: "live" | "fixed_original_body";
  rows: SourcePeriodPriceRow[];
  reference_id: string; reference_url: string;
  source_evidence: {
    url: string; request: Record<string, unknown>; requested_at: string; received_at: string;
    body_sha256: string; body_bytes: number; body_base64: string;
    fixed_manifest_sha256?: string;
  };
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
  open_interest?: number | null;
  source: SourceMetadata;
  evidence_channel_id: string;
  state: "provisional_quote" | "provisional_authoritative" | "final";
  revision: number | string;
  finalized_at: string | null;
}

export interface SourceDescriptor {
  source_id: SourceId;
  display_name: string;
  description: string;
  capabilities: string[];
  history_backfill_configured: boolean;
  selectable: boolean;
  delayed: boolean;
  requires_running_app: boolean;
  structured: boolean;
  quote_poll_interval_seconds: number;
  quote_timestamp_precision_seconds?: number;
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

export interface CandleBackfillResult {
  source_id: SourceId;
  state: "cached" | "joined" | "fetched" | "advanced" | "exhausted" | "deferred";
  start: string;
  end: string;
  row_count: number;
  covered_start: string | null;
  covered_end: string | null;
  authoritative_through: string | null;
  history_floor: string | null;
  retry_after: string | null;
  evidence_version: string | null;
}

export interface SourceConnectionTest {
  source_id: SourceId;
  code: string;
  state: string;
  detail: string | null;
  history_backfill_configured: boolean;
  data_fresh: boolean;
  last: number | string | null;
  observed_at: string | null;
  latency_ms: number | string | null;
  validation_performed?:boolean;
  capture_position?:{epoch:string;sequence:string;digest:string};
  quality: "complete" | "degraded" | "unavailable";
  unavailable_fields: string[];
  stale_fields: string[];
  kline_points: number;
  kline_open_time: string | null;
}

export interface InstrumentSourceSelection {
  code: string;
  source_id: SourceId;
}

export interface QuoteStreamEvent {
  kind: "bar" | "gap" | "quote" | "sample" | "status" | "range_invalidated" | "period_tail_changed";
  state: "connecting" | "live" | "unavailable";
  emitted_at: string;
  period_id: BarPeriodId;
  bar: Candle | null;
  quote: QuoteView | null;
  sample: QuoteSample | null;
  error: string | null;
  delivery_sequence?: string | number | null;
  source_id?:SourceId; symbol?:string; start_ns?:string;end_ns?:string;
  change?:{source_id:SourceId;symbol:string;start_ns:string;end_ns:string;interval_seconds:number;series_version:Record<string,unknown>};
  snapshot_version?:{commit_id:string;store_epoch:string};
  gap_from_sequence?: string | number | null;
  gap_to_sequence?: string | number | null;
}

export type ExactSequence = string | number;
export interface ReplayFrameBounds {
  state: "ready" | "empty" | "unavailable";
  first_sequence: ExactSequence | null; last_sequence: ExactSequence | null;
  message_count: ExactSequence;
  first_received_at: string | null; last_received_at: string | null;
  first_logical_at_ns?: string | null; last_logical_at_ns?: string | null;
  source_ids: SourceId[]; detail: string | null;
}
export interface ReplayFrameCursor {
  sequence: ExactSequence; received_at: string; received_at_ns?: string | null;
  logical_at_ns?: string | null; channel: string; connection_id: string;
  provider_sequence: ExactSequence;
}
export interface ReplayStreamEvent {
  kind: "bar" | "decode_error" | "frame" | "quote" | "status" | "snapshot" | "period_snapshot";
  state?: "playing" | "paused" | "seeking" | "completed" | "unavailable";
  items?: Candle[]; reset?: boolean; stream_sequence?: ExactSequence | null;
  frame_received_at?: string; actual_received_at_ns?: string | null; logical_at_ns?: string | null;
  knowledge_at_ns?:string|null;
  frame_channel?: string; period_id?: string; source_id?: SourceId;
  quote?: QuoteSnapshot | null; bar?: Candle | null; error?: string | null;
  start_sequence?: ExactSequence; end_sequence?: ExactSequence;
  replay_policy?: string; input_watermark?: {epoch:string;sequence:ExactSequence}|null;
  session_id?:string; quant_snapshot?:import("./quantTypes").QuantSnapshot;
}

export interface ChartBarPage {
  coverage?:{calendar_projection?:{excluded_outside_schedule:string;earliest_ns:string|null;latest_ns:string|null;schedule_version:string;reason:string|null;complete:boolean}};
  period_id: string;
  items: Candle[];
  next_before: string | null;
  next_cursor: string | null;
  local_status: "ready" | "empty";
  has_more: boolean;
}

export type ChartHistorySourceStatus =
  | "available"
  | "deferred"
  | "exhausted"
  | "unsupported";

export interface ChartHistoryResponse {
  source_id: SourceId;
  period_id: string;
  local_status: "ready" | "empty";
  source_status: ChartHistorySourceStatus;
  page: ChartBarPage;
  next_before: string | null;
  next_cursor: string | null;
  backfill: CandleBackfillResult | null;
}

export interface QuoteSample {
  source_id: SourceId;
  channel_id: string;
  event_id: string;
  instrument: Instrument;
  provider_symbol: string;
  observed_at: string;
  received_at: string;
  value: number | string;
  observation_kind: "event" | "snapshot";
  storage_id: number | null;
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
  resolutionSeconds?: number;
}
