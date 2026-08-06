import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type Time,
} from "lightweight-charts";

import { buildChartBars, formatBarCountdown, secondsUntilBarClose } from "./chartModel";
import type { Candle, HoverCandle } from "./types";

interface MarketChartProps {
  candles: Candle[];
  intervalMinutes: number;
  livePrice: number | null;
  observedAt: string | null;
  priceDigits: number;
  onHover: (value: HoverCandle | null) => void;
}

const UP_COLOR = "#e94357";
const DOWN_COLOR = "#35aa75";

export function MarketChart({
  candles,
  intervalMinutes,
  livePrice,
  observedAt,
  priceDigits,
  onHover,
}: MarketChartProps) {
  const rendererRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const dataLengthRef = useRef(0);
  const previousIntervalRef = useRef<number | null>(null);
  const previousLastTimeRef = useRef<number | null>(null);
  const followingRef = useRef(true);
  const returningRef = useRef(false);
  const returnTimerRef = useRef<number | null>(null);
  const [markerY, setMarkerY] = useState<number | null>(null);
  const [countdown, setCountdown] = useState("00:00:00");
  const [isFollowing, setIsFollowing] = useState(true);

  const bars = useMemo(
    () => buildChartBars(candles, intervalMinutes, livePrice, observedAt),
    [candles, intervalMinutes, livePrice, observedAt],
  );
  const latestBar = bars.at(-1) ?? null;
  const chartData = useMemo<CandlestickData<Time>[]>(
    () => bars.map((bar) => ({ ...bar, time: bar.time as Time })),
    [bars],
  );
  const liveColor = latestBar && latestBar.close >= latestBar.open ? UP_COLOR : DOWN_COLOR;

  const updateFollowing = useCallback((value: boolean) => {
    followingRef.current = value;
    setIsFollowing(value);
  }, []);

  useEffect(() => {
    const container = rendererRef.current;
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
        minimumWidth: 82,
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: false,
        rightOffset: 7,
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
      upColor: UP_COLOR,
      downColor: DOWN_COLOR,
      borderUpColor: UP_COLOR,
      borderDownColor: DOWN_COLOR,
      wickUpColor: UP_COLOR,
      wickDownColor: DOWN_COLOR,
      priceLineVisible: false,
      lastValueVisible: false,
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

    const handleLogicalRange = (range: LogicalRange | null) => {
      if (!range || returningRef.current || dataLengthRef.current === 0) return;
      updateFollowing(range.to >= dataLengthRef.current - 1.15);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleLogicalRange);

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    });
    resizeObserver.observe(container);
    return () => {
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      resizeObserver.disconnect();
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(handleLogicalRange);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [onHover, updateFollowing]);

  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;

    const previousInterval = previousIntervalRef.current;
    const previousLastTime = previousLastTimeRef.current;
    const nextLastTime = latestBar?.time ?? null;
    dataLengthRef.current = chartData.length;
    series.setData(chartData);

    if (chartData.length > 0 && (previousInterval === null || previousInterval !== intervalMinutes)) {
      chart.timeScale().fitContent();
      updateFollowing(true);
    } else if (
      chartData.length > 0 &&
      followingRef.current &&
      previousLastTime !== null &&
      nextLastTime !== null &&
      nextLastTime > previousLastTime
    ) {
      returningRef.current = true;
      chart.timeScale().scrollToRealTime();
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      returnTimerRef.current = window.setTimeout(() => {
        returningRef.current = false;
      }, 420);
    }

    previousIntervalRef.current = intervalMinutes;
    previousLastTimeRef.current = nextLastTime;
  }, [chartData, intervalMinutes, latestBar?.time, updateFollowing]);

  useEffect(() => {
    const refreshMarker = () => {
      setCountdown(formatBarCountdown(secondsUntilBarClose(intervalMinutes)));
      const series = seriesRef.current;
      if (!series || !latestBar) {
        setMarkerY(null);
        return;
      }
      const coordinate = series.priceToCoordinate(latestBar.close);
      setMarkerY((current) => {
        if (coordinate === null) return null;
        return current !== null && Math.abs(current - coordinate) < 0.25 ? current : coordinate;
      });
    };

    refreshMarker();
    const timer = window.setInterval(refreshMarker, 250);
    return () => window.clearInterval(timer);
  }, [intervalMinutes, latestBar]);

  const returnToRealtime = () => {
    const chart = chartRef.current;
    if (!chart) return;
    returningRef.current = true;
    updateFollowing(true);
    chart.timeScale().scrollToRealTime();
    if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
    returnTimerRef.current = window.setTimeout(() => {
      returningRef.current = false;
    }, 420);
  };

  const markerStyle = {
    "--live-y": `${markerY ?? 0}px`,
    "--live-color": liveColor,
  } as CSSProperties;

  return (
    <div className="market-chart">
      <div className="chart-renderer" ref={rendererRef} />
      {latestBar && markerY !== null ? (
        <div
          className="live-price-layer"
          style={markerStyle}
          aria-label={`当前价 ${latestBar.close.toFixed(priceDigits)}，本周期剩余 ${countdown}`}
        >
          <div className="live-price-line" />
          <div className="live-price-tag" key={latestBar.close.toFixed(priceDigits)}>
            <strong>{latestBar.close.toFixed(priceDigits)}</strong>
            <span>{countdown}</span>
          </div>
        </div>
      ) : null}
      {!isFollowing ? (
        <button type="button" className="return-live-button" onClick={returnToRealtime}>
          <span />回到实时
        </button>
      ) : null}
    </div>
  );
}
