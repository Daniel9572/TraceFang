import type { Candle } from "./types.ts";

export interface VolumeProfileSettings {
  width: number;
  opacity: number;
}

export const DEFAULT_VOLUME_PROFILE_SETTINGS: VolumeProfileSettings = { width: 120, opacity: 0.3 };

export function volumeProfileSettings(value?: Partial<VolumeProfileSettings>): VolumeProfileSettings {
  const clamp = (value: number | undefined, fallback: number, min: number, max: number) =>
    typeof value === "number" && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
  return {
    width: Math.round(clamp(value?.width, DEFAULT_VOLUME_PROFILE_SETTINGS.width, 60, 240)),
    opacity: clamp(value?.opacity, DEFAULT_VOLUME_PROFILE_SETTINGS.opacity, 0.1, 0.65),
  };
}

/** Current-period OHLCV estimate; absent volume never becomes synthetic activity. */
export function visibleVolumeProfile(
  candles: readonly Candle[],
  from: number,
  to: number,
  tickSize = 0.01,
) {
  const bars = candles.flatMap((candle) => {
    const time = Date.parse(candle.open_time) / 1000;
    const [open, high, low, close] = [candle.open, candle.high, candle.low, candle.close].map(Number);
    if (time < from || time > to || ![time, open, high, low, close].every(Number.isFinite)
      || high < low || open < low || open > high || close < low || close > high) return [];
    const rawVolume = candle.volume === null || candle.volume === "" ? null : Number(candle.volume);
    const volume = rawVolume !== null && Number.isFinite(rawVolume) && rawVolume >= 0 ? rawVolume : null;
    return [{ low, high, volume }];
  });
  const volumeBars = bars.filter((bar) => bar.volume !== null);
  const totalVolume = volumeBars.reduce((sum, bar) => sum + bar.volume!, 0);
  const result = { barCount: bars.length, volumeBarCount: volumeBars.length, totalVolume };
  if (bars.length === 0 || totalVolume <= 0) return { ...result, rows: [], pocIndex: null };

  let low = bars.reduce((min, bar) => Math.min(min, bar.low), Infinity);
  let high = bars.reduce((max, bar) => Math.max(max, bar.high), -Infinity);
  if (high === low) {
    const padding = Number.isFinite(tickSize) && tickSize > 0 ? tickSize / 2 : 0.005;
    low -= padding;
    high += padding;
  }
  const step = (high - low) / 32;
  const rows = Array.from({ length: 32 }, (_, index) => ({
    low: low + step * index,
    high: low + step * (index + 1),
    volume: 0,
  }));
  for (const bar of volumeBars) {
    if (bar.volume === 0) continue;
    if (bar.high === bar.low) {
      const index = Math.max(0, Math.min(31, Math.floor((bar.low - low) / step)));
      rows[index].volume += bar.volume!;
      continue;
    }
    for (const row of rows) {
      const overlap = Math.max(0, Math.min(row.high, bar.high) - Math.max(row.low, bar.low));
      row.volume += bar.volume! * overlap / (bar.high - bar.low);
    }
  }
  const pocIndex = rows.reduce((peak, row, index) => row.volume > rows[peak].volume ? index : peak, 0);
  return { ...result, rows, pocIndex };
}
