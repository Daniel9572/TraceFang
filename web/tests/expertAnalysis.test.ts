import assert from "node:assert/strict";
import test from "node:test";

import {
  buildExpertAnalysis,
  buildExpertAnalysisAt,
  buildExpertIndicatorSeriesAt,
  clearExpertIndicatorHistory,
  createExpertBacktestRunner,
  DEFAULT_EXPERT_STRATEGIES,
  expertIndicatorHistoryStats,
  EXPERT_STRATEGIES,
  runExpertBacktest,
} from "../src/expertAnalysis.ts";
import type { Candle } from "../src/types.ts";

function candle(index: number, close: number, volume: number | null = 100 + index): Candle {
  const open = close - 0.7;
  return {
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    interval: 60,
    open_time: new Date(Date.UTC(2026, 7, 7, 0, index)).toISOString(),
    open,
    high: close + 0.6,
    low: open - 0.4,
    close,
    volume,
    source: { provider: "test", provider_symbol: "XAUUSD", observed_at: new Date().toISOString(), received_at: new Date().toISOString() },
    evidence_channel_id: "test",
    state: "final",
    revision: 1,
    finalized_at: new Date().toISOString(),
  };
}

test("declares strategy data sources separately from calculation evidence", () => {
  const byId = new Map(EXPERT_STRATEGIES.map((strategy) => [strategy.id, strategy]));
  assert.equal(byId.get("macd")?.dataSource, "收盘价");
  assert.equal(byId.get("macd")?.evidenceMode, "native");
  assert.equal(byId.get("poc-proxy")?.dataSource, "OHLC + 可用总量");
  assert.equal(byId.get("poc-proxy")?.evidenceMode, "proxy");
  assert.equal(byId.get("volume-price")?.dataSource, "OHLC + 成交量");
  assert.equal(byId.get("volume-price")?.evidenceMode, "conditional");
});

test("reuses preset indicator history across array replacement and strategy openings", () => {
  const historyKey = "test:XAUUSD:jin10_client:15m";
  clearExpertIndicatorHistory(historyKey);
  const candles = Array.from({ length: 120 }, (_, index) => candle(index, 2400 + index * 0.2));
  buildExpertAnalysisAt(candles, ["macd"], candles.length - 1, historyKey);
  const firstStats = expertIndicatorHistoryStats(historyKey);
  assert.ok(firstStats);

  const sameHistoryWithNewObjects = candles.map((item) => ({
    ...item,
    source: { ...item.source },
  }));
  buildExpertAnalysisAt(
    sameHistoryWithNewObjects,
    ["kdj", "fair-value"],
    sameHistoryWithNewObjects.length - 1,
    historyKey,
  );
  const reusedStats = expertIndicatorHistoryStats(historyKey);
  assert.equal(reusedStats?.seriesPointCalculations, firstStats.seriesPointCalculations);
  assert.equal(reusedStats?.snapshotCalculations, firstStats.snapshotCalculations);

  const firstRunner = createExpertBacktestRunner(candles, ["macd", "kdj"], historyKey);
  while (!firstRunner.done) firstRunner.advance(256);
  const reusedRunner = createExpertBacktestRunner(
    sameHistoryWithNewObjects,
    ["kdj", "macd"],
    historyKey,
  );
  assert.strictEqual(reusedRunner, firstRunner);
  assert.equal(expertIndicatorHistoryStats(historyKey)?.backtestVariants, 1);
});

test("reuses complete indicator history when a period reopens with its recent page", () => {
  const historyKey = "test:XAUUSD:jin10_client:4h:reopen";
  clearExpertIndicatorHistory(historyKey);
  const completeHistory = Array.from(
    { length: 800 },
    (_, index) => candle(index, 2400 + Math.sin(index / 12) * 4),
  );
  const completeAnalysis = buildExpertAnalysis(
    completeHistory,
    DEFAULT_EXPERT_STRATEGIES,
    historyKey,
  );
  const completeStats = expertIndicatorHistoryStats(historyKey);
  const recentPage = completeHistory.slice(-120).map((item) => ({
    ...item,
    source: { ...item.source },
  }));
  const reopenedAnalysis = buildExpertAnalysis(
    recentPage,
    DEFAULT_EXPERT_STRATEGIES,
    historyKey,
  );
  const reopenedStats = expertIndicatorHistoryStats(historyKey);

  assert.deepEqual(reopenedAnalysis, completeAnalysis);
  assert.equal(reopenedStats?.barCount, completeHistory.length);
  assert.equal(reopenedStats?.seriesPointCalculations, completeStats?.seriesPointCalculations);
  assert.equal(reopenedStats?.snapshotCalculations, completeStats?.snapshotCalculations);
});

