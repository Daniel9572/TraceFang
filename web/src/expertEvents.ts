import type {
  ExpertGoldEventCatalogSnapshot,
  ExpertGoldEventFactDto,
  ExpertMarketEvent,
} from "./expertTypes";

export const EMPTY_EXPERT_MARKET_EVENTS: readonly ExpertMarketEvent[] = [];

function epoch(value: string | null): number | null {
  if (value === null) return null;
  const parsed = Date.parse(value) / 1_000;
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeFact(value: ExpertGoldEventFactDto): ExpertMarketEvent | null {
  const time = epoch(value.marker_at);
  const sourcePublishedAt = epoch(value.source_published_at);
  const ingestedAt = epoch(value.ingested_at);
  if (time === null || sourcePublishedAt === null || ingestedAt === null) return null;
  return {
    id: value.event_id,
    time,
    title: value.title,
    shortLabel: value.short_label,
    eventTypeId: value.event_type_id,
    releaseClusterId: value.release_cluster_id,
    family: value.family,
    baselineTier: value.baseline_tier,
    transmissionChannels: [...value.transmission_channels],
    directionRule: value.direction_rule,
    usDominanceTrigger: value.us_dominance_trigger,
    source: value.source,
    sourceUrl: value.source_url,
    sourceTier: value.source_tier,
    timing: value.released_at === null ? "scheduled" : "released",
    timePrecision: value.time_precision,
    scheduledAt: epoch(value.scheduled_at),
    releasedAt: epoch(value.released_at),
    effectivePeriodStart: epoch(value.effective_period_start),
    effectivePeriodEnd: epoch(value.effective_period_end),
    sourcePublishedAt,
    ingestedAt,
    revisionVintage: value.revision_vintage,
    actual: value.actual,
    consensus: value.consensus,
    previous: value.previous,
    revised: value.revised,
    flowDirection: value.flow_direction,
    flowAmount: value.flow_amount,
    flowUnit: value.flow_unit,
    note: value.note,
  };
}

export function expertMarketEventsFromSnapshot(
  snapshot: ExpertGoldEventCatalogSnapshot,
): ExpertMarketEvent[] {
  if (snapshot.contract_version !== "gold-events-v1") return [];
  return snapshot.facts
    .map(normalizeFact)
    .filter((value): value is ExpertMarketEvent => value !== null)
    .sort((left, right) => left.time - right.time || left.id.localeCompare(right.id));
}

export const IMPORTANT_EVENT_DISPLAY_STRATEGY = {
  id: "important-event-display",
  name: "黄金数据公布与重要事件",
  shortName: "数据/事件",
  description: "独立标记宏观、政策、风险与黄金资金流事实",
  dataSource: "官方优先事件事实库",
} as const;

function knownEventsAt(
  replayCutoff: number | null,
  marketEvents: readonly ExpertMarketEvent[],
): readonly ExpertMarketEvent[] {
  if (replayCutoff === null) return marketEvents;
  return marketEvents.filter((event) => event.sourcePublishedAt <= replayCutoff);
}

/**
 * Selects events for presentation only. Capital-dominance calculations consume
 * independently known facts even when the visual strategy is hidden.
 */
export function expertEventsForDisplay(
  enabled: boolean,
  replayCutoff: number | null,
  marketEvents: readonly ExpertMarketEvent[],
): readonly ExpertMarketEvent[] {
  if (!enabled) return [];
  const known = knownEventsAt(replayCutoff, marketEvents);
  if (replayCutoff === null) return known;
  return known.filter((event) => event.time <= replayCutoff);
}

export interface ExpertEventStrategyProjection {
  capitalDrivers: readonly ExpertMarketEvent[];
  displayMarkers: readonly ExpertMarketEvent[];
}

export function projectExpertEventStrategies(
  displayEnabled: boolean,
  replayCutoff: number | null,
  marketEvents: readonly ExpertMarketEvent[],
): ExpertEventStrategyProjection {
  return {
    capitalDrivers: knownEventsAt(replayCutoff, marketEvents),
    displayMarkers: expertEventsForDisplay(displayEnabled, replayCutoff, marketEvents),
  };
}
