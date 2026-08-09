import assert from "node:assert/strict";
import test from "node:test";

import { chartPeriodById } from "../src/chartPeriods.ts";
import {
  historyBatchMinutes,
  historyWindowBefore,
  prependedPointCount,
  shouldRequestOlderHistory,
} from "../src/historyLoading.ts";

test("requests older history only for a user gesture near the left edge", () => {
  const nearEdge = { from: 8, to: 88 };

  assert.equal(shouldRequestOlderHistory(nearEdge, 100, false, true), true);
  assert.equal(shouldRequestOlderHistory({ from: 18, to: 98 }, 100, false, true), false);
  assert.equal(shouldRequestOlderHistory(nearEdge, 100, true, true), false);
  assert.equal(shouldRequestOlderHistory(nearEdge, 100, false, false), false);
  assert.equal(shouldRequestOlderHistory(null, 100, false, true), false);
});

test("sizes history batches by requested bars without a business-level minute cap", () => {
  assert.equal(historyBatchMinutes(chartPeriodById("timeline")), 240);
  assert.equal(historyBatchMinutes(chartPeriodById("15m")), 3_600);
  assert.equal(historyBatchMinutes(chartPeriodById("1h")), 14_400);
  assert.equal(historyBatchMinutes(chartPeriodById("1w")), 10_080);
});

test("builds an exclusive older window ending at the current history cursor", () => {
  assert.deepEqual(historyWindowBefore(1_800_000_059, 2_880), {
    start: 1_799_827_200,
    end: 1_800_000_000,
    count: 2_880,
  });
  assert.equal(historyWindowBefore(1_800_000_000, 99_999).count, 99_999);
});
test("counts only points inserted before the previous first point", () => {
  assert.equal(prependedPointCount(20, 10, 1), 1);
  assert.equal(prependedPointCount(20, 5, 2), 2);
  assert.equal(prependedPointCount(20, 20, 0), 0);
  assert.equal(prependedPointCount(20, 5, -1), 0);
  assert.equal(prependedPointCount(null, 5, 2), 0);
});
