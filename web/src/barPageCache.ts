import type { ChartBarPage } from "./types.ts";

export interface BarPageCacheKey {
  code: string;
  sourceId: string;
  periodId: string;
  boundary: string;
  pageSize: number;
}

function keyOf(value: BarPageCacheKey): string {
  return [
    value.sourceId,
    value.code,
    value.periodId,
    value.boundary,
    value.pageSize,
  ].join(":");
}

/** A small LRU for immutable cursor pages; the moving realtime tail is never cached. */
export class BoundedBarPageCache {
  private readonly pages = new Map<string, ChartBarPage>();
  readonly maxPages: number;

  constructor(maxPages = 24) {
    if (!Number.isInteger(maxPages) || maxPages < 1) {
      throw new Error("maxPages must be a positive integer");
    }
    this.maxPages = maxPages;
  }

  get size(): number {
    return this.pages.size;
  }

  get(key: BarPageCacheKey): ChartBarPage | undefined {
    const cacheKey = keyOf(key);
    const page = this.pages.get(cacheKey);
    if (!page) return undefined;
    this.pages.delete(cacheKey);
    this.pages.set(cacheKey, page);
    return page;
  }

  set(key: BarPageCacheKey, page: ChartBarPage): void {
    const cacheKey = keyOf(key);
    this.pages.delete(cacheKey);
    this.pages.set(cacheKey, page);
    while (this.pages.size > this.maxPages) {
      const oldest = this.pages.keys().next().value as string | undefined;
      if (oldest === undefined) break;
      this.pages.delete(oldest);
    }
  }

  clear(): void {
    this.pages.clear();
  }
}
