import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineStyle,
  TickMarkType,
  createChart,
  type AreaData,
  type CandlestickData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type LogicalRange,
  type Time,
} from "lightweight-charts";

import { barsFromCandles, buildTimelineSeries, formatBarCountdown } from "./chartModel";
import { secondsUntilPeriodClose, type ChartPeriod } from "./chartPeriods";
import {
  DATE_ONLY_AXIS_THRESHOLD_SECONDS,
  actualTimeForChinaAxis,
  actualTimeForTimelineChartTime,
  buildTimelineLayout,
  buildTimelineSessionGaps,
  formatBeijingDateTime,
  formatChartTick,
  formatCrosshairTime,
  formatSessionGapDuration,
  formatTimelineTick,
  projectTimeForChinaAxis,
  projectTimelineSeries,
  type TimelineLayout,
  type TimelineSessionGap,
} from "./chartTimeAxis";
import { prependedPointCount, shouldRequestOlderHistory } from "./historyLoading";
import type { Candle, HoverCandle, MarketPhase, MarketSchedule, TimelineSample } from "./types";

interface MarketChartProps {
  candles: Candle[];
  period: ChartPeriod;
  timelineSamples: TimelineSample[];
  livePrice: number | null;
  observedAt: string | null;
  referencePrice: number | null;
  timelineResolutionSeconds: number;
  priceDigits: number;
  marketPhase: MarketPhase;
  marketSchedule: MarketSchedule | null | undefined;
  historyLoading: boolean;
  onRequestOlderHistory: () => void;
  onHover: (value: HoverCandle | null) => void;
}

const UP_COLOR = "#e94357";
const DOWN_COLOR = "#35aa75";

function secondsUntilBackendBarClose(candles: Candle[], period: ChartPeriod): number {
  const value = candles.at(-1)?.source.raw_payload?.bucket_end;
  if (typeof value === "string") {
    const closeMilliseconds = Date.parse(value);
    if (Number.isFinite(closeMilliseconds)) {
      return Math.max(0, Math.ceil((closeMilliseconds - Date.now()) / 1_000));
    }
  }
  return secondsUntilPeriodClose(period);
}

