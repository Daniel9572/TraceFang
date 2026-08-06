import assert from "node:assert/strict";
import test from "node:test";

import {
  aggregateCandles,
  formatBarCountdown,
  mergeLivePrice,
  secondsUntilBarClose,
} from "../src/chartModel.ts";
import type { Candle } from "../src/types.ts";

function candle(time: string, open: number, high: number, low: number, close: number): Candle {
  return {
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    interval: 60,
    open_time: time,
    open,
    high,
    low,
    close,
    volume: null,
    source: {
      provider: "test",
      provider_symbol: "XAUUSD",
      observed_at: time,
      received_at: time,
    },
  };
}

test("aggregates minute candles into the selected interval", () => {
  const bars = aggregateCandles(
    [
      candle("2026-08-06T01:00:00Z", 100, 102, 99, 101),
      candle("2026-08-06T01:01:00Z", 101, 104, 100, 103),
      candle("2026-08-06T01:05:00Z", 103, 105, 102, 104),
    ],
    5,
  );

  assert.equal(bars.length, 2);
  assert.deepEqual(
    { open: bars[0].open, high: bars[0].high, low: bars[0].low, close: bars[0].close },
    { open: 100, high: 104, low: 99, close: 103 },
  );
});

test("updates the current bar from a live quote without mutating history", () => {
  const currentTime = Math.floor(Date.parse("2026-08-06T01:47:00Z") / 1000);
  const original = [{ time: currentTime, open: 100, high: 102, low: 99, close: 101 }];
  const updated = mergeLivePrice(original, 1, 103, "2026-08-06T01:47:45Z");

  assert.equal(original[0].close, 101);
  assert.equal(updated.at(-1)?.close, 103);
  assert.equal(updated.at(-1)?.high, 103);
});

test("opens a new bar from the previous close when a quote crosses the boundary", () => {
  const previousTime = Math.floor(Date.parse("2026-08-06T01:47:00Z") / 1000);
  const updated = mergeLivePrice(
    [{ time: previousTime, open: 100, high: 102, low: 99, close: 101 }],
    1,
    100.5,
    "2026-08-06T01:48:03Z",
  );

  assert.equal(updated.length, 2);
  assert.deepEqual(updated.at(-1), {
    time: previousTime + 60,
    open: 101,
    high: 101,
    low: 100.5,
    close: 100.5,
  });
});

test("formats a stable exchange-style countdown", () => {
  const atThirtySeconds = Date.parse("2026-08-06T01:47:30Z");
  assert.equal(secondsUntilBarClose(1, atThirtySeconds), 30);
  assert.equal(secondsUntilBarClose(60, atThirtySeconds), 750);
  assert.equal(formatBarCountdown(750), "00:12:30");
});
