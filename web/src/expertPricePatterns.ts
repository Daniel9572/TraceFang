import type {
  ExpertPricePattern,
  ExpertPricePatternAnchor,
  ExpertPricePatternKind,
} from "./expertTypes";

export interface PricePatternInputBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

export interface ConfirmedSwingPoint extends ExpertPricePatternAnchor {
  kind: "high" | "low";
  confirmedAtIndex: number;
}

export interface PriceStructureSnapshot {
  support: number | null;
  resistance: number | null;
  swings: ConfirmedSwingPoint[];
  patterns: ExpertPricePattern[];
  latestActivePattern: ExpertPricePattern | null;
}

export interface PricePatternOptions {
  pivotRadius: number;
  lookbackBars: number;
  doubleMinimumSeparation: number;
  doubleMaximumSeparation: number;
  doubleConfirmationBars: number;
  doublePriceTolerancePercent: number;
  doublePriceToleranceAtr: number;
  minimumPatternHeightAtr: number;
  confirmationBufferAtr: number;
  twoBMaximumBars: number;
  twoBConfirmationBars: number;
  twoBMinimumBreachAtr: number;
  twoBMaximumBreachAtr: number;
  invalidationBufferAtr: number;
}

export const DEFAULT_PRICE_PATTERN_OPTIONS: PricePatternOptions = {
  pivotRadius: 2,
  lookbackBars: 160,
  doubleMinimumSeparation: 5,
  doubleMaximumSeparation: 80,
  doubleConfirmationBars: 40,
  doublePriceTolerancePercent: 0.004,
  doublePriceToleranceAtr: 0.5,
  minimumPatternHeightAtr: 1,
  confirmationBufferAtr: 0.05,
  twoBMaximumBars: 30,
  twoBConfirmationBars: 3,
  twoBMinimumBreachAtr: 0.05,
  twoBMaximumBreachAtr: 1.25,
  invalidationBufferAtr: 0.25,
};

function boundedEndIndex(bars: readonly PricePatternInputBar[], requestedIndex: number): number {
  return Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
}

function trueRangeAt(bars: readonly PricePatternInputBar[], index: number): number {
  const current = bars[index];
  if (index === 0) return Math.max(0, current.high - current.low);
  const previousClose = bars[index - 1].close;
  return Math.max(
    current.high - current.low,
    Math.abs(current.high - previousClose),
    Math.abs(current.low - previousClose),
  );
}

function averageTrueRangeAt(
  bars: readonly PricePatternInputBar[],
  endIndex: number,
  period = 14,
): number {
  const startIndex = Math.max(0, endIndex - period + 1);
  let sum = 0;
  for (let index = startIndex; index <= endIndex; index += 1) {
    sum += trueRangeAt(bars, index);
  }
  return Math.max(sum / Math.max(1, endIndex - startIndex + 1), Math.abs(bars[endIndex].close) * 1e-8);
}

export function confirmedSwingPointsAt(
  bars: readonly PricePatternInputBar[],
  requestedIndex: number,
  options: PricePatternOptions = DEFAULT_PRICE_PATTERN_OPTIONS,
): ConfirmedSwingPoint[] {
  const endIndex = boundedEndIndex(bars, requestedIndex);
  const radius = Math.max(1, Math.floor(options.pivotRadius));
  if (endIndex < radius * 2) return [];
  const firstIndex = Math.max(radius, endIndex - options.lookbackBars + 1);
  const lastIndex = endIndex - radius;
  const swings: ConfirmedSwingPoint[] = [];
  for (let index = firstIndex; index <= lastIndex; index += 1) {
    const current = bars[index];
    let isHigh = true;
    let isLow = true;
    for (let offset = 1; offset <= radius; offset += 1) {
      isHigh = isHigh
        && current.high > bars[index - offset].high
        && current.high >= bars[index + offset].high;
      isLow = isLow
        && current.low < bars[index - offset].low
        && current.low <= bars[index + offset].low;
    }
    if (isHigh) {
      swings.push({
        kind: "high",
        index,
        time: current.time,
        price: current.high,
        confirmedAtIndex: index + radius,
      });
    }
    if (isLow) {
      swings.push({
        kind: "low",
        index,
        time: current.time,
        price: current.low,
        confirmedAtIndex: index + radius,
      });
    }
  }
  return swings.sort((left, right) => left.index - right.index || left.kind.localeCompare(right.kind));
}

