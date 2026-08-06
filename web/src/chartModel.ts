import type { Candle, HoverCandle } from "./types";

function numberOf(value: number | string): number {
  return Number(value);
}

function epochSeconds(value: string | null): number | null {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null;
}

export function aggregateCandles(candles: Candle[], intervalMinutes: number): HoverCandle[] {
  const bucketSeconds = intervalMinutes * 60;
  const rows = new Map<number, HoverCandle>();

  for (const candle of candles) {
    const epoch = epochSeconds(candle.open_time);
    if (epoch === null) continue;

    const bucket = Math.floor(epoch / bucketSeconds) * bucketSeconds;
    const open = numberOf(candle.open);
    const high = numberOf(candle.high);
    const low = numberOf(candle.low);
    const close = numberOf(candle.close);
    if (![open, high, low, close].every(Number.isFinite)) continue;

    const current = rows.get(bucket);
    if (!current) {
      rows.set(bucket, { time: bucket, open, high, low, close });
      continue;
    }

    current.high = Math.max(current.high, high);
    current.low = Math.min(current.low, low);
    current.close = close;
  }

  return [...rows.values()].sort((left, right) => left.time - right.time);
}

export function mergeLivePrice(
  bars: HoverCandle[],
  intervalMinutes: number,
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): HoverCandle[] {
  if (livePrice === null || !Number.isFinite(livePrice)) return bars;

  const result = bars.map((bar) => ({ ...bar }));
  const bucketSeconds = intervalMinutes * 60;
  const observed = epochSeconds(observedAt) ?? Math.floor(nowMilliseconds / 1000);
  const bucket = Math.floor(observed / bucketSeconds) * bucketSeconds;
  const latest = result.at(-1);

  if (!latest) {
    return [{ time: bucket, open: livePrice, high: livePrice, low: livePrice, close: livePrice }];
  }

  if (bucket < latest.time) return result;

  if (bucket === latest.time) {
    latest.high = Math.max(latest.high, livePrice);
    latest.low = Math.min(latest.low, livePrice);
    latest.close = livePrice;
    return result;
  }

  const open = latest.close;
  result.push({
    time: bucket,
    open,
    high: Math.max(open, livePrice),
    low: Math.min(open, livePrice),
    close: livePrice,
  });
  return result;
}

export function buildChartBars(
  candles: Candle[],
  intervalMinutes: number,
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): HoverCandle[] {
  return mergeLivePrice(
    aggregateCandles(candles, intervalMinutes),
    intervalMinutes,
    livePrice,
    observedAt,
    nowMilliseconds,
  );
}

export function secondsUntilBarClose(intervalMinutes: number, nowMilliseconds = Date.now()): number {
  const intervalSeconds = intervalMinutes * 60;
  const nowSeconds = Math.floor(nowMilliseconds / 1000);
  const elapsed = nowSeconds % intervalSeconds;
  return elapsed === 0 ? intervalSeconds : intervalSeconds - elapsed;
}

export function formatBarCountdown(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}
