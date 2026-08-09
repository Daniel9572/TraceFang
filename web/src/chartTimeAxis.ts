import { TickMarkType } from "lightweight-charts";

import type { ChartPeriod } from "./chartPeriods";
import type { MarketSchedule, TimelineSample } from "./types";

export const DISPLAY_TIME_ZONE = "Asia/Shanghai";

const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_DAY = 24 * 60 * 60;
const SECONDS_PER_WEEK = 7 * SECONDS_PER_DAY;
const CHINA_STANDARD_TIME_OFFSET_SECONDS = 8 * 60 * 60;
export const DATE_ONLY_AXIS_THRESHOLD_SECONDS = 7 * SECONDS_PER_DAY;
const MINUTES_PER_DAY = 24 * 60;
const SUNDAY_1970_DAY_SERIAL = 3;

const TIMELINE_TIME_EPSILON_SECONDS = 0.000_001;

/**
 * lightweight-charts classifies tick boundaries from UTC calendar fields.
 * Shift only the chart coordinate so those fields match the China wall clock;
 * the source epoch remains unchanged everywhere else.
 */
export function projectTimeForChinaAxis(actualTime: number): number {
  return actualTime + CHINA_STANDARD_TIME_OFFSET_SECONDS;
}

export function actualTimeForChinaAxis(chartTime: number): number {
  return chartTime - CHINA_STANDARD_TIME_OFFSET_SECONDS;
}

const WEEKDAY_INDEX: Record<string, number> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
};

interface ZonedDateParts {
  year: number;
  month: number;
  day: number;
  weekday: number;
  hour: number;
  minute: number;
  second: number;
}

interface NormalizedSession {
  weekday: number;
  startSeconds: number;
  durationSeconds: number;
  position: number;
}

export interface TimelineTradingDay {
  key: string;
  label: string;
  ordinal: number;
  actualStart: number;
  actualEnd: number;
  chartStart: number;
  chartEnd: number;
  logicalStart: number;
  logicalEnd: number;
}

export interface TimelineLayout {
  days: TimelineTradingDay[];
}

export interface ProjectedTimelinePoint {
  time: number;
  actualTime: number;
  value: number;
}

export type SessionGapDirection = "up" | "down" | "flat" | "unknown";
export type SessionGapBoundaryState = "complete" | "missing-close" | "missing-open" | "missing-both";

export interface TimelineSessionGap {
  id: string;
  chartTime: number;
  previousTradingDay: string;
  nextTradingDay: string;
  closedAt: number;
  openedAt: number;
  durationSeconds: number;
  kind: "session" | "weekend";
  boundaryState: SessionGapBoundaryState;
  previousClose: number | null;
  nextOpen: number | null;
  priceDifference: number | null;
  pricePercent: number | null;
  direction: SessionGapDirection;
}

const zonedPartsFormatters = new Map<string, Intl.DateTimeFormat>();

function parseClock(value: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return hour * MINUTES_PER_DAY / 24 + minute;
}

function formatterForParts(timeZone: string): Intl.DateTimeFormat {
  const cached = zonedPartsFormatters.get(timeZone);
  if (cached) return cached;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    weekday: "short",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  });
  zonedPartsFormatters.set(timeZone, formatter);
  return formatter;
}

function zonedDateParts(epochSeconds: number, timeZone: string): ZonedDateParts | null {
  try {
    const parts = formatterForParts(timeZone).formatToParts(new Date(epochSeconds * 1_000));
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const weekday = WEEKDAY_INDEX[values.weekday];
    const result = {
      year: Number(values.year),
      month: Number(values.month),
      day: Number(values.day),
      weekday,
      hour: Number(values.hour),
      minute: Number(values.minute),
      second: Number(values.second),
    };
    return Object.values(result).every(Number.isFinite) ? result : null;
  } catch {
    return null;
  }
}

function normalizedSessions(schedule: MarketSchedule): NormalizedSession[] {
  const rows: Omit<NormalizedSession, "position">[] = [];
  for (const session of schedule.sessions) {
    const openMinute = parseClock(session.open);
    const closeMinute = parseClock(session.close);
    if (openMinute === null || closeMinute === null || session.close_day_offset < 0) continue;
    const durationMinutes = session.close_day_offset * MINUTES_PER_DAY + closeMinute - openMinute;
    if (durationMinutes <= 0 || durationMinutes * SECONDS_PER_MINUTE >= SECONDS_PER_WEEK) continue;
    rows.push({
      weekday: session.weekday,
      startSeconds: (session.weekday * MINUTES_PER_DAY + openMinute) * SECONDS_PER_MINUTE,
      durationSeconds: durationMinutes * SECONDS_PER_MINUTE,
    });
  }
  rows.sort((left, right) => left.startSeconds - right.startSeconds);

  return rows.map((session, position) => ({ ...session, position }));
}

