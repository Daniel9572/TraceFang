import { marketSessionAt } from "./marketSession.ts";
import type { ExpertSessionBand, ExpertSessionKind } from "./expertTypes";
import type { MarketSchedule } from "./types";

interface ZonedClock {
  year: number;
  month: number;
  day: number;
  weekday: number;
  minuteOfDay: number;
}

export interface ExpertHolidayClosure {
  id: string;
  label: string;
  start: number;
  end: number;
  source: string;
}

const WEEKDAY: Record<string, number> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
};
const SESSION_CLOCK_FORMATTERS = new Map<string, Intl.DateTimeFormat>();

const SESSION_LABELS: Record<ExpertSessionKind, string> = {
  asia: "亚盘主导",
  europe: "欧盘主导",
  us: "美盘主导",
};

const SESSION_TIME_ZONES: Record<ExpertSessionKind, string> = {
  asia: "Asia/Shanghai",
  europe: "Europe/London",
  us: "America/New_York",
};

export const EXPERT_HOLIDAY_CLOSURES_2026: ExpertHolidayClosure[] = [
  {
    id: "2026-new-year",
    label: "元旦参考休市",
    start: Date.parse("2026-01-01T05:00:00Z") / 1_000,
    end: Date.parse("2026-01-02T05:00:00Z") / 1_000,
    source: "US metals holiday reference",
  },
  {
    id: "2026-good-friday",
    label: "Good Friday 参考休市",
    start: Date.parse("2026-04-03T04:00:00Z") / 1_000,
    end: Date.parse("2026-04-04T04:00:00Z") / 1_000,
    source: "CME 2026 holiday notice",
  },
  {
    id: "2026-christmas",
    label: "圣诞节参考休市",
    start: Date.parse("2026-12-25T05:00:00Z") / 1_000,
    end: Date.parse("2026-12-26T05:00:00Z") / 1_000,
    source: "CME 2026 holiday notice",
  },
];

function zonedClock(epochSeconds: number, timeZone: string): ZonedClock | null {
  try {
    let formatter = SESSION_CLOCK_FORMATTERS.get(timeZone);
    if (!formatter) {
      formatter = new Intl.DateTimeFormat("en-US", {
        timeZone,
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        weekday: "short",
        hour: "2-digit",
        minute: "2-digit",
        hourCycle: "h23",
      });
      SESSION_CLOCK_FORMATTERS.set(timeZone, formatter);
    }
    const parts = formatter.formatToParts(new Date(epochSeconds * 1_000));
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const weekday = WEEKDAY[values.weekday];
    const hour = Number(values.hour);
    const minute = Number(values.minute);
    if (weekday === undefined || !Number.isInteger(hour) || !Number.isInteger(minute)) {
      return null;
    }
    const year = Number(values.year);
    const month = Number(values.month);
    const day = Number(values.day);
    if (![year, month, day].every(Number.isInteger)) return null;
    return { year, month, day, weekday, minuteOfDay: hour * 60 + minute };
  } catch {
    return null;
  }
}

function insideWeekdayWindow(
  epochSeconds: number,
  timeZone: string,
  startMinute: number,
  endMinute: number,
): boolean {
  const value = zonedClock(epochSeconds, timeZone);
  return Boolean(
    value
    && value.weekday >= 1
    && value.weekday <= 5
    && value.minuteOfDay >= startMinute
    && value.minuteOfDay < endMinute,
  );
}

export function dominantGoldSessionAt(epochSeconds: number): ExpertSessionKind | null {
  if (insideWeekdayWindow(epochSeconds, "America/New_York", 8 * 60 + 20, 17 * 60)) {
    return "us";
  }
  if (insideWeekdayWindow(epochSeconds, "Europe/London", 8 * 60, 16 * 60 + 30)) {
    return "europe";
  }
  if (insideWeekdayWindow(epochSeconds, "Asia/Shanghai", 8 * 60, 15 * 60 + 30)) {
    return "asia";
  }
  return null;
}

function isHolidayClosed(epochSeconds: number, closures: ExpertHolidayClosure[]): boolean {
  return closures.some((closure) => epochSeconds >= closure.start && epochSeconds < closure.end);
}

interface SessionInterval {
  kind: ExpertSessionKind;
  start: number;
  end: number;
  priority: number;
}

const SECONDS_PER_DAY = 24 * 60 * 60;
const SESSION_DEFINITIONS: ReadonlyArray<{
  kind: ExpertSessionKind;
  startMinute: number;
  endMinute: number;
  priority: number;
}> = [
  { kind: "asia", startMinute: 8 * 60, endMinute: 15 * 60 + 30, priority: 1 },
  { kind: "europe", startMinute: 8 * 60, endMinute: 16 * 60 + 30, priority: 2 },
  { kind: "us", startMinute: 8 * 60 + 20, endMinute: 17 * 60, priority: 3 },
];

