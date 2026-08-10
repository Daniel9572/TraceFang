import assert from "node:assert/strict";
import test from "node:test";

import { completedReplayHistory, formatReplayTimecode } from "../src/expertReplay.ts";
import type { Candle } from "../src/types.ts";

function candle(openTime: string): Candle {
  return {
    instrument: {
      symbol: "XAU/USD",
      asset_class: "spot",
      base: "XAU",
      quote: "USD",
      venue: "OTC",
    },
    interval: 60,
    open_time: openTime,
    open: 100,
    high: 100,
    low: 100,
    close: 100,
    volume: null,
    source: {
      provider: "jin10_client",
      provider_symbol: "XAUUSD",
      observed_at: openTime,
      received_at: openTime,
    },
    evidence_channel_id: "jin10_web",
    state: "final",
    revision: 1,
    finalized_at: openTime,
  };
}

test("formats exact replay frame time with milliseconds and explicit zone", () => {
  assert.equal(
    formatReplayTimecode("2026-08-10T16:12:00.123Z"),
    "2026-08-11 00:12:00.123 · Asia/Shanghai · UTC+08:00",
  );
});

test("seeds only Bars completed before the replay cursor", () => {
  const rows = [
    candle("2026-08-10T12:00:00Z"),
    candle("2026-08-10T12:01:00Z"),
    candle("2026-08-10T12:02:00Z"),
  ];
  const cutoff = Date.parse("2026-08-10T12:02:30Z") / 1_000;

  assert.deepEqual(completedReplayHistory(rows, cutoff), rows.slice(0, 2));
});
