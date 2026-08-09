import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type CandlestickData,
  type Coordinate,
  type HistogramData,
  type IChartApi,
  type IPaneApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type LineData,
  type LogicalRange,
  type SeriesMarker,
  type Time,
  type WhitespaceData,
} from "lightweight-charts";

import { barsFromCandles, buildTimelineSeries, formatBarCountdown } from "./chartModel";
import { secondsUntilPeriodClose, type ChartPeriod } from "./chartPeriods";
import {
  DATE_ONLY_AXIS_THRESHOLD_SECONDS,
  TIMELINE_CLOCK_BUCKET_SECONDS,
  actualTimeForChinaAxis,
  actualTimeForTimelineChartTime,
  buildSeriesDataGaps,
  buildTimelineLayout,
  buildTimelineSessionGaps,
  formatBeijingDateTime,
  formatChartTick,
  formatCrosshairTime,
  formatSessionGapDuration,
  formatTimelineTick,
  projectTimeForChinaAxis,
  projectTimelineTime,
  projectTimelineSeries,
  timelineClockBucketTime,
  timelineDayWindowAtLogicalRange,
  timelineGapSeparatorTime,
  timelineLogicalSpanAtRange,
  timelineLogicalViewportForSpan,
  timelineZoomSpanAfterWheel,
  type SeriesDataGapKind,
  type TimelineLayout,
  type TimelineLogicalDayRange,
  type TimelineSessionGap,
} from "./chartTimeAxis";
import { weakDrawingSnap } from "./expertDrawing";
import type { ChartIndicatorLayer, ChartLayer } from "./chartLayers";
import { prependedPointCount, shouldRequestOlderHistory } from "./historyLoading";
import type {
  ExpertDrawing,
  ExpertDrawingPoint,
  ExpertDrawingSnapMode,
  ExpertDrawingTool,
  ExpertIndicatorSeriesView,
} from "./expertTypes";
import type { Candle, HoverCandle, MarketPhase, MarketSchedule, TimelineSample } from "./types";
import {
  TimelineClockSeries,
  buildTimelineClockDomain,
  changedTimelineClockDataIndexes,
  isTimelineClockBucket,
  materializeTimelineClockData,
  nearestTimelineClockSample,
  timelineClockDataItemAt,
  timelineClockDomainsSharePrefix,
  timelineClockVisibleDataLength,
  timelineClockVisibleRanges,
  recentTimelineWindowStart,
  type TimelineClockDomain,
  type TimelineClockSeriesApi,
} from "./timelineClockSeries";

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
  appearance?: "default" | "expert";
  displayTimeZone?: string;
  layers?: readonly ChartLayer[];
  drawingTool?: ExpertDrawingTool | null;
  drawingSnapMode?: ExpertDrawingSnapMode;
  onDrawingCommit?: (drawing: ExpertDrawing) => void;
  onIndicatorPaneResize?: (layerId: string, height: number) => void;
  replayMode?: boolean;
  replayIndex?: number | null;
  replayCutoff?: number | null;
}

const UP_COLOR = "#e94357";
const DOWN_COLOR = "#35aa75";
const EMPTY_CHART_LAYERS: readonly ChartLayer[] = [];
const MAX_INCREMENTAL_REPLAY_POINTS = 2_000;
const INITIAL_TIMELINE_MATERIALIZED_DAYS = 1;

interface RenderableSeriesGap {
  kind: SeriesDataGapKind;
  nextIndex: number;
  time: Time;
  actualTime: number;
}

function insertGapWhitespace<T extends { time: Time }>(
  points: T[],
  gaps: readonly RenderableSeriesGap[],
): Array<T | WhitespaceData<Time>> {
  if (gaps.length === 0) return points;
  const result: Array<T | WhitespaceData<Time>> = [];
  let gapIndex = 0;
  for (let pointIndex = 0; pointIndex < points.length; pointIndex += 1) {
    while (gaps[gapIndex]?.nextIndex === pointIndex) {
      result.push({ time: gaps[gapIndex].time });
      gapIndex += 1;
    }
    result.push(points[pointIndex]);
  }
  return result;
}

function gapAwarePrefixLength(
  pointCount: number,
  gaps: readonly RenderableSeriesGap[],
): number {
  let low = 0;
  let high = gaps.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (gaps[middle].nextIndex < pointCount) low = middle + 1;
    else high = middle;
  }
  return pointCount + low;
}

interface DrawingDraft {
  start: ExpertDrawingPoint;
  startX: number;
  startY: number;
  startSnapped: boolean;
  currentX: number;
  currentY: number;
  currentSnapped: boolean;
}

interface PointerDrawingLocation {
  point: ExpertDrawingPoint;
  x: number;
  y: number;
  snapped: boolean;
}

interface IndicatorRenderState {
  historyKey: string | null;
  revision: number;
  offset: number;
  visibleLength: number;
  firstTime: number | null;
  projectionKey: string;
}

interface KdjIndicatorRuntime {
  kind: "kdj";
  pane: IPaneApi<Time>;
  configuredHeight: number;
  k: ISeriesApi<"Line">;
  d: ISeriesApi<"Line">;
  j: ISeriesApi<"Line">;
  state: IndicatorRenderState | null;
}

interface MacdIndicatorRuntime {
  kind: "macd";
  pane: IPaneApi<Time>;
  configuredHeight: number;
  value: ISeriesApi<"Line">;
  signal: ISeriesApi<"Line">;
  histogram: ISeriesApi<"Histogram">;
  state: IndicatorRenderState | null;
}

type IndicatorRuntime = KdjIndicatorRuntime | MacdIndicatorRuntime;

interface DrawingLayerRuntime {
  series: Array<ISeriesApi<"Line">>;
  priceLines: IPriceLine[];
  priceLineOwner: MainSeriesApi;
}

type MainSeriesApi = ISeriesApi<"Candlestick"> | TimelineClockSeriesApi;

interface TimelineSeriesRenderState {
  domain: TimelineClockDomain;
  pointCount: number;
  dataLength: number;
  periodId: string;
}

interface TimelineSeriesCache {
  sourceCandles: Candle[];
  sourceSamples: TimelineSample[];
  windowStart: number | null;
  livePrice: number | null;
  observedAt: string | null;
  data: TimelineSample[];
}

interface CandleSeriesRenderState {
  data: Array<CandlestickData<Time> | WhitespaceData<Time>>;
  dataLength: number;
}

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

function timelinePrefixCount(
  values: readonly { actualTime: number }[],
  cutoff: number | null,
): number {
  if (cutoff === null || !Number.isFinite(cutoff)) return 0;
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (values[middle].actualTime < cutoff) low = middle + 1;
    else high = middle;
  }
  return low;
}

function lowerBoundCandleTime(candles: readonly Candle[], targetTime: number): number {
  let low = 0;
  let high = candles.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const candleTime = Date.parse(candles[middle].open_time) / 1_000;
    if (!Number.isFinite(candleTime) || candleTime < targetTime) low = middle + 1;
    else high = middle;
  }
  return low;
}

function lowerBoundTimelineSampleTime(
  samples: readonly TimelineSample[],
  targetTime: number,
): number {
  let low = 0;
  let high = samples.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    const sample = samples[middle];
    const sampleTime = sample.observedTime ?? sample.time;
    if (sampleTime < targetTime) low = middle + 1;
    else high = middle;
  }
  return low;
}

function sameHoverCandle(left: HoverCandle | null, right: HoverCandle | null): boolean {
  return left === right || Boolean(
    left
    && right
    && left.time === right.time
    && left.open === right.open
    && left.high === right.high
    && left.low === right.low
    && left.close === right.close,
  );
}

function createIndicatorRuntime(
  chart: IChartApi,
  layer: ChartIndicatorLayer,
): IndicatorRuntime {
  const pane = chart.addPane(true);
  const paneIndex = pane.paneIndex();
  if (layer.indicatorId === "kdj") {
    const k = chart.addSeries(LineSeries, {
      title: "K",
      color: "#d5a84b",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    }, paneIndex);
    const d = chart.addSeries(LineSeries, {
      title: "D",
      color: "#3fa9b7",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    }, paneIndex);
    const j = chart.addSeries(LineSeries, {
      title: "J",
      color: "#d96c5f",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    }, paneIndex);
    for (const price of [20, 80]) {
      k.createPriceLine({
        price,
        color: "rgba(130, 154, 168, .34)",
        lineWidth: 1,
        lineStyle: LineStyle.Dotted,
        axisLabelVisible: false,
        title: "",
      });
    }
    pane.priceScale("right").applyOptions({
      borderVisible: false,
      scaleMargins: { top: 0.12, bottom: 0.12 },
    });
    pane.setHeight(layer.height);
    return { kind: "kdj", pane, configuredHeight: layer.height, k, d, j, state: null };
  }

  const histogram = chart.addSeries(HistogramSeries, {
    title: "柱",
    base: 0,
    priceLineVisible: false,
    lastValueVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  }, paneIndex);
  const value = chart.addSeries(LineSeries, {
    title: "DIF",
    color: "#d5a84b",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  }, paneIndex);
  const signal = chart.addSeries(LineSeries, {
    title: "DEA",
    color: "#3fa9b7",
    lineWidth: 1,
    priceLineVisible: false,
    lastValueVisible: true,
    crosshairMarkerVisible: false,
    priceFormat: { type: "price", precision: 2, minMove: 0.01 },
  }, paneIndex);
  value.createPriceLine({
    price: 0,
    color: "rgba(130, 154, 168, .34)",
    lineWidth: 1,
    lineStyle: LineStyle.Dotted,
    axisLabelVisible: false,
    title: "",
  });
  pane.priceScale("right").applyOptions({
    borderVisible: false,
    scaleMargins: { top: 0.14, bottom: 0.14 },
  });
  pane.setHeight(layer.height);
  return {
    kind: "macd",
    pane,
    configuredHeight: layer.height,
    value,
    signal,
    histogram,
    state: null,
  };
}

