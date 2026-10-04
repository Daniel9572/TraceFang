import assert from "node:assert/strict";
import test from "node:test";

import { mergeCandleRows } from "../src/api.ts";
import {
  buildTimelineSeries,
  candleAtChartTime,
  candleIntervalEvidence,
  candleSeriesUpdateStart,
  classifyCandleSeriesMutation,
  formatBarCountdown,
  upsertRealtimeBar,
  upsertRealtimeBarBatch,
} from "../src/chartModel.ts";
import { chartPeriodById, secondsUntilPeriodClose } from "../src/chartPeriods.ts";
import type { Candle } from "../src/types.ts";

function candle(time: string, open: number, high: number, low: number, close: number): Candle {
  return {
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    interval: 60,
    open_time: time,
    open,
    high,
    low,
    close,
    volume: null,
    source: {
      provider: "test",
      provider_symbol: "XAUUSD",
      observed_at: time,
      received_at: time,
    },
    evidence_channel_id: "test",
    state: "final",
    revision: 1,
    finalized_at: time,
  };
}

test("source end label remains separate from the canonical minute interval",()=>{
  const row=candle("2026-09-30T06:05:00Z",1,2,0,1);
  row.source.raw_payload={source_label_semantics:"interval_end",source_label:"202609301406",source_interval_end:"2026-09-30T06:06:00Z",minute_clock_policy:"ths-v6-period61-shfe-interval-end-v2",publication_time_unknown:true};
  const start=Date.parse(row.open_time)/1000;
  assert.deepEqual(candleIntervalEvidence(row),{start,end:start+60,sourceLabel:"202609301406"});
  assert.equal(row.source.raw_payload.publication_time_unknown,true);assert.equal(row.source.observed_at,row.open_time);
  row.source.raw_payload.bucket_end="2026-09-30T06:08:00Z";
  assert.equal(candleIntervalEvidence(row)?.end,start+180);
  row.source.raw_payload.source_label_semantics="unknown";
  assert.equal(candleIntervalEvidence(row),null);
});

test("hover selects exact loaded canonical candle while an absent time has no invented source label",()=>{
  const rows=[candle("2026-09-30T06:05:00Z",1,2,0,1),candle("2026-09-30T06:06:00Z",2,3,1,2)];
  assert.strictEqual(candleAtChartTime(rows,null),rows[1]);assert.strictEqual(candleAtChartTime(rows,Date.parse(rows[0].open_time)/1000),rows[0]);assert.equal(candleAtChartTime(rows,Date.parse(rows[0].open_time)/1000+30),null);assert.equal(candleIntervalEvidence(rows[0]),null);
});

test("formats a stable exchange-style countdown", () => {
  const atThirtySeconds = Date.parse("2026-08-06T01:47:30Z");
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1m"), atThirtySeconds), 30);
  assert.equal(secondsUntilPeriodClose(chartPeriodById("1h"), atThirtySeconds), 750);
  assert.equal(formatBarCountdown(750), "00:12:30");
});

test("projects standard one-second Bars into the timeline without a second quote model", () => {
  const first = { ...candle("2026-08-06T01:47:02Z", 100, 102, 99, 101), interval: 1 };
  const second = { ...candle("2026-08-06T01:47:03Z", 101, 103, 100, 102), interval: 1 };
  const series = buildTimelineSeries([first, second]);

  assert.deepEqual(series.map((point) => point.value), [101, 102]);
  assert.deepEqual(series.map((point) => point.resolutionSeconds), [1, 1]);
});

test("compacts only the historical timeline snapshot to the newest Bar revision", () => {
  const original = { ...candle("2026-08-06T01:47:02Z", 100, 100, 100, 100), interval: 1 };
  const correction = { ...original, high: 102, close: 102, revision: 3 };
  const stale = { ...original, close: 99, revision: 2 };

  assert.deepEqual(
    buildTimelineSeries([correction, original, stale]).map((point) => point.value),
    [102],
  );
});

test("replaces the complete current Bar while preserving the immutable history prefix", () => {
  const first = candle("2026-08-06T01:46:59Z", 99, 101, 98, 100);
  const current = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const incoming = {
    ...current,
    high: 104,
    close: 103,
    revision: 2,
    source: { ...current.source, received_at: "2026-08-06T01:47:01Z" },
  };
  const previous = [first, current];
  const next = upsertRealtimeBar(previous, incoming);

  assert.equal(next.length, 2);
  assert.equal(next[0], first);
  assert.equal(next[1], incoming);
  assert.equal(classifyCandleSeriesMutation(previous, next), "tail-update");
  assert.deepEqual(buildTimelineSeries(next).map((point) => point.value), [100, 103]);
});

