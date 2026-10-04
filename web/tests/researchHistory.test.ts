import assert from "node:assert/strict";
import test from "node:test";
import { canRequestResearchHistory, resolveResearchHistoryStep } from "../src/historyLoading.ts";

test("research cursor remains loadable past ten thousand displayed rows", () => {
  for (const count of [9999, 10000, 10001, 40000]) {
    const page = { next_before: "2020-01-01T00:00:00Z", items: new Array(count) };
    assert.equal(canRequestResearchHistory(page, false), true);
    assert.equal(canRequestResearchHistory(page, true), false);
  }
  assert.equal(canRequestResearchHistory({ next_before: null }, false), false);
});
test("empty advancing pages retain provider coverage cursor", () => {
  assert.equal(resolveResearchHistoryStep("2021-01-01", "2020-01-01", 0).state, "advanced");
  assert.equal(resolveResearchHistoryStep("2021-01-01", null, 0).state, "exhausted");
  assert.equal(resolveResearchHistoryStep("2021-01-01", null, 300).state, "loaded");
  assert.throws(() => resolveResearchHistoryStep("2021-01-01", "2021-01-01", 0), /游标没有向前推进/);
});
