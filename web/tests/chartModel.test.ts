import assert from "node:assert/strict";
import test from "node:test";

import { mergeCandleRows } from "../src/api.ts";
import {
  appendTimelineSample,
  buildTimelineSeries,
  formatBarCountdown,
  mergeTimelineSamples,
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
    evidence_channel_id: "test",
    state: "final",
    revision: 1,
    finalized_at: time,
  };
}

test("formats a stable exchange-style countdown", () => {
  const atThirtySeconds = Date.parse("2026-08-06T01:47:30Z");
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1m"), atThirtySeconds), 30);
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1h"), atThirtySeconds), 750);
  assert.equal(formatBarCountdown(750), "00:12:30");
});

test("uses native quote samples instead of a synthetic minute close for the same minute", () => {
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

  assert.deepEqual(series.map((point) => point.value), [101.25, 101.5]);
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
  assert.equal(second[0].time, sourceSecond + 0.125);
  assert.equal(second[1].time, sourceSecond + 0.125);
});

test("does not truncate the timeline after twenty thousand events", () => {
  const samples = Array.from({ length: 20_000 }, (_, index) => ({
    time: index,
    value: index,
    eventId: `event-${index}`,
  }));
  const result = appendTimelineSample(samples, {
    time: 20_000,
    value: 20_000,
    eventId: "event-20000",
  });

  assert.equal(result.length, 20_001);
  assert.equal(mergeTimelineSamples(result).length, 20_001);
});

test("merges backend Bar revisions without letting an older page overwrite a correction", () => {
  const original = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const corrected = {
    ...original,
    high: 104,
    close: 103,
    revision: 2,
    source: { ...original.source, received_at: "2026-08-06T01:49:00Z" },
  };

  const [result] = mergeCandleRows([corrected], [original]);

  assert.equal(result.revision, 2);
  assert.equal(result.close, 103);
});
