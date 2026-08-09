import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExpertSessionBands,
  dominantGoldSessionAt,
  EXPERT_HOLIDAY_CLOSURES_2026,
} from "../src/expertSessions.ts";

const epoch = (value: string) => Date.parse(value) / 1_000;

test("uses IANA daylight saving rules for the US gold session", () => {
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T13:30:00Z")), "us");
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T12:30:00Z")), "us");
});

test("uses IANA daylight saving rules for the London session", () => {
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T08:30:00Z")), "europe");
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T07:30:00Z")), "europe");
});

test("builds Asia and US bands without spanning a holiday closure", () => {
  const values = [
    epoch("2026-04-02T01:00:00Z"),
    epoch("2026-04-02T01:01:00Z"),
    epoch("2026-04-03T13:00:00Z"),
    epoch("2026-04-06T13:00:00Z"),
    epoch("2026-04-06T13:01:00Z"),
  ];
  const bands = buildExpertSessionBands(values, null, EXPERT_HOLIDAY_CLOSURES_2026);
  assert.ok(bands.some((band) => band.kind === "asia"));
  assert.ok(bands.some((band) => band.kind === "europe"));
  assert.ok(bands.some((band) => band.kind === "us"));
  const goodFridayUS = epoch("2026-04-03T13:00:00Z");
  assert.equal(bands.some((band) => goodFridayUS >= band.start && goodFridayUS < band.end), false);
});

test("session band count depends on covered days rather than realtime sample count", () => {
  const start = epoch("2026-07-15T00:00:00Z");
  const dense = Array.from({ length: 20_001 }, (_, index) => start + index);
  const sparse = [start, dense.at(-1)!];
  assert.deepEqual(
    buildExpertSessionBands(dense, null, []),
    buildExpertSessionBands(sparse, null, []),
  );
});
