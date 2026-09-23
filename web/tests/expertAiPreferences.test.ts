import assert from "node:assert/strict";
import test from "node:test";
import { resolveAiPreferences, reasoningEffortLabel } from "../src/expertAiPreferences.ts";

const models = [
  { model: "fast", display_name: "Fast", reasoning_efforts: ["low", "high"], default_reasoning_effort: "low", is_default: false },
  { model: "deep", display_name: "Deep", reasoning_efforts: ["medium", "high", "ultra"], default_reasoning_effort: "medium", is_default: true },
];

test("uses the catalog default, not a hardcoded model", () => {
  assert.deepEqual(resolveAiPreferences(models, {}), { model: "deep", reasoning_effort: "medium" });
});
test("preserves valid saved model and effort", () => {
  assert.deepEqual(resolveAiPreferences(models, { model: "fast", reasoning_effort: "high" }), { model: "fast", reasoning_effort: "high" });
});
test("switching models resets to that model's supported default", () => {
  assert.deepEqual(resolveAiPreferences(models, { model: "fast", reasoning_effort: "ultra" }), { model: "fast", reasoning_effort: "low" });
});
test("handles removed models and empty catalogs", () => {
  assert.deepEqual(resolveAiPreferences(models, { model: "removed" }), { model: "deep", reasoning_effort: "medium" });
  assert.deepEqual(resolveAiPreferences([], {}), { model: "", reasoning_effort: "" });
});
test("unknown future effort values remain visible", () => {
  assert.equal(reasoningEffortLabel("high"), "高");
  assert.equal(reasoningEffortLabel("future"), "future");
});
