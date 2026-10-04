import { RealtimeBarStream } from "../src/realtimeBarStream";
import type { Candle } from "../src/types";
export const stream = new RealtimeBarStream();
export const candles: Candle[] = Array.from({ length: 400 }, (_, index) => {
  const date = new Date(Date.UTC(2026, 9, 1, 0, index)).toISOString();
  const price = 2400 + Math.sin(index / 12) * 20 + index / 20;
  return { instrument: { symbol: "TEST-A", asset_class: "test", base: null, quote: null, venue: "fixture" }, interval: 60,
    open_time: date, open: price - 2, high: price + 5, low: price - 4, close: price,
    volume: null, source: { provider: "test-fixture", provider_symbol: "TEST-A", observed_at: date, received_at: date },
    evidence_channel_id: "test-fixture", state: "final", revision: 1, finalized_at: date };
});
