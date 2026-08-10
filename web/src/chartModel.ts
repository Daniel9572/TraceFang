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

export function upsertRealtimeBar(candles: Candle[], incoming: Candle): Candle[] {
  const incomingTime = epochSeconds(incoming.open_time);
  if (incomingTime === null) return candles;
  const current = candles.at(-1);
  if (!current) return [incoming];
  const currentTime = epochSeconds(current.open_time);
  if (currentTime === null || incomingTime < currentTime) return candles;
  if (incomingTime > currentTime) return [...candles, incoming];
  if (!realtimeBarCanReplace(current, incoming) || sameCandleVersion(current, incoming)) {
    return candles;
  }
  return [...candles.slice(0, -1), incoming];
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

export function buildTimelineSeries(candles: Candle[]): TimelineSample[] {
  const rows: TimelineSample[] = [];
  for (const candle of candles) {
    const time = epochSeconds(candle.open_time);
    const value = numberOf(candle.close);
    if (time !== null && Number.isFinite(value)) {
      rows.push({
        time,
        observedTime: time,
        value,
        eventId: `bar:${candle.source.provider}:${candle.open_time}:${candle.revision}`,
        resolutionSeconds: Number(candle.interval),
      });
    }
  }
  return rows;
}

export function formatBarCountdown(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const seconds = safeSeconds % 60;
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, "0")).join(":");
}
