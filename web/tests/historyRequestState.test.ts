import assert from "node:assert/strict";
import test from "node:test";

import {
  aggregateBackfillResults,
  backfillTransportWindows,
  marketApi,
} from "../src/api.ts";
import {
  HISTORY_LOADING_INDICATOR_DELAY_MS,
  nextHistoryDemandEvaluationDelay,
  resolveHistoryDemandOutcome,
  resolveChartHistoryStep,
  shouldShowHistoryLoading,
} from "../src/historyLoading.ts";
import type { CandleBackfillResult, ChartBarPage } from "../src/types.ts";

function result(
  state: CandleBackfillResult["state"],
  overrides: Partial<CandleBackfillResult> = {},
): CandleBackfillResult {
  return {
    source_id: "jin10_client",
    state,
    start: "2026-08-01T00:00:00Z",
    end: "2026-08-02T00:00:00Z",
    row_count: 0,
    covered_start: null,
    covered_end: null,
    authoritative_through: null,
    history_floor: null,
    retry_after: null,
    evidence_version: null,
    ...overrides,
  };
}

function page(
  nextCursor: string | null,
  nextBefore: string | null,
): ChartBarPage {
  return {
    period_id: "1h",
    items: [],
    next_before: nextBefore,
    next_cursor: nextCursor,
    local_status: "empty",
    has_more: nextCursor !== null,
  };
}

test("does not move an opaque cursor while the source is deferred", () => {
  const before = Date.parse("2026-08-02T00:00:00Z") / 1_000;
  const currentCursor = { token: "opaque-current", before };
  const resolution = resolveChartHistoryStep({
    currentCursor,
    page: page(null, null),
    localAdded: 0,
    sourceStatus: "deferred",
    retryAfter: "2026-08-02T00:00:05Z",
    nowMilliseconds: Date.parse("2026-08-02T00:00:00Z"),
  });

  assert.deepEqual(resolution.nextCursor, currentCursor);
  assert.equal(resolution.outcome.state, "busy");
  assert.equal(resolution.outcome.retryAfterMs, 5_000);
});

test("moves only to the server-issued next cursor and stops at a source floor", () => {
  const before = Date.parse("2026-08-02T00:00:00Z") / 1_000;
  const coveredStart = Date.parse("2026-08-01T00:00:00Z") / 1_000;
  const currentCursor = { token: "opaque-current", before };
  const advanced = resolveChartHistoryStep({
    currentCursor,
    page: page("opaque-next", "2026-08-01T00:00:00Z"),
    localAdded: 0,
    sourceStatus: "available",
    retryAfter: null,
  });
  assert.deepEqual(advanced.nextCursor, { token: "opaque-next", before: coveredStart });
  assert.equal(advanced.outcome.state, "advanced");

  const exhausted = resolveChartHistoryStep({
    currentCursor,
    page: page(null, null),
    localAdded: 0,
    sourceStatus: "exhausted",
    retryAfter: null,
  });
  assert.equal(exhausted.nextCursor, null);
  assert.equal(exhausted.outcome.state, "exhausted");
});

test("prefers newly loaded local Bars and advances with their opaque page cursor", () => {
  const before = Date.parse("2026-08-02T00:00:00Z") / 1_000;
  const localCursor = Date.parse("2026-08-01T12:00:00Z") / 1_000;
  const resolution = resolveChartHistoryStep({
    currentCursor: { token: "opaque-current", before },
    page: page("opaque-local", "2026-08-01T12:00:00Z"),
    localAdded: 12,
    sourceStatus: "available",
    retryAfter: null,
  });
  assert.deepEqual(resolution.nextCursor, { token: "opaque-local", before: localCursor });
  assert.equal(resolution.outcome.state, "loaded");
  assert.equal(resolution.outcome.added, 12);
});

test("rejects a server cursor that does not move backward", () => {
  const before = Date.parse("2026-08-02T00:00:00Z") / 1_000;
  assert.throws(() => resolveChartHistoryStep({
    currentCursor: { token: "opaque-current", before },
    page: page("opaque-next", "2026-08-02T00:00:00Z"),
    localAdded: 0,
    sourceStatus: "available",
    retryAfter: null,
  }), /服务端周期 Bar 游标未前进/);
});

