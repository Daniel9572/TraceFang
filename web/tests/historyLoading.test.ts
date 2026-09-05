import assert from "node:assert/strict";
import test from "node:test";

import {
  canBackfillOlderHistory,
  enabledIndicatorWarmupBars,
  enabledStrategyWarmupBars,
  historyGapWindow,
  historyDemandBars,
  historyDemandFor,
  historyPageCursor,
  historyWindowBefore,
  resolveHistoryDemandOutcome,
  shouldActivateOlderHistoryDemand,
  prependedPointCount,
  shouldRequestOlderHistory,
} from "../src/historyLoading.ts";

test("backfills only when the instrument supports it and the unified source is configured", () => {
  assert.equal(canBackfillOlderHistory(true, true), true);
  assert.equal(canBackfillOlderHistory(true, false), false);
  assert.equal(canBackfillOlderHistory(true, undefined), false);
  assert.equal(canBackfillOlderHistory(false, true), false);
});

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
  assert.equal(
    shouldActivateOlderHistoryDemand({ from: -12, to: 50 }, 50, false, 10_000),
    false,
  );
  assert.equal(
    shouldActivateOlderHistoryDemand({ from: -12, to: 50 }, 50, true, 10_000),
    true,
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
  assert.deepEqual(historyPageCursor({
    next_cursor: "opaque-page-2",
    next_before: "2026-08-10T01:00:00Z",
  }), {
    token: "opaque-page-2",
    before: Date.parse("2026-08-10T01:00:00Z") / 1_000,
  });
  assert.equal(historyPageCursor({ next_cursor: null, next_before: null }), null);
});

test("stops automatic demand after a confirmed cursor advance without a new Bar", () => {
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

  assert.equal(first.active, false);
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

test("expresses every period's history demand as logical Bars", () => {
  assert.equal(historyDemandBars(), 240);
  assert.equal(historyDemandBars(320, 35), 320);
  assert.equal(historyDemandBars(80, 400), 400);
  assert.equal(historyDemandBars(20_000, 35), 10_000);
  assert.equal(enabledIndicatorWarmupBars(["rsi", "macd"]), 35);
  assert.equal(enabledIndicatorWarmupBars(["kdj"]), 9);
  assert.equal(enabledStrategyWarmupBars(["rsi", "ma-structure"]), 250);
});

test("describes one accepted history demand with visible bars and its reason", () => {
  assert.deepEqual(historyDemandFor({ from: 8.2, to: 88.8 }, 100, true, 35), {
    visibleBars: 82,
    indicatorWarmupBars: 35,
    reason: "left-edge",
  });
  assert.deepEqual(historyDemandFor({ from: -12, to: 50 }, 50, false, 0), {
    visibleBars: 63,
    indicatorWarmupBars: 0,
    reason: "initial-fill",
  });
});

test("does not reconstruct an opaque cursor from a Bar timestamp", () => {
  assert.equal(historyPageCursor({
    next_cursor: null,
    next_before: "2026-08-10T01:00:00Z",
  }), null);
  assert.equal(historyPageCursor({
    next_cursor: "opaque-page-2",
    next_before: null,
  }), null);
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
