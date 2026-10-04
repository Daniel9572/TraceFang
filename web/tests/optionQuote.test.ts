import { test } from "node:test";
import assert from "node:assert/strict";
import { optionSourceText, optionScenarioNumber, positiveOptionSource, optionDayPriceText, optionDayClockText } from "../src/optionQuote.ts";
test("source option decimals display every digit; only the explicit model boundary approximates", () => {
  for (const text of ["9007199254740993.0000000000000000000000000001", "0.0000000000000000000000000001", "0", "2.7000"]) assert.equal(optionSourceText(text), text);
  assert.equal(optionSourceText(null), "—");
  assert.equal(positiveOptionSource("1"+"0".repeat(1000)),true);
  assert.equal(positiveOptionSource("0.0000"),false);
  assert.equal(optionScenarioNumber(null), null);
  assert.equal(optionScenarioNumber(""), null);
  assert.equal(optionScenarioNumber("NaN"), null);
  assert.equal(optionScenarioNumber("1e309"), null);
  assert.equal(optionScenarioNumber("0"), 0);
  assert.equal(optionScenarioNumber("2.7000"), 2.7);
  assert.notEqual(String(optionScenarioNumber("9007199254740993")), "9007199254740993", "model approximation cannot be returned as source price");
});
test("official daily close and settlement stay separate, including zero close and unknown trade clock",()=>{
  const row={price_semantics:"official_daily_close" as const,daily_close:"0",settlement:"9007199254740993.0000000000000000000000000001",last:null,source_date:"2026-09-30",source_received_at:"2026-10-04T04:00:00Z"};
  assert.equal(optionDayPriceText(row),"日收盘 0 / 日结算 9007199254740993.0000000000000000000000000001");
  assert.equal(optionDayClockText(row),"历史报告日期 2026-09-30（日精度） · 来源获取 2026-10-04T04:00:00Z；不是成交时刻");
  assert.equal(positiveOptionSource(row.last),false);
  assert.equal(optionDayPriceText({last:"0.0000000000000000000000000001"}),"最后 0.0000000000000000000000000001");
  assert.equal(optionDayClockText({last:"1"}),null);
});
