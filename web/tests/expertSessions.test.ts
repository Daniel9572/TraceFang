import assert from "node:assert/strict";
import test from "node:test";

import {
  projectExpertEventStrategies,
} from "../src/expertEvents.ts";
import {
  buildExpertSessionBands,
  buildExpertSessionBandsForRange,
  dominantGoldSessionAt,
  EXPERT_HOLIDAY_CLOSURES_2026,
} from "../src/expertSessions.ts";
import type { ExpertMarketEvent } from "../src/expertTypes.ts";

const epoch = (value: string) => Date.parse(value) / 1_000;
const majorDataEvent: ExpertMarketEvent = {
  id: "bls-cpi-2026-08",
  time: epoch("2026-08-12T12:30:00Z"),
  title: "美国 CPI",
  shortLabel: "CPI",
  eventTypeId: "us-cpi",
  releaseClusterId: "us-data:2026-08-12T12:30:00+00:00",
  family: "inflation",
  baselineTier: "S+",
  transmissionChannels: ["real-yields", "usd"],
  directionRule: "按实际利率和美元重定价判断",
  usDominanceTrigger: true,
  source: "U.S. Bureau of Labor Statistics",
  sourceUrl: "https://www.bls.gov/",
  sourceTier: "official",
  timing: "scheduled",
  timePrecision: "instant",
  scheduledAt: epoch("2026-08-12T12:30:00Z"),
  releasedAt: null,
  effectivePeriodStart: null,
  effectivePeriodEnd: null,
  sourcePublishedAt: epoch("2026-08-10T00:00:00Z"),
  ingestedAt: epoch("2026-08-10T00:00:00Z"),
  revisionVintage: "initial",
  actual: null,
  consensus: null,
  previous: null,
  revised: null,
  flowDirection: "unknown",
  flowAmount: null,
  flowUnit: null,
  note: null,
};

test("uses IANA daylight saving rules for Beijing capital-dominance boundaries", () => {
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T01:00:00Z")), "asia");
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T07:00:00Z")), "europe");
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T12:00:00Z")), null);
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T12:30:00Z")), null);
  assert.equal(dominantGoldSessionAt(epoch("2026-07-15T13:30:00Z")), "us");

  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T01:00:00Z")), "asia");
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T08:00:00Z")), "europe");
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T13:00:00Z")), null);
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T13:30:00Z")), null);
  assert.equal(dominantGoldSessionAt(epoch("2026-01-15T14:30:00Z")), "us");
});

test("starts US dominance at 08:30 ET on major-data days and 09:30 ET otherwise", () => {
  const dataDay = buildExpertSessionBandsForRange(
    epoch("2026-08-12T00:00:00Z"),
    epoch("2026-08-13T00:00:00Z"),
    null,
    [],
    [majorDataEvent],
  );
  const dataCore = dataDay.find((band) => band.kind === "us");
  assert.equal(dataCore?.start, epoch("2026-08-12T12:30:00Z"));
  assert.equal(dataCore?.end, epoch("2026-08-12T21:00:00Z"));
  assert.equal(dataCore?.driver, "us-data-release");
  assert.match(dataCore?.label ?? "", /美国 CPI/);

  const ordinaryDay = buildExpertSessionBandsForRange(
    epoch("2026-08-13T00:00:00Z"),
    epoch("2026-08-14T00:00:00Z"),
    null,
    [],
  );
  const ordinaryCore = ordinaryDay.find((band) => band.kind === "us");
  assert.equal(ordinaryCore?.start, epoch("2026-08-13T13:30:00Z"));
  assert.equal(ordinaryCore?.driver, "us-equity-open");
  assert.match(ordinaryCore?.label ?? "", /美股开盘/);
  assert.deepEqual(
    [...new Set([...dataDay, ...ordinaryDay].map((band) => band.kind))].sort(),
    ["asia", "europe", "us"],
  );
});

test("hiding event markers never removes the event facts that drive US dominance", () => {
  const projection = projectExpertEventStrategies(false, null, [majorDataEvent]);
  assert.deepEqual(projection.displayMarkers, []);
  assert.deepEqual(projection.capitalDrivers, [majorDataEvent]);

  const linkedBands = buildExpertSessionBandsForRange(
    epoch("2026-08-12T00:00:00Z"),
    epoch("2026-08-13T00:00:00Z"),
    null,
    [],
    projection.capitalDrivers,
  );
  const dataDrivenUS = linkedBands.find((band) => band.kind === "us");
  assert.equal(dataDrivenUS?.start, epoch("2026-08-12T12:30:00Z"));
  assert.equal(dataDrivenUS?.driver, "us-data-release");
  assert.equal(dataDrivenUS?.eventId, "bls-cpi-2026-08");
});

test("keeps Asia fixed while Europe and US move one hour in Beijing time", () => {
  const summer = buildExpertSessionBandsForRange(
    epoch("2026-07-15T00:00:00Z"),
    epoch("2026-07-16T00:00:00Z"),
    null,
    [],
  );
  assert.deepEqual(
    summer.map((band) => [band.kind, band.start, band.end]),
    [
      ["asia", epoch("2026-07-15T01:00:00Z"), epoch("2026-07-15T07:00:00Z")],
      ["europe", epoch("2026-07-15T07:00:00Z"), epoch("2026-07-15T12:00:00Z")],
      ["us", epoch("2026-07-15T13:30:00Z"), epoch("2026-07-15T21:00:00Z")],
    ],
  );

  const winter = buildExpertSessionBandsForRange(
    epoch("2026-01-15T00:00:00Z"),
    epoch("2026-01-16T00:00:00Z"),
    null,
    [],
  );
  assert.deepEqual(
    winter.map((band) => [band.kind, band.start, band.end]),
    [
      ["asia", epoch("2026-01-15T01:00:00Z"), epoch("2026-01-15T07:00:00Z")],
      ["europe", epoch("2026-01-15T08:00:00Z"), epoch("2026-01-15T13:00:00Z")],
      ["us", epoch("2026-01-15T14:30:00Z"), epoch("2026-01-15T22:00:00Z")],
    ],
  );
});

test("builds Asia and US bands without spanning a holiday closure", () => {
  const values = [
    epoch("2026-04-02T01:00:00Z"),
    epoch("2026-04-02T01:01:00Z"),
    epoch("2026-04-03T13:00:00Z"),
    epoch("2026-04-06T13:00:00Z"),
    epoch("2026-04-06T13:01:00Z"),
  ];
  const bands = buildExpertSessionBands(values, null, EXPERT_HOLIDAY_CLOSURES_2026);
  assert.ok(bands.some((band) => band.kind === "asia"));
  assert.ok(bands.some((band) => band.kind === "europe"));
  assert.ok(bands.some((band) => band.kind === "us"));
  const goodFridayUS = epoch("2026-04-03T13:00:00Z");
  assert.equal(bands.some((band) => goodFridayUS >= band.start && goodFridayUS < band.end), false);
});

test("session band count depends on covered days rather than realtime sample count", () => {
  const start = epoch("2026-07-15T00:00:00Z");
  const dense = Array.from({ length: 20_001 }, (_, index) => start + index);
  const sparse = [start, dense.at(-1)!];
  assert.deepEqual(
    buildExpertSessionBands(dense, null, []),
    buildExpertSessionBands(sparse, null, []),
  );
});
