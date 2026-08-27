import { sameCandleVersion, upsertRealtimeBar } from "./chartModel.ts";
import type { Candle } from "./types.ts";

export interface RealtimeBarDelivery {
  datasetKey: string;
  bar: Candle;
}

export type RealtimeBarListener = (delivery: RealtimeBarDelivery) => void;

interface DeliveredBarVersion {
  bar: Candle;
  openTime: number;
}

const candleStateRank: Record<Candle["state"], number> = {
  provisional_quote: 0,
  provisional_authoritative: 1,
  final: 2,
};

function epochSeconds(value: string): number | null {
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? Math.floor(milliseconds / 1_000) : null;
}

/**
 * Delivers accepted realtime Bar revisions synchronously, outside React's render
 * lifecycle. It removes only an exact transport replay or a stale revision; a
 * later revision is delivered even when its close returns to an earlier price.
 */
export class RealtimeBarStream {
  private readonly listeners = new Set<RealtimeBarListener>();
  private readonly latestByDataset = new Map<string, DeliveredBarVersion>();

  subscribe(listener: RealtimeBarListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  publish(datasetKey: string, bar: Candle): boolean {
    if (!datasetKey.trim()) return false;
    const openTime = epochSeconds(bar.open_time);
    if (openTime === null) return false;

    const previous = this.latestByDataset.get(datasetKey);
    if (previous) {
      if (openTime < previous.openTime) return false;
      if (openTime === previous.openTime) {
        if (bar.revision < previous.bar.revision) return false;
        if (
          bar.revision === previous.bar.revision
          && candleStateRank[bar.state] <= candleStateRank[previous.bar.state]
        ) return false;
        if (sameCandleVersion(previous.bar, bar)) return false;
      }
    }

    this.latestByDataset.set(datasetKey, { bar, openTime });
    const delivery = { datasetKey, bar };
    for (const listener of this.listeners) listener(delivery);
    return true;
  }

  reset(datasetKey?: string): void {
    if (datasetKey === undefined) this.latestByDataset.clear();
    else this.latestByDataset.delete(datasetKey);
  }
}

/**
 * Coalesces the React-facing tail while the transport stream keeps delivering
 * every accepted revision directly to the chart. Distinct Bar times are never
 * collapsed; revisions of one open time reduce to the newest complete Bar.
 */
export class RealtimeBarCommitBuffer {
  private pending: Candle[] = [];
  readonly maxBars: number;

  constructor(maxBars = 8) {
    if (!Number.isInteger(maxBars) || maxBars < 1) {
      throw new Error("maxBars must be a positive integer");
    }
    this.maxBars = maxBars;
  }

  get size(): number {
    return this.pending.length;
  }

  push(bar: Candle): boolean {
    this.pending = upsertRealtimeBar(this.pending, bar);
    return this.pending.length >= this.maxBars;
  }

  drain(): Candle[] {
    const bars = this.pending;
    this.pending = [];
    return bars;
  }

  clear(): void {
    this.pending = [];
  }
}

export function realtimeBarDatasetKey(
  instrumentCode: string,
  sourceId: string,
  periodId: string,
): string {
  return `${instrumentCode}:${sourceId}:${periodId}`;
}
