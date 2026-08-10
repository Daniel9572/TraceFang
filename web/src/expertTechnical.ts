import type {
  ExpertOverlaySeries,
  ExpertTrendLine,
  ExpertStrategyId,
} from "./expertTypes";
import type { Candle } from "./types";

export interface TechnicalBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface MovingAverageSnapshot {
  alignment: "bullish" | "bearish" | "mixed" | "insufficient";
  values: Array<{
    period: 20 | 60 | 120 | 250;
    value: number | null;
    slopePercent: number | null;
    distanceAtr: number | null;
    interaction: "support-test" | "resistance-test" | "break" | "none";
  }>;
}

export interface BollingerSnapshot {
  middle: number;
  upper: number;
  lower: number;
  bandwidth: number;
  bandwidthPercentile: number | null;
  position: number;
  state: "squeeze" | "expanding" | "normal";
}

export interface NineCountSnapshot {
  direction: "sell-setup" | "buy-setup" | "none";
  count: number;
  perfected: boolean;
  completedNow: boolean;
}

export interface MomentumSnapshot {
  score: number;
  availableHorizons: number;
  returns: Array<{ horizon: 20 | 60 | 120; percent: number | null }>;
}

const MA_PERIODS = [20, 60, 120, 250] as const;
const MOMENTUM_HORIZONS = [20, 60, 120] as const;
const MAX_OVERLAY_POINTS = 1_500;

export function latestFinalCandleIndex(candles: readonly Candle[]): number {
  for (let index = candles.length - 1; index >= 0; index -= 1) {
    if (candles[index].state === "final") return index;
  }
  return -1;
}

/**
 * Produces a compact content key for the confirmed prefix. The UI uses this to
 * keep the derived strategy model stable across provisional-tail quote ticks,
 * while still noticing a correction to any already-final candle in the prefix.
 */
export function candlePrefixRevisionKey(
  candles: readonly Candle[],
  requestedEndIndex: number,
): string {
  const endIndex = Math.min(candles.length - 1, Math.max(-1, Math.floor(requestedEndIndex)));
  let primary = 2_166_136_261;
  let secondary = 5381;
  const mix = (rawValue: unknown) => {
    const value = String(rawValue);
    for (let offset = 0; offset < value.length; offset += 1) {
      const code = value.charCodeAt(offset);
      primary = Math.imul(primary ^ code, 16_777_619) >>> 0;
      secondary = (Math.imul(secondary, 33) ^ code) >>> 0;
    }
    primary = Math.imul(primary ^ 31, 16_777_619) >>> 0;
    secondary = (Math.imul(secondary, 33) ^ 31) >>> 0;
  };
  for (let index = 0; index <= endIndex; index += 1) {
    const candle = candles[index];
    mix(candle.open_time);
    mix(candle.state);
    mix(candle.revision);
    mix(candle.open);
    mix(candle.high);
    mix(candle.low);
    mix(candle.close);
    mix(candle.volume);
  }
  return `${endIndex + 1}:${primary.toString(36)}:${secondary.toString(36)}`;
}

