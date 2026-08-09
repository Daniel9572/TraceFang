import type { Candle, TimelineSample } from "./types";

export interface ExpertReplayFrame {
  candles: Candle[];
  timelineSamples: TimelineSample[];
  cutoff: number | null;
  index: number;
}

function intervalSeconds(value: Candle["interval"]): number | null {
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? value : null;
  const match = /^(\d+)(s|m|h|d|w)$/i.exec(String(value).trim());
  if (!match) return null;
  const amount = Number(match[1]);
  const multiplier = match[2].toLowerCase() === "s"
    ? 1
    : match[2].toLowerCase() === "m"
      ? 60
      : match[2].toLowerCase() === "h"
        ? 60 * 60
        : match[2].toLowerCase() === "d" ? 24 * 60 * 60 : 7 * 24 * 60 * 60;
  return amount > 0 ? amount * multiplier : null;
}

/**
 * The replay cursor means "this candle is fully known", never "this candle
 * has just opened". Prefer the next candle boundary because it remains exact
 * for calendar periods and shortened sessions.
 */
export function candleReplayCutoff(candles: readonly Candle[], index: number): number | null {
  if (index < 0 || index >= candles.length) return null;
  const nextOpen = candles[index + 1] ? Date.parse(candles[index + 1].open_time) / 1_000 : null;
  if (nextOpen !== null && Number.isFinite(nextOpen)) return nextOpen;

  const candle = candles[index];
  if (candle.state !== "final") return null;
  const rawBucketEnd = candle.source.raw_payload?.bucket_end;
  if (typeof rawBucketEnd === "string") {
    const value = Date.parse(rawBucketEnd) / 1_000;
    if (Number.isFinite(value)) return value;
  }
  const open = Date.parse(candle.open_time) / 1_000;
  const duration = intervalSeconds(candle.interval);
  return Number.isFinite(open) && duration !== null ? open + duration : null;
}

export function expertReplayFrame(
  candles: readonly Candle[],
  timelineSamples: readonly TimelineSample[],
  requestedIndex: number,
): ExpertReplayFrame {
  if (candles.length === 0) {
    return { candles: [], timelineSamples: [], cutoff: null, index: -1 };
  }
  let index = Math.min(candles.length - 1, Math.floor(requestedIndex));
  while (index >= 0 && candleReplayCutoff(candles, index) === null) index -= 1;
  if (index < 0) return { candles: [], timelineSamples: [], cutoff: null, index: -1 };
  const visibleCandles = candles.slice(0, index + 1);
  const cutoff = candleReplayCutoff(candles, index)!;
  return {
    candles: visibleCandles,
    timelineSamples: timelineSamples.filter((sample) => {
      const actualTime = sample.observedTime ?? sample.time;
      return actualTime < cutoff;
    }),
    cutoff,
    index,
  };
}

export function nextReplayIndex(current: number, length: number, step = 1): number {
  if (length <= 0) return -1;
  return Math.min(length - 1, Math.max(0, current + Math.max(1, Math.floor(step))));
}

/**
 * Timeline samples are stored in observed-time order. Return the prefix that
 * was knowable strictly before the replay boundary without allocating it.
 */
export function timelineReplayCount(
  samples: readonly TimelineSample[],
  cutoff: number | null,
): number {
  if (cutoff === null || !Number.isFinite(cutoff)) return samples.length;
  let low = 0;
  let high = samples.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const actualTime = samples[middle].observedTime ?? samples[middle].time;
    if (actualTime < cutoff) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function replayIndexAtOrBefore(
  candles: readonly Candle[],
  cutoff: number | null,
): number {
  if (candles.length === 0) return -1;
  if (cutoff === null || !Number.isFinite(cutoff)) {
    let latest = candles.length - 1;
    while (latest >= 0 && candleReplayCutoff(candles, latest) === null) latest -= 1;
    return latest;
  }
  let low = 0;
  let high = candles.length - 1;
  let result = -1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const availableAt = candleReplayCutoff(candles, middle);
    if (availableAt !== null && availableAt <= cutoff) {
      result = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return result;
}
