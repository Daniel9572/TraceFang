import type {
  ExpertAnalysisSnapshot,
  ExpertBacktestResult,
  ExpertSignal,
  ExpertStrategyDefinition,
  ExpertStrategyId,
  ExpertValueZone,
} from "./expertTypes";
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
}

interface StrategyHistoryView {
  entry: StrategyHistoryEntry;
  offset: number;
  length: number;
  revision: number;
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

export const EXPERT_INDICATOR_HISTORY_VERSION = "expert-indicators-v1";

const strategyHistoryByKey = new Map<string, StrategyHistoryEntry>();
const strategyHistoryByCandles = new WeakMap<readonly Candle[], StrategyHistoryView>();

export const EXPERT_STRATEGIES: ExpertStrategyDefinition[] = [
  {
    id: "structure",
    name: "价格结构",
    shortName: "结构",
    description: "趋势斜率、摆动高低点、M 顶与支撑压力",
    dataSource: "OHLC",
    evidenceMode: "native",
  },
  {
    id: "macd",
    name: "MACD 动量",
    shortName: "MACD",
    description: "12/26 EMA 与 9 周期信号线",
    dataSource: "收盘价",
    evidenceMode: "native",
  },
  {
    id: "kdj",
    name: "KDJ 摆动",
    shortName: "KDJ",
    description: "9 周期随机值与平滑动量",
    dataSource: "OHLC",
    evidenceMode: "native",
  },
  {
    id: "fair-value",
    name: "公允价值缺口",
    shortName: "FVG",
    description: "三根 K 线价格失衡区间",
    dataSource: "OHLC",
    evidenceMode: "native",
  },
  {
    id: "poc-proxy",
    name: "POC 价格密度",
    shortName: "POC≈",
    description: "由 K 线区间与可用总量构造，非逐价成交分布",
    dataSource: "OHLC + 可用总量",
    evidenceMode: "proxy",
  },
  {
    id: "order-flow-proxy",
    name: "3D 订单流代理",
    shortName: "FLOW≈",
    description: "以实体/振幅/总量估计压力，等待 L2 与主动买卖方向",
    dataSource: "OHLC + 可用总量",
    evidenceMode: "proxy",
  },
  {
    id: "volume-price",
    name: "量价确认",
    shortName: "量价",
    description: "仅在数据源提供成交量时启用",
    dataSource: "OHLC + 成交量",
    evidenceMode: "conditional",
  },
];

export const DEFAULT_EXPERT_STRATEGIES: ExpertStrategyId[] = [
  "structure",
  "macd",
  "kdj",
  "fair-value",
  "poc-proxy",
];

const ALL_EXPERT_STRATEGIES = new Set<ExpertStrategyId>(
  EXPERT_STRATEGIES.map((strategy) => strategy.id),
);

function finiteNumber(value: number | string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizedBars(candles: readonly Candle[]): { bars: NumericBar[]; signatures: string[] } {
  const rows: Array<{ bar: NumericBar; signature: string }> = [];
  let ordered = true;
  let previousTime = Number.NEGATIVE_INFINITY;
  for (const candle of candles) {
    const time = Date.parse(candle.open_time) / 1_000;
    const open = finiteNumber(candle.open);
    const high = finiteNumber(candle.high);
    const low = finiteNumber(candle.low);
    const close = finiteNumber(candle.close);
    if (![time, open, high, low, close].every((value) => value !== null && Number.isFinite(value))) {
      continue;
    }
    const bar = {
      time,
      open: open as number,
      high: high as number,
      low: low as number,
      close: close as number,
      volume: finiteNumber(candle.volume),
    };
    rows.push({
      bar,
      signature: [bar.time, bar.open, bar.high, bar.low, bar.close, bar.volume ?? ""].join("|"),
    });
    if (time < previousTime) ordered = false;
    previousTime = time;
  }
  if (!ordered) rows.sort((left, right) => left.bar.time - right.bar.time);
  return {
    bars: rows.map((row) => row.bar),
    signatures: rows.map((row) => row.signature),
  };
}

function emptyStrategyEvaluationContext(): StrategyEvaluationContext {
  return {
    bars: [],
    macd: { fast: [], slow: [], value: [], signal: [], histogram: [] },
    kdj: { k: [], d: [], j: [] },
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

function swingLevels(
  bars: readonly NumericBar[],
  endIndex: number,
): { support: number | null; resistance: number | null; mTop: boolean } {
  const window = bars.slice(Math.max(0, endIndex - 159), endIndex + 1);
  const highs: Array<{ price: number; index: number }> = [];
  const lows: Array<{ price: number; index: number }> = [];
  for (let index = 2; index < window.length - 2; index += 1) {
    const bar = window[index];
    if (bar.high >= Math.max(...window.slice(index - 2, index + 3).map((item) => item.high))) {
      highs.push({ price: bar.high, index });
    }
    if (bar.low <= Math.min(...window.slice(index - 2, index + 3).map((item) => item.low))) {
      lows.push({ price: bar.low, index });
    }
  }
  const last = window.at(-1)?.close ?? null;
  const support = last === null
    ? null
    : [...lows].reverse().find((value) => value.price < last)?.price ?? Math.min(...window.map((bar) => bar.low));
  const resistance = last === null
    ? null
    : [...highs].reverse().find((value) => value.price > last)?.price ?? Math.max(...window.map((bar) => bar.high));
  const recentPeaks = highs.slice(-3);
  let mTop = false;
  if (recentPeaks.length >= 2 && last !== null) {
    const left = recentPeaks.at(-2)!;
    const right = recentPeaks.at(-1)!;
    const averagePeak = (left.price + right.price) / 2;
    const between = window.slice(left.index, right.index + 1);
    const neckline = between.length > 0 ? Math.min(...between.map((bar) => bar.low)) : averagePeak;
    mTop = Math.abs(left.price - right.price) / averagePeak <= 0.004
      && averagePeak > neckline * 1.003
      && last < neckline;
  }
  return { support, resistance, mTop };
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
  const window = bars
    .slice(Math.max(0, endIndex - 23), endIndex + 1)
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
    const view = { entry, offset, length: normalized.bars.length, revision: entry.revision };
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
  const { bars, macd: macdSeries, kdj: kdjSeries } = context;
  const lastIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (lastIndex < 0) {
    return {
      asOf: null,
      signals: [],
      levels: [],
      valueZones: [],
      indicators: {
        macd: null,
        kdj: null,
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
  const structure = swingLevels(bars, lastIndex);
  const zones = fairValueZones(bars, lastIndex);
  const signals: ExpertSignal[] = [];

  if (enabled.has("structure") && slope !== null) {
    const direction = slope > 0.004 ? "bullish" : slope < -0.004 ? "bearish" : "neutral";
    signals.push(createSignal(
      "structure",
      structure.mTop ? "M 顶跌破颈线" : direction === "bullish" ? "结构保持上行" : direction === "bearish" ? "结构转弱" : "结构震荡",
      structure.mTop ? "最近双峰接近且价格已跌破峰间低点" : `近端回归斜率 ${slope.toFixed(3)}%/Bar`,
      structure.mTop ? "bearish" : direction,
      structure.mTop ? 0.84 : clamp(0.56 + Math.abs(slope) * 5, 0.56, 0.82),
      latest.time,
      [
        structure.support === null ? "支撑待确认" : `支撑 ${structure.support.toFixed(2)}`,
        structure.resistance === null ? "压力待确认" : `压力 ${structure.resistance.toFixed(2)}`,
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

  const actionable = signals.filter((signal) => signal.confidence > 0);
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
      ...(enabled.has("structure") && structure.support !== null ? [{
        id: "structure-support",
        price: structure.support,
        label: "结构支撑",
        tone: "support" as const,
        style: "dashed" as const,
      }] : []),
      ...(enabled.has("structure") && structure.resistance !== null ? [{
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
    ],
    valueZones: enabled.has("fair-value") ? zones : [],
    indicators: {
      macd: macdSnapshot,
      kdj: kdjSnapshot,
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
  const actionable = signals.filter((signal) => signal.confidence > 0);
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
        : level.id === "poc-proxy" ? enabled.has("poc-proxy") : true
    )),
    valueZones: enabled.has("fair-value") ? snapshot.valueZones : [],
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
  return snapshotForEnabledStrategies(
    view.entry,
    new Set(enabledStrategies),
    view.offset + Math.min(requestedIndex, view.length - 1),
  );
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
  const enabled = new Set(enabledStrategies);
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
        latestScore = snapshotForEnabledStrategies(entry, enabled, index).compositeScore;
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