function patternLabel(kind: ExpertPricePatternKind): string {
  switch (kind) {
    case "double-bottom": return "W 底颈线突破";
    case "double-top": return "M 顶颈线跌破";
    case "two-b-bottom": return "2B 底部假突破";
    case "two-b-top": return "2B 顶部假突破";
  }
}

function patternAnchor(
  bars: readonly PricePatternInputBar[],
  index: number,
  price: number,
): ExpertPricePatternAnchor {
  return { index, time: bars[index].time, price };
}

function firstInvalidationIndex(
  bars: readonly PricePatternInputBar[],
  startIndex: number,
  endIndex: number,
  direction: "bullish" | "bearish",
  invalidationPrice: number,
): number | null {
  for (let index = startIndex; index <= endIndex; index += 1) {
    if (
      (direction === "bullish" && bars[index].close < invalidationPrice)
      || (direction === "bearish" && bars[index].close > invalidationPrice)
    ) {
      return index;
    }
  }
  return null;
}

function closesBeyondInvalidation(
  bar: PricePatternInputBar,
  direction: "bullish" | "bearish",
  invalidationPrice: number,
): boolean {
  return direction === "bullish"
    ? bar.close < invalidationPrice
    : bar.close > invalidationPrice;
}

function createPattern(
  bars: readonly PricePatternInputBar[],
  kind: ExpertPricePatternKind,
  first: ExpertPricePatternAnchor,
  neckline: ExpertPricePatternAnchor | null,
  second: ExpertPricePatternAnchor,
  confirmationIndex: number,
  triggerPrice: number,
  invalidationPrice: number,
  endIndex: number,
  confidence: number,
  evidence: string[],
): ExpertPricePattern {
  const direction = kind === "double-bottom" || kind === "two-b-bottom"
    ? "bullish" as const
    : "bearish" as const;
  const invalidatedAtIndex = firstInvalidationIndex(
    bars,
    confirmationIndex + 1,
    endIndex,
    direction,
    invalidationPrice,
  );
  return {
    id: `${kind}:${first.time}:${second.time}:${bars[confirmationIndex].time}`,
    kind,
    label: patternLabel(kind),
    direction,
    status: invalidatedAtIndex === null ? "confirmed" : "invalidated",
    first,
    neckline,
    second,
    confirmation: patternAnchor(bars, confirmationIndex, bars[confirmationIndex].close),
    triggerPrice,
    invalidationPrice,
    detectedAt: bars[confirmationIndex].time,
    invalidatedAt: invalidatedAtIndex === null ? null : bars[invalidatedAtIndex].time,
    confidence,
    evidence,
  };
}

