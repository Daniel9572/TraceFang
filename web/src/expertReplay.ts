import type { Candle, ReplayFrameBounds } from "./types";

const REPLAY_TIME_ZONE = "Asia/Shanghai";
export const REPLAY_RATE_LABEL = "ReplayOriginal · 1× 原速";
export const REPLAY_DERIVED_DOMAIN_NOTICE = "回放未提供该历史域；当前实时派生数据已隔离。";

export type ReplayProjectionState = "live" | "stopped" | "playing" | "completed";

export interface ReplayStreamOptions {
  period: string;
  startSequence: number;
  endSequence: number;
}

export interface ReplayProjectionStart extends ReplayStreamOptions {
  candles: Candle[];
  price: null;
}
const REPLAY_TIME_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: REPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  fractionalSecondDigits: 3,
  hourCycle: "h23",
});

export function formatReplayTimecode(value: string | null): string {
  if (value === null) return "等待精确帧时间";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "等待精确帧时间";
  const parts = Object.fromEntries(
    REPLAY_TIME_FORMATTER.formatToParts(date).map((part) => [part.type, part.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}.${parts.fractionalSecond} · ${REPLAY_TIME_ZONE} · UTC+08:00`;
}

/**
 * A replay projector must always start empty at the first retained raw frame.
 * Current chart Bars are deliberately not accepted as input: even finalized
 * Bars may contain provider evidence that arrived after the replay boundary.
 */
export function createReplayProjectionStart(
  bounds: ReplayFrameBounds,
  period: string,
): ReplayProjectionStart | null {
  if (
    bounds.state !== "ready"
    || bounds.first_sequence === null
    || bounds.last_sequence === null
  ) return null;
  return {
    period,
    startSequence: bounds.first_sequence,
    endSequence: bounds.last_sequence,
    candles: [],
    price: null,
  };
}

export function replayStreamQuery(options: ReplayStreamOptions): string {
  return new URLSearchParams({
    period: options.period,
    start_sequence: String(options.startSequence),
    end_sequence: String(options.endSequence),
  }).toString();
}

/** Prevents a current live-only snapshot from crossing into replay decisions or UI. */
export function replaySafeLiveDerivedValue<T>(
  replayState: ReplayProjectionState,
  value: T | null,
): T | null {
  return replayState === "live" ? value : null;
}
