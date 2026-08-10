export const DEFAULT_RSI_PERIOD = 14;
export const RSI_OVERSOLD_THRESHOLD = 30;
export const RSI_OVERBOUGHT_THRESHOLD = 70;

export interface RsiInputBar {
  close: number;
}

export type RsiState = "oversold" | "neutral" | "overbought";

export type RsiSignalState =
  | "oversold-recovery"
  | "overbought-reversal"
  | "oversold"
  | "overbought"
  | "none";

export interface WilderRsiRuntime {
  readonly period: number;
  readonly averageGain: Array<number | null>;
  readonly averageLoss: Array<number | null>;
  readonly values: Array<number | null>;
}

export interface RsiSnapshot {
  period: number;
  value: number;
  state: RsiState;
  signal: RsiSignalState;
}

function checkedPeriod(period: number): number {
  if (!Number.isInteger(period) || period < 2) {
    throw new RangeError("RSI period must be an integer greater than or equal to 2");
  }
  return period;
}

export function createWilderRsiRuntime(period = DEFAULT_RSI_PERIOD): WilderRsiRuntime {
  return {
    period: checkedPeriod(period),
    averageGain: [],
    averageLoss: [],
    values: [],
  };
}

function rsiFromAverages(averageGain: number, averageLoss: number): number {
  if (averageGain === 0 && averageLoss === 0) return 50;
  if (averageLoss === 0) return 100;
  if (averageGain === 0) return 0;
  return 100 - 100 / (1 + averageGain / averageLoss);
}

/**
 * Synchronizes Wilder RSI after an append or an in-place historical revision.
 * `requestedStartIndex` is the first changed close. Values before that index
 * remain stable; a change inside the seed window deliberately rebuilds from 0.
 */
export function synchronizeWilderRsiRuntime(
  runtime: WilderRsiRuntime,
  bars: readonly RsiInputBar[],
  requestedStartIndex = 0,
): number {
  const period = checkedPeriod(runtime.period);
  const boundedStart = Math.min(
    bars.length,
    Math.max(0, Math.floor(requestedStartIndex)),
  );
  let startIndex = boundedStart <= period ? 0 : boundedStart;
  if (
    runtime.values.length < startIndex
    || runtime.averageGain.length < startIndex
    || runtime.averageLoss.length < startIndex
    || (startIndex > period && (
      runtime.averageGain[startIndex - 1] === null
      || runtime.averageLoss[startIndex - 1] === null
    ))
  ) {
    startIndex = 0;
  }

  runtime.values.length = startIndex;
  runtime.averageGain.length = startIndex;
  runtime.averageLoss.length = startIndex;
  if (bars.length === 0) return 0;

  if (startIndex === 0) {
    runtime.values.push(null);
    runtime.averageGain.push(null);
    runtime.averageLoss.push(null);
    startIndex = 1;
  }

  for (let index = startIndex; index < bars.length; index += 1) {
    const change = bars[index].close - bars[index - 1].close;
    const gain = Math.max(change, 0);
    const loss = Math.max(-change, 0);
    if (index < period) {
      runtime.averageGain.push(null);
      runtime.averageLoss.push(null);
      runtime.values.push(null);
      continue;
    }

    let averageGain: number;
    let averageLoss: number;
    if (index === period) {
      let gainSum = 0;
      let lossSum = 0;
      for (let seedIndex = 1; seedIndex <= period; seedIndex += 1) {
        const seedChange = bars[seedIndex].close - bars[seedIndex - 1].close;
        gainSum += Math.max(seedChange, 0);
        lossSum += Math.max(-seedChange, 0);
      }
      averageGain = gainSum / period;
      averageLoss = lossSum / period;
    } else {
      const previousAverageGain = runtime.averageGain[index - 1];
      const previousAverageLoss = runtime.averageLoss[index - 1];
      if (previousAverageGain === null || previousAverageLoss === null) {
        throw new Error("RSI runtime lost its Wilder smoothing seed");
      }
      averageGain = (previousAverageGain * (period - 1) + gain) / period;
      averageLoss = (previousAverageLoss * (period - 1) + loss) / period;
    }
    runtime.averageGain.push(averageGain);
    runtime.averageLoss.push(averageLoss);
    runtime.values.push(rsiFromAverages(averageGain, averageLoss));
  }
  return bars.length - (startIndex === 1 && boundedStart <= period ? 0 : startIndex);
}

export function rsiSnapshotAt(
  runtime: WilderRsiRuntime,
  requestedIndex: number,
  oversold = RSI_OVERSOLD_THRESHOLD,
  overbought = RSI_OVERBOUGHT_THRESHOLD,
): RsiSnapshot | null {
  if (!(oversold > 0 && oversold < overbought && overbought < 100)) {
    throw new RangeError("RSI thresholds must satisfy 0 < oversold < overbought < 100");
  }
  const index = Math.min(
    runtime.values.length - 1,
    Math.max(-1, Math.floor(requestedIndex)),
  );
  if (index < 0) return null;
  const value = runtime.values[index];
  if (value === null || value === undefined) return null;
  const previous = index > 0 ? runtime.values[index - 1] : null;
  const state: RsiState = value <= oversold
    ? "oversold"
    : value >= overbought ? "overbought" : "neutral";
  const signal: RsiSignalState = previous !== null && previous <= oversold && value > oversold
    ? "oversold-recovery"
    : previous !== null && previous >= overbought && value < overbought
      ? "overbought-reversal"
      : state === "oversold"
        ? "oversold"
        : state === "overbought" ? "overbought" : "none";
  return { period: runtime.period, value, state, signal };
}
