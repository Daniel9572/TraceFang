import {
  Activity,
  Bot,
  BrainCircuit,
  CalendarClock,
  ChevronLeft,
  CirclePause,
  CirclePlay,
  Eye,
  EyeOff,
  Gauge,
  Layers3,
  Magnet,
  Minus,
  MousePointer2,
  Play,
  RotateCcw,
  Sparkles,
  Trash2,
  TrendingUp,
  Undo2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { marketApi } from "./api";
import {
  buildExpertAnalysisAt,
  buildExpertIndicatorSeriesAt,
  createExpertBacktestRunner,
  DEFAULT_EXPERT_STRATEGIES,
  EXPERT_INDICATOR_HISTORY_VERSION,
  EXPERT_STRATEGIES,
} from "./expertAnalysis";
import {
  EXPERT_GOLD_EVENTS_2026,
  IMPORTANT_EVENT_DISPLAY_STRATEGY,
  projectExpertEventStrategies,
} from "./expertEvents";
import { ChartLayerManager } from "./ChartLayerManager";
import {
  candleReplayCutoff,
  nextReplayIndex,
  replayClockAdvance,
  replayIndexAtOrBefore,
  timelineReplayCount,
} from "./expertReplay";
import {
  buildExpertOptionStrikeRows,
  expertOptionExpiryKey,
  optionPositioningLabel,
  resolveExpertOptionExpiry,
} from "./expertOptions";
import {
  activeDrawingLayer,
  addDrawingLayer,
  appendDrawingToActiveLayer,
  buildChartLayers,
  CHART_EVENT_LAYER_ID,
  CHART_GAP_LAYER_ID,
  CHART_SESSION_LAYER_ID,
  clearActiveDrawingLayer,
  deleteDrawingLayer,
  moveChartLayer,
  renameDrawingLayer,
  resizeIndicatorLayer,
  setActiveDrawingLayer,
  setChartLayerVisibility,
  type ChartLayerWorkspace,
  undoActiveDrawing,
} from "./chartLayers";
import {
  buildExpertSessionBandsForRange,
  CAPITAL_DOMINANCE_STRATEGY,
} from "./expertSessions";
import type {
  ExpertAiAnalysis,
  ExpertAiStatus,
  ExpertDrawingSnapMode,
  ExpertDrawingTool,
  ExpertIndicatorSeriesView,
  ExpertOptionContract,
  ExpertOptionsStatus,
  ExpertStrategyId,
} from "./expertTypes";
import { formatDateInTimeZone, formatDateTimeInTimeZone } from "./chartTimeAxis";
import type { ChartPeriodId } from "./chartPeriods";
import { chartPeriodById } from "./chartPeriods";
import { MarketChart } from "./MarketChart";
import { PeriodToolbar } from "./PeriodToolbar";
import type { Candle, HoverCandle, MarketPhase, MarketSchedule, TimelineSample } from "./types";

import "./expert-mode.css";

interface ExpertModeWorkspaceProps {
  code: string;
  instrumentName: string;
  unit: string;
  candles: Candle[];
  timelineSamples: TimelineSample[];
  periodId: ChartPeriodId;
  livePrice: number | null;
  change: number | null;
  changePercent: number | null;
  observedAt: string | null;
  referencePrice: number | null;
  timelineResolutionSeconds: number;
  priceDigits: number;
  marketPhase: MarketPhase;
  marketSchedule: MarketSchedule | null | undefined;
  sourceLabel: string;
  sourceState: "connecting" | "live" | "waiting" | "unavailable";
  liveIndicatorSeries: ExpertIndicatorSeriesView;
  layerWorkspace: ChartLayerWorkspace;
  onLayerWorkspaceChange: (
    update: (current: ChartLayerWorkspace) => ChartLayerWorkspace,
  ) => void;
  historyLoading: boolean;
  loading: boolean;
  error: string | null;
  onPeriodChange: (period: ChartPeriodId) => void;
  onRequestOlderHistory: () => void;
  onExit: () => void;
}

const TIME_ZONES = [
  { id: "Asia/Shanghai", label: "北京" },
  { id: "Europe/London", label: "伦敦" },
  { id: "America/New_York", label: "纽约" },
] as const;

const STRATEGY_STORAGE_KEY = "market-expert-strategies-v1";
const CAPITAL_DOMINANCE_STORAGE_KEY = "market-expert-capital-dominance-v1";
const OPENING_GAP_STRATEGY = {
  shortName: "跳空标注",
  description: "只在边界行情完整时标记休市后的首个真实价格点",
} as const;
const SECONDS_PER_DAY = 24 * 60 * 60;
const OPTION_QUANTITY_FORMATTER = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

function readStrategies(): ExpertStrategyId[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STRATEGY_STORAGE_KEY) ?? "null");
    if (!Array.isArray(parsed)) return DEFAULT_EXPERT_STRATEGIES;
    const known = new Set(EXPERT_STRATEGIES.map((strategy) => strategy.id));
    const values = parsed.filter((value): value is ExpertStrategyId => known.has(value));
    if (parsed.length === 0) return [];
    return values.length > 0 ? values : DEFAULT_EXPERT_STRATEGIES;
  } catch {
    return DEFAULT_EXPERT_STRATEGIES;
  }
}

function readCapitalDominanceStrategy(): boolean {
  try {
    return JSON.parse(window.localStorage.getItem(CAPITAL_DOMINANCE_STORAGE_KEY) ?? "false") === true;
  } catch {
    return false;
  }
}

