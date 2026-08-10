import assert from "node:assert/strict";
import test from "node:test";

import {
  confirmedSwingPointsAt,
  priceStructureSnapshotAt,
  type PricePatternInputBar,
} from "../src/expertPricePatterns.ts";

function barsFromCloses(
  values: readonly number[],
  overrides: Readonly<Record<number, Partial<PricePatternInputBar>>> = {},
): PricePatternInputBar[] {
  return values.map((close, index) => ({
    time: 1_700_000_000 + index * 60,
    open: close,
    high: close + 0.5,
    low: close - 0.5,
    close,
    ...overrides[index],
  }));
}

function wBottomBars(): PricePatternInputBar[] {
  return barsFromCloses(
    [102, 100, 98, 97, 96, 98, 101, 104, 101, 98, 96.2, 98, 101, 104, 106],
    {
      4: { high: 96.7, low: 95 },
      10: { high: 96.9, low: 95.2 },
    },
  );
}

function mTopBars(): PricePatternInputBar[] {
  return barsFromCloses(
    [98, 100, 102, 103, 104, 102, 99, 96, 99, 102, 103.8, 102, 99, 96, 94],
    {
      4: { high: 105, low: 103.3 },
      10: { high: 104.8, low: 103.1 },
    },
  );
}

function twoBBottomBars(): PricePatternInputBar[] {
  return barsFromCloses(
    [101, 100.5, 100, 99.5, 99, 98.5, 99.3, 100, 99.8, 99.2, 97.8, 99.2],
    {
      5: { high: 99, low: 98 },
      10: { high: 98.7, low: 97.5 },
    },
  );
}

function twoBTopBars(): PricePatternInputBar[] {
  return barsFromCloses(
    [99, 99.5, 100, 100.5, 101, 101.5, 100.7, 100, 100.2, 100.8, 102.2, 100.8],
    {
      5: { high: 102, low: 101 },
      10: { high: 102.5, low: 101.3 },
    },
  );
}

test("a radius-two swing is not available until both right-side bars exist", () => {
  const bars = wBottomBars();
  assert.equal(
    confirmedSwingPointsAt(bars, 5).some((swing) => swing.index === 4),
    false,
  );
  const confirmed = confirmedSwingPointsAt(bars, 6).find((swing) => swing.index === 4);
  assert.equal(confirmed?.kind, "low");
  assert.equal(confirmed?.confirmedAtIndex, 6);
});

test("W bottom and M top appear only after a close confirms the neckline", () => {
  const bullish = wBottomBars();
  const bearish = mTopBars();

  assert.equal(
    priceStructureSnapshotAt(bullish, 13).patterns.some((item) => item.kind === "double-bottom"),
    false,
  );
  const w = priceStructureSnapshotAt(bullish, 14).patterns.find((item) => item.kind === "double-bottom");
  assert.ok(w);
  assert.equal(w.direction, "bullish");
  assert.equal(w.confirmation.index, 14);
  assert.ok((w.neckline?.price ?? Number.POSITIVE_INFINITY) < bullish[14].close);

  assert.equal(
    priceStructureSnapshotAt(bearish, 13).patterns.some((item) => item.kind === "double-top"),
    false,
  );
  const m = priceStructureSnapshotAt(bearish, 14).patterns.find((item) => item.kind === "double-top");
  assert.ok(m);
  assert.equal(m.direction, "bearish");
  assert.equal(m.confirmation.index, 14);
  assert.ok((m.neckline?.price ?? Number.NEGATIVE_INFINITY) > bearish[14].close);
});

test("2B bottom and top require a confirmed prior swing, breach, and timely close reclaim", () => {
  const bottomBars = twoBBottomBars();
  const topBars = twoBTopBars();

  assert.equal(
    priceStructureSnapshotAt(bottomBars, 10).patterns.some((item) => item.kind === "two-b-bottom"),
    false,
  );
  const bottom = priceStructureSnapshotAt(bottomBars, 11).patterns.find((item) => item.kind === "two-b-bottom");
  assert.ok(bottom);
  assert.equal(bottom.first.index, 5);
  assert.equal(bottom.second.index, 10);
  assert.equal(bottom.confirmation.index, 11);

  assert.equal(
    priceStructureSnapshotAt(topBars, 10).patterns.some((item) => item.kind === "two-b-top"),
    false,
  );
  const top = priceStructureSnapshotAt(topBars, 11).patterns.find((item) => item.kind === "two-b-top");
  assert.ok(top);
  assert.equal(top.first.index, 5);
  assert.equal(top.second.index, 10);
  assert.equal(top.confirmation.index, 11);
});

