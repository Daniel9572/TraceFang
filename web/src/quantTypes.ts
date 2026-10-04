import type { ExpertStrategyId } from "./expertTypes";
export type QuantParameters = Record<string, string | number | (string | number)[] | Record<string, string | number>> & { enabled_strategies: string[] };
export interface SimulationConfig {
  assumption_provenance:Record<string,string>;
  start: string | null; end: string | null; parameters: QuantParameters;
  initial_capital: string; target_quantity: string; multiplier: string; allowed_direction: "both" | "long_only" | "short_only";
  neutral_exit: boolean; fee_mode: "percentage" | "fixed" | "combined"; fee_rate: string; fixed_fee: string;
  slippage_ticks: number; tick_size: string; settlement_precision: number; maximum_exposure_ratio: string;
  terminal_position: "mark_only" | "settle_at_last_close"; risk_free_annual_rate: string;
}
export interface QuantPoint { as_of: string; decision_at: string; composite_score: string; direction: number;
  indicators: Record<string, unknown>; signals: Array<{ strategy_id: ExpertStrategyId; direction: "bullish" | "bearish" | "neutral"; confidence: string; as_of: string; state: string; title: string; evidence: string[]; composite_eligible: boolean; backtest_eligible: boolean }> }
export interface QuantSnapshot {
  chart_basis_hash?:string;
  quote?:{price:string;observed_at:string;received_at:string;accepted_at:string|null;applied_frame_seq:string|null}|null;
  evidence: { schema_version: string; calculation_version: string; input_hash: string; snapshot_hash: string; effective_input_hash?:string; confirmed_prefix_hash?:string; code: string; source_id: string; period: string; decision_as_of: string; parameters: QuantParameters; confirmed_count: number; preview_count: number; warmup_complete: boolean; semantics: string; token: Record<string, unknown>; rounding_policy: string };
  confirmed: QuantPoint | null; series?: Array<Pick<QuantPoint,"as_of"|"decision_at"|"indicators">>; bars: Array<{open_time:string;close:string}>; name:string; unit:string; executable_contract:boolean;
}
export interface QuantMetric { value: string | null; unavailable_reason: string | null }
export interface SimulationSummary {
  execution_version:string;
  cash:string; quantity:string; final_equity:string; realized_profit:string; unrealized_profit:string; return_percent:string; fees:string; slippage_cost:string; total_cost:string; maximum_drawdown:string; maximum_exposure:string;
  fills:number;closed_trades:number;wins:number;losses:number;breakeven:number;rejected_intents:number;unfilled_intents:number;
  win_rate:QuantMetric;average_win:QuantMetric;average_loss:QuantMetric;profit_factor:QuantMetric;payoff_ratio:QuantMetric;average_held_seconds:QuantMetric;
  risk_sampling:{frequency:string;calendar:string;samples:number;annual_factor:number;sharpe:QuantMetric;annualized_return:QuantMetric};
  ledger_semantics:string;benchmark:{return_percent:string;final_equity:string;unavailable_reason:string|null};config:SimulationConfig;
}
export interface SimulationRun { id:string;status:"running"|"completed"|"cancelled"|"failed"|"interrupted";phase:string;created_at:string;processed_bars:string;event_pages:number;complete:boolean;error?:string;input_hash?:string;snapshot_hash?:string;run_hash?:string;market_data_hash?:string;canonical_file_sha256?:string;base_run_id?:string;semantics?:string;warmup_complete?:boolean;summary?:SimulationSummary;config:SimulationConfig;request:{code:string;source_id?:string;period:string;research_asset?:string;research_adjustment?:string;research_snapshot_id?:string;decision_as_of?:string;application_cursor?:string} }
export interface SimulationEvent {type:"fill"|"trade"|"equity"|"decision"|"rejected"|"unfilled";value:Record<string,unknown>}
