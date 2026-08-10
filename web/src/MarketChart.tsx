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
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  TickMarkType,
  createChart,
  createSeriesMarkers,
  type AreaData,
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

import {
  barsFromCandles,
  buildTimelineSeries,
  candleSeriesUpdateStart,
  classifyCandleSeriesMutation,
  formatBarCountdown,
  timelineSampleFromCandle,
  type CandleSeriesMutation,
} from "./chartModel";
import { secondsUntilPeriodClose, type ChartPeriod } from "./chartPeriods";
import {
  DATE_ONLY_AXIS_THRESHOLD_SECONDS,
  actualTimeForChinaAxis,
  buildSeriesDataGaps,
  buildTimelineLayout,
  buildTimelineSessionGaps,
  formatBeijingDateTime,
  formatChartTick,
  formatChartTimeLabel,
  formatSessionGapDuration,
  projectTimeForChinaAxis,
  type SeriesDataGapKind,
  type TimelineLayout,
  type TimelineSessionGap,
} from "./chartTimeAxis";
import { weakDrawingSnap } from "./expertDrawing";
import type { ChartIndicatorLayer, ChartLayer } from "./chartLayers";
import {
  historyGapWindow,
  isNearOlderHistoryEdge,
  prependedPointCount,
  resolveHistoryDemandOutcome,
  shouldActivateOlderHistoryDemand,
  shouldRequestOlderHistory,
  type HistoryLoadOutcome,
  type HistoryWindow,
} from "./historyLoading";
import type {
  ExpertDrawing,
  ExpertDrawingPoint,
  ExpertDrawingSnapMode,
  ExpertDrawingTool,
  ExpertIndicatorSeriesView,
  ExpertOverlaySeries,
  ExpertPricePattern,
  ExpertTrendLine,
} from "./expertTypes";
import type { ExpertMarketStructureEvent } from "./expertSmartMoney.ts";
import type { Candle, HoverCandle, MarketPhase, MarketSchedule, TimelineSample } from "./types";
import type { RealtimeBarStream } from "./realtimeBarStream";

