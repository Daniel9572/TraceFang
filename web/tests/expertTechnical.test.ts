import assert from "node:assert/strict";
import test from "node:test";

import {
  bollingerSnapshotAt,
  buildSmartTrendLines,
  buildTechnicalOverlaySeries,
  candlePrefixRevisionKey,
  latestFinalCandleIndex,
  momentumSnapshotAt,
  movingAverageSnapshotAt,
  nineCountSnapshotAt,
  technicalBarsFromCandles,
} from "../src/expertTechnical.ts";
import type { Candle } from "../src/types.ts";

function candle(index: number, close: number, volume: number | null = 100 + index): Candle {
  const open = close - 0.15;
  return {
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    interval: 60,
    open_time: new Date(Date.UTC(2025, 0, 1, 0, index)).toISOString(),
    open,
    high: Math.max(open, close) + 0.45,
    low: Math.min(open, close) - 0.45,
    close,
    volume,
    source: {
      provider: "test",
      provider_symbol: "XAUUSD",
      observed_at: new Date().toISOString(),
      received_at: new Date().toISOString(),
    },
    evidence_channel_id: "test",
    state: "final",
    revision: 1,
    finalized_at: new Date().toISOString(),
  };
}

test("classifies MA20/60/120/250 alignment without treating missing long history as evidence", () => {
  const candles = Array.from({ length: 300 }, (_, index) => candle(index, 1800 + index * 0.8));
  const bars = technicalBarsFromCandles(candles);
  const snapshot = movingAverageSnapshotAt(bars, bars.length - 1);

  assert.equal(snapshot?.alignment, "bullish");
  assert.deepEqual(snapshot?.values.map((item) => item.period), [20, 60, 120, 250]);
  assert.ok(snapshot?.values.every((item) => item.value !== null));

  const short = movingAverageSnapshotAt(bars.slice(0, 45), 44);
  assert.equal(short?.alignment, "insufficient");
  assert.equal(short?.values.find((item) => item.period === 60)?.value, null);
});

test("compares a break with the previous MA instead of the moved current MA", () => {
  const candles = Array.from(
    { length: 20 },
    (_, index) => candle(index, index === 19 ? 100.01 : 100),
  );
  candles.push(candle(20, 110));
  const snapshot = movingAverageSnapshotAt(technicalBarsFromCandles(candles), 20);

  assert.equal(snapshot?.values.find((item) => item.period === 20)?.interaction, "none");
});

test("uses rolling bandwidth percentile and requires expansion before a Bollinger breakout", () => {
  const candles = Array.from(
    { length: 100 },
    (_, index) => candle(index, index === 99 ? 110 : 100 + Math.sin(index / 4) * 0.08),
  );
  const bars = technicalBarsFromCandles(candles);
  const before = bollingerSnapshotAt(bars, 90);
  const after = bollingerSnapshotAt(bars, 99);

  assert.equal(before?.state, "squeeze");
  assert.equal(after?.state, "expanding");
  assert.ok((after?.position ?? 0) > 1);
});

test("implements only causal nine-count Setup and resets when the four-bar comparison flips", () => {
  const rising = Array.from({ length: 13 }, (_, index) => candle(index, 100 + index));
  const bars = technicalBarsFromCandles(rising);
  assert.deepEqual(nineCountSnapshotAt(bars, 12), {
    direction: "sell-setup",
    count: 9,
    perfected: true,
    completedNow: true,
  });

  const extended = [...rising, candle(13, 113)];
  assert.deepEqual(nineCountSnapshotAt(technicalBarsFromCandles(extended), 13), {
    direction: "sell-setup",
    count: 9,
    perfected: true,
    completedNow: false,
  });

  const flipped = [...rising, candle(13, 90)];
  const reset = nineCountSnapshotAt(technicalBarsFromCandles(flipped), 13);
  assert.equal(reset?.direction, "buy-setup");
  assert.equal(reset?.count, 1);
  assert.equal(reset?.completedNow, false);
});

test("builds volatility-normalized multi-horizon momentum only from available horizons", () => {
  const candles = Array.from({ length: 130 }, (_, index) => candle(index, 2000 + index * 0.5));
  const bars = technicalBarsFromCandles(candles);
  const complete = momentumSnapshotAt(bars, 129);
  const partial = momentumSnapshotAt(bars, 40);

  assert.equal(complete?.availableHorizons, 3);
  assert.ok((complete?.score ?? 0) > 0);
  assert.equal(partial?.availableHorizons, 1);
});

