import {
  Activity,
  Bell,
  CandlestickChart,
  ChevronDown,
  CircleHelp,
  Database,
  Gauge,
  Maximize2,
  Newspaper,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightOpen,
  Pin,
  PinOff,
  RefreshCw,
  Search,
  Settings2,
  Sparkles,
  Waypoints,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { marketApi } from "./api";
import { buildChartBars } from "./chartModel";
import { MarketChart } from "./MarketChart";
import { SourceDrawer, type SourceTestFeedback } from "./SourceDrawer";
import type {
  Candle,
  HoverCandle,
  InstrumentEntry,
  QuoteSnapshot,
  SourceDescriptor,
  SourceId,
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
  auto: "本地优先",
  jin10_mcp: "金十官方 MCP",
  jin10_desktop: "本地金十软件",
};

type ConcreteSourceId = Exclude<SourceId, "auto">;

const errorTranslations: Array<[RegExp, string]> = [
  [/Jin10 desktop process is not running/i, "金十数据软件未运行"],
  [/market window is minimized/i, "金十行情窗口已最小化，请先恢复窗口"],
  [/market window was not found/i, "未找到金十行情页，请在软件中打开行情窗口"],
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
  const [selectedSource, setSelectedSource] = useState<SourceId>("auto");
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, QuoteSnapshot>>({});
  const [candles, setCandles] = useState<Candle[]>([]);
  const [sources, setSources] = useState<SourceDescriptor[]>([]);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [watchOpen, setWatchOpen] = useState(true);
  const [watchPinned, setWatchPinned] = useState(true);
  const [intervalMinutes, setIntervalMinutes] = useState(1);
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [testMessage, setTestMessage] = useState<string | null>(null);
  const [testingSourceId, setTestingSourceId] = useState<ConcreteSourceId | null>(null);
  const [sourceTestResults, setSourceTestResults] = useState<
    Partial<Record<ConcreteSourceId, SourceTestFeedback>>
  >({});
  const [loadingQuote, setLoadingQuote] = useState(true);
  const [loadingCandles, setLoadingCandles] = useState(true);

  const selectedInstrument =
    instruments.find((instrument) => instrument.provider_code === selectedCode) ?? defaultInstruments[0];

  const loadSources = useCallback(async () => {
    setSourceBusy(true);
    try {
      setSources(await marketApi.sources());
    } catch (error) {
      setTestMessage(`来源检测失败：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  }, []);

  const loadQuote = useCallback(async () => {
    setLoadingQuote(true);
    try {
      const value = await marketApi.quote(selectedCode, selectedSource);
      setQuote(value);
      setWatchQuotes((current) => ({ ...current, [selectedCode]: value }));
      setQuoteError(null);
    } catch (error) {
      setQuoteError(translateError(error));
    } finally {
      setLoadingQuote(false);
    }
  }, [selectedCode, selectedSource]);

  const loadCandles = useCallback(async () => {
    setLoadingCandles(true);
    try {
      setCandles(await marketApi.candles(selectedCode));
      setCandleError(null);
    } catch (error) {
      setCandles([]);
      setCandleError(translateError(error));
    } finally {
      setLoadingCandles(false);
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
    setQuote(null);
    void loadQuote();
    void loadCandles();
  }, [loadQuote, loadCandles]);

  useEffect(() => {
    const delay = quote?.source.provider === "jin10_desktop" ? 5_000 : 65_000;
    const timer = window.setInterval(() => void loadQuote(), delay);
    return () => window.clearInterval(timer);
  }, [loadQuote, quote?.source.provider]);

  useEffect(() => {
    const timer = window.setInterval(() => void loadCandles(), 65_000);
    return () => window.clearInterval(timer);
  }, [loadCandles]);

  useEffect(() => {
    const missing = instruments.filter((item) => !watchQuotes[item.provider_code]);
    if (missing.length === 0) return;
    void Promise.allSettled(
      missing.map(async (item) => {
        const value = await marketApi.quote(item.provider_code, selectedSource);
        setWatchQuotes((current) => ({ ...current, [item.provider_code]: value }));
      }),
    );
  }, [instruments, selectedSource, watchQuotes]);

  const updateSource = async (
    source: SourceDescriptor,
    update: { enabled?: boolean; priority?: number },
  ) => {
    setSourceBusy(true);
    try {
      await marketApi.updateSource(source.source_id, update);
      await loadSources();
      await loadQuote();
    } catch (error) {
      setTestMessage(`更新失败：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  };

  const preferSource = async (source: SourceDescriptor) => {
    setSourceBusy(true);
    try {
      await Promise.all(
        sources.map((item) =>
          marketApi.updateSource(item.source_id, { priority: item.source_id === source.source_id ? 5 : 20 }),
        ),
      );
      await loadSources();
      await loadQuote();
    } catch (error) {
      setTestMessage(`排序失败：${translateError(error)}`);
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
      setTestingSourceId(null);
    }
  };

  const livePrice = numeric(quote?.last);
  const quoteObservedAt = quote?.source.observed_at ?? null;
  const chartBars = useMemo(
    () => buildChartBars(candles, intervalMinutes, livePrice, quoteObservedAt),
    [candles, intervalMinutes, livePrice, quoteObservedAt],
  );
  const displayBar = hover ?? chartBars.at(-1) ?? null;

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
          <button type="button" title="数据源" onClick={() => setDrawerOpen(true)}><Database size={18} /></button>
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
              onClick={() => setWatchPinned((current) => !current)}
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

        <button type="button" className="source-quick" onClick={() => setDrawerOpen(true)}>
          <div className="source-quick-icon"><Database size={18} /></div>
          <div>
            <strong>数据来源</strong>
            <span>{sources.filter((source) => source.enabled).length} 个已启用 · 点击管理</span>
          </div>
          <PanelRightOpen size={17} />
        </button>
      </aside>

      {!watchOpen ? (
        <button
          type="button"
          className="watch-reveal-button"
          title={watchPinned ? "展开并固定行情列表" : "展开悬浮行情列表"}
          aria-label="展开行情列表"
          onClick={() => setWatchOpen(true)}
        >
          <PanelLeftOpen size={16} />
          <span>行情</span>
        </button>
      ) : null}

      <main className="market-main">
        <div className="chart-toolbar">
          <div className="indicator-button"><span>指标</span><ChevronDown size={14} /></div>
          {[1, 5, 15, 30, 60].map((minutes) => (
            <button type="button" key={minutes} className={intervalMinutes === minutes ? "is-active" : ""} onClick={() => setIntervalMinutes(minutes)}>
              {minutes < 60 ? `${minutes}分` : "1小时"}
            </button>
          ))}
          <button type="button" disabled title="等待长周期历史数据源">4小时</button>
          <button type="button" disabled title="等待长周期历史数据源">日K</button>
          <div className="toolbar-spacer" />
          <span
            className={`live-chart-state ${quoteError || livePrice === null ? "is-offline" : ""}`}
            title={quoteError ?? (quote ? `报价采样于 ${new Date(quote.source.observed_at).toLocaleTimeString("zh-CN", { hour12: false })}` : "等待报价")}
          >
            <i />
            {quoteError || livePrice === null ? "等待报价" : "实时"}
          </span>
          <label className="source-select toolbar-source-select">
            <Database size={14} />
            <span>报价源</span>
            <select value={selectedSource} onChange={(event) => setSelectedSource(event.target.value as SourceId)}>
              <option value="auto">本地优先（自动回退）</option>
              <option value="jin10_desktop">本地金十软件</option>
              <option value="jin10_mcp">金十官方 MCP</option>
            </select>
          </label>
          <button type="button" className="toolbar-icon-button" title="刷新报价" onClick={() => void loadQuote()}><RefreshCw size={15} className={loadingQuote ? "spin" : ""} /></button>
          <button type="button" className="draw-button"><Activity size={15} />画线</button>
          <button type="button" className="draw-button" onClick={() => setDrawerOpen(true)}><Settings2 size={15} />设置</button>
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
            <div className="ohlc-overlay">
              <span>{new Date(displayBar.time * 1000).toLocaleString("zh-CN", { hour12: false })}</span>
              <span>开 <b>{formatPrice(displayBar.open, selectedCode)}</b></span>
              <span>高 <b className="trend-up">{formatPrice(displayBar.high, selectedCode)}</b></span>
              <span>低 <b className="trend-down">{formatPrice(displayBar.low, selectedCode)}</b></span>
              <span>收 <b>{formatPrice(displayBar.close, selectedCode)}</b></span>
            </div>
          ) : null}
          <MarketChart
            candles={candles}
            intervalMinutes={intervalMinutes}
            livePrice={livePrice}
            observedAt={quoteObservedAt}
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
        testingSourceId={testingSourceId}
        testResults={sourceTestResults}
        onClose={() => {
          setDrawerOpen(false);
          setTestMessage(null);
          setSourceTestResults({});
        }}
        onRefresh={() => void loadSources()}
        onToggle={(source) => void updateSource(source, { enabled: !source.enabled })}
        onPrefer={(source) => void preferSource(source)}
        onTest={(source) => void testSource(source)}
      />
    </div>
  );
}
