import assert from "node:assert/strict";
import test from "node:test";

import { nearestDrawingTimeIndex, weakDrawingSnap } from "../src/expertDrawing.ts";

const times = [
  { actualTime: 100, time: 10 },
  { actualTime: 200, time: 20 },
  { actualTime: 300, time: 30 },
];

test("finds the nearest visible drawing time without scanning history", () => {
  assert.equal(nearestDrawingTimeIndex(times, times.length, 240), 1);
  assert.equal(nearestDrawingTimeIndex(times, times.length, 260), 2);
  assert.equal(nearestDrawingTimeIndex(times, 2, 290), 1);
  assert.equal(nearestDrawingTimeIndex(times, 0, 200), null);
});

test("weak magnet snaps to the nearest OHLC coordinate inside both thresholds", () => {
  const result = weakDrawingSnap({
    times,
    visibleLength: times.length,
    targetTime: 205,
    pointerX: 101,
    pointerY: 52,
    pricesAt: (index) => index === 1 ? [100, 105, 95, 102] : [],
    timeToCoordinate: (time) => time * 5,
    priceToCoordinate: (price) => 150 - price,
  });

  assert.deepEqual(result, { time: 200, price: 100, x: 100, y: 50 });
});

test("weak magnet preserves a free anchor when time or price is not close", () => {
  const base = {
    times,
    visibleLength: times.length,
    targetTime: 205,
    pricesAt: () => [100],
    timeToCoordinate: (time: number) => time * 5,
    priceToCoordinate: (price: number) => 150 - price,
  };

  assert.equal(weakDrawingSnap({ ...base, pointerX: 130, pointerY: 50 }), null);
  assert.equal(weakDrawingSnap({ ...base, pointerX: 100, pointerY: 75 }), null);
});

test("weak magnet supports a single exact timeline price", () => {
  const result = weakDrawingSnap({
    times,
    visibleLength: times.length,
    targetTime: 298,
    pointerX: 149,
    pointerY: 77,
    pricesAt: (index) => index === 2 ? [73] : [],
    timeToCoordinate: (time) => time * 5,
    priceToCoordinate: (price) => 150 - price,
  });

  assert.deepEqual(result, { time: 300, price: 73, x: 150, y: 77 });
});
