import { compareSourceRevision } from "./quantFormat.ts";
import type { Candle, HoverCandle, TimelineSample } from "./types";

function numberOf(value: number | string): number {
  return Number(value);
}

function epochSeconds(value: string | null): number | null {
  if (!value) return null;
  const milliseconds = new Date(value).getTime();
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1000) : null;
}

export function candleAtChartTime(candles:readonly Candle[],time:number|null):Candle|null {
  if(time===null)return candles.at(-1)??null;
  let low=0,high=candles.length-1;
  while(low<=high){const middle=(low+high)>>>1;const at=epochSeconds(candles[middle].open_time);if(at===time)return candles[middle];if(at!==null&&at<time)low=middle+1;else high=middle-1;}
  return null;
}

/** Source end labels describe an interval, never an earlier publication clock. */
export function candleIntervalEvidence(candle:Candle|null):{start:number;end:number;sourceLabel:string|null}|null {
  const raw=candle?.source.raw_payload;
  if(!candle||raw?.source_label_semantics!=="interval_end")return null;
  const start=epochSeconds(candle.open_time),explicitEnd=typeof raw.bucket_end==="string"?epochSeconds(raw.bucket_end):null;
  const interval=Number(candle.interval);
  const end=explicitEnd??(start!==null&&Number.isSafeInteger(interval)&&interval>0?start+interval:null);
  if(start===null||end===null||end<=start)return null;
  const sourceLabel=typeof raw.source_label==="string"&&raw.source_label.length<=128?raw.source_label:typeof raw.source_interval_end==="string"?raw.source_interval_end:null;
  return {start,end,sourceLabel};
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
    && compareSourceRevision(left.revision, right.revision) === 0
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
  if (compareSourceRevision(incoming.revision, current.revision) !== 0) return compareSourceRevision(incoming.revision, current.revision) > 0;
  return candleStateRank[incoming.state] >= candleStateRank[current.state];
}

/** Revisions replace the complete known Bar, including lowered high/low and null volume. */
export function upsertRealtimeBar(candles:Candle[],incoming:Candle):Candle[]{return upsertRealtimeBarBatch(candles,[incoming]);}
/** Preserve loaded history; old notifications only replace an existing timestamp. */
export function upsertRealtimeBarBatch(candles:Candle[],incoming:readonly Candle[]):Candle[]{
  let next:Candle[]|null=null;
  for(const bar of incoming){const rows=next??candles;const time=epochSeconds(bar.open_time);if(time===null)continue;
    const tailTime=rows.length?epochSeconds(rows[rows.length-1].open_time):null;
    if(tailTime===null||time>tailTime){if(next===null)next=candles.slice();next.push(bar);continue;}
    let low=0,high=rows.length-1,index=-1;while(low<=high){const mid=(low+high)>>>1,at=epochSeconds(rows[mid].open_time);if(at===time){index=mid;break;}if(at!==null&&at<time)low=mid+1;else high=mid-1;}
    if(index<0||!realtimeBarCanReplace(rows[index],bar)||sameCandleVersion(rows[index],bar))continue;
    if(next===null)next=candles.slice();next[index]=bar;
  }
  return next??candles;
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
