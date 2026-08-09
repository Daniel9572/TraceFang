import type { ExpertMarketEvent } from "./expertTypes";

const epoch = (value: string) => Date.parse(value) / 1_000;

function blsEvent(
  id: string,
  time: string,
  title: string,
  category: "employment" | "inflation",
  timing: "scheduled" | "released" = "scheduled",
): ExpertMarketEvent {
  return {
    id,
    time: epoch(time),
    title,
    category,
    importance: "high",
    source: "U.S. BLS",
    sourceUrl: "https://www.bls.gov/schedule/2026/home.htm",
    timing,
    timePrecision: "instant",
  };
}

const BLS_MAJOR_DATA_EVENTS_2026: ExpertMarketEvent[] = [
  blsEvent("bls-nfp-2026-01", "2026-01-09T13:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-01", "2026-01-13T13:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-02", "2026-02-11T13:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-02", "2026-02-13T13:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-03", "2026-03-06T13:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-03", "2026-03-11T12:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-04", "2026-04-03T12:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-04", "2026-04-10T12:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-05", "2026-05-08T12:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-05", "2026-05-12T12:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-06", "2026-06-05T12:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-06", "2026-06-10T12:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-07", "2026-07-02T12:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-07", "2026-07-14T12:30:00Z", "美国 CPI", "inflation", "released"),
  blsEvent("bls-nfp-2026-08", "2026-08-07T12:30:00Z", "美国非农就业", "employment", "released"),
  blsEvent("bls-cpi-2026-08", "2026-08-12T12:30:00Z", "美国 CPI", "inflation"),
  blsEvent("bls-nfp-2026-09", "2026-09-04T12:30:00Z", "美国非农就业", "employment"),
  blsEvent("bls-cpi-2026-09", "2026-09-11T12:30:00Z", "美国 CPI", "inflation"),
  blsEvent("bls-nfp-2026-10", "2026-10-02T12:30:00Z", "美国非农就业", "employment"),
  blsEvent("bls-cpi-2026-10", "2026-10-14T12:30:00Z", "美国 CPI", "inflation"),
  blsEvent("bls-nfp-2026-11", "2026-11-06T13:30:00Z", "美国非农就业", "employment"),
  blsEvent("bls-cpi-2026-11", "2026-11-10T13:30:00Z", "美国 CPI", "inflation"),
  blsEvent("bls-nfp-2026-12", "2026-12-04T13:30:00Z", "美国非农就业", "employment"),
  blsEvent("bls-cpi-2026-12", "2026-12-10T13:30:00Z", "美国 CPI", "inflation"),
];

export const EXPERT_GOLD_EVENTS_2026: ExpertMarketEvent[] = ([
  {
    id: "wgc-central-bank-gold-2026-07",
    time: epoch("2026-07-02T12:00:00Z"),
    title: "全球央行 5 月净购金 41 吨",
    category: "central-bank-gold",
    importance: "medium",
    source: "World Gold Council",
    sourceUrl: "https://www.gold.org/goldhub/gold-focus/2026/07/central-bank-gold-statistics-central-banks-remain-committed-gold",
    timing: "released",
    timePrecision: "date",
  } satisfies ExpertMarketEvent,
  ...BLS_MAJOR_DATA_EVENTS_2026,
  {
    id: "fomc-minutes-2026-08",
    time: epoch("2026-08-19T18:00:00Z"),
    title: "FOMC 会议纪要",
    category: "fomc",
    importance: "high",
    source: "Federal Reserve",
    sourceUrl: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    timing: "scheduled",
    timePrecision: "instant",
  } satisfies ExpertMarketEvent,
  {
    id: "fomc-2026-09",
    time: epoch("2026-09-16T18:00:00Z"),
    title: "FOMC 利率决议",
    category: "fomc",
    importance: "high",
    source: "Federal Reserve",
    sourceUrl: "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    timing: "scheduled",
    timePrecision: "instant",
  } satisfies ExpertMarketEvent,
] satisfies ExpertMarketEvent[]).sort((left, right) => left.time - right.time);

export const IMPORTANT_EVENT_DISPLAY_STRATEGY = {
  id: "important-event-display",
  name: "黄金数据公布与重要事件",
  shortName: "数据/事件",
  description: "标记非农、CPI、FOMC 与央行购金等关键节点",
  dataSource: "官方事件日历",
} as const;

/**
 * Selects events for presentation only. Capital-dominance calculations must
 * continue to consume the complete event calendar even when this returns an
 * empty list because the user hid the visual strategy.
 */
export function expertEventsForDisplay(
  enabled: boolean,
  replayCutoff: number | null,
  marketEvents: readonly ExpertMarketEvent[] = EXPERT_GOLD_EVENTS_2026,
): readonly ExpertMarketEvent[] {
  if (!enabled) return [];
  if (replayCutoff === null) return marketEvents;
  return marketEvents.filter((event) => event.time <= replayCutoff);
}

export interface ExpertEventStrategyProjection {
  capitalDrivers: readonly ExpertMarketEvent[];
  displayMarkers: readonly ExpertMarketEvent[];
}

export function projectExpertEventStrategies(
  displayEnabled: boolean,
  replayCutoff: number | null,
  marketEvents: readonly ExpertMarketEvent[] = EXPERT_GOLD_EVENTS_2026,
): ExpertEventStrategyProjection {
  return {
    capitalDrivers: marketEvents,
    displayMarkers: expertEventsForDisplay(displayEnabled, replayCutoff, marketEvents),
  };
}