function dateKey(parts: Pick<ZonedDateParts, "year" | "month" | "day">): string {
  return [parts.year, parts.month, parts.day]
    .map((value, index) => index === 0 ? String(value) : String(value).padStart(2, "0"))
    .join("-");
}

function dateLabel(parts: Pick<ZonedDateParts, "month" | "day">): string {
  return `${String(parts.month).padStart(2, "0")}/${String(parts.day).padStart(2, "0")}`;
}

function localDaySerial(parts: Pick<ZonedDateParts, "year" | "month" | "day">): number {
  return Math.floor(Date.UTC(parts.year, parts.month - 1, parts.day) / (SECONDS_PER_DAY * 1_000));
}

function shiftedDateParts(
  parts: Pick<ZonedDateParts, "year" | "month" | "day">,
  days: number,
  skipWeekend = false,
): Pick<ZonedDateParts, "year" | "month" | "day"> {
  const value = new Date(Date.UTC(parts.year, parts.month - 1, parts.day + days));
  if (skipWeekend) {
    while (value.getUTCDay() === 0 || value.getUTCDay() === 6) {
      value.setUTCDate(value.getUTCDate() + 1);
    }
  }
  return {
    year: value.getUTCFullYear(),
    month: value.getUTCMonth() + 1,
    day: value.getUTCDate(),
  };
}

function calendarTradingDay(epochSeconds: number): TimelineTradingDay | null {
  const parts = zonedDateParts(epochSeconds, DISPLAY_TIME_ZONE);
  if (!parts) return null;
  const elapsed = parts.hour * 3_600 + parts.minute * 60 + parts.second;
  const actualStart = epochSeconds - elapsed;
  const ordinal = localDaySerial(parts);
  return {
    key: dateKey(parts),
    label: dateLabel(parts),
    ordinal,
    actualStart,
    actualEnd: actualStart + SECONDS_PER_DAY,
    chartStart: ordinal * SECONDS_PER_DAY,
    chartEnd: (ordinal + 1) * SECONDS_PER_DAY,
    logicalStart: 0,
    logicalEnd: 0,
  };
}

export function tradingDayAt(
  epochSeconds: number,
  schedule: MarketSchedule | null | undefined,
): TimelineTradingDay | null {
  if (!Number.isFinite(epochSeconds)) return null;
  if (!schedule || schedule.sessions.length === 0) return calendarTradingDay(epochSeconds);

  const parts = zonedDateParts(epochSeconds, schedule.time_zone);
  const sessions = normalizedSessions(schedule);
  if (!parts || sessions.length === 0) return calendarTradingDay(epochSeconds);
  const weekSecond = parts.weekday * SECONDS_PER_DAY
    + parts.hour * 3_600
    + parts.minute * 60
    + parts.second;

  for (const session of sessions) {
    const elapsed = (weekSecond - session.startSeconds + SECONDS_PER_WEEK) % SECONDS_PER_WEEK;
    if (elapsed >= session.durationSeconds) continue;

    const actualStart = epochSeconds - elapsed;
    const startParts = zonedDateParts(actualStart, schedule.time_zone);
    const closeParts = zonedDateParts(actualStart + session.durationSeconds - 1, schedule.time_zone);
    if (!startParts || !closeParts) return null;
    const tradingDateParts = schedule.trading_day_rule === "session_start"
      ? startParts
      : schedule.trading_day_rule === "shfe"
        ? startParts.hour >= 18
          ? shiftedDateParts(startParts, 1, true)
          : startParts
        : closeParts;
    const weekStartSerial = localDaySerial(startParts) - session.weekday;
    const weekIndex = Math.floor((weekStartSerial - SUNDAY_1970_DAY_SERIAL) / 7);
    const ordinal = weekIndex * sessions.length + session.position;

    return {
      key: dateKey(tradingDateParts),
      label: dateLabel(tradingDateParts),
      ordinal,
      actualStart,
      actualEnd: actualStart + session.durationSeconds,
      chartStart: ordinal * SECONDS_PER_DAY,
      chartEnd: (ordinal + 1) * SECONDS_PER_DAY,
      logicalStart: 0,
      logicalEnd: 0,
    };
  }
  return null;
}

