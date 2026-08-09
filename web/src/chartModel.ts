import type { Candle, HoverCandle, TimelineSample } from "./types";

function numberOf(value: number | string): number {
  return Number(value);
}

function epochSeconds(value: string | null): number | null {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null;
}

export function barsFromCandles(candles: Candle[]): HoverCandle[] {
  return candles.flatMap((candle) => {
    const time = epochSeconds(candle.open_time);
    const open = numberOf(candle.open);
    const high = numberOf(candle.high);
    const low = numberOf(candle.low);
    const close = numberOf(candle.close);
    return time !== null && [open, high, low, close].every(Number.isFinite)
      ? [{ time, open, high, low, close }]
      : [];
  }).sort((left, right) => left.time - right.time);
}

export function appendTimelineSample(
  samples: TimelineSample[],
  sample: TimelineSample,
): TimelineSample[] {
  if (!Number.isFinite(sample.time) || !Number.isFinite(sample.value)) return samples;
  if (sample.eventId && samples.some((item) => item.eventId === sample.eventId)) return samples;
  const latest = samples.at(-1);
  if (!latest || compareTimelineSamples(latest, sample) <= 0) return [...samples, sample];
  let low = 0;
  let high = samples.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (compareTimelineSamples(samples[middle], sample) <= 0) low = middle + 1;
    else high = middle;
  }
  return [...samples.slice(0, low), sample, ...samples.slice(low)];
}

function compareTimelineSamples(left: TimelineSample, right: TimelineSample): number {
  const observedDifference = (left.observedTime ?? left.time) - (right.observedTime ?? right.time);
  if (observedDifference !== 0) return observedDifference;
  const receivedDifference = left.time - right.time;
  if (receivedDifference !== 0) return receivedDifference;
  return (left.eventId ?? "").localeCompare(right.eventId ?? "");
}

export function mergeTimelineSamples(...pages: readonly TimelineSample[][]): TimelineSample[] {
  const rows: TimelineSample[] = [];
  const eventIds = new Set<string>();
  for (const page of pages) {
    for (const sample of page) {
      if (!Number.isFinite(sample.time) || !Number.isFinite(sample.value)) continue;
      if (sample.eventId && eventIds.has(sample.eventId)) continue;
      if (sample.eventId) eventIds.add(sample.eventId);
      rows.push(sample);
    }
  }
  return rows.sort(compareTimelineSamples);
}

export function buildTimelineSeries(
  candles: Candle[],
  samples: TimelineSample[],
  livePrice: number | null,
  observedAt: string | null,
  nowMilliseconds = Date.now(),
): TimelineSample[] {
  const candleRows: TimelineSample[] = [];
  const rawSampleMinutes = new Set(
    samples
      .map((sample) => Math.floor((sample.observedTime ?? sample.time) / 60))
      .filter(Number.isFinite),
  );
  for (const candle of candles) {
    const time = epochSeconds(candle.open_time);
    const value = numberOf(candle.close);
    if (
      time !== null
      && Number.isFinite(value)
      && !rawSampleMinutes.has(Math.floor(time / 60))
    ) {
      candleRows.push({
        time,
        observedTime: time,
        value,
        eventId: `bar:${candle.source.provider}:${candle.open_time}:${candle.revision}`,
      });
    }
  }
  const snapshotRows: TimelineSample[] = [];
  if (livePrice !== null && Number.isFinite(livePrice)) {
    const observed = epochSeconds(observedAt) ?? Math.floor(nowMilliseconds / 1000);
    const latest = samples.at(-1) ?? candleRows.at(-1);
    if (
      !latest
      || latest.value !== livePrice
      || (latest.observedTime ?? latest.time) !== observed
    ) {
      snapshotRows.push({
        time: observed,
        observedTime: observed,
        value: livePrice,
        eventId: `snapshot:${observed}:${livePrice}`,
      });
    }
  }
  return mergeTimelineSamples(candleRows, samples, snapshotRows);
}

export function formatBarCountdown(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}
