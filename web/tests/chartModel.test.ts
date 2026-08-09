import assert from "node:assert/strict";
import test from "node:test";

import {
  aggregateCandles,
  appendTimelineSample,
  buildChartBars,
  buildTimelineSeries,
  formatBarCountdown,
  mergeLivePrice,
} from "../src/chartModel.ts";
import { chartPeriodById, secondsUntilPeriodClose } from "../src/chartPeriods.ts";
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
    chartPeriodById("5m"),
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
  const updated = mergeLivePrice(original, chartPeriodById("1m"), 103, "2026-08-06T01:47:45Z");

  assert.equal(original[0].close, 101);
  assert.equal(updated.at(-1)?.close, 103);
  assert.equal(updated.at(-1)?.high, 103);
});

test("opens a new bar from the first same-source quote across the boundary", () => {
  const previousTime = Math.floor(Date.parse("2026-08-06T01:47:00Z") / 1000);
  const updated = mergeLivePrice(
    [{ time: previousTime, open: 100, high: 102, low: 99, close: 101 }],
    chartPeriodById("1m"),
    100.5,
    "2026-08-06T01:48:03Z",
  );

  assert.equal(updated.length, 2);
  assert.deepEqual(updated.at(-1), {
    time: previousTime + 60,
    open: 100.5,
    high: 100.5,
    low: 100.5,
    close: 100.5,
  });
});

test("formats a stable exchange-style countdown", () => {
  const atThirtySeconds = Date.parse("2026-08-06T01:47:30Z");
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1m"), atThirtySeconds), 30);
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1h"), atThirtySeconds), 750);
  assert.equal(formatBarCountdown(750), "00:12:30");
});

test("aggregates a real three-minute period", () => {
  const bars = aggregateCandles(
    [
      candle("2026-08-06T01:00:00Z", 100, 101, 99, 100.5),
      candle("2026-08-06T01:01:00Z", 100.5, 103, 100, 102),
      candle("2026-08-06T01:02:00Z", 102, 104, 101, 103),
      candle("2026-08-06T01:03:00Z", 103, 105, 102, 104),
    ],
    chartPeriodById("3m"),
  );

  assert.equal(bars.length, 2);
  assert.deepEqual(
    { open: bars[0].open, high: bars[0].high, low: bars[0].low, close: bars[0].close },
    { open: 100, high: 104, low: 99, close: 103 },
  );
});

test("keeps calendar periods distinct and ordered", () => {
  const rows = [
    candle(new Date(2026, 0, 31, 23, 58).toISOString(), 100, 102, 99, 101),
    candle(new Date(2026, 1, 1, 0, 1).toISOString(), 101, 104, 100, 103),
  ];

  assert.equal(aggregateCandles(rows, chartPeriodById("1d")).length, 2);
  assert.equal(aggregateCandles(rows, chartPeriodById("1mo")).length, 2);
  assert.equal(aggregateCandles(rows, chartPeriodById("1y")).length, 1);
});

test("builds timeline history from minute closes and native quote samples", () => {
  const minuteTime = Math.floor(Date.parse("2026-08-06T01:47:00Z") / 1_000);
  const samples = appendTimelineSample(
    [],
    { time: minuteTime + 1, value: 101.25 },
  );
  const series = buildTimelineSeries(
    [candle("2026-08-06T01:47:00Z", 100, 102, 99, 101)],
    samples,
    101.5,
    "2026-08-06T01:47:02Z",
  );

  assert.deepEqual(series.slice(-3).map((point) => point.value), [101, 101.25, 101.5]);
});

test("preserves multiple quote events received inside the same source second", () => {
  const sourceSecond = Date.parse("2026-08-06T01:47:02Z") / 1_000;
  const first = appendTimelineSample([], {
    time: sourceSecond + 0.125,
    observedTime: sourceSecond,
    value: 101.25,
    eventId: "first",
  });
  const second = appendTimelineSample(first, {
    time: sourceSecond + 0.125,
    observedTime: sourceSecond,
    value: 99.75,
    eventId: "second",
  });

  assert.equal(second.length, 2);
  assert.equal(second[0].value, 101.25);
  assert.equal(second[1].value, 99.75);
  assert.ok(second[1].time > second[0].time);
});

test("uses every live quote when growing the current candle", () => {
  const minute = Date.parse("2026-08-06T01:47:00Z") / 1_000;
  const bars = buildChartBars(
    [candle("2026-08-06T01:47:00Z", 100, 100, 100, 100)],
    chartPeriodById("1m"),
    [
      { time: minute + 1.1, observedTime: minute + 1, value: 103, eventId: "one" },
      { time: minute + 1.2, observedTime: minute + 1, value: 97, eventId: "two" },
      { time: minute + 1.3, observedTime: minute + 1, value: 101, eventId: "three" },
    ],
    101,
    "2026-08-06T01:47:01Z",
  );

  assert.deepEqual(
    { open: bars[0].open, high: bars[0].high, low: bars[0].low, close: bars[0].close },
    { open: 100, high: 103, low: 97, close: 101 },
  );
});
