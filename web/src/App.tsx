import {
  Activity,
  Bell,
  CandlestickChart,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Gauge,
  ListPlus,
  LoaderCircle,
  Maximize2,
  Newspaper,
  PanelLeftClose,
  Pin,
  PinOff,
  RefreshCw,
  Search,
  Sparkles,
  Waypoints,
  X,
} from "lucide-react";
import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { marketApi, mergeCandleRows } from "./api";
import { appendTimelineSample, barsFromCandles, mergeTimelineSamples } from "./chartModel";
import { chartPeriodById, type ChartPeriodId } from "./chartPeriods";
import { formatBeijingClock, formatChartTimeLabel } from "./chartTimeAxis";
import { historyBatchMinutes } from "./historyLoading";
import { marketSessionAt, SPOT_METALS_MARKET_SCHEDULE } from "./marketSession";
import { ExpertModeWorkspace } from "./ExpertModeWorkspace";
import { MarketChart } from "./MarketChart";
import { PeriodToolbar } from "./PeriodToolbar";
import { SourcePicker, type SourceTestFeedback } from "./SourcePicker";
import { startWatchlistQuoteStream, watchlistQuoteStreamTargets } from "./watchlistStreams";
import type {
  Candle,
  HoverCandle,
  InstrumentEntry,
  QuoteSnapshot,
  QuoteStreamEvent,
  QuoteView,
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
    price_unit: "美元/盎司",
    price_digits: 2,
    quote_kind: "direct",
    history_available: true,
    source_ids: ["jin10_client"],
    dependencies: [],
    market_schedule: SPOT_METALS_MARKET_SCHEDULE,
  },
  {
    provider: "canonical",
    provider_code: "XAGUSD",
    name: "现货白银",
    instrument: { symbol: "XAG/USD", asset_class: "spot", base: "XAG", quote: "USD", venue: "OTC" },
    price_unit: "美元/盎司",
    price_digits: 3,
    quote_kind: "direct",
    history_available: true,
    source_ids: ["jin10_client"],
    dependencies: [],
    market_schedule: SPOT_METALS_MARKET_SCHEDULE,
  },
];

const sourceLabels: Record<SourceId, string> = {
  jin10_client: "金十客户端行情",
  tonghuashun_futures: "同花顺公开行情",
};