export function tradingDayIdentity(
  epochSeconds: number | null,
  schedule: MarketSchedule | null | undefined,
): string {
  if (epochSeconds === null) return "";
  return tradingDayAt(epochSeconds, schedule)?.key ?? "";
}

export function buildTimelineLayout(
  actualTimes: readonly number[],
  schedule: MarketSchedule | null | undefined,
): TimelineLayout {
  const daysByOrdinal = new Map<number, TimelineTradingDay>();
  let activeDay: TimelineTradingDay | null = null;
  const sortedTimes = actualTimes.filter(Number.isFinite).sort((left, right) => left - right);

  for (const actualTime of sortedTimes) {
    if (
      activeDay
      && actualTime >= activeDay.actualStart
      && actualTime < activeDay.actualEnd
    ) {
      continue;
    }
    activeDay = tradingDayAt(actualTime, schedule);
    if (activeDay) daysByOrdinal.set(activeDay.ordinal, activeDay);
  }

  const days = [...daysByOrdinal.values()]
    .sort((left, right) => left.chartStart - right.chartStart)
    .map((day, index) => ({
      ...day,
      logicalStart: index,
      logicalEnd: index,
    }));
  return { days };
}

function dayForActualTime(layout: TimelineLayout, actualTime: number): TimelineTradingDay | null {
  let low = 0;
  let high = layout.days.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const day = layout.days[middle];
    if (actualTime < day.actualStart) high = middle - 1;
    else if (actualTime >= day.actualEnd) low = middle + 1;
    else return day;
  }
  return null;
}

export function projectTimelineTime(layout: TimelineLayout, actualTime: number): number | null {
  const day = dayForActualTime(layout, actualTime);
  if (!day) return null;
  const progress = Math.max(
    0,
    Math.min(1 - Number.EPSILON, (actualTime - day.actualStart) / (day.actualEnd - day.actualStart)),
  );
  return day.chartStart + progress * SECONDS_PER_DAY;
}

export function projectTimelineSeries(
  series: readonly TimelineSample[],
  layout: TimelineLayout,
): ProjectedTimelinePoint[] {
  const ordered = series
    .map((point) => ({
      point,
      actualTime: Number.isFinite(point.observedTime) ? Number(point.observedTime) : point.time,
    }))
    .filter(({ point, actualTime }) => Number.isFinite(actualTime) && Number.isFinite(point.value))
    .sort((left, right) => {
      const observedDifference = left.actualTime - right.actualTime;
      if (observedDifference !== 0) return observedDifference;
      const receivedDifference = left.point.time - right.point.time;
      if (receivedDifference !== 0) return receivedDifference;
      return (left.point.eventId ?? "").localeCompare(right.point.eventId ?? "");
    });

  const rows: ProjectedTimelinePoint[] = [];
  let previousChartTime = Number.NEGATIVE_INFINITY;
  for (const { point, actualTime } of ordered) {
    const projectedTime = projectTimelineTime(layout, actualTime);
    if (projectedTime === null) continue;
    const time = projectedTime > previousChartTime
      ? projectedTime
      : previousChartTime + TIMELINE_TIME_EPSILON_SECONDS;
    rows.push({ time, actualTime, value: point.value });
    previousChartTime = time;
  }
  return rows;
}

const SESSION_BOUNDARY_TOLERANCE_SECONDS = 2 * SECONDS_PER_MINUTE;

