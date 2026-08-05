import { useEffect, useMemo, useRef } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from "lightweight-charts";

import type { Candle, HoverCandle } from "./types";

interface MarketChartProps {
  candles: Candle[];
  intervalMinutes: number;
  onHover: (value: HoverCandle | null) => void;
}

function valueOf(value: number | string): number {
  return Number(value);
}

function aggregate(candles: Candle[], minutes: number): CandlestickData<Time>[] {
  const bucketSeconds = minutes * 60;
  const rows = new Map<number, CandlestickData<Time>>();
  for (const candle of candles) {
    const epoch = Math.floor(new Date(candle.open_time).getTime() / 1000);
    const bucket = Math.floor(epoch / bucketSeconds) * bucketSeconds;
    const current = rows.get(bucket);
    const open = valueOf(candle.open);
    const high = valueOf(candle.high);
    const low = valueOf(candle.low);
    const close = valueOf(candle.close);
    if (!current) {
      rows.set(bucket, { time: bucket as Time, open, high, low, close });
    } else {
      current.high = Math.max(current.high, high);
      current.low = Math.min(current.low, low);
      current.close = close;
    }
  }
  return [...rows.values()].sort((left, right) => Number(left.time) - Number(right.time));
}

export function MarketChart({ candles, intervalMinutes, onHover }: MarketChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const data = useMemo(() => aggregate(candles, intervalMinutes), [candles, intervalMinutes]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#8f98a6",
        fontFamily: '"Segoe UI", "Microsoft YaHei UI", sans-serif',
        fontSize: 12,
      },
      grid: {
        vertLines: { color: "#f1f3f7" },
        horzLines: { color: "#eef1f5" },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#a8b2c2", width: 1, style: 2, labelBackgroundColor: "#526074" },
        horzLine: { color: "#a8b2c2", width: 1, style: 2, labelBackgroundColor: "#526074" },
      },
      rightPriceScale: {
        borderVisible: false,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 5,
        barSpacing: 10,
        minBarSpacing: 3,
        lockVisibleTimeRangeOnResize: true,
      },
      handleScroll: {
        mouseWheel: true,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: true,
        mouseWheel: true,
        pinch: true,
      },
      localization: { locale: "zh-CN" },
    });
    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#e94357",
      downColor: "#35aa75",
      borderUpColor: "#e94357",
      borderDownColor: "#35aa75",
      wickUpColor: "#e94357",
      wickDownColor: "#35aa75",
      priceLineVisible: true,
      priceLineColor: "#e94357",
      priceLineStyle: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    chart.subscribeCrosshairMove((parameter) => {
      const item = parameter.seriesData.get(series);
      if (!item || !("open" in item) || parameter.time === undefined) {
        onHover(null);
        return;
      }
      onHover({
        time: Number(parameter.time),
        open: item.open,
        high: item.high,
        low: item.low,
        close: item.close,
      });
    });

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    });
    resizeObserver.observe(container);
    return () => {
      resizeObserver.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [onHover]);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    series.setData(data);
    if (data.length > 0) chart.timeScale().fitContent();
  }, [data]);

  return <div className="chart-canvas" ref={containerRef} />;
}
