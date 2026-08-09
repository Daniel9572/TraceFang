import { marketSessionAt } from "./marketSession.ts";
import { EXPERT_GOLD_EVENTS_2026 } from "./expertEvents.ts";
import type {
  ExpertMarketEvent,
  ExpertSessionBand,
  ExpertSessionDriver,
  ExpertSessionKind,
} from "./expertTypes";
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
  asia: "亚洲资金主导",
  europe: "欧洲/伦敦资金主导",
  us: "美国资金主导",
};

const SESSION_TIME_ZONES: Record<ExpertSessionKind, string> = {
  asia: "Asia/Shanghai",
  europe: "Europe/London",
  us: "America/New_York",
};

export const CAPITAL_DOMINANCE_STRATEGY = {
  id: "capital-dominance",
  name: "黄金资金主导时段",
  shortName: "资金主导",
  description: "只标注亚洲、欧洲/伦敦与美国资金主导；08:30 数据日提前进入美盘",
  dataSource: "IANA 时区 + 重要事件日历",
} as const;

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

function isHolidayClosed(epochSeconds: number, closures: ExpertHolidayClosure[]): boolean {
  return closures.some((closure) => epochSeconds >= closure.start && epochSeconds < closure.end);
}

interface SessionInterval {
  kind: ExpertSessionKind;
  start: number;
  end: number;
  priority: number;
  driver: ExpertSessionDriver;
  label: string;
  detail: string;
  eventId: string | null;
}

const SECONDS_PER_DAY = 24 * 60 * 60;
const NEW_YORK_DESK_MINUTE = 8 * 60;
const US_DATA_MINUTE = 8 * 60 + 30;
const US_EQUITY_OPEN_MINUTE = 9 * 60 + 30;
const US_DOMINANCE_END_MINUTE = 17 * 60;

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

function majorUSDataEventForDate(
  year: number,
  month: number,
  day: number,
  marketEvents: readonly ExpertMarketEvent[],
): ExpertMarketEvent | null {
  return marketEvents.find((event) => {
    if (event.importance !== "high" || event.timePrecision !== "instant") return false;
    const clock = zonedClock(event.time, "America/New_York");
    return Boolean(
      clock
      && clock.year === year
      && clock.month === month
      && clock.day === day
      && clock.minuteOfDay === US_DATA_MINUTE,
    );
  }) ?? null;
}

function sessionIntervalsForRange(
  start: number,
  end: number,
  marketEvents: readonly ExpertMarketEvent[],
): SessionInterval[] {
  const intervals: SessionInterval[] = [];
  const firstDay = Math.floor(start / SECONDS_PER_DAY) - 2;
  const lastDay = Math.floor(end / SECONDS_PER_DAY) + 2;
  const append = (interval: SessionInterval) => {
    if (interval.end > interval.start && interval.end > start && interval.start < end) {
      intervals.push(interval);
    }
  };
  for (let serial = firstDay; serial <= lastDay; serial += 1) {
    const date = new Date(serial * SECONDS_PER_DAY * 1_000);
    const year = date.getUTCFullYear();
    const month = date.getUTCMonth() + 1;
    const day = date.getUTCDate();
    const weekday = date.getUTCDay();
    if (weekday === 0 || weekday === 6) continue;
    const asiaStart = epochForZonedClock(year, month, day, 9 * 60, "Asia/Shanghai");
    const asiaEnd = epochForZonedClock(year, month, day, 15 * 60, "Asia/Shanghai");
    const londonStart = epochForZonedClock(year, month, day, 8 * 60, "Europe/London");
    const usDeskStart = epochForZonedClock(
      year,
      month,
      day,
      NEW_YORK_DESK_MINUTE,
      "America/New_York",
    );
    const dataEvent = majorUSDataEventForDate(year, month, day, marketEvents);
    const coreStartMinute = dataEvent ? US_DATA_MINUTE : US_EQUITY_OPEN_MINUTE;
    const usCoreStart = epochForZonedClock(
      year,
      month,
      day,
      coreStartMinute,
      "America/New_York",
    );
    const usEnd = epochForZonedClock(
      year,
      month,
      day,
      US_DOMINANCE_END_MINUTE,
      "America/New_York",
    );
    if (
      asiaStart === null
      || asiaEnd === null
      || londonStart === null
      || usDeskStart === null
      || usCoreStart === null
      || usEnd === null
    ) continue;

    append({
      kind: "asia",
      start: asiaStart,
      end: asiaEnd,
      priority: 1,
      driver: "regional-dominance",
      label: SESSION_LABELS.asia,
      detail: "北京时间 09:00–15:00",
      eventId: null,
    });
    append({
      kind: "europe",
      start: londonStart,
      end: usDeskStart,
      priority: 2,
      driver: "regional-dominance",
      label: SESSION_LABELS.europe,
      detail: "伦敦 08:00 开始，至纽约 08:00 资金进入前；交接阶段不标注主导方",
      eventId: null,
    });
    append({
      kind: "us",
      start: usCoreStart,
      end: usEnd,
      priority: 3,
      driver: dataEvent ? "us-data-release" : "us-equity-open",
      label: dataEvent
        ? `美国资金主导 · ${dataEvent.title}`
        : "美国资金主导 · 美股开盘",
      detail: dataEvent
        ? `${dataEvent.title}于纽约 08:30 发布，美国资金从数据时点开始主导`
        : "当日无已标记的 08:30 ET 高重要性数据，美国资金从 NYSE 09:30 开盘开始主导",
      eventId: dataEvent?.id ?? null,
    });
  }
  return intervals;
}

export function dominantGoldSessionAt(
  epochSeconds: number,
  marketEvents: readonly ExpertMarketEvent[] = EXPERT_GOLD_EVENTS_2026,
): ExpertSessionKind | null {
  if (!Number.isFinite(epochSeconds)) return null;
  return sessionIntervalsForRange(epochSeconds - 1, epochSeconds + 1, marketEvents)
    .filter((interval) => epochSeconds >= interval.start && epochSeconds < interval.end)
    .sort((left, right) => right.priority - left.priority)[0]?.kind ?? null;
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
  marketEvents: readonly ExpertMarketEvent[] = EXPERT_GOLD_EVENTS_2026,
): ExpertSessionBand[] {
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return [];
  const intervals = sessionIntervalsForRange(start, end, marketEvents);
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
    if (
      previous?.kind === active.kind
      && previous.driver === active.driver
      && previous.eventId === active.eventId
      && Math.abs(previous.end - segmentStart) < 1
    ) {
      previous.end = segmentEnd;
      continue;
    }
    bands.push({
      id: `${active.kind}:${active.driver}:${Math.floor(segmentStart)}`,
      kind: active.kind,
      label: active.label,
      detail: active.detail,
      start: segmentStart,
      end: segmentEnd,
      timeZone: SESSION_TIME_ZONES[active.kind],
      driver: active.driver,
      eventId: active.eventId,
    });
  }
  return bands;
}

export function buildExpertSessionBands(
  epochSeconds: readonly number[],
  marketSchedule?: MarketSchedule | null,
  closures: ExpertHolidayClosure[] = EXPERT_HOLIDAY_CLOSURES_2026,
  marketEvents: readonly ExpertMarketEvent[] = EXPERT_GOLD_EVENTS_2026,
): ExpertSessionBand[] {
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const value of epochSeconds) {
    if (!Number.isFinite(value)) continue;
    start = Math.min(start, value);
    end = Math.max(end, value);
  }
  return Number.isFinite(start) && Number.isFinite(end)
    ? buildExpertSessionBandsForRange(start, end + 60, marketSchedule, closures, marketEvents)
    : [];
}