export function buildTimelineSessionGaps(
  layout: TimelineLayout,
  series: readonly ProjectedTimelinePoint[],
  priceDigits: number,
): TimelineSessionGap[] {
  const pointsByOrdinal = new Map<number, ProjectedTimelinePoint[]>();
  for (const point of series) {
    const day = dayForActualTime(layout, point.actualTime);
    if (!day) continue;
    const points = pointsByOrdinal.get(day.ordinal) ?? [];
    points.push(point);
    pointsByOrdinal.set(day.ordinal, points);
  }

  const gaps: TimelineSessionGap[] = [];
  for (let index = 1; index < layout.days.length; index += 1) {
    const previous = layout.days[index - 1];
    const next = layout.days[index];
    const durationSeconds = Math.max(0, next.actualStart - previous.actualEnd);
    if (durationSeconds <= 0) continue;

    const previousPoints = pointsByOrdinal.get(previous.ordinal) ?? [];
    const nextPoints = pointsByOrdinal.get(next.ordinal) ?? [];
    const previousPoint = previousPoints.at(-1) ?? null;
    const nextPoint = nextPoints.at(0) ?? null;
    const closeComplete = previousPoint !== null
      && previous.actualEnd - previousPoint.actualTime <= SESSION_BOUNDARY_TOLERANCE_SECONDS;
    const openComplete = nextPoint !== null
      && nextPoint.actualTime - next.actualStart <= SESSION_BOUNDARY_TOLERANCE_SECONDS;
    const boundaryState: SessionGapBoundaryState = closeComplete && openComplete
      ? "complete"
      : !closeComplete && !openComplete
        ? "missing-both"
        : closeComplete
          ? "missing-open"
          : "missing-close";
    const previousClose = closeComplete ? previousPoint.value : null;
    const nextOpen = openComplete ? nextPoint.value : null;
    const priceDifference = previousClose !== null && nextOpen !== null
      ? nextOpen - previousClose
      : null;
    const pricePercent = priceDifference !== null && previousClose !== null && previousClose !== 0
      ? priceDifference / previousClose * 100
      : null;
    const flatThreshold = 0.5 * 10 ** -Math.max(0, priceDigits);
    const direction: SessionGapDirection = priceDifference === null
      ? "unknown"
      : Math.abs(priceDifference) < flatThreshold
        ? "flat"
        : priceDifference > 0
          ? "up"
          : "down";

    gaps.push({
      id: `${previous.key}:${next.key}`,
      chartTime: nextPoint?.time ?? next.chartStart,
      previousTradingDay: previous.key,
      nextTradingDay: next.key,
      closedAt: previous.actualEnd,
      openedAt: next.actualStart,
      durationSeconds,
      kind: durationSeconds >= SECONDS_PER_DAY ? "weekend" : "session",
      boundaryState,
      previousClose,
      nextOpen,
      priceDifference,
      pricePercent,
      direction,
    });
  }
  return gaps;
}

