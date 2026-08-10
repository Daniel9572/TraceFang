import {
  averageTrueRangeAt,
  type TechnicalBar,
} from "./expertTechnical.ts";

export type ExpertMarketStructureEventKind =
  | "bullish-bos"
  | "bearish-bos"
  | "bullish-choch"
  | "bearish-choch"
  | "low-liquidity-sweep"
  | "high-liquidity-sweep";

export interface ExpertMarketStructureAnchor {
  index: number;
  time: number;
  price: number;
}

export interface ExpertMarketStructureEvent {
  id: string;
  kind: ExpertMarketStructureEventKind;
  label: string;
  direction: "bullish" | "bearish";
  status: "confirmed" | "invalidated";
  reference: ExpertMarketStructureAnchor;
  confirmation: ExpertMarketStructureAnchor;
  detectedAt: number;
  invalidatedAt: number | null;
  confidence: number;
  evidence: string[];
}

export interface ExpertSmartMoneySetup {
  direction: "bullish" | "bearish";
  confidence: number;
  detectedAt: number;
  sweep: ExpertMarketStructureEvent;
  structureShift: ExpertMarketStructureEvent;
  evidence: string[];
}

interface SwingPivot extends ExpertMarketStructureAnchor {
  kind: "high" | "low";
  confirmedIndex: number;
}

const SWING_RADIUS = 2;
const MAX_SCAN_BARS = 220;
export const DEFAULT_VISIBLE_MARKET_STRUCTURE_EVENTS = 16;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function confirmedPivots(
  bars: readonly TechnicalBar[],
  endIndex: number,
): SwingPivot[] {
  const pivots: SwingPivot[] = [];
  const startIndex = Math.max(SWING_RADIUS, endIndex - MAX_SCAN_BARS);
  for (let index = startIndex; index <= endIndex - SWING_RADIUS; index += 1) {
    const bar = bars[index];
    let high = true;
    let low = true;
    let strictHigh = false;
    let strictLow = false;
    for (let offset = -SWING_RADIUS; offset <= SWING_RADIUS; offset += 1) {
      if (offset === 0) continue;
      const neighbor = bars[index + offset];
      if (neighbor.high > bar.high) high = false;
      if (neighbor.high < bar.high) strictHigh = true;
      if (neighbor.low < bar.low) low = false;
      if (neighbor.low > bar.low) strictLow = true;
    }
    if (high && strictHigh) {
      pivots.push({
        kind: "high",
        index,
        confirmedIndex: index + SWING_RADIUS,
        time: bar.time,
        price: bar.high,
      });
    }
    if (low && strictLow) {
      pivots.push({
        kind: "low",
        index,
        confirmedIndex: index + SWING_RADIUS,
        time: bar.time,
        price: bar.low,
      });
    }
  }
  return pivots.sort((left, right) => left.index - right.index || left.kind.localeCompare(right.kind));
}

function trendBefore(pivots: readonly SwingPivot[]): "bullish" | "bearish" | "mixed" {
  const highs = pivots.filter((pivot) => pivot.kind === "high").slice(-2);
  const lows = pivots.filter((pivot) => pivot.kind === "low").slice(-2);
  if (highs.length < 2 || lows.length < 2) return "mixed";
  if (highs[1].price > highs[0].price && lows[1].price > lows[0].price) return "bullish";
  if (highs[1].price < highs[0].price && lows[1].price < lows[0].price) return "bearish";
  return "mixed";
}

function eventInvalidation(
  event: ExpertMarketStructureEvent,
  bars: readonly TechnicalBar[],
  endIndex: number,
): Pick<ExpertMarketStructureEvent, "status" | "invalidatedAt"> {
  for (let index = event.confirmation.index + 1; index <= endIndex; index += 1) {
    const atr = averageTrueRangeAt(bars, index) ?? Math.abs(bars[index].close) * 0.004;
    const invalidated = event.direction === "bullish"
      ? bars[index].close < event.reference.price - atr * 0.5
      : bars[index].close > event.reference.price + atr * 0.5;
    if (invalidated) {
      return { status: "invalidated", invalidatedAt: bars[index].time };
    }
  }
  return { status: "confirmed", invalidatedAt: null };
}

