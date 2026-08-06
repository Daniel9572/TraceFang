import { CHART_PERIODS, type ChartPeriodId } from "./chartPeriods.ts";

export interface PeriodPreferences {
  order: ChartPeriodId[];
  visible: ChartPeriodId[];
}

export const DEFAULT_PERIOD_ORDER: readonly ChartPeriodId[] = [
  "timeline",
  "1m",
  "3m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "1d",
  "10m",
  "2h",
  "6h",
  "8h",
  "12h",
  "1w",
  "1mo",
  "1q",
  "1y",
];

export const DEFAULT_VISIBLE_PERIODS: readonly ChartPeriodId[] = [
  "timeline",
  "1m",
  "3m",
  "5m",
  "15m",
  "30m",
  "1h",
  "4h",
  "1d",
];

const VALID_PERIOD_IDS = new Set<ChartPeriodId>(CHART_PERIODS.map((period) => period.id));

function uniqueValidIds(value: unknown): ChartPeriodId[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<ChartPeriodId>();
  const result: ChartPeriodId[] = [];
  for (const item of value) {
    if (typeof item !== "string" || !VALID_PERIOD_IDS.has(item as ChartPeriodId)) continue;
    const id = item as ChartPeriodId;
    if (seen.has(id)) continue;
    seen.add(id);
    result.push(id);
  }
  return result;
}

export function defaultPeriodPreferences(): PeriodPreferences {
  return {
    order: [...DEFAULT_PERIOD_ORDER],
    visible: [...DEFAULT_VISIBLE_PERIODS],
  };
}

export function normalizePeriodPreferences(value: unknown): PeriodPreferences {
  if (!value || typeof value !== "object") return defaultPeriodPreferences();
  const record = value as Partial<Record<keyof PeriodPreferences, unknown>>;
  const order = uniqueValidIds(record.order);
  for (const id of DEFAULT_PERIOD_ORDER) {
    if (!order.includes(id)) order.push(id);
  }
  for (const period of CHART_PERIODS) {
    if (!order.includes(period.id)) order.push(period.id);
  }

  let visible = uniqueValidIds(record.visible);
  if (visible.length === 0) visible = [...DEFAULT_VISIBLE_PERIODS];
  const visibleSet = new Set(visible);
  visible = order.filter((id) => visibleSet.has(id));
  return { order, visible };
}

export function togglePeriodVisibility(
  preferences: PeriodPreferences,
  id: ChartPeriodId,
): PeriodPreferences {
  const visible = new Set(preferences.visible);
  if (visible.has(id)) {
    if (visible.size === 1) return preferences;
    visible.delete(id);
  } else {
    visible.add(id);
  }
  return {
    order: [...preferences.order],
    visible: preferences.order.filter((periodId) => visible.has(periodId)),
  };
}

export function movePeriod(
  preferences: PeriodPreferences,
  id: ChartPeriodId,
  direction: -1 | 1,
): PeriodPreferences {
  const index = preferences.order.indexOf(id);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= preferences.order.length) return preferences;
  const order = [...preferences.order];
  [order[index], order[target]] = [order[target], order[index]];
  const visible = new Set(preferences.visible);
  return { order, visible: order.filter((periodId) => visible.has(periodId)) };
}

export function reorderPeriodBefore(
  preferences: PeriodPreferences,
  sourceId: ChartPeriodId,
  targetId: ChartPeriodId,
): PeriodPreferences {
  if (sourceId === targetId || !preferences.order.includes(sourceId) || !preferences.order.includes(targetId)) {
    return preferences;
  }
  const order = preferences.order.filter((id) => id !== sourceId);
  order.splice(order.indexOf(targetId), 0, sourceId);
  const visible = new Set(preferences.visible);
  return { order, visible: order.filter((periodId) => visible.has(periodId)) };
}