test("waits for React data commit after loaded Bars before evaluating the edge again", () => {
  const loaded = { state: "loaded", added: 250, advancedMinutes: 250 } as const;
  assert.equal(
    nextHistoryDemandEvaluationDelay(loaded, resolveHistoryDemandOutcome(0, loaded)),
    null,
  );
  const advanced = { state: "advanced", added: 0, advancedMinutes: 250 } as const;
  assert.equal(
    nextHistoryDemandEvaluationDelay(advanced, resolveHistoryDemandOutcome(0, advanced)),
    null,
  );
  const busy = { state: "busy", added: 0, advancedMinutes: 0, retryAfterMs: 750 } as const;
  assert.equal(
    nextHistoryDemandEvaluationDelay(busy, resolveHistoryDemandOutcome(0, busy)),
    750,
  );
});

test("keeps the strictest transport state and earliest confirmed boundary", () => {
  const aggregate = aggregateBackfillResults(
    "jin10_client",
    { start: Date.parse("2026-07-01T00:00:00Z") / 1_000, end: Date.parse("2026-08-01T00:00:00Z") / 1_000, count: 44_640 },
    [
      result("fetched", {
        start: "2026-07-01T00:00:00Z",
        end: "2026-07-08T00:00:00Z",
        row_count: 10,
        covered_start: "2026-07-01T00:00:00Z",
        covered_end: "2026-07-08T00:00:00Z",
        authoritative_through: "2026-08-01T00:00:00Z",
        evidence_version: "v1",
      }),
      result("deferred", {
        start: "2026-07-08T00:00:00Z",
        end: "2026-07-15T00:00:00Z",
        covered_start: "2026-07-08T00:00:00Z",
        covered_end: "2026-07-15T00:00:00Z",
        authoritative_through: "2026-08-02T00:00:00Z",
        retry_after: "2026-08-02T00:00:05Z",
        evidence_version: "v2",
      }),
    ],
  );
  assert.equal(aggregate.state, "deferred");
  assert.equal(aggregate.covered_start, "2026-07-01T00:00:00.000Z");
  assert.equal(aggregate.covered_end, "2026-07-15T00:00:00.000Z");
  assert.equal(aggregate.authoritative_through, "2026-08-02T00:00:00.000Z");
  assert.equal(aggregate.row_count, 10);
  assert.equal(aggregate.retry_after, "2026-08-02T00:00:05.000Z");
  assert.equal(aggregate.evidence_version, "v1|v2");
});

test("slices large history demands into at most ten-thousand-minute transport pages", () => {
  const windows = backfillTransportWindows({ start: 0, end: 25_000 * 60, count: 25_000 });
  assert.deepEqual(windows.map((window) => window.count), [10_000, 10_000, 5_000]);
  assert.equal(windows[1].start, 10_000 * 60);
  assert.equal(windows[2].end, 25_000 * 60);
});

test("shows the non-blocking indicator only after 150ms", () => {
  assert.equal(HISTORY_LOADING_INDICATOR_DELAY_MS, 150);
  assert.equal(shouldShowHistoryLoading(1_000, 1_149), false);
  assert.equal(shouldShowHistoryLoading(1_000, 1_150), true);
});

test("sends one logical countBack command and consumes the server-prepared page", async () => {
  const before = Date.parse("2026-08-21T19:18:00Z") / 1_000;
  const requests: string[] = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input) => {
    const url = String(input);
    requests.push(url);
    return new Response(JSON.stringify({
      source_id: "jin10_client",
      period_id: "1h",
      local_status: "ready",
      source_status: "available",
      page: {
        period_id: "1h",
        items: [],
        next_before: "2026-08-11T09:18:00Z",
        next_cursor: "opaque-next",
        local_status: "empty",
        has_more: false,
      },
      next_before: "2026-08-11T09:18:00Z",
      next_cursor: "opaque-next",
      backfill: result("cached", {
        covered_start: "2026-08-11T09:18:00Z",
        covered_end: "2026-08-21T19:18:00Z",
        authoritative_through: "2026-08-27T17:48:00Z",
        evidence_version: "v1",
      }),
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  try {
    const filled = await marketApi.olderCandleHistory(
      "XAUUSD",
      "jin10_client",
      "1h",
      "opaque-current",
      240,
    );

    assert.equal(requests.length, 1);
    assert.match(requests[0], /\/api\/bars\/XAUUSD\/history\?/);
    assert.match(requests[0], /cursor=opaque-current/);
    assert.match(requests[0], /count_back=240/);
    assert.equal(requests.filter((url) => url.includes("/backfill?")).length, 0);
    assert.equal(requests.filter((url) => url.includes("/prepare?")) .length, 0);
    assert.equal(filled.page?.period_id, "1h");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