test("rebinds an older recent-page reference after complete history is prepended", () => {
  const historyKey = "test:XAUUSD:jin10_client:30m:prepend";
  clearExpertIndicatorHistory(historyKey);
  const completeHistory = Array.from(
    { length: 360 },
    (_, index) => candle(index, 2400 + Math.cos(index / 9) * 3),
  );
  const originalRecentPage = completeHistory.slice(-80);
  buildExpertAnalysis(originalRecentPage, DEFAULT_EXPERT_STRATEGIES, historyKey);
  const completeAnalysis = buildExpertAnalysis(
    completeHistory,
    DEFAULT_EXPERT_STRATEGIES,
    historyKey,
  );
  const completeStats = expertIndicatorHistoryStats(historyKey);
  const reboundAnalysis = buildExpertAnalysis(
    originalRecentPage,
    DEFAULT_EXPERT_STRATEGIES,
    historyKey,
  );
  const reboundStats = expertIndicatorHistoryStats(historyKey);

  assert.deepEqual(reboundAnalysis, completeAnalysis);
  assert.equal(reboundStats?.seriesPointCalculations, completeStats?.seriesPointCalculations);
  assert.equal(reboundStats?.snapshotCalculations, completeStats?.snapshotCalculations);
});

test("extends and repairs indicator history only from the earliest changed bar", () => {
  const historyKey = "test:XAUUSD:jin10_client:1m:incremental";
  clearExpertIndicatorHistory(historyKey);
  const initial = Array.from({ length: 90 }, (_, index) => candle(index, 2400 + index * 0.2));
  buildExpertAnalysis(initial, DEFAULT_EXPERT_STRATEGIES, historyKey);
  const initialStats = expertIndicatorHistoryStats(historyKey);
  assert.equal(initialStats?.seriesPointCalculations, 90);

  const extended = [...initial, candle(90, 2418.5)];
  buildExpertAnalysis(extended, DEFAULT_EXPERT_STRATEGIES, historyKey);
  const extendedStats = expertIndicatorHistoryStats(historyKey);
  assert.equal(extendedStats?.barCount, 91);
  assert.equal(extendedStats?.seriesPointCalculations, 91);

  const revisedLast = {
    ...extended.at(-1)!,
    close: "2419.2",
    high: "2419.8",
    revision: 2,
  };
  buildExpertAnalysis(
    [...extended.slice(0, -1), revisedLast],
    DEFAULT_EXPERT_STRATEGIES,
    historyKey,
  );
  const revisedStats = expertIndicatorHistoryStats(historyKey);
  assert.equal(revisedStats?.barCount, 91);
  assert.equal(revisedStats?.seriesPointCalculations, 92);
});

test("keeps indicator history beyond twenty thousand bars without truncation", () => {
  const historyKey = "test:XAUUSD:jin10_client:1m:unbounded";
  clearExpertIndicatorHistory(historyKey);
  const candles = Array.from({ length: 20_050 }, (_, index) => candle(index, 2400 + index * 0.001));
  buildExpertAnalysis(candles, ["macd"], historyKey);
  assert.equal(expertIndicatorHistoryStats(historyKey)?.barCount, candles.length);
  const series = buildExpertIndicatorSeriesAt(candles, candles.length - 1, historyKey);
  assert.equal(series.visibleLength, candles.length);
  assert.equal(series.macd.histogram.length, candles.length);
  assert.equal(series.kdj.j.length, candles.length);
});