test("pattern detection at a cutoff is unchanged by unknown future bars", () => {
  const prefix = wBottomBars();
  const future = barsFromCloses([80, 120, 85]).map((bar, offset) => ({
    ...bar,
    time: prefix.at(-1)!.time + (offset + 1) * 60,
  }));

  assert.deepEqual(
    priceStructureSnapshotAt([...prefix, ...future], prefix.length - 1),
    priceStructureSnapshotAt(prefix, prefix.length - 1),
  );
});

test("a confirmed pattern remains visible with an explicit invalidated lifecycle", () => {
  const prefix = wBottomBars();
  const confirmed = priceStructureSnapshotAt(prefix, prefix.length - 1)
    .patterns.find((item) => item.kind === "double-bottom");
  assert.ok(confirmed);
  assert.equal(confirmed.status, "confirmed");

  const invalidatingBar: PricePatternInputBar = {
    time: prefix.at(-1)!.time + 60,
    open: confirmed.invalidationPrice - 0.5,
    high: confirmed.invalidationPrice,
    low: confirmed.invalidationPrice - 1,
    close: confirmed.invalidationPrice - 0.5,
  };
  const invalidated = priceStructureSnapshotAt(
    [...prefix, invalidatingBar],
    prefix.length,
  ).patterns.find((item) => item.id === confirmed.id);

  assert.ok(invalidated);
  assert.equal(invalidated.status, "invalidated");
  assert.equal(invalidated.invalidatedAt, invalidatingBar.time);
});

test("rejects W and 2B candidates that close through their invalidation before confirmation", () => {
  const brokenW = wBottomBars();
  brokenW[13] = { ...brokenW[13], open: 90, high: 96, low: 89, close: 90 };
  assert.equal(
    priceStructureSnapshotAt(brokenW, 14).patterns.some((item) => item.kind === "double-bottom"),
    false,
  );

  const brokenTwoB = barsFromCloses([
    101, 100.5, 100, 99.5, 99, 98.5, 99.3, 100, 99.8, 99.2, 97.8, 90, 100,
  ], {
    5: { high: 99, low: 98 },
    10: { high: 98.7, low: 97.5 },
    11: { open: 90, high: 95, low: 89, close: 90 },
  });
  assert.equal(
    priceStructureSnapshotAt(brokenTwoB, 12).patterns.some((item) => item.kind === "two-b-bottom"),
    false,
  );
});

test("expires W/M candidates and neutralizes opposite patterns confirmed on one Bar", () => {
  const prefix = wBottomBars().slice(0, 13);
  const delayed = [
    ...prefix,
    ...barsFromCloses(Array.from({ length: 41 }, () => 98)).map((bar, offset) => ({
      ...bar,
      time: prefix.at(-1)!.time + (offset + 1) * 60,
    })),
  ];
  delayed.push({
    time: delayed.at(-1)!.time + 60,
    open: 106,
    high: 106.5,
    low: 105.5,
    close: 106,
  });
  assert.equal(
    priceStructureSnapshotAt(delayed, delayed.length - 1).patterns.some((item) => (
      item.kind === "double-bottom" && item.first.index === 4 && item.second.index === 10
    )),
    false,
  );

  const conflict = barsFromCloses([100, 101, 102, 104, 102, 100, 98, 100, 102, 100], {
    3: { high: 104.8 },
    6: { low: 97.2 },
    9: { open: 100, high: 105.4, low: 96.6, close: 100 },
  });
  const snapshot = priceStructureSnapshotAt(conflict, 9);
  assert.deepEqual(
    snapshot.patterns
      .filter((item) => item.confirmation.index === 9 && item.status === "confirmed")
      .map((item) => item.kind)
      .sort(),
    ["two-b-bottom", "two-b-top"],
  );
  assert.equal(snapshot.latestActivePattern, null);
});
