import type { ExpertMarketStructureEvent } from "./expertSmartMoney.ts";

export type ExpertSessionKind = "asia" | "europe" | "us";

export type ExpertSessionDriver =
  | "regional-dominance"
  | "us-data-release"
  | "us-equity-open";

export interface ExpertSessionBand {
  id: string;
  kind: ExpertSessionKind;
  label: string;
  detail: string;
  start: number;
  end: number;
  timeZone: string;
  driver: ExpertSessionDriver;
  eventId: string | null;
}

export type ExpertEventTier = "S+" | "S" | "A" | "B";

export type ExpertEventFamily =
  | "monetary-policy"
  | "inflation"
  | "employment"
  | "growth"
  | "geopolitical-risk"
  | "financial-risk"
  | "official-flow"
  | "investment-flow"
  | "physical-demand"
  | "market-structure"
  | "supply";

export type ExpertEventTransmissionChannel =
  | "real-yields"
  | "usd"
  | "risk"
  | "liquidity"
  | "central-bank"
  | "etf"
  | "positioning"
  | "physical-demand"
  | "supply";

export interface ExpertMarketEvent {
  id: string;
  time: number;
  title: string;
  shortLabel: string;
  eventTypeId: string;
  releaseClusterId: string | null;
  family: ExpertEventFamily;
  baselineTier: ExpertEventTier;
  transmissionChannels: readonly ExpertEventTransmissionChannel[];
  directionRule: string;
  usDominanceTrigger: boolean;
  source: string;
  sourceUrl: string | null;
  sourceTier: "official" | "institutional-research" | "manual-verified";
  timing: "scheduled" | "released" | "manual";
  timePrecision: "instant" | "date";
  scheduledAt: number | null;
  releasedAt: number | null;
  effectivePeriodStart: number | null;
  effectivePeriodEnd: number | null;
  sourcePublishedAt: number;
  ingestedAt: number;
  revisionVintage: string;
  actual: string | null;
  consensus: string | null;
  previous: string | null;
  revised: string | null;
  flowDirection: "inflow" | "outflow" | "mixed" | "unknown";
  flowAmount: number | null;
  flowUnit: string | null;
  note: string | null;
}

export interface ExpertGoldEventTypeDto {
  event_type_id: string;
  name: string;
  family: ExpertEventFamily;
  baseline_tier: ExpertEventTier;
  cadence: string;
  transmission_channels: ExpertEventTransmissionChannel[];
  direction_rule: string;
  official_source_urls: string[];
  us_dominance_trigger: boolean;
}

export interface ExpertGoldEventFactDto {
  event_id: string;
  event_type_id: string;
  title: string;
  short_label: string;
  country: string;
  release_cluster_id: string | null;
  marker_at: string;
  scheduled_at: string | null;
  released_at: string | null;
  effective_period_start: string | null;
  effective_period_end: string | null;
  source_published_at: string;
  ingested_at: string;
  revision_vintage: string;
  actual: string | null;
  consensus: string | null;
  previous: string | null;
  revised: string | null;
  source: string;
  source_url: string;
  source_tier: "official" | "institutional-research" | "manual-verified";
  time_precision: "instant" | "date";
  family: ExpertEventFamily;
  baseline_tier: ExpertEventTier;
  transmission_channels: ExpertEventTransmissionChannel[];
  direction_rule: string;
  us_dominance_trigger: boolean;
  flow_direction: "inflow" | "outflow" | "mixed" | "unknown";
  flow_amount: number | null;
  flow_unit: string | null;
  note: string | null;
}

export interface ExpertGoldEventCatalogSnapshot {
  contract_version: "gold-events-v1";
  generated_at: string;
  event_types: ExpertGoldEventTypeDto[];
  facts: ExpertGoldEventFactDto[];
  score_methodology: {
    shock: { label: string; weights: Record<string, number>; windows_seconds: number[]; rule: string };
    regime: { label: string; weights: Record<string, number>; windows_seconds: number[]; rule: string };
    tiers: Record<ExpertEventTier, [number, number]>;
  };
  source_precedence: string[];
  limitations: string[];
}

export interface ExpertVolatilityEodIndex {
  index_code: "VIX" | "GVZ";
  underlying: "SPX" | "GLD";
  value: number;
  as_of: string;
  trailing_percentile_252: number | null;
  history_sample_size: number;
  history_start: string | null;
  history_end: string | null;
  expected_horizon_days: number;
  directional: false;
  source: {
    provider_id: string;
    dataset_id: string;
    source_url: string;
    frequency: "daily_eod";
    received_at: string;
  };
}

export interface ExpertVolatilityContext {
  contract_version: "volatility-eod-context-v1";
  state: "ready";
  mode: "eod";
  refresh_after_seconds: number;
  directional: false;
  indices: ExpertVolatilityEodIndex[];
  limitations: string[];
}