function finiteNumber(value: number | string | null | undefined): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function technicalBarsFromCandles(candles: readonly Candle[]): TechnicalBar[] {
  const byTime = new Map<number, TechnicalBar>();
  for (const candle of candles) {
    const time = Date.parse(candle.open_time) / 1_000;
    const open = finiteNumber(candle.open);
    const high = finiteNumber(candle.high);
    const low = finiteNumber(candle.low);
    const close = finiteNumber(candle.close);
    if (
      !Number.isFinite(time)
      || open === null
      || high === null
      || low === null
      || close === null
      || low > high
      || open < low
      || open > high
      || close < low
      || close > high
    ) {
      continue;
    }
    const volume = finiteNumber(candle.volume);
    byTime.set(time, {
      time,
      open,
      high,
      low,
      close,
      volume: volume !== null && volume >= 0 ? volume : null,
    });
  }
  return [...byTime.values()].sort((left, right) => left.time - right.time);
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function mean(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function standardDeviation(values: readonly number[]): number | null {
  const average = mean(values);
  if (average === null) return null;
  const variance = values.reduce((sum, value) => sum + (value - average) ** 2, 0) / values.length;
  return Math.sqrt(Math.max(0, variance));
}

export function simpleMovingAverageAt(
  bars: readonly TechnicalBar[],
  endIndex: number,
  period: number,
): number | null {
  if (period <= 0 || endIndex + 1 < period) return null;
  let sum = 0;
  for (let index = endIndex - period + 1; index <= endIndex; index += 1) {
    sum += bars[index].close;
  }
  return sum / period;
}

function trueRangeAt(bars: readonly TechnicalBar[], index: number): number {
  const current = bars[index];
  if (index <= 0) return current.high - current.low;
  const previousClose = bars[index - 1].close;
  return Math.max(
    current.high - current.low,
    Math.abs(current.high - previousClose),
    Math.abs(current.low - previousClose),
  );
}

export function averageTrueRangeAt(
  bars: readonly TechnicalBar[],
  endIndex: number,
  period = 14,
): number | null {
  if (endIndex < 0 || endIndex + 1 < period) return null;
  let sum = 0;
  for (let index = endIndex - period + 1; index <= endIndex; index += 1) {
    sum += trueRangeAt(bars, index);
  }
  const value = sum / period;
  return value > 0 && Number.isFinite(value) ? value : null;
}

export function movingAverageSnapshotAt(
  bars: readonly TechnicalBar[],
  requestedIndex: number,
): MovingAverageSnapshot | null {
  const endIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (endIndex < 19) return null;
  const latest = bars[endIndex];
  const previous = endIndex > 0 ? bars[endIndex - 1] : null;
  const atr = averageTrueRangeAt(bars, endIndex) ?? latest.close * 0.005;
  const values = MA_PERIODS.map((period) => {
    const value = simpleMovingAverageAt(bars, endIndex, period);
    const previousValue = previous === null
      ? null
      : simpleMovingAverageAt(bars, endIndex - 1, period);
    const slopeAnchorIndex = endIndex - 5;
    const anchor = slopeAnchorIndex >= 0
      ? simpleMovingAverageAt(bars, slopeAnchorIndex, period)
      : null;
    const slopePercent = value === null || anchor === null || anchor === 0
      ? null
      : (value / anchor - 1) * 100 / 5;
    const distanceAtr = value === null || atr <= 0 ? null : (latest.close - value) / atr;
    let interaction: MovingAverageSnapshot["values"][number]["interaction"] = "none";
    if (value !== null) {
      const buffer = atr * (period >= 120 ? 0.38 : 0.28);
      if (
        previous
        && previousValue !== null
        && previous.close >= previousValue
        && latest.close < value - buffer
      ) interaction = "break";
      else if (
        previous
        && previousValue !== null
        && previous.close <= previousValue
        && latest.close > value + buffer
      ) interaction = "break";
      else if (latest.close >= value && latest.low <= value + buffer) interaction = "support-test";
      else if (latest.close <= value && latest.high >= value - buffer) interaction = "resistance-test";
    }
    return { period, value, slopePercent, distanceAtr, interaction };
  });
  const available = values.filter((item): item is typeof item & { value: number } => item.value !== null);
  let alignment: MovingAverageSnapshot["alignment"] = "insufficient";
  if (available.length >= 2) {
    const orderedBullish = available.every((item, index) => (
      index === 0 || available[index - 1].value > item.value
    ));
    const orderedBearish = available.every((item, index) => (
      index === 0 || available[index - 1].value < item.value
    ));
    const slopesBullish = available.every((item) => item.slopePercent === null || item.slopePercent >= 0);
    const slopesBearish = available.every((item) => item.slopePercent === null || item.slopePercent <= 0);
    if (latest.close > available[0].value && orderedBullish && slopesBullish) alignment = "bullish";
    else if (latest.close < available[0].value && orderedBearish && slopesBearish) alignment = "bearish";
    else alignment = "mixed";
  }
  return { alignment, values };
}

function bollingerAt(
  bars: readonly TechnicalBar[],
  endIndex: number,
  period = 20,
  deviations = 2,
): Omit<BollingerSnapshot, "bandwidthPercentile" | "state"> | null {
  if (endIndex + 1 < period) return null;
  const closes = bars.slice(endIndex - period + 1, endIndex + 1).map((bar) => bar.close);
  const middle = mean(closes);
  const deviation = standardDeviation(closes);
  if (middle === null || deviation === null || middle === 0) return null;
  const upper = middle + deviation * deviations;
  const lower = middle - deviation * deviations;
  const halfWidth = Math.max(Number.EPSILON, (upper - lower) / 2);
  return {
    middle,
    upper,
    lower,
    bandwidth: (upper - lower) / Math.abs(middle),
    position: (bars[endIndex].close - middle) / halfWidth,
  };
}

export function bollingerSnapshotAt(
  bars: readonly TechnicalBar[],
  requestedIndex: number,
): BollingerSnapshot | null {
  const endIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  const current = bollingerAt(bars, endIndex);
  if (current === null) return null;
  const history: number[] = [];
  for (let index = Math.max(19, endIndex - 119); index <= endIndex; index += 1) {
    const value = bollingerAt(bars, index);
    if (value !== null) history.push(value.bandwidth);
  }
  const bandwidthPercentile = history.length < 20
    ? null
    : history.filter((value) => value <= current.bandwidth).length / history.length;
  const previous = bollingerAt(bars, endIndex - 1);
  const expanding = previous !== null && current.bandwidth > previous.bandwidth * 1.04;
  const state = bandwidthPercentile !== null && bandwidthPercentile <= 0.2
    ? "squeeze" as const
    : expanding && (bandwidthPercentile ?? 0.5) >= 0.45
      ? "expanding" as const
      : "normal" as const;
  return { ...current, bandwidthPercentile, state };
}

export function nineCountSnapshotAt(
  bars: readonly TechnicalBar[],
  requestedIndex: number,
): NineCountSnapshot | null {
  const endIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (endIndex < 4) return null;
  const comparison = Math.sign(bars[endIndex].close - bars[endIndex - 4].close);
  if (comparison === 0) {
    return { direction: "none", count: 0, perfected: false, completedNow: false };
  }
  let streak = 0;
  for (let index = endIndex; index >= 4; index -= 1) {
    const sign = Math.sign(bars[index].close - bars[index - 4].close);
    if (sign !== comparison) break;
    streak += 1;
  }
  const count = Math.min(9, streak);
  const completedNow = streak === 9;
  const direction = comparison > 0 ? "sell-setup" as const : "buy-setup" as const;
  let perfected = false;
  const completionIndex = endIndex - Math.max(0, streak - 9);
  if (streak >= 9 && completionIndex >= 3) {
    if (direction === "sell-setup") {
      perfected = Math.max(bars[completionIndex].high, bars[completionIndex - 1].high)
        >= Math.max(bars[completionIndex - 2].high, bars[completionIndex - 3].high);
    } else {
      perfected = Math.min(bars[completionIndex].low, bars[completionIndex - 1].low)
        <= Math.min(bars[completionIndex - 2].low, bars[completionIndex - 3].low);
    }
  }
  return { direction, count, perfected, completedNow };
}

function realizedVolatility(bars: readonly TechnicalBar[], endIndex: number, period = 20): number | null {
  if (endIndex < period) return null;
  const returns: number[] = [];
  for (let index = endIndex - period + 1; index <= endIndex; index += 1) {
    const previous = bars[index - 1].close;
    if (previous <= 0 || bars[index].close <= 0) return null;
    returns.push(Math.log(bars[index].close / previous));
  }
  return standardDeviation(returns);
}

export function momentumSnapshotAt(
  bars: readonly TechnicalBar[],
  requestedIndex: number,
): MomentumSnapshot | null {
  const endIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (endIndex < 20) return null;
  const volatility = realizedVolatility(bars, endIndex) ?? 0;
  const weights = [0.5, 0.3, 0.2] as const;
  let weightedScore = 0;
  let availableWeight = 0;
  const returns = MOMENTUM_HORIZONS.map((horizon, index) => {
    if (endIndex < horizon || bars[endIndex - horizon].close <= 0) {
      return { horizon, percent: null };
    }
    const raw = bars[endIndex].close / bars[endIndex - horizon].close - 1;
    const scale = volatility > 0 ? volatility * Math.sqrt(horizon) : Math.max(0.01, Math.abs(raw));
    const normalized = Math.tanh(raw / Math.max(0.0001, scale));
    weightedScore += normalized * weights[index];
    availableWeight += weights[index];
    return { horizon, percent: raw * 100 };
  });
  const availableHorizons = returns.filter((item) => item.percent !== null).length;
  if (availableHorizons === 0) return null;
  return {
    score: clamp(weightedScore / Math.max(Number.EPSILON, availableWeight), -1, 1),
    availableHorizons,
    returns,
  };
}

function rollingSmaPoints(
  bars: readonly TechnicalBar[],
  period: number,
  endIndex: number,
): Array<{ time: number; value: number }> {
  const startIndex = Math.max(0, endIndex - MAX_OVERLAY_POINTS + 1);
  const points: Array<{ time: number; value: number }> = [];
  let sum = 0;
  for (let index = 0; index <= endIndex; index += 1) {
    sum += bars[index].close;
    if (index >= period) sum -= bars[index - period].close;
    if (index >= period - 1 && index >= startIndex) {
      points.push({ time: bars[index].time, value: sum / period });
    }
  }
  return points;
}

function bollingerPoints(
  bars: readonly TechnicalBar[],
  endIndex: number,
): { middle: ExpertOverlaySeries["points"]; upper: ExpertOverlaySeries["points"]; lower: ExpertOverlaySeries["points"] } {
  const middle: ExpertOverlaySeries["points"] = [];
  const upper: ExpertOverlaySeries["points"] = [];
  const lower: ExpertOverlaySeries["points"] = [];
  const startIndex = Math.max(19, endIndex - MAX_OVERLAY_POINTS + 1);
  for (let index = startIndex; index <= endIndex; index += 1) {
    const value = bollingerAt(bars, index);
    if (value === null) continue;
    const time = bars[index].time;
    middle.push({ time, value: value.middle });
    upper.push({ time, value: value.upper });
    lower.push({ time, value: value.lower });
  }
  return { middle, upper, lower };
}

export function buildTechnicalOverlaySeries(
  candles: readonly Candle[],
  enabledStrategies: readonly ExpertStrategyId[],
  requestedIndex = candles.length - 1,
): ExpertOverlaySeries[] {
  const candleEndIndex = Math.min(candles.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (candleEndIndex < 0) return [];
  const bars = technicalBarsFromCandles(candles.slice(0, candleEndIndex + 1));
  const endIndex = bars.length - 1;
  if (endIndex < 0) return [];
  const enabled = new Set(enabledStrategies);
  const result: ExpertOverlaySeries[] = [];
  if (enabled.has("ma-structure")) {
    const colors = new Map<number, string>([
      [20, "#58c4dd"],
      [60, "#e7b65a"],
      [120, "#a58be2"],
      [250, "#df7185"],
    ]);
    for (const period of MA_PERIODS) {
      if (endIndex + 1 < period) continue;
      result.push({
        id: `ma-${period}`,
        label: `MA${period}`,
        color: colors.get(period) ?? "#d8e1e6",
        lineStyle: period >= 120 ? "dashed" : "solid",
        lineWidth: period >= 250 ? 2 : 1,
        points: rollingSmaPoints(bars, period, endIndex),
        lastValueVisible: true,
      });
    }
  }
  if (enabled.has("bollinger") && endIndex >= 19) {
    const points = bollingerPoints(bars, endIndex);
    result.push(
      {
        id: "bollinger-upper",
        label: "BOLL 上轨",
        color: "rgba(126, 154, 202, .76)",
        lineStyle: "dashed",
        lineWidth: 1,
        points: points.upper,
        lastValueVisible: false,
      },
      {
        id: "bollinger-middle",
        label: "BOLL 中轨",
        color: "rgba(211, 221, 229, .65)",
        lineStyle: "dotted",
        lineWidth: 1,
        points: points.middle,
        lastValueVisible: false,
      },
      {
        id: "bollinger-lower",
        label: "BOLL 下轨",
        color: "rgba(126, 154, 202, .76)",
        lineStyle: "dashed",
        lineWidth: 1,
        points: points.lower,
        lastValueVisible: false,
      },
    );
  }
  return result;
}

interface Pivot {
  index: number;
  time: number;
  price: number;
}

interface TrendCandidate {
  direction: "support" | "resistance";
  first: Pivot;
  second: Pivot;
  endPrice: number;
  status: ExpertTrendLine["status"];
  touchCount: number;
  quality: number;
  atrError: number;
  invalidatedAt: number | null;
  invalidatedIndex: number | null;
  invalidationReason: string | null;
}

function confirmedPivots(
  bars: readonly TechnicalBar[],
  endIndex: number,
  direction: "support" | "resistance",
): Pivot[] {
  const radius = 2;
  const result: Pivot[] = [];
  const startIndex = Math.max(radius, endIndex - 260);
  for (let index = startIndex; index <= endIndex - radius; index += 1) {
    const price = direction === "support" ? bars[index].low : bars[index].high;
    let pivot = true;
    for (let offset = -radius; offset <= radius; offset += 1) {
      if (offset === 0) continue;
      const compared = direction === "support" ? bars[index + offset].low : bars[index + offset].high;
      if (
        (direction === "support" && compared <= price)
        || (direction === "resistance" && compared >= price)
      ) {
        pivot = false;
        break;
      }
    }
    if (pivot) result.push({ index, time: bars[index].time, price });
  }
  return result.slice(-10);
}

function evaluateTrendCandidate(
  bars: readonly TechnicalBar[],
  endIndex: number,
  direction: "support" | "resistance",
  first: Pivot,
  second: Pivot,
): TrendCandidate | null {
  const span = second.index - first.index;
  if (span < 5) return null;
  const anchorAtr = averageTrueRangeAt(bars, second.index) ?? bars[second.index].close * 0.005;
  if (anchorAtr <= 0) return null;
  if (direction === "support" && second.price < first.price - anchorAtr * 0.4) return null;
  if (direction === "resistance" && second.price > first.price + anchorAtr * 0.4) return null;
  const slope = (second.price - first.price) / span;
  if (Math.abs(slope) > anchorAtr * 0.35) return null;
  const lineAt = (index: number) => first.price + slope * (index - first.index);
  let touchCount = 2;
  let normalizedError = 0;
  let errorSamples = 0;
  let consecutiveBreaches = 0;
  let invalidatedAt: number | null = null;
  let invalidatedIndex: number | null = null;
  let invalidationReason: string | null = null;
  for (let index = second.index + 1; index <= endIndex; index += 1) {
    const expected = lineAt(index);
    const atr = averageTrueRangeAt(bars, index) ?? anchorAtr;
    const tolerance = atr * 0.28;
    const observed = direction === "support" ? bars[index].low : bars[index].high;
    const distance = Math.abs(observed - expected);
    normalizedError += Math.min(2, distance / Math.max(atr, Number.EPSILON));
    errorSamples += 1;
    const onCorrectSide = direction === "support"
      ? bars[index].close >= expected - tolerance
      : bars[index].close <= expected + tolerance;
    if (distance <= tolerance && onCorrectSide) touchCount += 1;
    const breachDistance = direction === "support"
      ? expected - bars[index].close
      : bars[index].close - expected;
    if (breachDistance > tolerance) consecutiveBreaches += 1;
    else consecutiveBreaches = 0;
    if (invalidatedAt === null && (consecutiveBreaches >= 2 || breachDistance > atr * 0.75)) {
      invalidatedAt = bars[index].time;
      invalidatedIndex = index;
      invalidationReason = consecutiveBreaches >= 2
        ? "连续两根收盘越过趋势线及 ATR 缓冲"
        : "单根收盘突破超过 0.75 ATR";
    }
  }
  const atrError = errorSamples === 0 ? 0 : normalizedError / errorSamples;
  const durationScore = clamp((endIndex - first.index) / 120, 0, 1);
  const touchScore = clamp((touchCount - 2) / 4, 0, 1);
  const errorScore = 1 - clamp(atrError / 1.5, 0, 1);
  const quality = clamp(durationScore * 0.25 + touchScore * 0.5 + errorScore * 0.25, 0, 1);
  const status = invalidatedAt !== null
    ? "invalidated" as const
    : touchCount >= 3
      ? "tested" as const
      : quality >= 0.48 ? "confirmed" as const : "candidate" as const;
  const endPrice = lineAt(endIndex);
  if (!Number.isFinite(endPrice) || endPrice <= 0) return null;
  return {
    direction,
    first,
    second,
    endPrice,
    status,
    touchCount,
    quality,
    atrError,
    invalidatedAt,
    invalidatedIndex,
    invalidationReason,
  };
}

export function buildSmartTrendLines(
  candles: readonly Candle[],
  requestedIndex = candles.length - 1,
): ExpertTrendLine[] {
  const candleEndIndex = Math.min(candles.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (candleEndIndex < 0) return [];
  const bars = technicalBarsFromCandles(candles.slice(0, candleEndIndex + 1));
  const endIndex = bars.length - 1;
  if (endIndex < 12) return [];
  const result: TrendCandidate[] = [];
  for (const direction of ["support", "resistance"] as const) {
    const pivots = confirmedPivots(bars, endIndex, direction);
    for (let right = 1; right < pivots.length; right += 1) {
      for (let left = Math.max(0, right - 5); left < right; left += 1) {
        const candidate = evaluateTrendCandidate(
          bars,
          endIndex,
          direction,
          pivots[left],
          pivots[right],
        );
        if (candidate !== null) result.push(candidate);
      }
    }
  }
  const selected: TrendCandidate[] = [];
  for (const direction of ["support", "resistance"] as const) {
    const candidates = result.filter((item) => item.direction === direction);
    const active = candidates
      .filter((item) => item.invalidatedAt === null)
      .sort((left, right) => right.quality - left.quality || right.second.index - left.second.index)[0];
    const invalidated = candidates
      .filter((item) => item.invalidatedAt !== null)
      .sort((left, right) => (right.invalidatedAt ?? 0) - (left.invalidatedAt ?? 0))[0];
    if (active) selected.push(active);
    if (
      invalidated
      && invalidated.invalidatedIndex !== null
      && endIndex - invalidated.invalidatedIndex <= 80
    ) {
      selected.push(invalidated);
    }
  }
  return selected.map((candidate) => ({
    id: [
      "smart-trend",
      candidate.direction,
      candidate.first.time,
      candidate.second.time,
    ].join(":"),
    direction: candidate.direction,
    start: { time: candidate.first.time, price: candidate.first.price },
    anchor: { time: candidate.second.time, price: candidate.second.price },
    end: { time: bars[endIndex].time, price: candidate.endPrice },
    status: candidate.status,
    touchCount: candidate.touchCount,
    quality: candidate.quality,
    atrError: candidate.atrError,
    invalidatedAt: candidate.invalidatedAt,
    invalidationReason: candidate.invalidationReason,
  }));
}
