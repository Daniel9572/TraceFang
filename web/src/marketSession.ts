import type { MarketPhase, MarketSchedule } from "./types";

const MINUTES_PER_DAY = 24 * 60;
const MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY;
const WEEKDAY_INDEX: Record<string, number> = {
  Sun: 0,
  Mon: 1,
  Tue: 2,
  Wed: 3,
  Thu: 4,
  Fri: 5,
  Sat: 6,
};

export const SPOT_METALS_MARKET_SCHEDULE: MarketSchedule = {
  time_zone: "America/New_York",
  reference: "OTC 贵金属常规交易时段",
  sessions: [0, 1, 2, 3, 4].map((weekday) => ({
    weekday: weekday as 0 | 1 | 2 | 3 | 4,
    open: "18:05",
    close: "16:59",
    close_day_offset: 1,
  })),
};

export interface MarketSessionState {
  phase: MarketPhase;
  label: "交易中" | "休市" | "状态未知";
}

function parseClock(value: string): number | null {
  const match = /^(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return hour * 60 + minute;
}

function marketWeekMinute(now: Date, timeZone: string): number | null {
  try {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone,
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(now);
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    const weekday = WEEKDAY_INDEX[values.weekday];
    const hour = Number(values.hour);
    const minute = Number(values.minute);
    const second = Number(values.second);
    if (
      weekday === undefined
      || !Number.isInteger(hour)
      || !Number.isInteger(minute)
      || !Number.isInteger(second)
    ) {
      return null;
    }
    return weekday * MINUTES_PER_DAY + hour * 60 + minute + second / 60;
  } catch {
    return null;
  }
}

export function marketSessionAt(
  schedule: MarketSchedule | null | undefined,
  now = new Date(),
): MarketSessionState {
  if (!schedule || schedule.sessions.length === 0) {
    return { phase: "unknown", label: "状态未知" };
  }
  const weekMinute = marketWeekMinute(now, schedule.time_zone);
  if (weekMinute === null) {
    return { phase: "unknown", label: "状态未知" };
  }

  for (const session of schedule.sessions) {
    const openMinute = parseClock(session.open);
    const closeMinute = parseClock(session.close);
    if (openMinute === null || closeMinute === null || session.close_day_offset < 0) {
      continue;
    }
    const start = session.weekday * MINUTES_PER_DAY + openMinute;
    const duration = session.close_day_offset * MINUTES_PER_DAY + closeMinute - openMinute;
    if (duration <= 0 || duration >= MINUTES_PER_WEEK) continue;
    const elapsed = (weekMinute - start + MINUTES_PER_WEEK) % MINUTES_PER_WEEK;
    if (elapsed < duration) {
      return { phase: "open", label: "交易中" };
    }
  }
  return { phase: "closed", label: "休市" };
}
