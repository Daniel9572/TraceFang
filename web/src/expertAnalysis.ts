import type {
  ExpertAnalysisSnapshot,
  ExpertBacktestResult,
  ExpertIndicatorSeriesView,
  ExpertSignal,
  ExpertStrategyId,
  ExpertValueZone,
} from "./expertTypes";
import {
  DEFAULT_EXPERT_STRATEGIES,
  EXPERT_STRATEGIES,
} from "./strategyCatalog.ts";
import {
  bollingerSnapshotAt,
  momentumSnapshotAt,
  movingAverageSnapshotAt,
  nineCountSnapshotAt,
} from "./expertTechnical.ts";
import { priceStructureSnapshotAt } from "./expertPricePatterns.ts";
import {
  createWilderRsiRuntime,
  rsiSnapshotAt,
  synchronizeWilderRsiRuntime,
  type WilderRsiRuntime,
} from "./expertRsi.ts";
import {
  latestSmartMoneySetup,
  marketStructureEventsAt,
  recentMarketStructureEvents,
} from "./expertSmartMoney.ts";
import type { Candle } from "./types";

interface NumericBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

interface MacdSeries {
  fast: number[];
  slow: number[];
  value: number[];
  signal: number[];
  histogram: number[];
}

interface KdjSeries {
  k: number[];
  d: number[];
  j: number[];
}

interface StrategyEvaluationContext {
  bars: NumericBar[];
  macd: MacdSeries;
  kdj: KdjSeries;
  rsi: WilderRsiRuntime;
}

interface StrategyHistoryEntry {
  key: string | null;
  signatures: string[];
  context: StrategyEvaluationContext;
  snapshots: Array<ExpertAnalysisSnapshot | undefined>;
  backtestRunners: Map<string, ExpertBacktestRunner>;
  seriesPointCalculations: number;
  snapshotCalculations: number;
  revision: number;
  lastSeriesChangeIndex: number;
}

interface StrategyHistoryView {
  entry: StrategyHistoryEntry;
  offset: number;
  length: number;
  revision: number;
  rawPrefixLengths: readonly number[];
  orderedInput: boolean;
}

export interface ExpertIndicatorHistoryStats {
  barCount: number;
  seriesPointCalculations: number;
  snapshotCalculations: number;
  backtestVariants: number;
  revision: number;
}

export interface ExpertBacktestRunner {
  readonly totalBars: number;
  readonly completedIndex: number;
  readonly done: boolean;
  advance(maxBars: number): number;
  resultAt(requestedIndex: number): ExpertBacktestResult;
}

export const EXPERT_INDICATOR_HISTORY_VERSION = "expert-indicators-v2";

const strategyHistoryByKey = new Map<string, StrategyHistoryEntry>();
const strategyHistoryByCandles = new WeakMap<readonly Candle[], StrategyHistoryView>();
const COMPOSITE_ELIGIBLE_STRATEGIES = new Set<ExpertStrategyId>(
  EXPERT_STRATEGIES
    .filter((strategy) => strategy.details.compositeEligible)
    .map((strategy) => strategy.id),
);
const BACKTEST_ELIGIBLE_STRATEGIES = new Set<ExpertStrategyId>(
  EXPERT_STRATEGIES
    .filter((strategy) => strategy.details.backtestEligible)
    .map((strategy) => strategy.id),
);

export { DEFAULT_EXPERT_STRATEGIES, EXPERT_STRATEGIES } from "./strategyCatalog.ts";

const ALL_EXPERT_STRATEGIES = new Set<ExpertStrategyId>(
  EXPERT_STRATEGIES.map((strategy) => strategy.id),
);