function formatSignedValue(value: number, digits: number): string {
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${value.toFixed(digits)}`;
}

function sessionGapDetail(gap: TimelineSessionGap, priceDigits: number): string {
  if (gap.boundaryState !== "complete") return "边界行情待补齐";
  if (gap.direction === "flat") return "近似平开";
  if (gap.priceDifference === null || gap.pricePercent === null) return "边界行情待补齐";
  return `跳空 ${formatSignedValue(gap.priceDifference, priceDigits)} (${formatSignedValue(gap.pricePercent, 2)}%)`;
}

function sessionGapBoundaryDetail(gap: TimelineSessionGap): string {
  switch (gap.boundaryState) {
    case "missing-close": return "收市行情待补齐";
    case "missing-open": return "开盘行情待补齐";
    case "missing-both": return "收开盘行情待补齐";
    default: return "收开盘边界完整";
  }
}

function sessionGapDescription(gap: TimelineSessionGap, priceDigits: number): string {
  const closedKind = gap.kind === "weekend" ? "周末休市" : "例行休市";
  return [
    `${formatBeijingDateTime(gap.closedAt)} 收市`,
    `${formatBeijingDateTime(gap.openedAt)} 开盘`,
    `${closedKind} ${formatSessionGapDuration(gap.durationSeconds)}`,
    gap.boundaryState === "complete"
      ? sessionGapDetail(gap, priceDigits)
      : sessionGapBoundaryDetail(gap),
  ].join("；");
}

export function MarketChart({
  candles,
  period,
  timelineSamples,
  livePrice,
  observedAt,
  referencePrice,
  timelineResolutionSeconds,
  priceDigits,
  marketPhase,
  marketSchedule,
  historyLoading,
  onRequestOlderHistory,
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
  const timelineDecorationFrameRef = useRef<number | null>(null);
  const latestPriceRef = useRef<number | null>(null);
  const dataLengthRef = useRef(0);
  const latestLogicalIndexRef = useRef(-1);
  const previousDataLengthRef = useRef(0);
  const previousSeriesDataLengthRef = useRef(0);
  const previousPeriodRef = useRef<string | null>(null);
  const previousLastTimeRef = useRef<number | null>(null);
  const previousFirstTimeRef = useRef<number | null>(null);
  const previousCandlesRef = useRef<Candle[] | null>(null);
  const followingRef = useRef(true);
  const returningRef = useRef(false);
  const returnTimerRef = useRef<number | null>(null);
  const historyInteractionUntilRef = useRef(0);
  const historyLoadingRef = useRef(historyLoading);
  const requestOlderHistoryRef = useRef(onRequestOlderHistory);
  const tradingDayLayerRef = useRef<HTMLDivElement>(null);
  const timelineLayoutRef = useRef<TimelineLayout>({ days: [] });
  const timelineSessionGapsRef = useRef<TimelineSessionGap[]>([]);
  const timelineActualTimeRef = useRef<Map<number, number>>(new Map());
  const timelineIntradayAxisRef = useRef(false);
  const visibleCandleSpanRef = useRef(0);
  const candleDateOnlyAxisRef = useRef(false);
  const periodRef = useRef(period);
  const timelineResolutionSecondsRef = useRef(timelineResolutionSeconds);
  historyLoadingRef.current = historyLoading;
  requestOlderHistoryRef.current = onRequestOlderHistory;
  periodRef.current = period;
  timelineResolutionSecondsRef.current = timelineResolutionSeconds;
  const [isFollowing, setIsFollowing] = useState(true);

  const bars = useMemo(
    () => barsFromCandles(candles),
    [candles],
  );
  const latestBar = bars.at(-1) ?? null;
  latestPriceRef.current = livePrice;
  const chartData = useMemo<CandlestickData<Time>[]>(
    () => bars.map((bar) => ({
      ...bar,
      time: projectTimeForChinaAxis(bar.time) as Time,
    })),
    [bars],
  );
  const rawTimelineData = useMemo(
    () => buildTimelineSeries(candles, timelineSamples, livePrice, observedAt),
    [candles, livePrice, observedAt, timelineSamples],
  );
  const observedEpoch = observedAt ? Date.parse(observedAt) / 1_000 : null;
  const timelineLayout = useMemo(() => {
    const actualTimes = rawTimelineData
      .map((point) => point.observedTime ?? point.time)
      .filter(Number.isFinite);
    if (observedEpoch !== null && Number.isFinite(observedEpoch)) actualTimes.push(observedEpoch);
    return buildTimelineLayout(actualTimes, marketSchedule);
  }, [marketSchedule, observedEpoch, rawTimelineData]);
  timelineLayoutRef.current = timelineLayout;
  const projectedTimelineData = useMemo(
    () => projectTimelineSeries(rawTimelineData, timelineLayout),
    [rawTimelineData, timelineLayout],
  );
  const timelineSessionGaps = useMemo(
    () => buildTimelineSessionGaps(timelineLayout, projectedTimelineData, priceDigits),
    [priceDigits, projectedTimelineData, timelineLayout],
  );
  timelineSessionGapsRef.current = timelineSessionGaps;
  const timelineData = useMemo<AreaData<Time>[]>(
    () => projectedTimelineData.map((point) => ({ time: point.time as Time, value: point.value })),
    [projectedTimelineData],
  );
  timelineActualTimeRef.current = new Map(
    projectedTimelineData.map((point) => [point.time, point.actualTime]),
  );
  const latestTimelineLogicalIndex = projectedTimelineData.length > 0
    ? projectedTimelineData.length - 1
    : null;
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

  const axisTickFormatter = useCallback((time: Time, tickMarkType: TickMarkType) => {
    const chartTime = Number(time);
    const activePeriod = periodRef.current;
    return activePeriod.mode === "timeline"
      ? formatTimelineTick(
        chartTime,
        tickMarkType,
        timelineLayoutRef.current,
        timelineIntradayAxisRef.current,
      )
      : formatChartTick(
        actualTimeForChinaAxis(chartTime),
        tickMarkType,
        activePeriod,
        visibleCandleSpanRef.current,
      );
  }, []);

  const axisTimeFormatter = useCallback((time: Time) => {
    const chartTime = Number(time);
    const activePeriod = periodRef.current;
    if (activePeriod.mode === "timeline") {
      return formatCrosshairTime(
        chartTime,
        activePeriod,
        timelineLayoutRef.current,
        timelineResolutionSecondsRef.current,
        timelineActualTimeRef.current.get(chartTime),
      );
    }
    return formatCrosshairTime(
      actualTimeForChinaAxis(chartTime),
      activePeriod,
      timelineLayoutRef.current,
    );
  }, []);

  const refreshTimelineDecorations = useCallback(() => {
    if (timelineDecorationFrameRef.current !== null) return;
    timelineDecorationFrameRef.current = window.requestAnimationFrame(() => {
      timelineDecorationFrameRef.current = null;
      const chart = chartRef.current;
      const layer = tradingDayLayerRef.current;
      const activePeriod = periodRef.current;
      if (!chart || !layer || activePeriod.mode !== "timeline") {
        layer?.replaceChildren();
        return;
      }

      const layout = timelineLayoutRef.current;
      const visibleRange = chart.timeScale().getVisibleRange();
      const visibleFrom = visibleRange ? Number(visibleRange.from) : Number.NEGATIVE_INFINITY;
      const visibleTo = visibleRange ? Number(visibleRange.to) : Number.POSITIVE_INFINITY;
      const tradingDays = [...layout.days.reduce((groups, day) => {
        const current = groups.get(day.key);
        groups.set(day.key, current
          ? { ...current, chartEnd: Math.max(current.chartEnd, day.chartEnd) }
          : day);
        return groups;
      }, new Map<string, TimelineLayout["days"][number]>()).values()];
      const visibleDays = tradingDays.filter((day) => (
        day.chartEnd >= visibleFrom && day.chartStart <= visibleTo
      ));
      const visibleSpan = visibleRange
        ? Math.max(0, visibleTo - visibleFrom)
        : Number.POSITIVE_INFINITY;
      const showTradingDays = visibleDays.length > 1 && visibleSpan >= 1.35 * 24 * 60 * 60;
      const nextIntradayAxis = !showTradingDays;
      if (timelineIntradayAxisRef.current !== nextIntradayAxis) {
        timelineIntradayAxisRef.current = nextIntradayAxis;
        chart.applyOptions({ timeScale: { tickMarkFormatter: axisTickFormatter } });
      }
      layer.dataset.axisMode = showTradingDays ? "trading-days" : "intraday";

      const fragment = document.createDocumentFragment();
      const chartWidth = rendererRef.current?.clientWidth ?? 0;
      const priceScaleWidth = chart.priceScale("right").width();
      const plotWidth = Math.max(0, chartWidth - priceScaleWidth);
      layer.style.right = `${priceScaleWidth}px`;
      for (const day of visibleDays) {
        const startX = chart.timeScale().timeToCoordinate(day.chartStart as Time);
        const centerX = chart.timeScale().timeToCoordinate(
          (day.chartStart + (day.chartEnd - day.chartStart) / 2) as Time,
        );
        if (startX !== null && startX > 0 && startX < plotWidth) {
          const separator = document.createElement("span");
          separator.className = "timeline-trading-day-separator";
          separator.style.transform = `translate3d(${startX.toFixed(2)}px, 0, 0)`;
          separator.dataset.tradingDay = day.key;
          fragment.append(separator);
        }
        if (showTradingDays && centerX !== null && centerX > 22 && centerX < plotWidth - 22) {
          const label = document.createElement("span");
          label.className = "timeline-trading-day-label";
          label.style.transform = `translate3d(${centerX.toFixed(2)}px, 0, 0) translateX(-50%)`;
          label.dataset.tradingDay = day.key;
          label.textContent = day.label;
          fragment.append(label);
        }
      }

      const visibleGaps: Array<{ gap: TimelineSessionGap; x: number }> = [];
      for (const gap of timelineSessionGapsRef.current) {
        if (gap.chartTime < visibleFrom || gap.chartTime > visibleTo) continue;
        const coordinate = chart.timeScale().timeToCoordinate(gap.chartTime as Time);
        if (coordinate === null) continue;
        const x = Number(coordinate);
        if (x >= 0 && x <= plotWidth) visibleGaps.push({ gap, x });
      }
      const sortedGapCoordinates = visibleGaps
        .map((item) => item.x)
        .sort((left, right) => left - right);
      let minimumGapSpacing = Number.POSITIVE_INFINITY;
      for (let index = 1; index < sortedGapCoordinates.length; index += 1) {
        minimumGapSpacing = Math.min(
          minimumGapSpacing,
          sortedGapCoordinates[index] - sortedGapCoordinates[index - 1],
        );
      }
      const markerDensity = chartWidth < 540 || minimumGapSpacing < 92
        ? "seam"
        : minimumGapSpacing < 180
          ? "compact"
          : "full";

      for (const { gap, x } of visibleGaps) {
        const tone = gap.boundaryState === "complete" ? gap.direction : "missing";
        const description = sessionGapDescription(gap, priceDigits);
        const seam = document.createElement("span");
        seam.className = `timeline-session-gap-seam is-${gap.kind}`;
        seam.style.transform = `translate3d(${x.toFixed(2)}px, 0, 0) translateX(-50%)`;
        seam.dataset.sessionGap = gap.id;
        seam.dataset.tone = tone;
        seam.title = description;
        if (markerDensity === "seam") {
          seam.setAttribute("aria-label", description);
          seam.setAttribute("role", "note");
        } else {
          seam.setAttribute("aria-hidden", "true");
        }
        fragment.append(seam);

        if (markerDensity === "seam") continue;
        const markerHalfWidth = markerDensity === "compact" ? 54 : 84;
        const clampedX = Math.min(
          Math.max(markerHalfWidth, x),
          Math.max(markerHalfWidth, plotWidth - markerHalfWidth),
        );
        const marker = document.createElement("span");
        marker.className = `timeline-session-gap-marker is-${markerDensity}`;
        marker.style.transform = `translate3d(${clampedX.toFixed(2)}px, 0, 0) translateX(-50%)`;
        marker.dataset.sessionGap = gap.id;
        marker.dataset.tone = tone;
        marker.dataset.boundaryState = gap.boundaryState;
        marker.title = description;
        marker.setAttribute("aria-label", description);
        marker.setAttribute("role", "note");
        marker.tabIndex = 0;

        const flow = document.createElement("span");
        flow.className = "session-gap-flow";
        if (markerDensity === "full") {
          const closeLabel = document.createElement("small");
          closeLabel.textContent = "收市";
          flow.append(closeLabel);
        }
        const durationLabel = document.createElement("strong");
        durationLabel.textContent = `${gap.kind === "weekend" ? "周末" : "休市"} ${formatSessionGapDuration(gap.durationSeconds, true)}`;
        flow.append(durationLabel);
        if (markerDensity === "full") {
          const openLabel = document.createElement("small");
          openLabel.textContent = "开盘";
          flow.append(openLabel);
        }
        marker.append(flow);

        if (markerDensity === "full") {
          const detail = document.createElement("em");
          detail.className = "session-gap-detail";
          detail.textContent = sessionGapDetail(gap, priceDigits);
          marker.append(detail);
        }
        fragment.append(marker);
      }
      layer.replaceChildren(fragment);
    });
  }, [axisTickFormatter, priceDigits]);

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
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: period.mode === "timeline" ? "rgba(0, 0, 0, 0)" : "#f1f3f7" },
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
        timeVisible: period.aggregation.kind === "fixed",
        secondsVisible: period.mode === "timeline" && timelineResolutionSeconds < 60,
        rightOffset: 0,
        rightOffsetPixels: 56,
        barSpacing: period.mode === "timeline" ? 5 : 10,
        minBarSpacing: period.mode === "timeline" ? 0.001 : 3,
        lockVisibleTimeRangeOnResize: true,
        minimumHeight: 28,
        allowBoldLabels: false,
        tickMarkMaxCharacterLength: 8,
        tickMarkFormatter: axisTickFormatter,
        enableConflation: false,
        uniformDistribution: true,
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
      localization: { locale: "zh-CN", timeFormatter: axisTimeFormatter },
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
        const chartTime = Number(parameter.time);
        const actualTime = timelineActualTimeRef.current.get(chartTime)
          ?? actualTimeForTimelineChartTime(timelineLayoutRef.current, chartTime)
          ?? chartTime;
        onHover({
          time: actualTime,
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
          time: actualTimeForChinaAxis(Number(parameter.time)),
          open: item.open,
          high: item.high,
          low: item.low,
          close: item.close,
        });
      }
    });

    const markHistoryInteraction = () => {
      historyInteractionUntilRef.current = window.performance.now() + 800;
    };
    const handleLogicalRange = (range: LogicalRange | null) => {
      scheduleLiveMarker();
      refreshTimelineDecorations();
      const activePeriod = periodRef.current;
      if (activePeriod.mode !== "timeline") {
        const visibleTimeRange = chart.timeScale().getVisibleRange();
        const visibleSpan = visibleTimeRange
          ? Math.max(0, Number(visibleTimeRange.to) - Number(visibleTimeRange.from))
          : 0;
        const nextDateOnlyAxis = activePeriod.aggregation.kind === "fixed"
          && visibleSpan >= DATE_ONLY_AXIS_THRESHOLD_SECONDS;
        visibleCandleSpanRef.current = visibleSpan;
        if (candleDateOnlyAxisRef.current !== nextDateOnlyAxis) {
          candleDateOnlyAxisRef.current = nextDateOnlyAxis;
          chart.applyOptions({ timeScale: { tickMarkFormatter: axisTickFormatter } });
        }
      }
      if (!range || returningRef.current || dataLengthRef.current === 0) return;
      updateFollowing(range.to >= latestLogicalIndexRef.current - 1.15);
      if (shouldRequestOlderHistory(
        range,
        dataLengthRef.current,
        historyLoadingRef.current,
        window.performance.now() <= historyInteractionUntilRef.current,
      )) {
        requestOlderHistoryRef.current();
      }
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleLogicalRange);

    const handlePointerMove = (event: PointerEvent) => {
      scheduleLiveMarker();
      if (event.buttons > 0) markHistoryInteraction();
    };
    const handleWheel = () => {
      markHistoryInteraction();
      scheduleLiveMarker();
    };
    container.addEventListener("pointerdown", markHistoryInteraction, true);
    container.addEventListener("pointermove", handlePointerMove, true);
    container.addEventListener("wheel", handleWheel, { passive: true, capture: true });

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      scheduleLiveMarker();
      refreshTimelineDecorations();
    });
    resizeObserver.observe(container);
    return () => {
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      if (markerFrameRef.current !== null) window.cancelAnimationFrame(markerFrameRef.current);
      if (timelineDecorationFrameRef.current !== null) {
        window.cancelAnimationFrame(timelineDecorationFrameRef.current);
      }
      markerFrameRef.current = null;
      timelineDecorationFrameRef.current = null;
      resizeObserver.disconnect();
      container.removeEventListener("pointerdown", markHistoryInteraction, true);
      container.removeEventListener("pointermove", handlePointerMove, true);
      container.removeEventListener("wheel", handleWheel, true);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(handleLogicalRange);
      chart.remove();
      chartRef.current = null;
      candlestickSeriesRef.current = null;
      areaSeriesRef.current = null;
      referenceLineRef.current = null;
    };
  }, [
    axisTickFormatter,
    axisTimeFormatter,
    onHover,
    period.mode,
    refreshTimelineDecorations,
    scheduleLiveMarker,
    timelineResolutionSeconds,
    updateFollowing,
  ]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      grid: {
        vertLines: { color: period.mode === "timeline" ? "rgba(0, 0, 0, 0)" : "#f1f3f7" },
      },
      timeScale: {
        timeVisible: period.aggregation.kind === "fixed",
        secondsVisible: period.mode === "timeline" && timelineResolutionSeconds < 60,
        rightOffset: 0,
        rightOffsetPixels: 56,
        barSpacing: period.mode === "timeline" ? 5 : 10,
        minBarSpacing: period.mode === "timeline" ? 0.001 : 3,
        tickMarkFormatter: axisTickFormatter,
        enableConflation: false,
        uniformDistribution: true,
      },
      localization: { locale: "zh-CN", timeFormatter: axisTimeFormatter },
    });
    refreshTimelineDecorations();
  }, [
    axisTickFormatter,
    axisTimeFormatter,
    period.id,
    period.mode,
    refreshTimelineDecorations,
    timelineResolutionSeconds,
  ]);

  const scrollToLatest = useCallback((chart: IChartApi) => {
    const latestLogicalIndex = latestLogicalIndexRef.current;
    const visibleRange = chart.timeScale().getVisibleLogicalRange();
    if (latestLogicalIndex < 0 || !visibleRange) {
      chart.timeScale().scrollToRealTime();
      return;
    }
    const options = chart.timeScale().options();
    const rightOffset = options.rightOffsetPixels === undefined
      ? options.rightOffset
      : options.rightOffsetPixels / Math.max(1, options.barSpacing);
    const visibleSpan = Math.max(2, visibleRange.to - visibleRange.from);
    const to = latestLogicalIndex + rightOffset;
    chart.timeScale().setVisibleLogicalRange({ from: to - visibleSpan, to });
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    const candlestickSeries = candlestickSeriesRef.current;
    const areaSeries = areaSeriesRef.current;
    if (!chart || (!candlestickSeries && !areaSeries)) return;

    const previousPeriod = previousPeriodRef.current;
    const previousLastTime = previousLastTimeRef.current;
    const previousFirstTime = previousFirstTimeRef.current;
    const visibleRangeBeforeUpdate = chart.timeScale().getVisibleLogicalRange();
    const previousDataLength = previousDataLengthRef.current;
    const previousSeriesDataLength = previousSeriesDataLengthRef.current;
    const activeSeries = period.mode === "timeline" ? timelineData : chartData;
    const activeDataLength = activeSeries.length;
    const logicalDataLength = activeDataLength;
    const nextFirstTime = Number(activeSeries.at(0)?.time);
    const timelineLastTime = timelineData.at(-1)?.time;
    const nextLastTime = period.mode === "timeline"
      ? typeof timelineLastTime === "number" ? timelineLastTime : null
      : latestBar?.time ?? null;
    const nextLatestLogicalIndex = period.mode === "timeline"
      ? latestTimelineLogicalIndex ?? -1
      : activeDataLength - 1;
    dataLengthRef.current = logicalDataLength;
    latestLogicalIndexRef.current = nextLatestLogicalIndex;
    const canIncrementLastPoint = previousPeriod === period.id
      && previousCandlesRef.current === candles
      && previousSeriesDataLength > 0
      && activeDataLength >= previousSeriesDataLength
      && activeDataLength <= previousSeriesDataLength + 1
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

    const firstDataArrival = previousDataLength === 0 && logicalDataLength > 0;
    const previousFirstIndex = (
      previousFirstTime !== null
      && Number.isFinite(nextFirstTime)
      && nextFirstTime < previousFirstTime
    )
      ? activeSeries.findIndex((point) => Number(point.time) === previousFirstTime)
      : 0;
    const prependedCount = prependedPointCount(
      previousFirstTime,
      Number.isFinite(nextFirstTime) ? nextFirstTime : null,
      previousFirstIndex,
    );
    const historyWasPrepended = previousPeriod === period.id && prependedCount > 0;
    if (
      historyWasPrepended
      && visibleRangeBeforeUpdate
      && !followingRef.current
    ) {
      chart.timeScale().setVisibleLogicalRange({
        from: visibleRangeBeforeUpdate.from + prependedCount,
        to: visibleRangeBeforeUpdate.to + prependedCount,
      });
    } else if (historyWasPrepended && followingRef.current) {
      chart.timeScale().fitContent();
      scrollToLatest(chart);
      updateFollowing(true);
    } else if (
      logicalDataLength > 0 &&
      (firstDataArrival || previousPeriod === null || previousPeriod !== period.id)
    ) {
      chart.timeScale().fitContent();
      scrollToLatest(chart);
      updateFollowing(true);
    } else if (
      logicalDataLength > 0 &&
      followingRef.current &&
      previousLastTime !== null &&
      nextLastTime !== null &&
      nextLastTime > previousLastTime
    ) {
      returningRef.current = true;
      scrollToLatest(chart);
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      returnTimerRef.current = window.setTimeout(() => {
        returningRef.current = false;
      }, 420);
    }

    previousPeriodRef.current = period.id;
    previousFirstTimeRef.current = Number.isFinite(nextFirstTime) ? nextFirstTime : null;
    previousLastTimeRef.current = nextLastTime;
    previousDataLengthRef.current = logicalDataLength;
    previousSeriesDataLengthRef.current = activeDataLength;
    previousCandlesRef.current = candles;
    scheduleLiveMarker();
    refreshTimelineDecorations();
  }, [
    candles,
    chartData,
    latestBar?.time,
    latestTimelineLogicalIndex,
    period.id,
    period.mode,
    refreshTimelineDecorations,
    scheduleLiveMarker,
    scrollToLatest,
    timelineData,
    updateFollowing,
  ]);

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
      const value = marketPhase === "closed"
        ? "休市"
        : formatBarCountdown(secondsUntilBackendBarClose(candles, period));
      if (countdownRef.current && countdownRef.current.textContent !== value) {
        countdownRef.current.textContent = value;
      }
      const layer = liveLayerRef.current;
      const price = latestPriceRef.current;
      if (layer && price !== null) {
        layer.setAttribute(
          "aria-label",
          marketPhase === "closed"
            ? `休市最后价 ${price.toFixed(priceDigits)}，等待下一交易时段`
            : `当前价 ${price.toFixed(priceDigits)}，本周期剩余 ${value}`,
        );
      }
    };

    refreshCountdown();
    if (marketPhase === "closed") return;
    const timer = window.setInterval(refreshCountdown, 250);
    return () => window.clearInterval(timer);
  }, [candles, marketPhase, period, priceDigits]);

  const returnToRealtime = () => {
    const chart = chartRef.current;
    if (!chart) return;
    returningRef.current = true;
    updateFollowing(true);
    scrollToLatest(chart);
    scheduleLiveMarker();
    refreshTimelineDecorations();
    if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
    returnTimerRef.current = window.setTimeout(() => {
      returningRef.current = false;
    }, 420);
  };

  const renderedCountdown = marketPhase === "closed"
    ? "休市"
    : formatBarCountdown(secondsUntilBackendBarClose(candles, period));
  const markerStyle = {
    "--live-color": liveColor,
  } as CSSProperties;

  return (
    <div
      className="market-chart"
      data-chart-mode={period.mode}
      data-chart-period={period.id}
      data-market-phase={marketPhase}
    >
      <div className="chart-renderer" ref={rendererRef} />
      <div
        ref={tradingDayLayerRef}
        className="timeline-trading-days"
        role="group"
        aria-label="交易时段标注"
      />
      {(period.mode === "timeline" ? timelineData.length > 0 : latestBar) && livePrice !== null ? (
        <div
          ref={liveLayerRef}
          className={`live-price-layer ${marketPhase === "closed" ? "is-market-closed" : ""}`}
          style={markerStyle}
          aria-label={marketPhase === "closed"
            ? `休市最后价 ${livePrice.toFixed(priceDigits)}，等待下一交易时段`
            : `当前价 ${livePrice.toFixed(priceDigits)}，本周期剩余 ${renderedCountdown}`}
        >
          <div className="live-price-line" />
          <div className="live-price-tag-track">
            <div className="live-price-tag" key={`${marketPhase}-${livePrice.toFixed(priceDigits)}`}>
              <strong>{livePrice.toFixed(priceDigits)}</strong>
              <span ref={countdownRef}>{renderedCountdown}</span>
            </div>
          </div>
        </div>
      ) : null}
      {!isFollowing ? (
        <button type="button" className="return-live-button" onClick={returnToRealtime}>
          <span />{marketPhase === "closed" ? "回到最新" : "回到实时"}
        </button>
      ) : null}
    </div>
  );
}
