import assert from "node:assert/strict";
import { test } from "node:test";
import { researchApi, sourcePeriods } from "../src/researchApi.ts";
import type { ResearchSource } from "../src/researchApi.ts";

test("AKShare periods follow the selected asset rather than the provider union", () => {
  const source = {
    periods: ["1m", "5m", "1h", "1d", "1w", "1M"],
    asset_periods: {
      future: ["1m", "5m", "1h", "1d"],
      option: ["1d"],
      equity: ["1d", "1w", "1M"],
    },
  } as ResearchSource;
  assert.deepEqual(sourcePeriods(source, "option"), ["1d"]);
  assert.deepEqual(sourcePeriods(source, "equity"), ["1d", "1w", "1M"]);
  assert.deepEqual(sourcePeriods(source, "future"), ["1m", "5m", "1h", "1d"]);
  assert.deepEqual(sourcePeriods(undefined, "future"), ["1d"]);
});

test("domestic chain requests send contract month and preserve cancellation", async (t) => {
  const calls: Array<{ path: string; init: RequestInit }> = [];
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    calls.push({ path, init });
    return { ok: true, json: async () => ({ contracts: [] }) };
  });
  const abort = new AbortController();
  await researchApi.chain("510050.SH", undefined, abort.signal, "akshare", "202610");
  const query = new URL(calls[0].path, "http://local").searchParams;
  assert.equal(query.get("source"), "akshare");
  assert.equal(query.get("month"), "202610");
  assert.equal(query.has("expiry"), false);
  assert.equal(calls[0].init.signal, abort.signal);
  await researchApi.chain("SPY", "2026-10-16");
  const legacy = new URL(calls[1].path, "http://local").searchParams;
  assert.equal(legacy.get("source"), "alpaca");
  assert.equal(legacy.get("expiry"), "2026-10-16");
  assert.equal(legacy.has("month"), false);
});
