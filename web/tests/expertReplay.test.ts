import assert from "node:assert/strict";
import test from "node:test";

import {
  createReplayProjectionStart,
  formatReplayTimecode,
  REPLAY_DERIVED_DOMAIN_NOTICE,
  replayStreamQuery,
  replaySafeLiveDerivedValue,
  REPLAY_RATE_LABEL,
} from "../src/expertReplay.ts";
import type { ReplayFrameBounds } from "../src/types.ts";

test("formats exact replay frame time with milliseconds and explicit zone", () => {
  assert.equal(
    formatReplayTimecode("2026-08-10T16:12:00.123Z"),
    "2026-08-11 00:12:00.123 · Asia/Shanghai · UTC+08:00",
  );
});

test("starts an isolated replay empty at the first retained raw frame", () => {
  const bounds: ReplayFrameBounds = {
    state: "ready",
    first_sequence: 41,
    last_sequence: 89,
    message_count: 49,
    first_received_at: "2026-08-10T12:00:00Z",
    last_received_at: "2026-08-10T12:03:00Z",
    source_ids: ["jin10_client", "tonghuashun_futures"],
    detail: null,
  };

  assert.deepEqual(createReplayProjectionStart(bounds, "1s"), {
    period: "1s",
    startSequence: 41,
    endSequence: 89,
    candles: [],
    price: null,
  });
});

test("builds a ReplayOriginal stream URL without a client speed parameter", () => {
  const query = replayStreamQuery({
    period: "3m",
    startSequence: 41,
    endSequence: 89,
  });

  assert.equal(query, "period=3m&start_sequence=41&end_sequence=89");
  assert.doesNotMatch(query, /speed/i);
  assert.equal(REPLAY_RATE_LABEL, "ReplayOriginal · 1× 原速");
});

test("isolates every live-only derived domain while a replay projection exists", () => {
  const currentLiveSnapshots = [
    { domain: "options", value: 1 },
    { domain: "volatility", value: 2 },
    { domain: "multi-timeframe", value: 3 },
    { domain: "positioning", value: 4 },
    { domain: "ai-analysis", value: 5 },
  ];

  assert.equal(
    replaySafeLiveDerivedValue("live", currentLiveSnapshots),
    currentLiveSnapshots,
  );
  for (const state of ["stopped", "playing", "completed"] as const) {
    assert.equal(replaySafeLiveDerivedValue(state, currentLiveSnapshots), null);
  }
  assert.match(REPLAY_DERIVED_DOMAIN_NOTICE, /历史域/);
  assert.match(REPLAY_DERIVED_DOMAIN_NOTICE, /隔离/);
});
