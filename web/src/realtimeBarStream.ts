import { sameCandleVersion } from "./chartModel.ts";
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

export function realtimeBarDatasetKey(
  instrumentCode: string,
  sourceId: string,
  periodId: string,
): string {
  return `${instrumentCode}:${sourceId}:${periodId}`;
}