test("smart trend lines are cutoff-causal and preserve invalidated lines as a separate lifecycle", () => {
  const base = Array.from(
    { length: 110 },
    (_, index) => candle(index, 100 + index * 0.08 + Math.sin(index / 3) * 2.2),
  );
  const future = [
    candle(110, 86),
    candle(111, 84),
    candle(112, 83),
  ];
  const atCutoff = buildSmartTrendLines(base, base.length - 1);
  const withUnknownFuture = buildSmartTrendLines([...base, ...future], base.length - 1);
  assert.deepEqual(withUnknownFuture, atCutoff);
  const invalidRow = { ...candle(55, 100), high: 1, low: 2 };
  const filteredPrefixWithFuture = [
    ...base.slice(0, 55),
    invalidRow,
    ...base.slice(55),
    ...future,
  ];
  assert.deepEqual(
    buildSmartTrendLines(filteredPrefixWithFuture, base.length),
    atCutoff,
  );
  assert.ok(atCutoff.some((line) => line.status !== "invalidated"));

  const afterBreak = buildSmartTrendLines([...base, ...future]);
  assert.ok(afterBreak.some((line) => line.status === "invalidated"));
  assert.ok(afterBreak.every((line) => line.end.time <= Date.parse(future.at(-1)!.open_time) / 1_000));
});

test("retains a newly invalidated trend line even when its second anchor is old", () => {
  const oldStructure = Array.from(
    { length: 40 },
    (_, index) => candle(index, 100 + Math.sin(index / 3) * 2),
  );
  const quietExtension = Array.from(
    { length: 130 },
    (_, offset) => candle(40 + offset, 105 + offset * 0.015),
  );
  const recentBreak = [candle(170, 91), candle(171, 89), candle(172, 88)];
  const lines = buildSmartTrendLines([...oldStructure, ...quietExtension, ...recentBreak]);
  const invalidatedSupport = lines.find((line) => (
    line.direction === "support" && line.status === "invalidated"
  ));

  assert.ok(invalidatedSupport);
  assert.ok(
    (Date.parse(recentBreak.at(-1)!.open_time) / 1_000 - invalidatedSupport.anchor.time) / 60 > 80,
  );
});

test("builds MA and Bollinger main-chart overlays only when their strategies are enabled", () => {
  const candles = Array.from({ length: 280 }, (_, index) => candle(index, 1900 + index * 0.2));
  const overlays = buildTechnicalOverlaySeries(candles, ["ma-structure", "bollinger"]);
  const ids = new Set(overlays.map((item) => item.id));

  assert.ok(ids.has("ma-20"));
  assert.ok(ids.has("ma-250"));
  assert.ok(ids.has("bollinger-upper"));
  assert.ok(overlays.every((series) => series.points.length > 0));
  assert.deepEqual(buildTechnicalOverlaySeries(candles, []), []);
});

test("keeps confirmed technical evidence stable while the tail Bar is provisional", () => {
  const completed = Array.from(
    { length: 130 },
    (_, index) => candle(index, 1900 + Math.sin(index / 4) * 4 + index * 0.1),
  );
  const provisional = {
    ...candle(130, 2100),
    state: "provisional_quote" as const,
    finalized_at: null,
  };
  const extended = [...completed, provisional];
  const cutoff = latestFinalCandleIndex(extended);

  assert.equal(cutoff, completed.length - 1);
  assert.deepEqual(
    buildTechnicalOverlaySeries(extended, ["ma-structure", "bollinger"], cutoff),
    buildTechnicalOverlaySeries(completed, ["ma-structure", "bollinger"]),
  );
  assert.deepEqual(
    buildSmartTrendLines(extended, cutoff),
    buildSmartTrendLines(completed),
  );
});

test("confirmed prefix keys ignore provisional ticks but detect a middle final correction", () => {
  const completed = Array.from({ length: 40 }, (_, index) => candle(index, 1900 + index));
  const provisional = {
    ...candle(40, 1940),
    state: "provisional_quote" as const,
    finalized_at: null,
  };
  const cutoff = completed.length - 1;
  const firstTickKey = candlePrefixRevisionKey([...completed, provisional], cutoff);
  const nextTickKey = candlePrefixRevisionKey(
    [...completed, { ...provisional, close: 1941, revision: 2 }],
    cutoff,
  );
  assert.equal(nextTickKey, firstTickKey);

  const corrected = completed.map((item, index) => (
    index === 18
      ? { ...item, close: item.close + 0.2, high: item.high + 0.2, revision: item.revision + 1 }
      : item
  ));
  assert.notEqual(candlePrefixRevisionKey(corrected, cutoff), firstTickKey);
});