interface MarketChartProps {
  candles: Candle[];
  realtimeBarStream: RealtimeBarStream;
  realtimeBarStreamKey: string;
  period: ChartPeriod;
  livePrice: number | null;
  referencePrice: number | null;
  timelineResolutionSeconds: number;
  priceDigits: number;
  marketPhase: MarketPhase;
  marketSchedule: MarketSchedule | null | undefined;
  historyLoading: boolean;
  onRequestOlderHistory: () => Promise<HistoryLoadOutcome>;
  onRequestHistoryGap: (window: HistoryWindow) => void;
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

interface RenderableSeriesGap {
  kind: SeriesDataGapKind;
  nextIndex: number;
  time: Time;
  actualTime: number;
  missingDurationSeconds: number;
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

interface RsiIndicatorRuntime {
  kind: "rsi";
  pane: IPaneApi<Time>;
  configuredHeight: number;
  value: ISeriesApi<"Line">;
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

type IndicatorRuntime = RsiIndicatorRuntime | KdjIndicatorRuntime | MacdIndicatorRuntime;

interface DrawingLayerRuntime {
  series: Array<ISeriesApi<"Line">>;
  priceLines: IPriceLine[];
  priceLineOwner: MainSeriesApi;
}

interface SystemLineRuntime {
  series: Array<ISeriesApi<"Line">>;
}

function chartLineStyle(style: "solid" | "dashed" | "dotted"): LineStyle {
  if (style === "dashed") return LineStyle.Dashed;
  if (style === "dotted") return LineStyle.Dotted;
  return LineStyle.Solid;
}

function trendLineAppearance(line: ExpertTrendLine): {
  color: string;
  lineStyle: LineStyle;
  lineWidth: 1 | 2 | 3;
} {
  if (line.status === "invalidated") {
    return { color: "rgba(158, 173, 181, .24)", lineStyle: LineStyle.Dashed, lineWidth: 1 };
  }
  const opacity = line.status === "candidate" ? 0.36 : line.status === "confirmed" ? 0.68 : 0.9;
  return {
    color: line.direction === "support"
      ? `rgba(67, 190, 137, ${opacity})`
      : `rgba(230, 92, 112, ${opacity})`,
    lineStyle: line.status === "candidate" ? LineStyle.Dotted : LineStyle.Solid,
    lineWidth: line.status === "tested" ? 2 : 1,
  };
}

function pricePatternAppearance(pattern: ExpertPricePattern): {
  color: string;
  lineStyle: LineStyle;
  lineWidth: 1 | 2;
} {
  if (pattern.status === "invalidated") {
    return { color: "rgba(151, 164, 174, .24)", lineStyle: LineStyle.Dashed, lineWidth: 1 };
  }
  if (pattern.kind === "two-b-bottom" || pattern.kind === "two-b-top") {
    return {
      color: pattern.direction === "bullish"
        ? "rgba(232, 93, 110, .78)"
        : "rgba(73, 184, 135, .78)",
      lineStyle: LineStyle.Solid,
      lineWidth: 2,
    };
  }
  return {
    color: pattern.direction === "bullish"
      ? "rgba(232, 93, 110, .8)"
      : "rgba(73, 184, 135, .8)",
    lineStyle: LineStyle.Solid,
    lineWidth: 2,
  };
}

function marketStructureAppearance(event: ExpertMarketStructureEvent): {
  color: string;
  lineStyle: LineStyle;
  lineWidth: 1 | 2;
} {
  if (event.status === "invalidated") {
    return { color: "rgba(151, 164, 174, .2)", lineStyle: LineStyle.Dashed, lineWidth: 1 };
  }
  const sweep = event.kind === "low-liquidity-sweep" || event.kind === "high-liquidity-sweep";
  return {
    color: sweep
      ? "rgba(213, 168, 75, .72)"
      : event.direction === "bullish" ? "rgba(232, 93, 110, .68)" : "rgba(73, 184, 135, .68)",
    lineStyle: sweep ? LineStyle.Dotted : LineStyle.Dashed,
    lineWidth: sweep ? 1 : 2,
  };
}

type MainSeriesApi = ISeriesApi<"Candlestick"> | ISeriesApi<"Area">;

interface TimelineSeriesRenderState {
  data: Array<AreaData<Time> | WhitespaceData<Time>>;
  dataLength: number;
  periodId: string;
}

interface TimelineSeriesCache {
  datasetKey: string;
  candles: Candle[];
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
  if (layer.indicatorId === "rsi") {
    const value = chart.addSeries(LineSeries, {
      title: "RSI 14",
      color: "#b49af4",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      crosshairMarkerVisible: false,
      priceFormat: { type: "price", precision: 1, minMove: 0.1 },
    }, paneIndex);
    for (const price of [30, 50, 70]) {
      value.createPriceLine({
        price,
        color: price === 50 ? "rgba(213, 168, 75, .24)" : "rgba(130, 154, 168, .3)",
        lineWidth: 1,
        lineStyle: price === 50 ? LineStyle.Dashed : LineStyle.Dotted,
        axisLabelVisible: false,
        title: "",
      });
    }
    pane.priceScale("right").applyOptions({
      borderVisible: false,
      scaleMargins: { top: 0.08, bottom: 0.08 },
    });
    pane.setHeight(layer.height);
    return { kind: "rsi", pane, configuredHeight: layer.height, value, state: null };
  }
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
  if (runtime.kind === "rsi") {
    chart.removeSeries(runtime.value);
  } else if (runtime.kind === "kdj") {
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
  realtimeBarStream,
  realtimeBarStreamKey,
  period,
  livePrice,
  referencePrice,
  timelineResolutionSeconds,
  priceDigits,
  marketPhase,
  marketSchedule,
  historyLoading,
  onRequestOlderHistory,
  onRequestHistoryGap,
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
  const smartTrendLines = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.trendLines),
    [annotationLayers],
  );
  const pricePatterns = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.pricePatterns),
    [annotationLayers],
  );
  const marketStructureEvents = useMemo(
    () => annotationLayers
      .filter((layer) => layer.definition.visible)
      .flatMap((layer) => layer.marketStructureEvents),
    [annotationLayers],
  );
  const technicalOverlaySeries = useMemo(
    () => annotationLayers.filter((layer) => layer.definition.visible).flatMap((layer) => layer.overlaySeries),
    [annotationLayers],
  );
  const smartTrendLineSignature = smartTrendLines.map((line) => [
    line.id,
    line.status,
    line.start.time,
    line.start.price,
    line.anchor.time,
    line.anchor.price,
    line.end.time,
    line.end.price,
    line.touchCount,
    line.quality,
    line.invalidatedAt ?? "",
  ].join(":")).join("|");
  const structureMarkSignature = [
    ...pricePatterns.map((pattern) => [
      pattern.id,
      pattern.status,
      pattern.first.time,
      pattern.first.price,
      pattern.neckline?.time ?? "",
      pattern.neckline?.price ?? "",
      pattern.second.time,
      pattern.second.price,
      pattern.confirmation.time,
      pattern.confirmation.price,
      pattern.invalidatedAt ?? "",
    ].join(":")),
    ...marketStructureEvents.map((event) => [
      event.id,
      event.status,
      event.reference.time,
      event.reference.price,
      event.confirmation.time,
      event.confirmation.price,
      event.invalidatedAt ?? "",
    ].join(":")),
  ].join("|");
  const technicalOverlaySignature = technicalOverlaySeries.map((series) => {
    const first = series.points[0];
    const last = series.points.at(-1);
    return [
      series.id,
      series.color,
      series.lineStyle,
      series.lineWidth,
      series.lastValueVisible ? 1 : 0,
      series.points.length,
      first?.time ?? "",
      first?.value ?? "",
      last?.time ?? "",
      last?.value ?? "",
    ].join(":");
  }).join("|");
  const drawingsVisible = drawingLayers.some((layer) => layer.definition.visible);
  const rendererRef = useRef<HTMLDivElement>(null);
  const liveLayerRef = useRef<HTMLDivElement>(null);
  const livePriceValueRef = useRef<HTMLElement>(null);
  const countdownRef = useRef<HTMLSpanElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candlestickSeriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const timelineSeriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const timelineSeriesRenderStateRef = useRef<TimelineSeriesRenderState | null>(null);
  const timelineSeriesCacheRef = useRef<TimelineSeriesCache | null>(null);
  const candleSeriesRenderStateRef = useRef<CandleSeriesRenderState | null>(null);
  const renderedCandlesRef = useRef<Candle[] | null>(null);
  const referenceLineRef = useRef<IPriceLine | null>(null);
  const eventMarkersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const drawingLayerRuntimeRef = useRef<Map<string, DrawingLayerRuntime>>(new Map());
  const systemLineRuntimeRef = useRef<SystemLineRuntime>({ series: [] });
  const indicatorRuntimeRef = useRef<Map<string, IndicatorRuntime>>(new Map());
  const strategyPriceLinesRef = useRef<IPriceLine[]>([]);
  const strategyPriceLineOwnerRef = useRef<MainSeriesApi | null>(null);
  const markerFrameRef = useRef<number | null>(null);
  const hoverFrameRef = useRef<number | null>(null);
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
  const requestHistoryGapRef = useRef(onRequestHistoryGap);
  const historyDemandActiveRef = useRef(false);
  const historyRequestPendingRef = useRef(false);
  const emptyHistoryAdvanceMinutesRef = useRef(0);
  const evaluateHistoryDemandRef = useRef<((userInitiated?: boolean) => void) | null>(null);
  const candleSeriesGapsRef = useRef<readonly RenderableSeriesGap[]>([]);
  const dispatchedHistoryGapsRef = useRef(new Set<string>());
  const historyGapDatasetKeyRef = useRef("");
  const expertOverlayLayerRef = useRef<HTMLDivElement>(null);
  const timelineLayoutRef = useRef<TimelineLayout>({ days: [] });
  const timelineSessionGapsRef = useRef<TimelineSessionGap[]>([]);
  const visibleCandleSpanRef = useRef(0);
  const candleDateOnlyAxisRef = useRef(false);
  const periodRef = useRef(period);
  const timelineResolutionSecondsRef = useRef(timelineResolutionSeconds);
  const displayTimeZoneRef = useRef(displayTimeZone);
  const sessionBandsRef = useRef(sessionBands);
  const eventMarkersByIdRef = useRef(eventMarkersById);
  const openingGapVisibleRef = useRef(openingGapVisible);
  const valueZonesRef = useRef(valueZones);
  const smartTrendLinesRef = useRef(smartTrendLines);
  const pricePatternsRef = useRef(pricePatterns);
  const marketStructureEventsRef = useRef(marketStructureEvents);
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
  requestHistoryGapRef.current = onRequestHistoryGap;
  periodRef.current = period;
  timelineResolutionSecondsRef.current = timelineResolutionSeconds;
  displayTimeZoneRef.current = displayTimeZone;
  sessionBandsRef.current = sessionBands;
  eventMarkersByIdRef.current = eventMarkersById;
  openingGapVisibleRef.current = openingGapVisible;
  valueZonesRef.current = valueZones;
  smartTrendLinesRef.current = smartTrendLines;
  pricePatternsRef.current = pricePatterns;
  marketStructureEventsRef.current = marketStructureEvents;
  drawingToolRef.current = drawingTool;
  drawingSnapModeRef.current = drawingSnapMode;
  onDrawingCommitRef.current = onDrawingCommit;
  onIndicatorPaneResizeRef.current = onIndicatorPaneResize;
  onHoverRef.current = onHover;
  const [isFollowing, setIsFollowing] = useState(true);
  const [drawingDraft, setDrawingDraft] = useState<DrawingDraft | null>(null);
  const [mainPaneHeight, setMainPaneHeight] = useState<number | null>(null);