function removeIndicatorRuntime(chart: IChartApi, runtime: IndicatorRuntime): void {
  if (runtime.kind === "kdj") {
    chart.removeSeries(runtime.k);
    chart.removeSeries(runtime.d);
    chart.removeSeries(runtime.j);
  } else {
    chart.removeSeries(runtime.histogram);
    chart.removeSeries(runtime.value);
    chart.removeSeries(runtime.signal);
  }
  const paneIndex = runtime.pane.paneIndex();
  if (paneIndex > 0 && chart.panes()[paneIndex] === runtime.pane) chart.removePane(paneIndex);
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
  appearance = "default",
  displayTimeZone = "Asia/Shanghai",
  layers = EMPTY_CHART_LAYERS,
  drawingTool = null,
  drawingSnapMode = "off",
  onDrawingCommit,
  onIndicatorPaneResize,
  replayMode = false,
  replayIndex = null,
  replayCutoff = null,
}: MarketChartProps) {
  const drawingLayers = useMemo(
    () => layers.filter((layer): layer is Extract<ChartLayer, { kind: "drawing" }> => layer.kind === "drawing"),
    [layers],
  );
  const drawingLayerSignature = drawingLayers.map((layer) => [
    layer.definition.id,
    layer.definition.order,
    layer.definition.visible ? 1 : 0,
    ...layer.definition.drawings.map((drawing) => [
      drawing.id,
      drawing.type,
      drawing.start.time,
      drawing.start.price,
      drawing.end.time,
      drawing.end.price,
      drawing.color,
      drawing.label,
    ].join(":")),
  ].join("|")).join("||");
  const indicatorLayers = useMemo(
    () => layers.filter((layer): layer is Extract<ChartLayer, { kind: "indicator" }> => layer.kind === "indicator"),
    [layers],
  );
  const visibleIndicatorLayers = indicatorLayers.filter((layer) => layer.definition.visible);
  const indicatorLayerSignature = visibleIndicatorLayers
    .map((layer) => `${layer.definition.id}:${layer.definition.indicatorId}:${layer.definition.order}:${layer.definition.height}`)
    .join("|");
  const annotationLayers = useMemo(
    () => layers.filter((layer): layer is Extract<ChartLayer, { kind: "annotation" }> => layer.kind === "annotation"),
    [layers],
  );
  const openingGapVisible = annotationLayers.some((layer) => (
    layer.definition.annotationId === "gaps"
    && layer.definition.visible
  ));
  const sessionBands = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.sessionBands),
    [annotationLayers],
  );
  const eventMarkers = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.eventMarkers),
    [annotationLayers],
  );
  const eventMarkersById = useMemo(
    () => new Map(eventMarkers.map((event) => [event.id, event] as const)),
    [eventMarkers],
  );
  const strategyLevels = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.priceLevels),
    [annotationLayers],
  );
  const valueZones = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.valueZones),
    [annotationLayers],
  );
  const drawingsVisible = drawingLayers.some((layer) => layer.definition.visible);
  const rendererRef = useRef<HTMLDivElement>(null);
  const liveLayerRef = useRef<HTMLDivElement>(null);
  const countdownRef = useRef<HTMLSpanElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlestickSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const timelineSeriesRef = useRef<TimelineClockSeriesApi | null>(null);
  const timelineSeriesRenderStateRef = useRef<TimelineSeriesRenderState | null>(null);
  const timelineSeriesCacheRef = useRef<TimelineSeriesCache | null>(null);
  const candleSeriesRenderStateRef = useRef<CandleSeriesRenderState | null>(null);
  const referenceLineRef = useRef<IPriceLine | null>(null);
  const eventMarkersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const drawingLayerRuntimeRef = useRef<Map<string, DrawingLayerRuntime>>(new Map());
  const indicatorRuntimeRef = useRef<Map<string, IndicatorRuntime>>(new Map());
  const strategyPriceLinesRef = useRef<IPriceLine[]>([]);
  const strategyPriceLineOwnerRef = useRef<MainSeriesApi | null>(null);
  const markerFrameRef = useRef<number | null>(null);
  const hoverFrameRef = useRef<number | null>(null);
  const timelineDecorationFrameRef = useRef<number | null>(null);
  const timelineViewportReleaseFrameRef = useRef<number | null>(null);
  const expertDecorationFrameRef = useRef<number | null>(null);
  const paneMeasureFrameRef = useRef<number | null>(null);
  const paneResizeReportRef = useRef(false);
  const latestPriceRef = useRef<number | null>(null);
  const dataLengthRef = useRef(0);
  const latestLogicalIndexRef = useRef(-1);
  const previousDataLengthRef = useRef(0);
  const previousPeriodRef = useRef<string | null>(null);
  const previousLastTimeRef = useRef<number | null>(null);
  const previousFirstTimeRef = useRef<number | null>(null);
  const previousChartDataRef = useRef<Array<CandlestickData<Time> | WhitespaceData<Time>> | null>(null);
  const followingRef = useRef(true);
  const returningRef = useRef(false);
  const returnTimerRef = useRef<number | null>(null);
  const historyInteractionUntilRef = useRef(0);
  const historyLoadingRef = useRef(historyLoading);
  const requestOlderHistoryRef = useRef(onRequestOlderHistory);
  const tradingDayLayerRef = useRef<HTMLDivElement>(null);
  const expertOverlayLayerRef = useRef<HTMLDivElement>(null);
  const timelineLayoutRef = useRef<TimelineLayout>({ days: [] });
  const timelineLogicalDayRangesRef = useRef<readonly TimelineLogicalDayRange[]>([]);
  const timelineTradingDaysRef = useRef<TimelineLayout["days"]>([]);
  const timelineSessionGapsRef = useRef<TimelineSessionGap[]>([]);
  const timelineViewportSpanRef = useRef(1);
  const timelineViewportEndKeyRef = useRef<string | null>(null);
  const timelineViewportManagedRef = useRef(true);
  const timelineViewportApplyingRef = useRef(false);
  const timelineMaterializationKeyRef = useRef("");
  const timelineMaterializedDayCountRef = useRef(INITIAL_TIMELINE_MATERIALIZED_DAYS);
  const timelineMaterializationPendingRef = useRef(false);
  const timelineHasUnmaterializedHistoryRef = useRef(false);
  const timelineIntradayAxisRef = useRef(false);
  const visibleCandleSpanRef = useRef(0);
  const candleDateOnlyAxisRef = useRef(false);
  const periodRef = useRef(period);
  const timelineResolutionSecondsRef = useRef(timelineResolutionSeconds);
  const displayTimeZoneRef = useRef(displayTimeZone);
  const sessionBandsRef = useRef(sessionBands);
  const eventMarkersByIdRef = useRef(eventMarkersById);
  const openingGapVisibleRef = useRef(openingGapVisible);
  const valueZonesRef = useRef(valueZones);
  const projectedTimesRef = useRef<ReadonlyArray<{ actualTime: number; time: number }>>([]);
  const candleTimesRef = useRef<ReadonlyArray<{ actualTime: number; time: number }>>([]);
  const visibleProjectedLengthRef = useRef(0);
  const visibleCandleLengthRef = useRef(0);
  const pendingHoverRef = useRef<HoverCandle | null>(null);
  const lastHoverRef = useRef<HoverCandle | null>(null);
  const onHoverRef = useRef(onHover);
  const drawingToolRef = useRef(drawingTool);
  const drawingSnapModeRef = useRef(drawingSnapMode);
  const onDrawingCommitRef = useRef(onDrawingCommit);
  const onIndicatorPaneResizeRef = useRef(onIndicatorPaneResize);
  const drawingDraftRef = useRef<DrawingDraft | null>(null);
  historyLoadingRef.current = historyLoading;
  requestOlderHistoryRef.current = onRequestOlderHistory;
  periodRef.current = period;
  timelineResolutionSecondsRef.current = timelineResolutionSeconds;
  displayTimeZoneRef.current = displayTimeZone;
  sessionBandsRef.current = sessionBands;
  eventMarkersByIdRef.current = eventMarkersById;
  openingGapVisibleRef.current = openingGapVisible;
  valueZonesRef.current = valueZones;
  drawingToolRef.current = drawingTool;
  drawingSnapModeRef.current = drawingSnapMode;
  onDrawingCommitRef.current = onDrawingCommit;
  onIndicatorPaneResizeRef.current = onIndicatorPaneResize;
  onHoverRef.current = onHover;
  const [isFollowing, setIsFollowing] = useState(true);
  const [drawingDraft, setDrawingDraft] = useState<DrawingDraft | null>(null);
  const [mainPaneHeight, setMainPaneHeight] = useState<number | null>(null);
  const [, setTimelineMaterializationRevision] = useState(0);

  const latestCandleIdentity = candles.at(-1);
  const timelineMaterializationKey = [
    period.mode,
    latestCandleIdentity?.instrument.symbol ?? "",
    latestCandleIdentity?.source.provider ?? "",
  ].join(":");
  if (timelineMaterializationKeyRef.current !== timelineMaterializationKey) {
    timelineMaterializationKeyRef.current = timelineMaterializationKey;
    timelineMaterializedDayCountRef.current = INITIAL_TIMELINE_MATERIALIZED_DAYS;
    timelineMaterializationPendingRef.current = false;
  }
  const timelineMaterializedDayCount = timelineMaterializedDayCountRef.current;
  const ensureTimelineMaterializedDays = useCallback((requestedDayCount: number): boolean => {
    const normalizedDayCount = Math.max(
      INITIAL_TIMELINE_MATERIALIZED_DAYS,
      Math.ceil(Number.isFinite(requestedDayCount) ? requestedDayCount : 0),
    );
    if (
      normalizedDayCount <= timelineMaterializedDayCountRef.current
      || timelineMaterializationPendingRef.current
    ) return false;
    timelineMaterializedDayCountRef.current = normalizedDayCount;
    timelineMaterializationPendingRef.current = true;
    setTimelineMaterializationRevision((current) => current + 1);
    return true;
  }, []);

  useEffect(() => {
    timelineMaterializationPendingRef.current = false;
  }, [timelineMaterializationKey, timelineMaterializedDayCount]);

  const activeMainSeries = useCallback((): MainSeriesApi | null => (
    periodRef.current.mode === "timeline"
      ? timelineSeriesRef.current
      : candlestickSeriesRef.current
  ), []);

  const bars = useMemo(
    () => barsFromCandles(candles),
    [candles],
  );
  latestPriceRef.current = livePrice;
  const candleTimes = useMemo(
    () => bars.map((bar) => ({
      actualTime: bar.time,
      time: projectTimeForChinaAxis(bar.time),
    })),
    [bars],
  );
  const baseChartData = useMemo<CandlestickData<Time>[]>(
    () => bars.map((bar) => ({
      ...bar,
      time: projectTimeForChinaAxis(bar.time) as Time,
    })),
    [bars],
  );
  const candleResolutionSeconds = period.aggregation.kind === "fixed"
    ? period.aggregation.minutes * 60
    : null;
  const candleGapLayout = useMemo(
    () => candleResolutionSeconds !== null && marketSchedule?.sessions.length
      ? buildTimelineLayout(candleTimes.map((point) => point.actualTime), marketSchedule)
      : null,
    [candleResolutionSeconds, candleTimes, marketSchedule],
  );
  const candleSeriesGaps = useMemo<RenderableSeriesGap[]>(
    () => candleResolutionSeconds === null
      ? []
      : buildSeriesDataGaps(
        candleTimes,
        candleResolutionSeconds,
        candleGapLayout,
      ).map((gap) => ({
        kind: gap.kind,
        nextIndex: gap.nextIndex,
        time: projectTimeForChinaAxis(gap.separatorTime) as Time,
        actualTime: gap.separatorTime,
      })),
    [candleGapLayout, candleResolutionSeconds, candleTimes],
  );
  const candleWhitespaceGaps = useMemo(
    () => candleSeriesGaps.filter((gap) => gap.kind === "missing-trade"),
    [candleSeriesGaps],
  );
  const chartData = useMemo(
    () => insertGapWhitespace(baseChartData, candleWhitespaceGaps),
    [baseChartData, candleWhitespaceGaps],
  );
  candleTimesRef.current = candleTimes;
  const visibleCandleCount = replayMode
    ? Math.min(bars.length, Math.max(0, (replayIndex ?? -1) + 1))
    : bars.length;
  const visibleChartDataCount = gapAwarePrefixLength(visibleCandleCount, candleWhitespaceGaps);
  visibleCandleLengthRef.current = visibleCandleCount;
  const latestBar = visibleCandleCount > 0 ? bars[visibleCandleCount - 1] : null;
  const timelineSnapshotPrice = replayMode ? null : livePrice;
  const timelineSnapshotObservedAt = replayMode ? null : observedAt;
  const timelineWindowStart = useMemo(() => {
    if (period.mode !== "timeline") return null;
    if (candleTimes.length > 0) {
      return recentTimelineWindowStart(
        candleTimes,
        timelineMaterializedDayCount,
        marketSchedule,
      );
    }
    const sampleTimes = timelineSamples.map((sample) => ({
      actualTime: sample.observedTime ?? sample.time,
    }));
    return recentTimelineWindowStart(
      sampleTimes,
      timelineMaterializedDayCount,
      marketSchedule,
    );
  }, [
    candleTimes,
    marketSchedule,
    period.mode,
    timelineMaterializedDayCount,
    timelineSamples,
  ]);
  const timelineWindowCandles = useMemo(() => {
    if (period.mode !== "timeline" || timelineWindowStart === null) return candles;
    const firstIndex = lowerBoundCandleTime(candles, timelineWindowStart);
    return firstIndex === 0 ? candles : candles.slice(firstIndex);
  }, [candles, period.mode, timelineWindowStart]);
  const timelineWindowSamples = useMemo(() => {
    if (period.mode !== "timeline" || timelineWindowStart === null) return timelineSamples;
    const firstIndex = lowerBoundTimelineSampleTime(timelineSamples, timelineWindowStart);
    return firstIndex === 0 ? timelineSamples : timelineSamples.slice(firstIndex);
  }, [period.mode, timelineSamples, timelineWindowStart]);
  const earliestLoadedTimelineTime = Math.min(
    candleTimes[0]?.actualTime ?? Number.POSITIVE_INFINITY,
    timelineSamples[0]?.observedTime
      ?? timelineSamples[0]?.time
      ?? Number.POSITIVE_INFINITY,
  );
  const timelineHasUnmaterializedHistory = timelineWindowStart !== null
    && earliestLoadedTimelineTime < timelineWindowStart;
  timelineHasUnmaterializedHistoryRef.current = timelineHasUnmaterializedHistory;
  const rawTimelineData = useMemo(() => {
    const cache = timelineSeriesCacheRef.current;
    if (period.mode !== "timeline") {
      return cache?.sourceCandles === candles && cache.sourceSamples === timelineSamples
        ? cache.data
        : [];
    }
    if (
      cache?.sourceCandles === candles
      && cache.sourceSamples === timelineSamples
      && cache.windowStart === timelineWindowStart
      && cache.livePrice === timelineSnapshotPrice
      && cache.observedAt === timelineSnapshotObservedAt
    ) return cache.data;
    const data = buildTimelineSeries(
      timelineWindowCandles,
      timelineWindowSamples,
      timelineSnapshotPrice,
      timelineSnapshotObservedAt,
    );
    timelineSeriesCacheRef.current = {
      sourceCandles: candles,
      sourceSamples: timelineSamples,
      windowStart: timelineWindowStart,
      livePrice: timelineSnapshotPrice,
      observedAt: timelineSnapshotObservedAt,
      data,
    };
    return data;
  }, [
    candles,
    period.mode,
    timelineSamples,
    timelineSnapshotObservedAt,
    timelineSnapshotPrice,
    timelineWindowCandles,
    timelineWindowSamples,
    timelineWindowStart,
  ]);
  const observedEpoch = timelineSnapshotObservedAt
    ? Date.parse(timelineSnapshotObservedAt) / 1_000
    : null;
  const timelineLayout = useMemo(() => {
    const actualTimes = rawTimelineData
      .map((point) => point.observedTime ?? point.time)
      .filter(Number.isFinite);
    if (observedEpoch !== null && Number.isFinite(observedEpoch)) actualTimes.push(observedEpoch);
    return buildTimelineLayout(actualTimes, marketSchedule);
  }, [marketSchedule, observedEpoch, rawTimelineData]);
  timelineLayoutRef.current = timelineLayout;
  const timelineTradingDays = useMemo(
    () => [...timelineLayout.days.reduce((groups, day) => {
      const current = groups.get(day.key);
      groups.set(day.key, current
        ? {
          ...current,
          actualStart: Math.min(current.actualStart, day.actualStart),
          actualEnd: Math.max(current.actualEnd, day.actualEnd),
          chartStart: Math.min(current.chartStart, day.chartStart),
          chartEnd: Math.max(current.chartEnd, day.chartEnd),
        }
        : day);
      return groups;
    }, new Map<string, TimelineLayout["days"][number]>()).values()],
    [timelineLayout],
  );
  timelineTradingDaysRef.current = timelineTradingDays;
  const projectedTimelineData = useMemo(
    () => projectTimelineSeries(rawTimelineData, timelineLayout),
    [rawTimelineData, timelineLayout],
  );
  projectedTimesRef.current = projectedTimelineData;
  const visibleTimelineCount = replayMode
    ? timelinePrefixCount(projectedTimelineData, replayCutoff)
    : projectedTimelineData.length;
  visibleProjectedLengthRef.current = visibleTimelineCount;
  const timelineSessionGaps = useMemo(
    () => buildTimelineSessionGaps(timelineLayout, projectedTimelineData, priceDigits).map((gap) => ({
      ...gap,
      chartTime: timelineClockBucketTime(timelineLayout, gap.chartTime) ?? gap.chartTime,
    })),
    [priceDigits, projectedTimelineData, timelineLayout],
  );
  timelineSessionGapsRef.current = timelineSessionGaps;
  const timelineSeriesGaps = useMemo<RenderableSeriesGap[]>(
    () => buildSeriesDataGaps(
      projectedTimelineData,
      timelineResolutionSeconds,
      marketSchedule?.sessions.length ? timelineLayout : null,
      TIMELINE_CLOCK_BUCKET_SECONDS,
    ).flatMap((gap) => {
      const projectedTime = timelineGapSeparatorTime(gap, projectedTimelineData, timelineLayout);
      return projectedTime === null
        ? []
        : [{
          kind: gap.kind,
          nextIndex: gap.nextIndex,
          time: projectedTime as Time,
          actualTime: gap.separatorTime,
        }];
    }),
    [marketSchedule, projectedTimelineData, timelineLayout, timelineResolutionSeconds],
  );
  const timelineClockDomain = useMemo(
    () => buildTimelineClockDomain(
      projectedTimelineData,
      timelineSeriesGaps,
      timelineLayout,
    ),
    [projectedTimelineData, timelineLayout, timelineSeriesGaps],
  );
  const timelineLogicalDayRanges = useMemo(
    () => timelineClockVisibleRanges(timelineClockDomain, visibleTimelineCount),
    [timelineClockDomain, visibleTimelineCount],
  );
  timelineLogicalDayRangesRef.current = timelineLogicalDayRanges;
  const visibleTimelineSeriesCount = timelineClockVisibleDataLength(
    timelineClockDomain,
    visibleTimelineCount,
  );
  const latestTimelineLogicalIndex = timelineLogicalDayRanges.length > 0
    ? timelineLogicalDayRanges[timelineLogicalDayRanges.length - 1].to - 0.5
    : null;
  const visibleTimelineLogicalCount = latestTimelineLogicalIndex === null
    ? 0
    : latestTimelineLogicalIndex + 1;
  const drawingDataRangeKey = period.mode === "timeline"
    ? `timeline:${projectedTimelineData[0]?.actualTime ?? ""}:${replayMode ? projectedTimelineData[visibleTimelineCount - 1]?.actualTime ?? "" : "live"}`
    : `candles:${candleTimes[0]?.actualTime ?? ""}:${replayMode ? candleTimes[visibleCandleCount - 1]?.actualTime ?? "" : "live"}`;
  const indicatorProjectionKey = useMemo(() => [
    period.mode,
    period.id,
    ...timelineLayout.days.map((day) => `${day.key}:${day.chartStart}:${day.chartEnd}`),
    ...candleSeriesGaps.map((gap) => `gap:${gap.nextIndex}:${gap.actualTime}`),
  ].join("|"), [candleSeriesGaps, period.id, period.mode, timelineLayout.days]);
  const comparisonPrice = period.mode === "timeline" ? referencePrice : latestBar?.open ?? null;
  const liveColor = livePrice !== null && comparisonPrice !== null && livePrice >= comparisonPrice
    ? UP_COLOR
    : DOWN_COLOR;

  const refreshLiveMarker = useCallback(() => {
    const layer = liveLayerRef.current;
    if (!layer) return;

    const series = activeMainSeries();
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
  }, [activeMainSeries]);

  const scheduleLiveMarker = useCallback(() => {
    if (markerFrameRef.current !== null) return;
    markerFrameRef.current = window.requestAnimationFrame(() => {
      markerFrameRef.current = null;
      refreshLiveMarker();
    });
  }, [refreshLiveMarker]);

  const schedulePaneMeasurement = useCallback((reportIndicatorHeights = false) => {
    paneResizeReportRef.current = paneResizeReportRef.current || reportIndicatorHeights;
    if (paneMeasureFrameRef.current !== null) return;
    paneMeasureFrameRef.current = window.requestAnimationFrame(() => {
      paneMeasureFrameRef.current = null;
      const chart = chartRef.current;
      if (!chart) return;
      const height = Math.round(chart.panes()[0]?.getHeight() ?? 0);
      if (height > 0) {
        setMainPaneHeight((current) => current === height ? current : height);
        if (expertOverlayLayerRef.current) {
          expertOverlayLayerRef.current.style.height = `${height}px`;
        }
      }
      const shouldReport = paneResizeReportRef.current;
      paneResizeReportRef.current = false;
      if (!shouldReport || !onIndicatorPaneResizeRef.current) return;
      for (const [layerId, runtime] of indicatorRuntimeRef.current) {
        const paneHeight = Math.round(runtime.pane.getHeight());
        if (paneHeight > 0 && Math.abs(paneHeight - runtime.configuredHeight) > 1) {
          onIndicatorPaneResizeRef.current(layerId, paneHeight);
        }
      }
    });
  }, []);

  const scheduleHoverChange = useCallback((value: HoverCandle | null) => {
    pendingHoverRef.current = value;
    if (hoverFrameRef.current !== null) return;
    hoverFrameRef.current = window.requestAnimationFrame(() => {
      hoverFrameRef.current = null;
      const next = pendingHoverRef.current;
      if (sameHoverCandle(lastHoverRef.current, next)) return;
      lastHoverRef.current = next;
      onHoverRef.current(next);
    });
  }, []);

  const updateFollowing = useCallback((value: boolean) => {
    if (followingRef.current === value) return;
    followingRef.current = value;
    setIsFollowing(value);
  }, []);

  const applyTimelineDayViewport = useCallback((
    chart: IChartApi,
    requestedDaySpan: number,
    endKey?: string | null,
  ) => {
    const ranges = timelineLogicalDayRangesRef.current;
    const normalizedDaySpan = Math.max(
      1,
      Number.isFinite(requestedDaySpan) ? requestedDaySpan : 1,
    );
    const viewport = timelineLogicalViewportForSpan(ranges, normalizedDaySpan, endKey);
    if (!viewport) return null;
    timelineViewportSpanRef.current = normalizedDaySpan;
    timelineViewportEndKeyRef.current = viewport.lastKey;
    timelineViewportManagedRef.current = true;
    timelineViewportApplyingRef.current = true;
    if (timelineViewportReleaseFrameRef.current !== null) {
      window.cancelAnimationFrame(timelineViewportReleaseFrameRef.current);
    }
    chart.timeScale().setVisibleLogicalRange({ from: viewport.from, to: viewport.to });
    const renderer = rendererRef.current;
    renderer?.setAttribute("data-timeline-visible-days", String(viewport.dayCount));
    const firstRange = ranges.find((range) => range.key === viewport.firstKey);
    const lastRange = ranges.find((range) => range.key === viewport.lastKey);
    if (firstRange && lastRange) {
      renderer?.setAttribute(
        "data-timeline-window-start",
        new Date(firstRange.actualFrom * 1_000).toISOString(),
      );
      renderer?.setAttribute(
        "data-timeline-window-end",
        new Date(lastRange.actualTo * 1_000).toISOString(),
      );
    }
    updateFollowing(viewport.lastKey === ranges[ranges.length - 1]?.key);
    timelineViewportReleaseFrameRef.current = window.requestAnimationFrame(() => {
      timelineViewportReleaseFrameRef.current = null;
      timelineViewportApplyingRef.current = false;
    });
    return viewport;
  }, [updateFollowing]);

  const axisTickFormatter = useCallback((time: Time, tickMarkType: TickMarkType) => {
    const chartTime = Number(time);
    const activePeriod = periodRef.current;
    return activePeriod.mode === "timeline"
      ? formatTimelineTick(
        chartTime,
        tickMarkType,
        timelineLayoutRef.current,
        timelineIntradayAxisRef.current,
        displayTimeZoneRef.current,
      )
      : formatChartTick(
        actualTimeForChinaAxis(chartTime),
        tickMarkType,
        activePeriod,
        visibleCandleSpanRef.current,
        displayTimeZoneRef.current,
      );
  }, []);

  const actualTimeAtProjectedPoint = useCallback((chartTime: number): number | undefined => {
    const values = projectedTimesRef.current;
    const visibleLength = visibleProjectedLengthRef.current;
    let low = 0;
    let high = visibleLength - 1;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const difference = values[middle].time - chartTime;
      if (Math.abs(difference) < 0.000_01) return values[middle].actualTime;
      if (difference < 0) low = middle + 1;
      else high = middle - 1;
    }
    return undefined;
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
        actualTimeAtProjectedPoint(chartTime),
        displayTimeZoneRef.current,
      );
    }
    return formatCrosshairTime(
      actualTimeForChinaAxis(chartTime),
      activePeriod,
      timelineLayoutRef.current,
      60,
      undefined,
      displayTimeZoneRef.current,
    );
  }, [actualTimeAtProjectedPoint]);

  const nearestChartTimeForActual = useCallback((actualTime: number): number | null => {
    const timelineMode = periodRef.current.mode === "timeline";
    const values = timelineMode ? projectedTimesRef.current : candleTimesRef.current;
    const visibleLength = timelineMode
      ? visibleProjectedLengthRef.current
      : visibleCandleLengthRef.current;
    if (
      visibleLength === 0
      || actualTime < values[0].actualTime
      || actualTime > values[visibleLength - 1].actualTime
    ) {
      return null;
    }
    let low = 0;
    let high = visibleLength - 1;
    while (low < high) {
      const middle = Math.floor((low + high) / 2);
      if (values[middle].actualTime < actualTime) low = middle + 1;
      else high = middle;
    }
    const right = values[low];
    const left = values[Math.max(0, low - 1)];
    const nearestTime = Math.abs(right.actualTime - actualTime) < Math.abs(actualTime - left.actualTime)
      ? right.time
      : left.time;
    return timelineMode
      ? timelineClockBucketTime(timelineLayoutRef.current, nearestTime)
      : nearestTime;
  }, []);

  const actualTimeForChartCoordinate = useCallback((chartTime: number): number | null => {
    if (periodRef.current.mode !== "timeline") return actualTimeForChinaAxis(chartTime);
    return actualTimeAtProjectedPoint(chartTime)
      ?? actualTimeForTimelineChartTime(timelineLayoutRef.current, chartTime);
  }, [actualTimeAtProjectedPoint]);

  const refreshExpertDecorations = useCallback(() => {
    if (expertDecorationFrameRef.current !== null) return;
    expertDecorationFrameRef.current = window.requestAnimationFrame(() => {
      expertDecorationFrameRef.current = null;
      const chart = chartRef.current;
      const layer = expertOverlayLayerRef.current;
      const series = activeMainSeries();
      if (!chart || !layer || !series) {
        layer?.replaceChildren();
        return;
      }
      const chartWidth = rendererRef.current?.clientWidth ?? 0;
      const priceScaleWidth = chart.priceScale("right").width();
      const plotWidth = Math.max(0, chartWidth - priceScaleWidth);
      layer.style.right = `${priceScaleWidth}px`;
      const paneHeight = Math.round(chart.panes()[0]?.getHeight() ?? 0);
      if (paneHeight > 0) layer.style.height = `${paneHeight}px`;
      const fragment = document.createDocumentFragment();
      const timelineMode = periodRef.current.mode === "timeline";
      const plottedTimes = timelineMode ? projectedTimesRef.current : candleTimesRef.current;
      const plottedLength = timelineMode
        ? visibleProjectedLengthRef.current
        : visibleCandleLengthRef.current;
      const firstActualTime = plottedTimes[0]?.actualTime ?? null;
      const lastActualTime = plottedLength > 0
        ? plottedTimes[plottedLength - 1]?.actualTime ?? null
        : null;
      const visibleRange = chart.timeScale().getVisibleRange();
      const rangeStart = visibleRange
        ? actualTimeForChartCoordinate(Number(visibleRange.from))
        : null;
      const rangeEnd = visibleRange
        ? actualTimeForChartCoordinate(Number(visibleRange.to))
        : null;
      const visibleActualStart = firstActualTime === null
        ? null
        : Math.max(firstActualTime, Math.min(rangeStart ?? firstActualTime, rangeEnd ?? firstActualTime));
      const visibleActualEnd = lastActualTime === null
        ? null
        : Math.min(lastActualTime, Math.max(rangeStart ?? lastActualTime, rangeEnd ?? lastActualTime));

      for (const band of sessionBandsRef.current) {
        if (
          visibleActualStart === null
          || visibleActualEnd === null
          || band.end < visibleActualStart
        ) continue;
        if (band.start > visibleActualEnd) break;
        const startTime = nearestChartTimeForActual(Math.max(band.start, visibleActualStart));
        const endTime = nearestChartTimeForActual(Math.min(band.end, visibleActualEnd));
        if (startTime === null || endTime === null) continue;
        const start = chart.timeScale().timeToCoordinate(startTime as Time);
        const end = chart.timeScale().timeToCoordinate(endTime as Time);
        if (start === null || end === null) continue;
        const left = Math.max(0, Math.min(Number(start), Number(end)));
        const right = Math.min(plotWidth, Math.max(Number(start), Number(end)));
        if (right <= 0 || left >= plotWidth || right - left < 1) continue;
        const element = document.createElement("span");
        const linkedEvent = band.eventId === null
          ? undefined
          : eventMarkersByIdRef.current.get(band.eventId);
        const eventShownSeparately = Boolean(
          linkedEvent
          && visibleActualStart !== null
          && visibleActualEnd !== null
          && linkedEvent.time >= visibleActualStart
          && linkedEvent.time <= visibleActualEnd,
        );
        element.className = `expert-session-band is-${band.kind}`;
        element.dataset.driver = band.driver;
        element.dataset.eventRelation = eventShownSeparately ? "linked" : "embedded";
        element.style.left = `${left.toFixed(2)}px`;
        element.style.width = `${Math.max(1, right - left).toFixed(2)}px`;
        element.title = `${band.label} · ${band.detail} · ${band.timeZone}${eventShownSeparately
          ? " · 具体事件由数据/事件策略标记"
          : ""}`;
        if (right - left >= 28) {
          const label = document.createElement("small");
          label.className = "expert-session-band-label";
          label.textContent = eventShownSeparately
            ? "美国资金主导 · 数据接管"
            : band.label;
          if (right - left < 110) label.dataset.compact = "true";
          element.append(label);
        }
        element.setAttribute("aria-hidden", "true");
        fragment.append(element);
      }

      if (timelineMode && openingGapVisibleRef.current) {
        for (const gap of timelineSessionGapsRef.current) {
          if (
            gap.boundaryState !== "complete"
            || gap.direction === "flat"
            || gap.direction === "unknown"
            || gap.nextOpen === null
            || gap.priceDifference === null
            || gap.pricePercent === null
            || visibleActualStart === null
            || visibleActualEnd === null
            || gap.openedAt < visibleActualStart
            || gap.openedAt > visibleActualEnd
          ) continue;
          const xCoordinate = chart.timeScale().timeToCoordinate(gap.chartTime as Time);
          const yCoordinate = series.priceToCoordinate(gap.nextOpen);
          if (xCoordinate === null || yCoordinate === null) continue;
          const x = Number(xCoordinate);
          const y = Number(yCoordinate);
          if (x < 0 || x > plotWidth || y < 0 || y > paneHeight) continue;

          const marker = document.createElement("span");
          marker.className = `opening-gap-annotation is-${gap.direction}`;
          if (x > plotWidth - 180) marker.classList.add("is-align-left");
          if (y < 70) marker.classList.add("is-below");
          marker.style.left = `${x.toFixed(2)}px`;
          marker.style.top = `${y.toFixed(2)}px`;
          marker.title = sessionGapDescription(gap, priceDigits);
          marker.setAttribute("role", "note");
          marker.setAttribute("aria-label", marker.title);

          const anchor = document.createElement("i");
          anchor.className = "opening-gap-anchor";
          marker.append(anchor);
          const stem = document.createElement("i");
          stem.className = "opening-gap-stem";
          marker.append(stem);
          const copy = document.createElement("span");
          copy.className = "opening-gap-copy";
          const value = document.createElement("strong");
          value.textContent = sessionGapDetail(gap, priceDigits);
          copy.append(value);
          const context = document.createElement("small");
          context.textContent = `${gap.kind === "weekend" ? "周末后" : "休市后"}首价 ${gap.nextOpen.toFixed(priceDigits)}`;
          copy.append(context);
          marker.append(copy);
          fragment.append(marker);
        }
      }

      for (const zone of valueZonesRef.current) {
        const startTime = nearestChartTimeForActual(zone.start);
        const endTime = nearestChartTimeForActual(zone.end);
        if (startTime === null || endTime === null) continue;
        const start = chart.timeScale().timeToCoordinate(startTime as Time);
        const end = chart.timeScale().timeToCoordinate(endTime as Time);
        const high = series.priceToCoordinate(zone.high);
        const low = series.priceToCoordinate(zone.low);
        if (start === null || end === null || high === null || low === null) continue;
        const left = Math.max(0, Math.min(Number(start), Number(end)));
        const right = Math.min(plotWidth, Math.max(Number(start), Number(end)));
        const top = Math.min(Number(high), Number(low));
        const bottom = Math.max(Number(high), Number(low));
        if (right <= left || bottom <= top) continue;
        const element = document.createElement("span");
        element.className = `expert-value-zone is-${zone.direction}`;
        element.style.left = `${left.toFixed(2)}px`;
        element.style.top = `${top.toFixed(2)}px`;
        element.style.width = `${Math.max(1, right - left).toFixed(2)}px`;
        element.style.height = `${Math.max(1, bottom - top).toFixed(2)}px`;
        element.title = `${zone.label} ${zone.low.toFixed(priceDigits)}–${zone.high.toFixed(priceDigits)}`;
        fragment.append(element);
      }
      layer.replaceChildren(fragment);
    });
  }, [actualTimeForChartCoordinate, nearestChartTimeForActual, priceDigits]);

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
      const logicalVisibleRange = chart.timeScale().getVisibleLogicalRange();
      const visibleLogicalDays = logicalVisibleRange
        ? timelineLogicalDayRangesRef.current.filter((range) => (
          range.to > logicalVisibleRange.from && range.from < logicalVisibleRange.to
        ))
        : timelineLogicalDayRangesRef.current;
      const visibleDayKeys = new Set(visibleLogicalDays.map((range) => range.key));
      const tradingDays = timelineTradingDaysRef.current;
      const visibleDays = tradingDays.filter((day) => visibleDayKeys.has(day.key));
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
        const dayRange = timelineLogicalDayRangesRef.current.find(
          (range) => range.key === day.key,
        );
        const startX = chart.timeScale().timeToCoordinate(day.chartStart as Time);
        const endX = dayRange
          ? chart.timeScale().timeToCoordinate(dayRange.chartTo as Time)
          : null;
        const centerX = startX === null || endX === null
          ? null
          : (Number(startX) + Number(endX)) / 2;
        if (startX !== null && startX > 0 && startX < plotWidth) {
          const separator = document.createElement("span");
          separator.className = "timeline-trading-day-separator";
          separator.style.transform = `translate3d(${startX.toFixed(2)}px, 0, 0)`;
          separator.dataset.tradingDay = day.key;
          fragment.append(separator);
        }
        if (showTradingDays && centerX !== null) {
          const labelX = Math.min(
            Math.max(22, centerX),
            Math.max(22, plotWidth - 22),
          );
          const label = document.createElement("span");
          label.className = "timeline-trading-day-label";
          label.style.transform = `translate3d(${labelX.toFixed(2)}px, 0, 0) translateX(-50%)`;
          label.dataset.tradingDay = day.key;
          label.textContent = day.label;
          fragment.append(label);
        }
      }

      const logicalWindow = timelineDayWindowAtLogicalRange(
        timelineLogicalDayRangesRef.current,
        logicalVisibleRange,
      );
      if (logicalWindow?.dayCount === 1) {
        const dayRange = timelineLogicalDayRangesRef.current.find(
          (range) => range.key === logicalWindow.endKey,
        );
        if (dayRange) {
          for (const boundary of [
            { edge: "open", time: dayRange.chartFrom },
            { edge: "close", time: dayRange.chartTo },
          ] as const) {
            const coordinate = chart.timeScale().timeToCoordinate(boundary.time as Time);
            if (coordinate === null) continue;
            const x = boundary.edge === "open"
              ? Math.max(4, Number(coordinate))
              : Math.min(plotWidth - 4, Number(coordinate));
            const label = document.createElement("span");
            label.className = `timeline-window-boundary-label is-${boundary.edge}`;
            label.style.transform = boundary.edge === "open"
              ? `translate3d(${x.toFixed(2)}px, 0, 0)`
              : `translate3d(${x.toFixed(2)}px, 0, 0) translateX(-100%)`;
            label.dataset.tradingDay = dayRange.key;
            label.dataset.edge = boundary.edge;
            label.textContent = formatTimelineTick(
              boundary.time,
              TickMarkType.Time,
              layout,
              true,
              displayTimeZoneRef.current,
            );
            fragment.append(label);
          }
        }
      }

      layer.replaceChildren(fragment);
    });
  }, [activeMainSeries, axisTickFormatter, priceDigits]);

  useEffect(() => {
    const container = rendererRef.current;
    if (!container) return;
    const expertAppearance = appearance === "expert";
    const backgroundColor = expertAppearance ? "#08151f" : "#ffffff";
    const textColor = expertAppearance ? "#718796" : "#8f98a6";
    const horizontalGridColor = expertAppearance ? "rgba(145, 171, 187, .10)" : "#eef1f5";
    const initialTimelineMode = periodRef.current.mode === "timeline";
    const verticalGridColor = initialTimelineMode
      ? "rgba(0, 0, 0, 0)"
      : expertAppearance ? "rgba(145, 171, 187, .075)" : "#f1f3f7";
    const crosshairColor = expertAppearance ? "#6d8492" : "#a8b2c2";
    const crosshairLabel = expertAppearance ? "#273d4a" : "#526074";

    const chart = createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: ColorType.Solid, color: backgroundColor },
        textColor,
        fontFamily: expertAppearance
          ? 'Bahnschrift, "Microsoft YaHei UI", sans-serif'
          : '"Segoe UI", "Microsoft YaHei UI", sans-serif',
        fontSize: 12,
        panes: {
          enableResize: true,
          separatorColor: expertAppearance ? "rgba(144, 174, 191, .18)" : "#e3e7ed",
          separatorHoverColor: expertAppearance ? "rgba(213, 168, 75, .28)" : "rgba(78, 125, 235, .18)",
        },
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: verticalGridColor },
        horzLines: { color: horizontalGridColor },
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: crosshairColor, width: 1, style: 2, labelBackgroundColor: crosshairLabel },
        horzLine: { color: crosshairColor, width: 1, style: 2, labelBackgroundColor: crosshairLabel },
      },
      rightPriceScale: {
        borderVisible: false,
        minimumWidth: 82,
        scaleMargins: { top: 0.17, bottom: 0.08 },
      },
      timeScale: {
        borderVisible: false,
        timeVisible: periodRef.current.aggregation.kind === "fixed",
        secondsVisible: initialTimelineMode && timelineResolutionSecondsRef.current < 60,
        rightOffset: 0,
        rightOffsetPixels: 56,
        barSpacing: initialTimelineMode ? 5 : 10,
        minBarSpacing: initialTimelineMode ? 0.001 : 3,
        lockVisibleTimeRangeOnResize: true,
        minimumHeight: 28,
        allowBoldLabels: false,
        tickMarkMaxCharacterLength: 8,
        tickMarkFormatter: axisTickFormatter,
        enableConflation: false,
        uniformDistribution: false,
      },
      handleScroll: {
        mouseWheel: !initialTimelineMode,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: !initialTimelineMode,
        mouseWheel: !initialTimelineMode,
        pinch: !initialTimelineMode,
      },
      localization: { locale: "zh-CN", timeFormatter: axisTimeFormatter },
    });
    let candlestickSeries: ISeriesApi<"Candlestick"> | null = null;
    let timelineSeries: TimelineClockSeriesApi | null = null;
    if (initialTimelineMode) {
      timelineSeries = chart.addCustomSeries(new TimelineClockSeries(), {
        color: expertAppearance ? "#ddb45c" : "#4e7deb",
        lineColor: expertAppearance ? "#ddb45c" : "#4e7deb",
        topColor: expertAppearance ? "rgba(221, 180, 92, .20)" : "rgba(78, 125, 235, .20)",
        bottomColor: expertAppearance ? "rgba(221, 180, 92, .015)" : "rgba(78, 125, 235, .015)",
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      timelineSeriesRef.current = timelineSeries;
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
    const mainSeries = timelineSeries ?? candlestickSeries;
    if (mainSeries) eventMarkersRef.current = createSeriesMarkers(mainSeries, []);
    scheduleLiveMarker();
    schedulePaneMeasurement();
    refreshExpertDecorations();

    chart.subscribeCrosshairMove((parameter) => {
      if (parameter.time === undefined) {
        scheduleHoverChange(null);
        return;
      }
      if (timelineSeries) {
        const item = parameter.seriesData.get(timelineSeries);
        if (!item || !isTimelineClockBucket(item)) {
          scheduleHoverChange(null);
          return;
        }
        const bucketX = chart.timeScale().timeToCoordinate(item.time);
        const pointerX = parameter.point?.x;
        const barSpacing = Math.max(0.000_001, chart.timeScale().options().barSpacing);
        const fraction = bucketX === null || pointerX === undefined
          ? 0
          : Math.max(0, Math.min(1, (Number(pointerX) - Number(bucketX)) / barSpacing));
        const sample = nearestTimelineClockSample(
          item,
          Number(item.time) + fraction * TIMELINE_CLOCK_BUCKET_SECONDS,
        );
        scheduleHoverChange({
          time: sample.actualTime,
          open: sample.value,
          high: sample.value,
          low: sample.value,
          close: sample.value,
        });
      } else if (candlestickSeries) {
        const item = parameter.seriesData.get(candlestickSeries);
        if (!item || !("open" in item)) {
          scheduleHoverChange(null);
          return;
        }
        scheduleHoverChange({
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
      refreshExpertDecorations();
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
      if (activePeriod.mode === "timeline" && !timelineViewportApplyingRef.current) {
        const windowState = timelineDayWindowAtLogicalRange(
          timelineLogicalDayRangesRef.current,
          range,
        );
        if (windowState) {
          timelineViewportSpanRef.current = timelineLogicalSpanAtRange(
            timelineLogicalDayRangesRef.current,
            range,
          ) ?? timelineViewportSpanRef.current;
          timelineViewportEndKeyRef.current = windowState.endKey;
          rendererRef.current?.setAttribute(
            "data-timeline-visible-days",
            String(windowState.dayCount),
          );
        }
      }
      if (timelineViewportApplyingRef.current) return;
      updateFollowing(range.to >= latestLogicalIndexRef.current - 1.15);
      if (shouldRequestOlderHistory(
        range,
        dataLengthRef.current,
        historyLoadingRef.current,
        window.performance.now() <= historyInteractionUntilRef.current,
      )) {
        if (
          activePeriod.mode === "timeline"
          && timelineHasUnmaterializedHistoryRef.current
        ) {
          ensureTimelineMaterializedDays(timelineMaterializedDayCountRef.current + 1);
        } else {
          requestOlderHistoryRef.current();
        }
      }
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleLogicalRange);

    const handlePointerMove = (event: PointerEvent) => {
      scheduleLiveMarker();
      if (event.buttons > 0) {
        markHistoryInteraction();
        if (periodRef.current.mode === "timeline") timelineViewportManagedRef.current = false;
        refreshExpertDecorations();
      }
    };
    const handlePointerDown = (event: PointerEvent) => {
      markHistoryInteraction();
      if (event.pointerType === "touch" && periodRef.current.mode === "timeline") {
        timelineViewportManagedRef.current = false;
      }
    };
    const handleWheel = (event: WheelEvent) => {
      markHistoryInteraction();
      if (
        periodRef.current.mode === "timeline"
        && Math.abs(event.deltaY) >= Math.abs(event.deltaX)
        && event.deltaY !== 0
      ) {
        event.preventDefault();
        event.stopPropagation();
        const ranges = timelineLogicalDayRangesRef.current;
        if (ranges.length === 0) return;
        const visibleRange = chart.timeScale().getVisibleLogicalRange();
        const currentWindow = timelineViewportManagedRef.current
          ? {
            endKey: timelineViewportEndKeyRef.current ?? ranges[ranges.length - 1].key,
          }
          : timelineDayWindowAtLogicalRange(ranges, visibleRange);
        const currentDaySpan = timelineViewportManagedRef.current
          ? timelineViewportSpanRef.current
          : timelineLogicalSpanAtRange(ranges, visibleRange) ?? 1;
        const requestedDaySpan = timelineZoomSpanAfterWheel(
          currentDaySpan,
          event.deltaY,
          event.deltaMode,
        );
        const endKey = followingRef.current
          ? ranges[ranges.length - 1].key
          : currentWindow?.endKey;
        const requestedEndIndex = endKey
          ? ranges.findIndex((range) => range.key === endKey)
          : -1;
        const endIndex = requestedEndIndex >= 0 ? requestedEndIndex : ranges.length - 1;
        const availableDaySpan = endIndex + 1;
        const nextDaySpan = Math.min(requestedDaySpan, availableDaySpan + 1);
        if (Math.abs(nextDaySpan - currentDaySpan) > 1e-4) {
          applyTimelineDayViewport(chart, nextDaySpan, endKey);
        }
        if (
          event.deltaY > 0
          && nextDaySpan > availableDaySpan
          && !historyLoadingRef.current
        ) {
          if (timelineHasUnmaterializedHistoryRef.current) {
            ensureTimelineMaterializedDays(Math.ceil(nextDaySpan));
          } else {
            requestOlderHistoryRef.current();
          }
        }
        scheduleLiveMarker();
        refreshTimelineDecorations();
        refreshExpertDecorations();
        return;
      }
      scheduleLiveMarker();
      refreshExpertDecorations();
    };
    const handlePointerUp = () => {
      schedulePaneMeasurement(true);
      refreshExpertDecorations();
    };
    container.addEventListener("pointerdown", handlePointerDown, true);
    container.addEventListener("pointermove", handlePointerMove, true);
    container.addEventListener("pointerup", handlePointerUp, true);
    container.addEventListener("wheel", handleWheel, { passive: false, capture: true });

    const resizeObserver = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
      scheduleLiveMarker();
      schedulePaneMeasurement();
      refreshTimelineDecorations();
      refreshExpertDecorations();
    });
    resizeObserver.observe(container);
    return () => {
      if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
      if (markerFrameRef.current !== null) window.cancelAnimationFrame(markerFrameRef.current);
      if (hoverFrameRef.current !== null) window.cancelAnimationFrame(hoverFrameRef.current);
      if (timelineDecorationFrameRef.current !== null) {
        window.cancelAnimationFrame(timelineDecorationFrameRef.current);
      }
      if (timelineViewportReleaseFrameRef.current !== null) {
        window.cancelAnimationFrame(timelineViewportReleaseFrameRef.current);
      }
      if (expertDecorationFrameRef.current !== null) {
        window.cancelAnimationFrame(expertDecorationFrameRef.current);
      }
      if (paneMeasureFrameRef.current !== null) {
        window.cancelAnimationFrame(paneMeasureFrameRef.current);
      }
      markerFrameRef.current = null;
      hoverFrameRef.current = null;
      timelineDecorationFrameRef.current = null;
      timelineViewportReleaseFrameRef.current = null;
      timelineViewportApplyingRef.current = false;
      expertDecorationFrameRef.current = null;
      paneMeasureFrameRef.current = null;
      paneResizeReportRef.current = false;
      resizeObserver.disconnect();
      container.removeEventListener("pointerdown", handlePointerDown, true);
      container.removeEventListener("pointermove", handlePointerMove, true);
      container.removeEventListener("pointerup", handlePointerUp, true);
      container.removeEventListener("wheel", handleWheel, true);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(handleLogicalRange);
      eventMarkersRef.current?.detach();
      chart.remove();
      chartRef.current = null;
      candlestickSeriesRef.current = null;
      timelineSeriesRef.current = null;
      referenceLineRef.current = null;
      eventMarkersRef.current = null;
      drawingLayerRuntimeRef.current.clear();
      indicatorRuntimeRef.current.clear();
      strategyPriceLinesRef.current = [];
      strategyPriceLineOwnerRef.current = null;
      pendingHoverRef.current = null;
      lastHoverRef.current = null;
      dataLengthRef.current = 0;
      latestLogicalIndexRef.current = -1;
      previousDataLengthRef.current = 0;
      previousPeriodRef.current = null;
      previousFirstTimeRef.current = null;
      previousLastTimeRef.current = null;
      previousChartDataRef.current = null;
      timelineSeriesRenderStateRef.current = null;
      candleSeriesRenderStateRef.current = null;
      timelineLogicalDayRangesRef.current = [];
      timelineViewportSpanRef.current = 1;
      timelineViewportEndKeyRef.current = null;
      timelineViewportManagedRef.current = true;
      rendererRef.current?.removeAttribute("data-timeline-visible-days");
      rendererRef.current?.removeAttribute("data-timeline-window-start");
      rendererRef.current?.removeAttribute("data-timeline-window-end");
    };
  }, [
    applyTimelineDayViewport,
    actualTimeAtProjectedPoint,
    appearance,
    axisTickFormatter,
    axisTimeFormatter,
    ensureTimelineMaterializedDays,
    period.mode,
    refreshExpertDecorations,
    refreshTimelineDecorations,
    scheduleLiveMarker,
    schedulePaneMeasurement,
    scheduleHoverChange,
    updateFollowing,
  ]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      grid: {
        vertLines: {
          color: period.mode === "timeline"
            ? "rgba(0, 0, 0, 0)"
            : appearance === "expert" ? "rgba(145, 171, 187, .075)" : "#f1f3f7",
        },
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
        uniformDistribution: false,
      },
      handleScroll: {
        mouseWheel: period.mode !== "timeline",
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        axisPressedMouseMove: period.mode !== "timeline",
        mouseWheel: period.mode !== "timeline",
        pinch: period.mode !== "timeline",
      },
      localization: { locale: "zh-CN", timeFormatter: axisTimeFormatter },
    });
    candlestickSeriesRef.current?.applyOptions({ visible: period.mode !== "timeline" });
    timelineSeriesRef.current?.applyOptions({ visible: period.mode === "timeline" });
    eventMarkersRef.current?.detach();
    const series = activeMainSeries();
    eventMarkersRef.current = series ? createSeriesMarkers(series, []) : null;
    refreshTimelineDecorations();
    refreshExpertDecorations();
  }, [
    appearance,
    activeMainSeries,
    axisTickFormatter,
    axisTimeFormatter,
    period.id,
    period.mode,
    refreshExpertDecorations,
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
    const timelineSeries = timelineSeriesRef.current;
    if (!chart || (!candlestickSeries && !timelineSeries)) return;

    const previousPeriod = previousPeriodRef.current;
    const previousLastTime = previousLastTimeRef.current;
    const previousFirstTime = previousFirstTimeRef.current;
    const visibleRangeBeforeUpdate = chart.timeScale().getVisibleLogicalRange();
    const previousDataLength = previousDataLengthRef.current;
    const activeSeries = chartData;
    const activeDataLength = period.mode === "timeline"
      ? visibleTimelineSeriesCount
      : visibleChartDataCount;
    const logicalDataLength = period.mode === "timeline"
      ? visibleTimelineLogicalCount
      : activeDataLength;
    const nextFirstTime = period.mode === "timeline"
      ? timelineLogicalDayRanges[0]?.chartFrom ?? Number.NaN
      : activeDataLength > 0 ? Number(activeSeries[0]?.time) : Number.NaN;
    const timelineLastTime = visibleTimelineCount > 0
      ? projectedTimelineData[visibleTimelineCount - 1]?.time
      : null;
    const nextLastTime = period.mode === "timeline"
      ? typeof timelineLastTime === "number" ? timelineLastTime : null
      : latestBar?.time ?? null;
    const nextLatestLogicalIndex = period.mode === "timeline"
      ? latestTimelineLogicalIndex ?? -1
      : activeDataLength - 1;
    dataLengthRef.current = logicalDataLength;
    latestLogicalIndexRef.current = nextLatestLogicalIndex;
    const previousCandleState = candleSeriesRenderStateRef.current;
    const previousCandleDataLength = previousCandleState?.dataLength ?? 0;
    const sameCandleSeriesData = previousCandleState?.data === chartData;
    const canAppendCandleIncrementally = period.mode !== "timeline"
      && sameCandleSeriesData
      && previousCandleDataLength > 0
      && activeDataLength >= previousCandleDataLength
      && activeDataLength <= previousCandleDataLength + MAX_INCREMENTAL_REPLAY_POINTS
      && previousLastTime !== null
      && nextLastTime !== null
      && nextLastTime >= previousLastTime;
    const candleLastPoint = activeDataLength > 0 ? chartData[activeDataLength - 1] : null;
    if (period.mode === "timeline" && timelineSeries) {
      const nextTimelineState: TimelineSeriesRenderState = {
        domain: timelineClockDomain,
        pointCount: visibleTimelineCount,
        dataLength: visibleTimelineSeriesCount,
        periodId: period.id,
      };
      const previousTimelineState = timelineSeriesRenderStateRef.current;
      const sharedPrefix = previousTimelineState !== null
        && previousTimelineState.periodId === period.id
        && (
          previousTimelineState.domain === timelineClockDomain
          || timelineClockDomainsSharePrefix(
            previousTimelineState.domain,
            timelineClockDomain,
            Math.min(previousTimelineState.pointCount, visibleTimelineCount),
          )
        );
      const pointDelta = previousTimelineState === null
        ? Number.POSITIVE_INFINITY
        : Math.abs(visibleTimelineCount - previousTimelineState.pointCount);
      if (!previousTimelineState || !sharedPrefix || pointDelta > MAX_INCREMENTAL_REPLAY_POINTS) {
        timelineSeries.setData(materializeTimelineClockData(
          timelineClockDomain,
          visibleTimelineCount,
        ));
      } else {
        if (visibleTimelineSeriesCount < previousTimelineState.dataLength) {
          timelineSeries.pop(previousTimelineState.dataLength - visibleTimelineSeriesCount);
        }
        if (visibleTimelineSeriesCount > previousTimelineState.dataLength) {
          for (
            let dataIndex = previousTimelineState.dataLength;
            dataIndex < visibleTimelineSeriesCount;
            dataIndex += 1
          ) {
            timelineSeries.update(timelineClockDataItemAt(
              timelineClockDomain,
              dataIndex,
              visibleTimelineCount,
            ));
          }
        }
        const changedIndexes = changedTimelineClockDataIndexes(
          timelineClockDomain,
          previousTimelineState.pointCount,
          visibleTimelineCount,
        );
        for (const dataIndex of changedIndexes) {
          if (dataIndex >= visibleTimelineSeriesCount) continue;
          timelineSeries.update(timelineClockDataItemAt(
            timelineClockDomain,
            dataIndex,
            visibleTimelineCount,
          ), true);
        }
      }
      timelineSeriesRenderStateRef.current = nextTimelineState;
    }
    if (period.mode !== "timeline" && candlestickSeries) {
      const candleDataAlreadyCurrent = sameCandleSeriesData
        && previousCandleDataLength === activeDataLength;
      if (!candleDataAlreadyCurrent && canAppendCandleIncrementally && candleLastPoint) {
        const firstUpdate = activeDataLength === previousCandleDataLength
          ? activeDataLength - 1
          : previousCandleDataLength;
        for (let index = firstUpdate; index < activeDataLength; index += 1) {
          candlestickSeries.update(chartData[index]);
        }
      } else if (!candleDataAlreadyCurrent) {
        candlestickSeries.setData(
          activeDataLength === chartData.length
            ? chartData
            : chartData.slice(0, activeDataLength),
        );
      }
      candleSeriesRenderStateRef.current = { data: chartData, dataLength: activeDataLength };
    }

    const firstDataArrival = previousDataLength === 0 && logicalDataLength > 0;
    const previousFirstIndex = (
      previousFirstTime !== null
      && Number.isFinite(nextFirstTime)
      && nextFirstTime < previousFirstTime
    )
      ? period.mode === "timeline"
        ? Math.max(
          0,
          (timelineLogicalDayRanges.find((range) => range.chartFrom === previousFirstTime)?.from ?? -0.5)
            + 0.5,
        )
        : activeSeries.findIndex((point) => Number(point.time) === previousFirstTime)
      : 0;
    const prependedCount = prependedPointCount(
      previousFirstTime,
      Number.isFinite(nextFirstTime) ? nextFirstTime : null,
      previousFirstIndex,
    );
    const historyWasPrepended = previousPeriod === period.id && prependedCount > 0;
    const tailMovedBackward = previousPeriod === period.id
      && previousLastTime !== null
      && nextLastTime !== null
      && nextLastTime < previousLastTime;
    const resetTimelineViewport = period.mode === "timeline"
      && logicalDataLength > 0
      && (firstDataArrival || previousPeriod === null || previousPeriod !== period.id);
    if (resetTimelineViewport) {
      timelineViewportSpanRef.current = 1;
      timelineViewportEndKeyRef.current = null;
      timelineViewportManagedRef.current = true;
    }
    const refreshManagedTimelineViewport = period.mode === "timeline"
      && logicalDataLength > 0
      && timelineViewportManagedRef.current
      && (
        resetTimelineViewport
        || historyWasPrepended
        || logicalDataLength !== previousDataLength
      );
    const timelineViewportHandled = refreshManagedTimelineViewport
      ? applyTimelineDayViewport(
        chart,
        timelineViewportSpanRef.current,
        followingRef.current || resetTimelineViewport ? null : timelineViewportEndKeyRef.current,
      ) !== null
      : false;
    if (timelineViewportHandled) {
      // The managed trading-day viewport already preserves its exact anchor.
    } else if (
      historyWasPrepended
      && visibleRangeBeforeUpdate
      && !followingRef.current
    ) {
      chart.timeScale().setVisibleLogicalRange({
        from: visibleRangeBeforeUpdate.from + prependedCount,
        to: visibleRangeBeforeUpdate.to + prependedCount,
      });
    } else if (historyWasPrepended && followingRef.current) {
      scrollToLatest(chart);
      updateFollowing(true);
    } else if (tailMovedBackward && logicalDataLength > 0 && period.mode !== "timeline") {
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
      nextLastTime > previousLastTime &&
      period.mode !== "timeline"
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
    previousChartDataRef.current = chartData;
    scheduleLiveMarker();
    refreshTimelineDecorations();
    refreshExpertDecorations();
  }, [
    applyTimelineDayViewport,
    chartData,
    displayTimeZone,
    latestBar?.time,
    latestTimelineLogicalIndex,
    period.id,
    period.mode,
    refreshExpertDecorations,
    refreshTimelineDecorations,
    scheduleLiveMarker,
    scrollToLatest,
    projectedTimelineData,
    timelineClockDomain,
    timelineLogicalDayRanges,
    updateFollowing,
    visibleChartDataCount,
    visibleTimelineLogicalCount,
    visibleTimelineCount,
    visibleTimelineSeriesCount,
  ]);

  useEffect(() => {
    const series = timelineSeriesRef.current;
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
  }, [displayTimeZone, period.mode, referencePrice, scheduleLiveMarker]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.applyOptions({
      timeScale: { tickMarkFormatter: axisTickFormatter },
      localization: { locale: "zh-CN", timeFormatter: axisTimeFormatter },
    });
    refreshTimelineDecorations();
    refreshExpertDecorations();
  }, [axisTickFormatter, axisTimeFormatter, displayTimeZone, refreshExpertDecorations, refreshTimelineDecorations]);

  useEffect(() => {
    const plugin = eventMarkersRef.current;
    if (!plugin) return;
    const markers = eventMarkers
      .map((event): SeriesMarker<Time> | null => {
        const chartTime = nearestChartTimeForActual(event.time);
        if (chartTime === null) return null;
        const text = event.category === "fomc"
          ? "FOMC"
          : event.category === "employment"
            ? "非农"
            : event.category === "inflation"
              ? "CPI"
              : event.category === "central-bank-gold" ? "央行金" : "事件";
        return {
          time: chartTime as Time,
          position: "aboveBar",
          color: event.importance === "high" ? "#d8a64b" : "#7896a7",
          shape: "circle",
          text,
          size: event.importance === "high" ? 1.3 : 1,
        };
      })
      .filter((marker): marker is SeriesMarker<Time> => marker !== null)
      .sort((left, right) => Number(left.time) - Number(right.time));
    plugin.setMarkers(markers);
  }, [chartData, displayTimeZone, eventMarkers, nearestChartTimeForActual, period.id, timelineClockDomain]);

  useEffect(() => {
    const series = activeMainSeries();
    if (!series) return;
    const previousOwner = strategyPriceLineOwnerRef.current;
    if (previousOwner) {
      for (const line of strategyPriceLinesRef.current) previousOwner.removePriceLine(line);
    }
    strategyPriceLinesRef.current = strategyLevels.map((level) => series.createPriceLine({
      price: level.price,
      color: level.tone === "gold"
        ? "#d5a84b"
        : level.tone === "support"
          ? "#3fa9b7"
          : level.tone === "resistance" ? "#d96c5f" : "#8796a0",
      lineWidth: 1,
      lineStyle: level.style === "solid"
        ? LineStyle.Solid
        : level.style === "dotted" ? LineStyle.Dotted : LineStyle.Dashed,
      axisLabelVisible: true,
      title: level.label,
    }));
    strategyPriceLineOwnerRef.current = series;
    scheduleLiveMarker();
  }, [activeMainSeries, displayTimeZone, period.id, scheduleLiveMarker, strategyLevels]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const desiredIds = new Set(visibleIndicatorLayers.map((layer) => layer.definition.id));
    for (const [layerId, runtime] of indicatorRuntimeRef.current) {
      if (desiredIds.has(layerId)) continue;
      removeIndicatorRuntime(chart, runtime);
      indicatorRuntimeRef.current.delete(layerId);
    }
    for (const layer of visibleIndicatorLayers) {
      const definition = layer.definition;
      let runtime = indicatorRuntimeRef.current.get(definition.id);
      if (runtime && runtime.kind !== definition.indicatorId) {
        removeIndicatorRuntime(chart, runtime);
        indicatorRuntimeRef.current.delete(definition.id);
        runtime = undefined;
      }
      if (!runtime) {
        runtime = createIndicatorRuntime(chart, definition);
        indicatorRuntimeRef.current.set(definition.id, runtime);
      }
      runtime.configuredHeight = definition.height;
      if (Math.abs(runtime.pane.getHeight() - definition.height) > 1) {
        runtime.pane.setHeight(definition.height);
      }
    }
    visibleIndicatorLayers.forEach((layer, index) => {
      indicatorRuntimeRef.current.get(layer.definition.id)?.pane.moveTo(index + 1);
    });
    schedulePaneMeasurement();
    refreshExpertDecorations();
  // The signature owns structure changes; indicator values update in the next effect.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appearance, displayTimeZone, indicatorLayerSignature, period.mode, refreshExpertDecorations, schedulePaneMeasurement]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const projectedTimeAt = (
      view: ExpertIndicatorSeriesView,
      localIndex: number,
    ): Time | null => {
      const actualTime = view.bars[view.offset + localIndex]?.time;
      if (!Number.isFinite(actualTime)) return null;
      if (period.mode !== "timeline") return projectTimeForChinaAxis(actualTime) as Time;
      const projected = projectTimelineTime(timelineLayout, actualTime);
      const bucketTime = projected === null
        ? null
        : timelineClockBucketTime(timelineLayout, projected);
      return bucketTime === null ? null : bucketTime as Time;
    };
    const gapTimeAt = (
      view: ExpertIndicatorSeriesView,
      sourceNextIndex: number,
    ): Time | null => {
      const gap = candleSeriesGaps.find((candidate) => candidate.nextIndex === sourceNextIndex);
      // Scheduled closures are compressed to zero width in every chart mode.
      // Only an unexpected in-session data hole gets a whitespace separator.
      if (!gap || gap.kind !== "missing-trade") return null;
      if (period.mode !== "timeline") return gap.time;
      const projected = projectTimelineTime(timelineLayout, gap.actualTime);
      const bucketTime = projected === null
        ? null
        : timelineClockBucketTime(timelineLayout, projected);
      return bucketTime === null ? null : bucketTime as Time;
    };

    const setFullData = (runtime: IndicatorRuntime, view: ExpertIndicatorSeriesView) => {
      const gapsByIndex = new Map<number, Time>();
      for (const gap of candleSeriesGaps) {
        const localNextIndex = gap.nextIndex - view.offset;
        if (localNextIndex <= 0 || localNextIndex >= view.visibleLength) continue;
        const time = gapTimeAt(view, gap.nextIndex);
        if (time !== null) gapsByIndex.set(localNextIndex, time);
      }
      if (runtime.kind === "kdj") {
        const k: Array<LineData<Time> | WhitespaceData<Time>> = [];
        const d: Array<LineData<Time> | WhitespaceData<Time>> = [];
        const j: Array<LineData<Time> | WhitespaceData<Time>> = [];
        for (let index = 0; index < view.visibleLength; index += 1) {
          const gapTime = gapsByIndex.get(index);
          if (gapTime !== undefined) {
            k.push({ time: gapTime });
            d.push({ time: gapTime });
            j.push({ time: gapTime });
          }
          const time = projectedTimeAt(view, index);
          if (time === null) continue;
          const sourceIndex = view.offset + index;
          k.push({ time, value: view.kdj.k[sourceIndex] });
          d.push({ time, value: view.kdj.d[sourceIndex] });
          j.push({ time, value: view.kdj.j[sourceIndex] });
        }
        runtime.k.setData(k);
        runtime.d.setData(d);
        runtime.j.setData(j);
        return;
      }
      const value: Array<LineData<Time> | WhitespaceData<Time>> = [];
      const signal: Array<LineData<Time> | WhitespaceData<Time>> = [];
      const histogram: Array<HistogramData<Time> | WhitespaceData<Time>> = [];
      for (let index = 0; index < view.visibleLength; index += 1) {
        const gapTime = gapsByIndex.get(index);
        if (gapTime !== undefined) {
          value.push({ time: gapTime });
          signal.push({ time: gapTime });
          histogram.push({ time: gapTime });
        }
        const time = projectedTimeAt(view, index);
        if (time === null) continue;
        const sourceIndex = view.offset + index;
        const histogramValue = view.macd.histogram[sourceIndex];
        value.push({ time, value: view.macd.value[sourceIndex] });
        signal.push({ time, value: view.macd.signal[sourceIndex] });
        histogram.push({
          time,
          value: histogramValue,
          color: histogramValue >= 0 ? "rgba(233, 67, 87, .55)" : "rgba(53, 170, 117, .55)",
        });
      }
      runtime.value.setData(value);
      runtime.signal.setData(signal);
      runtime.histogram.setData(histogram);
    };

    const updateSinglePoint = (
      runtime: IndicatorRuntime,
      view: ExpertIndicatorSeriesView,
      localIndex: number,
    ) => {
      const gapTime = gapTimeAt(view, view.offset + localIndex);
      if (gapTime !== null) {
        if (runtime.kind === "kdj") {
          runtime.k.update({ time: gapTime });
          runtime.d.update({ time: gapTime });
          runtime.j.update({ time: gapTime });
        } else {
          runtime.value.update({ time: gapTime });
          runtime.signal.update({ time: gapTime });
          runtime.histogram.update({ time: gapTime });
        }
      }
      const time = projectedTimeAt(view, localIndex);
      if (time === null) return;
      const sourceIndex = view.offset + localIndex;
      if (runtime.kind === "kdj") {
        runtime.k.update({ time, value: view.kdj.k[sourceIndex] });
        runtime.d.update({ time, value: view.kdj.d[sourceIndex] });
        runtime.j.update({ time, value: view.kdj.j[sourceIndex] });
        return;
      }
      const histogramValue = view.macd.histogram[sourceIndex];
      runtime.value.update({ time, value: view.macd.value[sourceIndex] });
      runtime.signal.update({ time, value: view.macd.signal[sourceIndex] });
      runtime.histogram.update({
        time,
        value: histogramValue,
        color: histogramValue >= 0 ? "rgba(233, 67, 87, .55)" : "rgba(53, 170, 117, .55)",
      });
    };

    for (const layer of indicatorLayers) {
      const runtime = indicatorRuntimeRef.current.get(layer.definition.id);
      if (!runtime || !layer.definition.visible) continue;
      const view = layer.series;
      const nextState: IndicatorRenderState = {
        historyKey: view.historyKey,
        revision: view.revision,
        offset: view.offset,
        visibleLength: view.visibleLength,
        firstTime: view.bars[view.offset]?.time ?? null,
        projectionKey: indicatorProjectionKey,
      };
      const previous = runtime.state;
      const sameBase = Boolean(
        previous
        && previous.historyKey === nextState.historyKey
        && previous.offset === nextState.offset
        && previous.firstTime === nextState.firstTime
        && previous.projectionKey === nextState.projectionKey,
      );
      if (
        sameBase
        && previous?.revision === nextState.revision
        && previous.visibleLength === nextState.visibleLength
      ) continue;

      let incrementalIndex: number | null = null;
      if (sameBase && previous && view.visibleLength >= previous.visibleLength) {
        if (view.revision === previous.revision && view.visibleLength === previous.visibleLength + 1) {
          incrementalIndex = previous.visibleLength;
        } else if (
          view.revision !== previous.revision
          && view.changedFrom === Math.max(0, view.visibleLength - 1)
        ) {
          incrementalIndex = view.changedFrom;
        }
      }
      if (incrementalIndex === null) setFullData(runtime, view);
      else updateSinglePoint(runtime, view, incrementalIndex);
      runtime.state = nextState;
    }
    schedulePaneMeasurement();
  }, [displayTimeZone, indicatorLayers, indicatorProjectionKey, period.mode, schedulePaneMeasurement]);

  useEffect(() => {
    const chart = chartRef.current;
    const series = activeMainSeries();
    if (!chart || !series) return;
    for (const runtime of drawingLayerRuntimeRef.current.values()) {
      for (const value of runtime.series) chart.removeSeries(value);
      for (const value of runtime.priceLines) runtime.priceLineOwner.removePriceLine(value);
    }
    drawingLayerRuntimeRef.current.clear();
    for (const layer of drawingLayers) {
      if (!layer.definition.visible) continue;
      const runtime: DrawingLayerRuntime = { series: [], priceLines: [], priceLineOwner: series };
      for (const drawing of layer.definition.drawings) {
        if (drawing.type === "horizontal") {
          runtime.priceLines.push(series.createPriceLine({
            price: drawing.start.price,
            color: drawing.color,
            lineWidth: 2,
            lineStyle: LineStyle.Dashed,
            axisLabelVisible: true,
            title: drawing.label,
          }));
          continue;
        }
        const startTime = nearestChartTimeForActual(drawing.start.time);
        const endTime = nearestChartTimeForActual(drawing.end.time);
        if (startTime === null || endTime === null || startTime === endTime) continue;
        const drawingSeries = chart.addSeries(LineSeries, {
          color: drawing.color,
          lineWidth: 2,
          lineStyle: LineStyle.Solid,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        });
        const values: LineData<Time>[] = [
          { time: startTime as Time, value: drawing.start.price },
          { time: endTime as Time, value: drawing.end.price },
        ].sort((left, right) => Number(left.time) - Number(right.time));
        drawingSeries.setData(values);
        runtime.series.push(drawingSeries);
      }
      drawingLayerRuntimeRef.current.set(layer.definition.id, runtime);
    }
    refreshExpertDecorations();
  // The drawing signature owns content changes; data-range changes cover replay and history prepend.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeMainSeries, displayTimeZone, drawingDataRangeKey, drawingLayerSignature, nearestChartTimeForActual, period.id, refreshExpertDecorations]);

  useEffect(() => {
    refreshExpertDecorations();
  }, [eventMarkers, openingGapVisible, refreshExpertDecorations, sessionBands, timelineSessionGaps, valueZones]);

  useEffect(() => {
    const refreshCountdown = () => {
      const value = replayMode
        ? "回放"
        : marketPhase === "closed"
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
          replayMode
            ? `回放价格 ${price.toFixed(priceDigits)}`
            : marketPhase === "closed"
            ? `休市最后价 ${price.toFixed(priceDigits)}，等待下一交易时段`
            : `当前价 ${price.toFixed(priceDigits)}，本周期剩余 ${value}`,
        );
      }
    };

    refreshCountdown();
    if (replayMode || marketPhase === "closed") return;
    const timer = window.setInterval(refreshCountdown, 250);
    return () => window.clearInterval(timer);
  }, [candles, marketPhase, period, priceDigits, replayMode]);

  const pointerDrawingLocation = useCallback((event: ReactPointerEvent<HTMLDivElement>): PointerDrawingLocation | null => {
    const chart = chartRef.current;
    const series = activeMainSeries();
    if (!chart || !series) return null;
    const bounds = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - bounds.left;
    const y = event.clientY - bounds.top;
    const chartTime = chart.timeScale().coordinateToTime(x as Coordinate);
    const price = series.coordinateToPrice(y as Coordinate);
    if (chartTime === null || price === null || !Number.isFinite(price)) return null;
    const actualTime = actualTimeForChartCoordinate(Number(chartTime));
    if (actualTime === null) return null;

    if (drawingSnapModeRef.current === "weak") {
      const timelineMode = periodRef.current.mode === "timeline";
      const times = timelineMode ? projectedTimelineData : candleTimes;
      const visibleLength = timelineMode ? visibleTimelineCount : visibleCandleCount;
      const snapped = weakDrawingSnap({
        times,
        visibleLength,
        targetTime: actualTime,
        pointerX: x,
        pointerY: y,
        pricesAt: (index) => {
          if (timelineMode) {
            const value = projectedTimelineData[index]?.value;
            return value === undefined ? [] : [value];
          }
          const bar = bars[index];
          return bar ? [bar.open, bar.high, bar.low, bar.close] : [];
        },
        timeToCoordinate: (time) => {
          const coordinate = chart.timeScale().timeToCoordinate(time as Time);
          return coordinate === null ? null : Number(coordinate);
        },
        priceToCoordinate: (candidatePrice) => {
          const coordinate = series.priceToCoordinate(candidatePrice);
          return coordinate === null ? null : Number(coordinate);
        },
      });
      if (snapped) {
        return {
          point: { time: snapped.time, price: snapped.price },
          x: snapped.x,
          y: snapped.y,
          snapped: true,
        };
      }
    }

    return { point: { time: actualTime, price }, x, y, snapped: false };
  }, [activeMainSeries, actualTimeForChartCoordinate, bars, candleTimes, projectedTimelineData, visibleCandleCount, visibleTimelineCount]);

  const beginDrawing = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    if (!drawingToolRef.current) return;
    const location = pointerDrawingLocation(event);
    if (!location) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    const draft = {
      start: location.point,
      startX: location.x,
      startY: location.y,
      startSnapped: location.snapped,
      currentX: location.x,
      currentY: location.y,
      currentSnapped: location.snapped,
    };
    drawingDraftRef.current = draft;
    setDrawingDraft(draft);
  }, [pointerDrawingLocation]);

  const updateDrawing = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const current = drawingDraftRef.current;
    if (!current) return;
    const location = pointerDrawingLocation(event);
    if (!location) return;
    const horizontal = drawingToolRef.current === "horizontal";
    const next = {
      ...current,
      currentX: location.x,
      currentY: horizontal
        ? current.startY
        : location.y,
      currentSnapped: horizontal ? current.startSnapped : location.snapped,
    };
    drawingDraftRef.current = next;
    setDrawingDraft(next);
  }, [pointerDrawingLocation]);

  const finishDrawing = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    const current = drawingDraftRef.current;
    const tool = drawingToolRef.current;
    if (!current || !tool) return;
    const endLocation = pointerDrawingLocation(event);
    drawingDraftRef.current = null;
    setDrawingDraft(null);
    if (!endLocation || !onDrawingCommitRef.current) return;
    if (
      tool === "trend"
      && Math.hypot(current.currentX - current.startX, current.currentY - current.startY) < 4
    ) return;
    const end = tool === "horizontal"
      ? { ...endLocation.point, price: current.start.price }
      : endLocation.point;
    onDrawingCommitRef.current({
      id: `drawing:${Date.now()}:${Math.round(current.start.time)}`,
      type: tool,
      start: current.start,
      end,
      color: tool === "horizontal" ? "#d5a84b" : "#e5edf1",
      label: tool === "horizontal" ? "手动画线" : "趋势线",
    });
  }, [pointerDrawingLocation]);

  const cancelDrawing = useCallback(() => {
    drawingDraftRef.current = null;
    setDrawingDraft(null);
  }, []);

  const returnToRealtime = () => {
    const chart = chartRef.current;
    if (!chart) return;
    returningRef.current = true;
    updateFollowing(true);
    scrollToLatest(chart);
    scheduleLiveMarker();
    refreshTimelineDecorations();
    refreshExpertDecorations();
    if (returnTimerRef.current !== null) window.clearTimeout(returnTimerRef.current);
    returnTimerRef.current = window.setTimeout(() => {
      returningRef.current = false;
    }, 420);
  };

  const renderedCountdown = replayMode
    ? "回放"
    : marketPhase === "closed"
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
      data-replay-mode={replayMode ? "true" : "false"}
      data-chart-appearance={appearance}
      data-drawings-visible={drawingsVisible ? "true" : "false"}
      data-drawing-snap-mode={drawingSnapMode}
      data-layer-count={layers.length}
      data-indicator-pane-count={visibleIndicatorLayers.length}
      data-timeline-available-days={period.mode === "timeline" ? timelineLogicalDayRanges.length : undefined}
      data-timeline-point-count={period.mode === "timeline" ? visibleTimelineCount : undefined}
      data-timeline-materialized-day-limit={period.mode === "timeline" ? timelineMaterializedDayCount : undefined}
      data-timeline-has-unmaterialized-history={period.mode === "timeline"
        ? String(timelineHasUnmaterializedHistory)
        : undefined}
    >
      <div className="chart-renderer" ref={rendererRef} />
      <div
        ref={expertOverlayLayerRef}
        className="expert-chart-overlays"
        aria-label="专家分析图层"
      />
      <div
        ref={tradingDayLayerRef}
        className="timeline-trading-days"
        role="group"
        aria-label="交易时段标注"
      />
      {drawingsVisible && drawingTool ? (
        <div
          className="chart-drawing-surface"
          data-tool={drawingTool}
          style={mainPaneHeight === null ? undefined : { height: `${mainPaneHeight}px`, bottom: "auto" }}
          role="application"
          aria-label={drawingTool === "horizontal" ? "点击价格位置绘制水平线" : "拖动绘制趋势线"}
          onPointerDown={beginDrawing}
          onPointerMove={updateDrawing}
          onPointerUp={finishDrawing}
          onPointerCancel={cancelDrawing}
          onKeyDown={(event) => {
            if (event.key === "Escape") cancelDrawing();
          }}
          tabIndex={0}
        >
          {drawingDraft ? (
            <svg className="chart-drawing-preview" aria-hidden="true">
              <line
                x1={drawingDraft.startX}
                y1={drawingDraft.startY}
                x2={drawingDraft.currentX}
                y2={drawingDraft.currentY}
              />
              {drawingDraft.startSnapped ? (
                <circle className="is-snapped" cx={drawingDraft.startX} cy={drawingDraft.startY} r="4" />
              ) : null}
              {drawingDraft.currentSnapped ? (
                <circle className="is-snapped" cx={drawingDraft.currentX} cy={drawingDraft.currentY} r="4" />
              ) : null}
            </svg>
          ) : null}
        </div>
      ) : null}
      {(period.mode === "timeline" ? visibleTimelineSeriesCount > 0 : latestBar) && livePrice !== null ? (
        <div
          ref={liveLayerRef}
          className={`live-price-layer ${marketPhase === "closed" || replayMode ? "is-market-closed" : ""}`}
          style={markerStyle}
          aria-label={replayMode
            ? `回放价格 ${livePrice.toFixed(priceDigits)}`
            : marketPhase === "closed"
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
              <span />{replayMode || marketPhase === "closed" ? "回到最新" : "回到实时"}
        </button>
      ) : null}
    </div>
  );
}
