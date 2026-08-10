import type { Candle } from "./types";

const REPLAY_TIME_ZONE = "Asia/Shanghai";
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

function epochSeconds(value: string): number | null {
  const milliseconds = Date.parse(value);
  return Number.isFinite(milliseconds) ? milliseconds / 1_000 : null;
}

function intervalSeconds(value: Candle["interval"]): number | null {
  const numeric = typeof value === "number" ? value : Number(value);
  return Number.isFinite(numeric) && numeric > 0 ? numeric : null;
}

/**
 * Seeds replay with completed historical Bars only. The active Bar is omitted
 * because its final OHLC may contain evidence captured after the seek cursor.
 */
export function completedReplayHistory(candles: Candle[], cutoffSeconds: number): Candle[] {
  const completed: Candle[] = [];
  for (let index = 0; index < candles.length; index += 1) {
    const candle = candles[index];
    const open = epochSeconds(candle.open_time);
    if (open === null || open >= cutoffSeconds) continue;
    const nextOpen = index + 1 < candles.length
      ? epochSeconds(candles[index + 1].open_time)
      : null;
    const boundary = nextOpen ?? (
      intervalSeconds(candle.interval) === null
        ? null
        : open + (intervalSeconds(candle.interval) as number)
    );
    if (boundary !== null && boundary <= cutoffSeconds) completed.push(candle);
  }
  return completed;
}
