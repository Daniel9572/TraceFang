import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExpertOptionStrikeRows,
  expertOptionExpiryKey,
  resolveExpertOptionExpiry,
} from "../src/expertOptions.ts";
import type { ExpertOptionContract, ExpertOptionExpiryAnalysis } from "../src/expertTypes.ts";

function expiry(
  underlying_contract_id: string,
  expiryDate: string,
): ExpertOptionExpiryAnalysis {
  return {
    underlying_contract_id,
    expiry: expiryDate,
    underlying_price: 786,
    option_count: 6,
    call_open_interest: 320,
    put_open_interest: 280,
    put_call_open_interest_ratio: 0.875,
    call_volume: 80,
    put_volume: 70,
    put_call_volume_ratio: 0.875,
    atm_strike: 780,
    call_wall_strike: 800,
    put_wall_strike: 760,
    max_pain_strike: 780,
    reference_iv: 0.21,
    expected_move_percent: 4.2,
    delta_coverage_ratio: 1,
    positioning_state: "balanced_open_interest",
    gamma_state: "unavailable_missing_contract_gamma_and_dealer_position",
    gex: null,
  };
}

function contract(
  strike: number,
  option_type: "call" | "put",
  contractId = `au2608${option_type === "call" ? "C" : "P"}${strike}`,
): ExpertOptionContract {
  return {
    contract_id: contractId,
    underlying_contract_id: "au2608",
    expiry: "2026-07-24",
    strike,
    option_type,
    contract_multiplier: 1_000,
    bid: 10,
    ask: 11,
    last: 10.5,
    previous_settlement: 10.2,
    volume: 20,
    open_interest: 100,
    open_interest_change: 3,
    turnover: 210_000,
    observed_at: "2026-07-10T06:15:00Z",
    delta: option_type === "call" ? 0.5 : -0.5,
    delta_as_of: "2026-07-09",
  };
}

test("keeps the selected expiry when it still exists and falls back deterministically", () => {
  const first = expiry("au2608", "2026-07-24");
  const second = expiry("au2610", "2026-09-24");
  assert.equal(expertOptionExpiryKey(second), "au2610|2026-09-24");
  assert.equal(resolveExpertOptionExpiry([first, second], "au2610|2026-09-24"), second);
  assert.equal(resolveExpertOptionExpiry([first, second], "expired|contract"), first);
  assert.equal(resolveExpertOptionExpiry([], null), null);
});

test("pairs the complete selected chain by strike without a row cap", () => {
  const selected = expiry("au2608", "2026-07-24");
  const strikes = Array.from({ length: 41 }, (_, index) => 580 + index * 10);
  const contracts = strikes.flatMap((strike) => [contract(strike, "call"), contract(strike, "put")]);
  contracts.push({ ...contract(800, "call"), underlying_contract_id: "au2610" });

  const rows = buildExpertOptionStrikeRows(contracts, selected);

  assert.equal(rows.length, 41);
  assert.equal(rows[0]?.strike, 580);
  assert.equal(rows.at(-1)?.strike, 980);
  assert.equal(rows.find((row) => row.strike === 780)?.isAtm, true);
  assert.equal(rows.find((row) => row.strike === 760)?.isPutWall, true);
  assert.equal(rows.find((row) => row.strike === 800)?.isCallWall, true);
  assert.ok(rows.every((row) => row.call !== null && row.put !== null));
});

test("preserves one-sided strikes instead of inventing the missing quote", () => {
  const rows = buildExpertOptionStrikeRows(
    [contract(780, "call"), contract(790, "put")],
    expiry("au2608", "2026-07-24"),
  );
  assert.equal(rows[0]?.call?.contract_id, "au2608C780");
  assert.equal(rows[0]?.put, null);
  assert.equal(rows[1]?.call, null);
  assert.equal(rows[1]?.put?.contract_id, "au2608P790");
});
