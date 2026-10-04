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

test("strong magnet ignores weak distance limits but rejects offscreen, missing and future data", async () => {
  const { drawingSnap, effectiveDrawingSnap } = await import("../src/expertDrawing.ts");
  const options = { times, visibleLength: 2, targetTime: 190, pointerX: 85, pointerY: 80,
    pricesAt: () => [NaN, 100], timeToCoordinate: (time: number) => time * 5, priceToCoordinate: (price: number) => 150 - price,
    viewportWidth: 120, viewportHeight: 100 };
  assert.equal(drawingSnap(options, "weak"), null);
  assert.deepEqual(drawingSnap(options, "strong"), { time: 200, price: 100, x: 100, y: 50 });
  assert.equal(drawingSnap({ ...options, targetTime: 290 }, "strong"), null);
  assert.equal(drawingSnap({ ...options, viewportWidth: 90 }, "strong"), null);
  assert.equal(drawingSnap({ ...options, pricesAt: () => [NaN] }, "strong"), null);
  assert.equal(effectiveDrawingSnap("strong", true), "off");
  assert.equal(effectiveDrawingSnap("off", true), "weak");
});

test("anchor editing keeps one-point tools coherent and the channel width independent", async () => {
  const { moveDrawingAnchor } = await import("../src/expertDrawing.ts");
  const drawing = { id: "test", type: "channel" as const, start: { time: 100, price: 100 }, end: { time: 200, price: 120 },
    widthAnchor: { time: 150, price: 130 }, color: "#245c8c", label: "test" };
  const point = { time: 170, price: 145 };
  assert.deepEqual(moveDrawingAnchor(drawing, "widthAnchor", point), { ...drawing, widthAnchor: point });
  const horizontal = moveDrawingAnchor({ ...drawing, type: "horizontal" }, "start", point);
  assert.equal(horizontal.start.price, horizontal.end.price);
  const vertical = moveDrawingAnchor({ ...drawing, type: "vertical" }, "start", point);
  assert.equal(vertical.start.time, vertical.end.time);
});

test("whole-object movement translates every anchor through the chart and aborts on absent historical anchors", async () => {
  const { translateDrawing, drawingMeasure } = await import("../src/expertDrawing.ts");
  const drawing = { id: "test", type: "channel" as const, start: { time: 100, price: 100 }, end: { time: 200, price: 120 },
    widthAnchor: { time: 150, price: 130 }, color: "#245c8c", label: "test" };
  const project = (point: { time: number; price: number }) => ({ x: point.time / 2, y: 200 - point.price });
  const locate = (x: number, y: number) => ({ time: x * 2, price: 200 - y });
  const moved = translateDrawing(drawing, 10, -5, project, locate)!;
  assert.deepEqual(moved.start, { time: 120, price: 105 });
  assert.deepEqual(moved.end, { time: 220, price: 125 });
  assert.deepEqual(moved.widthAnchor, { time: 170, price: 135 });
  assert.equal(translateDrawing(drawing, 10, 5, (point) => point.time === 100 ? null : project(point), locate), null);
  assert.deepEqual(drawingMeasure({ ...drawing, end: { time: 864100, price: 110 } }), { change: 10, percent: 10, elapsedSeconds: 864000 });
  assert.equal(drawingMeasure({ ...drawing, start: { time: 100, price: 0 } }).percent, null);
});
