import assert from "node:assert/strict";
import test from "node:test";

import { BoundedBarPageCache, type BarPageCacheKey } from "../src/barPageCache.ts";
import type { ChartBarPage } from "../src/types.ts";

function key(before: number): BarPageCacheKey {
  return { code: "XAUUSD", sourceId: "jin10_client", periodId: "1s", before, pageSize: 500 };
}

function page(periodId = "1s"): ChartBarPage {
  return { period_id: periodId, items: [], next_before: null, has_more: false };
}

test("evicts the least recently used immutable history page", () => {
  const cache = new BoundedBarPageCache(2);
  cache.set(key(3), page());
  cache.set(key(2), page());
  assert.ok(cache.get(key(3)));

  cache.set(key(1), page());

  assert.equal(cache.size, 2);
  assert.equal(cache.get(key(2)), undefined);
  assert.ok(cache.get(key(3)));
  assert.ok(cache.get(key(1)));
});

test("keeps datasets and page sizes isolated", () => {
  const cache = new BoundedBarPageCache();
  const first = key(3);
  const other = { ...first, periodId: "1m", pageSize: 300 };
  cache.set(first, page("1s"));
  cache.set(other, page("1m"));

  assert.equal(cache.get(first)?.period_id, "1s");
  assert.equal(cache.get(other)?.period_id, "1m");
});