export interface ExpertShfePositioningContract {
  product_code: "AU" | "AG";
  contract_code: string;
  volume: number;
  open_interest: number;
  open_interest_change: number | null;
  last_price: number | null;
  observed_at: string;
}

export interface ExpertShfePositioningContext {
  contract_version: "shfe-positioning-context-v1";
  state: "ready";
  mode: "delayed_snapshot";
  refresh_after_seconds: number;
  as_of: string;
  delayed: true;
  declared_delay_seconds: number;
  product_code: "AU" | "AG";
  contract_count: number;
  volume: number;
  open_interest: number;
  open_interest_change: number | null;
  open_interest_change_contracts: number;
  unit: "lots";
  counting_method: "single_side";
  directional_inference: "unavailable";
  derived_aggregate: true;
  contracts: ExpertShfePositioningContract[];
  source: {
    provider_id: string;
    dataset_id: string;
    source_url: string;
    observed_at: string;
    received_at: string;
    published_at: string | null;
  };
  limitations: string[];
}

export type ExpertMultiTimeframeDirection = "up" | "down" | "mixed" | "unavailable";

export interface ExpertMultiTimeframeItem {
  horizon: "short" | "medium" | "long";
  period_id: "1h" | "1d" | "1w";
  state: "ready" | "insufficient_data" | "unavailable";
  direction: ExpertMultiTimeframeDirection;
  required_final_bars: number;
  loaded_bar_count: number;
  eligible_final_bar_count: number;
  used_bar_count: number;
  excluded_non_final_bars: number;
  excluded_after_as_of_bars: number;
  excluded_invalid_time_bars: number;
  first_open_time: string | null;
  last_open_time: string | null;
  last_bucket_end: string | null;
  last_available_at: string | null;
  last_close: number | null;
  sma_fast: number | null;
  sma_slow: number | null;
  window_return_percent: number | null;
  limitation: string | null;
}

export interface ExpertMultiTimeframeContext {
  contract_version: "multi-timeframe-trend-v1";
  profile_id: "swing-1h-1d-1w-v1";
  state: "ready" | "partial" | "insufficient_data" | "unavailable";
  code: string;
  instrument_symbol: string;
  source_id: string;
  decision_as_of: string;
  as_of_policy: string;
  direction_rule: string;
  period_mapping: Record<"short" | "medium" | "long", {
    period_id: "1h" | "1d" | "1w";
    required_final_bars: number;
    fast_sma_bars: number;
    slow_sma_bars: number;
  }>;
  timeframes: ExpertMultiTimeframeItem[];
  comparison: {
    state: "aligned" | "divergent" | "mixed" | "not_comparable";
    comparable: boolean;
    aligned_direction: "up" | "down" | null;
    differences: Array<{
      left: "short" | "medium" | "long";
      right: "short" | "medium" | "long";
      left_direction: ExpertMultiTimeframeDirection;
      right_direction: ExpertMultiTimeframeDirection;
    }>;
    incomparable_reasons: string[];
  };
  limitations: string[];
}

export interface ExpertEventWindowReaction {
  windowSeconds: number;
  returnPercent: number;
  robustZ: number | null;
}