function detectDoublePatterns(
  bars: readonly PricePatternInputBar[],
  endIndex: number,
  swings: readonly ConfirmedSwingPoint[],
  options: PricePatternOptions,
): ExpertPricePattern[] {
  const patterns: ExpertPricePattern[] = [];
  for (const swingKind of ["low", "high"] as const) {
    const sameKind = swings.filter((swing) => swing.kind === swingKind);
    for (let position = 1; position < sameKind.length; position += 1) {
      const first = sameKind[position - 1];
      const second = sameKind[position];
      const separation = second.index - first.index;
      if (
        separation < options.doubleMinimumSeparation
        || separation > options.doubleMaximumSeparation
        || second.confirmedAtIndex > endIndex
      ) {
        continue;
      }
      const averageExtreme = (first.price + second.price) / 2;
      const atr = averageTrueRangeAt(bars, second.index);
      const priceTolerance = Math.max(
        Math.abs(averageExtreme) * options.doublePriceTolerancePercent,
        atr * options.doublePriceToleranceAtr,
      );
      if (Math.abs(first.price - second.price) > priceTolerance) continue;

      let triggerPrice = swingKind === "low"
        ? Number.NEGATIVE_INFINITY
        : Number.POSITIVE_INFINITY;
      let triggerIndex = first.index;
      for (let index = first.index; index <= second.index; index += 1) {
        const candidate = swingKind === "low" ? bars[index].high : bars[index].low;
        if (
          (swingKind === "low" && candidate > triggerPrice)
          || (swingKind === "high" && candidate < triggerPrice)
        ) {
          triggerPrice = candidate;
          triggerIndex = index;
        }
      }
      const patternHeight = swingKind === "low"
        ? triggerPrice - averageExtreme
        : averageExtreme - triggerPrice;
      if (patternHeight < atr * options.minimumPatternHeightAtr) continue;

      const triggerBuffer = atr * options.confirmationBufferAtr;
      const direction = swingKind === "low" ? "bullish" as const : "bearish" as const;
      const invalidationPrice = swingKind === "low"
        ? Math.min(first.price, second.price) - atr * options.invalidationBufferAtr
        : Math.max(first.price, second.price) + atr * options.invalidationBufferAtr;
      let confirmationIndex: number | null = null;
      const confirmationEnd = Math.min(
        endIndex,
        second.confirmedAtIndex + options.doubleConfirmationBars,
      );
      for (let index = second.confirmedAtIndex; index <= confirmationEnd; index += 1) {
        if (closesBeyondInvalidation(bars[index], direction, invalidationPrice)) break;
        if (
          (swingKind === "low" && bars[index].close > triggerPrice + triggerBuffer)
          || (swingKind === "high" && bars[index].close < triggerPrice - triggerBuffer)
        ) {
          confirmationIndex = index;
          break;
        }
      }
      if (confirmationIndex === null) continue;
      const kind: ExpertPricePatternKind = swingKind === "low" ? "double-bottom" : "double-top";
      patterns.push(createPattern(
        bars,
        kind,
        first,
        patternAnchor(bars, triggerIndex, triggerPrice),
        second,
        confirmationIndex,
        triggerPrice,
        invalidationPrice,
        endIndex,
        0.68,
        [
          `双极值间隔 ${separation} Bar，价差 ${Math.abs(first.price - second.price).toFixed(2)}`,
          `颈线 ${triggerPrice.toFixed(2)}，收盘于确认缓冲外`,
          `第二极值在第 ${second.confirmedAtIndex} 根才完成右侧确认`,
        ],
      ));
    }
  }
  return patterns;
}

