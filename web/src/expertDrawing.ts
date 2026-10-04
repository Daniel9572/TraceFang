import type { ExpertDrawing, ExpertDrawingPoint, ExpertDrawingSnapMode } from "./expertTypes.ts";

export const WEAK_MAGNET_TIME_DISTANCE_PX = 14;
export const WEAK_MAGNET_PRICE_DISTANCE_PX = 10;

export interface DrawingTimePoint {
  actualTime: number;
  time: number;
}

interface WeakDrawingSnapOptions {
  times: readonly DrawingTimePoint[];
  visibleLength: number;
  targetTime: number;
  pointerX: number;
  pointerY: number;
  pricesAt: (index: number) => readonly number[];
  timeToCoordinate: (time: number) => number | null;
  priceToCoordinate: (price: number) => number | null;
  viewportWidth?: number;
  viewportHeight?: number;
  maxTimeDistancePixels?: number;
  maxPriceDistancePixels?: number;
}

export interface WeakDrawingSnapResult {
  time: number;
  price: number;
  x: number;
  y: number;
}

export function nearestDrawingTimeIndex(
  values: readonly DrawingTimePoint[],
  visibleLength: number,
  targetTime: number,
): number | null {
  const length = Math.min(values.length, Math.max(0, visibleLength));
  if (length === 0 || !Number.isFinite(targetTime)) return null;

  let low = 0;
  let high = length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (values[middle].actualTime < targetTime) low = middle + 1;
    else high = middle;
  }

  if (low === 0) return 0;
  if (low === length) return length - 1;
  const right = values[low];
  const left = values[low - 1];
  return Math.abs(right.actualTime - targetTime) < Math.abs(targetTime - left.actualTime)
    ? low
    : low - 1;
}

export function weakDrawingSnap({
  times,
  visibleLength,
  targetTime,
  pointerX,
  pointerY,
  pricesAt,
  timeToCoordinate,
  priceToCoordinate,
  viewportWidth = Number.POSITIVE_INFINITY,
  viewportHeight = Number.POSITIVE_INFINITY,
  maxTimeDistancePixels = WEAK_MAGNET_TIME_DISTANCE_PX,
  maxPriceDistancePixels = WEAK_MAGNET_PRICE_DISTANCE_PX,
}: WeakDrawingSnapOptions): WeakDrawingSnapResult | null {
  const index = nearestDrawingTimeIndex(times, visibleLength, targetTime);
  if (index === null) return null;
  const candidate = times[index];
  if (targetTime < times[0].actualTime || targetTime > times[Math.min(times.length, visibleLength) - 1].actualTime) return null;
  const x = timeToCoordinate(candidate.time);
  if (x === null || !Number.isFinite(x) || x < 0 || x > viewportWidth || Math.abs(x - pointerX) > maxTimeDistancePixels) {
    return null;
  }

  let match: WeakDrawingSnapResult | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const price of pricesAt(index)) {
    if (!Number.isFinite(price)) continue;
    const y = priceToCoordinate(price);
    if (y === null || !Number.isFinite(y) || y < 0 || y > viewportHeight) continue;
    const distance = Math.abs(y - pointerY);
    if (distance <= maxPriceDistancePixels && distance < bestDistance) {
      bestDistance = distance;
      match = { time: candidate.actualTime, price, x, y };
    }
  }
  return match;
}


export function effectiveDrawingSnap(mode: ExpertDrawingSnapMode, modifier: boolean): ExpertDrawingSnapMode {
  return modifier ? (mode === "off" ? "weak" : "off") : mode;
}

export function drawingSnap(options: WeakDrawingSnapOptions, mode: ExpertDrawingSnapMode): WeakDrawingSnapResult | null {
  if (mode === "off") return null;
  return weakDrawingSnap(mode === "strong" ? {
    ...options, maxTimeDistancePixels: Number.POSITIVE_INFINITY, maxPriceDistancePixels: Number.POSITIVE_INFINITY,
  } : options);
}

export function moveDrawingAnchor(drawing: ExpertDrawing, anchor: "start" | "end" | "widthAnchor", point: ExpertDrawingPoint): ExpertDrawing {
  const next = { ...drawing, [anchor]: point };
  if (drawing.type === "horizontal") {
    next.start = { ...next.start, price: point.price };
    next.end = { ...next.end, price: point.price };
  }
  if (drawing.type === "vertical") {
    next.start = { ...next.start, time: point.time };
    next.end = { ...next.end, time: point.time };
  }
  if (drawing.type === "text") next.start = next.end = point;
  return next;
}

// Translate every anchor through the chart mapping; unavailable history cancels rather than collapsing it onto a newer bar.
export function translateDrawing(drawing: ExpertDrawing, dx: number, dy: number,
  project: (point: ExpertDrawingPoint) => { x: number; y: number } | null,
  locate: (x: number, y: number) => ExpertDrawingPoint | null): ExpertDrawing | null {
  const translate = (point: ExpertDrawingPoint) => {
    const position = project(point);
    return position ? locate(position.x + dx, position.y + dy) : null;
  };
  const start = translate(drawing.start);
  const end = translate(drawing.end);
  const widthAnchor = drawing.widthAnchor ? translate(drawing.widthAnchor) : undefined;
  if (!start || !end || (drawing.widthAnchor && !widthAnchor)) return null;
  return { ...drawing, start, end, ...(widthAnchor ? { widthAnchor } : {}) };
}

export function drawingMeasure(drawing: ExpertDrawing) {
  const change = drawing.end.price - drawing.start.price;
  return {
    change,
    percent: drawing.start.price === 0 ? null : change / drawing.start.price * 100,
    elapsedSeconds: drawing.end.time - drawing.start.time,
  };
}