function formatSigned(value: number | null, digits = 2, suffix = ""): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}${suffix}`;
}

function formatPrice(value: number | null, digits: number): string {
  return value === null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
}

function signalDirectionLabel(direction: "bullish" | "bearish" | "neutral"): string {
  if (direction === "bullish") return "偏多";
  if (direction === "bearish") return "偏空";
  return "中性";
}

function regimeLabel(regime: string): string {
  if (regime === "trend-up") return "趋势上行";
  if (regime === "trend-down") return "趋势下行";
  if (regime === "balanced") return "均衡震荡";
  return "等待样本";
}

function optionMarketStateLabel(state: string): string {
  if (state === "provider_required") return "待配置行情源";
  if (state === "provider_and_entitlement_required") return "待配置行情源与授权";
  if (state === "live") return "实时";
  if (state === "delayed") return "延时";
  if (state === "unavailable") return "暂不可用";
  if (state === "unconfigured") return "未配置";
  return state;
}

function formatOptionMetric(value: number | null, digits = 2): string {
  return value === null || !Number.isFinite(value) ? "—" : value.toFixed(digits);
}

function formatOptionQuantity(value: number): string {
  return Number.isFinite(value) ? OPTION_QUANTITY_FORMATTER.format(value) : "—";
}

function formatOptionObservedAt(value: string | null, timeZone: string): string {
  if (value === null) return "—";
  const seconds = Date.parse(value) / 1_000;
  return Number.isFinite(seconds) ? formatDateTimeInTimeZone(seconds, timeZone) : "—";
}

function optionPriceUnitLabel(value: string | null | undefined): string {
  if (value === "CNY_PER_GRAM") return "人民币/克";
  if (value === "USD_PER_TROY_OUNCE") return "美元/盎司";
  return value ?? "—";
}

function ExpertOptionSide({ contract }: { contract: ExpertOptionContract | null }) {
  if (contract === null) return <span className="expert-option-missing">—</span>;
  return (
    <span className="expert-option-contract" title={`${contract.contract_id} · Bid ${formatOptionMetric(contract.bid)} / Ask ${formatOptionMetric(contract.ask)}`}>
      <strong>{formatOptionMetric(contract.last)}</strong>
      <small>OI {formatOptionQuantity(contract.open_interest)} · V {formatOptionQuantity(contract.volume)}</small>
    </span>
  );
}

export function ExpertModeWorkspace({
  code,
  instrumentName,
  unit,
  candles,
  timelineSamples,
  periodId,
  livePrice,
  change,
  changePercent,
  observedAt,
  referencePrice,
  timelineResolutionSeconds,
  priceDigits,
  marketPhase,
  marketSchedule,
  sourceLabel,
  sourceState,
  liveIndicatorSeries,
  layerWorkspace,
  onLayerWorkspaceChange,
  historyLoading,
  loading,
  error,
  onPeriodChange,
  onRequestOlderHistory,
  onExit,
}: ExpertModeWorkspaceProps) {
  const period = chartPeriodById(periodId);
  const [displayTimeZone, setDisplayTimeZone] = useState("Asia/Shanghai");
  const [enabledStrategies, setEnabledStrategies] = useState<ExpertStrategyId[]>(readStrategies);
  const [capitalDominanceEnabled, setCapitalDominanceEnabled] = useState(readCapitalDominanceStrategy);
  const setLayerWorkspace = onLayerWorkspaceChange;
  const sessionLayerNormalizedRef = useRef(false);
  const [layerManagerOpen, setLayerManagerOpen] = useState(true);
  const [drawingTool, setDrawingTool] = useState<ExpertDrawingTool | null>(null);
  const [drawingSnapMode, setDrawingSnapMode] = useState<ExpertDrawingSnapMode>("weak");
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [replayEnabled, setReplayEnabled] = useState(false);
  const [replayCutoff, setReplayCutoff] = useState<number | null>(null);
  const [replayPlaying, setReplayPlaying] = useState(false);
  const [replaySpeed, setReplaySpeed] = useState(2);
  const [replayDraftIndex, setReplayDraftIndex] = useState<number | null>(null);
  const [backtestRevision, setBacktestRevision] = useState(0);
  const [intelligenceTab, setIntelligenceTab] = useState<"signals" | "options" | "ai">("signals");
  const [optionsStatus, setOptionsStatus] = useState<ExpertOptionsStatus | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [selectedOptionExpiryKey, setSelectedOptionExpiryKey] = useState<string | null>(null);
  const optionChainScrollRef = useRef<HTMLDivElement | null>(null);
  const [aiStatus, setAiStatus] = useState<ExpertAiStatus | null>(null);
  const [aiAnalysis, setAiAnalysis] = useState<ExpertAiAnalysis | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const currentDrawingLayer = activeDrawingLayer(layerWorkspace);
  const importantEventsEnabled = layerWorkspace.layers.some((layer) => (
    layer.kind === "annotation"
    && layer.annotationId === "events"
    && layer.visible
  ));
  const openingGapEnabled = layerWorkspace.layers.some((layer) => (
    layer.kind === "annotation"
    && layer.annotationId === "gaps"
    && layer.visible
  ));

  useEffect(() => {
    try {
      window.localStorage.setItem(STRATEGY_STORAGE_KEY, JSON.stringify(enabledStrategies));
    } catch {
      // Strategy selection remains usable when local persistence is unavailable.
    }
  }, [enabledStrategies]);

  useEffect(() => {
    try {
      window.localStorage.setItem(
        CAPITAL_DOMINANCE_STORAGE_KEY,
        JSON.stringify(capitalDominanceEnabled),
      );
    } catch {
      // Visual strategy selection remains usable when local persistence is unavailable.
    }
  }, [capitalDominanceEnabled]);

  useEffect(() => {
    if (sessionLayerNormalizedRef.current) return;
    sessionLayerNormalizedRef.current = true;
    if (!capitalDominanceEnabled) {
      setLayerWorkspace((current) => setChartLayerVisibility(
        current,
        CHART_SESSION_LAYER_ID,
        false,
      ));
    }
  }, [capitalDominanceEnabled, setLayerWorkspace]);

  useEffect(() => {
    let disposed = false;
    void marketApi.expertAiStatus()
      .then((value) => { if (!disposed) setAiStatus(value); })
      .catch((requestError) => {
        if (!disposed) setAiStatus({
          state: "unavailable",
          available: false,
          authenticated: null,
          auth_mode: null,
          provider: "local_codex_chatgpt",
          detail: requestError instanceof Error ? requestError.message : String(requestError),
          checked_at: new Date().toISOString(),
        });
      });
    return () => { disposed = true; };
  }, []);

  useEffect(() => {
    if (intelligenceTab !== "options") return;
    let disposed = false;
    let timer: number | null = null;
    const schedule = (seconds: number) => {
      if (disposed) return;
      timer = window.setTimeout(() => void load(), Math.max(5, seconds) * 1_000);
    };
    const load = async () => {
      if (document.visibilityState === "hidden") {
        schedule(optionsStatus?.refresh_after_seconds ?? 15);
        return;
      }
      try {
        const value = await marketApi.expertGoldOptions();
        if (disposed) return;
        setOptionsStatus(value);
        setOptionsError(null);
        schedule(value.refresh_after_seconds);
      } catch (requestError) {
        if (disposed) return;
        setOptionsError(requestError instanceof Error ? requestError.message : String(requestError));
        schedule(15);
      }
    };
    const handleVisibility = () => {
      if (document.visibilityState !== "visible") return;
      if (timer !== null) window.clearTimeout(timer);
      void load();
    };
    void load();
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  }, [intelligenceTab]);

  useEffect(() => {
    if (!replayEnabled || !replayPlaying || candles.length === 0) return;
    const stepMilliseconds = 1_000 / Math.max(1, replaySpeed);
    let nextStepAt = window.performance.now() + stepMilliseconds;
    const timer = window.setInterval(() => {
      const now = window.performance.now();
      const advance = replayClockAdvance(now, nextStepAt, stepMilliseconds);
      if (advance.steps === 0) return;
      nextStepAt = advance.nextStepAt;
      setReplayCutoff((current) => {
        const currentIndex = replayIndexAtOrBefore(candles, current);
        const replayableLastIndex = replayIndexAtOrBefore(candles, null);
        const next = nextReplayIndex(currentIndex, replayableLastIndex + 1, advance.steps);
        if (next >= replayableLastIndex) setReplayPlaying(false);
        return candleReplayCutoff(candles, next) ?? current;
      });
    }, Math.min(100, stepMilliseconds));
    return () => window.clearInterval(timer);
  }, [candles.length, replayEnabled, replayPlaying, replaySpeed]);

  const replayIndex = useMemo(
    () => replayIndexAtOrBefore(candles, replayCutoff),
    [candles, replayCutoff],
  );
  const replayableLastIndex = useMemo(
    () => replayIndexAtOrBefore(candles, null),
    [candles],
  );

  const replayBoundary = replayEnabled
    ? candleReplayCutoff(candles, replayIndex)
    : replayableLastIndex >= 0 ? candleReplayCutoff(candles, replayableLastIndex) : null;
  useEffect(() => {
    setHover(null);
  }, [replayBoundary, replayEnabled]);
  const analysisIndex = replayEnabled ? replayIndex : candles.length - 1;
  const indicatorHistoryKey = `${EXPERT_INDICATOR_HISTORY_VERSION}:${code}:${candles.at(-1)?.source.provider ?? "pending"}:${period.id}`;
  const analysis = useMemo(
    () => buildExpertAnalysisAt(candles, enabledStrategies, analysisIndex, indicatorHistoryKey),
    [analysisIndex, candles, enabledStrategies, indicatorHistoryKey],
  );
  const indicatorSeries = useMemo(
    () => replayEnabled
      ? buildExpertIndicatorSeriesAt(candles, analysisIndex, indicatorHistoryKey)
      : liveIndicatorSeries,
    [analysisIndex, candles, indicatorHistoryKey, liveIndicatorSeries, replayEnabled],
  );
  const enabledStrategyKey = [...enabledStrategies].sort().join(":");
  const backtestLastIndex = replayEnabled
    ? replayIndex
    : candles.at(-1)?.state === "final" ? candles.length - 1 : candles.length - 2;
  const backtestRunner = useMemo(
    () => createExpertBacktestRunner(candles, enabledStrategies, indicatorHistoryKey),
    // Strategy identity, rather than array order, owns the causal replay index.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [candles, enabledStrategyKey, indicatorHistoryKey],
  );
  useEffect(() => {
    if (historyLoading || replayPlaying) return;
    let disposed = false;
    let lastPublished = window.performance.now();
    const channel = new MessageChannel();
    setBacktestRevision((current) => current + 1);
    const advance = () => {
      if (disposed || backtestRunner.done) return;
      const frameStart = window.performance.now();
      do {
        if (backtestRunner.advance(64) === 0) break;
      } while (!backtestRunner.done && window.performance.now() - frameStart < 8);
      const now = window.performance.now();
      if (backtestRunner.done || now - lastPublished >= 120) {
        lastPublished = now;
        setBacktestRevision((current) => current + 1);
      }
      if (!backtestRunner.done) channel.port2.postMessage(null);
    };
    channel.port1.onmessage = advance;
    channel.port2.postMessage(null);
    return () => {
      disposed = true;
      channel.port1.close();
      channel.port2.close();
    };
  }, [backtestRunner, historyLoading, replayPlaying]);
  const backtest = useMemo(
    () => backtestRunner.resultAt(backtestLastIndex),
    [backtestLastIndex, backtestRevision, backtestRunner],
  );
  const backtestReady = backtestLastIndex < 0
    || backtestRunner.done
    || backtestRunner.completedIndex >= backtestLastIndex;
  const backtestProgress = backtestReady
    ? 100
    : Math.min(99, Math.max(0, Math.floor(
      (backtestRunner.completedIndex + 1) / Math.max(1, backtestLastIndex + 1) * 100,
    )));
  const visibleTimelineCount = replayEnabled
    ? timelineReplayCount(timelineSamples, replayBoundary)
    : timelineSamples.length;
  const firstCandleTime = candles[0]
    ? Date.parse(candles[0].open_time) / 1_000
    : null;
  const firstTimelineSample = visibleTimelineCount > 0 ? timelineSamples[0] : null;
  const firstTimelineTime = firstTimelineSample?.observedTime
    ?? firstTimelineSample?.time
    ?? null;
  const firstSessionTime = period.mode === "timeline"
    ? [firstCandleTime, firstTimelineTime]
      .filter((value): value is number => value !== null && Number.isFinite(value))
      .reduce<number | null>((minimum, value) => minimum === null ? value : Math.min(minimum, value), null)
    : firstCandleTime;
  const lastTimelineSample = visibleTimelineCount > 0
    ? timelineSamples[visibleTimelineCount - 1]
    : null;
  const lastTimelineTime = lastTimelineSample?.observedTime ?? lastTimelineSample?.time ?? null;
  const lastSessionTime = period.mode === "timeline"
    ? [replayBoundary, lastTimelineTime]
      .filter((value): value is number => value !== null && Number.isFinite(value))
      .reduce<number | null>((maximum, value) => maximum === null ? value : Math.max(maximum, value), null)
    : replayBoundary;
  const sessionStartDay = firstSessionTime === null
    ? null
    : Math.floor(firstSessionTime / SECONDS_PER_DAY) * SECONDS_PER_DAY;
  const sessionEndDay = lastSessionTime === null
    ? null
    : (Math.floor(lastSessionTime / SECONDS_PER_DAY) + 1) * SECONDS_PER_DAY;
  const eventStrategyProjection = useMemo(
    () => projectExpertEventStrategies(
      importantEventsEnabled,
      replayEnabled ? replayBoundary : null,
    ),
    [importantEventsEnabled, replayBoundary, replayEnabled],
  );
  const capitalDriverEvents = eventStrategyProjection.capitalDrivers;
  const sessionBands = useMemo(
    () => !capitalDominanceEnabled || sessionStartDay === null || sessionEndDay === null
      ? []
      : buildExpertSessionBandsForRange(
        sessionStartDay,
        sessionEndDay,
        marketSchedule,
        undefined,
        capitalDriverEvents,
      ),
    [capitalDominanceEnabled, capitalDriverEvents, marketSchedule, sessionEndDay, sessionStartDay],
  );

  const replayLast = replayEnabled ? candles[replayIndex] ?? null : candles.at(-1) ?? null;
  const chartLivePrice = replayEnabled
    ? replayLast ? Number(replayLast.close) : null
    : livePrice;
  const chartObservedAt = replayEnabled && replayBoundary !== null
    ? new Date(replayBoundary * 1_000).toISOString()
    : observedAt;
  const displayedBar = hover ?? (replayLast ? {
    time: Date.parse(replayLast.open_time) / 1_000,
    open: Number(replayLast.open),
    high: Number(replayLast.high),
    low: Number(replayLast.low),
    close: Number(replayLast.close),
  } : null);
  const priceTone = (replayEnabled
    ? Boolean(chartLivePrice !== null && replayLast && Number(replayLast.close) >= Number(replayLast.open))
    : (change ?? 0) >= 0)
    ? "is-up"
    : "is-down";
  const selectedOptionExpiry = useMemo(
    () => resolveExpertOptionExpiry(optionsStatus?.expiries ?? [], selectedOptionExpiryKey),
    [optionsStatus?.expiries, selectedOptionExpiryKey],
  );
  const optionStrikeRows = useMemo(
    () => buildExpertOptionStrikeRows(optionsStatus?.contracts ?? [], selectedOptionExpiry),
    [optionsStatus?.contracts, selectedOptionExpiry],
  );
  const selectedOptionExpiryValue = selectedOptionExpiry === null
    ? ""
    : expertOptionExpiryKey(selectedOptionExpiry);
  useEffect(() => {
    const scroll = optionChainScrollRef.current;
    const atmRow = scroll?.querySelector<HTMLElement>("tbody tr.is-atm");
    if (!scroll || !atmRow) return;
    scroll.scrollTop = Math.max(
      0,
      atmRow.offsetTop - (scroll.clientHeight - atmRow.clientHeight) / 2,
    );
  }, [selectedOptionExpiryValue]);
  const aiReady = aiStatus?.state === "ready" && aiStatus.authenticated === true;

  const toggleStrategy = (strategyId: ExpertStrategyId) => {
    setEnabledStrategies((current) => current.includes(strategyId)
      ? current.filter((value) => value !== strategyId)
      : [...current, strategyId]);
  };

  const toggleCapitalDominanceStrategy = () => {
    const next = !capitalDominanceEnabled;
    setCapitalDominanceEnabled(next);
    setLayerWorkspace((current) => setChartLayerVisibility(
      current,
      CHART_SESSION_LAYER_ID,
      next,
    ));
  };

  const toggleImportantEventStrategy = () => {
    setLayerWorkspace((current) => setChartLayerVisibility(
      current,
      CHART_EVENT_LAYER_ID,
      !importantEventsEnabled,
    ));
  };

  const toggleOpeningGapStrategy = () => {
    setLayerWorkspace((current) => setChartLayerVisibility(
      current,
      CHART_GAP_LAYER_ID,
      !openingGapEnabled,
    ));
  };

  const requestAiAnalysis = useCallback(async () => {
    setAiBusy(true);
    setAiError(null);
    try {
      const result = await marketApi.expertAiAnalyze({
        code,
        period: period.mode === "timeline" ? "1m" : period.id,
        enabled_strategies: enabledStrategies,
      });
      setAiAnalysis(result);
      if (result.state !== "completed" || !result.analysis) setAiError(result.detail);
    } catch (requestError) {
      setAiError(requestError instanceof Error ? requestError.message : String(requestError));
    } finally {
      setAiBusy(false);
    }
  }, [code, enabledStrategies, period.id, period.mode]);

  const commitReplayPosition = useCallback((requestedIndex: number) => {
    const nextIndex = Math.min(
      Math.max(0, replayableLastIndex),
      Math.max(0, Math.floor(requestedIndex)),
    );
    const nextCutoff = candleReplayCutoff(candles, nextIndex);
    if (nextCutoff !== null) setReplayCutoff(nextCutoff);
    setReplayDraftIndex(null);
    setReplayPlaying(false);
  }, [candles, replayableLastIndex]);

  const displayedReplayIndex = replayDraftIndex ?? replayIndex;
  const displayedReplayCutoff = replayEnabled && replayDraftIndex !== null
    ? candleReplayCutoff(candles, replayDraftIndex)
    : replayBoundary;

  const eventReferenceTime = replayEnabled ? replayBoundary : Date.now() / 1_000;
  const latestEvent = !importantEventsEnabled || eventReferenceTime === null
    ? undefined
    : replayEnabled
      ? [...EXPERT_GOLD_EVENTS_2026].reverse().find((event) => event.time <= eventReferenceTime)
      : EXPERT_GOLD_EVENTS_2026.find((event) => event.time >= eventReferenceTime);
  const visibleEvents = eventStrategyProjection.displayMarkers;
  const chartLayers = useMemo(
    () => buildChartLayers(layerWorkspace, {
      indicatorSeries,
      sessionBands,
      eventMarkers: visibleEvents,
      priceLevels: analysis.levels,
      valueZones: analysis.valueZones,
    }),
    [analysis.levels, analysis.valueZones, indicatorSeries, layerWorkspace, sessionBands, visibleEvents],
  );

  return (
    <div className="expert-workspace" data-replay={replayEnabled ? "active" : "live"}>
      <header className="expert-command-deck">
        <button
          type="button"
          className="expert-exit"
          onClick={onExit}
          title="返回普通行情"
          aria-label="返回普通行情"
        >
          <ChevronLeft size={17} />
        </button>
        <div className="expert-brand">
          <span className="expert-brand-mark"><Sparkles size={18} /></span>
          <div><small>GOLD DESK · EXPERIMENTAL</small><strong>专家模式</strong></div>
        </div>
        <div className="expert-symbol-block">
          <span>{instrumentName}</span>
          <strong>{code}</strong>
          <em>{unit}</em>
        </div>
        <div className={`expert-live-quote ${priceTone}`}>
          <strong>{formatPrice(chartLivePrice, priceDigits)}</strong>
          <span>{replayEnabled ? "回放价格" : `${formatSigned(change, priceDigits)} · ${formatSigned(changePercent, 2, "%")}`}</span>
        </div>
        <div className="expert-periods chart-toolbar">
          <PeriodToolbar
            selectedId={periodId}
            onSelect={(next) => {
              setHover(null);
              onPeriodChange(next);
            }}
          />
        </div>
        <div className="expert-time-zone" role="group" aria-label="图表显示时区">
          {TIME_ZONES.map((zone) => (
            <button
              type="button"
              key={zone.id}
              className={displayTimeZone === zone.id ? "is-active" : ""}
              aria-pressed={displayTimeZone === zone.id}
              onClick={() => setDisplayTimeZone(zone.id)}
            >
              {zone.label}
            </button>
          ))}
        </div>
        <div className="expert-feed-state">
          <span className={`expert-feed-dot is-${sourceState}`} />
          <div><strong>{replayEnabled ? "历史回放" : marketPhase === "closed" ? "休市" : "实时"}</strong><small>{historyLoading ? `完整历史同步中 · ${sourceLabel}` : sourceLabel}</small></div>
        </div>
      </header>

      <aside className="expert-tool-rail" aria-label="图表工具">
        <button type="button" className={drawingTool === null ? "is-active" : ""} onClick={() => setDrawingTool(null)} title="浏览图表">
          <MousePointer2 size={18} /><span>浏览</span>
        </button>
        <button
          type="button"
          className={layerManagerOpen ? "is-active" : ""}
          aria-pressed={layerManagerOpen}
          onClick={() => setLayerManagerOpen((current) => !current)}
          title={layerManagerOpen ? "关闭图层管理" : "打开图层管理"}
        >
          <Layers3 size={18} /><span>图层</span>
        </button>
        <button
          type="button"
          className={drawingTool === "trend" ? "is-active" : ""}
          onClick={() => {
            setLayerWorkspace((current) => setChartLayerVisibility(current, current.activeDrawingLayerId, true));
            setDrawingTool("trend");
          }}
          title="趋势线"
        >
          <TrendingUp size={18} /><span>趋势</span>
        </button>
        <button
          type="button"
          className={drawingTool === "horizontal" ? "is-active" : ""}
          onClick={() => {
            setLayerWorkspace((current) => setChartLayerVisibility(current, current.activeDrawingLayerId, true));
            setDrawingTool("horizontal");
          }}
          title="水平线"
        >
          <Minus size={18} /><span>水平</span>
        </button>
        <button
          type="button"
          className={drawingSnapMode === "weak" ? "is-active" : ""}
          aria-pressed={drawingSnapMode === "weak"}
          onClick={() => setDrawingSnapMode((current) => current === "weak" ? "off" : "weak")}
          title={drawingSnapMode === "weak" ? "弱磁吸已开启：靠近行情锚点时吸附" : "开启弱磁吸"}
        >
          <Magnet size={17} /><span>弱吸</span>
        </button>
        <button
          type="button"
          className={!currentDrawingLayer.visible ? "is-active" : ""}
          aria-pressed={!currentDrawingLayer.visible}
          onClick={() => {
            setDrawingTool(null);
            setLayerWorkspace((current) => {
              const active = activeDrawingLayer(current);
              return setChartLayerVisibility(current, active.id, !active.visible);
            });
          }}
          title={`${currentDrawingLayer.visible ? "隐藏" : "显示"}${currentDrawingLayer.name}`}
        >
          {currentDrawingLayer.visible ? <EyeOff size={17} /> : <Eye size={17} />}
          <span>层显隐</span>
        </button>
        <div className="expert-tool-separator" />
        <button type="button" disabled={currentDrawingLayer.drawings.length === 0} onClick={() => setLayerWorkspace(undoActiveDrawing)} title={`撤销${currentDrawingLayer.name}最后一条线`}>
          <Undo2 size={17} /><span>撤销</span>
        </button>
        <button type="button" disabled={currentDrawingLayer.drawings.length === 0} onClick={() => setLayerWorkspace(clearActiveDrawingLayer)} title={`清空${currentDrawingLayer.name}`}>
          <Trash2 size={17} /><span>清空</span>
        </button>
      </aside>

      <main className="expert-chart-stage">
        <div className="expert-chart-readout">
          <div>
            <span>{displayedBar ? formatDateTimeInTimeZone(displayedBar.time, displayTimeZone) : "等待行情"}</span>
            {displayedBar ? (
              <small>
                O {displayedBar.open.toFixed(priceDigits)} · H {displayedBar.high.toFixed(priceDigits)} · L {displayedBar.low.toFixed(priceDigits)} · C {displayedBar.close.toFixed(priceDigits)}
              </small>
            ) : null}
          </div>
          <div className={`expert-regime is-${analysis.regime}`}>
            <Activity size={14} />
            <strong>{regimeLabel(analysis.regime)}</strong>
            <span>{formatSigned(analysis.compositeScore * 100, 0)}</span>
          </div>
        </div>
        {layerManagerOpen ? (
          <ChartLayerManager
            workspace={layerWorkspace}
            onClose={() => setLayerManagerOpen(false)}
            onAddDrawingLayer={() => {
              setLayerWorkspace((current) => addDrawingLayer(
                current,
                `layer:drawing:${Date.now()}`,
              ));
            }}
            onSelectDrawingLayer={(layerId) => setLayerWorkspace((current) => setActiveDrawingLayer(current, layerId))}
            onToggleLayer={(layerId, visible) => {
              if (layerId === layerWorkspace.activeDrawingLayerId && !visible) setDrawingTool(null);
              setLayerWorkspace((current) => setChartLayerVisibility(current, layerId, visible));
            }}
            onRenameDrawingLayer={(layerId, name) => setLayerWorkspace((current) => renameDrawingLayer(current, layerId, name))}
            onDeleteDrawingLayer={(layerId) => {
              const layer = layerWorkspace.layers.find((candidate) => candidate.id === layerId);
              if (layer?.kind === "drawing" && layer.drawings.length > 0) {
                const confirmed = window.confirm(`删除“${layer.name}”及其中 ${layer.drawings.length} 条画线？`);
                if (!confirmed) return;
              }
              if (layerId === layerWorkspace.activeDrawingLayerId) setDrawingTool(null);
              setLayerWorkspace((current) => deleteDrawingLayer(current, layerId));
            }}
            onMoveLayer={(layerId, targetLayerId) => setLayerWorkspace((current) => moveChartLayer(current, layerId, targetLayerId))}
            onResizeIndicatorLayer={(layerId, height) => setLayerWorkspace((current) => resizeIndicatorLayer(current, layerId, height))}
          />
        ) : null}
        <MarketChart
          candles={candles}
          period={period}
          timelineSamples={timelineSamples}
          livePrice={chartLivePrice}
          observedAt={chartObservedAt}
          referencePrice={replayEnabled ? null : referencePrice}
          timelineResolutionSeconds={timelineResolutionSeconds}
          priceDigits={priceDigits}
          marketPhase={marketPhase}
          replayMode={replayEnabled}
          replayIndex={replayEnabled ? replayIndex : null}
          replayCutoff={replayEnabled ? replayBoundary : null}
          marketSchedule={marketSchedule}
          historyLoading={historyLoading}
          onRequestOlderHistory={onRequestOlderHistory}
          onHover={setHover}
          appearance="expert"
          displayTimeZone={displayTimeZone}
          layers={chartLayers}
          drawingTool={drawingTool}
          drawingSnapMode={drawingSnapMode}
          onDrawingCommit={(drawing) => {
            setLayerWorkspace((current) => appendDrawingToActiveLayer(current, drawing));
            setDrawingTool(null);
          }}
          onIndicatorPaneResize={(layerId, height) => {
            setLayerWorkspace((current) => resizeIndicatorLayer(current, layerId, height));
          }}
        />
        {loading && candles.length === 0 ? <div className="expert-chart-message"><RotateCcw className="spin" size={18} />正在读取现货黄金</div> : null}
        {error ? <div className="expert-chart-message is-error">{error}</div> : null}
        {drawingTool ? (
          <div className="expert-drawing-hint">
            {currentDrawingLayer.name} · {drawingTool === "trend" ? "在图上拖动两个锚点" : "点击目标价格位置"}
            {drawingSnapMode === "weak" ? " · 靠近 O/H/L/C 自动弱吸附" : ""} · Esc 取消
          </div>
        ) : null}
      </main>

      <aside className="expert-intelligence" aria-label="策略与智能分析">
        <section className="expert-strategy-stack">
          <header><div><Layers3 size={15} /><strong>策略层</strong></div><span>{enabledStrategies.length + Number(capitalDominanceEnabled) + Number(importantEventsEnabled) + Number(openingGapEnabled)}/{EXPERT_STRATEGIES.length + 3}</span></header>
          <div className="expert-strategy-list">
            <button
              type="button"
              className={capitalDominanceEnabled ? "is-enabled" : ""}
              aria-pressed={capitalDominanceEnabled}
              title="视觉策略：只标注资金主导；始终读取事件事实判断 08:30 数据接管，不受数据/事件图层是否显示影响"
              onClick={toggleCapitalDominanceStrategy}
            >
              <span className="strategy-quality is-calendar" />
              <div>
                <strong>{CAPITAL_DOMINANCE_STRATEGY.shortName}</strong>
                <small>{CAPITAL_DOMINANCE_STRATEGY.description}</small>
              </div>
              <span className="strategy-provenance">
                <small>规则 · 时区 + 事件</small>
                <em>视觉策略</em>
              </span>
            </button>
            <button
              type="button"
              className={importantEventsEnabled ? "is-enabled" : ""}
              aria-pressed={importantEventsEnabled}
              title="视觉策略：控制图上事件节点和侧栏事件提示；关闭只隐藏显示，不改变资金主导的接管时间判断"
              onClick={toggleImportantEventStrategy}
            >
              <span className="strategy-quality is-event" />
              <div>
                <strong>{IMPORTANT_EVENT_DISPLAY_STRATEGY.shortName}</strong>
                <small>{IMPORTANT_EVENT_DISPLAY_STRATEGY.description}</small>
              </div>
              <span className="strategy-provenance">
                <small>数据 · {IMPORTANT_EVENT_DISPLAY_STRATEGY.dataSource}</small>
                <em>{capitalDominanceEnabled && importantEventsEnabled ? "联动中" : "视觉策略"}</em>
              </span>
            </button>
            <button
              type="button"
              className={openingGapEnabled ? "is-enabled" : ""}
              aria-pressed={openingGapEnabled}
              title="视觉策略：不显示休市区间或卡片；仅在收开盘边界完整且存在真实价差时标记复市首点"
              onClick={toggleOpeningGapStrategy}
            >
              <span className="strategy-quality is-gap" />
              <div>
                <strong>{OPENING_GAP_STRATEGY.shortName}</strong>
                <small>{OPENING_GAP_STRATEGY.description}</small>
              </div>
              <span className="strategy-provenance">
                <small>原生 · 收开盘边界</small>
                <em>视觉策略</em>
              </span>
            </button>
            {EXPERT_STRATEGIES.map((strategy) => {
              const enabled = enabledStrategies.includes(strategy.id);
              const evidenceLabel = strategy.evidenceMode === "native"
                ? "原生K线"
                : strategy.evidenceMode === "proxy"
                  ? "估算"
                  : "需成交量";
              return (
                <button
                  type="button"
                  key={strategy.id}
                  className={enabled ? "is-enabled" : ""}
                  aria-pressed={enabled}
                  title={`数据：${strategy.dataSource}；口径：${evidenceLabel}`}
                  onClick={() => toggleStrategy(strategy.id)}
                >
                  <span className={`strategy-quality is-${strategy.evidenceMode}`} />
                  <div><strong>{strategy.shortName}</strong><small>{strategy.description}</small></div>
                  <span className="strategy-provenance">
                    <small>数据 · {strategy.dataSource}</small>
                    <em>{evidenceLabel}</em>
                  </span>
                </button>
              );
            })}
          </div>
        </section>

        <div className="expert-intelligence-tabs" role="tablist" aria-label="智能分析类别">
          <button
            type="button"
            id="expert-intelligence-tab-signals"
            role="tab"
            aria-selected={intelligenceTab === "signals"}
            aria-controls="expert-intelligence-panel"
            className={intelligenceTab === "signals" ? "is-active" : ""}
            onClick={() => setIntelligenceTab("signals")}
          ><Gauge size={14} />信号</button>
          <button
            type="button"
            id="expert-intelligence-tab-options"
            role="tab"
            aria-selected={intelligenceTab === "options"}
            aria-controls="expert-intelligence-panel"
            className={intelligenceTab === "options" ? "is-active" : ""}
            onClick={() => setIntelligenceTab("options")}
          ><Layers3 size={14} />期权</button>
          <button
            type="button"
            id="expert-intelligence-tab-ai"
            role="tab"
            aria-selected={intelligenceTab === "ai"}
            aria-controls="expert-intelligence-panel"
            className={intelligenceTab === "ai" ? "is-active" : ""}
            onClick={() => setIntelligenceTab("ai")}
          ><BrainCircuit size={14} />AI</button>
        </div>

        <div
          className="expert-intelligence-body"
          id="expert-intelligence-panel"
          role="tabpanel"
          aria-labelledby={`expert-intelligence-tab-${intelligenceTab}`}
        >
          {intelligenceTab === "signals" ? (
            <>
              <div className="expert-signal-summary">
                <span>MACD <b>{analysis.indicators.macd?.histogram.toFixed(2) ?? "—"}</b></span>
                <span>KDJ <b>{analysis.indicators.kdj?.j.toFixed(1) ?? "—"}</b></span>
                <span>POC≈ <b>{analysis.indicators.pocPrice?.toFixed(priceDigits) ?? "—"}</b></span>
              </div>
              <div className="expert-signal-feed">
                {analysis.signals.slice(0, 5).map((signal) => (
                  <article key={signal.id} className={`is-${signal.direction}`}>
                    <header><strong>{signal.title}</strong><span>{signalDirectionLabel(signal.direction)} · {Math.round(signal.confidence * 100)}</span></header>
                    <p>{signal.detail}</p>
                    <small>{signal.evidence.join(" · ")}</small>
                  </article>
                ))}
                {analysis.signals.length === 0 ? <div className="expert-empty">等待足够行情样本</div> : null}
              </div>
              {latestEvent ? (
                <div className="expert-next-event">
                  <CalendarClock size={15} />
                  <div><strong>{latestEvent.title}</strong><span>{latestEvent.timePrecision === "date"
                    ? formatDateInTimeZone(latestEvent.time, displayTimeZone)
                    : formatDateTimeInTimeZone(latestEvent.time, displayTimeZone)} · {latestEvent.source}</span></div>
                </div>
              ) : null}
            </>
          ) : null}

          {intelligenceTab === "options" ? (
            <div className="expert-options-panel">
              <div className={`expert-capability-state is-${optionsStatus?.state ?? "unavailable"}`}>
                <div>
                  <span className="expert-option-status-dot" />
                  <strong>{optionsStatus?.state === "live"
                    ? "期权实时链已连接"
                    : optionsStatus?.state === "delayed"
                      ? "上期所官方延时链已连接"
                      : optionsStatus === null && optionsError === null
                        ? "正在读取官方期权链"
                        : "期权行情暂不可用"}</strong>
                  <em>{optionsStatus?.delivery_mode === "exchange_delayed" ? "EXCHANGE DELAYED" : optionMarketStateLabel(optionsStatus?.state ?? "unavailable")}</em>
                </div>
                <span>{optionsError ?? optionsStatus?.detail ?? "正在检测沪金与国际金期权接入能力"}</span>
                {optionsStatus?.observed_at ? (
                  <small>{formatOptionQuantity(optionsStatus.quote_count)} 合约 · 截至 {formatOptionObservedAt(optionsStatus.observed_at, displayTimeZone)}</small>
                ) : null}
              </div>

              {selectedOptionExpiry ? (
                <>
                  <label className="expert-option-expiry-picker">
                    <span>到期剖面</span>
                    <select
                      value={selectedOptionExpiryValue}
                      onChange={(event) => setSelectedOptionExpiryKey(event.target.value)}
                      aria-label="选择黄金期权到期月份"
                    >
                      {(optionsStatus?.expiries ?? []).map((expiry) => (
                        <option key={expertOptionExpiryKey(expiry)} value={expertOptionExpiryKey(expiry)}>
                          {expiry.underlying_contract_id.toUpperCase()} · {expiry.expiry}
                        </option>
                      ))}
                    </select>
                  </label>

                  <div className="expert-option-metrics">
                    <span><small>标的期货</small><strong>{formatOptionMetric(selectedOptionExpiry.underlying_price)}</strong><em>{optionPriceUnitLabel(optionsStatus?.price_unit)}</em></span>
                    <span><small>P/C · 持仓</small><strong>{formatOptionMetric(selectedOptionExpiry.put_call_open_interest_ratio)}</strong><em>{optionPositioningLabel(selectedOptionExpiry.positioning_state)}</em></span>
                    <span><small>P/C · 成交</small><strong>{formatOptionMetric(selectedOptionExpiry.put_call_volume_ratio)}</strong><em>真实成交量</em></span>
                    <span><small>参考 IV</small><strong>{selectedOptionExpiry.reference_iv === null ? "—" : `${(selectedOptionExpiry.reference_iv * 100).toFixed(1)}%`}</strong><em>{optionsStatus?.reference_data_as_of ?? "无参考日"}</em></span>
                  </div>

                  <div className="expert-option-structure" aria-label="期权持仓结构关键行权价">
                    <span className="is-call-wall"><small>CALL WALL</small><strong>{formatOptionMetric(selectedOptionExpiry.call_wall_strike, 0)}</strong><em>最大 Call OI</em></span>
                    <span className="is-max-pain"><small>MAX PAIN</small><strong>{formatOptionMetric(selectedOptionExpiry.max_pain_strike, 0)}</strong><em>ATM {formatOptionMetric(selectedOptionExpiry.atm_strike, 0)}</em></span>
                    <span className="is-put-wall"><small>PUT WALL</small><strong>{formatOptionMetric(selectedOptionExpiry.put_wall_strike, 0)}</strong><em>最大 Put OI</em></span>
                  </div>

                  <div className="expert-option-evidence-strip">
                    <span>预期波动 {selectedOptionExpiry.expected_move_percent === null ? "—" : `±${selectedOptionExpiry.expected_move_percent.toFixed(2)}%`}</span>
                    <span>Delta 覆盖 {(selectedOptionExpiry.delta_coverage_ratio * 100).toFixed(0)}% · EOD</span>
                    <span>GEX 不可用</span>
                  </div>

                  <div className="expert-option-chain">
                    <div className="expert-option-chain-caption">
                      <strong>完整行权价链</strong>
                      <span>{optionStrikeRows.length} 档 · 最新价 / OI / 成交量</span>
                    </div>
                    <div className="expert-option-chain-scroll" ref={optionChainScrollRef}>
                      <table>
                        <thead><tr><th>CALL</th><th>行权价</th><th>PUT</th></tr></thead>
                        <tbody>
                          {optionStrikeRows.map((row) => (
                            <tr
                              key={row.strike}
                              className={row.isAtm ? "is-atm" : ""}
                            >
                              <td className="is-call"><ExpertOptionSide contract={row.call} /></td>
                              <td className="is-strike">
                                <strong>{formatOptionMetric(row.strike, 0)}</strong>
                                <span>
                                  {row.isCallWall ? <i className="is-call">C</i> : null}
                                  {row.isAtm ? <i className="is-atm">ATM</i> : null}
                                  {row.isMaxPain ? <i className="is-pain">M</i> : null}
                                  {row.isPutWall ? <i className="is-put">P</i> : null}
                                </span>
                              </td>
                              <td className="is-put"><ExpertOptionSide contract={row.put} /></td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              ) : (
                <p>尚未取得可展示的真实合约。系统不会生成假 Put/Call、Greeks 或 GEX。</p>
              )}

              {(optionsStatus?.limitations.length ?? 0) > 0 ? (
                <details className="expert-option-limitations">
                  <summary>数据边界与使用约束</summary>
                  <ul>{optionsStatus?.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                </details>
              ) : null}

              <div className="expert-option-sources">
                {(optionsStatus?.markets ?? []).map((source) => (
                  <div key={source.market_id}>
                    <strong>{source.label}</strong>
                    <em>{optionMarketStateLabel(source.state)}</em>
                    <span>{source.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}

          {intelligenceTab === "ai" ? (
            <div className="expert-ai-panel">
              <div className={`expert-ai-connection ${aiReady ? "is-ready" : ""}`}>
                <Bot size={19} />
                <div><strong>{aiReady ? "本机 ChatGPT 已连接" : "等待本机 GPT"}</strong><span>{aiStatus?.detail ?? "正在检测 Codex 账户状态"}</span></div>
              </div>
              <button type="button" className="expert-ai-run" disabled={!aiReady || aiBusy || replayEnabled || candles.length === 0} onClick={() => void requestAiAnalysis()}>
                {aiBusy ? <RotateCcw className="spin" size={15} /> : <Sparkles size={15} />}
                {aiBusy ? "分析行情中" : replayEnabled ? "退出回放后分析" : "用当前账户分析"}
              </button>
              <small className="expert-ai-quota-note">只读临时会话；发送来源、截止时间、最近 Bar 与已启用策略，会消耗本机 Codex/ChatGPT 配额。</small>
              {aiError ? <div className="expert-ai-error">{aiError}</div> : null}
              {aiAnalysis ? (
                <div className="expert-ai-answer">
                  <header><strong>GPT 行情研判</strong><span>{aiAnalysis.data_as_of ?? aiAnalysis.generated_at}</span></header>
                  <p>{aiAnalysis.analysis ?? aiAnalysis.detail}</p>
                  <small>{aiAnalysis.source_id} · {aiAnalysis.bar_count} 根 Bar</small>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </aside>

      <footer className="expert-replay-deck">
        <button
          type="button"
          className={replayEnabled ? "is-active" : ""}
          aria-pressed={replayEnabled}
          onClick={() => {
            const nextEnabled = !replayEnabled;
            setReplayEnabled(nextEnabled);
            setReplayPlaying(false);
            setReplayDraftIndex(null);
            const nextIndex = Math.max(0, replayableLastIndex - 119);
            setReplayCutoff(nextEnabled && candles[nextIndex]
              ? candleReplayCutoff(candles, nextIndex)
              : null);
          }}
        >
          <Play size={14} />{replayEnabled ? "退出回放" : "行情回放"}
        </button>
        <button
          type="button"
          disabled={!replayEnabled}
          onClick={() => setReplayPlaying((current) => !current)}
          title={replayPlaying ? "暂停" : "播放"}
          aria-label={replayPlaying ? "暂停行情回放" : "播放行情回放"}
          aria-pressed={replayPlaying}
        >
          {replayPlaying ? <CirclePause size={17} /> : <CirclePlay size={17} />}
        </button>
        <input
          type="range"
          min={0}
          max={Math.max(0, replayableLastIndex)}
          value={Math.max(0, Math.min(displayedReplayIndex, Math.max(0, replayableLastIndex)))}
          disabled={!replayEnabled || replayableLastIndex < 0}
          onPointerDown={() => {
            setReplayPlaying(false);
            setReplayDraftIndex(Math.max(0, replayIndex));
          }}
          onChange={(event) => {
            setReplayDraftIndex(Number(event.target.value));
          }}
          onPointerUp={(event) => commitReplayPosition(Number(event.currentTarget.value))}
          onKeyUp={(event) => commitReplayPosition(Number(event.currentTarget.value))}
          onBlur={(event) => commitReplayPosition(Number(event.currentTarget.value))}
          aria-label="回放位置"
        />
        <span className="expert-replay-time">{displayedReplayCutoff ? formatDateTimeInTimeZone(displayedReplayCutoff, displayTimeZone) : "等待历史"}</span>
        <label>速度
          <select value={replaySpeed} onChange={(event) => setReplaySpeed(Number(event.target.value))} disabled={!replayEnabled}>
            <option value={1}>1×</option><option value={2}>2×</option><option value={5}>5×</option><option value={10}>10×</option>
          </select>
        </label>
        <div className="expert-backtest-strip" title={backtest.caveat}>
          <span>{backtestReady ? "实验回测" : `回测计算中 ${backtestProgress}%`}</span>
          {backtestReady ? (
            <>
              <strong className={backtest.totalReturnPercent >= 0 ? "is-up" : "is-down"}>{formatSigned(backtest.totalReturnPercent, 2, "%")}</strong>
              <span>{backtest.tradeCount} 笔</span>
              <span>胜率 {backtest.winRate.toFixed(0)}%</span>
              <span>回撤 {backtest.maxDrawdownPercent.toFixed(2)}%</span>
            </>
          ) : <strong>—</strong>}
        </div>
      </footer>
    </div>
  );
}
