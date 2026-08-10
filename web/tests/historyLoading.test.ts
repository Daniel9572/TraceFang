import assert from "node:assert/strict";
import test from "node:test";

import { chartPeriodById } from "../src/chartPeriods.ts";
import {
  historyGapWindow,
  historyCursorEpoch,
  historyBatchMinutes,
  historyWindowBefore,
  resolveHistoryDemandOutcome,
  shouldActivateOlderHistoryDemand,
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

test("keeps an accepted left-edge demand pending while a history request is busy", () => {
  const nearEdge = { from: 8, to: 88 };

  assert.equal(shouldActivateOlderHistoryDemand(nearEdge, 100, true), true);
  assert.equal(shouldRequestOlderHistory(nearEdge, 100, true, true), false);
  assert.equal(shouldRequestOlderHistory(nearEdge, 100, false, true), true);
});

test("automatically fills a chart whose complete loaded series still leaves the left edge visible", () => {
  assert.equal(
    shouldActivateOlderHistoryDemand({ from: -12, to: 50 }, 50, false),
    true,
  );
  assert.equal(
    shouldActivateOlderHistoryDemand({ from: 440, to: 500 }, 500, false),
    false,
  );
});

test("turns one display gap into the exact missing minute window", () => {
  assert.deepEqual(historyGapWindow(1_800_000_060, 240), {
    start: 1_800_000_060,
    end: 1_800_000_300,
    count: 4,
  });
});

test("continues past a final local page using its earliest Bar as the next cursor", () => {
  assert.equal(
    historyCursorEpoch(null, "2026-08-10T01:00:00Z"),
    Date.parse("2026-08-10T01:00:00Z") / 1_000,
  );
  assert.equal(historyCursorEpoch(null, null), null);
});

test("stops automatic empty-window advancement at the seven-day safety boundary", () => {
  const first = resolveHistoryDemandOutcome(0, {
    state: "advanced",
    added: 0,
    advancedMinutes: 3 * 24 * 60,
  });
  const second = resolveHistoryDemandOutcome(first.emptyAdvanceMinutes, {
    state: "advanced",
    added: 0,
    advancedMinutes: 4 * 24 * 60,
  });

  assert.equal(first.active, true);
  assert.equal(second.active, false);
  assert.equal(
    resolveHistoryDemandOutcome(second.emptyAdvanceMinutes, {
      state: "loaded",
      added: 1,
      advancedMinutes: 0,
    }).emptyAdvanceMinutes,
    0,
  );
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
