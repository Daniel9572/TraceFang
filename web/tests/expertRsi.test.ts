import assert from "node:assert/strict";
import test from "node:test";

import {
  createWilderRsiRuntime,
  rsiSnapshotAt,
  synchronizeWilderRsiRuntime,
} from "../src/expertRsi.ts";

function closes(values: readonly number[]): Array<{ close: number }> {
  return values.map((close) => ({ close }));
}

test("seeds Wilder RSI only after a complete period and treats a flat market as neutral", () => {
  const runtime = createWilderRsiRuntime(3);
  synchronizeWilderRsiRuntime(runtime, closes([100, 100, 100, 100]));

  assert.deepEqual(runtime.values.slice(0, 3), [null, null, null]);
  assert.equal(runtime.values[3], 50);
  assert.deepEqual(rsiSnapshotAt(runtime, 3), {
    period: 3,
    value: 50,
    state: "neutral",
    signal: "none",
  });
});

test("distinguishes an RSI extreme from the confirmed threshold recross", () => {
  const runtime = createWilderRsiRuntime(3);
  synchronizeWilderRsiRuntime(runtime, closes([
    100, 99, 98, 97, 98, 99, 100, 99,
  ]));

  assert.equal(rsiSnapshotAt(runtime, 3)?.signal, "oversold");
  assert.equal(rsiSnapshotAt(runtime, 4)?.signal, "oversold-recovery");
  assert.equal(rsiSnapshotAt(runtime, 6)?.signal, "overbought");
  assert.equal(rsiSnapshotAt(runtime, 7)?.signal, "overbought-reversal");
  assert.ok(Math.abs((rsiSnapshotAt(runtime, 4)?.value ?? 0) - 100 / 3) < 1e-10);
});

test("RSI at a cutoff is identical whether or not unknown future bars are supplied", () => {
  const prefix = closes(Array.from({ length: 40 }, (_, index) => (
    100 + Math.sin(index / 3) * 4 + index * 0.07
  )));
  const future = closes([92, 88, 105, 111]);
  const prefixRuntime = createWilderRsiRuntime();
  const fullRuntime = createWilderRsiRuntime();

  synchronizeWilderRsiRuntime(prefixRuntime, prefix);
  synchronizeWilderRsiRuntime(fullRuntime, [...prefix, ...future]);

  assert.deepEqual(
    fullRuntime.values.slice(0, prefix.length),
    prefixRuntime.values,
  );
  assert.deepEqual(
    rsiSnapshotAt(fullRuntime, prefix.length - 1),
    rsiSnapshotAt(prefixRuntime, prefix.length - 1),
  );
});

test("an in-place close correction recomputes only the dependent Wilder suffix", () => {
  const original = closes(Array.from({ length: 32 }, (_, index) => 100 + index * 0.2));
  const runtime = createWilderRsiRuntime();
  synchronizeWilderRsiRuntime(runtime, original);
  const stablePrefix = runtime.values.slice(0, 20);

  const corrected = original.map((item, index) => (
    index === 20 ? { close: item.close - 3 } : item
  ));
  synchronizeWilderRsiRuntime(runtime, corrected, 20);
  const rebuilt = createWilderRsiRuntime();
  synchronizeWilderRsiRuntime(rebuilt, corrected);

  assert.deepEqual(runtime.values.slice(0, 20), stablePrefix);
  assert.deepEqual(runtime.values, rebuilt.values);
  assert.deepEqual(runtime.averageGain, rebuilt.averageGain);
  assert.deepEqual(runtime.averageLoss, rebuilt.averageLoss);
});

test("rejects invalid periods and threshold ordering", () => {
  assert.throws(() => createWilderRsiRuntime(1), RangeError);
  const runtime = createWilderRsiRuntime(3);
  synchronizeWilderRsiRuntime(runtime, closes([100, 99, 98, 97]));
  assert.throws(() => rsiSnapshotAt(runtime, 3, 70, 30), RangeError);
});
