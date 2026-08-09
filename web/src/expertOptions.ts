import type {
  ExpertOptionContract,
  ExpertOptionExpiryAnalysis,
} from "./expertTypes";

export interface ExpertOptionStrikeRow {
  strike: number;
  call: ExpertOptionContract | null;
  put: ExpertOptionContract | null;
  isAtm: boolean;
  isCallWall: boolean;
  isPutWall: boolean;
  isMaxPain: boolean;
}

export function expertOptionExpiryKey(expiry: ExpertOptionExpiryAnalysis): string {
  return `${expiry.underlying_contract_id}|${expiry.expiry}`;
}

export function resolveExpertOptionExpiry(
  expiries: ExpertOptionExpiryAnalysis[],
  selectedKey: string | null,
): ExpertOptionExpiryAnalysis | null {
  if (expiries.length === 0) return null;
  return expiries.find((expiry) => expertOptionExpiryKey(expiry) === selectedKey) ?? expiries[0];
}

function sameStrike(left: number | null, right: number): boolean {
  return left !== null && Number.isFinite(left) && Math.abs(left - right) < 1e-9;
}

export function buildExpertOptionStrikeRows(
  contracts: ExpertOptionContract[],
  expiry: ExpertOptionExpiryAnalysis | null,
): ExpertOptionStrikeRow[] {
  if (expiry === null) return [];
  const byStrike = new Map<number, { call: ExpertOptionContract | null; put: ExpertOptionContract | null }>();
  for (const contract of contracts) {
    if (
      contract.underlying_contract_id !== expiry.underlying_contract_id
      || contract.expiry !== expiry.expiry
      || !Number.isFinite(contract.strike)
    ) continue;
    const pair = byStrike.get(contract.strike) ?? { call: null, put: null };
    if (contract.option_type === "call") pair.call = contract;
    if (contract.option_type === "put") pair.put = contract;
    byStrike.set(contract.strike, pair);
  }
  return [...byStrike.entries()]
    .sort(([left], [right]) => left - right)
    .map(([strike, pair]) => ({
      strike,
      ...pair,
      isAtm: sameStrike(expiry.atm_strike, strike),
      isCallWall: sameStrike(expiry.call_wall_strike, strike),
      isPutWall: sameStrike(expiry.put_wall_strike, strike),
      isMaxPain: sameStrike(expiry.max_pain_strike, strike),
    }));
}

export function optionPositioningLabel(state: string): string {
  if (state === "put_open_interest_dominant") return "Put 持仓占优";
  if (state === "call_open_interest_dominant") return "Call 持仓占优";
  if (state === "balanced_open_interest") return "持仓相对均衡";
  return "样本不足";
}