function finiteNumber(value: number | string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizedBars(candles: readonly Candle[]): {
  bars: NumericBar[];
  signatures: string[];
  rawPrefixLengths: number[];
  orderedInput: boolean;
} {
  const rows: Array<{ bar: NumericBar; signature: string }> = [];
  const rawPrefixLengths: number[] = [];
  let ordered = true;
  let previousTime = Number.NEGATIVE_INFINITY;
  for (const candle of candles) {
    const time = Date.parse(candle.open_time) / 1_000;
    const open = finiteNumber(candle.open);
    const high = finiteNumber(candle.high);
    const low = finiteNumber(candle.low);
    const close = finiteNumber(candle.close);
    if (
      ![time, open, high, low, close].every((value) => value !== null && Number.isFinite(value))
      || (low as number) > (high as number)
      || (open as number) < (low as number)
      || (open as number) > (high as number)
      || (close as number) < (low as number)
      || (close as number) > (high as number)
    ) {
      rawPrefixLengths.push(rows.length);
      continue;
    }
    const rawVolume = finiteNumber(candle.volume);
    const bar = {
      time,
      open: open as number,
      high: high as number,
      low: low as number,
      close: close as number,
      volume: rawVolume !== null && rawVolume >= 0 ? rawVolume : null,
    };
    rows.push({
      bar,
      signature: [bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume ?? ""].join("|"),
    });
    if (time < previousTime) ordered = false;
    previousTime = time;
    rawPrefixLengths.push(rows.length);
  }
  if (!ordered) rows.sort((left, right) => left.bar.time - right.bar.time);
  return {
    bars: rows.map((row) => row.bar),
    signatures: rows.map((row) => row.signature),
    rawPrefixLengths,
    orderedInput: ordered,
  };
}

function emptyStrategyEvaluationContext(): StrategyEvaluationContext {
  return {
    bars: [],
    macd: { fast: [], slow: [], value: [], signal: [], histogram: [] },
    kdj: { k: [], d: [], j: [] },
    rsi: createWilderRsiRuntime(),
  };
}

function synchronizeIndicatorSeries(
  context: StrategyEvaluationContext,
  bars: NumericBar[],
  requestedStartIndex: number,
): number {
  const startIndex = Math.min(bars.length, Math.max(0, Math.floor(requestedStartIndex)));
  context.bars = bars;
  const { macd: macdSeries, kdj: kdjSeries } = context;
  for (const series of [
    macdSeries.fast,
    macdSeries.slow,
    macdSeries.value,
    macdSeries.signal,
    macdSeries.histogram,
    kdjSeries.k,
    kdjSeries.d,
    kdjSeries.j,
  ]) {
    series.length = startIndex;
  }

  const fastMultiplier = 2 / 13;
  const slowMultiplier = 2 / 27;
  const signalMultiplier = 2 / 10;
  let previousK = startIndex > 0 ? kdjSeries.k[startIndex - 1] : 50;
  let previousD = startIndex > 0 ? kdjSeries.d[startIndex - 1] : 50;
  for (let index = startIndex; index < bars.length; index += 1) {
    const close = bars[index].close;
    const fast = index === 0
      ? close
      : close * fastMultiplier + macdSeries.fast[index - 1] * (1 - fastMultiplier);
    const slow = index === 0
      ? close
      : close * slowMultiplier + macdSeries.slow[index - 1] * (1 - slowMultiplier);
    const value = fast - slow;
    const signal = index === 0
      ? value
      : value * signalMultiplier + macdSeries.signal[index - 1] * (1 - signalMultiplier);
    macdSeries.fast.push(fast);
    macdSeries.slow.push(slow);
    macdSeries.value.push(value);
    macdSeries.signal.push(signal);
    macdSeries.histogram.push(value - signal);

    let lowest = Number.POSITIVE_INFINITY;
    let highest = Number.NEGATIVE_INFINITY;
    for (let windowIndex = Math.max(0, index - 8); windowIndex <= index; windowIndex += 1) {
      lowest = Math.min(lowest, bars[windowIndex].low);
      highest = Math.max(highest, bars[windowIndex].high);
    }
    const rsv = highest === lowest ? 50 : (close - lowest) / (highest - lowest) * 100;
    previousK = previousK * 2 / 3 + rsv / 3;
    previousD = previousD * 2 / 3 + previousK / 3;
    kdjSeries.k.push(previousK);
    kdjSeries.d.push(previousD);
    kdjSeries.j.push(previousK * 3 - previousD * 2);
  }
  synchronizeWilderRsiRuntime(context.rsi, bars, startIndex);
  return bars.length - startIndex;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function directionValue(direction: ExpertSignal["direction"]): number {
  if (direction === "bullish") return 1;
  if (direction === "bearish") return -1;
  return 0;
}

function recentTrendSlopePercent(
  bars: readonly NumericBar[],
  endIndex: number,
  length = 30,
): number | null {
  const values = bars
    .slice(Math.max(0, endIndex - length + 1), endIndex + 1)
    .map((bar) => bar.close);
  if (values.length < 4) return null;
  const meanX = (values.length - 1) / 2;
  const meanY = values.reduce((sum, value) => sum + value, 0) / values.length;
  const numerator = values.reduce((sum, value, index) => sum + (index - meanX) * (value - meanY), 0);
  const denominator = values.reduce((sum, _, index) => sum + (index - meanX) ** 2, 0);
  return denominator === 0 || meanY === 0 ? null : numerator / denominator / meanY * 100;
}

function priceDensityPoc(bars: readonly NumericBar[], endIndex: number): number | null {
  const window = bars.slice(Math.max(0, endIndex - 239), endIndex + 1);
  if (window.length === 0) return null;
  const minimum = Math.min(...window.map((bar) => bar.low));
  const maximum = Math.max(...window.map((bar) => bar.high));
  if (maximum <= minimum) return window.at(-1)?.close ?? null;
  const bins = 32;
  const width = (maximum - minimum) / bins;
  const weights = Array.from({ length: bins }, () => 0);
  for (const bar of window) {
    const first = clamp(Math.floor((bar.low - minimum) / width), 0, bins - 1);
    const last = clamp(Math.floor((bar.high - minimum) / width), 0, bins - 1);
    const occupied = Math.max(1, last - first + 1);
    const weight = (bar.volume && bar.volume > 0 ? bar.volume : 1) / occupied;
    for (let index = first; index <= last; index += 1) weights[index] += weight;
  }
  const strongest = weights.reduce(
    (best, value, index) => value > weights[best] ? index : best,
    0,
  );
  return minimum + (strongest + 0.5) * width;
}

function fairValueZones(bars: readonly NumericBar[], endIndex: number): ExpertValueZone[] {
  const zones: ExpertValueZone[] = [];
  for (let index = Math.max(2, endIndex - 119); index <= endIndex; index += 1) {
    const first = bars[index - 2];
    const third = bars[index];
    if (third.low > first.high) {
      zones.push({
        id: `fvg-up:${third.time}`,
        start: first.time,
        end: bars[endIndex]?.time ?? third.time,
        low: first.high,
        high: third.low,
        direction: "bullish",
        label: "多方 FVG",
      });
    } else if (third.high < first.low) {
      zones.push({
        id: `fvg-down:${third.time}`,
        start: first.time,
        end: bars[endIndex]?.time ?? third.time,
        low: third.high,
        high: first.low,
        direction: "bearish",
        label: "空方 FVG",
      });
    }
  }
  return zones.slice(-4);
}

function orderFlowPressure(bars: readonly NumericBar[], endIndex: number): number | null {
  const window = bars.slice(Math.max(0, endIndex - 31), endIndex + 1);
  if (window.length === 0) return null;
  let weighted = 0;
  let weightSum = 0;
  for (const bar of window) {
    const range = Math.max(bar.high - bar.low, Math.abs(bar.close) * 1e-8);
    const weight = bar.volume && bar.volume > 0 ? Math.sqrt(bar.volume) : 1;
    weighted += clamp((bar.close - bar.open) / range, -1, 1) * weight;
    weightSum += weight;
  }
  return weightSum === 0 ? null : clamp(weighted / weightSum, -1, 1);
}

function volumePriceState(
  bars: readonly NumericBar[],
  endIndex: number,
): "confirming" | "diverging" | "unavailable" {
  const rawWindow = bars.slice(Math.max(0, endIndex - 23), endIndex + 1);
  if (rawWindow.at(-1)?.volume === null) return "unavailable";
  const window = rawWindow
    .filter((bar) => bar.volume !== null && bar.volume > 0);
  if (window.length < 12) return "unavailable";
  const half = Math.floor(window.length / 2);
  const earlierVolume = window.slice(0, half).reduce((sum, bar) => sum + (bar.volume ?? 0), 0) / half;
  const laterVolume = window.slice(half).reduce((sum, bar) => sum + (bar.volume ?? 0), 0) / (window.length - half);
  const priceMove = window.at(-1)!.close - window[0].close;
  const volumeMove = laterVolume - earlierVolume;
  return Math.sign(priceMove) === Math.sign(volumeMove) || Math.abs(volumeMove / earlierVolume) < 0.04
    ? "confirming"
    : "diverging";
}

function createSignal(
  strategyId: ExpertStrategyId,
  title: string,
  detail: string,
  direction: ExpertSignal["direction"],
  confidence: number,
  triggeredAt: number,
  evidence: string[],
): ExpertSignal {
  return {
    id: `${strategyId}:${triggeredAt}:${direction}`,
    strategyId,
    title,
    detail,
    direction,
    confidence: clamp(confidence, 0, 1),
    triggeredAt,
    evidence,
  };
}

function createStrategyHistoryEntry(
  bars: NumericBar[],
  signatures: string[],
  key: string | null,
): StrategyHistoryEntry {
  const context = emptyStrategyEvaluationContext();
  const calculated = synchronizeIndicatorSeries(context, bars, 0);
  return {
    key,
    signatures,
    context,
    snapshots: Array.from({ length: bars.length }),
    backtestRunners: new Map(),
    seriesPointCalculations: calculated,
    snapshotCalculations: 0,
    revision: 1,
    lastSeriesChangeIndex: 0,
  };
}

function exactTimeIndex(bars: readonly NumericBar[], time: number): number {
  let low = 0;
  let high = bars.length - 1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const value = bars[middle].time;
    if (value === time) return middle;
    if (value < time) low = middle + 1;
    else high = middle - 1;
  }
  return -1;
}

function updateStrategyHistoryEntry(
  entry: StrategyHistoryEntry,
  bars: NumericBar[],
  signatures: string[],
  startIndex: number,
): void {
  entry.seriesPointCalculations += synchronizeIndicatorSeries(entry.context, bars, startIndex);
  entry.signatures = signatures;
  entry.snapshots.length = startIndex;
  entry.snapshots.length = bars.length;
  entry.backtestRunners.clear();
  entry.revision += 1;
  entry.lastSeriesChangeIndex = startIndex;
}

function strategyHistoryView(
  candles: readonly Candle[],
  historyKey?: string,
): StrategyHistoryView {
  const cachedByReference = strategyHistoryByCandles.get(candles);
  if (
    cachedByReference
    && cachedByReference.entry.key === (historyKey ?? null)
    && cachedByReference.revision === cachedByReference.entry.revision
  ) {
    return cachedByReference;
  }

  const normalized = normalizedBars(candles);
  const createView = (entry: StrategyHistoryEntry, offset = 0): StrategyHistoryView => {
    const view = {
      entry,
      offset,
      length: normalized.bars.length,
      revision: entry.revision,
      rawPrefixLengths: normalized.rawPrefixLengths,
      orderedInput: normalized.orderedInput,
    };
    strategyHistoryByCandles.set(candles, view);
    return view;
  };
  if (!historyKey || normalized.bars.length === 0) {
    return createView(createStrategyHistoryEntry(
      normalized.bars,
      normalized.signatures,
      historyKey ?? null,
    ));
  }

  const existing = strategyHistoryByKey.get(historyKey);
  if (!existing) {
    const entry = createStrategyHistoryEntry(normalized.bars, normalized.signatures, historyKey);
    strategyHistoryByKey.set(historyKey, entry);
    return createView(entry);
  }

  const offset = exactTimeIndex(existing.context.bars, normalized.bars[0].time);
  if (offset >= 0) {
    const overlap = Math.min(normalized.bars.length, existing.context.bars.length - offset);
    let aligned = true;
    let firstChanged = overlap;
    for (let index = 0; index < overlap; index += 1) {
      if (existing.context.bars[offset + index].time !== normalized.bars[index].time) {
        aligned = false;
        break;
      }
      if (
        firstChanged === overlap
        && existing.signatures[offset + index] !== normalized.signatures[index]
      ) {
        firstChanged = index;
      }
    }
    if (aligned) {
      if (normalized.bars.length > overlap) firstChanged = Math.min(firstChanged, overlap);
      if (firstChanged < normalized.bars.length) {
        const replaceEnd = offset + normalized.bars.length;
        const mergedBars = [
          ...existing.context.bars.slice(0, offset),
          ...normalized.bars,
          ...existing.context.bars.slice(replaceEnd),
        ];
        const mergedSignatures = [
          ...existing.signatures.slice(0, offset),
          ...normalized.signatures,
          ...existing.signatures.slice(replaceEnd),
        ];
        updateStrategyHistoryEntry(existing, mergedBars, mergedSignatures, offset + firstChanged);
      }
      return createView(existing, offset);
    }
  }

  updateStrategyHistoryEntry(existing, normalized.bars, normalized.signatures, 0);
  return createView(existing);
}

export function expertIndicatorHistoryStats(historyKey: string): ExpertIndicatorHistoryStats | null {
  const entry = strategyHistoryByKey.get(historyKey);
  return entry ? {
    barCount: entry.context.bars.length,
    seriesPointCalculations: entry.seriesPointCalculations,
    snapshotCalculations: entry.snapshotCalculations,
    backtestVariants: entry.backtestRunners.size,
    revision: entry.revision,
  } : null;
}

export function clearExpertIndicatorHistory(historyKey?: string): void {
  if (historyKey) strategyHistoryByKey.delete(historyKey);
  else strategyHistoryByKey.clear();
}

function evaluateExpertStrategiesAt(
  context: StrategyEvaluationContext,
  enabled: ReadonlySet<ExpertStrategyId>,
  requestedIndex: number,
): ExpertAnalysisSnapshot {
  const { bars, macd: macdSeries, kdj: kdjSeries, rsi: rsiRuntime } = context;
  const lastIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (lastIndex < 0) {
    return {
      asOf: null,
      signals: [],
      levels: [],
      valueZones: [],
      pricePatterns: [],
      marketStructureEvents: [],
      indicators: {
        macd: null,
        kdj: null,
        rsi: null,
        movingAverage: null,
        bollinger: null,
        nineCount: null,
        momentum: null,
        trendSlopePercent: null,
        pocPrice: null,
        orderFlowPressure: null,
        volumePriceState: "unavailable",
      },
      regime: "insufficient",
      compositeScore: 0,
    };
  }

  const latest = bars[lastIndex];
  const slope = recentTrendSlopePercent(bars, lastIndex);
  const poc = priceDensityPoc(bars, lastIndex);
  const pressure = orderFlowPressure(bars, lastIndex);
  const volumeState = volumePriceState(bars, lastIndex);
  const structure = enabled.has("structure") ? priceStructureSnapshotAt(bars, lastIndex) : null;
  const allMarketStructureEvents = enabled.has("smart-money")
    ? marketStructureEventsAt(bars, lastIndex)
    : [];
  const marketStructureEvents = recentMarketStructureEvents(allMarketStructureEvents);
  const zones = fairValueZones(bars, lastIndex);
  const movingAverage = movingAverageSnapshotAt(bars, lastIndex);
  const bollinger = bollingerSnapshotAt(bars, lastIndex);
  const nineCount = nineCountSnapshotAt(bars, lastIndex);
  const momentum = momentumSnapshotAt(bars, lastIndex);
  const signals: ExpertSignal[] = [];

  const patternsCompletedNow = structure?.patterns.filter((pattern) => (
    pattern.status === "confirmed" && pattern.detectedAt === latest.time
  )) ?? [];
  const patternDirections = new Set(patternsCompletedNow.map((pattern) => pattern.direction));
  const patternConflict = patternDirections.size > 1;
  const latestPattern = patternConflict
    ? null
    : [...patternsCompletedNow].sort((left, right) => (
      right.confidence - left.confidence || left.id.localeCompare(right.id)
    ))[0] ?? null;
  const patternCompletedNow = latestPattern !== null;
  if (enabled.has("structure") && (slope !== null || patternCompletedNow || patternConflict)) {
    const slopeDirection = (slope ?? 0) > 0.004
      ? "bullish" as const
      : (slope ?? 0) < -0.004 ? "bearish" as const : "neutral" as const;
    const direction = patternConflict
      ? "neutral" as const
      : patternCompletedNow ? latestPattern.direction : slopeDirection;
    signals.push(createSignal(
      "structure",
      patternConflict
        ? "同柱结构方向冲突"
        : patternCompletedNow
        ? latestPattern.label
        : direction === "bullish" ? "结构保持上行" : direction === "bearish" ? "结构转弱" : "结构震荡",
      patternConflict
        ? "同一根已收盘 Bar 同时确认多空形态，不做任意方向裁决"
        : patternCompletedNow
        ? `确认价 ${latestPattern.confirmation.price.toFixed(2)} · 失效位 ${latestPattern.invalidationPrice.toFixed(2)}`
        : `近端回归斜率 ${(slope ?? 0).toFixed(3)}%/Bar`,
      direction,
      patternConflict
        ? 0
        : patternCompletedNow
        ? latestPattern.confidence
        : clamp(0.56 + Math.abs(slope ?? 0) * 5, 0.56, 0.82),
      latest.time,
      patternConflict
        ? patternsCompletedNow.map((pattern) => `${pattern.label} ${pattern.direction}`)
        : patternCompletedNow ? latestPattern.evidence : [
        structure?.support === null || structure?.support === undefined ? "支撑待确认" : `支撑 ${structure.support.toFixed(2)}`,
        structure?.resistance === null || structure?.resistance === undefined ? "压力待确认" : `压力 ${structure.resistance.toFixed(2)}`,
      ],
    ));
  }

  if (enabled.has("ma-structure")) {
    const alignment = movingAverage?.alignment ?? "insufficient";
    const interaction = movingAverage?.values.find((item) => (
      (item.period === 60 || item.period === 250) && item.interaction !== "none"
    ));
    const direction = interaction?.interaction === "support-test"
      ? "bullish" as const
      : interaction?.interaction === "resistance-test"
        ? "bearish" as const
        : interaction?.interaction === "break"
          ? (interaction.distanceAtr ?? 0) >= 0 ? "bullish" as const : "bearish" as const
          : alignment === "bullish"
            ? "bullish" as const
            : alignment === "bearish" ? "bearish" as const : "neutral" as const;
    const available = movingAverage?.values.filter((item) => item.value !== null) ?? [];
    const title = alignment === "insufficient"
      ? "MA 样本不足"
      : interaction?.interaction === "support-test"
      ? `MA${interaction.period} 支撑区测试`
      : interaction?.interaction === "resistance-test"
        ? `MA${interaction.period} 压力区测试`
        : interaction?.interaction === "break"
          ? `MA${interaction.period} 收盘突破`
          : alignment === "bullish"
            ? "MA 多头排列"
            : alignment === "bearish" ? "MA 空头排列" : "MA 结构混合";
    signals.push(createSignal(
      "ma-structure",
      title,
      available.length === 0
        ? "至少需要 20 根 Bar；MA250 需要 250 根当前周期 Bar"
        : `${available.map((item) => `MA${item.period} ${item.value?.toFixed(2)}`).join(" · ")}`,
      direction,
      alignment === "insufficient" ? 0 : clamp(0.52 + available.length * 0.045, 0.52, 0.72),
      latest.time,
      [
        interaction
          ? `${interaction.interaction}；距离 ${interaction.distanceAtr?.toFixed(2) ?? "—"} ATR`
          : "支撑/压力按 ATR 缓冲区判断，不把均线视为精确价位",
        "日线 MA250 在产品中称年线；周线/年 K 的 250 周期含义不同",
      ],
    ));
  }

  const macdSnapshot = lastIndex + 1 >= 26 ? {
    value: macdSeries.value[lastIndex],
    signal: macdSeries.signal[lastIndex],
    histogram: macdSeries.histogram[lastIndex],
  } : null;
  if (enabled.has("macd") && macdSnapshot) {
    const direction = macdSnapshot.histogram > 0 ? "bullish" : macdSnapshot.histogram < 0 ? "bearish" : "neutral";
    signals.push(createSignal(
      "macd",
      direction === "bullish" ? "MACD 动量向上" : direction === "bearish" ? "MACD 动量向下" : "MACD 动量中性",
      `柱值 ${macdSnapshot.histogram.toFixed(3)}，DIF ${macdSnapshot.value.toFixed(3)}`,
      direction,
      clamp(0.56 + Math.abs(macdSnapshot.histogram) / Math.max(1, latest.close) * 60, 0.56, 0.82),
      latest.time,
      [`DEA ${macdSnapshot.signal.toFixed(3)}`],
    ));
  }

  const kdjSnapshot = lastIndex + 1 >= 9 ? {
    k: kdjSeries.k[lastIndex],
    d: kdjSeries.d[lastIndex],
    j: kdjSeries.j[lastIndex],
  } : null;
  if (enabled.has("kdj") && kdjSnapshot) {
    const direction = kdjSnapshot.k > kdjSnapshot.d ? "bullish" : "bearish";
    const extreme = kdjSnapshot.j >= 100 ? "，处于高位" : kdjSnapshot.j <= 0 ? "，处于低位" : "";
    signals.push(createSignal(
      "kdj",
      direction === "bullish" ? "KDJ 多方占优" : "KDJ 空方占优",
      `K ${kdjSnapshot.k.toFixed(1)} / D ${kdjSnapshot.d.toFixed(1)} / J ${kdjSnapshot.j.toFixed(1)}${extreme}`,
      direction,
      kdjSnapshot.j > 110 || kdjSnapshot.j < -10 ? 0.63 : 0.58,
      latest.time,
      [extreme ? "极值区信号需防反转" : "摆动区间正常"],
    ));
  }

  const rsiSnapshot = rsiSnapshotAt(rsiRuntime, lastIndex);
  if (enabled.has("rsi")) {
    const direction = rsiSnapshot?.signal === "oversold-recovery"
      ? "bullish" as const
      : rsiSnapshot?.signal === "overbought-reversal"
        ? "bearish" as const
        : "neutral" as const;
    const title = rsiSnapshot === null
      ? "RSI 样本不足"
      : rsiSnapshot.signal === "oversold-recovery"
        ? "RSI 离开超卖区"
        : rsiSnapshot.signal === "overbought-reversal"
          ? "RSI 离开超买区"
          : rsiSnapshot.state === "oversold"
            ? "RSI 仍处超卖区"
            : rsiSnapshot.state === "overbought" ? "RSI 仍处超买区" : "RSI 位于中性区";
    signals.push(createSignal(
      "rsi",
      title,
      rsiSnapshot === null
        ? "Wilder RSI(14) 至少需要 15 个收盘价"
        : `RSI(14) ${rsiSnapshot.value.toFixed(1)} · 30/70 阈值`,
      direction,
      direction === "neutral" ? 0 : 0.54,
      latest.time,
      [
        direction === "neutral"
          ? "超买/超卖本身不是反转信号；只在重新穿回阈值时提醒"
          : "阈值回穿只作确认，强趋势中仍可能再次进入极值区",
        "使用 Wilder 递推平滑，按已收盘 Bar 计算",
      ],
    ));
  }

  if (enabled.has("bollinger")) {
    const breakoutDirection = bollinger && bollinger.state === "expanding" && bollinger.position > 1
      ? "bullish" as const
      : bollinger && bollinger.state === "expanding" && bollinger.position < -1
        ? "bearish" as const
        : "neutral" as const;
    signals.push(createSignal(
      "bollinger",
      bollinger === null
        ? "布林样本不足"
        : bollinger.state === "squeeze"
          ? "布林带处于低带宽压缩"
          : breakoutDirection === "bullish"
            ? "上轨外扩张突破"
            : breakoutDirection === "bearish" ? "下轨外扩张突破" : "布林波动状态正常",
      bollinger === null
        ? "需要至少 20 根 Bar；滚动带宽分位需要更长历史"
        : `位置 ${bollinger.position.toFixed(2)}σ · 带宽 ${(bollinger.bandwidth * 100).toFixed(2)}% · 分位 ${bollinger.bandwidthPercentile === null ? "—" : `${(bollinger.bandwidthPercentile * 100).toFixed(0)}%`}`,
      breakoutDirection,
      breakoutDirection === "neutral" ? 0 : 0.61,
      latest.time,
      ["首次触轨不是反转信号；只有带宽扩张且收盘越轨才作为方向确认"],
    ));
  }

  if (enabled.has("nine-count")) {
    const direction = nineCount?.completedNow === true
      ? nineCount.direction === "sell-setup" ? "bearish" as const : "bullish" as const
      : "neutral" as const;
    signals.push(createSignal(
      "nine-count",
      nineCount === null || nineCount.count === 0
        ? "九转 Setup 尚未计数"
        : `${nineCount.direction === "sell-setup" ? "上涨" : "下跌"} Setup ${nineCount.count}/9`,
      nineCount?.completedNow === true
        ? `${nineCount.perfected ? "已满足 Perfected 条件" : "尚未满足 Perfected 条件"}；等待结构确认`
        : nineCount?.count === 9
          ? "Setup 9 已在更早的 Bar 完成；等待价格翻转后重新计数"
        : "每根收盘价与四根之前比较；方向改变即重置",
      direction,
      nineCount?.completedNow === true ? 0.35 : 0,
      latest.time,
      ["这里只实现 1–9 Setup，不冒充包含 13 Countdown 的完整 Sequential", "实验性耗竭提醒不计入综合方向分"],
    ));
  }

  if (enabled.has("momentum-ensemble")) {
    const availableReturns = momentum?.returns
      .filter((item): item is typeof item & { percent: number } => item.percent !== null) ?? [];
    const positiveHorizons = availableReturns.filter((item) => item.percent > 0).length;
    const negativeHorizons = availableReturns.filter((item) => item.percent < 0).length;
    const direction = availableReturns.length >= 2
      && positiveHorizons >= 2
      && (momentum?.score ?? 0) > 0.15
      ? "bullish" as const
      : availableReturns.length >= 2
        && negativeHorizons >= 2
        && (momentum?.score ?? 0) < -0.15
        ? "bearish" as const
        : "neutral" as const;
    signals.push(createSignal(
      "momentum-ensemble",
      momentum === null
        ? "多周期动量样本不足"
        : direction === "bullish" ? "多周期动量同向偏多" : direction === "bearish" ? "多周期动量同向偏空" : "多周期动量分歧",
      momentum === null
        ? "至少需要 21 根 Bar；完整 20/60/120 组合需要 121 根"
        : `波动率标准化得分 ${momentum.score.toFixed(2)} · ${momentum.availableHorizons}/3 个期限可用`,
      direction,
      momentum === null || direction === "neutral"
        ? 0
        : clamp(0.5 + Math.abs(momentum.score) * 0.22, 0.5, 0.72),
      latest.time,
      momentum?.returns.map((item) => (
        `${item.horizon} Bar ${item.percent === null ? "不可用" : `${item.percent >= 0 ? "+" : ""}${item.percent.toFixed(2)}%`}`
      )) ?? ["等待历史"],
    ));
  }

  const latestZone = zones.at(-1);
  if (enabled.has("fair-value") && latestZone) {
    signals.push(createSignal(
      "fair-value",
      latestZone.direction === "bullish" ? "下方存在多方 FVG" : "上方存在空方 FVG",
      `${latestZone.low.toFixed(2)} – ${latestZone.high.toFixed(2)}`,
      latestZone.direction,
      0.57,
      latest.time,
      ["三根 K 线失衡区，回补并非必然"],
    ));
  }

  if (enabled.has("poc-proxy") && poc !== null) {
    const direction = latest.close >= poc ? "bullish" : "bearish";
    signals.push(createSignal(
      "poc-proxy",
      latest.close >= poc ? "价格位于 POC 代理上方" : "价格位于 POC 代理下方",
      `K 线价格密度中心 ${poc.toFixed(2)}`,
      direction,
      0.52,
      latest.time,
      ["代理口径：非逐价成交量，不等同真实 Volume Profile"],
    ));
  }

  if (enabled.has("order-flow-proxy") && pressure !== null) {
    const direction = pressure > 0.08 ? "bullish" : pressure < -0.08 ? "bearish" : "neutral";
    signals.push(createSignal(
      "order-flow-proxy",
      direction === "bullish" ? "订单流代理偏多" : direction === "bearish" ? "订单流代理偏空" : "订单流代理平衡",
      `实体/振幅压力 ${(pressure * 100).toFixed(1)}`,
      direction,
      0.44,
      latest.time,
      ["缺少 bid/ask、主动买卖方向和 L2 深度"],
    ));
  }

  if (enabled.has("volume-price")) {
    signals.push(createSignal(
      "volume-price",
      volumeState === "unavailable" ? "成交量口径不可用" : volumeState === "confirming" ? "量价相互确认" : "量价出现背离",
      volumeState === "unavailable" ? "当前现货源未提供可比较成交量" : "比较最近两个等长窗口的价格与总量变化",
      volumeState === "diverging" ? "bearish" : volumeState === "confirming" ? "bullish" : "neutral",
      volumeState === "unavailable" ? 0 : 0.54,
      latest.time,
      [volumeState === "unavailable" ? "等待期货或交易所成交量源" : "仅代表当前数据源口径"],
    ));
  }

  if (enabled.has("smart-money")) {
    const setup = latestSmartMoneySetup(allMarketStructureEvents, lastIndex);
    signals.push(createSignal(
      "smart-money",
      setup === null
        ? "等待扫掠与结构确认"
        : setup.direction === "bullish" ? "下方扫掠后结构转强" : "上方扫掠后结构转弱",
      setup === null
        ? "需要先出现流动性扫掠代理，再在 8 根 Bar 内出现同向 BOS/CHOCH"
        : `${setup.sweep.label} → ${setup.structureShift.label}`,
      setup?.direction ?? "neutral",
      setup?.confidence ?? 0,
      latest.time,
      setup?.evidence ?? [
        "只读取摆动高低点、影线回收和收盘突破",
        "不识别订单身份，也不把价格结构等同于机构行为",
      ],
    ));
  }

  const actionable = signals.filter((signal) => (
    signal.confidence > 0 && COMPOSITE_ELIGIBLE_STRATEGIES.has(signal.strategyId)
  )).sort((left, right) => left.strategyId.localeCompare(right.strategyId));
  const compositeScore = actionable.length === 0
    ? 0
    : actionable.reduce((sum, signal) => sum + directionValue(signal.direction) * signal.confidence, 0)
      / actionable.reduce((sum, signal) => sum + signal.confidence, 0);
  const regime = compositeScore > 0.2
    ? "trend-up"
    : compositeScore < -0.2
      ? "trend-down"
      : "balanced";

  return {
    asOf: latest.time,
    signals: signals.sort((left, right) => right.confidence - left.confidence),
    levels: [
      ...(enabled.has("structure") && structure?.support !== null && structure?.support !== undefined ? [{
        id: "structure-support",
        price: structure.support,
        label: "结构支撑",
        tone: "support" as const,
        style: "dashed" as const,
      }] : []),
      ...(enabled.has("structure") && structure?.resistance !== null && structure?.resistance !== undefined ? [{
        id: "structure-resistance",
        price: structure.resistance,
        label: "结构压力",
        tone: "resistance" as const,
        style: "dashed" as const,
      }] : []),
      ...(enabled.has("poc-proxy") && poc !== null ? [{
        id: "poc-proxy",
        price: poc,
        label: "POC≈",
        tone: "gold" as const,
        style: "dotted" as const,
      }] : []),
      ...(enabled.has("ma-structure") ? (movingAverage?.values ?? [])
        .filter((item) => (
          (item.period === 60 || item.period === 250)
          && item.value !== null
          && item.interaction !== "none"
        ))
        .map((item) => ({
          id: `ma-${item.period}-interaction`,
          price: item.value as number,
          label: `MA${item.period} ${item.interaction === "support-test" ? "支撑区" : item.interaction === "resistance-test" ? "压力区" : "突破位"}`,
          tone: item.interaction === "support-test" ? "support" as const : item.interaction === "resistance-test" ? "resistance" as const : "gold" as const,
          style: "dotted" as const,
        })) : []),
    ],
    valueZones: enabled.has("fair-value") ? zones : [],
    pricePatterns: enabled.has("structure") ? structure?.patterns ?? [] : [],
    marketStructureEvents: enabled.has("smart-money") ? marketStructureEvents : [],
    indicators: {
      macd: macdSnapshot,
      kdj: kdjSnapshot,
      rsi: rsiSnapshot,
      movingAverage,
      bollinger,
      nineCount,
      momentum,
      trendSlopePercent: slope,
      pocPrice: poc,
      orderFlowPressure: pressure,
      volumePriceState: volumeState,
    },
    regime,
    compositeScore,
  };
}

function cachedBaseSnapshotAt(
  entry: StrategyHistoryEntry,
  requestedIndex: number,
): ExpertAnalysisSnapshot {
  const index = Math.min(
    entry.context.bars.length - 1,
    Math.max(-1, Math.floor(requestedIndex)),
  );
  if (index < 0) return evaluateExpertStrategiesAt(entry.context, ALL_EXPERT_STRATEGIES, -1);
  const cached = entry.snapshots[index];
  if (cached) return cached;
  const snapshot = evaluateExpertStrategiesAt(entry.context, ALL_EXPERT_STRATEGIES, index);
  entry.snapshots[index] = snapshot;
  entry.snapshotCalculations += 1;
  return snapshot;
}

function snapshotForEnabledStrategies(
  entry: StrategyHistoryEntry,
  enabled: ReadonlySet<ExpertStrategyId>,
  requestedIndex: number,
): ExpertAnalysisSnapshot {
  const snapshot = cachedBaseSnapshotAt(entry, requestedIndex);
  if (snapshot.regime === "insufficient") return snapshot;
  const signals = snapshot.signals.filter((signal) => enabled.has(signal.strategyId));
  const actionable = signals.filter((signal) => (
    signal.confidence > 0 && COMPOSITE_ELIGIBLE_STRATEGIES.has(signal.strategyId)
  )).sort((left, right) => left.strategyId.localeCompare(right.strategyId));
  const compositeScore = actionable.length === 0
    ? 0
    : actionable.reduce(
      (sum, signal) => sum + directionValue(signal.direction) * signal.confidence,
      0,
    ) / actionable.reduce((sum, signal) => sum + signal.confidence, 0);
  const regime = compositeScore > 0.2
    ? "trend-up" as const
    : compositeScore < -0.2
      ? "trend-down" as const
      : "balanced" as const;
  return {
    ...snapshot,
    signals,
    levels: snapshot.levels.filter((level) => (
      level.id.startsWith("structure-")
        ? enabled.has("structure")
        : level.id.startsWith("ma-")
          ? enabled.has("ma-structure")
          : level.id === "poc-proxy" ? enabled.has("poc-proxy") : true
    )),
    valueZones: enabled.has("fair-value") ? snapshot.valueZones : [],
    pricePatterns: enabled.has("structure") ? snapshot.pricePatterns : [],
    marketStructureEvents: enabled.has("smart-money") ? snapshot.marketStructureEvents : [],
    regime,
    compositeScore,
  };
}

export function buildExpertAnalysis(
  candles: readonly Candle[],
  enabledStrategies: readonly ExpertStrategyId[],
  historyKey?: string,
): ExpertAnalysisSnapshot {
  return buildExpertAnalysisAt(candles, enabledStrategies, candles.length - 1, historyKey);
}

export function buildExpertAnalysisAt(
  candles: readonly Candle[],
  enabledStrategies: readonly ExpertStrategyId[],
  requestedIndex: number,
  historyKey?: string,
): ExpertAnalysisSnapshot {
  const view = strategyHistoryView(candles, historyKey);
  const rawCutoff = Math.min(
    candles.length - 1,
    Math.max(-1, Math.floor(requestedIndex)),
  );
  if (!view.orderedInput && rawCutoff < candles.length - 1) {
    const prefix = candles.slice(0, rawCutoff + 1);
    const prefixView = strategyHistoryView(prefix);
    return snapshotForEnabledStrategies(
      prefixView.entry,
      new Set(enabledStrategies),
      prefixView.offset + prefixView.length - 1,
    );
  }
  const visibleLength = rawCutoff < 0
    ? 0
    : view.rawPrefixLengths[rawCutoff] ?? view.length;
  return snapshotForEnabledStrategies(
    view.entry,
    new Set(enabledStrategies),
    view.offset + visibleLength - 1,
  );
}

function indicatorSeriesView(
  view: StrategyHistoryView,
  visibleLength: number,
): ExpertIndicatorSeriesView {
  const boundedVisibleLength = Math.min(view.length, Math.max(0, visibleLength));
  return {
    historyKey: view.entry.key,
    revision: view.entry.revision,
    offset: view.offset,
    length: view.length,
    visibleLength: boundedVisibleLength,
    changedFrom: Math.min(
      view.length,
      Math.max(0, view.entry.lastSeriesChangeIndex - view.offset),
    ),
    bars: view.entry.context.bars,
    macd: {
      value: view.entry.context.macd.value,
      signal: view.entry.context.macd.signal,
      histogram: view.entry.context.macd.histogram,
    },
    kdj: {
      k: view.entry.context.kdj.k,
      d: view.entry.context.kdj.d,
      j: view.entry.context.kdj.j,
    },
    rsi: {
      value: view.entry.context.rsi.values,
    },
  };
}

export function buildExpertIndicatorSeriesAt(
  candles: readonly Candle[],
  requestedIndex: number,
  historyKey?: string,
): ExpertIndicatorSeriesView {
  const view = strategyHistoryView(candles, historyKey);
  const rawCutoff = Math.min(
    candles.length - 1,
    Math.max(-1, Math.floor(requestedIndex)),
  );
  if (!view.orderedInput && rawCutoff < candles.length - 1) {
    const prefixView = strategyHistoryView(candles.slice(0, rawCutoff + 1));
    return indicatorSeriesView(prefixView, prefixView.length);
  }
  const visibleLength = rawCutoff < 0
    ? 0
    : view.rawPrefixLengths[rawCutoff] ?? view.length;
  return indicatorSeriesView(view, visibleLength);
}

const BACKTEST_CAVEAT = "实验回测与实时信号共用逐 Bar evaluator，按信号柱收盘换仓并计入 0.02% 单边摩擦；交易数和胜率仅统计已平仓持仓，期末持仓按末价计入收益但不计胜负；未包含真实点差、滑点和 as-of 修订快照。";

function emptyBacktestResult(barCount: number, enabledCount: number): ExpertBacktestResult {
  return {
    barCount,
    tradeCount: 0,
    winRate: 0,
    totalReturnPercent: 0,
    maxDrawdownPercent: 0,
    latestScore: 0,
    caveat: enabledCount === 0
      ? "尚未启用策略；实验回测不会建立持仓。"
      : "样本不足；实验回测需要至少 35 根 Bar。",
  };
}

function createBacktestRunnerForEntry(
  entry: StrategyHistoryEntry,
  enabledStrategies: readonly ExpertStrategyId[],
): ExpertBacktestRunner {
  const strategyKey = [...new Set(enabledStrategies)].sort().join(":");
  const cached = entry.backtestRunners.get(strategyKey);
  if (cached) return cached;
  const { bars } = entry.context;
  const enabled = new Set(enabledStrategies.filter((strategyId) => (
    BACKTEST_ELIGIBLE_STRATEGIES.has(strategyId)
  )));
  const equityByIndex = new Float64Array(bars.length);
  const drawdownByIndex = new Float64Array(bars.length);
  const scoreByIndex = new Float64Array(bars.length);
  const tradeCountByIndex = new Uint32Array(bars.length);
  const winningTradeCountByIndex = new Uint32Array(bars.length);
  let position = 0;
  let entryPrice = 0;
  let equity = 1;
  let peak = 1;
  let maximumDrawdown = 0;
  let closedTrades = 0;
  let winningTrades = 0;
  let latestScore = 0;
  let completedIndex = Math.min(33, bars.length - 1);

  for (let index = 0; index <= completedIndex; index += 1) {
    equityByIndex[index] = 1;
  }

  const resultAt = (requestedIndex: number): ExpertBacktestResult => {
    if (bars.length === 0) return emptyBacktestResult(0, enabled.size);
    const requested = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
    if (requested < 0) return emptyBacktestResult(0, enabled.size);
    const available = Math.min(requested, completedIndex);
    const barCount = available + 1;
    if (barCount < 35 || enabled.size === 0) return emptyBacktestResult(barCount, enabled.size);
    const completeForRequest = completedIndex >= requested;
    const caveat = completeForRequest
      ? BACKTEST_CAVEAT
      : `回测索引构建中 ${barCount}/${requested + 1}；${BACKTEST_CAVEAT}`;
    const trades = tradeCountByIndex[available];
    return {
      barCount,
      tradeCount: trades,
      winRate: trades === 0 ? 0 : winningTradeCountByIndex[available] / trades * 100,
      totalReturnPercent: (equityByIndex[available] - 1) * 100,
      maxDrawdownPercent: drawdownByIndex[available] * 100,
      latestScore: scoreByIndex[available],
      caveat,
    };
  };

  const runner: ExpertBacktestRunner = {
    get totalBars() { return bars.length; },
    get completedIndex() { return completedIndex; },
    get done() { return completedIndex >= bars.length - 1 || enabled.size === 0; },
    advance(maxBars: number) {
      if (enabled.size === 0 || completedIndex >= bars.length - 1) return 0;
      const limit = Math.min(
        bars.length - 1,
        completedIndex + Math.max(1, Math.floor(maxBars)),
      );
      let processed = 0;
      for (let index = completedIndex + 1; index <= limit; index += 1) {
        const bar = bars[index];
        latestScore = evaluateExpertStrategiesAt(entry.context, enabled, index).compositeScore;
        const target = latestScore >= 0.2 ? 1 : latestScore <= -0.2 ? -1 : 0;
        const marketReturn = bar.close / bars[index - 1].close - 1;
        equity *= Math.max(0.01, 1 + position * marketReturn);
        if (target !== position) {
          equity *= 1 - Math.abs(target - position) * 0.0002;
          if (position !== 0 && entryPrice > 0) {
            const tradeReturn = (bar.close / entryPrice - 1) * position;
            closedTrades += 1;
            if (tradeReturn > 0) winningTrades += 1;
          }
          entryPrice = target === 0 ? 0 : bar.close;
          position = target;
        }
        peak = Math.max(peak, equity);
        maximumDrawdown = Math.max(maximumDrawdown, peak === 0 ? 0 : (peak - equity) / peak);
        equityByIndex[index] = equity;
        drawdownByIndex[index] = maximumDrawdown;
        scoreByIndex[index] = latestScore;
        tradeCountByIndex[index] = closedTrades;
        winningTradeCountByIndex[index] = winningTrades;
        completedIndex = index;
        processed += 1;
      }
      return processed;
    },
    resultAt,
  };
  entry.backtestRunners.set(strategyKey, runner);
  return runner;
}

export function createExpertBacktestRunner(
  candles: readonly Candle[],
  enabledStrategies: readonly ExpertStrategyId[],
  historyKey?: string,
): ExpertBacktestRunner {
  const view = strategyHistoryView(candles, historyKey);
  const runner = createBacktestRunnerForEntry(view.entry, enabledStrategies);
  if (view.offset === 0 && view.length === runner.totalBars) return runner;
  return {
    get totalBars() { return view.length; },
    get completedIndex() {
      return Math.min(view.length - 1, Math.max(-1, runner.completedIndex - view.offset));
    },
    get done() {
      return runner.done || runner.completedIndex >= view.offset + view.length - 1;
    },
    advance(maxBars: number) {
      return this.done ? 0 : runner.advance(maxBars);
    },
    resultAt(requestedIndex: number) {
      const viewIndex = Math.min(view.length - 1, Math.max(-1, Math.floor(requestedIndex)));
      return runner.resultAt(view.offset + viewIndex);
    },
  };
}

export function runExpertBacktest(
  candles: readonly Candle[],
  enabledStrategies: readonly ExpertStrategyId[],
  historyKey?: string,
): ExpertBacktestResult {
  const runner = createExpertBacktestRunner(candles, enabledStrategies, historyKey);
  while (!runner.done) runner.advance(2_048);
  return runner.resultAt(runner.totalBars - 1);
}
