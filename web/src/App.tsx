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
  PanelRightOpen,
  RefreshCw,
  Search,
  Settings2,
  SlidersHorizontal,
  Sparkles,
  Waypoints,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";

import { marketApi } from "./api";
import { MarketChart } from "./MarketChart";
import { SourceDrawer } from "./SourceDrawer";
import type {
  Candle,
  HoverCandle,
  InstrumentEntry,
  QuoteComparison,
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
  auto: "自动选择",
  jin10_mcp: "金十官方 MCP",
  jin10_desktop: "本地金十软件",
};

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

export default function App() {
  const [instruments, setInstruments] = useState(defaultInstruments);
  const [selectedCode, setSelectedCode] = useState("XAUUSD");
  const [selectedSource, setSelectedSource] = useState<SourceId>("auto");
  const [quote, setQuote] = useState<QuoteSnapshot | null>(null);
  const [watchQuotes, setWatchQuotes] = useState<Record<string, QuoteSnapshot>>({});
  const [candles, setCandles] = useState<Candle[]>([]);
  const [sources, setSources] = useState<SourceDescriptor[]>([]);
  const [comparison, setComparison] = useState<QuoteComparison | null>(null);
  const [compareOpen, setCompareOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [intervalMinutes, setIntervalMinutes] = useState(1);
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [quoteError, setQuoteError] = useState<string | null>(null);
  const [candleError, setCandleError] = useState<string | null>(null);
  const [sourceBusy, setSourceBusy] = useState(false);
  const [testMessage, setTestMessage] = useState<string | null>(null);
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
    setComparison(null);
    void loadQuote();
    void loadCandles();
  }, [loadQuote, loadCandles]);

  useEffect(() => {
    const delay = quote?.source.provider === "jin10_desktop" ? 5_000 : 90_000;
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

  const loadComparison = useCallback(async () => {
    setComparison(null);
    try {
      setComparison(await marketApi.compare(selectedCode));
    } catch (error) {
      setTestMessage(`双源比较失败：${translateError(error)}`);
    }
  }, [selectedCode]);

  const toggleComparison = () => {
    setCompareOpen((current) => !current);
    if (!compareOpen) void loadComparison();
  };

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
    setSourceBusy(true);
    try {
      const value = await marketApi.quote("XAUUSD", source.source_id);
      setTestMessage(`${source.display_name}：${formatPrice(value.last, "XAUUSD")}，测试成功`);
    } catch (error) {
      setTestMessage(`${source.display_name}：${translateError(error)}`);
    } finally {
      setSourceBusy(false);
    }
  };

  const displayBar = useMemo(() => {
    if (hover) return hover;
    const last = candles.at(-1);
    if (!last) return null;
    return {
      time: Math.floor(new Date(last.open_time).getTime() / 1000),
      open: Number(last.open),
      high: Number(last.high),
      low: Number(last.low),
      close: Number(last.close),
    };
  }, [candles, hover]);

  const quoteTrend = trendClass(quote);
  const quoteProvider = quote?.source.provider as SourceId | undefined;

  return (
    <div className="terminal-shell">
      <nav className="rail" aria-label="主功能">
        <div className="brand-mark" title="Market Pulse">
          <span>M</span>
          <small>LAB</small>
        </div>
        <div className="rail-tools">
          <button type="button" title="市场概览"><Gauge size={22} /></button>
          <button type="button" title="策略观察"><Waypoints size={22} /></button>
          <button type="button" className="is-active" title="行情图表"><CandlestickChart size={22} /></button>
          <button type="button" title="资讯"><Newspaper size={22} /></button>
          <button type="button" title="数据源" onClick={() => setDrawerOpen(true)}><Database size={22} /></button>
        </div>
        <div className="rail-bottom">
          <button type="button" title="帮助"><CircleHelp size={20} /></button>
          <div className="avatar">D</div>
        </div>
      </nav>

      <aside className="watch-panel">
        <header className="watch-head">
          <div>
            <p className="eyebrow">MARKET WATCH</p>
            <h1>行情</h1>
          </div>
          <button type="button" className="icon-button" title="搜索品种"><Search size={18} /></button>
        </header>
        <div className="market-tabs">
          <button type="button">自选</button>
          <button type="button">外汇</button>
          <button type="button" className="is-active">贵金属</button>
          <button type="button">能源</button>
        </div>

        <div className="quote-tiles">
          {instruments.slice(0, 2).map((item) => {
            const itemQuote = watchQuotes[item.provider_code];
            const direction = trendClass(itemQuote ?? null);
            return (
              <button
                type="button"
                key={item.provider_code}
                className={`${direction} ${selectedCode === item.provider_code ? "is-selected" : ""}`}
                onClick={() => setSelectedCode(item.provider_code)}
              >
                <span>{item.name}</span>
                <strong>{formatPrice(itemQuote?.last, item.provider_code)}</strong>
                <small>{formatSigned(itemQuote?.change_percent)}%</small>
              </button>
            );
          })}
        </div>

        <div className="watch-section-title">
          <div><ChevronDown size={15} /><strong>贵金属</strong></div>
          <span>实时观察</span>
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

      <main className="market-main">
        <div className="utility-bar">
          <div className="search-box"><Search size={16} /><span>搜索品种、资讯或指标</span></div>
          <div className="utility-actions">
            <button type="button" title="提醒"><Bell size={17} /></button>
            <button type="button" title="全屏"><Maximize2 size={17} /></button>
          </div>
        </div>

        <header className="instrument-head">
          <div className="instrument-identity">
            <div><strong>{selectedInstrument.name}</strong><span>{selectedCode}</span><ChevronDown size={15} /></div>
            <small>{new Date().toLocaleString("zh-CN", { hour12: false })}</small>
            <span className="session-badge">交易中</span>
          </div>
          <div className="head-actions">
            <label className="source-select">
              <Database size={14} />
              <span>报价源</span>
              <select value={selectedSource} onChange={(event) => setSelectedSource(event.target.value as SourceId)}>
                <option value="auto">自动选择</option>
                <option value="jin10_mcp">金十官方 MCP</option>
                <option value="jin10_desktop">本地金十软件</option>
              </select>
            </label>
            <button type="button" className={`compare-button ${compareOpen ? "is-active" : ""}`} onClick={toggleComparison}>
              <SlidersHorizontal size={15} />双源对比
            </button>
            <button type="button" className="icon-button" title="来源设置" onClick={() => setDrawerOpen(true)}><Settings2 size={18} /></button>
          </div>
        </header>

        <div className="content-tabs">
          <button type="button" className="is-active">图表</button>
          <button type="button">快讯</button>
          <button type="button">头条</button>
          <button type="button">研报</button>
          <button type="button">指标库</button>
          <button type="button" className="accent"><Sparkles size={14} />分析器</button>
        </div>

        <section className="quote-strip">
          <div className={`hero-price ${quoteTrend}`}>
            <strong>{loadingQuote && !quote ? "读取中" : formatPrice(quote?.last, selectedCode)}</strong>
            {numeric(quote?.change) === null ? null : (
              <span>{(numeric(quote?.change) ?? 0) >= 0 ? "↑" : "↓"}</span>
            )}
            <small>{formatSigned(quote?.change_percent)}%</small>
            <small>{formatSigned(quote?.change, digitsFor(selectedCode))}</small>
          </div>
          <dl className="daily-stats">
            <div><dt>最高</dt><dd className="trend-up">{formatPrice(quote?.high, selectedCode)}</dd></div>
            <div><dt>最低</dt><dd className="trend-down">{formatPrice(quote?.low, selectedCode)}</dd></div>
            <div><dt>今开</dt><dd>{formatPrice(quote?.open, selectedCode)}</dd></div>
            <div><dt>来源</dt><dd>{sourceLabels[quoteProvider ?? selectedSource]}</dd></div>
          </dl>
          <div className="freshness">
            <span className={`status-dot ${quoteError ? "is-error" : ""}`} />
            <div>
              <strong>{quoteError ? "报价暂不可用" : "报价已连接"}</strong>
              <span>{quoteError ?? (quote ? `采样 ${new Date(quote.source.observed_at).toLocaleTimeString("zh-CN", { hour12: false })}` : "等待数据")}</span>
            </div>
            <button type="button" className="icon-button" title="刷新报价" onClick={() => void loadQuote()}><RefreshCw size={16} className={loadingQuote ? "spin" : ""} /></button>
          </div>
        </section>

        {compareOpen ? (
          <section className="comparison-strip">
            <div className="comparison-title"><Activity size={16} /><div><strong>双源实时对照</strong><span>官方 MCP 作为偏差基准；失败来源不会影响另一路结果</span></div></div>
            <div className="comparison-items">
              {comparison ? comparison.items.map((item) => (
                <div className={`comparison-item ${item.error ? "has-error" : ""}`} key={item.source_id}>
                  <span>{sourceLabels[item.source_id]}</span>
                  <strong>{item.quote ? formatPrice(item.quote.last, selectedCode) : "不可用"}</strong>
                  <small>{item.error ? translateError(item.error) : `偏差 ${formatSigned(item.deviation, digitsFor(selectedCode))} · ${item.request_latency_ms}ms`}</small>
                </div>
              )) : <div className="comparison-loading"><RefreshCw size={16} className="spin" />正在分别读取两套来源…</div>}
            </div>
            <button type="button" className="icon-button" title="重新比较" onClick={() => void loadComparison()}><RefreshCw size={15} /></button>
          </section>
        ) : null}

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
          <span className="chart-source">K 线：金十官方 MCP · 最多 100 条分钟数据</span>
          <button type="button" className="draw-button"><Activity size={15} />画线</button>
          <button type="button" className="draw-button"><Settings2 size={15} />设置</button>
        </div>

        <section className="chart-area">
          {displayBar ? (
            <div className="ohlc-overlay">
              <span>{new Date(displayBar.time * 1000).toLocaleString("zh-CN", { hour12: false })}</span>
              <span>开 <b>{formatPrice(displayBar.open, selectedCode)}</b></span>
              <span>高 <b className="trend-up">{formatPrice(displayBar.high, selectedCode)}</b></span>
              <span>低 <b className="trend-down">{formatPrice(displayBar.low, selectedCode)}</b></span>
              <span>收 <b>{formatPrice(displayBar.close, selectedCode)}</b></span>
            </div>
          ) : null}
          <MarketChart candles={candles} intervalMinutes={intervalMinutes} onHover={setHover} />
          {loadingCandles && candles.length === 0 ? <div className="chart-state"><RefreshCw size={20} className="spin" /><strong>正在读取 K 线</strong></div> : null}
          {candleError ? <div className="chart-state is-error"><CircleHelp size={22} /><strong>K 线暂不可用</strong><span>{candleError}</span><button type="button" onClick={() => void loadCandles()}>重试</button></div> : null}
        </section>
      </main>

      <SourceDrawer
        open={drawerOpen}
        sources={sources}
        busy={sourceBusy}
        testMessage={testMessage}
        onClose={() => { setDrawerOpen(false); setTestMessage(null); }}
        onRefresh={() => void loadSources()}
        onToggle={(source) => void updateSource(source, { enabled: !source.enabled })}
        onPrefer={(source) => void preferSource(source)}
        onTest={(source) => void testSource(source)}
      />
    </div>
  );
}
