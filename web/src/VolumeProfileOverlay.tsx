import { useCallback, useEffect, useImperativeHandle, useRef, useState, type Ref, type RefObject, type PointerEvent } from "react";
import { GripVertical } from "lucide-react";
import type { IChartApi, ISeriesApi } from "lightweight-charts";
import { actualTimeForChinaAxis } from "./chartTimeAxis";
import { DEFAULT_VOLUME_PROFILE_SETTINGS, visibleVolumeProfile, volumeProfileSettings, type VolumeProfileSettings } from "./volumeProfile";
import type { Candle } from "./types";

export interface VolumeProfileHandle { refresh: () => void }

interface Props {
  ref: Ref<VolumeProfileHandle>;
  chartRef: RefObject<IChartApi | null>;
  mainSeries: () => ISeriesApi<"Candlestick"> | ISeriesApi<"Area"> | null;
  candles: readonly Candle[];
  settings?: VolumeProfileSettings;
  viewport: { width: number; height: number };
  priceDigits: number;
  through?: number | null;
  onChange?: (settings: Partial<VolumeProfileSettings>) => void;
}

type Profile = ReturnType<typeof visibleVolumeProfile>;
interface View {
  profile: Profile;
  width: number;
  height: number;
  rows: { top: number; height: number }[];
  pocY: number | null;
}