const errorTranslations: Array<[RegExp, string]> = [
  [/internal channel.*not a selectable realtime source/i, "该通道不是可绑定的实时数据源"],
  [/not a selectable realtime source/i, "该来源不是可绑定的实时数据源"],
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

function digitsFor(code: string, instrument?: InstrumentEntry): number {
  if (instrument) return instrument.price_digits;
  if (code === "XAGUSD") return 3;
  if (code === "USDCNH") return 4;
  return 2;
}

function unitFor(code: string, instrument?: InstrumentEntry): string {
  if (instrument) return instrument.price_unit;
  if (code === "USDCNH") return "人民币/美元";
  if (code === "XAUCNHG") return "人民币/克";
  return code === "XAUUSD" || code === "XAGUSD" ? "美元/盎司" : "";
}

function formatPrice(
  value: number | string | null | undefined,
  code: string,
  instrument?: InstrumentEntry,
): string {
  const parsed = numeric(value);
  return parsed === null ? "—" : parsed.toFixed(digitsFor(code, instrument));
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

const WATCH_SPARKLINE_LIMIT = 48;
const WATCH_SPARKLINE_CANDLE_WINDOW = 480;

function downsamplePrices(values: number[], limit: number): number[] {
  if (values.length <= limit) return values;
  return Array.from({ length: limit }, (_, index) => (
    values[Math.round(index * (values.length - 1) / (limit - 1))]
  ));
}

function appendWatchPriceSample(
  current: Record<string, number[]>,
  code: string,
  value: QuoteView,
): Record<string, number[]> {
  const price = numeric(value.quote.last);
  if (price === null) return current;
  const previous = current[code] ?? [];
  return {
    ...current,
    [code]: [...previous.slice(-(WATCH_SPARKLINE_LIMIT - 1)), price],
  };
}

function WatchSparkline({
  code,
  quote,
  samples,
}: {
  code: string;
  quote: QuoteSnapshot | null;
  samples: number[];
}) {
  const width = 94;
  const height = 54;
  const padding = 3;
  const last = numeric(quote?.last);
  const change = numeric(quote?.change);
  const reference = last !== null && change !== null ? last - change : null;
  const observed = samples.length > 0 ? samples : last !== null ? [last] : [];
  const plotted = observed.length === 1 && reference !== null
    ? [reference, observed[0]]
    : observed;
  const domain = reference === null ? plotted : [...plotted, reference];
  const minimum = domain.length > 0 ? Math.min(...domain) : 0;
  const maximum = domain.length > 0 ? Math.max(...domain) : 1;
  const spread = maximum - minimum;
  const domainPadding = spread === 0 ? Math.max(Math.abs(maximum) * 0.0005, 0.01) : spread * 0.16;
  const floor = minimum - domainPadding;
  const ceiling = maximum + domainPadding;
  const xFor = (index: number) => padding + (plotted.length <= 1
    ? width - padding * 2
    : index * (width - padding * 2) / (plotted.length - 1));
  const yFor = (value: number) => padding + (ceiling - value) / (ceiling - floor) * (height - padding * 2);
  const linePath = plotted.map((value, index) => `${index === 0 ? "M" : "L"}${xFor(index).toFixed(2)},${yFor(value).toFixed(2)}`).join(" ");
  const areaPath = linePath
    ? `${linePath} L${xFor(plotted.length - 1).toFixed(2)},${height - padding} L${xFor(0).toFixed(2)},${height - padding} Z`
    : "";
  const gradientId = `watch-spark-${code.replace(/[^a-z0-9_-]/gi, "-")}`;

  return (
    <svg
      className={`watch-sparkline ${trendClass(quote)}`}
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${code}实时走势`}
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="currentColor" stopOpacity="0.16" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </linearGradient>
      </defs>
      <line
        x1={padding}
        x2={width - padding}
        y1={reference === null ? height / 2 : yFor(reference)}
        y2={reference === null ? height / 2 : yFor(reference)}
        className="watch-spark-reference"
      />
      {areaPath ? <path d={areaPath} fill={`url(#${gradientId})`} /> : null}
      {linePath ? <path d={linePath} className="watch-spark-line" /> : null}
    </svg>
  );
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
  const [catalog, setCatalog] = useState(defaultInstruments);
  const [instruments, setInstruments] = useState(defaultInstruments);
  const [selectedCode, setSelectedCode] = useState("XAUUSD");
  const selectedCodeRef = useRef(selectedCode);
  selectedCodeRef.current = selectedCode;
  const [instrumentSources, setInstrumentSources] = useState<Record<string, SourceId>>({});
  const [instrumentSourcesLoaded, setInstrumentSourcesLoaded] = useState(false);
  const [quote, setQuote] = useState<QuoteView | null>(null);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, QuoteView>>({});
  const [watchPriceSeries, setWatchPriceSeries] = useState<Record<string, number[]>>({});
  const [candles, setCandles] = useState<Candle[]>([]);
  const [timelineSamples, setTimelineSamples] = useState<TimelineSample[]>([]);
  const [sources, setSources] = useState<SourceDescriptor[]>([]);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [sourceMenuOpen, setSourceMenuOpen] = useState(false);
  const [watchOpen, setWatchOpen] = useState(true);
  const [watchPinned, setWatchPinned] = useState(true);
  const [watchManagerOpen, setWatchManagerOpen] = useState(false);
  const [watchSearch, setWatchSearch] = useState("");
  const [watchlistBusyCode, setWatchlistBusyCode] = useState<string | null>(null);
  const [watchlistError, setWatchlistError] = useState<string | null>(null);
  const [periodId, setPeriodId] = useState<ChartPeriodId>("1m");
  const [expertMode, setExpertMode] = useState(false);
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
  const [historySyncing, setHistorySyncing] = useState(false);
  const [timelineHistorySyncing, setTimelineHistorySyncing] = useState(false);
  const [quoteStreamState, setQuoteStreamState] = useState<"connecting" | "live" | "waiting" | "unavailable">("connecting");
  const candleRequestRef = useRef(0);
  const candlesRef = useRef<Candle[]>([]);
  const historyCursorRef = useRef<number | null>(null);
  const historyLoadInFlightRef = useRef(false);
  const barRefreshInFlightRef = useRef(false);
  const barRefreshRequestedRef = useRef(false);
  candlesRef.current = candles;
  const watchlistQuoteStreamsRef = useRef(
    new Map<string, { sourceId: SourceId; stop: () => void }>(),
  );

  const selectedInstrument =
    catalog.find((instrument) => instrument.provider_code === selectedCode)
    ?? instruments.find((instrument) => instrument.provider_code === selectedCode)
    ?? defaultInstruments[0];
  const selectedSource = instrumentSources[selectedCode] ?? "jin10_client";
  const selectedPeriod = chartPeriodById(periodId);
  const sourceById = useMemo(
    () => new Map(sources.map((source) => [source.source_id, source])),
    [sources],
  );
  const compatibleSources = useMemo(
    () => sources.filter((source) => (selectedInstrument.source_ids ?? ["jin10_client"]).includes(source.source_id)),
    [selectedInstrument, sources],
  );
  const selectedSourceDescriptor = sourceById.get(selectedSource);
  const selectedSourceReady = Boolean(
    sourcesLoaded
    && selectedSourceDescriptor
    && selectedSourceDescriptor.selectable
    && (!selectedSourceDescriptor.manual_connection_required || selectedSourceDescriptor.connection_active),
  );
  const [marketSession, setMarketSession] = useState(() => marketSessionAt(defaultInstruments[0].market_schedule));
  const marketPhaseRef = useRef(marketSession.phase);
  marketPhaseRef.current = marketSession.phase;

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
    historyCursorRef.current = null;
    historyLoadInFlightRef.current = false;
    setLoadingCandles(true);
    setHistorySyncing(false);
    setCandles([]);
    try {
      const recent = await marketApi.barPage(
        selectedCode,
        selectedSource,
        selectedPeriod.id,
      );
      if (requestId !== candleRequestRef.current) return;
      setCandles(recent.items);
      setCandleError(null);
      const firstCursor = recent.next_before
        ? Math.floor(Date.parse(recent.next_before) / 1_000)
        : null;
      if (firstCursor !== null && Number.isFinite(firstCursor)) {
        historyCursorRef.current = firstCursor;
      }
      if (!recent.has_more || firstCursor === null) return;
      void (async () => {
        historyLoadInFlightRef.current = true;
        setHistorySyncing(true);
        let page = recent;
        let cursor = firstCursor;
        const seen = new Set<number>();
        const historyPages: Candle[][] = [];
        try {
          while (page.has_more && requestId === candleRequestRef.current) {
            if (seen.has(cursor)) throw new Error("周期 Bar 游标未前进");
            seen.add(cursor);
            page = await marketApi.barPage(
              selectedCode,
              selectedSource,
              selectedPeriod.id,
              cursor,
              5_000,
            );
            if (requestId !== candleRequestRef.current) return;
            historyPages.push(page.items);
            if (page.next_before === null) break;
            const nextCursor = Math.floor(Date.parse(page.next_before) / 1_000);
            if (!Number.isFinite(nextCursor)) break;
            cursor = nextCursor;
            historyCursorRef.current = nextCursor;
          }
        } catch {
          // Keep the newest page usable; the left-edge gesture can resume from the last cursor.
        } finally {
          if (requestId === candleRequestRef.current) {
            if (historyPages.length > 0) {
              const completeHistory = mergeCandleRows(...historyPages, recent.items);
              startTransition(() => {
                setCandles((current) => mergeCandleRows(completeHistory, current));
              });
            }
            historyLoadInFlightRef.current = false;
            setHistorySyncing(false);
          }
        }
      })();
    } catch (error) {
      if (requestId !== candleRequestRef.current) return;
      setCandles([]);
      setCandleError(translateError(error));
    } finally {
      if (requestId === candleRequestRef.current) setLoadingCandles(false);
    }
  }, [selectedCode, selectedPeriod.id, selectedSource]);

  const loadOlderCandles = useCallback(async () => {
    if (!selectedInstrument.history_available || historyLoadInFlightRef.current) return;
    const earliest = candlesRef.current.at(0);
    const earliestSeconds = earliest
      ? Math.floor(new Date(earliest.open_time).getTime() / 1_000)
      : null;
    const before = historyCursorRef.current ?? earliestSeconds;
    if (before === null || !Number.isFinite(before)) return;

    const requestId = candleRequestRef.current;
    historyLoadInFlightRef.current = true;
    setHistorySyncing(true);
    try {
      const localPage = await marketApi.barPage(
        selectedCode,
        selectedSource,
        selectedPeriod.id,
        before,
      );
      if (requestId !== candleRequestRef.current) return;
      if (localPage.items.length > 0) {
        const localCursor = localPage.next_before
          ? Math.floor(Date.parse(localPage.next_before) / 1_000)
          : null;
        if (localCursor !== null && Number.isFinite(localCursor)) {
          historyCursorRef.current = localCursor;
        }
        setCandles((current) => mergeCandleRows(localPage.items, current));
        return;
      }
      const filled = await marketApi.olderCandleHistory(
        selectedCode,
        selectedSource,
        before,
        historyBatchMinutes(selectedPeriod),
      );
      if (requestId !== candleRequestRef.current) return;
      const page = await marketApi.barPage(
        selectedCode,
        selectedSource,
        selectedPeriod.id,
        before,
      );
      if (requestId !== candleRequestRef.current) return;
      const cursor = page.next_before
        ? Math.floor(Date.parse(page.next_before) / 1_000)
        : Math.floor(Date.parse(filled.result.start) / 1_000);
      if (Number.isFinite(cursor)) historyCursorRef.current = cursor;
      setCandles((current) => mergeCandleRows(page.items, current));
    } catch {
      // Keep the visible chart interactive; the next left-edge gesture can retry.
    } finally {
      if (requestId === candleRequestRef.current) {
        historyLoadInFlightRef.current = false;
        setHistorySyncing(false);
      }
    }
  }, [selectedCode, selectedInstrument.history_available, selectedPeriod, selectedSource]);

  const refreshCandles = useCallback(async () => {
    if (barRefreshInFlightRef.current) {
      barRefreshRequestedRef.current = true;
      return;
    }
    barRefreshInFlightRef.current = true;
    const requestId = candleRequestRef.current;
    try {
      do {
        barRefreshRequestedRef.current = false;
        try {
          const recent = await marketApi.barPage(
            selectedCode,
            selectedSource,
            selectedPeriod.id,
          );
          if (requestId !== candleRequestRef.current) return;
          setCandles((current) => mergeCandleRows(current, recent.items));
          setCandleError(null);
        } catch (error) {
          setCandleError(translateError(error));
        }
      } while (barRefreshRequestedRef.current);
    } finally {
      barRefreshInFlightRef.current = false;
    }
  }, [selectedCode, selectedPeriod.id, selectedSource]);

  useEffect(() => {
    void Promise.all([marketApi.instruments(), marketApi.watchlist()])
      .then(([available, observed]) => {
        const nextCatalog = available.length > 0 ? available : defaultInstruments;
        const nextWatchlist = observed.length > 0 ? observed : defaultInstruments;
        setCatalog(nextCatalog);
        setInstruments(nextWatchlist);
        setSelectedCode((current) =>
          nextWatchlist.some((item) => item.provider_code === current)
            ? current
            : nextWatchlist[0].provider_code,
        );
      })
      .catch(() => {
        setCatalog(defaultInstruments);
        setInstruments(defaultInstruments);
      });
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
          return [instrument.provider_code, "jin10_client" as SourceId] as const;
        }
      }),
    ).then((values) => {
      if (disposed) return;
      setInstrumentSources(Object.fromEntries(values));
      setWatchQuotes({});
      setWatchPriceSeries({});
      setInstrumentSourcesLoaded(true);
    });
    return () => {
      disposed = true;
    };
  }, [instruments]);

  useEffect(() => {
    const refresh = () => {
      const next = marketSessionAt(selectedInstrument.market_schedule);
      setMarketSession((current) => current.phase === next.phase ? current : next);
    };
    refresh();
    const timer = window.setInterval(refresh, 15_000);
    return () => window.clearInterval(timer);
  }, [selectedInstrument.market_schedule, selectedInstrument.provider_code]);

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
      setQuoteError(selectedSourceDescriptor && !selectedSourceDescriptor.selectable
        ? `${selectedSourceDescriptor.display_name}当前不可选择`
        : `${selectedSourceDescriptor?.display_name ?? "当前行情源"}待连接，请在实时数据源菜单中测试连接`);
      setLoadingQuote(false);
      return;
    }

    const connect = () => {
      if (disposed || selectedCodeRef.current !== selectedCode) return;
      socket = marketApi.openQuoteStream(selectedCode);
      socket.onopen = () => {
        if (disposed || selectedCodeRef.current !== selectedCode) return;
        retryCount = 0;
        const marketClosed = marketPhaseRef.current === "closed";
        setQuoteStreamState(marketClosed ? "waiting" : "connecting");
        if (marketClosed) setLoadingQuote(false);
      };
      socket.onmessage = (message) => {
        if (disposed || selectedCodeRef.current !== selectedCode) return;
        const event = JSON.parse(String(message.data)) as QuoteStreamEvent;
        const marketClosed = marketPhaseRef.current === "closed";
        if (event.kind === "bar") {
          setQuoteStreamState(marketClosed ? "waiting" : "live");
          void refreshCandles();
        } else if (event.kind === "sample" && event.sample) {
          const sampleValue = numeric(event.sample.value);
          const observedTime = Date.parse(event.sample.observed_at) / 1_000;
          const receivedTime = Date.parse(event.sample.received_at) / 1_000;
          if (
            sampleValue !== null
            && Number.isFinite(observedTime)
            && Number.isFinite(receivedTime)
          ) {
            setTimelineSamples((current) => appendTimelineSample(current, {
              time: receivedTime,
              observedTime,
              value: sampleValue,
              eventId: event.sample!.event_id,
            }));
          }
          setQuoteStreamState(marketClosed ? "waiting" : "live");
          if (selectedPeriod.mode !== "timeline") void refreshCandles();
        } else if (event.kind === "quote" && event.quote) {
          setQuoteStreamState(marketClosed ? "waiting" : "live");
          setQuote(event.quote);
          setWatchQuotes((current) => ({ ...current, [selectedCode]: event.quote as QuoteView }));
          setWatchPriceSeries((current) => appendWatchPriceSample(current, selectedCode, event.quote as QuoteView));
          setQuoteError(null);
          setLoadingQuote(false);
        } else if (event.state === "unavailable") {
          setQuoteStreamState(marketClosed ? "waiting" : "unavailable");
          setQuoteError(marketClosed ? null : translateError(event.error ?? "当前行情源不可用"));
          setLoadingQuote(false);
        } else {
          setQuoteStreamState(marketClosed ? "waiting" : event.state);
        }
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (disposed || selectedCodeRef.current !== selectedCode) return;
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
  }, [
    instrumentSourcesLoaded,
    refreshCandles,
    selectedCode,
    selectedPeriod.mode,
    selectedSource,
    selectedSourceDescriptor?.display_name,
    selectedSourceReady,
    sourcesLoaded,
  ]);

  useEffect(() => {
    const activeStreams = watchlistQuoteStreamsRef.current;
    const targets = !expertMode && instrumentSourcesLoaded && sourcesLoaded
      ? watchlistQuoteStreamTargets(instruments, selectedCode, instrumentSources, sourceById)
      : [];
    const targetByCode = new Map(targets.map((target) => [target.code, target]));

    for (const [code, stream] of activeStreams) {
      const target = targetByCode.get(code);
      if (target && target.sourceId === stream.sourceId) continue;
      stream.stop();
      activeStreams.delete(code);
    }
    for (const target of targets) {
      if (activeStreams.has(target.code)) continue;
      activeStreams.set(target.code, {
        sourceId: target.sourceId,
        stop: startWatchlistQuoteStream({
          code: target.code,
          openStream: marketApi.openQuoteStream,
          onQuote: (code, value) => {
            setWatchQuotes((current) => ({ ...current, [code]: value }));
            setWatchPriceSeries((current) => appendWatchPriceSample(current, code, value));
          },
        }),
      });
    }
  }, [expertMode, instrumentSources, instrumentSourcesLoaded, instruments, selectedCode, sourceById, sourcesLoaded]);

  useEffect(() => () => {
    const activeStreams = watchlistQuoteStreamsRef.current;
    for (const stream of activeStreams.values()) stream.stop();
    activeStreams.clear();
  }, []);

  useEffect(() => {
    if (
      marketSession.phase !== "closed"
      || !instrumentSourcesLoaded
      || !selectedSourceReady
    ) {
      return;
    }
    let disposed = false;
    void marketApi.lastQuote(selectedCode).then((lastView) => {
      if (
        disposed
        || selectedCodeRef.current !== selectedCode
        || lastView.source_id !== selectedSource
      ) return;
      setQuote((current) => {
        if (!current) return lastView;
        const currentTime = new Date(current.quote.source.received_at).getTime();
        const lastTime = new Date(lastView.quote.source.received_at).getTime();
        return currentTime >= lastTime ? current : lastView;
      });
      setWatchQuotes((current) => {
        const currentView = current[selectedCode];
        if (
          currentView
          && new Date(currentView.quote.source.received_at).getTime()
            >= new Date(lastView.quote.source.received_at).getTime()
        ) {
          return current;
        }
        return { ...current, [selectedCode]: lastView };
      });
      setWatchPriceSeries((current) => appendWatchPriceSample(current, selectedCode, lastView));
      setLoadingQuote(false);
    }).catch(() => {
      // Same-source K-line close remains the final display fallback during a cold start.
    });
    return () => {
      disposed = true;
    };
  }, [instrumentSourcesLoaded, marketSession.phase, selectedCode, selectedSource, selectedSourceReady]);

  useEffect(() => {
    let disposed = false;
    setTimelineSamples([]);
    if (selectedPeriod.mode !== "timeline") {
      setTimelineHistorySyncing(false);
      return () => {
        disposed = true;
      };
    }
    setTimelineHistorySyncing(true);
    void (async () => {
      let cursor: number | undefined;
      const seenCursors = new Set<number>();
      const historyPages: TimelineSample[][] = [];
      while (!disposed) {
        const page = await marketApi.timelineSamplePage(selectedCode, selectedSource, cursor);
        if (disposed || selectedCodeRef.current !== selectedCode) return;
        const samples = page.items.map((item) => ({
          time: Date.parse(item.received_at) / 1_000,
          observedTime: Date.parse(item.observed_at) / 1_000,
          value: Number(item.value),
          eventId: item.event_id,
        })).filter((item) => (
          Number.isFinite(item.time)
          && Number.isFinite(item.observedTime)
          && Number.isFinite(item.value)
        ));
        historyPages.push(samples);
        if (historyPages.length === 1) {
          setTimelineSamples((current) => mergeTimelineSamples(samples, current));
        }
        if (!page.has_more || page.next_cursor === null) break;
        if (seenCursors.has(page.next_cursor)) {
          throw new Error("分时事件游标未前进");
        }
        seenCursors.add(page.next_cursor);
        cursor = page.next_cursor;
      }
      if (disposed || selectedCodeRef.current !== selectedCode) return;
      const completeHistory = mergeTimelineSamples(...historyPages);
      startTransition(() => {
        setTimelineSamples((current) => mergeTimelineSamples(completeHistory, current));
        setTimelineHistorySyncing(false);
      });
    })().catch(() => {
      // Persisted samples enrich the timeline; the live raw-event stream remains independent.
      if (!disposed) setTimelineHistorySyncing(false);
    });
    return () => {
      disposed = true;
    };
  }, [selectedCode, selectedPeriod.mode, selectedSource]);

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
    if (!sourceMenuOpen) return;
    void refreshSourceSnapshot();
    const timer = window.setInterval(() => void refreshSourceSnapshot(), 5_000);
    return () => window.clearInterval(timer);
  }, [refreshSourceSnapshot, sourceMenuOpen]);

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
        const source = instrumentSources[item.provider_code] ?? "jin10_client";
        const descriptor = sourceById.get(source);
        if (!descriptor || (descriptor.manual_connection_required && !descriptor.connection_active)) return;
        const value = await marketApi.quote(item.provider_code);
        setWatchQuotes((current) => ({ ...current, [item.provider_code]: value }));
        setWatchPriceSeries((current) => appendWatchPriceSample(current, item.provider_code, value));
      }),
    );
  }, [instrumentSources, instrumentSourcesLoaded, instruments, selectedCode, sourceById, sourcesLoaded, watchQuotes]);

  useEffect(() => {
    if (!instrumentSourcesLoaded || !sourcesLoaded) return;
    let disposed = false;
    void Promise.allSettled(
      instruments.map(async (item) => {
        const source = instrumentSources[item.provider_code] ?? "jin10_client";
        const rows = await marketApi.candles(item.provider_code, source, { count: WATCH_SPARKLINE_CANDLE_WINDOW });
        const prices = downsamplePrices(rows
          .map((row) => numeric(row.close))
          .filter((value): value is number => value !== null), WATCH_SPARKLINE_LIMIT);
        if (disposed || prices.length === 0) return;
        setWatchPriceSeries((current) => {
          const live = current[item.provider_code] ?? [];
          return {
            ...current,
            [item.provider_code]: [...prices, ...live].slice(-WATCH_SPARKLINE_LIMIT),
          };
        });
      }),
    );
    return () => {
      disposed = true;
    };
  }, [instrumentSources, instrumentSourcesLoaded, instruments, sourcesLoaded]);

  const preferSource = async (source: SourceDescriptor) => {
    setSourceBusy(true);
    try {
      const value = await marketApi.updateInstrumentSource(selectedCode, source.source_id);
      setCandles([]);
      setTimelineSamples([]);
      setInstrumentSources((current) => ({ ...current, [selectedCode]: value.source_id }));
      setWatchQuotes((current) => {
        const next = { ...current };
        delete next[selectedCode];
        return next;
      });
      setTestMessage(`${selectedCode} 的实时数据源已切换为 ${source.display_name}`);
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
      const value = await marketApi.testSource(source.source_id, selectedCode);
      const observedAt = value.observed_at
        ? formatBeijingClock(Date.parse(value.observed_at) / 1_000)
        : null;
      const ready = value.data_fresh && value.kline_points > 0;
      const qualityWarning = value.quality !== "complete";
      const message = ready
        ? `连接成功 · 报价 ${formatPrice(value.last!, value.code, selectedInstrument)} · K线 ${value.kline_points} · ${value.latency_ms}ms · ${observedAt}`
        : `连接已建立 · ${value.detail ?? "正在等待同源报价与 K 线"} · ${value.latency_ms}ms`;
      setSourceTestResults((current) => ({
        ...current,
        [source.source_id]: {
          tone: ready && !qualityWarning ? "success" : "warning",
          message,
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

  const handleSourceMenuOpenChange = useCallback((next: boolean) => {
    setSourceMenuOpen(next);
    if (!next) {
      setTestMessage(null);
      setSourceTestResults({});
    }
  }, []);

  const observedCodes = useMemo(
    () => new Set(instruments.map((item) => item.provider_code)),
    [instruments],
  );
  const filteredCatalog = useMemo(() => {
    const query = watchSearch.trim().toLocaleLowerCase("zh-CN");
    if (!query) return catalog;
    return catalog.filter((item) =>
      item.name.toLocaleLowerCase("zh-CN").includes(query)
      || item.provider_code.toLocaleLowerCase("zh-CN").includes(query)
      || item.price_unit.toLocaleLowerCase("zh-CN").includes(query),
    );
  }, [catalog, watchSearch]);

  const toggleWatchlistInstrument = async (item: InstrumentEntry) => {
    const isObserved = observedCodes.has(item.provider_code);
    setWatchlistBusyCode(item.provider_code);
    setWatchlistError(null);
    try {
      const next = isObserved
        ? await marketApi.removeFromWatchlist(item.provider_code)
        : await marketApi.addToWatchlist(item.provider_code);
      setInstruments(next);
      if (
        isObserved
        && item.provider_code === selectedCode
        && next.length > 0
      ) {
        setSelectedCode(next[0].provider_code);
      }
    } catch (error) {
      setWatchlistError(translateError(error));
    } finally {
      setWatchlistBusyCode(null);
    }
  };

  const priceQuote = quote?.quote ?? null;
  const dailyQuote = priceQuote;
  const latestCandle = candles.at(-1) ?? null;
  const quotedPrice = numeric(priceQuote?.last);
  const cachedClose = numeric(latestCandle?.close);
  const livePrice = quotedPrice ?? (marketSession.phase === "closed" ? cachedClose : null);
  const quoteObservedAt = priceQuote?.source.observed_at
    ?? (marketSession.phase === "closed" ? latestCandle?.open_time ?? null : null);
  const chartBars = useMemo(
    () => barsFromCandles(candles),
    [candles],
  );
  const displayBar = hover ?? chartBars.at(-1) ?? null;
  const timelineReferencePrice = useMemo(() => {
    const last = numeric(priceQuote?.last);
    const change = numeric(priceQuote?.change);
    if (last !== null && change !== null) return last - change;
    return chartBars[0]?.open ?? null;
  }, [chartBars, priceQuote?.change, priceQuote?.last]);
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

  const quoteTrend = priceQuote
    ? trendClass(priceQuote)
    : latestCandle && numeric(latestCandle.close) !== null && numeric(latestCandle.open) !== null
      ? (numeric(latestCandle.close) as number) >= (numeric(latestCandle.open) as number)
        ? "trend-up"
        : "trend-down"
      : "trend-neutral";
  const aggregateState = quote?.stale_fields.length
    ? "部分聚合字段已过期"
    : quote?.unavailable_fields.length
      ? "部分聚合字段缺失"
      : null;

  if (expertMode) {
    return (
      <ExpertModeWorkspace
        code={selectedCode}
        instrumentName={selectedInstrument.name}
        unit={unitFor(selectedCode, selectedInstrument) || "美元/盎司"}
        candles={candles}
        timelineSamples={timelineSamples}
        periodId={periodId}
        livePrice={livePrice}
        change={numeric(priceQuote?.change)}
        changePercent={numeric(priceQuote?.change_percent)}
        observedAt={quoteObservedAt}
        referencePrice={timelineReferencePrice}
        timelineResolutionSeconds={timelineSamplingSeconds}
        priceDigits={digitsFor(selectedCode, selectedInstrument)}
        marketPhase={marketSession.phase}
        marketSchedule={selectedInstrument.market_schedule}
        sourceLabel={selectedSourceDescriptor?.display_name ?? sourceLabels[selectedSource] ?? selectedSource}
        sourceState={quoteStreamState}
        historyLoading={historySyncing || timelineHistorySyncing}
        loading={loadingQuote || loadingCandles}
        error={quoteError ?? candleError}
        onPeriodChange={setPeriodId}
        onRequestOlderHistory={loadOlderCandles}
        onExit={() => setExpertMode(false)}
      />
    );
  }

  return (
    <div
      className={`terminal-shell ${watchOpen && watchPinned ? "is-watch-docked" : ""}`}
      data-market-phase={marketSession.phase}
    >
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
          <button
            type="button"
            className="accent"
            onClick={() => {
              if (selectedCode !== "XAUUSD") {
                selectedCodeRef.current = "XAUUSD";
                candleRequestRef.current += 1;
                historyCursorRef.current = null;
                historyLoadInFlightRef.current = false;
                setQuote(null);
                setCandles([]);
                setTimelineSamples([]);
                setQuoteError(null);
                setCandleError(null);
                setHistorySyncing(false);
                setLoadingQuote(true);
                setLoadingCandles(true);
              }
              setSelectedCode("XAUUSD");
              setHover(null);
              setExpertMode(true);
            }}
          >
            <Sparkles size={14} />专家模式
          </button>
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
          <div className="watch-panel-title">
            <strong>自选</strong>
            <span>{instruments.length}</span>
          </div>
          <div className="watch-panel-actions">
            <button
              type="button"
              className={watchManagerOpen ? "is-active" : ""}
              title="管理观察品种"
              aria-label="管理观察品种"
              aria-expanded={watchManagerOpen}
              onClick={() => {
                setWatchManagerOpen((current) => !current);
                setWatchlistError(null);
              }}
            >
              <ListPlus size={15} />
            </button>
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

        {watchManagerOpen ? (
          <section id="watch-manager" className="watch-manager" aria-label="管理观察品种">
            <div className="watch-manager-heading">
              <div>
                <strong>添加观察品种</strong>
                <span>直连行情与实时换算</span>
              </div>
              <button
                type="button"
                title="关闭品种管理"
                aria-label="关闭品种管理"
                onClick={() => setWatchManagerOpen(false)}
              >
                <X size={14} />
              </button>
            </div>
            <label className="watch-manager-search">
              <Search size={13} />
              <input
                value={watchSearch}
                onChange={(event) => setWatchSearch(event.target.value)}
                placeholder="搜索名称或代码"
              />
            </label>
            <div className="watch-manager-list">
              {filteredCatalog.map((item) => {
                const isObserved = observedCodes.has(item.provider_code);
                const isBusy = watchlistBusyCode === item.provider_code;
                const isLast = isObserved && instruments.length === 1;
                return (
                  <button
                    type="button"
                    key={item.provider_code}
                    className={"watch-manager-row " + (isObserved ? "is-observed" : "")}
                    aria-pressed={isObserved}
                    disabled={isBusy || isLast}
                    title={isLast ? "观察列表至少保留一个品种" : undefined}
                    onClick={() => void toggleWatchlistInstrument(item)}
                  >
                    <span className={"instrument-kind is-" + item.quote_kind}>
                      {item.quote_kind === "derived" ? "算" : "直"}
                    </span>
                    <span>
                      <strong>{item.name}</strong>
                      <small>{item.provider_code} · {item.price_unit}</small>
                    </span>
                    <span className="watch-manager-toggle">
                      {isBusy
                        ? <RefreshCw size={13} className="spin" />
                        : isObserved
                          ? <Check size={14} />
                          : <ListPlus size={14} />}
                    </span>
                  </button>
                );
              })}
              {filteredCatalog.length === 0 ? (
                <div className="watch-manager-empty">没有匹配的品种</div>
              ) : null}
            </div>
            {watchlistError ? <div className="watch-manager-error">{watchlistError}</div> : null}
          </section>
        ) : null}

        <div className="watch-list-head" aria-hidden="true">
          <span className="watch-list-modes"><Activity size={17} /><Waypoints size={16} /></span>
          <span>价格</span>
          <span>涨跌</span>
        </div>
        <div className="instrument-list">
          {instruments.map((item) => {
            const itemQuote = watchQuotes[item.provider_code];
            const direction = trendClass(itemQuote?.quote ?? null);
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
                <WatchSparkline
                  code={item.provider_code}
                  quote={itemQuote?.quote ?? null}
                  samples={watchPriceSeries[item.provider_code] ?? []}
                />
                <div className={`instrument-price ${direction}`}>
                  <strong>{formatPrice(itemQuote?.quote.last, item.provider_code, item)}</strong>
                </div>
                <div className={`instrument-change ${direction}`}>
                  <strong>
                    {numeric(itemQuote?.quote.change_percent) === null
                      ? "—"
                      : `${formatSigned(itemQuote?.quote.change_percent)}%`}
                  </strong>
                  <span>{formatSigned(itemQuote?.quote.change, digitsFor(item.provider_code, item))}</span>
                </div>
              </button>
            );
          })}
          <button
            type="button"
            className="watch-add-market-button"
            aria-controls="watch-manager"
            aria-expanded={watchManagerOpen}
            onClick={() => {
              setWatchSearch("");
              setWatchlistError(null);
              setWatchManagerOpen(true);
            }}
          >
            + 添加行情
          </button>
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
            sources={compatibleSources}
            selectedSource={selectedSource}
            fallbackLabel={sourceLabels[selectedSource]}
            busy={sourceBusy}
            contractCode={selectedCode}
            connectionState={quoteStreamState}
            marketPhase={marketSession.phase}
            connectionError={quoteError}
            testingSourceId={testingSourceId}
            testResults={sourceTestResults}
            notice={testMessage}
            onSelect={preferSource}
            onTest={(source) => void testSource(source)}
            onOpenChange={handleSourceMenuOpenChange}
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
                <small><LiveClock /></small>
                <span
                  className={`session-badge is-${marketSession.phase}`}
                  title={selectedInstrument.market_schedule?.reference}
                >
                  {marketSession.label}
                </span>
                {unitFor(selectedCode, selectedInstrument) ? <span className="quote-unit-badge">{unitFor(selectedCode, selectedInstrument)}</span> : null}
                {selectedInstrument.quote_kind === "derived" ? (
                  <span
                    className="quote-derivation-badge"
                    title="现货黄金(美元/盎司) × 美元兑离岸人民币 ÷ 31.1034768"
                  >
                    实时换算
                  </span>
                ) : null}
              </div>
              <div className={`hero-price chart-hero-price ${quoteTrend}`}>
                <strong>{loadingQuote && livePrice === null ? "读取中" : formatPrice(livePrice, selectedCode, selectedInstrument)}</strong>
                {numeric(priceQuote?.change) === null ? null : (
                  <span>{(numeric(priceQuote?.change) ?? 0) >= 0 ? "↑" : "↓"}</span>
                )}
                <small>{formatSigned(priceQuote?.change_percent)}%</small>
                <small>{formatSigned(priceQuote?.change, digitsFor(selectedCode, selectedInstrument))}</small>
              </div>
            </div>
            <div className="chart-context-secondary">
              <dl className="chart-daily-stats">
                <div><dt>最高</dt><dd className="trend-up">{formatPrice(dailyQuote?.high, selectedCode, selectedInstrument)}</dd></div>
                <div><dt>最低</dt><dd className="trend-down">{formatPrice(dailyQuote?.low, selectedCode, selectedInstrument)}</dd></div>
                <div><dt>今开</dt><dd>{formatPrice(dailyQuote?.open, selectedCode, selectedInstrument)}</dd></div>
              </dl>
              <div className="chart-freshness">
                <span className={`status-dot ${quoteError ? "is-error" : marketSession.phase === "closed" ? "is-closed" : ""}`} />
                <span>
                  {quoteError
                    ? quoteError
                    : marketSession.phase === "closed"
                      ? `${sourceById.get(selectedSource)?.display_name ?? sourceLabels[selectedSource]} · 休市，显示最后有效行情${quoteObservedAt ? ` · ${formatBeijingClock(Date.parse(quoteObservedAt) / 1_000)}` : ""}${aggregateState ? ` · ${aggregateState}` : ""}`
                      : `${sourceById.get(selectedSource)?.display_name ?? sourceLabels[selectedSource]} · ${priceQuote ? formatBeijingClock(Date.parse(priceQuote.source.observed_at) / 1_000) : "等待数据"}${aggregateState ? ` · ${aggregateState}` : ""}`}
                </span>
              </div>
            </div>
          </div>
          {displayBar ? (
            <div className={`ohlc-overlay ${selectedPeriod.mode === "timeline" ? "is-timeline" : ""}`}>
              <span>{formatChartTimeLabel(displayBar.time, selectedPeriod, timelineSamplingSeconds)}</span>
              {selectedPeriod.mode === "timeline" ? (
                <>
                  <span>价格 <b>{formatPrice(displayBar.close, selectedCode, selectedInstrument)}</b></span>
                  <span>涨跌 <b className={timelineTrend}>{formatSigned(timelineChange, digitsFor(selectedCode, selectedInstrument))}</b></span>
                  <span>涨幅 <b className={timelineTrend}>{formatSigned(timelinePercent)}%</b></span>
                </>
              ) : (
                <>
                  <span>开 <b>{formatPrice(displayBar.open, selectedCode, selectedInstrument)}</b></span>
                  <span>高 <b className="trend-up">{formatPrice(displayBar.high, selectedCode, selectedInstrument)}</b></span>
                  <span>低 <b className="trend-down">{formatPrice(displayBar.low, selectedCode, selectedInstrument)}</b></span>
                  <span>收 <b>{formatPrice(displayBar.close, selectedCode, selectedInstrument)}</b></span>
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
            priceDigits={digitsFor(selectedCode, selectedInstrument)}
            marketPhase={marketSession.phase}
            marketSchedule={selectedInstrument.market_schedule}
            historyLoading={historySyncing || timelineHistorySyncing}
            onRequestOlderHistory={loadOlderCandles}
            onHover={setHover}
          />
          {historySyncing || timelineHistorySyncing ? (
            <div
              className="history-loading-indicator"
              role="status"
              aria-live="polite"
              title="正在从当前实时数据源无上限补齐完整历史行情"
            >
              <LoaderCircle size={12} aria-hidden="true" />
              <span>加载中...</span>
            </div>
          ) : null}
          {loadingCandles && candles.length === 0 ? <div className="chart-state"><RefreshCw size={20} className="spin" /><strong>正在读取 K 线</strong></div> : null}
          {candleError ? <div className="chart-state is-error"><CircleHelp size={22} /><strong>K 线暂不可用</strong><span>{candleError}</span><button type="button" onClick={() => void loadCandles()}>重试</button></div> : null}
        </section>
      </main>

    </div>
  );
}
