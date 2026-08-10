import type { Candle } from "./types";
import type {
  ExpertEventAssessment,
  ExpertEventWindowReaction,
  ExpertMarketEvent,
} from "./expertTypes";

const SHOCK_WINDOWS = [60, 5 * 60, 30 * 60, 2 * 60 * 60] as const;
const REGIME_WINDOWS = [4 * 60 * 60, 24 * 60 * 60, 5 * 24 * 60 * 60, 20 * 24 * 60 * 60] as const;
const BASELINE_DAYS = 60;
const MIN_BASELINE_OBSERVATIONS = 5;
const SECONDS_PER_DAY = 24 * 60 * 60;

interface PricePoint {
  index: number;
  availableAt: number;
  close: number;
}

function epoch(value: string | null | undefined): number | null {
  if (!value) return null;
  const parsed = Date.parse(value) / 1_000;
  return Number.isFinite(parsed) ? parsed : null;
}

function number(value: number | string | null): number | null {
  if (value === null) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function intervalSeconds(candle: Candle): number {
  const parsed = Number(candle.interval);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 60;
}

function candleAvailableAt(candle: Candle): number | null {
  if (candle.state !== "final") {
    return epoch(candle.source.observed_at) ?? epoch(candle.source.received_at);
  }
  const rawEnd = candle.source.raw_payload?.bucket_end;
  const bucketEnd = typeof rawEnd === "string" ? epoch(rawEnd) : null;
  if (bucketEnd !== null) return bucketEnd;
  const finalizedAt = epoch(candle.finalized_at);
  if (finalizedAt !== null) return finalizedAt;
  const openTime = epoch(candle.open_time);
  return openTime === null ? null : openTime + intervalSeconds(candle);
}

function lowerBoundOpenTime(candles: readonly Candle[], target: number, maxIndex: number): number {
  let low = 0;
  let high = Math.min(candles.length, maxIndex + 1);
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2);
    const time = epoch(candles[middle].open_time) ?? Number.POSITIVE_INFINITY;
    if (time < target) low = middle + 1;
    else high = middle;
  }
  return low;
}

function priceAvailableAtOrBefore(
  candles: readonly Candle[],
  target: number,
  maxIndex: number,
): PricePoint | null {
  let index = Math.min(maxIndex, lowerBoundOpenTime(candles, target, maxIndex));
  if (index >= candles.length || (epoch(candles[index].open_time) ?? Infinity) >= target) index -= 1;
  for (; index >= 0; index -= 1) {
    const candle = candles[index];
    const availableAt = candleAvailableAt(candle);
    const close = number(candle.close);
    if (availableAt !== null && availableAt <= target && close !== null && close > 0) {
      return { index, availableAt, close };
    }
  }
  return null;
}

function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function robustZ(value: number, baseline: readonly number[]): number | null {
  if (baseline.length < MIN_BASELINE_OBSERVATIONS) return null;
  const center = median(baseline);
  if (center === null) return null;
  const deviation = median(baseline.map((sample) => Math.abs(sample - center)));
  if (deviation === null || deviation < 1e-9) return Math.abs(value - center) < 1e-9 ? 0 : null;
  return (value - center) / (1.4826 * deviation);
}

function returnPercent(start: PricePoint | null, end: PricePoint | null): number | null {
  if (start === null || end === null || start.close <= 0) return null;
  return (end.close / start.close - 1) * 100;
}

function historicalReturns(
  candles: readonly Candle[],
  eventTime: number,
  windowSeconds: number,
  maxIndex: number,
): number[] {
  const values: number[] = [];
  for (let day = 1; day <= BASELINE_DAYS; day += 1) {
    const sampleTime = eventTime - day * SECONDS_PER_DAY;
    const endTime = sampleTime + windowSeconds;
    if (endTime > eventTime) continue;
    const value = returnPercent(
      priceAvailableAtOrBefore(candles, sampleTime, maxIndex),
      priceAvailableAtOrBefore(candles, endTime, maxIndex),
    );
    if (value !== null) values.push(value);
  }
  return values;
}