function epochForZonedClock(
  year: number,
  month: number,
  day: number,
  minuteOfDay: number,
  timeZone: string,
): number | null {
  const hour = Math.floor(minuteOfDay / 60);
  const minute = minuteOfDay % 60;
  const target = Date.UTC(year, month - 1, day, hour, minute) / 1_000;
  let guess = target;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const parts = zonedClock(guess, timeZone);
    if (!parts) return null;
    const represented = Date.UTC(
      parts.year,
      parts.month - 1,
      parts.day,
      Math.floor(parts.minuteOfDay / 60),
      parts.minuteOfDay % 60,
    ) / 1_000;
    const correction = target - represented;
    guess += correction;
    if (Math.abs(correction) < 1) return guess;
  }
  return guess;
}

function sessionIntervalsForRange(start: number, end: number): SessionInterval[] {
  const intervals: SessionInterval[] = [];
  const firstDay = Math.floor(start / SECONDS_PER_DAY) - 2;
  const lastDay = Math.floor(end / SECONDS_PER_DAY) + 2;
  for (let serial = firstDay; serial <= lastDay; serial += 1) {
    const date = new Date(serial * SECONDS_PER_DAY * 1_000);
    const year = date.getUTCFullYear();
    const month = date.getUTCMonth() + 1;
    const day = date.getUTCDate();
    const weekday = date.getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    for (const definition of SESSION_DEFINITIONS) {
      const timeZone = SESSION_TIME_ZONES[definition.kind];
      const intervalStart = epochForZonedClock(
        year,
        month,
        day,
        definition.startMinute,
        timeZone,
      );
      const intervalEnd = epochForZonedClock(
        year,
        month,
        day,
        definition.endMinute,
        timeZone,
      );
      if (
        intervalStart === null
        || intervalEnd === null
        || intervalEnd <= start
        || intervalStart >= end
      ) continue;
      intervals.push({
        kind: definition.kind,
        start: intervalStart,
        end: intervalEnd,
        priority: definition.priority,
      });
    }
  }
  return intervals;
}

/**
 * Builds exact session boundaries from the covered date range. Its cost grows
 * with trading days, not with the number of ticks, so retaining every realtime
 * sample does not make each new quote scan the entire price history.
 */
export function buildExpertSessionBandsForRange(
  start: number,
  end: number,
  marketSchedule?: MarketSchedule | null,
  closures: ExpertHolidayClosure[] = EXPERT_HOLIDAY_CLOSURES_2026,
): ExpertSessionBand[] {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
  const intervals = sessionIntervalsForRange(start, end);
  const boundaries = new Set<number>([start, end]);
  for (const interval of intervals) {
    boundaries.add(Math.max(start, interval.start));
    boundaries.add(Math.min(end, interval.end));
  }
  for (const closure of closures) {
    if (closure.end <= start || closure.start >= end) continue;
    boundaries.add(Math.max(start, closure.start));
    boundaries.add(Math.min(end, closure.end));
  }
  const points = [...boundaries].sort((left, right) => left - right);
  const bands: ExpertSessionBand[] = [];
  for (let index = 0; index < points.length - 1; index += 1) {
    const segmentStart = points[index];
    const segmentEnd = points[index + 1];
    if (segmentEnd <= segmentStart) continue;
    const middle = segmentStart + (segmentEnd - segmentStart) / 2;
    if (isHolidayClosed(middle, closures)) continue;
    if (marketSchedule && marketSessionAt(marketSchedule, new Date(middle * 1_000)).phase !== "open") {
      continue;
    }
    const active = intervals
      .filter((interval) => middle >= interval.start && middle < interval.end)
      .sort((left, right) => right.priority - left.priority)[0];
    if (!active) continue;
    const previous = bands.at(-1);
    if (previous?.kind === active.kind && Math.abs(previous.end - segmentStart) < 1) {
      previous.end = segmentEnd;
      continue;
    }
    bands.push({
      id: `${active.kind}:${Math.floor(segmentStart)}`,
      kind: active.kind,
      label: SESSION_LABELS[active.kind],
      start: segmentStart,
      end: segmentEnd,
      timeZone: SESSION_TIME_ZONES[active.kind],
    });
  }
  return bands;
}

export function buildExpertSessionBands(
  epochSeconds: readonly number[],
  marketSchedule?: MarketSchedule | null,
  closures: ExpertHolidayClosure[] = EXPERT_HOLIDAY_CLOSURES_2026,
): ExpertSessionBand[] {
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const value of epochSeconds) {
    if (!Number.isFinite(value)) continue;
    start = Math.min(start, value);
    end = Math.max(end, value);
  }
  return Number.isFinite(start) && Number.isFinite(end)
    ? buildExpertSessionBandsForRange(start, end + 60, marketSchedule, closures)
    : [];
}
