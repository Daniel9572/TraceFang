import {
  Activity,
  BookOpen,
  Bot,
  BrainCircuit,
  CalendarClock,
  ChevronLeft,
  Eye,
  EyeOff,
  Gauge,
  Layers3,
  Magnet,
  Minus,
  MousePointer2,
  Play,
  Radio,
  RotateCcw,
  Sparkles,
  Square,
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
  IMPORTANT_EVENT_DISPLAY_STRATEGY,
  projectExpertEventStrategies,
} from "./expertEvents";
import { buildExpertEventAssessments } from "./expertEventScoring";
import {
  buildSmartTrendLines,
  buildTechnicalOverlaySeries,
  candlePrefixRevisionKey,
  latestFinalCandleIndex,
} from "./expertTechnical";
import {
  createReplayProjectionStart,
  formatReplayTimecode,
  REPLAY_DERIVED_DOMAIN_NOTICE,
  REPLAY_RATE_LABEL,
  replaySafeLiveDerivedValue,
  type ReplayProjectionState,
} from "./expertReplay";
import { ChartLayerManager } from "./ChartLayerManager";
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
  ExpertEventAssessment,
  ExpertIndicatorSeriesView,
  ExpertMarketEvent,
  ExpertMultiTimeframeContext,
  ExpertOptionContract,
  ExpertOptionsStatus,
  ExpertShfePositioningContext,
  ExpertStrategyId,
  ExpertVolatilityContext,
} from "./expertTypes";
import { formatDateInTimeZone, formatDateTimeInTimeZone } from "./chartTimeAxis";
import { barDataPeriodId, chartPeriodById, type ChartPeriodId } from "./chartPeriods";
import { MarketChart } from "./MarketChart";
import { PeriodToolbar } from "./PeriodToolbar";
import type { RealtimeBarStream } from "./realtimeBarStream";
import { StrategyDetailDrawer } from "./StrategyDetailDrawer";
import { strategyById } from "./strategyCatalog";
import type { HistoryLoadOutcome, HistoryWindow } from "./historyLoading";
import type {
  Candle,
  HoverCandle,
  MarketPhase,
  MarketSchedule,
  ReplayFrameBounds,
  ReplayFrameCursor,
  ReplayStreamEvent,
  SourceId,
} from "./types";
import { upsertRealtimeBar } from "./chartModel";

import "./expert-mode.css";

interface ExpertModeWorkspaceProps {
  code: string;
  instrumentName: string;
  unit: string;
  candles: Candle[];
  realtimeBarStream: RealtimeBarStream;
  realtimeBarStreamKey: string;
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
  sourceId: SourceId;
  sourceState: "connecting" | "live" | "waiting" | "unavailable";
  liveIndicatorSeries: ExpertIndicatorSeriesView;
  marketEvents: readonly ExpertMarketEvent[];
  marketEventsLoading: boolean;
  marketEventsError: string | null;
  layerWorkspace: ChartLayerWorkspace;
  onLayerWorkspaceChange: (
    update: (current: ChartLayerWorkspace) => ChartLayerWorkspace,
  ) => void;
  historyLoading: boolean;
  loading: boolean;
  error: string | null;
  onPeriodChange: (period: ChartPeriodId) => void;
  onRequestOlderHistory: () => Promise<HistoryLoadOutcome>;
  onRequestHistoryGap: (window: HistoryWindow) => void;
  onExit: () => void;
}

const TIME_ZONES = [
  { id: "Asia/Shanghai", label: "北京" },
  { id: "Europe/London", label: "伦敦" },
  { id: "America/New_York", label: "纽约" },
] as const;

const STRATEGY_STORAGE_KEY = "market-expert-strategies-v3";
const LEGACY_STRATEGY_STORAGE_KEYS = [
  "market-expert-strategies-v2",
  "market-expert-strategies-v1",
] as const;
const V3_DEFAULT_ADDITIONS: ExpertStrategyId[] = ["rsi", "multi-timeframe", "smart-money"];
const CAPITAL_DOMINANCE_STORAGE_KEY = "market-expert-capital-dominance-v1";
const OPENING_GAP_STRATEGY = {
  shortName: "跳空标注",
  description: "只在边界行情完整时标记休市后的首个真实价格点",
} as const;
const SECONDS_PER_DAY = 24 * 60 * 60;
const OPTION_QUANTITY_FORMATTER = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 });

