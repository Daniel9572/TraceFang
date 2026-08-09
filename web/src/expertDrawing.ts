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
  maxTimeDistancePixels = WEAK_MAGNET_TIME_DISTANCE_PX,
  maxPriceDistancePixels = WEAK_MAGNET_PRICE_DISTANCE_PX,
}: WeakDrawingSnapOptions): WeakDrawingSnapResult | null {
  const index = nearestDrawingTimeIndex(times, visibleLength, targetTime);
  if (index === null) return null;
  const candidate = times[index];
  const x = timeToCoordinate(candidate.time);
  if (x === null || !Number.isFinite(x) || Math.abs(x - pointerX) > maxTimeDistancePixels) {
    return null;
  }

  let match: WeakDrawingSnapResult | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const price of pricesAt(index)) {
    if (!Number.isFinite(price)) continue;
    const y = priceToCoordinate(price);
    if (y === null || !Number.isFinite(y)) continue;
    const distance = Math.abs(y - pointerY);
    if (distance <= maxPriceDistancePixels && distance < bestDistance) {
      bestDistance = distance;
      match = { time: candidate.actualTime, price, x, y };
    }
  }
  return match;
}