function reactionForWindow(
  candles: readonly Candle[],
  eventTime: number,
  windowSeconds: number,
  evaluatedAt: number,
  maxIndex: number,
): ExpertEventWindowReaction | null {
  const endTime = eventTime + windowSeconds;
  if (endTime > evaluatedAt) return null;
  const value = returnPercent(
    priceAvailableAtOrBefore(candles, eventTime, maxIndex),
    priceAvailableAtOrBefore(candles, endTime, maxIndex),
  );
  if (value === null) return null;
  return {
    windowSeconds,
    returnPercent: value,
    robustZ: robustZ(value, historicalReturns(candles, eventTime, windowSeconds, maxIndex)),
  };
}

function volumeInWindow(
  candles: readonly Candle[],
  start: number,
  end: number,
  maxIndex: number,
): number | null {
  let total = 0;
  let observed = false;
  let index = Math.max(0, lowerBoundOpenTime(candles, start, maxIndex) - 1);
  for (; index <= maxIndex && index < candles.length; index += 1) {
    const candle = candles[index];
    const availableAt = candleAvailableAt(candle);
    if (availableAt === null || availableAt <= start) continue;
    if (availableAt > end) break;
    const value = number(candle.volume);
    if (value === null || value < 0) continue;
    total += value;
    observed = true;
  }
  return observed ? total : null;
}

function volumeRobustZ(
  candles: readonly Candle[],
  eventTime: number,
  evaluatedAt: number,
  maxIndex: number,
): number | null {
  const windowSeconds = 30 * 60;
  if (eventTime + windowSeconds > evaluatedAt) return null;
  const value = volumeInWindow(candles, eventTime, eventTime + windowSeconds, maxIndex);
  if (value === null) return null;
  const baseline: number[] = [];
  for (let day = 1; day <= BASELINE_DAYS; day += 1) {
    const start = eventTime - day * SECONDS_PER_DAY;
    const sample = volumeInWindow(candles, start, start + windowSeconds, maxIndex);
    if (sample !== null) baseline.push(sample);
  }
  return robustZ(value, baseline);
}

function boundedScoreFromZ(value: number): number {
  return Math.min(100, Math.max(0, Math.abs(value) * 25));
}

function weightedScore(components: ReadonlyArray<{ score: number; weight: number }>): number | null {
  const weight = components.reduce((total, component) => total + component.weight, 0);
  if (weight === 0) return null;
  return Math.round(
    components.reduce((total, component) => total + component.score * component.weight, 0) / weight,
  );
}

function scoreCoverage(components: ReadonlyArray<{ weight: number }>): number {
  return components.reduce((total, component) => total + component.weight, 0);
}

function observedDirection(
  reactions: readonly ExpertEventWindowReaction[],
): ExpertEventAssessment["observedDirection"] {
  const usable = reactions.filter((reaction) => reaction.robustZ !== null);
  const reference = usable.at(-1) ?? reactions.at(-1);
  if (!reference) return "unavailable";
  if (reference.robustZ !== null && Math.abs(reference.robustZ) < 0.75) return "neutral";
  if (Math.abs(reference.returnPercent) < 1e-6) return "neutral";
  return reference.returnPercent > 0 ? "bullish" : "bearish";
}

function confidence(
  shockCoverage: number,
  regimeCoverage: number,
  reactions: readonly ExpertEventWindowReaction[],
): ExpertEventAssessment["confidence"] {
  const strength = Math.max(
    0,
    ...reactions.map((reaction) => Math.abs(reaction.robustZ ?? 0)),
  );
  const coverage = Math.max(shockCoverage, regimeCoverage);
  if (reactions.length === 0) return "unavailable";
  if (coverage >= 70 && strength >= 2) return "high";
  if (coverage >= 50 && strength >= 1.25) return "medium";
  return "low";
}