  const latestCandleIdentity = candles.at(-1);
  const timelineDatasetKey = [
    latestCandleIdentity?.instrument.symbol ?? "",
    latestCandleIdentity?.source.provider ?? "",
  ].join(":");
  const historyGapDatasetKey = `${period.id}:${timelineDatasetKey}`;
  if (historyGapDatasetKeyRef.current !== historyGapDatasetKey) {
    historyGapDatasetKeyRef.current = historyGapDatasetKey;
    dispatchedHistoryGapsRef.current.clear();
  }
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
  const candleResolutionSeconds = period.mode === "timeline"
    ? 1
    : period.aggregation.kind === "fixed"
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
        missingDurationSeconds: gap.missingDurationSeconds,
      })),
    [candleGapLayout, candleResolutionSeconds, candleTimes],
  );
  candleSeriesGapsRef.current = candleSeriesGaps;
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
  const rawTimelineData = useMemo(() => {
    const cache = timelineSeriesCacheRef.current;
    if (period.mode !== "timeline") {
      return cache?.datasetKey === timelineDatasetKey
        ? cache.data
        : [];
    }
    if (
      cache?.datasetKey === timelineDatasetKey
      && cache.candles === candles
    ) return cache.data;
    const data = buildTimelineSeries(candles);
    timelineSeriesCacheRef.current = {
      datasetKey: timelineDatasetKey,
      candles,
      data,
    };
    return data;
  }, [
    candles,
    period.mode,
    timelineDatasetKey,
  ]);
  const timelineLayout = useMemo(() => {
    const actualTimes = rawTimelineData
      .map((point) => point.observedTime ?? point.time)
      .filter(Number.isFinite);
    return buildTimelineLayout(actualTimes, marketSchedule);
  }, [marketSchedule, rawTimelineData]);
  timelineLayoutRef.current = timelineLayout;
  const projectedTimelineData = useMemo(
    () => rawTimelineData.map((point) => ({
      ...point,
      actualTime: point.observedTime ?? point.time,
      time: projectTimeForChinaAxis(point.time),
    })),
    [rawTimelineData],
  );
  projectedTimesRef.current = projectedTimelineData;
  const visibleTimelineCount = replayMode
    ? timelinePrefixCount(projectedTimelineData, replayCutoff)
    : projectedTimelineData.length;
  visibleProjectedLengthRef.current = visibleTimelineCount;
  const timelineSessionGaps = useMemo(
    () => buildTimelineSessionGaps(timelineLayout, projectedTimelineData, priceDigits),
    [priceDigits, projectedTimelineData, timelineLayout],
  );
  timelineSessionGapsRef.current = timelineSessionGaps;
  const timelineSeriesGaps = useMemo<RenderableSeriesGap[]>(
    () => buildSeriesDataGaps(
      projectedTimelineData,
      1,
      marketSchedule?.sessions.length ? timelineLayout : null,
    ).map((gap) => ({
      kind: gap.kind,
      nextIndex: gap.nextIndex,
      time: projectTimeForChinaAxis(gap.separatorTime) as Time,
      actualTime: gap.separatorTime,
      missingDurationSeconds: gap.missingDurationSeconds,
    })),
    [marketSchedule, projectedTimelineData, timelineLayout],
  );
  const timelineSeriesData = useMemo(
    () => insertGapWhitespace(
      projectedTimelineData.map((point) => ({
        time: point.time as Time,
        value: point.value,
      })),
      timelineSeriesGaps,
    ),
    [projectedTimelineData, timelineSeriesGaps],
  );
  const visibleTimelineSeriesCount = gapAwarePrefixLength(
    visibleTimelineCount,
    timelineSeriesGaps,
  );
  const drawingDataRangeKey = period.mode === "timeline"
    ? `timeline:${projectedTimelineData[0]?.actualTime ?? ""}:${replayMode ? projectedTimelineData[visibleTimelineCount - 1]?.actualTime ?? "" : "live"}`
    : `candles:${candleTimes[0]?.actualTime ?? ""}:${replayMode ? candleTimes[visibleCandleCount - 1]?.actualTime ?? "" : "live"}`;
  const indicatorProjectionKey = useMemo(() => [
    period.mode,
    period.id,
    displayTimeZone,
    ...candleSeriesGaps.map((gap) => `gap:${gap.nextIndex}:${gap.actualTime}`),
  ].join("|"), [candleSeriesGaps, displayTimeZone, period.id, period.mode]);
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

  const axisTickFormatter = useCallback((time: Time, tickMarkType: TickMarkType) => {
    const chartTime = Number(time);
    const activePeriod = periodRef.current;
    return formatChartTick(
      actualTimeForChinaAxis(chartTime),
      tickMarkType,
      activePeriod,
      visibleCandleSpanRef.current,
      displayTimeZoneRef.current,
    );
  }, []);

  const axisTimeFormatter = useCallback((time: Time) => {
    const chartTime = Number(time);
    const activePeriod = periodRef.current;
    return formatChartTimeLabel(
      actualTimeForChinaAxis(chartTime),
      activePeriod,
      activePeriod.mode === "timeline" ? timelineResolutionSecondsRef.current : 60,
      displayTimeZoneRef.current,
    );
  }, []);

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
    return nearestTime;
  }, []);

  const actualTimeForChartCoordinate = useCallback((chartTime: number): number | null => {
    return actualTimeForChinaAxis(chartTime);
  }, []);

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
      for (const trendLine of smartTrendLinesRef.current) {
        const endTime = nearestChartTimeForActual(trendLine.end.time);
        if (endTime === null) continue;
        const xCoordinate = chart.timeScale().timeToCoordinate(endTime as Time);
        const yCoordinate = series.priceToCoordinate(trendLine.end.price);
        if (xCoordinate === null || yCoordinate === null) continue;
        const x = Number(xCoordinate);
        const y = Number(yCoordinate);
        if (x < 0 || x > plotWidth || y < 0 || y > paneHeight) continue;
        const label = document.createElement("span");
        label.className = `smart-trend-label is-${trendLine.direction} is-${trendLine.status}`;
        label.style.left = `${Math.min(plotWidth - 8, x).toFixed(2)}px`;
        label.style.top = `${y.toFixed(2)}px`;
        label.textContent = trendLine.status === "invalidated"
          ? "已失效"
          : `${trendLine.direction === "support" ? "支撑" : "压力"} · ${trendLine.touchCount}触`;
        label.title = [
          `智能${trendLine.direction === "support" ? "支撑" : "压力"}趋势线`,
          `状态 ${trendLine.status}`,
          `质量 ${(trendLine.quality * 100).toFixed(0)}%`,
          `平均误差 ${trendLine.atrError.toFixed(2)} ATR`,
          trendLine.invalidationReason,
        ].filter(Boolean).join(" · ");
        label.setAttribute("role", "note");
        fragment.append(label);
      }
      const appendStructureStamp = (
        time: number,
        price: number,
        className: string,
        labelText: string,
        stateText: string,
        title: string,
      ) => {
        const chartTime = nearestChartTimeForActual(time);
        if (chartTime === null) return;
        const xCoordinate = chart.timeScale().timeToCoordinate(chartTime as Time);
        const yCoordinate = series.priceToCoordinate(price);
        if (xCoordinate === null || yCoordinate === null) return;
        const x = Number(xCoordinate);
        const y = Number(yCoordinate);
        if (x < 0 || x > plotWidth || y < 0 || y > paneHeight) return;
        const stamp = document.createElement("span");
        stamp.className = `structure-stamp ${className}`;
        if (x > plotWidth - 86) stamp.classList.add("is-align-left");
        if (y < 44) stamp.classList.add("is-below");
        stamp.style.left = `${x.toFixed(2)}px`;
        stamp.style.top = `${y.toFixed(2)}px`;
        stamp.title = title;
        stamp.setAttribute("role", "note");
        stamp.setAttribute("aria-label", title);
        stamp.tabIndex = 0;
        const mark = document.createElement("strong");
        mark.textContent = labelText;
        stamp.append(mark);
        const state = document.createElement("small");
        state.textContent = stateText;
        stamp.append(state);
        fragment.append(stamp);
      };
      for (const pattern of pricePatternsRef.current) {
        appendStructureStamp(
          pattern.confirmation.time,
          pattern.confirmation.price,
          `is-pattern is-${pattern.kind} is-${pattern.direction} is-${pattern.status}`,
          pattern.kind === "double-bottom"
            ? "W"
            : pattern.kind === "double-top"
              ? "M"
              : pattern.kind === "two-b-bottom" ? "2B↑" : "2B↓",
          pattern.status === "invalidated"
            ? "失效"
            : pattern.kind === "two-b-bottom" ? "底部" : pattern.kind === "two-b-top" ? "顶部" : "确认",
          [
            pattern.label,
            `置信 ${Math.round(pattern.confidence * 100)}%`,
            ...pattern.evidence,
          ].join(" · "),
        );
      }
      for (const event of marketStructureEventsRef.current) {
        appendStructureStamp(
          event.confirmation.time,
          event.confirmation.price,
          `is-market-structure is-${event.direction} is-${event.status}`,
          event.label,
          event.status === "invalidated" ? "失效" : "结构",
          [
            event.label,
            `置信 ${Math.round(event.confidence * 100)}%`,
            ...event.evidence,
          ].join(" · "),
        );
      }
      layer.replaceChildren(fragment);
    });
  }, [actualTimeForChartCoordinate, nearestChartTimeForActual, priceDigits]);

  const refreshTimelineDecorations = useCallback(() => {
    // Standard time-scale labels and explicit coverage metadata replace the
    // former synthetic trading-day clock overlay.
  }, []);

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
        barSpacing: initialTimelineMode ? 4 : 10,
        minBarSpacing: initialTimelineMode ? 0.5 : 3,
        lockVisibleTimeRangeOnResize: true,
        minimumHeight: 28,
        allowBoldLabels: false,
        tickMarkMaxCharacterLength: 8,
        tickMarkFormatter: axisTickFormatter,
        enableConflation: false,
        uniformDistribution: false,
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
    let timelineSeries: ISeriesApi<"Area"> | null = null;
    if (initialTimelineMode) {
      timelineSeries = chart.addSeries(AreaSeries, {
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
        if (!item || !("value" in item)) {
          scheduleHoverChange(null);
          return;
        }
        scheduleHoverChange({
          time: actualTimeForChinaAxis(Number(parameter.time)),
          open: item.value,
          high: item.value,
          low: item.value,
          close: item.value,
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
    const repairVisibleCandleGaps = (range: LogicalRange) => {
      if (periodRef.current.mode === "timeline") return;
      let whitespaceOffset = 0;
      let requested = 0;
      for (const gap of candleSeriesGapsRef.current) {
        if (gap.kind !== "missing-trade") continue;
        const logicalIndex = gap.nextIndex + whitespaceOffset;
        whitespaceOffset += 1;
        if (logicalIndex < range.from - 1 || logicalIndex > range.to + 1) continue;
        const gapWindow = historyGapWindow(gap.actualTime, gap.missingDurationSeconds);
        const gapKey = `${gapWindow.start}:${gapWindow.count}`;
        if (dispatchedHistoryGapsRef.current.has(gapKey)) continue;
        dispatchedHistoryGapsRef.current.add(gapKey);
        requestHistoryGapRef.current(gapWindow);
        requested += 1;
        if (requested >= 4) break;
      }
    };
    const runOlderHistoryRequest = () => {
      if (historyRequestPendingRef.current || historyLoadingRef.current) return;
      historyRequestPendingRef.current = true;
      void Promise.resolve(requestOlderHistoryRef.current())
        .then((outcome) => {
          if (chartRef.current !== chart) return;
          const resolution = resolveHistoryDemandOutcome(
            emptyHistoryAdvanceMinutesRef.current,
            outcome,
          );
          emptyHistoryAdvanceMinutesRef.current = resolution.emptyAdvanceMinutes;
          historyDemandActiveRef.current = resolution.active;
        })
        .catch(() => {
          if (chartRef.current === chart) historyDemandActiveRef.current = false;
        })
        .finally(() => {
          if (chartRef.current === chart) historyRequestPendingRef.current = false;
        });
    };
    const evaluateHistoryDemand = (userInitiated = false) => {
      const range = chart.timeScale().getVisibleLogicalRange();
      if (!range || returningRef.current || dataLengthRef.current === 0) return;
      repairVisibleCandleGaps(range);
      if (!isNearOlderHistoryEdge(range, dataLengthRef.current)) {
        historyDemandActiveRef.current = false;
        emptyHistoryAdvanceMinutesRef.current = 0;
        return;
      }
      if (shouldActivateOlderHistoryDemand(range, dataLengthRef.current, userInitiated)) {
        historyDemandActiveRef.current = true;
      }
      if (!historyDemandActiveRef.current) return;
      if (shouldRequestOlderHistory(
        range,
        dataLengthRef.current,
        historyLoadingRef.current || historyRequestPendingRef.current,
        historyDemandActiveRef.current,
      )) {
        runOlderHistoryRequest();
      }
    };
    evaluateHistoryDemandRef.current = evaluateHistoryDemand;
    const handleLogicalRange = (range: LogicalRange | null) => {
      scheduleLiveMarker();
      refreshTimelineDecorations();
      refreshExpertDecorations();
      const activePeriod = periodRef.current;
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
      if (!range || returningRef.current || dataLengthRef.current === 0) return;
      updateFollowing(range.to >= latestLogicalIndexRef.current - 1.15);
      evaluateHistoryDemand(
        window.performance.now() <= historyInteractionUntilRef.current,
      );
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(handleLogicalRange);

    const handlePointerMove = (event: PointerEvent) => {
      scheduleLiveMarker();
      if (event.buttons > 0) {
        markHistoryInteraction();
        refreshExpertDecorations();
      }
    };
    const handlePointerDown = () => {
      markHistoryInteraction();
    };
    const handleWheel = () => {
      markHistoryInteraction();
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
    container.addEventListener("wheel", handleWheel, { passive: true, capture: true });

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
      if (expertDecorationFrameRef.current !== null) {
        window.cancelAnimationFrame(expertDecorationFrameRef.current);
      }
      if (paneMeasureFrameRef.current !== null) {
        window.cancelAnimationFrame(paneMeasureFrameRef.current);
      }
      markerFrameRef.current = null;
      hoverFrameRef.current = null;
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
      systemLineRuntimeRef.current.series = [];
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
      renderedCandlesRef.current = null;
      historyDemandActiveRef.current = false;
      historyRequestPendingRef.current = false;
      emptyHistoryAdvanceMinutesRef.current = 0;
      evaluateHistoryDemandRef.current = null;
    };
  }, [
    appearance,
    axisTickFormatter,
    axisTimeFormatter,
    period.mode,
    refreshExpertDecorations,
    refreshTimelineDecorations,
    scheduleLiveMarker,
    schedulePaneMeasurement,
    scheduleHoverChange,
    updateFollowing,
  ]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      evaluateHistoryDemandRef.current?.(false);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [
    candleSeriesGaps,
    chartData.length,
    historyLoading,
    period.id,
  ]);

  useEffect(() => {
    if (replayMode) return;
    return realtimeBarStream.subscribe(({ datasetKey, bar }) => {
      if (datasetKey !== realtimeBarStreamKey) return;
      const price = Number(bar.close);
      if (!Number.isFinite(price)) return;

      const activePeriod = periodRef.current;
      if (activePeriod.mode === "timeline") {
        const sample = timelineSampleFromCandle(bar);
        const series = timelineSeriesRef.current;
        if (!sample || !series) return;
        const time = projectTimeForChinaAxis(sample.time);
        if (previousLastTimeRef.current !== null && time < previousLastTimeRef.current) return;
        series.update({ time: time as Time, value: sample.value });
      } else {
        const series = candlestickSeriesRef.current;
        const value = barsFromCandles([bar])[0];
        if (!series || !value) return;
        const time = projectTimeForChinaAxis(value.time);
        if (previousLastTimeRef.current !== null && time < previousLastTimeRef.current) return;
        series.update({ ...value, time: time as Time });
      }

      latestPriceRef.current = price;
      if (livePriceValueRef.current) {
        livePriceValueRef.current.textContent = price.toFixed(priceDigits);
      }
      const comparison = activePeriod.mode === "timeline"
        ? referencePrice
        : Number(bar.open);
      const layer = liveLayerRef.current;
      if (layer) {
        layer.style.setProperty(
          "--live-color",
          comparison !== null && Number.isFinite(comparison) && price >= comparison
            ? UP_COLOR
            : DOWN_COLOR,
        );
        layer.setAttribute("aria-label", `当前价 ${price.toFixed(priceDigits)}`);
      }
      scheduleLiveMarker();
    });
  }, [
    priceDigits,
    realtimeBarStream,
    realtimeBarStreamKey,
    referencePrice,
    replayMode,
    scheduleLiveMarker,
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
        barSpacing: period.mode === "timeline" ? 4 : 10,
        minBarSpacing: period.mode === "timeline" ? 0.5 : 3,
        tickMarkFormatter: axisTickFormatter,
        enableConflation: false,
        uniformDistribution: false,
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
    const candleMutation: CandleSeriesMutation = previousPeriod === period.id
      ? classifyCandleSeriesMutation(renderedCandlesRef.current, candles)
      : "reset";
    const visibleRangeBeforeUpdate = chart.timeScale().getVisibleLogicalRange();
    const previousDataLength = previousDataLengthRef.current;
    const activeSeries = period.mode === "timeline" ? timelineSeriesData : chartData;
    const activeDataLength = period.mode === "timeline"
      ? visibleTimelineSeriesCount
      : visibleChartDataCount;
    const logicalDataLength = activeDataLength;
    const nextFirstTime = activeDataLength > 0
      ? Number(activeSeries[0]?.time)
      : Number.NaN;
    const nextLastTime = activeDataLength > 0
      ? Number(activeSeries[activeDataLength - 1]?.time)
      : null;
    const nextLatestLogicalIndex = activeDataLength - 1;
    dataLengthRef.current = logicalDataLength;
    latestLogicalIndexRef.current = nextLatestLogicalIndex;
    const previousCandleState = candleSeriesRenderStateRef.current;
    const previousCandleDataLength = previousCandleState?.dataLength ?? 0;
    const candleUpdateStart = candleSeriesUpdateStart(
      candleMutation,
      previousCandleDataLength,
      activeDataLength,
      MAX_INCREMENTAL_REPLAY_POINTS,
    );
    const realtimeTailOwnedByStream = !replayMode
      && (candleMutation === "tail-update" || candleMutation === "tail-append");
    const canUpdateCandleIncrementally = period.mode !== "timeline"
      && replayMode
      && candleUpdateStart !== null
      && previousLastTime !== null
      && nextLastTime !== null
      && nextLastTime >= previousLastTime
      && (
        candleMutation !== "tail-append"
        || activeDataLength === previousCandleDataLength + 1
      );
    const candleLastPoint = activeDataLength > 0 ? chartData[activeDataLength - 1] : null;
    if (period.mode === "timeline" && timelineSeries) {
      const previousTimelineState = timelineSeriesRenderStateRef.current;
      const previousTimelineDataLength = previousTimelineState?.dataLength ?? 0;
      const timelineUpdateStart = candleSeriesUpdateStart(
        candleMutation,
        previousTimelineDataLength,
        activeDataLength,
        MAX_INCREMENTAL_REPLAY_POINTS,
      );
      const canUpdateTimelineIncrementally = timelineUpdateStart !== null
        && replayMode
        && previousTimelineState?.periodId === period.id
        && previousLastTime !== null
        && nextLastTime !== null
        && nextLastTime >= previousLastTime
        && (
          candleMutation !== "tail-append"
          || activeDataLength === previousTimelineDataLength + 1
        );
      const timelineDataAlreadyCurrent = realtimeTailOwnedByStream || (
        candleMutation === "unchanged"
        && previousTimelineState?.data === timelineSeriesData
        && previousTimelineDataLength === activeDataLength
      );
      if (!timelineDataAlreadyCurrent && canUpdateTimelineIncrementally) {
        for (let index = timelineUpdateStart; index < activeDataLength; index += 1) {
          timelineSeries.update(timelineSeriesData[index]);
        }
      } else if (!timelineDataAlreadyCurrent) {
        timelineSeries.setData(
          activeDataLength === timelineSeriesData.length
            ? timelineSeriesData
            : timelineSeriesData.slice(0, activeDataLength),
        );
      }
      timelineSeriesRenderStateRef.current = {
        data: timelineSeriesData,
        dataLength: activeDataLength,
        periodId: period.id,
      };
    }
    if (period.mode !== "timeline" && candlestickSeries) {
      const candleDataAlreadyCurrent = realtimeTailOwnedByStream || (
        candleMutation === "unchanged"
        && previousCandleDataLength === activeDataLength
      );
      if (!candleDataAlreadyCurrent && canUpdateCandleIncrementally && candleLastPoint) {
        for (let index = candleUpdateStart; index < activeDataLength; index += 1) {
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
      ? activeSeries.findIndex((point) => Number(point.time) === previousFirstTime)
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
      scrollToLatest(chart);
      updateFollowing(true);
    } else if (tailMovedBackward && logicalDataLength > 0) {
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
    previousChartDataRef.current = chartData;
    renderedCandlesRef.current = candles;
    scheduleLiveMarker();
    refreshTimelineDecorations();
    refreshExpertDecorations();
  }, [
    chartData,
    candles,
    period.id,
    period.mode,
    refreshExpertDecorations,
    refreshTimelineDecorations,
    replayMode,
    scheduleLiveMarker,
    scrollToLatest,
    timelineSeriesData,
    updateFollowing,
    visibleChartDataCount,
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
        const highPriority = event.baselineTier === "S+" || event.baselineTier === "S";
        return {
          time: chartTime as Time,
          position: "aboveBar",
          color: highPriority ? "#d8a64b" : "#7896a7",
          shape: "circle",
          text: `${event.shortLabel} · ${event.baselineTier}`,
          size: highPriority ? 1.3 : 1,
        };
      })
      .filter((marker): marker is SeriesMarker<Time> => marker !== null)
      .sort((left, right) => Number(left.time) - Number(right.time));
    plugin.setMarkers(markers);
  }, [chartData, displayTimeZone, eventMarkers, nearestChartTimeForActual, period.id]);

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
      return projectTimeForChinaAxis(actualTime) as Time;
    };
    const gapTimeAt = (
      view: ExpertIndicatorSeriesView,
      sourceNextIndex: number,
    ): Time | null => {
      const gap = candleSeriesGaps.find((candidate) => candidate.nextIndex === sourceNextIndex);
      // Scheduled closures are compressed to zero width in every chart mode.
      // Only an unexpected in-session data hole gets a whitespace separator.
      if (!gap || gap.kind !== "missing-trade") return null;
      return gap.time;
    };

    const setFullData = (runtime: IndicatorRuntime, view: ExpertIndicatorSeriesView) => {
      const gapsByIndex = new Map<number, Time>();
      for (const gap of candleSeriesGaps) {
        const localNextIndex = gap.nextIndex;
        if (localNextIndex <= 0 || localNextIndex >= view.visibleLength) continue;
        const time = gapTimeAt(view, gap.nextIndex);
        if (time !== null) gapsByIndex.set(localNextIndex, time);
      }
      if (runtime.kind === "rsi") {
        const values: Array<LineData<Time> | WhitespaceData<Time>> = [];
        for (let index = 0; index < view.visibleLength; index += 1) {
          const gapTime = gapsByIndex.get(index);
          if (gapTime !== undefined) values.push({ time: gapTime });
          const time = projectedTimeAt(view, index);
          if (time === null) continue;
          const value = view.rsi.value[view.offset + index];
          values.push(value === null || !Number.isFinite(value) ? { time } : { time, value });
        }
        runtime.value.setData(values);
        return;
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
      const time = projectedTimeAt(view, localIndex);
      if (time === null) return;
      const sourceIndex = view.offset + localIndex;
      if (runtime.kind === "rsi") {
        const value = view.rsi.value[sourceIndex];
        runtime.value.update(value === null || !Number.isFinite(value) ? { time } : { time, value });
        return;
      }
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
      if (incrementalIndex !== null && gapTimeAt(view, incrementalIndex) !== null) {
        // A separator is older than the Bar it precedes and cannot be inserted
        // with update(). Rebuild this bounded indicator page atomically instead.
        incrementalIndex = null;
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
    const chart = chartRef.current;
    if (!chart || !activeMainSeries()) return;
    for (const series of systemLineRuntimeRef.current.series) chart.removeSeries(series);
    const runtime: SystemLineRuntime = { series: [] };
    const projectPoints = (points: readonly { time: number; value: number }[]): LineData<Time>[] => {
      const byTime = new Map<number, LineData<Time>>();
      for (const point of points) {
        const chartTime = nearestChartTimeForActual(point.time);
        if (chartTime === null || !Number.isFinite(point.value)) continue;
        byTime.set(chartTime, { time: chartTime as Time, value: point.value });
      }
      return [...byTime.values()].sort((left, right) => Number(left.time) - Number(right.time));
    };
    for (const overlay of technicalOverlaySeries) {
      const values = projectPoints(overlay.points);
      if (values.length < 2) continue;
      const series = chart.addSeries(LineSeries, {
        title: overlay.label,
        color: overlay.color,
        lineWidth: overlay.lineWidth,
        lineStyle: chartLineStyle(overlay.lineStyle),
        priceLineVisible: false,
        lastValueVisible: overlay.lastValueVisible,
        crosshairMarkerVisible: false,
      });
      series.setData(values);
      runtime.series.push(series);
    }
    for (const trendLine of smartTrendLines) {
      const values = projectPoints([
        { time: trendLine.start.time, value: trendLine.start.price },
        { time: trendLine.anchor.time, value: trendLine.anchor.price },
        { time: trendLine.end.time, value: trendLine.end.price },
      ]);
      if (values.length < 2) continue;
      const appearance = trendLineAppearance(trendLine);
      const series = chart.addSeries(LineSeries, {
        title: trendLine.direction === "support" ? "智能支撑" : "智能压力",
        color: appearance.color,
        lineWidth: appearance.lineWidth,
        lineStyle: appearance.lineStyle,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData(values);
      runtime.series.push(series);
    }
    for (const pattern of pricePatterns) {
      const values = projectPoints([
        { time: pattern.first.time, value: pattern.first.price },
        ...(pattern.neckline
          ? [{ time: pattern.neckline.time, value: pattern.neckline.price }]
          : []),
        { time: pattern.second.time, value: pattern.second.price },
        { time: pattern.confirmation.time, value: pattern.confirmation.price },
      ]);
      if (values.length < 2) continue;
      const appearance = pricePatternAppearance(pattern);
      const series = chart.addSeries(LineSeries, {
        title: pattern.label,
        color: appearance.color,
        lineWidth: appearance.lineWidth,
        lineStyle: appearance.lineStyle,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData(values);
      runtime.series.push(series);
    }
    for (const event of marketStructureEvents) {
      const values = projectPoints([
        { time: event.reference.time, value: event.reference.price },
        { time: event.confirmation.time, value: event.confirmation.price },
      ]);
      if (values.length < 2) continue;
      const appearance = marketStructureAppearance(event);
      const series = chart.addSeries(LineSeries, {
        title: event.label,
        color: appearance.color,
        lineWidth: appearance.lineWidth,
        lineStyle: appearance.lineStyle,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      series.setData(values);
      runtime.series.push(series);
    }
    systemLineRuntimeRef.current = runtime;
    refreshExpertDecorations();
  }, [
    activeMainSeries,
    displayTimeZone,
    drawingDataRangeKey,
    nearestChartTimeForActual,
    period.id,
    refreshExpertDecorations,
    smartTrendLineSignature,
    structureMarkSignature,
    technicalOverlaySeries,
    technicalOverlaySignature,
  ]);

  useEffect(() => {
    refreshExpertDecorations();
  }, [
    eventMarkers,
    marketStructureEvents,
    openingGapVisible,
    pricePatterns,
    refreshExpertDecorations,
    sessionBands,
    smartTrendLines,
    timelineSessionGaps,
    valueZones,
  ]);

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
      data-timeline-available-days={period.mode === "timeline" ? timelineLayout.days.length : undefined}
      data-timeline-point-count={period.mode === "timeline" ? visibleTimelineCount : undefined}
    >
      <div className="chart-renderer" ref={rendererRef} />
      <div
        ref={expertOverlayLayerRef}
        className="expert-chart-overlays"
        aria-label="专家分析图层"
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
              <strong ref={livePriceValueRef}>{livePrice.toFixed(priceDigits)}</strong>
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
