export type ChartDisplayMode = "timeline" | "candlestick";
export type CalendarPeriodUnit = "day" | "week" | "month" | "quarter" | "year";
export type ChartPeriodId =
  | "timeline"
  | "1m"
  | "3m"
  | "5m"
  | "1h"
  | "1d"
  | "30m"
  | "4h"
  | "10m"
  | "15m"
  | "2h"
  | "6h"
  | "8h"
  | "12h"
  | "1w"
  | "1mo"
  | "1q"
  | "1y";

type FixedAggregation = {
  kind: "fixed";
  minutes: number;
};

type CalendarAggregation = {
  kind: "calendar";
  unit: CalendarPeriodUnit;
};

export interface ChartPeriod {
  id: ChartPeriodId;
  label: string;
  mode: ChartDisplayMode;
  aggregation: FixedAggregation | CalendarAggregation;
}

export const CHART_PERIODS: readonly ChartPeriod[] = [
  { id: "timeline", label: "分时", mode: "timeline", aggregation: { kind: "fixed", minutes: 1 } },
  { id: "1m", label: "1分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 1 } },
  { id: "3m", label: "3分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 3 } },
  { id: "5m", label: "5分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 5 } },
  { id: "1h", label: "1小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 60 } },
  { id: "1d", label: "日K", mode: "candlestick", aggregation: { kind: "calendar", unit: "day" } },
  { id: "30m", label: "30分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 30 } },
  { id: "4h", label: "4小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 240 } },
  { id: "10m", label: "10分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 10 } },
  { id: "15m", label: "15分", mode: "candlestick", aggregation: { kind: "fixed", minutes: 15 } },
  { id: "2h", label: "2小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 120 } },
  { id: "6h", label: "6小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 360 } },
  { id: "8h", label: "8小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 480 } },
  { id: "12h", label: "12小时", mode: "candlestick", aggregation: { kind: "fixed", minutes: 720 } },
  { id: "1w", label: "周K", mode: "candlestick", aggregation: { kind: "calendar", unit: "week" } },
  { id: "1mo", label: "月K", mode: "candlestick", aggregation: { kind: "calendar", unit: "month" } },
  { id: "1q", label: "季K", mode: "candlestick", aggregation: { kind: "calendar", unit: "quarter" } },
  { id: "1y", label: "年K", mode: "candlestick", aggregation: { kind: "calendar", unit: "year" } },
] as const;

const PERIODS_BY_ID = new Map(CHART_PERIODS.map((period) => [period.id, period]));

export function chartPeriodById(id: ChartPeriodId): ChartPeriod {
  return PERIODS_BY_ID.get(id) ?? CHART_PERIODS[1];
}

export function periodBucketSeconds(period: ChartPeriod, epochSeconds: number): number {
  if (period.aggregation.kind === "fixed") {
    const durationSeconds = period.aggregation.minutes * 60;
    return Math.floor(epochSeconds / durationSeconds) * durationSeconds;
  }

  const date = new Date(epochSeconds * 1000);
  date.setHours(0, 0, 0, 0);
  switch (period.aggregation.unit) {
    case "day":
      break;
    case "week": {
      const daysSinceMonday = (date.getDay() + 6) % 7;
      date.setDate(date.getDate() - daysSinceMonday);
      break;
    }
    case "month":
      date.setDate(1);
      break;
    case "quarter":
      date.setMonth(Math.floor(date.getMonth() / 3) * 3, 1);
      break;
    case "year":
      date.setMonth(0, 1);
      break;
  }
  return Math.floor(date.getTime() / 1000);
}

export function secondsUntilPeriodClose(period: ChartPeriod, nowMilliseconds = Date.now()): number {
  if (period.aggregation.kind === "fixed") {
    const durationSeconds = period.aggregation.minutes * 60;
    const nowSeconds = Math.floor(nowMilliseconds / 1000);
    const elapsed = nowSeconds % durationSeconds;
    return elapsed === 0 ? durationSeconds : durationSeconds - elapsed;
  }

  const startSeconds = periodBucketSeconds(period, Math.floor(nowMilliseconds / 1000));
  const next = new Date(startSeconds * 1000);
  switch (period.aggregation.unit) {
    case "day":
      next.setDate(next.getDate() + 1);
      break;
    case "week":
      next.setDate(next.getDate() + 7);
      break;
    case "month":
      next.setMonth(next.getMonth() + 1, 1);
      break;
    case "quarter":
      next.setMonth(next.getMonth() + 3, 1);
      break;
    case "year":
      next.setFullYear(next.getFullYear() + 1, 0, 1);
      break;
  }
  return Math.max(1, Math.ceil((next.getTime() - nowMilliseconds) / 1000));
}
