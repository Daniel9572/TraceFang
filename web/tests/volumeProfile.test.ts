import assert from "node:assert/strict";
import test from "node:test";
import { visibleVolumeProfile } from "../src/volumeProfile.ts";
import type { Candle } from "../src/types.ts";

function candle(time: number, low: number, high: number, volume: Candle["volume"]): Candle {
  const at = new Date(time * 1000).toISOString();
  return {
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    interval: 60, open_time: at, open: low, high, low, close: high, volume,
    source: { provider: "test", provider_symbol: "XAUUSD", observed_at: at, received_at: at },
    evidence_channel_id: "test", state: "final", revision: 1, finalized_at: at,
  };
}

test("visible-range allocation conserves source volume and moves its peak with the time window", () => {
  const bars = [candle(100, 100, 132, 320), candle(160, 105, 105, 1000), candle(220, 129, 129, 2000)];
  const early = visibleVolumeProfile(bars, 100, 160);
  assert.equal(early.barCount, 2);
  assert.equal(early.totalVolume, 1320);
  assert.ok(Math.abs(early.rows.reduce((sum, row) => sum + row.volume, 0) - early.totalVolume) < 1e-8);
  assert.equal(early.pocIndex, 5);
  assert.equal(early.rows[5].volume, 1010);
  const later = visibleVolumeProfile(bars, 160, 220);
  assert.equal(later.totalVolume, 3000);
  assert.equal(later.pocIndex, 31);
  assert.ok(later.rows[later.pocIndex!].low > early.rows[early.pocIndex!].high);
  const revised = visibleVolumeProfile([{ ...bars[0], volume: 640 }, bars[1]], 100, 160);
  assert.equal(revised.totalVolume, 1640);
});

test("absent, invalid and zero volume remain distinct and never fabricate peaks", () => {
  const absent = visibleVolumeProfile([candle(100, 100, 102, null)], 100, 160);
  assert.equal(absent.volumeBarCount, 0);
  assert.equal(absent.pocIndex, null);
  assert.deepEqual(absent.rows, []);
  const zero = visibleVolumeProfile([candle(100, 100, 102, 0)], 100, 160);
  assert.equal(zero.volumeBarCount, 1);
  assert.equal(zero.totalVolume, 0);
  assert.equal(zero.pocIndex, null);
  const mixed = visibleVolumeProfile([
    candle(100, 100, 102, null), candle(110, 100, 102, ""),
    candle(120, 100, 102, -1), candle(130, 100, 102, "NaN"),
    candle(140, 100, 102, 0), candle(150, 100, 102, "20"),
  ], 100, 160);
  assert.equal(mixed.barCount, 6);
  assert.equal(mixed.volumeBarCount, 2);
  assert.equal(mixed.totalVolume, 20);
  assert.ok(Math.abs(mixed.rows.reduce((sum, row) => sum + row.volume, 0) - 20) < 1e-8);
  assert.equal(visibleVolumeProfile([candle(100, 100, 102, 20)], 200, 100).barCount, 0);
});

test("flat bars and partial price-bin overlaps retain their full volume without NaN", () => {
  const flat = visibleVolumeProfile([candle(100, 100, 100, 12)], 100, 100);
  assert.ok(flat.rows.every((row) => Number.isFinite(row.volume) && row.high > row.low));
  assert.equal(flat.rows.reduce((sum, row) => sum + row.volume, 0), 12);
  const profile = visibleVolumeProfile([candle(100, 100, 132, 0), candle(160, 100.5, 101.5, 10)], 100, 160);
  assert.ok(Math.abs(profile.rows[0].volume - 5) < 1e-8);
  assert.ok(Math.abs(profile.rows[1].volume - 5) < 1e-8);
  assert.equal(profile.rows.reduce((sum, row) => sum + row.volume, 0), 10);
});
