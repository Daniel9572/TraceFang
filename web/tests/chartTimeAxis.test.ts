import assert from "node:assert/strict";
import test from "node:test";

import { TickMarkType } from "lightweight-charts";

import { chartPeriodById } from "../src/chartPeriods.ts";
import {
  actualTimeForChinaAxis,
  buildTimelineLayout,
  buildTimelineSessionGaps,
  formatChartTick,
  formatChartTimeLabel,
  formatCrosshairTime,
  formatDateTimeInTimeZone,
  formatSessionGapDuration,
  projectTimeForChinaAxis,
  projectTimelineSeries,
  tradingDayAt,
} from "../src/chartTimeAxis.ts";
import { SPOT_METALS_MARKET_SCHEDULE } from "../src/marketSession.ts";

const SHFE_SCHEDULE = {
  time_zone: "Asia/Shanghai",
  trading_day_rule: "shfe" as const,
  reference: "test",
  sessions: [
    ...[1, 2, 3, 4, 5].map((weekday) => ({
      weekday: weekday as 1 | 2 | 3 | 4 | 5,
      open: "09:00",
      close: "15:00",
      close_day_offset: 0,
    })),
    ...[1, 2, 3, 4, 5].map((weekday) => ({
      weekday: weekday as 1 | 2 | 3 | 4 | 5,
      open: "21:00",
      close: "02:30",
      close_day_offset: 1,
    })),
  ],
};

test("renders the same market instant in selectable desk time zones", () => {
  const instant = Date.parse("2026-08-07T20:56:00Z") / 1_000;
  assert.equal(formatDateTimeInTimeZone(instant, "Asia/Shanghai"), "2026/08/08 04:56");
  assert.equal(formatDateTimeInTimeZone(instant, "America/New_York"), "2026/08/07 16:56");
  assert.equal(formatDateTimeInTimeZone(instant, "Europe/London"), "2026/08/07 21:56");
});

test("compresses the weekend into adjacent fixed-width trading days", () => {
  const friday = Date.parse("2026-08-07T20:58:00Z") / 1_000;
  const monday = Date.parse("2026-08-10T20:58:00Z") / 1_000;
  const layout = buildTimelineLayout([friday, monday], SPOT_METALS_MARKET_SCHEDULE);

  assert.deepEqual(layout.days.map((day) => day.key), ["2026-08-07", "2026-08-10"]);
  assert.equal(layout.days[1].chartStart - layout.days[0].chartStart, 24 * 60 * 60);
});

test("uses the session close date as the trading-day label", () => {
  const sundayOpen = Date.parse("2026-08-09T22:05:00Z") / 1_000;
  const beforeSundayOpen = sundayOpen - 60;

  assert.equal(tradingDayAt(sundayOpen, SPOT_METALS_MARKET_SCHEDULE)?.label, "08/10");
  assert.equal(tradingDayAt(beforeSundayOpen, SPOT_METALS_MARKET_SCHEDULE), null);
});

test("groups SHFE night and day sessions under the exchange trading date", () => {
  const mondayNight = Date.parse("2026-08-10T13:00:00Z") / 1_000;
  const tuesdayDay = Date.parse("2026-08-11T01:00:00Z") / 1_000;
  const fridayNight = Date.parse("2026-08-14T13:00:00Z") / 1_000;

  assert.equal(tradingDayAt(mondayNight, SHFE_SCHEDULE)?.key, "2026-08-11");
  assert.equal(tradingDayAt(tuesdayDay, SHFE_SCHEDULE)?.key, "2026-08-11");
  assert.equal(tradingDayAt(fridayNight, SHFE_SCHEDULE)?.key, "2026-08-17");
});

test("projects every quote without changing or merging raw event timestamps", () => {
  const first = Date.parse("2026-08-09T22:05:01Z") / 1_000;
  const layout = buildTimelineLayout([first], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: first, value: 100 },
    { time: first + 8, value: 101 },
    { time: first + 16, value: 102 },
  ], layout);

  assert.equal(projected.length, 3);
  assert.deepEqual(projected.map((point) => point.value), [100, 101, 102]);
  assert.deepEqual(projected.map((point) => point.actualTime), [first, first + 8, first + 16]);
  assert.ok(projected[0].time < projected[1].time);
  assert.ok(projected[1].time < projected[2].time);
});

test("assigns distinct chart coordinates to events with the same source timestamp", () => {
  const actualTime = Date.parse("2026-08-09T22:05:01Z") / 1_000;
  const layout = buildTimelineLayout([actualTime], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: actualTime + 0.1, observedTime: actualTime, value: 100, eventId: "one" },
    { time: actualTime + 0.2, observedTime: actualTime, value: 102, eventId: "two" },
  ], layout);

  assert.equal(projected.length, 2);
  assert.deepEqual(projected.map((point) => point.actualTime), [actualTime, actualTime]);
  assert.ok(projected[1].time > projected[0].time);
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
  assert.equal(formatChartTick(epoch, TickMarkType.Month, period), "2026/08");
  assert.equal(
    formatCrosshairTime(epoch, period, { days: [] }),
    "2026/08/06 09:47",
  );
});

test("projects only chart coordinates so UTC tick boundaries match China Standard Time", () => {
  const actualTime = Date.parse("2026-07-31T16:00:00Z") / 1_000;
  const chartTime = projectTimeForChinaAxis(actualTime);

  assert.equal(new Date(chartTime * 1_000).toISOString(), "2026-08-01T00:00:00.000Z");
  assert.equal(actualTimeForChinaAxis(chartTime), actualTime);
  assert.equal(formatChartTimeLabel(actualTime, chartPeriodById("1m")), "2026/08/01 00:00");
});

test("calendar periods never fall back to misleading intraday precision", () => {
  const epoch = Date.parse("2026-08-06T01:47:02Z") / 1_000;

  assert.equal(formatChartTick(epoch, TickMarkType.Time, chartPeriodById("1d")), "08/06");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, chartPeriodById("1w")), "08/06");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, chartPeriodById("1mo")), "2026/08");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, chartPeriodById("1q")), "2026 Q3");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, chartPeriodById("1y")), "2026");
});

test("uses period-aware precision for chart hover and crosshair labels", () => {
  const epoch = Date.parse("2026-08-06T01:47:02Z") / 1_000;

  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("timeline"), 15), "2026/08/06 09:47:02");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("timeline"), 60), "2026/08/06 09:47");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1h")), "2026/08/06 09:47");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1d")), "2026/08/06");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1w")), "2026/08/06");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1mo")), "2026/08");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1q")), "2026 Q3");
  assert.equal(formatChartTimeLabel(epoch, chartPeriodById("1y")), "2026");
});

test("hides isolated clock ticks when a fixed-period view spans many days", () => {
  const epoch = Date.parse("2026-08-06T01:47:02Z") / 1_000;
  const period = chartPeriodById("1h");

  assert.equal(formatChartTick(epoch, TickMarkType.Time, period, 6 * 24 * 60 * 60), "09:47");
  assert.equal(formatChartTick(epoch, TickMarkType.Time, period, 8 * 24 * 60 * 60), "");
  assert.equal(formatChartTick(epoch, TickMarkType.DayOfMonth, period, 8 * 24 * 60 * 60), "08/06");
});