function detectTwoBPatterns(
  bars: readonly PricePatternInputBar[],
  endIndex: number,
  swings: readonly ConfirmedSwingPoint[],
  options: PricePatternOptions,
): ExpertPricePattern[] {
  const patterns: ExpertPricePattern[] = [];
  for (const prior of swings) {
    const direction = prior.kind === "low" ? "bullish" as const : "bearish" as const;
    const probeEnd = Math.min(endIndex, prior.confirmedAtIndex + options.twoBMaximumBars);
    for (let probeIndex = prior.confirmedAtIndex + 1; probeIndex <= probeEnd; probeIndex += 1) {
      const atr = averageTrueRangeAt(bars, probeIndex);
      const breach = direction === "bullish"
        ? prior.price - bars[probeIndex].low
        : bars[probeIndex].high - prior.price;
      if (
        breach < atr * options.twoBMinimumBreachAtr
        || breach > atr * options.twoBMaximumBreachAtr
      ) {
        continue;
      }
      const reclaimBuffer = atr * options.confirmationBufferAtr;
      const probePrice = direction === "bullish" ? bars[probeIndex].low : bars[probeIndex].high;
      const invalidationPrice = direction === "bullish"
        ? probePrice - atr * options.invalidationBufferAtr
        : probePrice + atr * options.invalidationBufferAtr;
      const confirmationEnd = Math.min(endIndex, probeIndex + options.twoBConfirmationBars);
      let confirmationIndex: number | null = null;
      for (let index = probeIndex; index <= confirmationEnd; index += 1) {
        if (closesBeyondInvalidation(bars[index], direction, invalidationPrice)) break;
        if (
          (direction === "bullish" && bars[index].close > prior.price + reclaimBuffer)
          || (direction === "bearish" && bars[index].close < prior.price - reclaimBuffer)
        ) {
          confirmationIndex = index;
          break;
        }
      }
      if (confirmationIndex === null) continue;
      const kind: ExpertPricePatternKind = direction === "bullish" ? "two-b-bottom" : "two-b-top";
      patterns.push(createPattern(
        bars,
        kind,
        prior,
        null,
        patternAnchor(bars, probeIndex, probePrice),
        confirmationIndex,
        prior.price,
        invalidationPrice,
        endIndex,
        0.64,
        [
          `先突破前${prior.kind === "low" ? "低" : "高"} ${prior.price.toFixed(2)}，幅度 ${(breach / atr).toFixed(2)} ATR`,
          `${confirmationIndex - probeIndex} Bar 内收盘重新回到前极值内侧`,
          `前摆动点在第 ${prior.confirmedAtIndex} 根已经确认，探测发生在其后`,
        ],
      ));
      break;
    }
  }
  return patterns;
}

export function priceStructureSnapshotAt(
  bars: readonly PricePatternInputBar[],
  requestedIndex: number,
  options: PricePatternOptions = DEFAULT_PRICE_PATTERN_OPTIONS,
): PriceStructureSnapshot {
  const endIndex = boundedEndIndex(bars, requestedIndex);
  if (endIndex < 0) {
    return { support: null, resistance: null, swings: [], patterns: [], latestActivePattern: null };
  }
  const swings = confirmedSwingPointsAt(bars, endIndex, options);
  const latestClose = bars[endIndex].close;
  const lows = swings.filter((swing) => swing.kind === "low");
  const highs = swings.filter((swing) => swing.kind === "high");
  const windowStart = Math.max(0, endIndex - options.lookbackBars + 1);
  let fallbackLow = Number.POSITIVE_INFINITY;
  let fallbackHigh = Number.NEGATIVE_INFINITY;
  for (let index = windowStart; index <= endIndex; index += 1) {
    fallbackLow = Math.min(fallbackLow, bars[index].low);
    fallbackHigh = Math.max(fallbackHigh, bars[index].high);
  }
  const support = [...lows].reverse().find((swing) => swing.price < latestClose)?.price ?? fallbackLow;
  const resistance = [...highs].reverse().find((swing) => swing.price > latestClose)?.price ?? fallbackHigh;
  const uniquePatterns = new Map<string, ExpertPricePattern>();
  for (const pattern of [
    ...detectDoublePatterns(bars, endIndex, swings, options),
    ...detectTwoBPatterns(bars, endIndex, swings, options),
  ]) {
    uniquePatterns.set(pattern.id, pattern);
  }
  const patterns = [...uniquePatterns.values()]
    .sort((left, right) => left.detectedAt - right.detectedAt || left.id.localeCompare(right.id))
    .slice(-12);
  const activePatterns = patterns.filter((pattern) => pattern.status === "confirmed");
  const latestDetectedAt = activePatterns.reduce(
    (latest, pattern) => Math.max(latest, pattern.detectedAt),
    Number.NEGATIVE_INFINITY,
  );
  const latestCandidates = activePatterns.filter((pattern) => pattern.detectedAt === latestDetectedAt);
  const latestDirections = new Set(latestCandidates.map((pattern) => pattern.direction));
  const latestActivePattern = latestDirections.size === 1
    ? [...latestCandidates].sort((left, right) => (
      right.confidence - left.confidence || left.id.localeCompare(right.id)
    ))[0] ?? null
    : null;
  return { support, resistance, swings, patterns, latestActivePattern };
}