function assessEvent(
  candles: readonly Candle[],
  event: ExpertMarketEvent,
  evaluatedAt: number,
  maxIndex: number,
): ExpertEventAssessment {
  const shockReactions = SHOCK_WINDOWS
    .map((window) => reactionForWindow(candles, event.time, window, evaluatedAt, maxIndex))
    .filter((value): value is ExpertEventWindowReaction => value !== null);
  const realizedZ = Math.max(
    ...shockReactions
      .map((reaction) => reaction.robustZ)
      .filter((value): value is number => value !== null)
      .map(Math.abs),
    Number.NEGATIVE_INFINITY,
  );
  const shockComponents: Array<{ score: number; weight: number }> = [];
  const evidence: string[] = [];
  if (Number.isFinite(realizedZ)) {
    shockComponents.push({ score: boundedScoreFromZ(realizedZ), weight: 35 });
    evidence.push("黄金价格异常波动");
  }
  const volumeZ = volumeRobustZ(candles, event.time, evaluatedAt, maxIndex);
  if (volumeZ !== null) {
    shockComponents.push({ score: boundedScoreFromZ(volumeZ), weight: 20 });
    evidence.push("成交活跃度（非净资金流）");
  }

  const regimeReactions = REGIME_WINDOWS
    .map((window) => reactionForWindow(candles, event.time, window, evaluatedAt, maxIndex))
    .filter((value): value is ExpertEventWindowReaction => value !== null);
  const regimeZ = regimeReactions
    .map((reaction) => reaction.robustZ)
    .filter((value): value is number => value !== null);
  const regimeComponents: Array<{ score: number; weight: number }> = [];
  if (regimeZ.length > 0) {
    const terminalDirection = Math.sign(regimeReactions.at(-1)?.returnPercent ?? 0);
    const aligned = regimeReactions.filter((reaction) => (
      terminalDirection !== 0 && Math.sign(reaction.returnPercent) === terminalDirection
    )).length;
    const consistency = aligned / regimeReactions.length;
    const rawPersistence = regimeZ.reduce(
      (total, value) => total + boundedScoreFromZ(value),
      0,
    ) / regimeZ.length;
    regimeComponents.push({ score: rawPersistence * (0.5 + consistency * 0.5), weight: 30 });
    evidence.push("黄金价格持续性");
  }
  if (event.flowDirection !== "unknown") {
    evidence.push(`已核验资金事实：${event.flowDirection}`);
  }
  if (event.releaseClusterId !== null) {
    evidence.push("同一事件簇不可做单因子归因");
  }

  const reactions = [...shockReactions, ...regimeReactions]
    .filter((reaction, index, values) => (
      values.findIndex((candidate) => candidate.windowSeconds === reaction.windowSeconds) === index
    ))
    .sort((left, right) => left.windowSeconds - right.windowSeconds);
  const shockCoverage = scoreCoverage(shockComponents);
  const regimeCoverage = scoreCoverage(regimeComponents);
  return {
    eventId: event.id,
    evaluatedAt,
    shockScore: weightedScore(shockComponents),
    shockCoverage,
    regimeScore: weightedScore(regimeComponents),
    regimeCoverage,
    observedDirection: observedDirection(shockReactions),
    confidence: confidence(shockCoverage, regimeCoverage, reactions),
    reactions,
    evidence,
  };
}

/**
 * Scores only facts that have occurred and only candle states available at the
 * requested cutoff. Missing dollar, real-yield, ETF, positioning and venue
 * evidence remains missing and is represented by the coverage percentages.
 */
export function buildExpertEventAssessments(
  candles: readonly Candle[],
  marketEvents: readonly ExpertMarketEvent[],
  evaluatedAt: number | null,
  maxIndex = candles.length - 1,
): ExpertEventAssessment[] {
  if (evaluatedAt === null || !Number.isFinite(evaluatedAt) || maxIndex < 0) return [];
  const boundedIndex = Math.min(maxIndex, candles.length - 1);
  return marketEvents
    .filter((event) => event.time <= evaluatedAt && event.sourcePublishedAt <= evaluatedAt)
    .map((event) => assessEvent(candles, event, evaluatedAt, boundedIndex));
}
