import {
  Activity,
  Bell,
  CandlestickChart,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Gauge,
  Maximize2,
  Newspaper,
  PanelLeftClose,
  Pin,
  PinOff,
  RefreshCw,
  Search,
  Sparkles,
  Waypoints,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { marketApi, mergeCandleRows } from "./api";
import { appendTimelineSample, buildChartBars } from "./chartModel";
import { chartPeriodById, type ChartPeriodId } from "./chartPeriods";
import { MarketChart } from "./MarketChart";
import { PeriodToolbar } from "./PeriodToolbar";
import { SourceDrawer, type SourceTestFeedback } from "./SourceDrawer";
import { SourcePicker } from "./SourcePicker";
import type {
  Candle,
  HoverCandle,
  InstrumentEntry,
  QuoteSnapshot,
  QuoteStreamEvent,
  SourceDescriptor,
  SourceId,
  TimelineSample,
} from "./types";

const defaultInstruments: InstrumentEntry[] = [
  {
    provider: "canonical",
    provider_code: "XAUUSD",
    name: "现货黄金",
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
  },
  {
    provider: "canonical",
    provider_code: "XAGUSD",
    name: "现货白银",
    instrument: { symbol: "XAG/USD", asset_class: "spot", base: "XAG", quote: "USD", venue: "OTC" },
  },
];

const sourceLabels: Record<SourceId, string> = {
  jin10_mcp: "金十官方 MCP",
  jin10_local: "金十本地行情",
  jin10_web: "金十极速行情",
};

const errorTranslations: Array<[RegExp, string]> = [
  [/local structured realtime capture channel/i, "本地结构化实时通道尚未接通"],
  [/本地结构化实时采集通道尚未接通/i, "本地结构化实时通道尚未接通"],
  [/JIN10_MCP_BEARER_TOKEN is required/i, "官方 MCP Token 尚未配置"],
];

function translateError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  return errorTranslations.find(([pattern]) => pattern.test(message))?.[1] ?? message;
}