export function VolumeProfileOverlay({ ref, chartRef, mainSeries, candles, settings, viewport, priceDigits, through, onChange }: Props) {
  const config = volumeProfileSettings(settings);
  const [view, setView] = useState<View | null>(null);
  const [draftWidth, setDraftWidth] = useState<number | null>(null);
  const dragRef = useRef<{ pointerId: number; startX: number; width: number } | null>(null);
  const cacheRef = useRef<{ candles: readonly Candle[]; from: number; to: number; profile: Profile } | null>(null);
  const refresh = useCallback(() => {
    const chart = chartRef.current;
    const series = mainSeries();
    const range = chart?.timeScale().getVisibleRange();
    if (!chart || !series || !range || through === null) { setView(null); return; }
    const from = actualTimeForChinaAxis(Number(range.from));
    const to = Math.min(actualTimeForChinaAxis(Number(range.to)), through ?? Infinity);
    const cache = cacheRef.current;
    const profile = cache?.candles === candles && cache.from === from && cache.to === to
      ? cache.profile
      : visibleVolumeProfile(candles, from, to, 10 ** -priceDigits);
    cacheRef.current = { candles, from, to, profile };
    const rows = profile.rows.map((row) => {
      const top = series.priceToCoordinate(row.high);
      const bottom = series.priceToCoordinate(row.low);
      return { top: top === null || bottom === null ? -1000 : Math.min(top, bottom), height: top === null || bottom === null ? 0 : Math.abs(bottom - top) };
    });
    const peak = profile.pocIndex === null ? null : profile.rows[profile.pocIndex];
    const pocY = peak ? series.priceToCoordinate((peak.low + peak.high) / 2) : null;
    const width = chart.timeScale().width();
    const height = chart.panes()[0]?.getHeight() ?? 0;
    setView((previous) => previous?.profile === profile && previous.width === width && previous.height === height
      && previous.pocY === pocY && rows.every((row, index) => previous.rows[index]?.top === row.top && previous.rows[index]?.height === row.height)
      ? previous : { profile, width, height, rows, pocY });
  }, [candles, chartRef, mainSeries, priceDigits, through]);
  useImperativeHandle(ref, () => ({ refresh }), [refresh]);
  useEffect(() => {
    const frame = requestAnimationFrame(refresh);
    return () => cancelAnimationFrame(frame);
  }, [refresh, viewport]);

  const dragWidth = (event: PointerEvent) => volumeProfileSettings({ width: dragRef.current
    ? dragRef.current.width + dragRef.current.startX - event.clientX : config.width }).width;
  const finishDrag = (event: PointerEvent<HTMLButtonElement>, commit: boolean) => {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    const width = dragWidth(event);
    dragRef.current = null;
    setDraftWidth(null);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    if (commit) onChange?.({ width });
  };
  if (!view || view.width < 100 || view.height < 60) return null;
  const { profile } = view;
  const width = Math.min(draftWidth ?? config.width, view.width * 0.32);
  const maxVolume = profile.pocIndex === null ? 0 : profile.rows[profile.pocIndex].volume;
  const peak = profile.pocIndex === null ? null : profile.rows[profile.pocIndex];
  const provider = candles.at(-1)?.source.provider ?? "";
  const source = provider.startsWith("jin10") ? "金十" : provider === "tonghuashun_futures" ? "同花顺" : "当前来源";
  const detail = `当前可见区间 · 量覆盖 ${profile.volumeBarCount}/${profile.barCount} 根 K 线 · ${source}原始量，单位未核实。将当前周期 K 线总量按价格区间分摊，非真实逐价成交量。`;
  const noVolume = profile.volumeBarCount === 0 ? "暂无可用量" : "区间量为 0";
  return (
    <div className="chart-volume-profile" style={{ width: view.width, height: view.height }}
      data-volume-profile-bars={profile.barCount} data-volume-profile-coverage={profile.volumeBarCount}
      data-volume-profile-width={width} data-volume-profile-poc={peak ? (peak.low + peak.high) / 2 : undefined}
      aria-label={`成交量价格分布估算。${detail}`}>
      <svg width={view.width} height={view.height} aria-hidden="true">
        {profile.rows.map((row, index) => {
          const position = view.rows[index];
          const length = maxVolume > 0 ? row.volume / maxVolume * width : 0;
          return length > 0 && position.height > 0 ? <rect key={index}
            x={view.width - length} y={position.top + Math.min(0.6, position.height * 0.1)}
            width={length} height={Math.max(0.5, position.height - Math.min(1.2, position.height * 0.2))}
            className={index === profile.pocIndex ? "is-poc" : undefined}
            fillOpacity={Math.min(0.85, config.opacity * (index === profile.pocIndex ? 1.65 : 1))} /> : null;
        })}
        {view.pocY !== null && view.pocY > 32 && view.pocY < view.height - 8 && peak ? <g>
          <line x1={0} x2={view.width} y1={view.pocY} y2={view.pocY} />
          <text x={view.width - width - 5} y={view.pocY - 5} textAnchor="end">POC≈ {((peak.low + peak.high) / 2).toFixed(priceDigits)}</text>
        </g> : null}
      </svg>
      <div className="chart-volume-profile-caption" style={{ width }} title={detail}>
        <strong>量分布≈</strong>
        <span>{maxVolume > 0 ? `量覆盖 ${profile.volumeBarCount}/${profile.barCount}` : noVolume}</span>
      </div>
      {onChange ? <button type="button" className="chart-volume-profile-resize"
        style={{ left: view.width - width - 4 }} aria-label="拖动调整成交量分布宽度"
        title="左右拖动调整宽度 · ←/→微调 · Home复位 · Esc取消"
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          event.preventDefault(); event.stopPropagation(); event.currentTarget.focus();
          event.currentTarget.setPointerCapture(event.pointerId);
          dragRef.current = { pointerId: event.pointerId, startX: event.clientX, width };
          setDraftWidth(width);
        }}
        onPointerMove={(event) => {
          if (dragRef.current?.pointerId !== event.pointerId) return;
          event.stopPropagation(); setDraftWidth(dragWidth(event));
        }}
        onPointerUp={(event) => finishDrag(event, true)} onPointerCancel={(event) => finishDrag(event, false)}
        onLostPointerCapture={() => { dragRef.current = null; setDraftWidth(null); }}
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "Escape"].includes(event.key)) return;
          event.preventDefault(); event.stopPropagation();
          if (event.key === "Escape") {
            const pointerId = dragRef.current?.pointerId;
            dragRef.current = null; setDraftWidth(null);
            if (pointerId !== undefined && event.currentTarget.hasPointerCapture(pointerId)) event.currentTarget.releasePointerCapture(pointerId);
          } else onChange({ width: event.key === "Home" ? DEFAULT_VOLUME_PROFILE_SETTINGS.width
            : volumeProfileSettings({ width: width + (event.key === "ArrowLeft" ? 1 : -1) * (event.shiftKey ? 20 : 5) }).width });
        }}><GripVertical size={12} aria-hidden="true" /></button> : null}
    </div>
  );
}
