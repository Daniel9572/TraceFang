import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_EXPERT_STRATEGIES,
  EXPERT_STRATEGIES,
  strategyById,
} from "../src/strategyCatalog.ts";

test("publishes one complete research dossier for every expert strategy", () => {
  assert.equal(EXPERT_STRATEGIES.length, 17);
  assert.equal(new Set(EXPERT_STRATEGIES.map((strategy) => strategy.id)).size, 17);

  for (const strategy of EXPERT_STRATEGIES) {
    assert.equal(strategyById(strategy.id), strategy);
    assert.ok(strategy.name.length > 0);
    assert.ok(strategy.description.length > 0);
    assert.ok(strategy.dataSource.length > 0);
    assert.ok(strategy.details.principle.length > 0);
    assert.ok(strategy.details.horizon.length > 0);
    assert.ok(strategy.details.validation.length > 0);
    assert.ok(strategy.details.version.length > 0);
    for (const values of [
      strategy.details.formula,
      strategy.details.parameters,
      strategy.details.signalRules,
      strategy.details.requiredFields,
      strategy.details.suitableRegimes,
      strategy.details.boundaryConditions,
      strategy.details.invalidation,
      strategy.details.references,
    ]) {
      assert.ok(values.length > 0, `${strategy.id} has an empty dossier section`);
    }
    for (const reference of strategy.details.references) {
      assert.doesNotThrow(() => new URL(reference.url));
      assert.ok(reference.title.length > 0);
      assert.ok(reference.publisher.length > 0);
      assert.ok(reference.note.length > 0);
    }
  }
});

test("keeps visual, proxy, and context-only strategies out of directional scoring", () => {
  for (const id of [
    "nine-count",
    "multi-timeframe",
    "auto-trend",
    "smart-money",
    "vix-gvz",
    "volume-open-interest",
  ] as const) {
    const details = strategyById(id).details;
    assert.equal(details.compositeEligible, false);
    assert.equal(details.backtestEligible, false);
  }
  assert.equal(strategyById("ma-structure").details.compositeEligible, true);
  assert.equal(strategyById("momentum-ensemble").details.backtestEligible, true);
  assert.equal(strategyById("rsi").details.backtestEligible, true);
  assert.ok(DEFAULT_EXPERT_STRATEGIES.includes("ma-structure"));
  assert.ok(DEFAULT_EXPERT_STRATEGIES.includes("rsi"));
  assert.ok(DEFAULT_EXPERT_STRATEGIES.includes("multi-timeframe"));
  assert.ok(DEFAULT_EXPERT_STRATEGIES.includes("smart-money"));
  assert.ok(DEFAULT_EXPERT_STRATEGIES.includes("auto-trend"));
});

test("documents opportunity and proxy boundaries without overstating causality", () => {
  const multiTimeframe = strategyById("multi-timeframe");
  const smartMoney = strategyById("smart-money");

  assert.match(multiTimeframe.details.principle, /候选/);
  assert.match(multiTimeframe.details.principle, /不能.*入场保证/);
  assert.equal(multiTimeframe.details.compositeEligible, false);
  assert.equal(multiTimeframe.details.backtestEligible, false);

  assert.equal(smartMoney.evidenceMode, "proxy");
  assert.match(smartMoney.details.principle, /不识别.*机构/);
  assert.match(smartMoney.details.boundaryConditions.join(" "), /OHLC.*不含订单身份/);
  assert.equal(smartMoney.details.compositeEligible, false);
  assert.equal(smartMoney.details.backtestEligible, false);
});