function numeric(value: number | string | null | undefined): number | null {
  if (value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function digitsFor(code: string): number {
  return code === "XAGUSD" ? 3 : 2;
}

function formatPrice(value: number | string | null | undefined, code: string): string {
  const parsed = numeric(value);
  return parsed === null ? "—" : parsed.toFixed(digitsFor(code));
}

function formatSigned(value: number | string | null | undefined, digits = 2): string {
  const parsed = numeric(value);
  if (parsed === null) return "—";
  return `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}`;
}

function trendClass(quote: QuoteSnapshot | null): string {
  const change = numeric(quote?.change);
  if (change === null) return "trend-neutral";
  return change >= 0 ? "trend-up" : "trend-down";
}

function LiveClock() {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, []);

  const current = new Date(now);
  const pad = (value: number) => value.toString().padStart(2, "0");
  return `${pad(current.getMonth() + 1)}/${pad(current.getDate())} ${pad(current.getHours())}:${pad(current.getMinutes())}:${pad(current.getSeconds())}`;
}

export default function App() {
  const [instruments, setInstruments] = useState(defaultInstruments);
  const [selectedCode, setSelectedCode] = useState("XAUUSD");
  const [instrumentSources, setInstrumentSources] = useState<Record<string, SourceId>>({});
  const [instrumentSourcesLoaded, setInstrumentSourcesLoaded] = useState(false);
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, QuoteSnapshot>>({});
  const [candles, setCandles] = useState<Candle[]>([]);
  const [timelineSamples, setTimelineSamples] = useState<TimelineSample[]>([]);
  const [sources, setSources] = useState<SourceDescriptor[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [watchOpen, setWatchOpen] = useState(true);
  const [watchPinned, setWatchPinned] = useState(true);
  const [periodId, setPeriodId] = useState<ChartPeriodId>("1m");
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [testMessage, setTestMessage] = useState<string | null>(null);
  const [testingSourceId, setTestingSourceId] = useState<SourceId | null>(null);
  const [sourceTestResults, setSourceTestResults] = useState<
    Partial<Record<SourceId, SourceTestFeedback>>
  >({});
  const [loadingQuote, setLoadingQuote] = useState(true);
  const [loadingCandles, setLoadingCandles] = useState(true);
  const [quoteStreamState, setQuoteStreamState] = useState<"connecting" | "live" | "unavailable">("connecting");
  const candleRequestRef = useRef(0);

  const selectedInstrument =
    instruments.find((instrument) => instrument.provider_code === selectedCode) ?? defaultInstruments[0];
  const selectedSource = instrumentSources[selectedCode] ?? "jin10_local";
  const selectedPeriod = chartPeriodById(periodId);
  const sourceById = useMemo(
    () => new Map(sources.map((source) => [source.source_id, source])),
    [sources],
  );
  const selectedSourceDescriptor = sourceById.get(selectedSource);
  const selectedSourceReady = Boolean(
    sourcesLoaded
    && selectedSourceDescriptor
    && (!selectedSourceDescriptor.manual_connection_required || selectedSourceDescriptor.connection_active),
  );

  const loadSources = useCallback(async () => {
    setSourceBusy(true);
    try {
      setSources(await marketApi.sources());
      setSourcesLoaded(true);
    } catch (error) {
      setTestMessage(`来源检测失败：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  }, []);

  const refreshSourceSnapshot = useCallback(async () => {
    try {
      setSources(await marketApi.sources(false));
      setSourcesLoaded(true);
    } catch {
      // Keep the last known source state when a lightweight status refresh fails.
    }
  }, []);

  const loadCandles = useCallback(async () => {
    const requestId = ++candleRequestRef.current;
    setLoadingCandles(true);
    setCandles([]);
    try {
      const recent = await marketApi.candles(selectedCode);
      if (requestId !== candleRequestRef.current) return;
      setCandles(recent);
      setCandleError(null);
      if (recent.length < 100) {
        void (async () => {
          for (const delay of [150, 500, 1_500]) {
            await new Promise((resolve) => window.setTimeout(resolve, delay));
            if (requestId !== candleRequestRef.current) return;
            try {
              const localRows = await marketApi.candles(selectedCode);
              if (requestId !== candleRequestRef.current) return;
              setCandles((current) => mergeCandleRows(current, localRows));
              if (localRows.length >= 100) return;
            } catch {
              // A failed background fill never blocks local history or the live stream.
            }
          }
        })();
      }
    } catch (error) {
      if (requestId !== candleRequestRef.current) return;
      setCandles([]);
      setCandleError(translateError(error));
    } finally {
      if (requestId === candleRequestRef.current) setLoadingCandles(false);
    }
  }, [selectedCode]);

  const refreshCandles = useCallback(async () => {
    try {
      const recent = await marketApi.candles(selectedCode);
      setCandles((current) => mergeCandleRows(current, recent));
      setCandleError(null);
    } catch (error) {
      setCandleError(translateError(error));
    }
  }, [selectedCode]);

  useEffect(() => {
    void marketApi
      .instruments()
      .then((values) => setInstruments(values.length > 0 ? values : defaultInstruments))
      .catch(() => setInstruments(defaultInstruments));
    void loadSources();
  }, [loadSources]);

  useEffect(() => {
    let disposed = false;
    setInstrumentSourcesLoaded(false);
    void Promise.all(
      instruments.map(async (instrument) => {
        try {
          const value = await marketApi.instrumentSource(instrument.provider_code);
          return [instrument.provider_code, value.source_id] as const;
        } catch {
          return [instrument.provider_code, "jin10_local" as SourceId] as const;
        }
      }),
    ).then((values) => {
      if (disposed) return;
      setInstrumentSources(Object.fromEntries(values));
      setWatchQuotes({});
      setInstrumentSourcesLoaded(true);
    });
    return () => {
      disposed = true;
    };
  }, [instruments]);

  useEffect(() => {
    void loadCandles();
  }, [loadCandles]);

  useEffect(() => {
    if (!instrumentSourcesLoaded || !sourcesLoaded) return;
    let disposed = false;
    let socket: WebSocket | null = null;
    let retryTimer: number | null = null;
    let retryCount = 0;

    setLoadingQuote(true);
    setQuote(null);
    setQuoteError(null);
    setQuoteStreamState("connecting");

    if (!selectedSourceReady) {
      setQuoteStreamState("unavailable");
      setQuoteError(`${selectedSourceDescriptor?.display_name ?? "当前行情源"}待连接，请在行情源管理中点击连接并测试`);
      setLoadingQuote(false);
      return;
    }

    const connect = () => {
      if (disposed) return;
      socket = marketApi.openQuoteStream(selectedCode, selectedSource);
      socket.onopen = () => {
        retryCount = 0;
        setQuoteStreamState("connecting");
      };
      socket.onmessage = (message) => {
        if (disposed) return;
        const event = JSON.parse(String(message.data)) as QuoteStreamEvent;
        setQuoteStreamState(event.state);
        if (event.kind === "quote" && event.quote) {
          setQuote(event.quote);
          setWatchQuotes((current) => ({ ...current, [selectedCode]: event.quote as QuoteSnapshot }));
          const sampleValue = numeric(event.quote.last);
          const observedTime = new Date(event.quote.source.observed_at).getTime() / 1_000;
          const receivedTime = new Date(event.quote.source.received_at).getTime() / 1_000;
          if (
            sampleValue !== null
            && Number.isFinite(observedTime)
            && Number.isFinite(receivedTime)
          ) {
            setTimelineSamples((current) => appendTimelineSample(
              current,
              {
                time: receivedTime,
                observedTime,
                value: sampleValue,
                eventId: [
                  event.quote?.source.provider,
                  event.quote?.source.provider_symbol,
                  event.quote?.source.observed_at,
                  event.quote?.source.received_at,
                  String(event.quote?.last),
                ].join("|"),
              },
            ));
          }
          setQuoteError(null);
          setLoadingQuote(false);
        } else if (event.state === "unavailable") {
          setQuoteError(translateError(event.error ?? "当前行情源不可用"));
          setLoadingQuote(false);
        }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (disposed) return;
        setQuoteStreamState("unavailable");
        setQuoteError("实时报价连接已断开，正在重连");
        setLoadingQuote(false);
        const delay = Math.min(1_000, 100 * 2 ** retryCount);
        retryCount += 1;
        retryTimer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      disposed = true;
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [instrumentSourcesLoaded, selectedCode, selectedSource, selectedSourceDescriptor?.display_name, selectedSourceReady, sourcesLoaded]);

  useEffect(() => {
    setTimelineSamples([]);
  }, [selectedCode, selectedSource]);

  useEffect(() => {
    let disposed = false;
    let timer: number | null = null;
    const scheduleNextMinute = () => {
      const delay = 60_000 - (Date.now() % 60_000) + 25;
      timer = window.setTimeout(async () => {
        if (disposed) return;
        await refreshCandles();
        if (!disposed) scheduleNextMinute();
      }, delay);
    };
    scheduleNextMinute();
    return () => {
      disposed = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [refreshCandles]);

  useEffect(() => {
    if (!drawerOpen) return;
    void refreshSourceSnapshot();
    const timer = window.setInterval(() => void refreshSourceSnapshot(), 5_000);
    return () => window.clearInterval(timer);
  }, [drawerOpen, refreshSourceSnapshot]);

  useEffect(() => {
    const hasActiveLimitedSource = sources.some(
      (source) => source.access_model !== "unmetered" && source.connection_active,
    );
    if (!hasActiveLimitedSource) return;
    const timer = window.setInterval(() => void refreshSourceSnapshot(), 15_000);
    return () => window.clearInterval(timer);
  }, [refreshSourceSnapshot, sources]);

  useEffect(() => {
    if (!instrumentSourcesLoaded || !sourcesLoaded) return;
    const missing = instruments.filter(
      (item) => item.provider_code !== selectedCode && !watchQuotes[item.provider_code],
    );
    if (missing.length === 0) return;
    void Promise.allSettled(
      missing.map(async (item) => {
        const source = instrumentSources[item.provider_code] ?? "jin10_local";
        const descriptor = sourceById.get(source);
        if (!descriptor || (descriptor.manual_connection_required && !descriptor.connection_active)) return;
        const value = await marketApi.quote(item.provider_code, source);
        setWatchQuotes((current) => ({ ...current, [item.provider_code]: value }));
      }),
    );
  }, [instrumentSources, instrumentSourcesLoaded, instruments, selectedCode, sourceById, sourcesLoaded, watchQuotes]);

  const updateSource = async (
    source: SourceDescriptor,
    update: { enabled?: boolean; priority?: number },
  ) => {
    setSourceBusy(true);
    try {
      await marketApi.updateSource(source.source_id, update);
      await loadSources();
    } catch (error) {
      setTestMessage(`更新失败：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  };

  const preferSource = async (source: SourceDescriptor) => {
    setSourceBusy(true);
    try {
      const value = await marketApi.updateInstrumentSource(selectedCode, source.source_id);
      setInstrumentSources((current) => ({ ...current, [selectedCode]: value.source_id }));
      setWatchQuotes((current) => {
        const next = { ...current };
        delete next[selectedCode];
        return next;
      });
      setTestMessage(`${selectedCode} 的实时行情已切换为 ${source.display_name}`);
    } catch (error) {
      setTestMessage(`合约来源更新失败：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  };

  const testSource = async (source: SourceDescriptor) => {
    setTestingSourceId(source.source_id);
    setSourceTestResults((current) => {
      const next = { ...current };
      delete next[source.source_id];
      return next;
    });
    try {
      const value = await marketApi.testSource(source.source_id);
      const observedAt = new Date(value.observed_at).toLocaleTimeString("zh-CN", { hour12: false });
      setSourceTestResults((current) => ({
        ...current,
        [source.source_id]: {
          tone: "success",
          message: `连接成功 · 黄金 ${formatPrice(value.last, "XAUUSD")} · ${value.latency_ms}ms · ${observedAt}`,
        },
      }));
    } catch (error) {
      setSourceTestResults((current) => ({
        ...current,
        [source.source_id]: { tone: "error", message: translateError(error) },
      }));
    } finally {
      await refreshSourceSnapshot();
      setTestingSourceId(null);
    }
  };

  const livePrice = numeric(quote?.last);
  const quoteObservedAt = quote?.source.observed_at ?? null;
  const chartBars = useMemo(
    () => buildChartBars(
      candles,
      selectedPeriod,
      timelineSamples,
      livePrice,
      quoteObservedAt,
    ),
    [candles, livePrice, quoteObservedAt, selectedPeriod, timelineSamples],
  );
  const displayBar = hover ?? chartBars.at(-1) ?? null;
  const timelineReferencePrice = useMemo(() => {
    const last = numeric(quote?.last);
    const change = numeric(quote?.change);
    if (last !== null && change !== null) return last - change;
    return chartBars[0]?.open ?? null;
  }, [chartBars, quote?.change, quote?.last]);
  const timelineChange = displayBar && timelineReferencePrice !== null
    ? displayBar.close - timelineReferencePrice
    : null;
  const timelinePercent = timelineChange !== null && timelineReferencePrice
    ? (timelineChange / timelineReferencePrice) * 100
    : null;
  const timelineTrend = timelineChange === null
    ? "trend-neutral"
    : timelineChange >= 0
      ? "trend-up"
      : "trend-down";
  const quoteStreaming = selectedSourceDescriptor?.quote_streaming ?? false;
  const timelineSamplingSeconds = quoteStreaming
    ? 1
    : selectedSourceDescriptor?.quote_poll_interval_seconds ?? 60;
  const timelineResolutionLabel = quoteStreaming
    ? "推送"
    : timelineSamplingSeconds <= 2
      ? "秒级"
      : timelineSamplingSeconds < 60
        ? `${Math.round(timelineSamplingSeconds)}秒`
        : "分钟级";

  const quoteTrend = trendClass(quote);
  const quoteProvider = quote?.source.provider as SourceId | undefined;

  return (
    <div className={`terminal-shell ${watchOpen && watchPinned ? "is-watch-docked" : ""}`}>
      <header className="top-command-bar">
        <div className="top-brand" title="Market Pulse">
          <div className="top-brand-mark"><span>M</span></div>
          <div>
            <small>MARKET WATCH</small>
            <strong>行情</strong>
          </div>
        </div>
        <nav className="top-tool-icons" aria-label="主功能">
          <button type="button" title="市场概览"><Gauge size={18} /></button>
          <button type="button" title="策略观察"><Waypoints size={18} /></button>
          <button type="button" className="is-active" title="行情图表"><CandlestickChart size={18} /></button>
          <button type="button" title="资讯"><Newspaper size={18} /></button>
        </nav>
        <div className="content-tabs">
          <button type="button" className="is-active">图表</button>
          <button type="button">快讯</button>
          <button type="button">头条</button>
          <button type="button">研报</button>
          <button type="button">指标库</button>
          <button type="button" className="accent"><Sparkles size={14} />分析器</button>
        </div>
        <div className="utility-cluster">
          <div className="search-box"><Search size={16} /><span>搜索品种、资讯或指标</span></div>
          <div className="utility-actions">
            <button type="button" title="提醒"><Bell size={17} /></button>
            <button type="button" title="全屏"><Maximize2 size={17} /></button>
            <button type="button" title="帮助"><CircleHelp size={17} /></button>
          </div>
          <div className="avatar" title="当前用户">D</div>
        </div>
      </header>

      <aside
        className={`watch-panel ${watchOpen ? "is-open" : "is-collapsed"} ${watchPinned ? "is-pinned" : "is-floating"}`}
        aria-label="行情列表"
        aria-hidden={!watchOpen}
        inert={!watchOpen}
        onPointerLeave={() => {
          if (!watchPinned) setWatchOpen(false);
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape" && !watchPinned) {
            setWatchOpen(false);
          }
        }}
      >
        <div className="market-tabs">
          <div className="market-tab-list">
            <button type="button">自选</button>
            <button type="button">外汇</button>
            <button type="button" className="is-active">贵金属</button>
            <button type="button">能源</button>
          </div>
          <div className="watch-panel-actions">
            <button
              type="button"
              className={watchPinned ? "is-active" : ""}
              title={watchPinned ? "取消固定，改为悬浮侧栏" : "固定侧栏"}
              aria-pressed={watchPinned}
              onClick={() => {
                if (watchPinned) {
                  setWatchPinned(false);
                  setWatchOpen(false);
                } else {
                  setWatchPinned(true);
                  setWatchOpen(true);
                }
              }}
            >
              {watchPinned ? <Pin size={14} /> : <PinOff size={14} />}
            </button>
            <button type="button" title="收起行情列表" onClick={() => setWatchOpen(false)}>
              <PanelLeftClose size={15} />
            </button>
          </div>
        </div>

        <div className="watch-section-title">
          <div><ChevronDown size={15} /><strong>贵金属</strong></div>
          <span>实时观察</span>
        </div>
        <div className="watch-list-head" aria-hidden="true">
          <span>名称 / 代码</span>
          <span>现价 / 涨跌</span>
        </div>
        <div className="instrument-list">
          {instruments.map((item) => {
            const itemQuote = watchQuotes[item.provider_code];
            const direction = trendClass(itemQuote ?? null);
            return (
              <button
                type="button"
                key={item.provider_code}
                className={`instrument-row ${selectedCode === item.provider_code ? "is-selected" : ""}`}
                onClick={() => setSelectedCode(item.provider_code)}
              >
                <div>
                  <strong>{item.name}</strong>
                  <span>{item.provider_code}</span>
                </div>
                <div className={direction}>
                  <strong>{formatPrice(itemQuote?.last, item.provider_code)}</strong>
                  <span>{formatSigned(itemQuote?.change_percent)}%</span>
                </div>
              </button>
            );
          })}
        </div>

      </aside>

      {!watchOpen ? (
        <button
          type="button"
          className={`watch-reveal-button ${watchPinned ? "is-docked" : "is-floating"}`}
          title={watchPinned ? "展开并固定行情列表" : "展开悬浮行情列表"}
          aria-label="展开行情列表"
          aria-expanded={watchOpen}
          onPointerEnter={() => {
            if (!watchPinned) setWatchOpen(true);
          }}
          onClick={() => setWatchOpen(true)}
        >
          <ChevronRight size={14} />
        </button>
      ) : null}

      <main className="market-main">
        <div className="chart-toolbar">
          <div className="indicator-button"><span>指标</span><ChevronDown size={14} /></div>
          <PeriodToolbar
            selectedId={periodId}
            onSelect={(id) => {
              setHover(null);
              setPeriodId(id);
            }}
          />
          {selectedPeriod.mode === "timeline" ? (
            <span
              className="timeline-resolution-badge"
              title={quoteStreaming
                ? "当前报价源在行情帧到达时立即推送；历史段由分钟 K 线补齐"
                : `当前报价源约每 ${timelineSamplingSeconds} 秒请求一次；历史段由分钟 K 线补齐`}
            >
              {timelineResolutionLabel}
            </span>
          ) : null}
          <SourcePicker
            sources={sources}
            selectedSource={selectedSource}
            fallbackLabel={sourceLabels[selectedSource]}
            busy={sourceBusy}
            connectionState={quoteStreamState}
            connectionError={quoteError}
            quoteObservedAt={quoteProvider === selectedSource ? quote?.source.observed_at ?? null : null}
            quoteReceivedAt={quoteProvider === selectedSource ? quote?.source.received_at ?? null : null}
            onSelect={preferSource}
            onManage={() => setDrawerOpen(true)}
          />
          <button type="button" className="draw-button"><Activity size={15} />画线</button>
        </div>

        <section className="chart-area">
          <div className="chart-context-overlay">
            <div className="chart-context-primary">
              <div className="chart-symbol">
                <strong>{selectedInstrument.name}</strong>
                <span>{selectedCode}</span>
                <ChevronDown size={13} />
                <span className="session-badge">交易中</span>
                <small><LiveClock /></small>
              </div>
              <div className={`hero-price chart-hero-price ${quoteTrend}`}>
                <strong>{loadingQuote && !quote ? "读取中" : formatPrice(quote?.last, selectedCode)}</strong>
                {numeric(quote?.change) === null ? null : (
                  <span>{(numeric(quote?.change) ?? 0) >= 0 ? "↑" : "↓"}</span>
                )}
                <small>{formatSigned(quote?.change_percent)}%</small>
                <small>{formatSigned(quote?.change, digitsFor(selectedCode))}</small>
              </div>
            </div>
            <div className="chart-context-secondary">
              <dl className="chart-daily-stats">
                <div><dt>最高</dt><dd className="trend-up">{formatPrice(quote?.high, selectedCode)}</dd></div>
                <div><dt>最低</dt><dd className="trend-down">{formatPrice(quote?.low, selectedCode)}</dd></div>
                <div><dt>今开</dt><dd>{formatPrice(quote?.open, selectedCode)}</dd></div>
              </dl>
              <div className="chart-freshness">
                <span className={`status-dot ${quoteError ? "is-error" : ""}`} />
                <span>
                  {quoteError
                    ? quoteError
                    : `${sourceLabels[quoteProvider ?? selectedSource]} · ${quote ? new Date(quote.source.observed_at).toLocaleTimeString("zh-CN", { hour12: false }) : "等待数据"}`}
                </span>
              </div>
            </div>
          </div>
          {displayBar ? (
            <div className={`ohlc-overlay ${selectedPeriod.mode === "timeline" ? "is-timeline" : ""}`}>
              <span>{new Date(displayBar.time * 1000).toLocaleString("zh-CN", { hour12: false })}</span>
              {selectedPeriod.mode === "timeline" ? (
                <>
                  <span>价格 <b>{formatPrice(displayBar.close, selectedCode)}</b></span>
                  <span>涨跌 <b className={timelineTrend}>{formatSigned(timelineChange, digitsFor(selectedCode))}</b></span>
                  <span>涨幅 <b className={timelineTrend}>{formatSigned(timelinePercent)}%</b></span>
                </>
              ) : (
                <>
                  <span>开 <b>{formatPrice(displayBar.open, selectedCode)}</b></span>
                  <span>高 <b className="trend-up">{formatPrice(displayBar.high, selectedCode)}</b></span>
                  <span>低 <b className="trend-down">{formatPrice(displayBar.low, selectedCode)}</b></span>
                  <span>收 <b>{formatPrice(displayBar.close, selectedCode)}</b></span>
                </>
              )}
            </div>
          ) : null}
          <MarketChart
            candles={candles}
            period={selectedPeriod}
            timelineSamples={timelineSamples}
            livePrice={livePrice}
            observedAt={quoteObservedAt}
            referencePrice={timelineReferencePrice}
            timelineResolutionSeconds={timelineSamplingSeconds}
            priceDigits={digitsFor(selectedCode)}
            onHover={setHover}
          />
          {loadingCandles && candles.length === 0 ? <div className="chart-state"><RefreshCw size={20} className="spin" /><strong>正在读取 K 线</strong></div> : null}
          {candleError ? <div className="chart-state is-error"><CircleHelp size={22} /><strong>K 线暂不可用</strong><span>{candleError}</span><button type="button" onClick={() => void loadCandles()}>重试</button></div> : null}
        </section>
      </main>

      <SourceDrawer
        open={drawerOpen}
        sources={sources}
        busy={sourceBusy}
        notice={testMessage}
        contractCode={selectedCode}
        selectedSource={selectedSource}
        testingSourceId={testingSourceId}
        testResults={sourceTestResults}
        onClose={() => {
          setDrawerOpen(false);
          setTestMessage(null);
          setSourceTestResults({});
        }}
        onToggle={(source) => void updateSource(source, { enabled: !source.enabled })}
        onPrefer={(source) => void preferSource(source)}
        onTest={(source) => void testSource(source)}
      />
    </div>
  );
}