test("exposes causal indicator curves at the replay index without copying or truncating history", () => {
  const historyKey = "test:XAUUSD:jin10_client:5m:series-view";
  clearExpertIndicatorHistory(historyKey);
  const candles = Array.from({ length: 140 }, (_, index) => candle(index, 2390 + Math.sin(index / 7) * 8));
  const first = buildExpertIndicatorSeriesAt(candles, 59, historyKey);
  const second = buildExpertIndicatorSeriesAt(candles, 99, historyKey);

  assert.equal(first.visibleLength, 60);
  assert.equal(second.visibleLength, 100);
  assert.strictEqual(first.bars, second.bars);
  assert.strictEqual(first.kdj.k, second.kdj.k);
  assert.strictEqual(first.macd.value, second.macd.value);
  assert.equal(second.bars[second.offset + 99].time, Date.parse(candles[99].open_time) / 1_000);
});

test("builds explainable indicators and price levels from real bars", () => {
  const candles = Array.from({ length: 90 }, (_, index) => candle(index, 2400 + index * 0.8));
  const analysis = buildExpertAnalysis(candles, DEFAULT_EXPERT_STRATEGIES);
  assert.equal(analysis.regime, "trend-up");
  assert.ok((analysis.indicators.macd?.histogram ?? 0) >= 0);
  assert.ok(analysis.levels.some((level) => level.id === "poc-proxy"));
  assert.ok(analysis.signals.every((signal) => signal.evidence.length > 0));
});

test("marks volume-price analysis unavailable when spot volume is absent", () => {
  const candles = Array.from({ length: 40 }, (_, index) => candle(index, 2400 + index * 0.2, null));
  const analysis = buildExpertAnalysis(candles, ["volume-price"]);
  assert.equal(analysis.indicators.volumePriceState, "unavailable");
  assert.equal(analysis.signals[0]?.confidence, 0);
});

test("uses the same causal strategy evaluator for live signals and every backtest cutoff", () => {
  const candles = Array.from(
    { length: 180 },
    (_, index) => candle(index, 2400 + Math.sin(index / 5) * 18 + Math.sin(index / 2) * 3),
  );
  const enabled = ["structure", "macd", "kdj", "fair-value", "poc-proxy", "order-flow-proxy"] as const;

  for (const length of [35, 51, 79, 120, 180]) {
    const prefix = candles.slice(0, length);
    const live = buildExpertAnalysis(prefix, enabled);
    const backtest = runExpertBacktest(prefix, enabled);
    assert.equal(backtest.latestScore, live.compositeScore, `cutoff length ${length}`);
  }

  const first = runExpertBacktest(candles, enabled);
  const second = runExpertBacktest(candles, enabled);
  assert.deepEqual(second, first);
  assert.ok(first.tradeCount >= 1);
  assert.ok(Number.isFinite(first.totalReturnPercent));
  assert.match(first.caveat, /共用逐 Bar evaluator/);
});

test("excludes the still-open position from trade count and win rate", () => {
  const candles = Array.from({ length: 120 }, (_, index) => candle(index, 2350 + index * 0.6));
  const result = runExpertBacktest(candles, ["structure", "macd", "kdj"]);

  assert.equal(result.tradeCount, 0);
  assert.equal(result.winRate, 0);
  assert.ok(result.totalReturnPercent > 0);
  assert.match(result.caveat, /仅统计已平仓/);
});

test("reads causal analysis and backtest snapshots by replay index without rebuilding prefixes", () => {
  const candles = Array.from(
    { length: 160 },
    (_, index) => candle(index, 2380 + Math.sin(index / 4) * 12 + index * 0.08),
  );
  const enabled = ["structure", "macd", "kdj", "fair-value", "poc-proxy"] as const;
  const runner = createExpertBacktestRunner(candles, enabled);

  while (runner.completedIndex < 79) runner.advance(7);
  assert.deepEqual(
    buildExpertAnalysisAt(candles, enabled, 79),
    buildExpertAnalysis(candles.slice(0, 80), enabled),
  );
  assert.deepEqual(
    runner.resultAt(79),
    runExpertBacktest(candles.slice(0, 80), enabled),
  );

  while (!runner.done) runner.advance(11);
  assert.deepEqual(runner.resultAt(159), runExpertBacktest(candles, enabled));
});
