import assert from "node:assert/strict";
import test from "node:test";

import {
  defaultPeriodPreferences,
  movePeriod,
  normalizePeriodPreferences,
  reorderPeriodBefore,
  togglePeriodVisibility,
} from "../src/periodPreferences.ts";

test("keeps 15-minute candles in the default compact toolbar", () => {
  const preferences = defaultPeriodPreferences();
  assert.ok(preferences.visible.includes("15m"));
  assert.ok(!preferences.visible.includes("10m"));
  assert.ok(preferences.order.indexOf("15m") < preferences.order.indexOf("30m"));
});

test("normalizes duplicate and unknown persisted period ids", () => {
  const preferences = normalizePeriodPreferences({
    order: ["15m", "15m", "unknown", "1m"],
    visible: ["15m", "unknown"],
  });
  assert.equal(preferences.order[0], "15m");
  assert.equal(new Set(preferences.order).size, preferences.order.length);
  assert.deepEqual(preferences.visible, ["15m"]);
  assert.ok(preferences.order.includes("timeline"));
  assert.ok(preferences.order.includes("1y"));
});

test("toggles visibility while retaining at least one toolbar period", () => {
  const initial = normalizePeriodPreferences({ order: ["1m", "15m"], visible: ["1m"] });
  assert.equal(togglePeriodVisibility(initial, "1m"), initial);
  const expanded = togglePeriodVisibility(initial, "15m");
  assert.deepEqual(expanded.visible.slice(0, 2), ["1m", "15m"]);
  assert.deepEqual(togglePeriodVisibility(expanded, "1m").visible, ["15m"]);
});

test("supports button and drag-style period ordering", () => {
  const initial = defaultPeriodPreferences();
  const moved = movePeriod(initial, "15m", -1);
  assert.ok(moved.order.indexOf("15m") < moved.order.indexOf("5m"));
  const dragged = reorderPeriodBefore(moved, "1d", "timeline");
  assert.equal(dragged.order[0], "1d");
  assert.equal(dragged.visible[0], "1d");
});
