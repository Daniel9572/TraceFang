export type ExpertSessionKind = "asia" | "europe" | "us";

export interface ExpertSessionBand {
  id: string;
  kind: ExpertSessionKind;
  label: string;
  start: number;
  end: number;
  timeZone: string;
}

export interface ExpertMarketEvent {
  id: string;
  time: number;
  title: string;
  category: "fomc" | "employment" | "central-bank-gold" | "custom";
  importance: "high" | "medium";
  source: string;
  sourceUrl: string | null;
  timing: "scheduled" | "released" | "manual";
  timePrecision?: "instant" | "date";
}

export type ExpertDrawingTool = "trend" | "horizontal";
export type ExpertDrawingSnapMode = "off" | "weak";

export interface ExpertDrawingPoint {
  time: number;
  price: number;
}

export interface ExpertDrawing {
  id: string;
  type: ExpertDrawingTool;
  start: ExpertDrawingPoint;
  end: ExpertDrawingPoint;
  color: string;
  label: string;
}

export interface ExpertPriceLevel {
  id: string;
  price: number;
  label: string;
  tone: "gold" | "support" | "resistance" | "neutral";
  style: "solid" | "dashed" | "dotted";
}

export interface ExpertValueZone {
  id: string;
  start: number;
  end: number;
  low: number;
  high: number;
  direction: "bullish" | "bearish";
  label: string;
}

export type ExpertStrategyId =
  | "structure"
  | "macd"
  | "kdj"
  | "fair-value"
  | "poc-proxy"
  | "order-flow-proxy"
  | "volume-price";

export interface ExpertStrategyDefinition {
  id: ExpertStrategyId;
  name: string;
  shortName: string;
  description: string;
  dataSource: string;
  evidenceMode: "native" | "proxy" | "conditional";
}

export interface ExpertSignal {
  id: string;
  strategyId: ExpertStrategyId;
  title: string;
  detail: string;
  direction: "bullish" | "bearish" | "neutral";
  confidence: number;
  triggeredAt: number;
  evidence: string[];
}

export interface ExpertIndicatorSnapshot {
  macd: { value: number; signal: number; histogram: number } | null;
  kdj: { k: number; d: number; j: number } | null;
  trendSlopePercent: number | null;
  pocPrice: number | null;
  orderFlowPressure: number | null;
  volumePriceState: "confirming" | "diverging" | "unavailable";
}

export interface ExpertAnalysisSnapshot {
  asOf: number | null;
  signals: ExpertSignal[];
  levels: ExpertPriceLevel[];
  valueZones: ExpertValueZone[];
  indicators: ExpertIndicatorSnapshot;
  regime: "trend-up" | "trend-down" | "balanced" | "insufficient";
  compositeScore: number;
}

export interface ExpertBacktestResult {
  barCount: number;
  tradeCount: number;
  winRate: number;
  totalReturnPercent: number;
  maxDrawdownPercent: number;
  latestScore: number;
  caveat: string;
}

export interface ExpertOptionsStatus {
  contract_version: string;
  state: "live" | "delayed" | "unconfigured" | "unavailable";
  available: boolean;
  provider_id: string | null;
  market_id: string | null;
  delivery_mode: "live" | "exchange_delayed" | "end_of_day" | null;
  checked_at: string;
  observed_at: string | null;
  trading_day: string | null;
  reference_data_as_of: string | null;
  quote_currency: string | null;
  price_unit: string | null;
  quote_count: number;
  markets: Array<{
    market_id: string;
    label: string;
    state: string;
    detail: string;
    delivery_mode: string | null;
    quote_count: number;
    observed_at: string | null;
    required_data: string[];
  }>;
  expiries: ExpertOptionExpiryAnalysis[];
  contracts: ExpertOptionContract[];
  underlyings: ExpertOptionUnderlying[];
  source_urls: string[];
  required_quote_fields: string[];
  analysis_state: string;
  detail: string;
  limitations: string[];
  usage_notice: string;
  refresh_after_seconds: number;
}

export interface ExpertOptionExpiryAnalysis {
  underlying_contract_id: string;
  expiry: string;
  underlying_price: number | null;
  option_count: number;
  call_open_interest: number;
  put_open_interest: number;
  put_call_open_interest_ratio: number | null;
  call_volume: number;
  put_volume: number;
  put_call_volume_ratio: number | null;
  atm_strike: number | null;
  call_wall_strike: number | null;
  put_wall_strike: number | null;
  max_pain_strike: number | null;
  reference_iv: number | null;
  expected_move_percent: number | null;
  delta_coverage_ratio: number;
  positioning_state: string;
  gamma_state: string;
  gex: null;
}

export interface ExpertOptionContract {
  contract_id: string;
  underlying_contract_id: string;
  expiry: string;
  strike: number;
  option_type: "call" | "put";
  contract_multiplier: number;
  bid: number | null;
  ask: number | null;
  last: number | null;
  previous_settlement: number | null;
  volume: number;
  open_interest: number;
  open_interest_change: number;
  turnover: number | null;
  observed_at: string;
  delta: number | null;
  delta_as_of: string | null;
}

export interface ExpertOptionUnderlying {
  contract_id: string;
  bid: number | null;
  ask: number | null;
  last: number | null;
  previous_settlement: number | null;
  volume: number;
  open_interest: number;
  observed_at: string;
}

export interface ExpertAiStatus {
  state: "ready" | "unavailable" | "not_authenticated" | "timeout" | "error";
  available: boolean;
  authenticated: boolean | null;
  auth_mode: "chatgpt" | "api_key" | "authenticated" | null;
  provider: string;
  detail: string;
  checked_at: string;
}

export interface ExpertAiAnalysis {
  state: "completed" | "unavailable" | "not_authenticated" | "timeout" | "failed" | "error";
  provider: string;
  analysis: string | null;
  detail: string;
  generated_at: string;
  auth_mode: string | null;
  source_id: string;
  data_as_of: string | null;
  bar_count: number;
}