export interface ExpertEventAssessment {
  eventId: string;
  evaluatedAt: number;
  shockScore: number | null;
  shockCoverage: number;
  regimeScore: number | null;
  regimeCoverage: number;
  observedDirection: "bullish" | "bearish" | "neutral" | "unavailable";
  confidence: "high" | "medium" | "low" | "unavailable";
  reactions: ExpertEventWindowReaction[];
  evidence: string[];
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

export interface ExpertTrendLine {
  id: string;
  direction: "support" | "resistance";
  start: ExpertDrawingPoint;
  anchor: ExpertDrawingPoint;
  end: ExpertDrawingPoint;
  status: "candidate" | "confirmed" | "tested" | "invalidated";
  touchCount: number;
  quality: number;
  atrError: number;
  invalidatedAt: number | null;
  invalidationReason: string | null;
}

export type ExpertPricePatternKind =
  | "double-bottom"
  | "double-top"
  | "two-b-bottom"
  | "two-b-top";

export interface ExpertPricePatternAnchor {
  index: number;
  time: number;
  price: number;
}

export interface ExpertPricePattern {
  id: string;
  kind: ExpertPricePatternKind;
  label: string;
  direction: "bullish" | "bearish";
  status: "confirmed" | "invalidated";
  first: ExpertPricePatternAnchor;
  neckline: ExpertPricePatternAnchor | null;
  second: ExpertPricePatternAnchor;
  confirmation: ExpertPricePatternAnchor;
  triggerPrice: number;
  invalidationPrice: number;
  detectedAt: number;
  invalidatedAt: number | null;
  confidence: number;
  evidence: string[];
}

export interface ExpertOverlayPoint {
  time: number;
  value: number;
}

export interface ExpertOverlaySeries {
  id: string;
  label: string;
  color: string;
  lineStyle: "solid" | "dashed" | "dotted";
  lineWidth: 1 | 2 | 3;
  points: ExpertOverlayPoint[];
  lastValueVisible: boolean;
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
  | "ma-structure"
  | "macd"
  | "kdj"
  | "rsi"
  | "bollinger"
  | "nine-count"
  | "momentum-ensemble"
  | "auto-trend"
  | "vix-gvz"
  | "volume-open-interest"
  | "multi-timeframe"
  | "smart-money"
  | "fair-value"
  | "poc-proxy"
  | "order-flow-proxy"
  | "volume-price";

export interface ExpertStrategyReference {
  title: string;
  publisher: string;
  url: string;
  note: string;
}

export interface ExpertStrategyDetails {
  role: "direction" | "confirmation" | "exhaustion" | "rhythm" | "structure" | "risk-context";
  horizon: string;
  principle: string;
  formula: string[];
  parameters: string[];
  signalRules: string[];
  requiredFields: string[];
  suitableRegimes: string[];
  boundaryConditions: string[];
  invalidation: string[];
  references: ExpertStrategyReference[];
  validation: string;
  version: string;
  compositeEligible: boolean;
  backtestEligible: boolean;
}

export interface ExpertStrategyDefinition {
  id: ExpertStrategyId;
  name: string;
  shortName: string;
  description: string;
  dataSource: string;
  evidenceMode: "native" | "proxy" | "conditional";
  details: ExpertStrategyDetails;
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

export interface ExpertKdjDullingSnapshot {
  zone: "low" | "middle" | "high";
  dulling:
    | "normal"
    | "high-entering"
    | "high-dulling"
    | "high-releasing"
    | "low-entering"
    | "low-dulling"
    | "low-releasing";
  streak: number;
  cross: "bullish" | "bearish" | "none";
  scoreEligible: boolean;
}

export interface ExpertIndicatorSnapshot {
  macd: { value: number; signal: number; histogram: number } | null;
  kdj: ({ k: number; d: number; j: number } & ExpertKdjDullingSnapshot) | null;
  rsi: {
    period: number;
    value: number;
    state: "oversold" | "neutral" | "overbought";
    signal: "oversold-recovery" | "overbought-reversal" | "oversold" | "overbought" | "none";
  } | null;
  movingAverage: {
    alignment: "bullish" | "bearish" | "mixed" | "insufficient";
    values: Array<{
      period: 20 | 60 | 120 | 250;
      value: number | null;
      slopePercent: number | null;
      distanceAtr: number | null;
      interaction: "support-test" | "resistance-test" | "break" | "none";
    }>;
  } | null;
  bollinger: {
    middle: number;
    upper: number;
    lower: number;
    bandwidth: number;
    bandwidthPercentile: number | null;
    position: number;
    state: "squeeze" | "expanding" | "normal";
  } | null;
  nineCount: {
    direction: "sell-setup" | "buy-setup" | "none";
    count: number;
    perfected: boolean;
    completedNow: boolean;
  } | null;
  momentum: {
    score: number;
    availableHorizons: number;
    returns: Array<{ horizon: 20 | 60 | 120; percent: number | null }>;
  } | null;
  trendSlopePercent: number | null;
  pocPrice: number | null;
  orderFlowPressure: number | null;
  volumePriceState: "confirming" | "diverging" | "unavailable";
}

export type ExpertIndicatorId = "rsi" | "kdj" | "macd";

export interface ExpertIndicatorBarReference {
  readonly time: number;
}

export interface ExpertIndicatorSeriesView {
  readonly historyKey: string | null;
  readonly revision: number;
  readonly offset: number;
  readonly length: number;
  readonly visibleLength: number;
  readonly changedFrom: number;
  readonly bars: readonly ExpertIndicatorBarReference[];
  readonly macd: {
    readonly value: readonly number[];
    readonly signal: readonly number[];
    readonly histogram: readonly number[];
  };
  readonly kdj: {
    readonly k: readonly number[];
    readonly d: readonly number[];
    readonly j: readonly number[];
  };
  readonly rsi: {
    readonly value: readonly (number | null)[];
  };
}

export interface ExpertAnalysisSnapshot {
  asOf: number | null;
  signals: ExpertSignal[];
  levels: ExpertPriceLevel[];
  valueZones: ExpertValueZone[];
  pricePatterns: ExpertPricePattern[];
  marketStructureEvents: ExpertMarketStructureEvent[];
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
  diagnostic_code: ExpertAiDiagnosticCode | null;
}

export type ExpertAiDiagnosticCode =
  | "analysis_failed"
  | "analysis_timeout"
  | "cli_not_found"
  | "cli_path_invalid"
  | "cli_start_failed"
  | "not_authenticated"
  | "status_request_failed"
  | "status_timeout"
  | "status_unrecognized";

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
  diagnostic_code: ExpertAiDiagnosticCode | null;
}
