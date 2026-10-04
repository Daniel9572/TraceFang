import assert from "node:assert/strict";
import test from "node:test";
import {
  expirationPayoff,
  optionGreeks,
  portfolioRisk,
  templateLegs,
  validatePortfolio,
  type OptionLeg,
} from "../src/optionPortfolio.ts";

const leg = (
  kind: OptionLeg["kind"],
  quantity: number,
  strike: number,
  premium: number,
): OptionLeg => ({
  id: `${kind}${strike}`,
  kind,
  quantity,
  strike,
  premium,
  multiplier: 100,
  expiry: "2027-01-15",
});
test("call debit spread includes multiplier, fees, exact breakeven and capped risks", () => {
  const legs = [leg("call", 1, 100, 7), leg("call", -1, 110, 3)];
  const risk = portfolioRisk(legs, 10);
  assert.equal(risk.maxProfit, 590);
  assert.equal(risk.maxLoss, 410);
  assert.deepEqual(risk.breakevens, [104.1]);
  assert.equal(expirationPayoff(legs, 105, 10), 90);
});
test("short call exposes unlimited loss; short put loss is bounded at zero underlying", () => {
  assert.equal(portfolioRisk([leg("call", -1, 100, 5)]).maxLoss, Infinity);
  assert.equal(portfolioRisk([leg("call", -1, 100, 5)]).maxProfit, 500);
  assert.equal(portfolioRisk([leg("put", -1, 100, 5)]).maxLoss, 9500);
});
test("covered call uses stock share quantity and does not claim unlimited upside", () => {
  const risk = portfolioRisk([
    { ...leg("stock", 100, 0, 100), multiplier: 1 },
    leg("call", -1, 110, 3),
  ]);
  assert.equal(risk.maxProfit, 1300);
  assert.equal(risk.maxLoss, 9700);
  assert.deepEqual(risk.breakevens, [97]);
});
test("iron condor exact risk and both breakevens", () => {
  const legs = [
    leg("put", 1, 90, 1),
    leg("put", -1, 95, 3),
    leg("call", -1, 105, 3),
    leg("call", 1, 110, 1),
  ];
  const risk = portfolioRisk(legs);
  assert.equal(risk.maxProfit, 400);
  assert.equal(risk.maxLoss, 100);
  assert.deepEqual(risk.breakevens, [91, 109]);
});
test("mixed expiry, mixed underlying, missing premium and invalid inputs do not produce charts", () => {
  assert.match(
    validatePortfolio(
      [
        leg("put", 1, 100, 2),
        { ...leg("call", 1, 100, 2), expiry: "2027-02-15" },
      ],
      0,
    )!,
    /不同到期/,
  );
  assert.match(
    validatePortfolio(
      [
        { ...leg("put", 1, 100, 2), underlying: "SPY" },
        { ...leg("call", 1, 100, 2), underlying: "AAPL" },
      ],
      0,
    )!,
    /同一标的/,
  );
  assert.ok(
    validatePortfolio(templateLegs("bull-call", 100, 5, "2027-01-15"), 0),
  );
  assert.ok(validatePortfolio([leg("put", 0.5, 100, 1)], 0));
  assert.ok(
    validatePortfolio([{ ...leg("put", 1, 100, 1), expiry: "2027-02-31" }], 0),
  );
});
test("European prices and Greeks match the textbook case and put-call parity", () => {
  const call = optionGreeks("call", 100, 100, 1, 0.2, 0.05)!;
  const put = optionGreeks("put", 100, 100, 1, 0.2, 0.05)!;
  assert.ok(Math.abs(call.price - 10.4506) < 0.001);
  assert.ok(Math.abs(call.delta - 0.63683) < 0.0001);
  assert.ok(
    Math.abs(call.price - put.price - (100 - 100 * Math.exp(-0.05))) < 0.0001,
  );
  assert.equal(optionGreeks("call", 100, 100, 0, 0.2, 0.05), null);
  const futuresCall = optionGreeks("call", 100, 100, 1, 0.2, 0.05, 0, true)!;
  const futuresPut = optionGreeks("put", 100, 100, 1, 0.2, 0.05, 0, true)!;
  assert.ok(Math.abs(futuresCall.price - futuresPut.price) < 0.0001);
});