function eventLabel(kind: ExpertMarketStructureEventKind): string {
  if (kind === "bullish-bos" || kind === "bearish-bos") return "BOS";
  if (kind === "bullish-choch" || kind === "bearish-choch") return "CHOCH";
  return "SWEEP";
}

/**
 * Mechanical OHLC structure events often discussed under "smart money".
 * They describe observable swing breaks and failed excursions only; they do
 * not identify the trader behind an order or infer institutional intent.
 */
export function marketStructureEventsAt(
  bars: readonly TechnicalBar[],
  requestedIndex = bars.length - 1,
): ExpertMarketStructureEvent[] {
  const endIndex = Math.min(bars.length - 1, Math.max(-1, Math.floor(requestedIndex)));
  if (endIndex < 18) return [];
  const pivots = confirmedPivots(bars, endIndex);
  const events: ExpertMarketStructureEvent[] = [];
  const startIndex = Math.max(14, endIndex - MAX_SCAN_BARS);
  for (let index = startIndex; index <= endIndex; index += 1) {
    const available = pivots.filter((pivot) => pivot.confirmedIndex <= index && pivot.index < index);
    const latestHigh = available.filter((pivot) => pivot.kind === "high").at(-1);
    const latestLow = available.filter((pivot) => pivot.kind === "low").at(-1);
    const current = bars[index];
    const previous = bars[index - 1];
    const atr = averageTrueRangeAt(bars, index) ?? Math.abs(current.close) * 0.004;
    if (!(atr > 0)) continue;
    const structureTrend = trendBefore(available);
    const confirmation: ExpertMarketStructureAnchor = {
      index,
      time: current.time,
      price: current.close,
    };

    if (latestHigh) {
      const overshootAtr = (current.high - latestHigh.price) / atr;
      if (
        overshootAtr >= 0.08
        && overshootAtr <= 1.5
        && current.close < latestHigh.price - atr * 0.02
      ) {
        const base: ExpertMarketStructureEvent = {
          id: `sm:high-sweep:${latestHigh.time}:${current.time}`,
          kind: "high-liquidity-sweep",
          label: "SWEEP",
          direction: "bearish",
          status: "confirmed",
          reference: latestHigh,
          confirmation,
          detectedAt: current.time,
          invalidatedAt: null,
          confidence: clamp(0.56 + Math.min(0.18, overshootAtr * 0.12), 0, 0.78),
          evidence: [
            `上影越过已确认摆动高点 ${overshootAtr.toFixed(2)} ATR`,
            "收盘重新落回该高点下方",
          ],
        };
        events.push({ ...base, ...eventInvalidation(base, bars, endIndex) });
      }
      const breakThreshold = latestHigh.price + atr * 0.12;
      if (previous.close <= breakThreshold && current.close > breakThreshold) {
        const kind: ExpertMarketStructureEventKind = structureTrend === "bearish"
          ? "bullish-choch"
          : "bullish-bos";
        const base: ExpertMarketStructureEvent = {
          id: `sm:${kind}:${latestHigh.time}:${current.time}`,
          kind,
          label: eventLabel(kind),
          direction: "bullish",
          status: "confirmed",
          reference: latestHigh,
          confirmation,
          detectedAt: current.time,
          invalidatedAt: null,
          confidence: structureTrend === "mixed" ? 0.58 : 0.66,
          evidence: [
            `收盘超过已确认摆动高点 ${((current.close - latestHigh.price) / atr).toFixed(2)} ATR`,
            structureTrend === "bearish" ? "突破前为下降摆动结构" : "突破前未处于确认下降结构",
          ],
        };
        events.push({ ...base, ...eventInvalidation(base, bars, endIndex) });
      }
    }

    if (latestLow) {
      const overshootAtr = (latestLow.price - current.low) / atr;
      if (
        overshootAtr >= 0.08
        && overshootAtr <= 1.5
        && current.close > latestLow.price + atr * 0.02
      ) {
        const base: ExpertMarketStructureEvent = {
          id: `sm:low-sweep:${latestLow.time}:${current.time}`,
          kind: "low-liquidity-sweep",
          label: "SWEEP",
          direction: "bullish",
          status: "confirmed",
          reference: latestLow,
          confirmation,
          detectedAt: current.time,
          invalidatedAt: null,
          confidence: clamp(0.56 + Math.min(0.18, overshootAtr * 0.12), 0, 0.78),
          evidence: [
            `下影越过已确认摆动低点 ${overshootAtr.toFixed(2)} ATR`,
            "收盘重新站回该低点上方",
          ],
        };
        events.push({ ...base, ...eventInvalidation(base, bars, endIndex) });
      }
      const breakThreshold = latestLow.price - atr * 0.12;
      if (previous.close >= breakThreshold && current.close < breakThreshold) {
        const kind: ExpertMarketStructureEventKind = structureTrend === "bullish"
          ? "bearish-choch"
          : "bearish-bos";
        const base: ExpertMarketStructureEvent = {
          id: `sm:${kind}:${latestLow.time}:${current.time}`,
          kind,
          label: eventLabel(kind),
          direction: "bearish",
          status: "confirmed",
          reference: latestLow,
          confirmation,
          detectedAt: current.time,
          invalidatedAt: null,
          confidence: structureTrend === "mixed" ? 0.58 : 0.66,
          evidence: [
            `收盘跌破已确认摆动低点 ${((latestLow.price - current.close) / atr).toFixed(2)} ATR`,
            structureTrend === "bullish" ? "跌破前为上升摆动结构" : "跌破前未处于确认上升结构",
          ],
        };
        events.push({ ...base, ...eventInvalidation(base, bars, endIndex) });
      }
    }
  }
  return events;
}

