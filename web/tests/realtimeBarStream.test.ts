import assert from "node:assert/strict";
import test from "node:test";

import {
  RealtimeBarStream,
  realtimeBarDatasetKey,
} from "../src/realtimeBarStream.ts";
import type { Candle } from "../src/types.ts";

function bar(revision: number, close: number, openTime = "2026-08-11T01:00:00Z"): Candle {
  return {
    instrument: { symbol: "XAUUSD", asset_class: "commodity", venue: "OTC" },
    interval: 1,
    open_time: openTime,
    open: 100,
    high: Math.max(100, close),
    low: Math.min(100, close),
    close,
    volume: null,
    source: {
      provider: "jin10_client",
      provider_symbol: "XAUUSD",
      observed_at: openTime,
      received_at: openTime,
      raw_payload: null,
    },
    evidence_channel_id: "jin10_web",
    state: "provisional_quote",
    revision,
    finalized_at: null,
  };
}

test("delivers every ordered same-second revision even when the price returns", () => {
  const stream = new RealtimeBarStream();
  const key = realtimeBarDatasetKey("XAUUSD", "jin10_client", "1s");
  const delivered: number[] = [];
  stream.subscribe(({ bar: value }) => delivered.push(Number(value.close)));

  assert.equal(stream.publish(key, bar(1, 100)), true);
  assert.equal(stream.publish(key, bar(2, 102)), true);
  assert.equal(stream.publish(key, bar(3, 100)), true);
  assert.deepEqual(delivered, [100, 102, 100]);
});

test("drops only stale or exact transport revisions", () => {
  const stream = new RealtimeBarStream();
  const key = realtimeBarDatasetKey("XAUUSD", "jin10_client", "1s");
  const delivered: number[] = [];
  stream.subscribe(({ bar: value }) => delivered.push(Number(value.close)));

  const first = bar(4, 101);
  assert.equal(stream.publish(key, first), true);
  assert.equal(stream.publish(key, first), false);
  assert.equal(stream.publish(key, bar(3, 99)), false);
  assert.equal(stream.publish(key, bar(5, 101)), true);
  assert.deepEqual(delivered, [101, 101]);
});

test("accepts the next second and keeps datasets independent", () => {
  const stream = new RealtimeBarStream();
  const firstKey = realtimeBarDatasetKey("XAUUSD", "jin10_client", "1s");
  const secondKey = realtimeBarDatasetKey("XAUCNHG", "jin10_client", "1s");
  const delivered: string[] = [];
  stream.subscribe(({ datasetKey }) => delivered.push(datasetKey));

  assert.equal(stream.publish(firstKey, bar(7, 100)), true);
  assert.equal(stream.publish(firstKey, bar(1, 103, "2026-08-11T01:00:01Z")), true);
  assert.equal(stream.publish(secondKey, bar(1, 700)), true);
  assert.deepEqual(delivered, [firstKey, firstKey, secondKey]);
});