function readStrategies(): ExpertStrategyId[] {
  try {
    const current = window.localStorage.getItem(STRATEGY_STORAGE_KEY);
    const stored = current ?? LEGACY_STRATEGY_STORAGE_KEYS
      .map((key) => window.localStorage.getItem(key))
      .find((value) => value !== null);
    const parsed = JSON.parse(stored ?? "null");
    if (!Array.isArray(parsed)) return DEFAULT_EXPERT_STRATEGIES;
    const known = new Set(EXPERT_STRATEGIES.map((strategy) => strategy.id));
    const values = parsed.filter((value): value is ExpertStrategyId => known.has(value));
    if (parsed.length === 0) return [];
    if (values.length === 0) return DEFAULT_EXPERT_STRATEGIES;
    return current === null
      ? [...new Set([...values, ...V3_DEFAULT_ADDITIONS])]
      : values;
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

function finiteNumber(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
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

function movingAverageAlignmentLabel(
  alignment: "bullish" | "bearish" | "mixed" | "insufficient" | undefined,
): string {
  if (alignment === "bullish") return "多排";
  if (alignment === "bearish") return "空排";
  if (alignment === "mixed") return "缠绕";
  return "—";
}

function bollingerStateLabel(state: "squeeze" | "expanding" | "normal" | undefined): string {
  if (state === "squeeze") return "压缩";
  if (state === "expanding") return "扩张";
  if (state === "normal") return "常态";
  return "—";
}

function timeframeDirectionLabel(direction: "up" | "down" | "mixed" | "unavailable"): string {
  if (direction === "up") return "上行";
  if (direction === "down") return "下行";
  if (direction === "mixed") return "混合";
  return "不足";
}

function timeframeLimitationLabel(value: string | null): string {
  if (value === null) return "当前周期不可比较";
  if (value === "non_positive_close_not_comparable") return "存在非正收盘价，拒绝比较";
  if (value === "history_scan_limit_before_required_sample") return "历史扫描上限内样本仍不足";
  if (value.startsWith("requires_")) return "合格已收盘 Bar 不足 20 根";
  return value;
}

function multiTimeframeOpportunity(context: ExpertMultiTimeframeContext | null): {
  tone: "bullish" | "bearish" | "neutral";
  title: string;
  detail: string;
} | null {
  if (context === null || !context.comparison.comparable) return null;
  const short = context.timeframes.find((item) => item.horizon === "short");
  const medium = context.timeframes.find((item) => item.horizon === "medium");
  const long = context.timeframes.find((item) => item.horizon === "long");
  if (!short || !long) return null;
  if (long.direction === "up" && short.direction === "down") {
    return {
      tone: "bullish",
      title: "长多 × 短空：回撤候选",
      detail: medium?.direction === "up"
        ? "中长周期保持上行；等待短周期 RSI 回穿、W 底或 2B 底部再确认。"
        : "长期上行而短期回撤；只标记相对低位观察窗，不直接视为买点。",
    };
  }
  if (long.direction === "down" && short.direction === "up") {
    return {
      tone: "bearish",
      title: "长空 × 短多：反弹候选",
      detail: medium?.direction === "down"
        ? "中长周期保持下行；等待短周期 RSI 回落、M 顶或 2B 顶部再确认。"
        : "长期下行而短期反弹；只标记相对高位观察窗，不直接视为卖点。",
    };
  }
  if (context.comparison.state === "aligned") {
    return {
      tone: context.comparison.aligned_direction === "up" ? "bullish" : "bearish",
      title: context.comparison.aligned_direction === "up" ? "周期同向上行" : "周期同向下行",
      detail: "同向不代表追价优势；等待波动收敛、结构回踩或风险收益改善。",
    };
  }
  return {
    tone: "neutral",
    title: "周期张力尚无清晰优势",
    detail: "方向混合但未形成明确长短反向组合，继续等待确认。",
  };
}

function eventDirectionLabel(direction: ExpertEventAssessment["observedDirection"]): string {
  if (direction === "bullish") return "金价向上";
  if (direction === "bearish") return "金价向下";
  if (direction === "neutral") return "反应有限";
  return "等待反应";
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

function formatSignedQuantity(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${OPTION_QUANTITY_FORMATTER.format(value)}`;
}

function volumePriceStateLabel(state: "confirming" | "diverging" | "unavailable"): string {
  if (state === "confirming") return "量价确认";
  if (state === "diverging") return "量价背离";
  return "量价样本不足";
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
  candles: liveCandles,
  realtimeBarStream,
  realtimeBarStreamKey,
  periodId,
  livePrice: currentLivePrice,
  change,
  changePercent,
  observedAt: liveObservedAt,
  referencePrice,
  timelineResolutionSeconds,
  priceDigits,
  marketPhase,
  marketSchedule,
  sourceLabel,
  sourceId,
  sourceState,
  liveIndicatorSeries,
  marketEvents,
  marketEventsLoading,
  marketEventsError,
  layerWorkspace,
  onLayerWorkspaceChange,
  historyLoading,
  loading,
  error,
  onPeriodChange,
  onRequestOlderHistory,
  onRequestHistoryGap,
  onExit,
}: ExpertModeWorkspaceProps) {
  const period = chartPeriodById(periodId);
  const expertBarPeriodId = barDataPeriodId(period);
  const [displayTimeZone, setDisplayTimeZone] = useState("Asia/Shanghai");
  const [enabledStrategies, setEnabledStrategies] = useState<ExpertStrategyId[]>(readStrategies);
  const [selectedStrategyId, setSelectedStrategyId] = useState<ExpertStrategyId | null>(null);
  const [capitalDominanceEnabled, setCapitalDominanceEnabled] = useState(readCapitalDominanceStrategy);
  const setLayerWorkspace = onLayerWorkspaceChange;
  const sessionLayerNormalizedRef = useRef(false);
  const [layerManagerOpen, setLayerManagerOpen] = useState(true);
  const [drawingTool, setDrawingTool] = useState<ExpertDrawingTool | null>(null);
  const [drawingSnapMode, setDrawingSnapMode] = useState<ExpertDrawingSnapMode>("weak");
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [backtestRevision, setBacktestRevision] = useState(0);
  const [intelligenceTab, setIntelligenceTab] = useState<"signals" | "options" | "ai">("signals");
  const [optionsStatus, setOptionsStatus] = useState<ExpertOptionsStatus | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [selectedOptionExpiryKey, setSelectedOptionExpiryKey] = useState<string | null>(null);
  const optionChainScrollRef = useRef<HTMLDivElement | null>(null);
  const [volatilityContext, setVolatilityContext] = useState<ExpertVolatilityContext | null>(null);
  const [volatilityContextError, setVolatilityContextError] = useState<string | null>(null);
  const [multiTimeframeContext, setMultiTimeframeContext] = useState<ExpertMultiTimeframeContext | null>(null);
  const [multiTimeframeError, setMultiTimeframeError] = useState<string | null>(null);
  const [positioningContext, setPositioningContext] = useState<ExpertShfePositioningContext | null>(null);
  const [positioningContextError, setPositioningContextError] = useState<string | null>(null);
  const [aiStatus, setAiStatus] = useState<ExpertAiStatus | null>(null);
  const [aiAnalysis, setAiAnalysis] = useState<ExpertAiAnalysis | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const replaySocketRef = useRef<WebSocket | null>(null);
  const [replayBounds, setReplayBounds] = useState<ReplayFrameBounds | null>(null);
  const [replayCursor, setReplayCursor] = useState<number | null>(null);
  const [replayCursorFrame, setReplayCursorFrame] = useState<ReplayFrameCursor | null>(null);
  const [replayState, setReplayState] = useState<ReplayProjectionState>("live");
  const [replayCandles, setReplayCandles] = useState<Candle[]>([]);
  const [replayPrice, setReplayPrice] = useState<number | null>(null);
  const [replayError, setReplayError] = useState<string | null>(null);
  const [replayWarning, setReplayWarning] = useState<string | null>(null);
  const replayActive = replayState !== "live";
  const replaySupported = replayBounds?.source_ids.includes(sourceId) ?? false;
  const candles = replayActive ? replayCandles : liveCandles;
  const livePrice = replayActive ? replayPrice : currentLivePrice;
  const observedAt = replayActive
    ? replayCursorFrame?.received_at ?? null
    : liveObservedAt;
  const replayCutoff = replayActive && replayCursorFrame
    ? Date.parse(replayCursorFrame.received_at) / 1_000
    : null;
  const goldOptionsApplicable = code === "XAUUSD" || code === "AU8888";
  const volatilityContextEnabled = enabledStrategies.includes("vix-gvz");
  const multiTimeframeEnabled = enabledStrategies.includes("multi-timeframe");
  const positioningContextEnabled = enabledStrategies.includes("volume-open-interest");
  const positioningProduct = code === "AU8888" ? "au" : code === "AG8888" ? "ag" : null;
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

  const closeReplaySocket = useCallback(() => {
    const socket = replaySocketRef.current;
    replaySocketRef.current = null;
    socket?.close();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void marketApi.replayFrameBounds()
      .then((value) => {
        if (controller.signal.aborted) return;
        setReplayBounds(value);
        setReplayError(value.state === "unavailable" ? value.detail : null);
        if (value.state === "ready" && value.first_sequence !== null) {
          setReplayCursor((current) => {
            return current === null
              ? value.first_sequence
              : Math.min(value.last_sequence ?? current, Math.max(value.first_sequence!, current));
          });
        }
      })
      .catch((requestError) => {
        if (!controller.signal.aborted) {
          setReplayError(requestError instanceof Error ? requestError.message : String(requestError));
        }
      });
    return () => controller.abort();
  }, [code]);

  useEffect(() => {
    if (replayCursor === null || replayState === "playing") return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void marketApi.replayFrameCursor(replayCursor, controller.signal)
        .then((value) => {
          if (controller.signal.aborted) return;
          setReplayCursorFrame(value);
          setReplayError(null);
        })
        .catch((requestError) => {
          if (!controller.signal.aborted) {
            setReplayError(requestError instanceof Error ? requestError.message : String(requestError));
          }
        });
    }, 80);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [replayCursor, replayState]);

  useEffect(() => () => closeReplaySocket(), [closeReplaySocket]);

  useEffect(() => {
    closeReplaySocket();
    setReplayState("live");
    setReplayCandles([]);
    setReplayPrice(null);
    setReplayWarning(null);
  }, [closeReplaySocket, code, periodId, sourceId]);

  const stopReplay = useCallback(() => {
    closeReplaySocket();
    setReplayState("stopped");
    setReplayWarning("回放已停止；再次播放将从留存首帧空状态重建");
  }, [closeReplaySocket]);

  const returnToLive = useCallback(() => {
    closeReplaySocket();
    setReplayState("live");
    setReplayCandles([]);
    setReplayPrice(null);
    setReplayError(null);
    setReplayWarning(null);
    if (replayBounds?.state === "ready") {
      setReplayCursor(replayBounds.first_sequence);
      setReplayCursorFrame(null);
    }
  }, [closeReplaySocket, replayBounds]);

  const startReplay = useCallback(async () => {
    if (replayBounds?.state !== "ready") return;
    try {
      const latestBounds = await marketApi.replayFrameBounds();
      const projection = createReplayProjectionStart(latestBounds, period.id);
      if (projection === null) {
        throw new Error(latestBounds.detail ?? "真实行情回放暂不可用");
      }
      closeReplaySocket();
      setReplayBounds(latestBounds);
      setReplayCandles(projection.candles);
      setReplayPrice(projection.price);
      setReplayCursor(projection.startSequence);
      setReplayCursorFrame(null);
      const socket = marketApi.openReplayStream(code, projection);
      replaySocketRef.current = socket;
      setReplayState("playing");
      setReplayError(null);
      setReplayWarning(null);
      let terminalStateReceived = false;
      socket.onmessage = (message) => {
        const event = JSON.parse(String(message.data)) as ReplayStreamEvent;
        if (event.kind === "frame" && event.stream_sequence !== undefined && event.frame_received_at) {
          const sequence = event.stream_sequence ?? projection.startSequence;
          setReplayCursor(sequence);
          setReplayCursorFrame({
            sequence,
            received_at: event.frame_received_at,
            channel: event.frame_channel ?? "unknown",
            connection_id: "",
            provider_sequence: 0,
          });
        } else if (event.kind === "bar" && event.bar) {
          setReplayCandles((current) => upsertRealtimeBar(current, event.bar as Candle));
        } else if (event.kind === "quote" && event.quote) {
          setReplayPrice(finiteNumber(event.quote.last));
        } else if (event.kind === "decode_error") {
          setReplayWarning(event.error ?? "原始帧无法解码");
        } else if (event.kind === "status" && event.state === "completed") {
          terminalStateReceived = true;
          setReplayState("completed");
        } else if (event.kind === "status" && event.state === "unavailable") {
          terminalStateReceived = true;
          setReplayError(event.error ?? "真实行情回放不可用");
          setReplayState("stopped");
        }
      };
      socket.onerror = () => {
        setReplayError("原始帧回放连接中断");
        socket.close();
      };
      socket.onclose = () => {
        if (replaySocketRef.current !== socket) return;
        replaySocketRef.current = null;
        if (!terminalStateReceived) {
          setReplayState("stopped");
          setReplayWarning((current) => current ?? "回放已停止；再次播放将从留存首帧空状态重建");
        }
      };
    } catch (requestError) {
      setReplayError(requestError instanceof Error ? requestError.message : String(requestError));
      setReplayState("stopped");
    }
  }, [
    closeReplaySocket,
    code,
    period.id,
    replayBounds,
  ]);

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
    if (replayActive || intelligenceTab !== "options" || !goldOptionsApplicable) return;
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
  }, [goldOptionsApplicable, intelligenceTab, replayActive]);

  useEffect(() => {
    if (replayActive) return;
    if (!volatilityContextEnabled) {
      setVolatilityContext(null);
      setVolatilityContextError(null);
      return;
    }
    let disposed = false;
    let timer: number | null = null;
    const load = async () => {
      try {
        const value = await marketApi.expertVolatilityContext();
        if (disposed) return;
        setVolatilityContext(value);
        setVolatilityContextError(null);
        timer = window.setTimeout(
          () => void load(),
          Math.max(60, value.refresh_after_seconds) * 1_000,
        );
      } catch (requestError) {
        if (disposed) return;
        setVolatilityContextError(
          requestError instanceof Error ? requestError.message : String(requestError),
        );
        timer = window.setTimeout(() => void load(), 5 * 60 * 1_000);
      }
    };
    void load();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [replayActive, volatilityContextEnabled]);

  useEffect(() => {
    if (replayActive) return;
    if (!multiTimeframeEnabled) {
      setMultiTimeframeContext(null);
      setMultiTimeframeError(null);
      return;
    }
    let disposed = false;
    let timer: number | null = null;
    setMultiTimeframeContext(null);
    const load = async () => {
      try {
        const value = await marketApi.expertMultiTimeframe(code);
        if (disposed) return;
        setMultiTimeframeContext(value);
        setMultiTimeframeError(null);
        timer = window.setTimeout(() => void load(), 60 * 1_000);
      } catch (requestError) {
        if (disposed) return;
        setMultiTimeframeError(
          requestError instanceof Error ? requestError.message : String(requestError),
        );
        timer = window.setTimeout(() => void load(), 60 * 1_000);
      }
    };
    void load();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [code, multiTimeframeEnabled, realtimeBarStreamKey, replayActive]);

  useEffect(() => {
    if (replayActive) return;
    if (!positioningContextEnabled || positioningProduct === null) {
      setPositioningContext(null);
      setPositioningContextError(null);
      return;
    }
    let disposed = false;
    let timer: number | null = null;
    const load = async () => {
      try {
        const value = await marketApi.expertShfePositioning(positioningProduct);
        if (disposed) return;
        setPositioningContext(value);
        setPositioningContextError(null);
        timer = window.setTimeout(
          () => void load(),
          Math.max(30, value.refresh_after_seconds) * 1_000,
        );
      } catch (requestError) {
        if (disposed) return;
        setPositioningContextError(
          requestError instanceof Error ? requestError.message : String(requestError),
        );
        timer = window.setTimeout(() => void load(), 60 * 1_000);
      }
    };
    void load();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [positioningContextEnabled, positioningProduct, replayActive]);

  const finalCandleIndex = useMemo(() => latestFinalCandleIndex(candles), [candles]);
  const firstEvidenceCandle = candles[0];
  const finalEvidenceCandle = finalCandleIndex >= 0 ? candles[finalCandleIndex] : undefined;
  const confirmedPrefixRevisionKey = useMemo(
    () => candlePrefixRevisionKey(candles, finalCandleIndex),
    [candles, finalCandleIndex],
  );
  const confirmedHistoryRevisionKey = [
    code,
    expertBarPeriodId,
    finalCandleIndex,
    confirmedPrefixRevisionKey,
    firstEvidenceCandle?.open_time ?? "empty",
    firstEvidenceCandle?.revision ?? 0,
    finalEvidenceCandle?.open_time ?? "empty",
    finalEvidenceCandle?.revision ?? 0,
  ].join(":");
  const confirmedCandles = useMemo(
    () => finalCandleIndex < 0 ? [] : candles.slice(0, finalCandleIndex + 1),
    // The revision key deliberately ignores repeated updates to the unclosed tail Bar.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [confirmedHistoryRevisionKey],
  );
  const analysisIndex = confirmedCandles.length - 1;
  const enabledStrategyKey = [...enabledStrategies].sort().join(":");
  const indicatorHistoryKey = `${EXPERT_INDICATOR_HISTORY_VERSION}:${code}:${finalEvidenceCandle?.source.provider ?? "pending"}:${expertBarPeriodId}:confirmed`;
  const analysis = useMemo(
    () => buildExpertAnalysisAt(
      confirmedCandles,
      enabledStrategies,
      analysisIndex,
      indicatorHistoryKey,
    ),
    [analysisIndex, confirmedCandles, enabledStrategies, indicatorHistoryKey],
  );
  const technicalOverlaySeries = useMemo(
    () => buildTechnicalOverlaySeries(confirmedCandles, enabledStrategies, analysisIndex),
    [analysisIndex, confirmedCandles, enabledStrategies],
  );
  const smartTrendLines = useMemo(
    () => enabledStrategies.includes("auto-trend")
      ? buildSmartTrendLines(confirmedCandles, analysisIndex)
      : [],
    [analysisIndex, confirmedCandles, enabledStrategies],
  );
  const trendLineStats = useMemo(() => ({
    active: smartTrendLines.filter((line) => line.status !== "invalidated").length,
    invalidated: smartTrendLines.filter((line) => line.status === "invalidated").length,
  }), [smartTrendLines]);
  const patternStats = useMemo(() => ({
    active: analysis.pricePatterns.filter((pattern) => pattern.status === "confirmed").length
      + analysis.marketStructureEvents.filter((event) => event.status === "confirmed").length,
    invalidated: analysis.pricePatterns.filter((pattern) => pattern.status === "invalidated").length
      + analysis.marketStructureEvents.filter((event) => event.status === "invalidated").length,
  }), [analysis.marketStructureEvents, analysis.pricePatterns]);
  const displayedMultiTimeframeContext = replaySafeLiveDerivedValue(
    replayState,
    multiTimeframeContext,
  );
  const displayedVolatilityContext = replaySafeLiveDerivedValue(
    replayState,
    volatilityContext,
  );
  const displayedPositioningContext = replaySafeLiveDerivedValue(
    replayState,
    positioningContext,
  );
  const timeframeOpportunity = useMemo(
    () => multiTimeframeOpportunity(displayedMultiTimeframeContext),
    [displayedMultiTimeframeContext],
  );
  const indicatorSeries = useMemo(
    () => replayActive
      ? buildExpertIndicatorSeriesAt(
        confirmedCandles,
        analysisIndex,
        `${indicatorHistoryKey}:replay`,
      )
      : liveIndicatorSeries,
    [analysisIndex, confirmedCandles, indicatorHistoryKey, liveIndicatorSeries, replayActive],
  );
  const backtestLastIndex = analysisIndex;
  const backtestRunner = useMemo(
    () => createExpertBacktestRunner(confirmedCandles, enabledStrategies, indicatorHistoryKey),
    // Strategy identity, rather than array order, owns the causal backtest index.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [confirmedCandles, enabledStrategyKey, indicatorHistoryKey],
  );
  useEffect(() => {
    if (historyLoading) return;
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
  }, [backtestRunner, historyLoading]);
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
  const firstCandleTime = candles[0]
    ? Date.parse(candles[0].open_time) / 1_000
    : null;
  const firstSessionTime = firstCandleTime;
  const lastSessionTime = candles.at(-1)
    ? Date.parse(candles.at(-1)!.open_time) / 1_000
    : null;
  const sessionStartDay = firstSessionTime === null
    ? null
    : Math.floor(firstSessionTime / SECONDS_PER_DAY) * SECONDS_PER_DAY;
  const sessionEndDay = lastSessionTime === null
    ? null
    : (Math.floor(lastSessionTime / SECONDS_PER_DAY) + 1) * SECONDS_PER_DAY;
  const eventStrategyProjection = useMemo(
    () => projectExpertEventStrategies(
      importantEventsEnabled,
      replayCutoff,
      marketEvents,
    ),
    [importantEventsEnabled, marketEvents, replayCutoff],
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

  const latestBar = candles.at(-1) ?? null;
  const displayedBar = hover ?? (latestBar ? {
    time: Date.parse(latestBar.open_time) / 1_000,
    open: Number(latestBar.open),
    high: Number(latestBar.high),
    low: Number(latestBar.low),
    close: Number(latestBar.close),
  } : null);
  const displayedReferencePrice = replayActive
    ? finiteNumber(replayCandles[0]?.open)
    : referencePrice;
  const displayedChange = replayActive
    ? livePrice !== null && displayedReferencePrice !== null
      ? livePrice - displayedReferencePrice
      : null
    : change;
  const displayedChangePercent = replayActive
    ? displayedChange !== null && displayedReferencePrice !== null && displayedReferencePrice !== 0
      ? displayedChange / displayedReferencePrice * 100
      : null
    : changePercent;
  const priceTone = (displayedChange ?? 0) >= 0
    ? "is-up"
    : "is-down";
  const displayedOptionsStatus = replaySafeLiveDerivedValue(replayState, optionsStatus);
  const displayedOptionsError = replaySafeLiveDerivedValue(replayState, optionsError);
  const displayedAiStatus = replaySafeLiveDerivedValue(replayState, aiStatus);
  const displayedAiAnalysis = replaySafeLiveDerivedValue(replayState, aiAnalysis);
  const displayedAiError = replaySafeLiveDerivedValue(replayState, aiError);
  const selectedOptionExpiry = useMemo(
    () => resolveExpertOptionExpiry(
      displayedOptionsStatus?.expiries ?? [],
      selectedOptionExpiryKey,
    ),
    [displayedOptionsStatus?.expiries, selectedOptionExpiryKey],
  );
  const optionStrikeRows = useMemo(
    () => buildExpertOptionStrikeRows(
      displayedOptionsStatus?.contracts ?? [],
      selectedOptionExpiry,
    ),
    [displayedOptionsStatus?.contracts, selectedOptionExpiry],
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
  const aiReady = displayedAiStatus?.state === "ready"
    && displayedAiStatus.authenticated === true;

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
    if (replayActive) {
      setAiError(REPLAY_DERIVED_DOMAIN_NOTICE);
      return;
    }
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
  }, [code, enabledStrategies, period.id, period.mode, replayActive]);

  const eventReferenceTime = replayCutoff ?? Date.now() / 1_000;
  const latestEvent = !importantEventsEnabled || eventReferenceTime === null
    ? undefined
    : marketEvents.find((event) => event.time >= eventReferenceTime);
  const eventAssessmentTime = observedAt === null ? null : Date.parse(observedAt) / 1_000;
  const eventAssessments = useMemo(
    () => importantEventsEnabled
      ? buildExpertEventAssessments(
        candles,
        latestEvent ? [latestEvent] : [],
        eventAssessmentTime,
        candles.length - 1,
      )
      : [],
    [
      candles,
      eventAssessmentTime,
      importantEventsEnabled,
      latestEvent,
    ],
  );
  const eventAssessmentById = useMemo(
    () => new Map(eventAssessments.map((assessment) => [assessment.eventId, assessment] as const)),
    [eventAssessments],
  );
  const latestEventAssessment = latestEvent
    ? eventAssessmentById.get(latestEvent.id)
    : undefined;
  const visibleEvents = eventStrategyProjection.displayMarkers;
  const chartLayers = useMemo(
    () => buildChartLayers(layerWorkspace, {
      indicatorSeries,
      sessionBands,
      eventMarkers: visibleEvents,
      priceLevels: analysis.levels,
      valueZones: analysis.valueZones,
      trendLines: smartTrendLines,
      overlaySeries: technicalOverlaySeries,
      pricePatterns: analysis.pricePatterns,
      marketStructureEvents: analysis.marketStructureEvents,
    }),
    [
      analysis.levels,
      analysis.marketStructureEvents,
      analysis.pricePatterns,
      analysis.valueZones,
      indicatorSeries,
      layerWorkspace,
      sessionBands,
      smartTrendLines,
      technicalOverlaySeries,
      visibleEvents,
    ],
  );
  const selectedStrategy = selectedStrategyId === null
    ? null
    : strategyById(selectedStrategyId);

  return (
    <div className="expert-workspace" data-replay={replayState}>
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
          <strong>{formatPrice(livePrice, priceDigits)}</strong>
          <span>{formatSigned(displayedChange, priceDigits)} · {formatSigned(displayedChangePercent, 2, "%")}</span>
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
          <div>
            <strong>{replayActive
              ? replayState === "playing"
                ? "回放中"
                : replayState === "completed"
                  ? "回放完成"
                  : "回放已停止"
              : marketPhase === "closed" ? "休市" : "实时"}</strong>
            <small>{replayActive ? REPLAY_RATE_LABEL : historyLoading ? `历史加载中 · ${sourceLabel}` : sourceLabel}</small>
          </div>
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
            trendLineStats={trendLineStats}
            patternStats={patternStats}
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
          realtimeBarStream={realtimeBarStream}
          realtimeBarStreamKey={realtimeBarStreamKey}
          period={period}
          livePrice={livePrice}
          referencePrice={displayedReferencePrice}
          timelineResolutionSeconds={timelineResolutionSeconds}
          priceDigits={priceDigits}
          marketPhase={marketPhase}
          marketSchedule={marketSchedule}
          historyLoading={replayActive ? false : historyLoading}
          onRequestOlderHistory={replayActive
            ? async () => ({ state: "exhausted", added: 0, advancedMinutes: 0 })
            : onRequestOlderHistory}
          onRequestHistoryGap={replayActive ? () => undefined : onRequestHistoryGap}
          onHover={setHover}
          appearance="expert"
          displayTimeZone={displayTimeZone}
          replayMode={replayActive}
          replayIndex={replayActive ? candles.length - 1 : null}
          replayCutoff={replayCutoff}
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
        {loading && candles.length === 0 ? <div className="expert-chart-message"><RotateCcw className="spin" size={18} />正在读取{instrumentName}</div> : null}
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
              title={marketEventsError
                ? `事件事实库不可用：${marketEventsError}`
                : "视觉策略：控制图上事件节点和侧栏事件提示；关闭只隐藏显示，不改变资金主导的接管时间判断"}
              onClick={toggleImportantEventStrategy}
            >
              <span className="strategy-quality is-event" />
              <div>
                <strong>{IMPORTANT_EVENT_DISPLAY_STRATEGY.shortName}</strong>
                <small>{IMPORTANT_EVENT_DISPLAY_STRATEGY.description}</small>
              </div>
              <span className="strategy-provenance">
                <small>{marketEventsLoading
                  ? "事实库 · 加载中"
                  : marketEventsError
                    ? "事实库 · 不可用"
                    : `事实库 · ${marketEvents.length} 条`}</small>
                <em>{marketEventsError
                  ? "无数据"
                  : capitalDominanceEnabled && importantEventsEnabled ? "独立联动" : "视觉策略"}</em>
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
                  : "条件数据";
              return (
                <article
                  key={strategy.id}
                  className={`expert-strategy-row ${enabled ? "is-enabled" : ""}`}
                >
                  <button
                    type="button"
                    className="expert-strategy-toggle"
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
                  <button
                    type="button"
                    className="expert-strategy-detail-button"
                    aria-label={`查看${strategy.name}策略详情`}
                    aria-haspopup="dialog"
                    title="查看原理、参考依据与边界条件"
                    onClick={() => setSelectedStrategyId(strategy.id)}
                  >
                    <BookOpen size={13} />
                  </button>
                </article>
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
            disabled={!goldOptionsApplicable}
            title={goldOptionsApplicable ? "黄金期权结构" : "当前只接入黄金期权，白银不复用黄金期权数据"}
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
                <span>MA <b>{movingAverageAlignmentLabel(analysis.indicators.movingAverage?.alignment)}</b></span>
                <span>BOLL <b>{bollingerStateLabel(analysis.indicators.bollinger?.state)}</b></span>
                <span>九转 <b>{analysis.indicators.nineCount === null ? "—" : `${analysis.indicators.nineCount.count}/9`}</b></span>
                <span>MACD <b>{analysis.indicators.macd?.histogram.toFixed(2) ?? "—"}</b></span>
                <span>KDJ <b>{analysis.indicators.kdj?.j.toFixed(1) ?? "—"}</b></span>
                <span>RSI <b>{analysis.indicators.rsi?.value.toFixed(1) ?? "—"}</b></span>
                <span>POC≈ <b>{analysis.indicators.pocPrice?.toFixed(priceDigits) ?? "—"}</b></span>
              </div>
              {multiTimeframeEnabled ? (
                <section className="expert-context-card is-timeframe" aria-label="1小时、日线与周线周期张力">
                  <header>
                    <div><TrendingUp size={13} /><strong>周期张力</strong></div>
                    <span>1H · 1D · 1W</span>
                  </header>
                  {replayActive ? (
                    <div className="expert-context-empty">{REPLAY_DERIVED_DOMAIN_NOTICE}</div>
                  ) : displayedMultiTimeframeContext ? (
                    <>
                      <div className="expert-timeframe-ladder">
                        {displayedMultiTimeframeContext.timeframes.map((item) => (
                          <article key={item.horizon} className={`is-${item.direction}`}>
                            <span>{item.horizon === "short" ? "短" : item.horizon === "medium" ? "中" : "长"}</span>
                            <div>
                              <strong>{item.period_id.toUpperCase()}</strong>
                              <small>{item.state === "ready"
                                ? `SMA5 ${item.sma_fast?.toFixed(priceDigits) ?? "—"} / SMA20 ${item.sma_slow?.toFixed(priceDigits) ?? "—"}`
                                : item.state === "insufficient_data"
                                  ? `${item.used_bar_count}/${item.required_final_bars} 根已收盘 Bar`
                                  : timeframeLimitationLabel(item.limitation)}</small>
                            </div>
                            <b>{timeframeDirectionLabel(item.direction)}</b>
                            <em>{formatSigned(item.window_return_percent, 1, "%")}</em>
                          </article>
                        ))}
                      </div>
                      {timeframeOpportunity ? (
                        <div className={`expert-timeframe-opportunity is-${timeframeOpportunity.tone}`}>
                          <strong>{timeframeOpportunity.title}</strong>
                          <span>{timeframeOpportunity.detail}</span>
                        </div>
                      ) : (
                        <div className="expert-context-empty">
                          {displayedMultiTimeframeContext.comparison.incomparable_reasons.length > 0
                            ? displayedMultiTimeframeContext.comparison.incomparable_reasons
                              .map((reason) => timeframeLimitationLabel(reason.split(":").slice(1).join(":")))
                              .join(" · ")
                            : "暂不可比较：三个周期均需至少 20 根在共同 as-of 前可用的已收盘 Bar。"}
                        </div>
                      )}
                      <footer className="expert-context-source">
                        <span>同品种 · 同数据源 · 已收盘</span>
                        <time dateTime={displayedMultiTimeframeContext.decision_as_of}>
                          {formatOptionObservedAt(displayedMultiTimeframeContext.decision_as_of, displayTimeZone)}
                        </time>
                      </footer>
                    </>
                  ) : <div className="expert-context-empty">{multiTimeframeError ?? "正在比较 1H / 1D / 1W 已收盘走势"}</div>}
                  {!replayActive && multiTimeframeError && displayedMultiTimeframeContext ? <small className="expert-context-warning">刷新失败，保留上一份快照：{multiTimeframeError}</small> : null}
                  <p>周期差异只定位相对优势观察窗；必须等待短周期结构或 RSI 确认，不输出胜率或保证入场。</p>
                </section>
              ) : null}
              {volatilityContextEnabled ? (
                <section className="expert-context-card is-volatility" aria-label="VIX 与 GVZ 日终波动率背景">
                  <header>
                    <div><Activity size={13} /><strong>隐含波动背景</strong></div>
                    <span>EOD · 不判方向</span>
                  </header>
                  {replayActive ? (
                    <div className="expert-context-empty">{REPLAY_DERIVED_DOMAIN_NOTICE}</div>
                  ) : displayedVolatilityContext ? (
                    <div className="expert-volatility-grid">
                      {displayedVolatilityContext.indices.map((index) => {
                        const percentile = index.trailing_percentile_252;
                        return (
                          <article key={index.index_code}>
                            <header><b>{index.index_code}</b><strong>{index.value.toFixed(2)}</strong></header>
                            <small>{index.underlying} 期权 · 未来约 {index.expected_horizon_days} 日波动</small>
                            <i className="expert-context-meter" aria-hidden="true">
                              <span style={{ width: `${Math.min(100, Math.max(0, percentile ?? 0))}%` }} />
                            </i>
                            <footer>
                              <span>252日分位 {percentile === null ? "—" : `${percentile.toFixed(0)}%`}</span>
                              <time dateTime={index.as_of}>{index.as_of}</time>
                            </footer>
                          </article>
                        );
                      })}
                    </div>
                  ) : <div className="expert-context-empty">{volatilityContextError ?? "正在读取 Cboe 日终数据"}</div>}
                  {!replayActive && volatilityContextError && displayedVolatilityContext ? <small className="expert-context-warning">刷新失败，保留上一份 EOD：{volatilityContextError}</small> : null}
                  <p>VIX 是 SPX 风险波动；GVZ 才是 GLD 期权隐含波动。两者只作风险背景，不直接推断金价涨跌。</p>
                </section>
              ) : null}
              {positioningContextEnabled ? (
                <section className="expert-context-card is-positioning" aria-label="沪金沪银成交量与持仓量背景">
                  <header>
                    <div><Layers3 size={13} /><strong>量价与持仓</strong></div>
                    <span>SHFE · 延迟快照</span>
                  </header>
                  {replayActive ? (
                    <div className="expert-context-empty">{REPLAY_DERIVED_DOMAIN_NOTICE}</div>
                  ) : positioningProduct === null ? (
                    <div className="expert-context-empty">仅在沪金加权 AU8888 / 沪银加权 AG8888 读取交易所量仓。</div>
                  ) : displayedPositioningContext ? (
                    <>
                      <dl className="expert-positioning-metrics">
                        <div><dt>成交量</dt><dd>{formatOptionQuantity(displayedPositioningContext.volume)}</dd></div>
                        <div><dt>总持仓</dt><dd>{formatOptionQuantity(displayedPositioningContext.open_interest)}</dd></div>
                        <div><dt>持仓变化</dt><dd>{formatSignedQuantity(displayedPositioningContext.open_interest_change)}</dd></div>
                        <div><dt>真实合约</dt><dd>{displayedPositioningContext.contract_count}</dd></div>
                      </dl>
                      <div className="expert-positioning-readout">
                        <strong>{volumePriceStateLabel(analysis.indicators.volumePriceState)}</strong>
                        <span>{displayedPositioningContext.open_interest_change === null
                          ? "官方快照未给完整 ΔOI，不补 0、不推断多空"
                          : "ΔOI 仅表示未平仓参与变化，仍不等于净多或净空"}</span>
                      </div>
                      <footer className="expert-context-source">
                        <time dateTime={displayedPositioningContext.as_of}>{formatOptionObservedAt(displayedPositioningContext.as_of, displayTimeZone)}</time>
                        <a href={displayedPositioningContext.source.source_url} target="_blank" rel="noreferrer">交易所源</a>
                      </footer>
                    </>
                  ) : <div className="expert-context-empty">{positioningContextError ?? "正在读取 SHFE 延迟量仓"}</div>}
                  {!replayActive && positioningContextError && displayedPositioningContext ? <small className="expert-context-warning">刷新失败，保留上一份快照：{positioningContextError}</small> : null}
                </section>
              ) : null}
              <div className="expert-signal-feed">
                {analysis.signals.slice(0, 8).map((signal) => (
                  <article key={signal.id} className={`is-${signal.direction}`}>
                    <header><strong>{signal.title}</strong><span>{signalDirectionLabel(signal.direction)} · {Math.round(signal.confidence * 100)}</span></header>
                    <p>{signal.detail}</p>
                    <small>{signal.evidence.join(" · ")}</small>
                  </article>
                ))}
                {analysis.signals.length === 0 ? <div className="expert-empty">等待足够行情样本</div> : null}
              </div>
              {latestEvent ? (
                <div
                  className="expert-next-event"
                  data-tier={latestEvent.baselineTier}
                  title={latestEvent.directionRule}
                >
                  <CalendarClock size={15} />
                  <div>
                    <strong><b>{latestEvent.baselineTier}</b>{latestEvent.title}</strong>
                    <span>{latestEvent.timePrecision === "date"
                      ? formatDateInTimeZone(latestEvent.time, displayTimeZone)
                      : formatDateTimeInTimeZone(latestEvent.time, displayTimeZone)} · {latestEvent.source}</span>
                    {latestEventAssessment ? (
                      <small className="expert-event-score">
                        冲击 {latestEventAssessment.shockScore ?? "—"}/{latestEventAssessment.shockCoverage}%
                        <i />趋势 {latestEventAssessment.regimeScore ?? "—"}/{latestEventAssessment.regimeCoverage}%
                        <i />{eventDirectionLabel(latestEventAssessment.observedDirection)}
                      </small>
                    ) : (
                      <small className="expert-event-score">公布后按已到达证据评分</small>
                    )}
                  </div>
                </div>
              ) : null}
            </>
          ) : null}

          {intelligenceTab === "options" ? (
            <div className="expert-options-panel">
              <div className={`expert-capability-state is-${displayedOptionsStatus?.state ?? "unavailable"}`}>
                <div>
                  <span className="expert-option-status-dot" />
                  <strong>{replayActive
                    ? "回放派生域已隔离"
                    : displayedOptionsStatus?.state === "live"
                      ? "期权实时链已连接"
                      : displayedOptionsStatus?.state === "delayed"
                        ? "上期所官方延时链已连接"
                        : displayedOptionsStatus === null && displayedOptionsError === null
                          ? "正在读取官方期权链"
                          : "期权行情暂不可用"}</strong>
                  <em>{replayActive
                    ? "REPLAY ISOLATED"
                    : displayedOptionsStatus?.delivery_mode === "exchange_delayed"
                      ? "EXCHANGE DELAYED"
                      : optionMarketStateLabel(displayedOptionsStatus?.state ?? "unavailable")}</em>
                </div>
                <span>{replayActive
                  ? REPLAY_DERIVED_DOMAIN_NOTICE
                  : displayedOptionsError ?? displayedOptionsStatus?.detail ?? "正在检测沪金与国际金期权接入能力"}</span>
                {displayedOptionsStatus?.observed_at ? (
                  <small>{formatOptionQuantity(displayedOptionsStatus.quote_count)} 合约 · 截至 {formatOptionObservedAt(displayedOptionsStatus.observed_at, displayTimeZone)}</small>
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
                      {(displayedOptionsStatus?.expiries ?? []).map((expiry) => (
                        <option key={expertOptionExpiryKey(expiry)} value={expertOptionExpiryKey(expiry)}>
                          {expiry.underlying_contract_id.toUpperCase()} · {expiry.expiry}
                        </option>
                      ))}
                    </select>
                  </label>

                  <div className="expert-option-metrics">
                    <span><small>标的期货</small><strong>{formatOptionMetric(selectedOptionExpiry.underlying_price)}</strong><em>{optionPriceUnitLabel(displayedOptionsStatus?.price_unit)}</em></span>
                    <span><small>P/C · 持仓</small><strong>{formatOptionMetric(selectedOptionExpiry.put_call_open_interest_ratio)}</strong><em>{optionPositioningLabel(selectedOptionExpiry.positioning_state)}</em></span>
                    <span><small>P/C · 成交</small><strong>{formatOptionMetric(selectedOptionExpiry.put_call_volume_ratio)}</strong><em>真实成交量</em></span>
                    <span><small>参考 IV</small><strong>{selectedOptionExpiry.reference_iv === null ? "—" : `${(selectedOptionExpiry.reference_iv * 100).toFixed(1)}%`}</strong><em>{displayedOptionsStatus?.reference_data_as_of ?? "无参考日"}</em></span>
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
                <p>{replayActive
                  ? REPLAY_DERIVED_DOMAIN_NOTICE
                  : "尚未取得可展示的真实合约。系统不会生成假 Put/Call、Greeks 或 GEX。"}</p>
              )}

              {(displayedOptionsStatus?.limitations.length ?? 0) > 0 ? (
                <details className="expert-option-limitations">
                  <summary>数据边界与使用约束</summary>
                  <ul>{displayedOptionsStatus?.limitations.map((item) => <li key={item}>{item}</li>)}</ul>
                </details>
              ) : null}

              <div className="expert-option-sources">
                {(displayedOptionsStatus?.markets ?? []).map((source) => (
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
                <div>
                  <strong>{replayActive
                    ? "回放未接入历史 AI 分析"
                    : aiReady ? "本机 ChatGPT 已连接" : "等待本机 GPT"}</strong>
                  <span>{replayActive
                    ? REPLAY_DERIVED_DOMAIN_NOTICE
                    : displayedAiStatus?.detail ?? "正在检测 Codex 账户状态"}</span>
                </div>
              </div>
              <button type="button" className="expert-ai-run" disabled={replayActive || !aiReady || aiBusy || candles.length === 0} onClick={() => void requestAiAnalysis()}>
                {!replayActive && aiBusy ? <RotateCcw className="spin" size={15} /> : <Sparkles size={15} />}
                {replayActive ? "回放隔离中" : aiBusy ? "分析行情中" : "用当前账户分析"}
              </button>
              <small className="expert-ai-quota-note">{replayActive
                ? "不会把当前实时状态发送给 AI，也不会用当前 AI 结论解释历史回放。"
                : "只读临时会话；发送来源、截止时间、最近 Bar 与已启用策略，会消耗本机 Codex/ChatGPT 配额。"}</small>
              {displayedAiError ? <div className="expert-ai-error">{displayedAiError}</div> : null}
              {displayedAiAnalysis ? (
                <div className="expert-ai-answer">
                  <header><strong>GPT 行情研判</strong><span>{displayedAiAnalysis.data_as_of ?? displayedAiAnalysis.generated_at}</span></header>
                  <p>{displayedAiAnalysis.analysis ?? displayedAiAnalysis.detail}</p>
                  <small>{displayedAiAnalysis.source_id} · {displayedAiAnalysis.bar_count} 根 Bar</small>
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      </aside>

      <StrategyDetailDrawer
        strategy={selectedStrategy}
        onClose={() => setSelectedStrategyId(null)}
      />

      <footer className="expert-replay-deck">
        <button
          type="button"
          className={replayActive ? "is-active" : ""}
          disabled={!replaySupported || replayBounds?.state !== "ready"}
          onClick={() => replayState === "playing" ? stopReplay() : void startReplay()}
          title={!replaySupported
            ? "当前行情源尚未接入原始帧回放"
            : replayError ?? "ReplayOriginal 1× 原速；每次从留存首帧空状态重建，不注入当前行情"}
        >
          {replayState === "playing" ? <Square size={12} /> : <Play size={13} />}
          {replayState === "playing" ? "停止回放" : replayActive ? "从头重放" : "行情回放"}
        </button>
        <button
          type="button"
          disabled={!replayActive}
          onClick={returnToLive}
          title="关闭隔离回放并回到实时行情"
          aria-label="回到实时行情"
        >
          <Radio size={13} />
        </button>
        <input
          type="range"
          aria-label="真实行情回放进度"
          min={replayBounds?.first_sequence ?? 0}
          max={replayBounds?.last_sequence ?? 0}
          step={1}
          value={replayCursor ?? replayBounds?.first_sequence ?? 0}
          disabled
        />
        <span
          className={`expert-replay-time ${replayBounds?.state === "unavailable" ? "is-error" : ""}`}
          title={replayError ?? replayWarning ?? undefined}
        >
          {replayBounds?.state === "unavailable" || replayBounds?.state === "empty"
            ? replayBounds.detail
            : replayCursorFrame?.sequence === replayCursor
              ? formatReplayTimecode(replayCursorFrame.received_at)
              : "正在定位精确帧时间…"}
          {replayWarning ? <i aria-label={replayWarning}>!</i> : null}
        </span>
        <small className="expert-replay-rate" title="时间间隔由 NATS JetStream ReplayOriginal 保持">
          {REPLAY_RATE_LABEL}
        </small>
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