export function recentMarketStructureEvents(
  events: readonly ExpertMarketStructureEvent[],
  limit = DEFAULT_VISIBLE_MARKET_STRUCTURE_EVENTS,
): ExpertMarketStructureEvent[] {
  const boundedLimit = Math.max(0, Math.floor(limit));
  return boundedLimit === 0 ? [] : events.slice(-boundedLimit);
}

export function latestSmartMoneySetup(
  events: readonly ExpertMarketStructureEvent[],
  latestBarIndex: number,
): ExpertSmartMoneySetup | null {
  const shifts = events.filter((event) => (
    event.status === "confirmed"
    && event.confirmation.index === latestBarIndex
    && (
      event.kind === "bullish-bos"
      || event.kind === "bearish-bos"
      || event.kind === "bullish-choch"
      || event.kind === "bearish-choch"
    )
  ));
  const structureShift = shifts.at(-1);
  if (!structureShift) return null;
  const sweep = [...events].reverse().find((event) => (
    event.status === "confirmed"
    && event.direction === structureShift.direction
    && (
      event.kind === "low-liquidity-sweep"
      || event.kind === "high-liquidity-sweep"
    )
    && event.confirmation.index < structureShift.confirmation.index
    && structureShift.confirmation.index - event.confirmation.index <= 8
  ));
  if (!sweep) return null;
  return {
    direction: structureShift.direction,
    confidence: clamp((sweep.confidence + structureShift.confidence) / 2 + 0.08, 0, 0.82),
    detectedAt: structureShift.detectedAt,
    sweep,
    structureShift,
    evidence: [
      `${sweep.label} 后 ${structureShift.confirmation.index - sweep.confirmation.index} 根 Bar 出现 ${structureShift.label}`,
      "只证明价格扫掠与结构变化，不证明机构订单身份",
    ],
  };
}