test("appends only a newer complete Bar and classifies it for chart update", () => {
  const current = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const incoming = candle("2026-08-06T01:48:00Z", 101, 103, 100, 102);
  const previous = [current];
  const next = upsertRealtimeBar(previous, incoming);

  assert.deepEqual(next, [current, incoming]);
  assert.equal(classifyCandleSeriesMutation(previous, next), "tail-append");
});

test("applies a coalesced realtime batch with one immutable history copy", () => {
  const first = candle("2026-08-06T01:46:00Z", 99, 101, 98, 100);
  const current = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const revision = { ...current, close: 103, revision: 2 };
  const finalRevision = { ...revision, high: 104, close: 102, revision: 3 };
  const nextBar = candle("2026-08-06T01:48:00Z", 102, 103, 101, 102.5);

  const result = upsertRealtimeBarBatch(
    [first, current],
    [revision, finalRevision, nextBar],
  );

  assert.deepEqual(result, [first, finalRevision, nextBar]);
  assert.equal(result[0], first);
});

test("ignores older timestamps and stale revisions on the realtime path", () => {
  const current = { ...candle("2026-08-06T01:47:00Z", 100, 102, 99, 101), revision: 2 };
  const older = candle("2026-08-06T01:46:00Z", 99, 100, 98, 99.5);
  const stale = { ...current, close: 90, revision: 1 };
  const bars = [current];

  assert.equal(upsertRealtimeBar(bars, older), bars);
  assert.equal(upsertRealtimeBar(bars, stale), bars);
  assert.equal(classifyCandleSeriesMutation(bars, bars), "unchanged");
});

test("classifies history prepend as a reset instead of a realtime tail update", () => {
  const current = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const older = candle("2026-08-06T01:46:00Z", 99, 100, 98, 99.5);

  assert.equal(classifyCandleSeriesMutation([current], [older, current]), "reset");
});

test("routes only realtime tail changes to the chart update API", () => {
  assert.equal(candleSeriesUpdateStart("reset", 0, 500, 2_000), null);
  assert.equal(candleSeriesUpdateStart("tail-update", 500, 500, 2_000), 499);
  assert.equal(candleSeriesUpdateStart("tail-append", 500, 501, 2_000), 500);
  assert.equal(candleSeriesUpdateStart("reset", 500, 1_000, 2_000), null);
});

test("merges backend Bar revisions without letting an older page overwrite a correction", () => {
  const original = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const corrected = {
    ...original,
    high: 104,
    close: 103,
    revision: 2,
    source: { ...original.source, received_at: "2026-08-06T01:49:00Z" },
  };

  const [result] = mergeCandleRows([corrected], [original]);

  assert.equal(result.revision, 2);
  assert.equal(result.close, 103);
});

test("merges equivalent ISO encodings of the same Bar open time", () => {
  const snapshot = candle("2026-08-06T01:47:00Z", 100, 102, 99, 101);
  const realtime = {
    ...snapshot,
    open_time: "2026-08-06T01:47:00+00:00",
    close: 103,
    revision: 2,
  };

  const result = mergeCandleRows([snapshot], [realtime]);

  assert.equal(result.length, 1);
  assert.equal(result[0], realtime);
});

test("keeps the current candle array when a refresh contains no revision", () => {
  const current = [candle("2026-08-06T01:47:00Z", 100, 102, 99, 101)];
  const unchangedRefresh = [{
    ...current[0],
    source: { ...current[0].source },
  }];

  assert.equal(mergeCandleRows(current, unchangedRefresh), current);
});

 test("replaces a loaded intermediate correction without losing history or null volume",()=>{
  const first=candle("2026-08-06T01:45:00Z",100,150,90,101),middle=candle("2026-08-06T01:46:00Z",101,160,91,102),tail=candle("2026-08-06T01:47:00Z",102,170,92,103);
  const original=[first,middle,tail], corrected={...middle,high:"110",low:"98",volume:null,revision:"10"};
  const result=upsertRealtimeBarBatch(original,[corrected]);assert.equal(result.length,3);assert.equal(result[0],first);assert.equal(result[2],tail);assert.equal(result[1],corrected);assert.equal(classifyCandleSeriesMutation(original,result),"reset");
  assert.equal(upsertRealtimeBar(result,{...middle,revision:"9"}),result);
 });
