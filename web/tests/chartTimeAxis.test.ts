import assert from "node:assert/strict";
import test from "node:test";

import { TickMarkType } from "lightweight-charts";

import { chartPeriodById } from "../src/chartPeriods.ts";
import {
  actualTimeForChinaAxis,
  buildOpenSessionDataGaps,
  buildSeriesDataGaps,
  buildTimelineLayout,
  buildTimelineLogicalDayRanges,
  buildTimelineSessionGaps,
  formatChartTick,
  formatChartTimeLabel,
  formatCrosshairTime,
  formatDateTimeInTimeZone,
  formatSessionGapDuration,
  projectTimeForChinaAxis,
  projectTimelineSeries,
  timelineDayWindowAtLogicalRange,
  timelineGapSeparatorTime,
  timelineLogicalViewport,
  tradingDayAt,
} from "../src/chartTimeAxis.ts";
import { SPOT_METALS_MARKET_SCHEDULE } from "../src/marketSession.ts";

const SHFE_SCHEDULE = {
  time_zone: "Asia/Shanghai",
  trading_day_rule: "shfe" as const,
  reference: "test",
  sessions: [
    ...[1, 2, 3, 4, 5].flatMap((weekday) => [
      { weekday, open: "09:00", close: "10:15", close_day_offset: 0 },
      { weekday, open: "10:30", close: "11:30", close_day_offset: 0 },
      { weekday, open: "13:30", close: "15:00", close_day_offset: 0 },
      { weekday, open: "21:00", close: "02:30", close_day_offset: 1 },
    ]).map((session) => ({
      ...session,
      weekday: session.weekday as 1 | 2 | 3 | 4 | 5,
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
  const sundayOpen = Date.parse("2026-08-09T22:00:00Z") / 1_000;
  const beforeSundayOpen = sundayOpen - 60;

  assert.equal(tradingDayAt(sundayOpen, SPOT_METALS_MARKET_SCHEDULE)?.label, "08/10");
  assert.equal(tradingDayAt(beforeSundayOpen, SPOT_METALS_MARKET_SCHEDULE), null);
});

test("builds exact one-day, two-day, and three-day timeline viewports", () => {
  const starts = [
    Date.parse("2026-08-06T22:00:00Z") / 1_000,
    Date.parse("2026-08-09T22:00:00Z") / 1_000,
    Date.parse("2026-08-10T22:00:00Z") / 1_000,
  ];
  const actualTimes = starts.flatMap((start) => [start, start + 60]);
  const layout = buildTimelineLayout(actualTimes, SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries(
    actualTimes.map((time, index) => ({ time, value: 2_000 + index })),
    layout,
  );
  const ranges = buildTimelineLogicalDayRanges(projected, [], layout);

  assert.deepEqual(ranges.map((range) => range.key), ["2026-08-07", "2026-08-10", "2026-08-11"]);
  assert.deepEqual(timelineLogicalViewport(ranges, 1), {
    from: ranges[2].from,
    to: ranges[2].to,
    dayCount: 1,
    firstKey: "2026-08-11",
    lastKey: "2026-08-11",
  });
  assert.deepEqual(timelineLogicalViewport(ranges, 2), {
    from: ranges[1].from,
    to: ranges[2].to,
    dayCount: 2,
    firstKey: "2026-08-10",
    lastKey: "2026-08-11",
  });
  assert.equal(timelineLogicalViewport(ranges, 3)?.dayCount, 3);
  assert.deepEqual(
    timelineDayWindowAtLogicalRange(ranges, timelineLogicalViewport(ranges, 1)),
    { dayCount: 1, endKey: "2026-08-11" },
  );
});

test("timeline viewport counts gap separators without dropping native points", () => {
  const start = Date.parse("2026-08-09T22:00:00Z") / 1_000;
  const layout = buildTimelineLayout([start, start + 600], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: start, value: 2_000 },
    { time: start + 600, value: 2_001 },
  ], layout);
  const ranges = buildTimelineLogicalDayRanges(projected, [{ nextIndex: 1 }], layout);

  assert.equal(projected.length, 2);
  assert.deepEqual(ranges, [{
    key: "2026-08-10",
    label: "08/10",
    from: -0.5,
    to: 2.5,
  }]);
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

test("marks one display-only gap instead of filling every missing open-session bucket", () => {
  const first = Date.parse("2026-08-09T22:10:00Z") / 1_000;
  const points = [
    { actualTime: first, resolutionSeconds: 60 },
    { actualTime: first + 60, resolutionSeconds: 60 },
    { actualTime: first + 10 * 60, resolutionSeconds: 60 },
  ];
  const layout = buildTimelineLayout(
    points.map((point) => point.actualTime),
    SPOT_METALS_MARKET_SCHEDULE,
  );

  assert.deepEqual(buildOpenSessionDataGaps(points, 1, layout), [{
    nextIndex: 2,
    separatorTime: first + 2 * 60,
    missingDurationSeconds: 8 * 60,
  }]);
});

test("does not mistake a scheduled market closure for a missing-trade gap", () => {
  const beforeClose = Date.parse("2026-08-06T20:58:00Z") / 1_000;
  const nextOpen = Date.parse("2026-08-06T22:00:00Z") / 1_000;
  const points = [
    { actualTime: beforeClose },
    { actualTime: nextOpen },
  ];
  const layout = buildTimelineLayout(
    points.map((point) => point.actualTime),
    SPOT_METALS_MARKET_SCHEDULE,
  );

  assert.deepEqual(buildOpenSessionDataGaps(points, 60, layout), []);
});

test("breaks every scheduled closure without inventing a market value", () => {
  const cases = [
    {
      name: "spot daily maintenance",
      schedule: SPOT_METALS_MARKET_SCHEDULE,
      previous: "2026-08-06T20:58:00Z",
      next: "2026-08-06T22:00:00Z",
    },
    {
      name: "spot weekend",
      schedule: SPOT_METALS_MARKET_SCHEDULE,
      previous: "2026-08-07T20:58:00Z",
      next: "2026-08-09T22:00:00Z",
    },
    {
      name: "SHFE morning break",
      schedule: SHFE_SCHEDULE,
      previous: "2026-08-11T02:14:00Z",
      next: "2026-08-11T02:30:00Z",
    },
    {
      name: "SHFE lunch break",
      schedule: SHFE_SCHEDULE,
      previous: "2026-08-11T03:29:00Z",
      next: "2026-08-11T05:30:00Z",
    },
  ];

  for (const value of cases) {
    const points = [value.previous, value.next].map((time) => ({
      actualTime: Date.parse(time) / 1_000,
      resolutionSeconds: 60,
    }));
    const layout = buildTimelineLayout(points.map((point) => point.actualTime), value.schedule);
    const gaps = buildSeriesDataGaps(points, 60, layout);
    assert.equal(gaps.length, 1, value.name);
    assert.equal(gaps[0].kind, "session-boundary", value.name);
    assert.equal(gaps[0].nextIndex, 1, value.name);
    assert.equal(Object.hasOwn(gaps[0], "value"), false, value.name);

    const coarseGaps = buildSeriesDataGaps(
      points.map((point) => ({ actualTime: point.actualTime })),
      4 * 60 * 60,
      layout,
    );
    assert.equal(coarseGaps[0]?.kind, "session-boundary", `${value.name} at 4h`);
  }
});

test("projects a closed-session separator between adjacent real timeline observations", () => {
  const previous = Date.parse("2026-08-07T20:58:00Z") / 1_000;
  const next = Date.parse("2026-08-09T22:00:00Z") / 1_000;
  const layout = buildTimelineLayout([previous, next], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: previous, value: 100 },
    { time: next, value: 104 },
  ], layout);
  const [gap] = buildSeriesDataGaps(projected, 60, layout);
  const separator = timelineGapSeparatorTime(gap, projected, layout);

  assert.ok(separator !== null);
  assert.ok(separator > projected[0].time);
  assert.ok(separator < projected[1].time);
});

test("retains every provider observation from the first minute of the spot session", () => {
  const first = Date.parse("2026-08-02T22:00:00Z") / 1_000;
  const input = Array.from({ length: 5 }, (_, index) => ({
    time: first + index * 60,
    value: 4_082.82 - index,
  }));
  const layout = buildTimelineLayout(input.map((point) => point.time), SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries(input, layout);

  assert.equal(projected.length, 5);
  assert.deepEqual(projected.map((point) => point.actualTime), input.map((point) => point.time));
});

test("retains a native close marker and still breaks the following closed session", () => {
  const beforeClose = Date.parse("2026-08-11T06:59:00Z") / 1_000;
  const closeMarker = Date.parse("2026-08-11T07:00:00Z") / 1_000;
  const nextOpen = Date.parse("2026-08-11T13:00:00Z") / 1_000;
  const input = [beforeClose, closeMarker, nextOpen].map((time, index) => ({
    time,
    value: 900 + index,
    resolutionSeconds: 60,
  }));
  const layout = buildTimelineLayout(input.map((point) => point.time), SHFE_SCHEDULE);
  const projected = projectTimelineSeries(input, layout);
  const gaps = buildSeriesDataGaps(projected, 60, layout);

  assert.deepEqual(projected.map((point) => point.actualTime), input.map((point) => point.time));
  assert.equal(gaps.length, 1);
  assert.equal(gaps[0].kind, "session-boundary");
  assert.equal(gaps[0].nextIndex, 2);
  assert.ok(projected[1].time < projected[2].time);
});

test("does not infer an open-session gap without a market schedule", () => {
  const first = Date.parse("2026-08-09T22:10:00Z") / 1_000;

  assert.deepEqual(buildOpenSessionDataGaps([
    { actualTime: first },
    { actualTime: first + 10 * 60 },
  ], 60, null), []);
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
  const nextOpen = Date.parse("2026-08-06T22:00:00Z") / 1_000;
  const layout = buildTimelineLayout([beforeClose, nextOpen], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: beforeClose, value: 100 },
    { time: nextOpen, value: 101 },
  ], layout);
  const [gap] = buildTimelineSessionGaps(layout, projected, 2);

  assert.ok(gap);
  assert.equal(gap.kind, "session");
  assert.equal(gap.durationSeconds, 60 * 60);
  assert.equal(gap.boundaryState, "complete");
  assert.equal(gap.direction, "up");
  assert.equal(gap.priceDifference, 1);
  assert.equal(gap.pricePercent, 1);
  assert.equal(formatSessionGapDuration(gap.durationSeconds), "1小时");
  assert.equal(formatSessionGapDuration(gap.durationSeconds, true), "1时");
});

test("labels the weekend closure without expanding it on the chart", () => {
  const fridayClose = Date.parse("2026-08-07T20:58:00Z") / 1_000;
  const sundayOpen = Date.parse("2026-08-09T22:00:00Z") / 1_000;
  const layout = buildTimelineLayout([fridayClose, sundayOpen], SPOT_METALS_MARKET_SCHEDULE);
  const projected = projectTimelineSeries([
    { time: fridayClose, value: 100 },
    { time: sundayOpen, value: 99.5 },
  ], layout);
  const [gap] = buildTimelineSessionGaps(layout, projected, 2);

  assert.ok(gap);
  assert.equal(gap.kind, "weekend");
  assert.equal(gap.durationSeconds, 49 * 60 * 60);
  assert.equal(gap.boundaryState, "complete");
  assert.equal(gap.direction, "down");
  assert.equal(formatSessionGapDuration(gap.durationSeconds), "2天1小时");
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
