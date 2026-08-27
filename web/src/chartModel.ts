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
  const rows = candles.flatMap((candle) => {
    const time = epochSeconds(candle.open_time);
    const open = numberOf(candle.open);
    const high = numberOf(candle.high);
    const low = numberOf(candle.low);
    const close = numberOf(candle.close);
    return time !== null && [open, high, low, close].every(Number.isFinite)
      ? [{ time, open, high, low, close }]
      : [];
  });
  return rows.every((row, index) => index === 0 || rows[index - 1].time <= row.time)
    ? rows
    : rows.sort((left, right) => left.time - right.time);
}

const candleStateRank: Record<Candle["state"], number> = {
  provisional_quote: 0,
  provisional_authoritative: 1,
  final: 2,
};

export function sameCandleVersion(left: Candle, right: Candle): boolean {
  return left === right || (
    left.open_time === right.open_time
    && left.revision === right.revision
    && left.state === right.state
    && left.open === right.open
    && left.high === right.high
    && left.low === right.low
    && left.close === right.close
    && left.volume === right.volume
    && left.finalized_at === right.finalized_at
    && left.source.provider === right.source.provider
    && left.source.observed_at === right.source.observed_at
    && left.source.received_at === right.source.received_at
    && left.source.raw_payload?.bucket_end === right.source.raw_payload?.bucket_end
  );
}

function realtimeBarCanReplace(current: Candle, incoming: Candle): boolean {
  if (incoming.revision !== current.revision) return incoming.revision > current.revision;
  return candleStateRank[incoming.state] >= candleStateRank[current.state];
}

type RealtimeBarTailMutation = "append" | "replace" | null;

function realtimeBarTailMutation(
  current: Candle | undefined,
  incoming: Candle,
): RealtimeBarTailMutation {
  const incomingTime = epochSeconds(incoming.open_time);
  if (incomingTime === null) return null;
  if (!current) return "append";
  const currentTime = epochSeconds(current.open_time);
  if (currentTime === null || incomingTime < currentTime) return null;
  if (incomingTime > currentTime) return "append";
  if (!realtimeBarCanReplace(current, incoming) || sameCandleVersion(current, incoming)) {
    return null;
  }
  return "replace";
}

export function upsertRealtimeBar(candles: Candle[], incoming: Candle): Candle[] {
  const current = candles.at(-1);
  const mutation = realtimeBarTailMutation(current, incoming);
  if (mutation === "append") return [...candles, incoming];
  if (mutation === "replace") return [...candles.slice(0, -1), incoming];
  return candles;
}

/** Applies a small ordered realtime batch with at most one copy of chart history. */
export function upsertRealtimeBarBatch(candles: Candle[], incoming: readonly Candle[]): Candle[] {
  let next: Candle[] | null = null;
  for (const bar of incoming) {
    const rows = next ?? candles;
    const mutation = realtimeBarTailMutation(rows.at(-1), bar);
    if (mutation === null) continue;
    if (next === null) next = candles.slice();
    if (mutation === "append") next.push(bar);
    else next[next.length - 1] = bar;
  }
  return next ?? candles;
}

export type CandleSeriesMutation = "unchanged" | "tail-update" | "tail-append" | "reset";

export function classifyCandleSeriesMutation(
  previous: readonly Candle[] | null,
  next: readonly Candle[],
): CandleSeriesMutation {
  if (previous === next) return "unchanged";
  if (!previous) return "reset";
  if (next.length === previous.length && next.length > 0) {
    for (let index = 0; index < next.length - 1; index += 1) {
      if (next[index] !== previous[index]) return "reset";
    }
    return next[next.length - 1].open_time === previous[previous.length - 1].open_time
      ? "tail-update"
      : "reset";
  }
  if (next.length === previous.length + 1) {
    for (let index = 0; index < previous.length; index += 1) {
      if (next[index] !== previous[index]) return "reset";
    }
    return "tail-append";
  }
  return "reset";
}

export function candleSeriesUpdateStart(
  mutation: CandleSeriesMutation,
  previousDataLength: number,
  nextDataLength: number,
  maxAppendPoints: number,
): number | null {
  if (previousDataLength <= 0 || nextDataLength <= 0) return null;
  if (mutation === "tail-update" && nextDataLength === previousDataLength) {
    return nextDataLength - 1;
  }
  if (
    mutation === "tail-append"
    && nextDataLength >= previousDataLength
    && nextDataLength <= previousDataLength + maxAppendPoints
  ) {
    return previousDataLength;
  }
  return null;
}

export function timelineSampleFromCandle(candle: Candle): TimelineSample | null {
  const time = epochSeconds(candle.open_time);
  const value = numberOf(candle.close);
  if (time === null || !Number.isFinite(value)) return null;
  return {
    time,
    observedTime: time,
    value,
    eventId: `bar:${candle.source.provider}:${candle.open_time}:${candle.revision}`,
    resolutionSeconds: Number(candle.interval),
  };
}

/**
 * Builds a historical snapshot with one visible state per Bar time. Snapshot
 * compaction is deliberately separate from realtime delivery, where every
 * increasing revision must still be emitted in order.
 */
export function buildTimelineSeries(candles: Candle[]): TimelineSample[] {
  const byTime = new Map<number, { candle: Candle; sample: TimelineSample }>();
  for (const candle of candles) {
    const sample = timelineSampleFromCandle(candle);
    if (!sample) continue;
    const current = byTime.get(sample.time);
    if (!current || realtimeBarCanReplace(current.candle, candle)) {
      byTime.set(sample.time, { candle, sample });
    }
  }
  return [...byTime.values()]
    .sort((left, right) => left.sample.time - right.sample.time)
    .map(({ sample }) => sample);
}

export function formatBarCountdown(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}