export function formatSessionGapDuration(totalSeconds: number, compact = false): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(seconds / SECONDS_PER_DAY);
  const hours = Math.floor(seconds % SECONDS_PER_DAY / 3_600);
  const minutes = Math.floor(seconds % 3_600 / 60);
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}天`);
  if (hours > 0) parts.push(`${hours}${compact ? "时" : "小时"}`);
  if (minutes > 0 || parts.length === 0) {
    parts.push(`${parts.length > 0 ? String(minutes).padStart(2, "0") : minutes}分`);
  }
  return parts.join("");
}

export function dayForChartTime(
  layout: TimelineLayout,
  chartTime: number,
): TimelineTradingDay | null {
  let low = 0;
  let high = layout.days.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const day = layout.days[middle];
    if (chartTime < day.chartStart) high = middle - 1;
    else if (chartTime >= day.chartEnd) low = middle + 1;
    else return day;
  }
  return null;
}

export function actualTimeForTimelineChartTime(
  layout: TimelineLayout,
  chartTime: number,
): number | null {
  const day = dayForChartTime(layout, chartTime);
  if (!day) return null;
  const progress = (chartTime - day.chartStart) / SECONDS_PER_DAY;
  return day.actualStart + progress * (day.actualEnd - day.actualStart);
}

function pad2(value: number): string {
  return String(value).padStart(2, "0");
}

function yearLabel(parts: ZonedDateParts): string {
  return String(parts.year);
}

function monthLabel(parts: ZonedDateParts): string {
  return `${parts.year}/${pad2(parts.month)}`;
}

function quarterLabel(parts: ZonedDateParts): string {
  return `${parts.year} Q${Math.floor((parts.month - 1) / 3) + 1}`;
}

function shortDateLabel(parts: ZonedDateParts): string {
  return `${pad2(parts.month)}/${pad2(parts.day)}`;
}

function fullDateLabel(parts: ZonedDateParts): string {
  return `${parts.year}/${pad2(parts.month)}/${pad2(parts.day)}`;
}

function clockLabel(parts: ZonedDateParts, seconds: boolean): string {
  const minute = `${pad2(parts.hour)}:${pad2(parts.minute)}`;
  return seconds ? `${minute}:${pad2(parts.second)}` : minute;
}

function beijingParts(epochSeconds: number): ZonedDateParts | null {
  return zonedDateParts(epochSeconds, DISPLAY_TIME_ZONE);
}

export function formatChartTick(
  actualTime: number,
  tickMarkType: TickMarkType,
  period: ChartPeriod,
  visibleSpanSeconds = 0,
): string {
  if (!Number.isFinite(actualTime)) return "";
  const parts = beijingParts(actualTime);
  if (!parts) return "";

  if (period.aggregation.kind === "calendar") {
    switch (period.aggregation.unit) {
      case "year":
        return yearLabel(parts);
      case "quarter":
        return quarterLabel(parts);
      case "month":
        return tickMarkType === TickMarkType.Year ? yearLabel(parts) : monthLabel(parts);
      case "day":
      case "week":
        if (tickMarkType === TickMarkType.Year) return yearLabel(parts);
        if (tickMarkType === TickMarkType.Month) return monthLabel(parts);
        return shortDateLabel(parts);
    }
  }

  if (
    visibleSpanSeconds >= DATE_ONLY_AXIS_THRESHOLD_SECONDS
    && (tickMarkType === TickMarkType.Time || tickMarkType === TickMarkType.TimeWithSeconds)
  ) return "";
  if (tickMarkType === TickMarkType.Year) return yearLabel(parts);
  if (tickMarkType === TickMarkType.Month) return monthLabel(parts);
  if (tickMarkType === TickMarkType.DayOfMonth) return shortDateLabel(parts);
  return clockLabel(parts, tickMarkType === TickMarkType.TimeWithSeconds);
}

export function formatTimelineTick(
  chartTime: number,
  tickMarkType: TickMarkType,
  layout: TimelineLayout,
  intraday: boolean,
): string {
  const day = dayForChartTime(layout, chartTime);
  if (!day) return "";
  if (!intraday) return "";
  if (
    tickMarkType === TickMarkType.Year
    || tickMarkType === TickMarkType.Month
    || tickMarkType === TickMarkType.DayOfMonth
  ) return day.label;
  const actualTime = actualTimeForTimelineChartTime(layout, chartTime);
  if (actualTime === null) return "";
  const parts = beijingParts(actualTime);
  return parts ? clockLabel(parts, tickMarkType === TickMarkType.TimeWithSeconds) : "";
}

export function formatChartTimeLabel(
  actualTime: number,
  period: ChartPeriod,
  timelineResolutionSeconds = 60,
): string {
  if (!Number.isFinite(actualTime)) return "--";
  const parts = beijingParts(actualTime);
  if (!parts) return "--";

  if (period.mode === "timeline") {
    return `${fullDateLabel(parts)} ${clockLabel(parts, timelineResolutionSeconds < 60)}`;
  }
  if (period.aggregation.kind === "fixed") {
    return `${fullDateLabel(parts)} ${clockLabel(parts, false)}`;
  }
  switch (period.aggregation.unit) {
    case "day":
    case "week":
      return fullDateLabel(parts);
    case "month":
      return monthLabel(parts);
    case "quarter":
      return quarterLabel(parts);
    case "year":
      return yearLabel(parts);
  }
}

export function formatCrosshairTime(
  chartTime: number,
  period: ChartPeriod,
  layout: TimelineLayout,
  timelineResolutionSeconds = 60,
  exactTimelineActualTime?: number,
): string {
  const actualTime = period.mode === "timeline"
    ? exactTimelineActualTime ?? actualTimeForTimelineChartTime(layout, chartTime)
    : chartTime;
  if (actualTime === null || !Number.isFinite(actualTime)) return "--";
  return formatChartTimeLabel(actualTime, period, timelineResolutionSeconds);
}

export function formatBeijingClock(epochSeconds: number, includeSeconds = true): string {
  if (!Number.isFinite(epochSeconds)) return "--";
  const parts = beijingParts(epochSeconds);
  return parts ? clockLabel(parts, includeSeconds) : "--";
}

export function formatBeijingDateTime(epochSeconds: number): string {
  if (!Number.isFinite(epochSeconds)) return "--";
  const parts = beijingParts(epochSeconds);
  return parts ? `${shortDateLabel(parts)} ${clockLabel(parts, false)}` : "--";
}
