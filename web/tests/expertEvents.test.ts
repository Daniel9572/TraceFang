import assert from "node:assert/strict";
import test from "node:test";

import { buildExpertEventAssessments } from "../src/expertEventScoring.ts";
import {
  expertMarketEventsFromSnapshot,
  projectExpertEventStrategies,
} from "../src/expertEvents.ts";
import type {
  ExpertGoldEventCatalogSnapshot,
  ExpertGoldEventFactDto,
} from "../src/expertTypes.ts";
import type { Candle } from "../src/types.ts";

const epoch = (value: string) => Date.parse(value) / 1_000;

function fact(overrides: Partial<ExpertGoldEventFactDto> = {}): ExpertGoldEventFactDto {
  return {
    event_id: "bls-cpi-2026-08",
    event_type_id: "us-cpi",
    title: "美国 CPI",
    short_label: "CPI",
    country: "US",
    release_cluster_id: "us-data:2026-08-10T12:30:00+00:00",
    marker_at: "2026-08-10T12:30:00Z",
    scheduled_at: "2026-08-10T12:30:00Z",
    released_at: "2026-08-10T12:30:00Z",
    effective_period_start: "2026-07-01T00:00:00Z",
    effective_period_end: "2026-08-01T00:00:00Z",
    source_published_at: "2026-08-10T12:30:00Z",
    ingested_at: "2026-08-10T13:00:00Z",
    revision_vintage: "initial",
    actual: null,
    consensus: null,
    previous: null,
    revised: null,
    source: "U.S. Bureau of Labor Statistics",
    source_url: "https://www.bls.gov/",
    source_tier: "official",
    time_precision: "instant",
    family: "inflation",
    baseline_tier: "S+",
    transmission_channels: ["real-yields", "usd"],
    direction_rule: "按实际利率和美元重定价判断",
    us_dominance_trigger: true,
    flow_direction: "unknown",
    flow_amount: null,
    flow_unit: null,
    note: null,
    ...overrides,
  };
}

function snapshot(facts: ExpertGoldEventFactDto[]): ExpertGoldEventCatalogSnapshot {
  return {
    contract_version: "gold-events-v1",
    generated_at: "2026-08-10T13:00:00Z",
    event_types: [],
    facts,
    score_methodology: {
      shock: { label: "短期冲击分", weights: {}, windows_seconds: [], rule: "" },
      regime: { label: "中长期定价分", weights: {}, windows_seconds: [], rule: "" },
      tiers: { "S+": [85, 100], S: [70, 84], A: [55, 69], B: [40, 54] },
    },
    source_precedence: [],
    limitations: [],
  };
}

function syntheticCandles(): Candle[] {
  const start = epoch("2026-08-01T00:00:00Z");
  const eventTime = epoch("2026-08-10T12:30:00Z");
  const interval = 15 * 60;
  return Array.from({ length: 11 * 24 * 4 }, (_, index) => {
    const open = start + index * interval;
    const day = Math.floor((open - start) / (24 * 60 * 60));
    const minuteOfDay = ((open % (24 * 60 * 60)) + (24 * 60 * 60)) % (24 * 60 * 60);
    const afterDailyRelease = minuteOfDay >= 12 * 60 * 60 + 30 * 60;
    const normalMove = ((day % 5) - 2) * 0.08;
    const eventMove = open >= eventTime ? 5 : 0;
    const price = 100 + (afterDailyRelease ? normalMove : 0) + eventMove;
    const iso = new Date(open * 1_000).toISOString();
    const bucketEnd = new Date((open + interval) * 1_000).toISOString();
    return {
      instrument: { symbol: "XAUUSD", asset_class: "commodity", base: "XAU", quote: "USD", venue: null },
      interval,
      open_time: iso,
      open: price,
      high: price + 0.02,
      low: price - 0.02,
      close: price,
      volume: null,
      source: {
        provider: "test",
        provider_symbol: "XAUUSD",
        observed_at: bucketEnd,
        received_at: bucketEnd,
        raw_payload: { bucket_end: bucketEnd },
      },
      evidence_channel_id: "test",
      state: "final",
      revision: 1,
      finalized_at: bucketEnd,
    } satisfies Candle;
  });
}

test("normalizes independent event facts without collapsing effective and publication time", () => {
  const events = expertMarketEventsFromSnapshot(snapshot([fact()]));

  assert.equal(events.length, 1);
  assert.equal(events[0].baselineTier, "S+");
  assert.equal(events[0].usDominanceTrigger, true);
  assert.equal(events[0].releaseClusterId, "us-data:2026-08-10T12:30:00+00:00");
  assert.equal(events[0].effectivePeriodStart, epoch("2026-07-01T00:00:00Z"));
  assert.equal(events[0].sourcePublishedAt, epoch("2026-08-10T12:30:00Z"));
  assert.notEqual(events[0].effectivePeriodStart, events[0].sourcePublishedAt);
});

test("hiding event markers retains known capital-driver facts while replay hides future knowledge", () => {
  const event = expertMarketEventsFromSnapshot(snapshot([fact()]))[0];
  const hidden = projectExpertEventStrategies(false, null, [event]);
  assert.deepEqual(hidden.displayMarkers, []);
  assert.deepEqual(hidden.capitalDrivers, [event]);

  const beforePublication = projectExpertEventStrategies(
    true,
    epoch("2026-08-10T12:29:59Z"),
    [event],
  );
  assert.deepEqual(beforePublication.capitalDrivers, []);
  assert.deepEqual(beforePublication.displayMarkers, []);
});

test("scores only completed event windows and exposes missing evidence as coverage", () => {
  const event = expertMarketEventsFromSnapshot(snapshot([fact()]))[0];
  const candles = syntheticCandles();
  const eventTime = epoch("2026-08-10T12:30:00Z");

  assert.deepEqual(
    buildExpertEventAssessments(candles, [event], eventTime - 1),
    [],
  );
  const [assessment] = buildExpertEventAssessments(
    candles,
    [event],
    eventTime + 6 * 60 * 60,
  );
  assert.ok(assessment.shockScore !== null && assessment.shockScore > 80);
  assert.equal(assessment.shockCoverage, 35);
  assert.equal(assessment.regimeCoverage, 30);
  assert.equal(assessment.observedDirection, "bullish");
  assert.equal(assessment.confidence, "low");
  assert.equal(assessment.evidence.includes("成交活跃度（非净资金流）"), false);
});
