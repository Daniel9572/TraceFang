import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  createChart,
  type AreaData,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LogicalRange,
  type Time,
} from "lightweight-charts";

import { buildChartBars, buildTimelineSeries, formatBarCountdown } from "./chartModel";
import { secondsUntilPeriodClose, type ChartPeriod } from "./chartPeriods";
import type { Candle, HoverCandle, TimelineSample } from "./types";

interface MarketChartProps {
  candles: Candle[];
  period: ChartPeriod;
  timelineSamples: TimelineSample[];
  livePrice: number | null;
  observedAt: string | null;
  referencePrice: number | null;
  timelineResolutionSeconds: number;
  priceDigits: number;
  onHover: (value: HoverCandle | null) => void;
}

const UP_COLOR = "#e94357";
const DOWN_COLOR = "#35aa75";

export function MarketChart({
  candles,
  period,
  timelineSamples,
  livePrice,
  observedAt,
  referencePrice,
  timelineResolutionSeconds,
  priceDigits,
  onHover,
}: MarketChartProps) {
  const rendererRef = useRef<HTMLDivElement>(null);
  const liveLayerRef = useRef<HTMLDivElement>(null);
  const countdownRef = useRef<HTMLSpanElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlestickSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const areaSeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const referenceLineRef = useRef<IPriceLine | null>(null);
  const markerFrameRef = useRef<number | null>(null);
  const latestPriceRef = useRef<number | null>(null);
  const dataLengthRef = useRef(0);
  const previousDataLengthRef = useRef(0);
  const previousPeriodRef = useRef<string | null>(null);
  const previousLastTimeRef = useRef<number | null>(null);
  const previousCandlesRef = useRef<Candle[] | null>(null);
  const followingRef = useRef(true);
  const returningRef = useRef(false);
  const returnTimerRef = useRef<number | null>(null);
  const [isFollowing, setIsFollowing] = useState(true);

  const bars = useMemo(
    () => buildChartBars(candles, period, timelineSamples, livePrice, observedAt),
    [candles, livePrice, observedAt, period, timelineSamples],
  );
  const latestBar = bars.at(-1) ?? null;
  latestPriceRef.current = livePrice;
  const chartData = useMemo<CandlestickData<Time>[]>(
    () => bars.map((bar) => ({ ...bar, time: bar.time as Time })),
    [bars],
  );
  const timelineData = useMemo<AreaData<Time>[]>(
    () => buildTimelineSeries(candles, timelineSamples, livePrice, observedAt)
      .map((point) => ({ time: point.time as Time, value: point.value })),
    [candles, livePrice, observedAt, timelineSamples],
  );
  const comparisonPrice = period.mode === "timeline" ? referencePrice : latestBar?.open ?? null;
  const liveColor = livePrice !== null && comparisonPrice !== null && livePrice >= comparisonPrice
    ? UP_COLOR
    : DOWN_COLOR;

  const refreshLiveMarker = useCallback(() => {
    const layer = liveLayerRef.current;
    if (!layer) return;

    const series = candlestickSeriesRef.current ?? areaSeriesRef.current;
    const price = latestPriceRef.current;
    if (!series || price === null) {
      layer.style.visibility = "hidden";
      return;
    }

    const coordinate = series.priceToCoordinate(price);
    if (coordinate === null || !Number.isFinite(coordinate)) {
      layer.style.visibility = "hidden";
      return;
    }

    const tagY = Math.min(Math.max(16, coordinate), Math.max(16, layer.clientHeight - 31));
    layer.style.setProperty("--live-y", `${coordinate.toFixed(2)}px`);
    layer.style.setProperty("--live-tag-y", `${tagY.toFixed(2)}px`);
    layer.style.visibility = "visible";
  }, []);

  const scheduleLiveMarker = useCallback(() => {
    if (markerFrameRef.current !== null) return;
    markerFrameRef.current = window.requestAnimationFrame(() => {
      markerFrameRef.current = null;
      refreshLiveMarker();
    });
  }, [refreshLiveMarker]);

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
        scaleMargins: { top: 0.17, bottom: 0.08 },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: true,
        secondsVisible: period.mode === "timeline" && timelineResolutionSeconds < 60,
        rightOffset: 7,
        barSpacing: period.mode === "timeline" ? 5 : 10,
        minBarSpacing: period.mode === "timeline" ? 1 : 3,
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
    let candlestickSeries: ISeriesApi<"Candlestick"> | null = null;
    let areaSeries: ISeriesApi<"Area"> | null = null;
    if (period.mode === "timeline") {
      areaSeries = chart.addSeries(AreaSeries, {
        lineColor: "#4e7deb",
        lineWidth: 2,
        topColor: "rgba(78, 125, 235, .20)",
        bottomColor: "rgba(78, 125, 235, .015)",
        crosshairMarkerVisible: true,
        crosshairMarkerRadius: 4,
        crosshairMarkerBorderColor: "#ffffff",
        crosshairMarkerBackgroundColor: "#4e7deb",
        priceLineVisible: false,
        lastValueVisible: false,
      });
      areaSeriesRef.current = areaSeries;
    } else {
      candlestickSeries = chart.addSeries(CandlestickSeries, {
        upColor: UP_COLOR,
        downColor: DOWN_COLOR,
        borderUpColor: UP_COLOR,
        borderDownColor: DOWN_COLOR,
        wickUpColor: UP_COLOR,
        wickDownColor: DOWN_COLOR,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      candlestickSeriesRef.current = candlestickSeries;
    }
    chartRef.current = chart;
    scheduleLiveMarker();

    chart.subscribeCrosshairMove((parameter) => {
      if (parameter.time === undefined) {
        onHover(null);
        return;
      }
      if (areaSeries) {
        const item = parameter.seriesData.get(areaSeries);
        if (!item || !("value" in item)) {
          onHover(null);
          return;
        }
        onHover({
          time: Number(parameter.time),
          open: item.value,
          high: item.value,
          low: item.value,
          close: item.value,
        });
      } else if (candlestickSeries) {
        const item = parameter.seriesData.get(candlestickSeries);
        if (!item || !("open" in item)) {
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
      }
    });

    const handleLogicalRange = (range: LogicalRange | null) => {
      scheduleLiveMarker();
      if (!range || returningRef.current || dataLengthRef.current === 0) return;
      updateFollowing(range.to >= dataLengthRef.current - 1.15);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleLogicalRange);

    const handleChartMotion = () => scheduleLiveMarker();
    container.addEventListener("pointermove", handleChartMotion, { passive: true });
    container.addEventListener("wheel", handleChartMotion, { passive: true });

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      scheduleLiveMarker();
    });
    resizeObserver.observe(container);
    return () => {
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      if (markerFrameRef.current !== null) window.cancelAnimationFrame(markerFrameRef.current);
      markerFrameRef.current = null;
      resizeObserver.disconnect();
      container.removeEventListener("pointermove", handleChartMotion);
      container.removeEventListener("wheel", handleChartMotion);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(handleLogicalRange);
      chart.remove();
      chartRef.current = null;
      candlestickSeriesRef.current = null;
      areaSeriesRef.current = null;
      referenceLineRef.current = null;
    };
  }, [onHover, period.mode, scheduleLiveMarker, timelineResolutionSeconds, updateFollowing]);

  useEffect(() => {
    const chart = chartRef.current;
    const candlestickSeries = candlestickSeriesRef.current;
    const areaSeries = areaSeriesRef.current;
    if (!chart || (!candlestickSeries && !areaSeries)) return;

    const previousPeriod = previousPeriodRef.current;
    const previousLastTime = previousLastTimeRef.current;
    const previousDataLength = previousDataLengthRef.current;
    const activeDataLength = period.mode === "timeline" ? timelineData.length : chartData.length;
    const timelineLastTime = timelineData.at(-1)?.time;
    const nextLastTime = period.mode === "timeline"
      ? typeof timelineLastTime === "number" ? timelineLastTime : null
      : latestBar?.time ?? null;
    dataLengthRef.current = activeDataLength;
    const canIncrementLastPoint = previousPeriod === period.id
      && previousCandlesRef.current === candles
      && previousDataLength > 0
      && activeDataLength >= previousDataLength
      && activeDataLength <= previousDataLength + 1
      && previousLastTime !== null
      && nextLastTime !== null
      && nextLastTime >= previousLastTime;
    const timelineLastPoint = timelineData.at(-1);
    const candleLastPoint = chartData.at(-1);
    if (areaSeries) {
      if (canIncrementLastPoint && timelineLastPoint) areaSeries.update(timelineLastPoint);
      else areaSeries.setData(timelineData);
    }
    if (candlestickSeries) {
      if (canIncrementLastPoint && candleLastPoint) candlestickSeries.update(candleLastPoint);
      else candlestickSeries.setData(chartData);
    }

    const historyWasPrepended = activeDataLength > previousDataLength && nextLastTime === previousLastTime;
    if (
      activeDataLength > 0 &&
      (previousPeriod === null || previousPeriod !== period.id || historyWasPrepended)
    ) {
      chart.timeScale().fitContent();
      updateFollowing(true);
    } else if (
      activeDataLength > 0 &&
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

    previousPeriodRef.current = period.id;
    previousLastTimeRef.current = nextLastTime;
    previousDataLengthRef.current = activeDataLength;
    previousCandlesRef.current = candles;
    scheduleLiveMarker();
  }, [candles, chartData, latestBar?.time, period.id, period.mode, scheduleLiveMarker, timelineData, updateFollowing]);

  useEffect(() => {
    const series = areaSeriesRef.current;
    if (!series) return;
    if (referencePrice === null || !Number.isFinite(referencePrice)) {
      if (referenceLineRef.current) series.removePriceLine(referenceLineRef.current);
      referenceLineRef.current = null;
      return;
    }
    if (referenceLineRef.current) {
      referenceLineRef.current.applyOptions({ price: referencePrice });
    } else {
      referenceLineRef.current = series.createPriceLine({
        price: referencePrice,
        color: "#aeb8c6",
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: false,
        title: "",
      });
    }
    scheduleLiveMarker();
  }, [period.mode, referencePrice, scheduleLiveMarker]);

  useEffect(() => {
    const refreshCountdown = () => {
      const value = formatBarCountdown(secondsUntilPeriodClose(period));
      if (countdownRef.current && countdownRef.current.textContent !== value) {
        countdownRef.current.textContent = value;
      }
      const layer = liveLayerRef.current;
      const price = latestPriceRef.current;
      if (layer && price !== null) {
        layer.setAttribute(
          "aria-label",
          `当前价 ${price.toFixed(priceDigits)}，本周期剩余 ${value}`,
        );
      }
    };

    refreshCountdown();
    const timer = window.setInterval(refreshCountdown, 250);
    return () => window.clearInterval(timer);
  }, [period, priceDigits]);

  const returnToRealtime = () => {
    const chart = chartRef.current;
    if (!chart) return;
    returningRef.current = true;
    updateFollowing(true);
    chart.timeScale().scrollToRealTime();
    scheduleLiveMarker();
    if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
    returnTimerRef.current = window.setTimeout(() => {
      returningRef.current = false;
    }, 420);
  };

  const renderedCountdown = formatBarCountdown(secondsUntilPeriodClose(period));
  const markerStyle = {
    "--live-color": liveColor,
  } as CSSProperties;

  return (
    <div
      className="market-chart"
      data-chart-mode={period.mode}
      data-chart-period={period.id}
    >
      <div className="chart-renderer" ref={rendererRef} />
      {(period.mode === "timeline" ? timelineData.length > 0 : latestBar) && livePrice !== null ? (
        <div
          ref={liveLayerRef}
          className="live-price-layer"
          style={markerStyle}
          aria-label={`当前价 ${livePrice.toFixed(priceDigits)}，本周期剩余 ${renderedCountdown}`}
        >
          <div className="live-price-line" />
          <div className="live-price-tag-track">
            <div className="live-price-tag" key={livePrice.toFixed(priceDigits)}>
              <strong>{livePrice.toFixed(priceDigits)}</strong>
              <span ref={countdownRef}>{renderedCountdown}</span>
            </div>
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
