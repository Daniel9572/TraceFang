import type { Candle, HoverCandle, TimelineSample } from "./types";
import { periodBucketSeconds, type ChartPeriod } from "./chartPeriods.ts";

function numberOf(value: number | string): number {
  return Number(value);
}

function epochSeconds(value: string | null): number | null {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null;
}

export function aggregateCandles(candles: Candle[], period: ChartPeriod): HoverCandle[] {
  const rows = new Map<number, HoverCandle>();

  for (const candle of candles) {
    const epoch = epochSeconds(candle.open_time);
    if (epoch === null) continue;

    const bucket = periodBucketSeconds(period, epoch);
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
  period: ChartPeriod,
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): HoverCandle[] {
  if (livePrice === null || !Number.isFinite(livePrice)) return bars;

  const result = bars.map((bar) => ({ ...bar }));
  const observed = epochSeconds(observedAt) ?? Math.floor(nowMilliseconds / 1000);
  const bucket = periodBucketSeconds(period, observed);
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
  period: ChartPeriod,
  samples: TimelineSample[],
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): HoverCandle[] {
  const withSamples = mergeLiveSamples(aggregateCandles(candles, period), period, samples);
  const latestSample = samples.at(-1);
  if (latestSample && latestSample.value === livePrice) return withSamples;
  return mergeLivePrice(withSamples, period, livePrice, observedAt, nowMilliseconds);
}

export function mergeLiveSamples(
  bars: HoverCandle[],
  period: ChartPeriod,
  samples: TimelineSample[],
): HoverCandle[] {
  const result = bars.map((bar) => ({ ...bar }));
  for (const sample of samples) {
    if (!Number.isFinite(sample.value)) continue;
    const observed = Math.floor(sample.observedTime ?? sample.time);
    if (!Number.isFinite(observed)) continue;
    const bucket = periodBucketSeconds(period, observed);
    const latest = result.at(-1);
    if (!latest) {
      result.push({
        time: bucket,
        open: sample.value,
        high: sample.value,
        low: sample.value,
        close: sample.value,
      });
      continue;
    }
    if (bucket < latest.time) continue;
    if (bucket === latest.time) {
      latest.high = Math.max(latest.high, sample.value);
      latest.low = Math.min(latest.low, sample.value);
      latest.close = sample.value;
      continue;
    }
    const open = latest.close;
    result.push({
      time: bucket,
      open,
      high: Math.max(open, sample.value),
      low: Math.min(open, sample.value),
      close: sample.value,
    });
  }
  return result;
}

export function appendTimelineSample(
  samples: TimelineSample[],
  sample: TimelineSample,
  maxSamples = 20_000,
): TimelineSample[] {
  if (!Number.isFinite(sample.time) || !Number.isFinite(sample.value)) return samples;
  const latest = samples.at(-1);
  if (
    sample.eventId
    && samples.slice(-32).some((item) => item.eventId === sample.eventId)
  ) return samples;
  const normalized = latest && sample.time <= latest.time
    ? { ...sample, time: latest.time + 0.001 }
    : sample;
  const next = [...samples, normalized];
  return next.length > maxSamples ? next.slice(next.length - maxSamples) : next;
}

export function buildTimelineSeries(
  candles: Candle[],
  samples: TimelineSample[],
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): TimelineSample[] {
  const rows = new Map<number, TimelineSample>();
  for (const candle of candles) {
    const time = epochSeconds(candle.open_time);
    const value = numberOf(candle.close);
    if (time !== null && Number.isFinite(value)) rows.set(time, { time, value });
  }
  for (const sample of samples) {
    if (Number.isFinite(sample.time) && Number.isFinite(sample.value)) rows.set(sample.time, sample);
  }
  if (livePrice !== null && Number.isFinite(livePrice)) {
    const latest = samples.at(-1);
    if (!latest || latest.value !== livePrice) {
      const observed = epochSeconds(observedAt) ?? Math.floor(nowMilliseconds / 1000);
      const time = latest ? Math.max(observed, latest.time + 0.001) : observed;
      rows.set(time, { time, observedTime: observed, value: livePrice });
    }
  }
  return [...rows.values()].sort((left, right) => left.time - right.time);
}

export function formatBarCountdown(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}
