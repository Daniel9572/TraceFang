import assert from "node:assert/strict";
import test from "node:test";

import { TickMarkType } from "lightweight-charts";

import { chartPeriodById } from "../src/chartPeriods.ts";
import {
  TIMELINE_SLOTS_PER_DAY,
  buildTimelineLayout,
  buildTimelineSessionGaps,
  formatChartTick,
  formatCrosshairTime,
  formatSessionGapDuration,
  projectTimelineSeries,
  timelineLogicalIndex,
  tradingDayAt,
} from "../src/chartTimeAxis.ts";
import { SPOT_METALS_MARKET_SCHEDULE } from "../src/marketSession.ts";

test("compresses the weekend into adjacent fixed-width trading days", () => {
  const friday = Date.parse("2026-08-07T20:58:00Z") / 1_000;
  const monday = Date.parse("2026-08-10T20:58:00Z") / 1_000;
  const layout = buildTimelineLayout([friday, monday], SPOT_METALS_MARKET_SCHEDULE);

  assert.deepEqual(layout.days.map((day) => day.key), ["2026-08-07", "2026-08-10"]);
  assert.equal(layout.days[1].chartStart - layout.days[0].chartStart, 24 * 60 * 60);
  assert.equal(layout.spacingTimes.length, TIMELINE_SLOTS_PER_DAY * 2);
  assert.equal(layout.days[1].logicalStart - layout.days[0].logicalStart, TIMELINE_SLOTS_PER_DAY);
});

test("uses the session close date as the trading-day label", () => {
  const sundayOpen = Date.parse("2026-08-09T22:05:00Z") / 1_000;
  const beforeSundayOpen = sundayOpen - 60;

  assert.equal(tradingDayAt(sundayOpen, SPOT_METALS_MARKET_SCHEDULE)?.label, "08/10");
  assert.equal(tradingDayAt(beforeSundayOpen, SPOT_METALS_MARKET_SCHEDULE), null);
});

test("projects quotes into fixed slots without changing the raw event timestamps", () => {
  const first = Date.parse("2026-08-09T22:05:01Z") / 1_000;
  const layout = buildTimelineLayout([first], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: first, value: 100 },
    { time: first + 8, value: 101 },
    { time: first + 16, value: 102 },
  ], layout);

  assert.equal(projected.length, 2);
  assert.equal(projected[0].value, 101);
  assert.equal(projected[0].actualTime, first + 8);
  assert.equal(projected[1].value, 102);
  assert.equal(
    timelineLogicalIndex(layout, projected[1].time),
    timelineLogicalIndex(layout, projected[0].time)! + 1,
  );
});

test("describes the daily closed session and calculates a verified opening gap", () => {
  const beforeClose = Date.parse("2026-08-06T20:58:00Z") / 1_000;
  const nextOpen = Date.parse("2026-08-06T22:05:00Z") / 1_000;
  const layout = buildTimelineLayout([beforeClose, nextOpen], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: beforeClose, value: 100 },
    { time: nextOpen, value: 101 },
  ], layout);
  const [gap] = buildTimelineSessionGaps(layout, projected, 2);

  assert.ok(gap);
  assert.equal(gap.kind, "session");
  assert.equal(gap.durationSeconds, 66 * 60);
  assert.equal(gap.boundaryState, "complete");
  assert.equal(gap.direction, "up");
  assert.equal(gap.priceDifference, 1);
  assert.equal(gap.pricePercent, 1);
  assert.equal(formatSessionGapDuration(gap.durationSeconds), "1小时06分");
  assert.equal(formatSessionGapDuration(gap.durationSeconds, true), "1时06分");
});

test("labels the weekend closure without expanding it on the chart", () => {
  const fridayClose = Date.parse("2026-08-07T20:58:00Z") / 1_000;
  const sundayOpen = Date.parse("2026-08-09T22:05:00Z") / 1_000;
  const layout = buildTimelineLayout([fridayClose, sundayOpen], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: fridayClose, value: 100 },
    { time: sundayOpen, value: 99.5 },
  ], layout);
  const [gap] = buildTimelineSessionGaps(layout, projected, 2);

  assert.ok(gap);
  assert.equal(gap.kind, "weekend");
  assert.equal(gap.durationSeconds, 49 * 60 * 60 + 6 * 60);
  assert.equal(gap.boundaryState, "complete");
  assert.equal(gap.direction, "down");
  assert.equal(formatSessionGapDuration(gap.durationSeconds), "2天1小时06分");
});

test("does not infer an opening price gap when a session boundary is incomplete", () => {
  const beforeClose = Date.parse("2026-08-06T20:58:00Z") / 1_000;
  const afterOpen = Date.parse("2026-08-06T22:15:00Z") / 1_000;
  const layout = buildTimelineLayout([beforeClose, afterOpen], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: beforeClose, value: 100 },
    { time: afterOpen, value: 104 },
  ], layout);
  const [gap] = buildTimelineSessionGaps(layout, projected, 2);

  assert.ok(gap);
  assert.equal(gap.boundaryState, "missing-open");
  assert.equal(gap.nextOpen, null);
  assert.equal(gap.priceDifference, null);
  assert.equal(gap.pricePercent, null);
  assert.equal(gap.direction, "unknown");
});

test("formats candle ticks and crosshair labels explicitly in Beijing time", () => {
  const epoch = Date.parse("2026-08-06T01:47:02Z") / 1_000;
  const period = chartPeriodById("1m");

  assert.equal(formatChartTick(epoch, TickMarkType.DayOfMonth, period), "08/06");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, period), "09:47");
  assert.match(formatCrosshairTime(epoch, period, { days: [], spacingTimes: [] }), /08\/06.*09:47:02/);
});
