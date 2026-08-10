import assert from "node:assert/strict";
import test from "node:test";

import {
  latestSmartMoneySetup,
  marketStructureEventsAt,
} from "../src/expertSmartMoney.ts";
import type { TechnicalBar } from "../src/expertTechnical.ts";

function bar(index: number, close: number, high = close + 0.4, low = close - 0.4): TechnicalBar {
  return { time: 1_700_000_000 + index * 60, open: close, high, low, close, volume: 100 };
}

test("detects a failed excursion beyond a confirmed swing without inventing order identity", () => {
  const bars = Array.from({ length: 24 }, (_, index) => bar(index, 100 + index * 0.04));
  bars[8] = bar(8, 101, 103, 100.7);
  bars[9] = bar(9, 100.8, 101.1, 100.5);
  bars[10] = bar(10, 100.5, 100.8, 100.2);
  bars[18] = bar(18, 102.5, 103.45, 102.1);
  const events = marketStructureEventsAt(bars);
  const sweep = events.find((event) => event.kind === "high-liquidity-sweep");

  assert.ok(sweep);
  assert.equal(sweep.direction, "bearish");
  assert.equal(sweep.label, "SWEEP");
  assert.ok(sweep.evidence.some((item) => item.includes("摆动高点")));
});

test("requires a same-direction sweep before promoting a fresh structure break to a setup", () => {
  const events = [
    {
      id: "sweep",
      kind: "low-liquidity-sweep" as const,
      label: "SWEEP",
      direction: "bullish" as const,
      status: "confirmed" as const,
      reference: { index: 20, time: 20, price: 100 },
      confirmation: { index: 24, time: 24, price: 101 },
      detectedAt: 24,
      invalidatedAt: null,
      confidence: 0.64,
      evidence: [],
    },
    {
      id: "shift",
      kind: "bullish-choch" as const,
      label: "CHOCH",
      direction: "bullish" as const,
      status: "confirmed" as const,
      reference: { index: 22, time: 22, price: 103 },
      confirmation: { index: 29, time: 29, price: 104 },
      detectedAt: 29,
      invalidatedAt: null,
      confidence: 0.68,
      evidence: [],
    },
  ];
  const setup = latestSmartMoneySetup(events, 29);

  assert.equal(setup?.direction, "bullish");
  assert.ok((setup?.confidence ?? 0) > 0.7);
  assert.equal(latestSmartMoneySetup(events, 30), null);
  assert.equal(latestSmartMoneySetup([events[1]], 29), null);
});

test("is cutoff-causal when future bars are present", () => {
  const bars = Array.from({ length: 80 }, (_, index) => (
    bar(index, 100 + Math.sin(index / 3) * 2 + index * 0.02)
  ));
  const cutoff = 60;
  const atCutoff = marketStructureEventsAt(bars.slice(0, cutoff + 1));
  const withFuture = marketStructureEventsAt(bars, cutoff);
  assert.deepEqual(withFuture, atCutoff);
});

test("does not promote opposite, stale, or invalidated sweeps into a setup", () => {
  const shift = {
    id: "shift",
    kind: "bullish-bos" as const,
    label: "BOS",
    direction: "bullish" as const,
    status: "confirmed" as const,
    reference: { index: 28, time: 28, price: 103 },
    confirmation: { index: 30, time: 30, price: 104 },
    detectedAt: 30,
    invalidatedAt: null,
    confidence: 0.66,
    evidence: [],
  };
  const sweep = {
    id: "sweep",
    kind: "low-liquidity-sweep" as const,
    label: "SWEEP",
    direction: "bullish" as const,
    status: "confirmed" as const,
    reference: { index: 24, time: 24, price: 100 },
    confirmation: { index: 25, time: 25, price: 101 },
    detectedAt: 25,
    invalidatedAt: null,
    confidence: 0.64,
    evidence: [],
  };

  assert.equal(
    latestSmartMoneySetup([{ ...sweep, direction: "bearish" }, shift], 30),
    null,
  );
  assert.equal(
    latestSmartMoneySetup([{ ...sweep, confirmation: { ...sweep.confirmation, index: 21 } }, shift], 30),
    null,
  );
  assert.equal(
    latestSmartMoneySetup([{ ...sweep, status: "invalidated", invalidatedAt: 28 }, shift], 30),
    null,
  );
  assert.equal(
    latestSmartMoneySetup([sweep, { ...shift, status: "invalidated", invalidatedAt: 31 }], 30),
    null,
  );
});

test("keeps an invalidated sweep as auditable history instead of deleting it", () => {
  const prefix = Array.from({ length: 24 }, (_, index) => bar(index, 100 + index * 0.04));
  prefix[8] = bar(8, 101, 103, 100.7);
  prefix[9] = bar(9, 100.8, 101.1, 100.5);
  prefix[10] = bar(10, 100.5, 100.8, 100.2);
  prefix[18] = bar(18, 102.5, 103.45, 102.1);
  const confirmed = marketStructureEventsAt(prefix)
    .find((event) => event.kind === "high-liquidity-sweep");
  assert.ok(confirmed);

  const invalidating = bar(24, 110, 110.4, 109.6);
  const invalidated = marketStructureEventsAt([...prefix, invalidating])
    .find((event) => event.id === confirmed.id);
  assert.ok(invalidated);
  assert.equal(invalidated.status, "invalidated");
  assert.equal(invalidated.invalidatedAt, invalidating.time);
});

test("a wick through structure is a sweep candidate, not a close-confirmed BOS", () => {
  const bars = Array.from({ length: 24 }, (_, index) => bar(index, 100 + index * 0.04));
  bars[8] = bar(8, 101, 103, 100.7);
  bars[9] = bar(9, 100.8, 101.1, 100.5);
  bars[10] = bar(10, 100.5, 100.8, 100.2);
  bars[18] = bar(18, 102.5, 103.45, 102.1);

  const atWick = marketStructureEventsAt(bars, 18);
  assert.ok(atWick.some((event) => event.kind === "high-liquidity-sweep"));
  assert.equal(
    atWick.some((event) => event.confirmation.index === 18 && event.kind === "bullish-bos"),
    false,
  );
});

test("setup confidence is capped and its evidence disclaims order identity", () => {
  const setup = latestSmartMoneySetup([
    {
      id: "sweep",
      kind: "low-liquidity-sweep",
      label: "SWEEP",
      direction: "bullish",
      status: "confirmed",
      reference: { index: 20, time: 20, price: 100 },
      confirmation: { index: 24, time: 24, price: 101 },
      detectedAt: 24,
      invalidatedAt: null,
      confidence: 0.99,
      evidence: [],
    },
    {
      id: "shift",
      kind: "bullish-choch",
      label: "CHOCH",
      direction: "bullish",
      status: "confirmed",
      reference: { index: 27, time: 27, price: 103 },
      confirmation: { index: 29, time: 29, price: 104 },
      detectedAt: 29,
      invalidatedAt: null,
      confidence: 0.99,
      evidence: [],
    },
  ], 29);

  assert.ok(setup);
  assert.equal(setup.confidence, 0.82);
  assert.ok(setup.evidence.some((item) => item.includes("不证明机构订单身份")));
});
