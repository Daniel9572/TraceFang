import {isResearchLabelOnly,mergeResearchDisplayRows,researchSourceText} from "./researchTemporal";
import {
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  CandlestickChart,
  Database,
  FlaskConical,
  Search,
  RefreshCw,
  Download,
  Star,
} from "lucide-react";
import { MarketChart } from "./MarketChart";
import { canRequestResearchHistory, resolveResearchHistoryStep } from "./historyLoading";
import { chartPeriodById, type ChartPeriodId } from "./chartPeriods";
import {
  activeDrawingLayer,
  appendDrawingToActiveLayer,
  buildChartLayers,
  createDefaultChartLayerWorkspace,
  readChartLayerWorkspace,
  type ChartLayerWorkspace,
} from "./chartLayers";
import {
  DrawingControls,
  replaceDrawing,
  useDrawingHistory,
} from "./DrawingControls";
import {projectQuantAnalysis,projectQuantSeries,projectQuantOverlays,projectQuantTrendLines} from "./quantProjection";
import {useQuantSnapshot} from "./useQuantSnapshot";
import {QuantSimulationPanel} from "./QuantSimulationPanel";
import type {
  ExpertDrawingSnapMode,
  ExpertDrawingTool,
  ExpertStrategyId,
} from "./expertTypes";
import type { Candle, HoverCandle } from "./types";
import { RealtimeBarStream } from "./realtimeBarStream";
import { ResearchAiPanel } from "./ResearchAiPanel";

import {
  downloadText,
  researchApi,
  type ResearchAsset,
  type ResearchInstrument,
  type ResearchPage,
  type ResearchQuery,
  type ResearchSource,
  type ResearchSourceId,
  sourcePeriods,
} from "./researchApi";
import "./research.css";

const OptionsLab = lazy(() =>
  import("./OptionsLab").then((module) => ({ default: module.OptionsLab })),
);
const RealtimeApp = lazy(() => import("./App"));
const ASSETS: Record<ResearchAsset, string> = {
  equity: "股票",
  etf: "ETF",
  index: "指数",
  future: "期货",
  option: "期权",
};
const PERIODS: Record<string, string> = {
  "1m": "1分",
  "5m": "5分",
  "15m": "15分",
  "30m": "30分",
  "1h": "1小时",
  "1d": "日线",
  "1w": "周线",
  "1M": "月线",
};
const EXAMPLES: Record<ResearchSourceId, string> = {
  tencent: "600519.SH / 510300.SH",
  eastmoney: "600519.SH / 510300.SH",
  sina: "AU0 / RB0 / AU2612",
  tushare: "600519.SH / AU2612.SHF",
  alpaca: "AAPL / SPY / OCC期权代码",
  akshare: "AU0 / AU2612C900 / 10011425.SH / 600519.SH",
};
const initial: ResearchInstrument = {
  source: "akshare",
  symbol: "AU0",
  name: "沪金连续",
  asset: "future",
  currency: "CNY",
};
const format = researchSourceText;
const identity = (item: ResearchInstrument) =>
  `${item.source}:${item.asset}:${item.symbol}`;

function readSelection(): ResearchInstrument {
  try {
    const value = JSON.parse(
      localStorage.getItem("tracefang.research.selection") ?? "null",
    );
    if (
      value &&
      value.source in EXAMPLES &&
      value.asset in ASSETS &&
      typeof value.symbol === "string"
    )
      return value;
  } catch {
    /* Use the default instrument. */
  }
  return initial;
}
function readFavorites(): ResearchInstrument[] {
  try {
    const value = JSON.parse(
      localStorage.getItem("tracefang.research.favorites") ?? "[]",
    );
    return Array.isArray(value)
      ? value
          .filter(
            (item) =>
              item &&
              item.source in EXAMPLES &&
              item.asset in ASSETS &&
              typeof item.symbol === "string",
          )
          .slice(0, 200)
      : [];
  } catch {
    return [];
  }
}
function readWorkspace(scope: string): ChartLayerWorkspace {
  try {
    return readChartLayerWorkspace(
      localStorage.getItem(`tracefang.research.drawings:${scope}`),
    );
  } catch {
    return createDefaultChartLayerWorkspace();
  }
}

function DataCenter({
  sources,
  reload,
}: {
  sources: ResearchSource[];
  reload: () => void;
}) {
  const [probe, setProbe] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const test = async (source: ResearchSource) => {
    setBusy(source.id);
    const sample: Record<string, Partial<ResearchQuery>> = {
      tencent: { symbol: "510300.SH", asset: "etf" },
      eastmoney: { symbol: "510300.SH", asset: "etf" },
      sina: { symbol: "AU0", asset: "future" },
      tushare: { symbol: "600519.SH", asset: "equity" },
      alpaca: { symbol: "SPY", asset: "etf" },
      akshare: { symbol: "AU0", asset: "future", period: "5m" },
    };
    try {
      const page = await researchApi.bars(
        {
          source: source.id,
          period: "1d",
          adjustment: "raw",
          limit: 5,
          ...sample[source.id],
        } as ResearchQuery,
        undefined,
        true,
      );
      setProbe((current) => ({
        ...current,
        [source.id]: `${page.cache_state === "stale" ? "上游失败，读取旧缓存" : page.items.length ? "读取成功" : "连接成功但无行情"} · ${page.items.length} 根 · ${page.data_as_of ?? "无时间"}`,
      }));
    } catch (error) {
      setProbe((current) => ({
        ...current,
        [source.id]: error instanceof Error ? error.message : String(error),
      }));
    } finally {
      setBusy(null);
      reload();
    }
  };
  return (
    <main className="data-center">
      <header className="research-page-heading">
        <div>
          <small>DATA / CONNECTIONS</small>
          <h1>数据中心</h1>
          <p>确认来源支持什么、数据更新到何时，以及当前账户能否读取。</p>
        </div>
        <button onClick={reload}>
          <RefreshCw size={15} />
          重新检测配置
        </button>
      </header>
      <div className="data-source-grid">
        {sources.map((source) => (
          <article className="research-card" key={source.id}>
            <header>
              <h2>{source.name}</h2>
              <span
                className={`research-badge ${source.configured ? "ok" : "warning"}`}
              >
                {source.configured
                  ? source.credentials.length
                    ? "凭据已配置 · 待验证权限"
                    : "公开接口"
                  : "待配置"}
              </span>
            </header>
            <p>{source.market}</p>
            <p className="muted">{source.note}</p>
            <dl>
              <dt>原生周期</dt>
              <dd>
                {source.asset_periods
                  ? Object.entries(source.asset_periods)
                      .map(
                        ([kind, values]) =>
                          `${ASSETS[kind as ResearchAsset]}：${values.map((value) => PERIODS[value]).join(" / ")}`,
                      )
                      .join("；")
                  : source.periods.map((value) => PERIODS[value]).join(" / ")}
              </dd>
              <dt>代码示例</dt>
              <dd>{EXAMPLES[source.id]}</dd>
              <dt>最近检查</dt>
              <dd>{source.diagnostic?.detail ?? "尚未执行网络检查"}</dd>
            </dl>
            {source.credentials.length ? (
              <details open={!source.configured}>
                <summary>连接配置</summary>
                <p>
                  在项目的 .env.local 中设置以下变量，然后重启
                  TraceFang。状态页只显示是否配置，不读取或展示凭据值。
                </p>
                <pre>
                  {source.credentials
                    .map((key) => `${key}=你的凭据`)
                    .join("\n")}
                </pre>
              </details>
            ) : null}
            <div className="research-inline-actions">
              <button
                disabled={busy !== null || !source.configured}
                onClick={() => void test(source)}
              >
                {busy === source.id ? "测试中…" : "测试读取"}
              </button>
              <a href={source.url} target="_blank" rel="noreferrer">
                来源文档 ↗
              </a>
            </div>
            {probe[source.id] ? (
              <p role="status" className="research-notice">
                {probe[source.id]}
              </p>
            ) : null}
          </article>
        ))}
      </div>
      <div className="research-card">
        <h2>数据使用口径</h2>
        <p>
          实时终端继续使用金十、同花顺和原始帧回放。多市场研究使用来源的原生周期数据，独立保存于本机。切换来源会重新加载，不会合并不同来源的
          K 线。日频数据不是实时报价；连续合约不是可下单月份合约。
        </p>
        <p>
          过期缓存保留最后一次成功数据并提示故障。没有盘口、成交量或期权权限时，页面显示缺失，不用零值替代。
        </p>
      </div>
    </main>
  );
}

function MarketResearch({
  sources,
  onDataCenter,
}: {
  sources: ResearchSource[];
  onDataCenter: () => void;
}) {
  const [catalog, setCatalog] = useState<ResearchInstrument[]>([]);
  const [favorites, setFavorites] = useState(readFavorites);
  const [selected, setSelected] = useState(readSelection);
  const [filter, setFilter] = useState("");
  const [asset, setAsset] = useState<ResearchAsset | "all" | "favorites">(
    "all",
  );
  const [customCode, setCustomCode] = useState("");
  const [customSource, setCustomSource] = useState<ResearchSourceId>("tencent");
  const [customAsset, setCustomAsset] = useState<ResearchAsset>("equity");
  const [exchange, setExchange] = useState("SHFE");
  const catalogExchanges =
    customAsset === "equity"
      ? ["SSE", "SZSE", "BSE"]
      : [
          "SHFE",
          "DCE",
          "CZCE",
          "CFFEX",
          "INE",
          "GFEX",
          ...(customAsset === "option" ? ["SSE", "SZSE"] : []),
        ];
  const catalogExchange = catalogExchanges.includes(exchange)
    ? exchange
    : catalogExchanges[0];
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [period, setPeriod] = useState("1d");
  const [adjustment, setAdjustment] =
    useState<ResearchQuery["adjustment"]>("raw");
  const [page, setPage] = useState<ResearchPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [olderLoading, setOlderLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [tab, setTab] = useState<"analysis" | "ai" | "data" | "simulation">("analysis");
  const [strategies, setStrategies] = useState<ExpertStrategyId[]>([
    "ma-structure",
    "rsi",
  ]);
  const forceRefresh = useRef(false);
  const refreshPage = () => {
    forceRefresh.current = true;
    setRefresh((value) => value + 1);
  };
  const scope = `${identity(selected)}:${adjustment}`;
  const [workspaces, setWorkspaces] = useState<
    Record<string, ChartLayerWorkspace>
  >({});
  const workspace = useMemo(
    () => workspaces[scope] ?? readWorkspace(scope),
    [scope, workspaces],
  );
  const setWorkspace = useCallback(
    (update: (current: ChartLayerWorkspace) => ChartLayerWorkspace) =>
      setWorkspaces((current) => {
        const next = update(current[scope] ?? readWorkspace(scope));
        try {
          localStorage.setItem(
            `tracefang.research.drawings:${scope}`,
            JSON.stringify(next),
          );
        } catch {
          /* Drawings remain in memory. */
        }
        return { ...current, [scope]: next };
      }),
    [scope],
  );
  const history = useDrawingHistory(workspace, setWorkspace, scope);
  const [selectedDrawingId, setSelectedDrawingId] = useState<string | null>(null);
  const [continuousDrawing, setContinuousDrawing] = useState(false);
  const [tool, setTool] = useState<ExpertDrawingTool | null>(null);
  const [snap, setSnap] = useState<ExpertDrawingSnapMode>("weak");
  useEffect(() => { setTool(null); setSelectedDrawingId(null); }, [scope]);
  useEffect(() => { if (selectedDrawingId && !workspace.layers.some((layer) => layer.kind === "drawing" && layer.drawings.some((drawing) => drawing.id === selectedDrawingId))) setSelectedDrawingId(null); }, [workspace, selectedDrawingId]);
  const [hover, setHover] = useState<HoverCandle | null>(null);
  const [stream] = useState(() => new RealtimeBarStream());
  const controller = useRef<AbortController | null>(null);
  const olderController = useRef<AbortController | null>(null);
  const latestPage = useRef(page);
  latestPage.current = page;
  const pendingOlder = useRef(false);
  const generation = useRef(0);
  const source = sources.find((item) => item.id === selected.source);
  const query: ResearchQuery = useMemo(
    () => ({
      source: selected.source,
      symbol: selected.symbol,
      asset: selected.asset,
      period,
      adjustment,
      limit: 300,
    }),
    [selected, period, adjustment],
  );
  const queryKey = JSON.stringify(query);
  useEffect(() => {
    void researchApi
      .catalog()
      .then(setCatalog)
      .catch((failure) => setCatalogError(failure.message));
  }, []);
  useEffect(() => {
    try {
      localStorage.setItem(
        "tracefang.research.selection",
        JSON.stringify(selected),
      );
    } catch {
      /* Session selection remains usable. */
    }
  }, [selected]);
  useEffect(() => {
    try {
      localStorage.setItem(
        "tracefang.research.favorites",
        JSON.stringify(favorites),
      );
    } catch {
      /* Session favorites remain usable. */
    }
  }, [favorites]);
  useEffect(() => {
    controller.current?.abort();
    olderController.current?.abort();
    pendingOlder.current = false;
    const abort = new AbortController();
    controller.current = abort;
    const request = ++generation.current;
    setLoading(true);
    setOlderLoading(false);
    setError(null);
    setPage(null);
    setHover(null);
    setTool(null);
    stream.reset(queryKey);
    const forced = forceRefresh.current;
    forceRefresh.current = false;
    void researchApi
      .bars(query, abort.signal, forced)
      .then((value) => {
        if (request === generation.current) {
          latestPage.current = value;
          setPage(value);
        }
      })
      .catch((failure) => {
        if (!abort.signal.aborted) setError(failure.message);
      })
      .finally(() => {
        if (request === generation.current) setLoading(false);
      });
    return () => {
      abort.abort();
      olderController.current?.abort();
    };
  }, [queryKey, refresh, stream]);
  const older = useCallback(async () => {
    const current = latestPage.current;
    if (pendingOlder.current)
      return { state: "busy" as const, added: 0, advancedMinutes: 0 };
    if (!current?.next_before || !canRequestResearchHistory(current, false))
      return { state: "exhausted" as const, added: 0, advancedMinutes: 0 };
    pendingOlder.current = true;
    setOlderLoading(true);
    setError(null);
    const request = generation.current;
    const abort = new AbortController();
    olderController.current = abort;
    try {
      const result = await researchApi.bars(
        { ...query, before: current.next_before },
        abort.signal,
      );
      if (request !== generation.current)
        return { state: "busy" as const, added: 0, advancedMinutes: 0 };
      const labelOnly=isResearchLabelOnly(current)&&isResearchLabelOnly(result);
      if(!labelOnly&&(!current.authority_snapshot_id||!result.authority_snapshot_id))throw new Error("研究页面缺少可核验的输入版本，无法合并历史");
      const merged=labelOnly?null:await researchApi.mergeAuthority(current.authority_snapshot_id!,result.authority_snapshot_id!,abort.signal);
      if(request!==generation.current)return {state:"busy" as const,added:0,advancedMinutes:0};
      const displayRows=labelOnly?mergeResearchDisplayRows(current.items,result.items):null;
      const rows = new Map(current.items.map((row) => [row.open_time, row]));
      result.items.forEach((row) => {
        if (!rows.has(row.open_time)) rows.set(row.open_time, row);
      });
      const added = rows.size - current.items.length;
      const step = resolveResearchHistoryStep(current.next_before, result.next_before, added);
      const updated = {
        ...current,
        authority_snapshot_id:merged?.authority_snapshot_id??null,
        authority_manifest:merged?.authority_manifest??null,
        fetched_at:merged?.authority_manifest.fetched_at??result.fetched_at,
        items: displayRows??[...rows.values()].sort((a, b) =>
          a.open_time.localeCompare(b.open_time),
        ),
        next_before: result.next_before,
        warnings: [...new Set([...current.warnings, ...result.warnings,labelOnly?"已扩展原标签图表；区间未核验，没有生成可执行指标、AI或模拟版本。":"已发布包含加载历史的新研究版本，图表、指标、AI和模拟共享该输入；来源页面独立读取，不代表上游共同事务，完整预热范围仍未知。"])],
        cache_state:
          result.cache_state === "stale"
            ? ("stale" as const)
            : current.cache_state,
      };
      latestPage.current = updated;
      setPage(updated);
      return step;
    } catch (failure) {
      if (!abort.signal.aborted)
        setError(failure instanceof Error ? failure.message : String(failure));
      return { state: "failed" as const, added: 0, advancedMinutes: 0 };
    } finally {
      if (request === generation.current) {
        pendingOlder.current = false;
        setOlderLoading(false);
      }
    }
  }, [query]);
  const candles = page?.items ?? [];
  const sourceRowsByTime = useMemo(() => new Map(candles.map(row => [Date.parse(row.open_time) / 1000, row])), [candles]);
  const hoverSource = hover ? sourceRowsByTime.get(hover.time) : null;
  const researchReference=page?.authority_snapshot_id?{research_snapshot_id:page.authority_snapshot_id,research_adjustment:adjustment,research_asset:selected.asset}:undefined;
  const quant=useQuantSnapshot(selected.symbol,selected.source,period,`${page?.authority_snapshot_id??''}:${strategies.join(',')}`,!!researchReference,researchReference,strategies);
  const analysis=useMemo(()=>projectQuantAnalysis(quant.snapshot),[quant.snapshot]);
  const indicatorSeries=useMemo(()=>projectQuantSeries(quant.snapshot),[quant.snapshot]);
  const authorityOverlays=useMemo(()=>projectQuantOverlays(quant.snapshot),[quant.snapshot]);
  const authorityTrends=useMemo(()=>projectQuantTrendLines(quant.snapshot),[quant.snapshot]);
  const layers = useMemo(
    () =>
      buildChartLayers(
        {
          ...workspace,
          layers: workspace.layers.map((layer) =>
            layer.kind === "indicator"
              ? { ...layer, visible: strategies.includes(layer.indicatorId) }
              : layer,
          ),
        },
        {
          indicatorSeries,
          sessionBands: [],
          eventMarkers: [],
          priceLevels: analysis.levels,
          valueZones: analysis.valueZones,
          trendLines: authorityTrends,
          pricePatterns: analysis.pricePatterns.slice(-4),
          marketStructureEvents: analysis.marketStructureEvents,
          overlaySeries: authorityOverlays,
        },
      ),
    [workspace, indicatorSeries, analysis, strategies,authorityOverlays,authorityTrends],
  );
  const last = candles.at(-1);
  const pick = (item: ResearchInstrument) => {
    setSelected(item);
    setPeriod("1d");
    setAdjustment("raw");
  };
  const universe = [
    ...new Map(
      [...catalog, ...favorites].map((item) => [identity(item), item]),
    ).values(),
  ];
  const visible = (asset === "favorites" ? favorites : universe).filter(
    (item) =>
      (asset === "all" || asset === "favorites" || item.asset === asset) &&
      `${item.symbol} ${item.name}`
        .toLowerCase()
        .includes(filter.toLowerCase()),
  );
  const favorite = favorites.some(
    (item) => identity(item) === identity(selected),
  );
  const syncCatalog = async () => {
    setCatalogBusy(true);
    setCatalogError(null);
    try {
      const rows = await researchApi.contracts(
        customAsset,
        catalogExchange,
        customSource,
      );
      setCatalog((current) => [
        ...new Map(
          [...current, ...rows].map((item) => [identity(item), item]),
        ).values(),
      ]);
      setAsset(customAsset);
      setFilter("");
    } catch (failure) {
      setCatalogError(
        failure instanceof Error ? failure.message : String(failure),
      );
    } finally {
      setCatalogBusy(false);
    }
  };
  return (
    <main className="research-market">
      <aside className="instrument-browser">
        <header>
          <h2>市场目录</h2>
          <small>{universe.length} 个观察品种</small>
        </header>
        <label className="research-search">
          <Search size={16} />
          <input
            aria-label="搜索股票期货期权"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="名称 / 代码"
          />
        </label>
        <div className="asset-filters">
          {(["all", "favorites", ...Object.keys(ASSETS)] as const).map(
            (item) => (
              <button
                key={item}
                className={asset === item ? "is-active" : ""}
                onClick={() => setAsset(item as typeof asset)}
              >
                {item === "all"
                  ? "全部"
                  : item === "favorites"
                    ? "自选"
                    : ASSETS[item as ResearchAsset]}
              </button>
            ),
          )}
        </div>
        <div className="instrument-results">
          {visible.slice(0, 300).map((item) => (
            <button
              className={
                identity(item) === identity(selected) ? "is-selected" : ""
              }
              key={identity(item)}
              onClick={() => pick(item)}
            >
              <span>
                <strong>{item.name}</strong>
                <small>{item.symbol}</small>
              </span>
              <span>
                <small>{ASSETS[item.asset]}</small>
                <small>
                  {sources.find((entry) => entry.id === item.source)?.name ??
                    item.source}
                </small>
              </span>
            </button>
          ))}
          {!visible.length ? (
            <p className="muted">
              没有匹配品种。可在下方按代码打开，或同步合约目录。
            </p>
          ) : null}
        </div>
        <details className="research-add-symbol">
          <summary>按代码打开 / 同步目录</summary>
          <label>
            来源
            <select
              value={customSource}
              onChange={(event) => {
                const next = event.target.value as ResearchSourceId;
                setCustomSource(next);
                setCustomAsset(
                  sources.find((item) => item.id === next)?.assets[0] ??
                    "equity",
                );
              }}
            >
              {sources.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            资产类别
            <select
              value={customAsset}
              onChange={(event) =>
                setCustomAsset(event.target.value as ResearchAsset)
              }
            >
              {(
                sources.find((item) => item.id === customSource)?.assets ?? [
                  "equity",
                ]
              ).map((value) => (
                <option key={value} value={value}>
                  {ASSETS[value]}
                </option>
              ))}
            </select>
          </label>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!customCode.trim()) return;
              const item: ResearchInstrument = {
                source: customSource,
                symbol: customCode.trim().toUpperCase(),
                name: customCode.trim().toUpperCase(),
                asset: customAsset,
                currency: customSource === "alpaca" ? "USD" : "CNY",
              };
              setCatalog((current) => [
                ...current.filter(
                  (entry) => identity(entry) !== identity(item),
                ),
                item,
              ]);
              pick(item);
            }}
          >
            <label>
              证券代码
              <input
                maxLength={40}
                required
                value={customCode}
                onChange={(event) => setCustomCode(event.target.value)}
                placeholder={EXAMPLES[customSource]}
              />
            </label>
            <button type="submit">打开行情</button>
          </form>
          {(customSource === "tushare" &&
            ["equity", "future", "option"].includes(customAsset)) ||
          (customSource === "akshare" &&
            ["future", "option"].includes(customAsset)) ? (
            <>
              <label>
                交易所
                <select
                  value={catalogExchange}
                  onChange={(event) => setExchange(event.target.value)}
                >
                  {catalogExchanges.map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <button disabled={catalogBusy} onClick={() => void syncCatalog()}>
                {catalogBusy ? "同步中…" : "同步合约目录"}
              </button>
              <small>
                每次同步一个交易所，最多 6000 条；未列出的合约可按代码打开。
                {customSource === "akshare"
                  ? "期货目录仅列出具有上市期权的商品标的月份合约。"
                  : ""}
              </small>
            </>
          ) : null}
        </details>
        {catalogError ? (
          <p role="alert" className="research-error">
            {catalogError}
          </p>
        ) : null}
      </aside>
      <section className="research-chart-column">
        <header className="research-symbol-heading">
          <div>
            <small>
              {ASSETS[selected.asset]} / {source?.name ?? selected.source}
            </small>
            <h1>
              {selected.name} <span>{selected.symbol}</span>
            </h1>
          </div>
          <div className="research-quote">
            <strong>
              {format(
                last?.close,
                selected.asset === "option"
                  ? 4
                  : selected.asset === "etf"
                    ? 3
                    : 2,
              )}
            </strong>
            <small>{selected.currency} · {isResearchLabelOnly(page) ? "来源价格 / 原始标签" : "最近收盘/快照"}</small>
          </div>
          <button
            title={favorite ? "移出自选" : "加入自选"}
            aria-label={favorite ? "移出自选" : "加入自选"}
            onClick={() =>
              setFavorites((current) =>
                favorite
                  ? current.filter(
                      (item) => identity(item) !== identity(selected),
                    )
                  : [...current, selected],
              )
            }
          >
            <Star size={17} fill={favorite ? "currentColor" : "none"} />
          </button>
        </header>
        <div className="research-period-bar">
          <div>
            {sourcePeriods(source, selected.asset).map((value) => (
              <button
                key={value}
                className={period === value ? "is-active" : ""}
                onClick={() => setPeriod(value)}
              >
                {PERIODS[value]}
              </button>
            ))}
          </div>
          {(/^\d{6}\.(SH|SZ)$/.test(selected.symbol) &&
            ["equity", "etf", "index"].includes(selected.asset)) ||
          (selected.asset === "future" &&
            /^[A-Z]{1,3}\d{1,4}$/.test(selected.symbol)) ? (
            <label>
              来源
              <select
                aria-label="切换行情来源"
                value={selected.source}
                onChange={(event) =>
                  pick({
                    ...selected,
                    source: event.target.value as ResearchSourceId,
                  })
                }
              >
                {sources
                  .filter(
                    (item) =>
                      item.assets.includes(selected.asset) &&
                      (selected.asset === "future"
                        ? ["sina", "akshare"]
                        : ["tencent", "eastmoney", "tushare", "akshare"]
                      ).includes(item.id),
                  )
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
              </select>
            </label>
          ) : null}
          <label>
            复权
            <select
              value={adjustment}
              disabled={
                !["eastmoney", "tencent", "akshare"].includes(
                  selected.source,
                ) || !["equity", "etf"].includes(selected.asset)
              }
              onChange={(event) =>
                setAdjustment(event.target.value as typeof adjustment)
              }
            >
              <option value="raw">不复权</option>
              <option value="forward">前复权</option>
              <option value="backward">后复权</option>
            </select>
          </label>
          <button onClick={refreshPage} disabled={loading} title="刷新行情">
            <RefreshCw size={15} />
          </button>
          <button
            disabled={!candles.length}
            title="导出当前 K 线"
            onClick={() =>
              downloadText(
                `${selected.symbol}-${period}.csv`,
                [
                  "display_time,open,high,low,close,volume,source,adjustment,source_label,clock_policy_verified,span_start_unknown",
                  ...candles.map((row) =>
                    [
                      row.open_time,
                      row.open,
                      row.high,
                      row.low,
                      row.close,
                      row.volume ?? "",
                      selected.source,
                      adjustment,
                      (row.source.raw_payload as Record<string,unknown>|undefined)?.source_label??"",
                      (row.source.raw_payload as Record<string,unknown>|undefined)?.clock_policy_verified??"",
                      (row.source.raw_payload as Record<string,unknown>|undefined)?.span_start_unknown??"",
                    ].join(","),
                  ),
                ].join("\n"),
                "text/csv",
              )
            }
          >
            <Download size={15} />
          </button>
        </div>
        <DrawingControls
          workspace={workspace}
          tool={tool}
          onTool={setTool}
          snap={snap}
          onSnap={setSnap}
          history={history}
          selectedId={selectedDrawingId} onSelect={setSelectedDrawingId}
          continuous={continuousDrawing} onContinuous={setContinuousDrawing}
        />
        <div className="research-chart-readout">
          {hover
            ? `${new Date(hover.time * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })}${isResearchLabelOnly(page) ? "（来源标签）" : ""} · O ${format(hoverSource?.open)}  H ${format(hoverSource?.high)}  L ${format(hoverSource?.low)}  C ${format(hoverSource?.close)}`
            : "拖动查看历史 · 滚轮缩放 · 点击画线可编辑端点"}
        </div>
        <div className="research-chart-canvas">
          <MarketChart
            key={queryKey}
            candles={candles}
            realtimeBarStream={stream}
            realtimeBarStreamKey={queryKey}
            period={chartPeriodById(
              (period === "1M" ? "1mo" : period) as ChartPeriodId,
            )}
            livePrice={last ? Number(last.close) : null}
            referencePrice={null}
            timelineResolutionSeconds={60}
            priceStatusLabel="来源快照"
            displayTimeZone={
              selected.source === "alpaca"
                ? "America/New_York"
                : "Asia/Shanghai"
            }
            priceDigits={
              selected.asset === "option" ? 4 : selected.asset === "etf" ? 3 : 2
            }
            marketPhase="closed"
            marketSchedule={null}
            historyLoading={loading || olderLoading}
            historyResetKey={refresh}
            onRequestOlderHistory={older}
            onRequestHistoryGap={async () => undefined}
            onHover={setHover}
            layers={layers}
            drawingTool={tool}
            drawingSnapMode={snap}
            selectedDrawingId={selectedDrawingId} onDrawingSelect={setSelectedDrawingId}
            onDrawingCommit={(drawing) => {
              history.change((current) =>
                appendDrawingToActiveLayer(current, drawing),
              );
              setSelectedDrawingId(drawing.id);
              if (!continuousDrawing) setTool(null);
            }}
            onDrawingUpdate={(drawing) =>
              history.change((current) =>
                replaceDrawing(current, drawing, drawing.id),
              )
            }
          />
          {loading ? (
            <div className="research-chart-empty" role="status">
              <RefreshCw className="spin" size={22} />
              <strong>
                正在读取{source?.name ?? "来源"}原生{PERIODS[period]}
              </strong>
              <small>优先读取本机缓存，首次访问需连接数据源。</small>
            </div>
          ) : !candles.length ? (
            <div className="research-chart-empty">
              <Database size={28} />
              <strong>{error ? "行情暂时无法读取" : "当前范围没有行情"}</strong>
              <p>{error ?? page?.empty_reason}</p>
              <div>
                <button onClick={refreshPage}>重试</button>
                <button onClick={onDataCenter}>检查数据源</button>
              </div>
            </div>
          ) : null}
        </div>
        <div
          className={`research-data-strip ${page?.cache_state === "stale" ? "is-stale" : ""}`}
          role="status"
        >
          <span>
            {loading
              ? "读取中"
              : page?.cache_state === "stale"
                ? "过期缓存"
                : page?.cache_state === "cached"
                  ? "本机缓存"
                  : page
                    ? "来源快照"
                    : "未连接"}
          </span>
          <span>
            图表 {candles.length} 根 · 完整权威输入 {page?.authority_manifest?.row_count ?? "—"} 行 · 指标确认 {quant.snapshot?.evidence.confirmed_count ?? (page?.authority_unavailable_reason?"不可用":"计算中")} 根 · {page?.feed ?? selected.source}
          </span>
          <span>
            截止{" "}
            {page?.data_as_of
              ? new Date(page.data_as_of).toLocaleString("zh-CN", {
                  timeZone:
                    selected.source === "alpaca"
                      ? "America/New_York"
                      : "Asia/Shanghai",
                })
              : "—"}
          </span>
          <button
            disabled={
              !canRequestResearchHistory(page, olderLoading)
            }
            onClick={() => void older()}
          >
            {olderLoading ? "读取历史中…" : "加载更早"}
          </button>
        </div>
        {error && candles.length ? (
          <p className="research-error" role="alert">
            {error} <button onClick={refreshPage}>重试</button>
          </p>
        ) : null}
      </section>
      <aside className="research-inspector">
        <div className="research-tabs" role="tablist" aria-label="研究面板">
          {(["analysis", "ai", "data", "simulation"] as const).map((value) => (
            <button
              key={value}
              role="tab"
              aria-selected={tab === value}
              className={tab === value ? "is-active" : ""}
              onClick={() => setTab(value)}
            >
              {{ analysis: "技术研究", ai: "AI 解读", data: "数据质量", simulation:"模拟回测" }[value]}
            </button>
          ))}
        </div>
        <div className="research-inspector-body">
          {page?.authority_unavailable_reason?<p className="research-notice" role="status">{page.authority_unavailable_reason}</p>:null}
          {tab === "ai" ? (
            <ResearchAiPanel key={queryKey} query={query} page={page} snapshot={quant.snapshot} />
          ) : tab === "simulation" ? (
            researchReference?<QuantSimulationPanel code={selected.symbol} sourceId={selected.source} period={period} strategies={strategies} unit={page?.currency??"品种计价单位"} research={researchReference} onClose={()=>setTab("analysis")}/>:<p role="status">{page?.authority_unavailable_reason??"服务端研究输入尚未发布，请刷新来源。"}</p>
          ) : tab === "data" ? (
            <>
              <h2>数据证据</h2>
              <dl>
                <dt>来源</dt>
                <dd>{source?.name}</dd>
                <dt>读取时间</dt>
                <dd>
                  {page?.fetched_at
                    ? new Date(page.fetched_at).toLocaleString()
                    : "—"}
                </dd>
                <dt>周期 / 复权</dt>
                <dd>
                  {PERIODS[period]} /{" "}
                  {
                    { raw: "不复权", forward: "前复权", backward: "后复权" }[
                      adjustment
                    ]
                  }
                </dd>
                <dt>成交量单位</dt>
                <dd>{page?.volume_unit ?? "—"}</dd>
                <dt>无效行</dt>
                <dd>{page?.rejected_rows ?? "—"}</dd>
                {selected.asset === "future" ? (
                  <>
                    <dt>最新持仓量</dt>
                    <dd>{format(last?.open_interest, 0)}</dd>
                  </>
                ) : null}
                <dt>图表已收盘样本</dt>
                <dd>{candles.filter(row=>row.state==='final').length}</dd>
                <dt>权威研究输入总行数</dt>
                <dd>{page?.authority_manifest?.row_count ?? "—"}</dd>
                <dt>指标 / AI 确认输入</dt>
                <dd>{quant.snapshot?.evidence.confirmed_count ?? (page?.authority_unavailable_reason?"不可用":"计算中")}</dd>
              </dl>
              {page?.warnings.map((warning) => (
                <p className="research-notice" key={warning}>
                  {warning}
                </p>
              ))}
              <p className="muted">
                缺失时间不补零，不跨来源拼接。日期是来源标记的周期日期，读取时间另列。来源快照不承诺实时更新。
              </p>
              {page?.authority_manifest && page.authority_manifest.row_count !== candles.length ? <p className="research-notice">图表当前显示 {candles.length} 根；权威输入保留来源返回的 {page.authority_manifest.row_count} 行（包含分页边界样本）。指标、AI 和模拟使用完整权威输入，分页只控制图表展示。</p> : null}
              <button onClick={onDataCenter}>打开数据中心</button>
            </>
          ) : (
            <>
              <h2>研究工具</h2>
              <p className="muted">
                指标来自同一服务端输入版本；勾选工具控制显示。
              </p>
              <div className="research-strategy-groups">
                {[
                  {
                    title: "趋势与结构",
                    items: [
                      ["ma-structure", "均线结构"],
                      ["structure", "价格形态"],
                      ["bollinger", "布林带"],
                    ],
                  },
                  {
                    title: "动量与节奏",
                    items: [
                      ["rsi", "RSI 14"],
                      ["macd", "MACD"],
                      ["kdj", "KDJ"],
                    ],
                  },
                ].map((group) => (
                  <section key={group.title}>
                    <h3>{group.title}</h3>
                    {group.items.map(([id, label]) => (
                      <label key={id}>
                        <input
                          type="checkbox"
                          checked={strategies.includes(id as ExpertStrategyId)}
                          onChange={() =>
                            setStrategies((current) =>
                              current.includes(id as ExpertStrategyId)
                                ? current.filter((item) => item !== id)
                                : [...current, id as ExpertStrategyId],
                            )
                          }
                        />
                        {label}
                      </label>
                    ))}
                  </section>
                ))}
              </div>
              <p className="muted" role="status">{quant.error?`指标不可用：${quant.error}`:quant.progress?.state==='building'?`服务端计算 · ${quant.progress.processed_bars} 根`:quant.snapshot?`服务端同版本指标 · ${quant.snapshot.evidence.input_hash.slice(0,16)} · 预热${quant.snapshot.evidence.warmup_complete?'完整':'范围未知'}`:(page?.authority_unavailable_reason??'服务端研究输入尚未发布，请刷新来源。')}</p>
              <h2>
                已确认信号 <span>{analysis.signals.length}</span>
              </h2>
              {analysis.signals.slice(0, 8).map((signal) => (
                <article
                  className={`research-signal is-${signal.direction}`}
                  key={signal.id}
                >
                  <strong>{signal.title}</strong>
                  <p>{signal.detail}</p>
                  <small>{signal.evidence.join(" · ")}</small>
                </article>
              ))}
              {!analysis.signals.length ? (
                <p className="muted">
                  服务端同版本指标就绪后显示已确认信号。
                </p>
              ) : null}
              <h2>关键价位</h2>
              {analysis.levels.slice(0, 5).map((level) => (
                <p key={level.id} className="research-level">
                  <span>{level.label}</span>
                  <strong>{format(level.price)}</strong>
                </p>
              ))}
              <p className="muted">
                当前画线图层：{activeDrawingLayer(workspace).name}
                。画线按来源、品种与复权方式保存，切换周期可继续使用。
              </p>
            </>
          )}
        </div>
      </aside>
    </main>
  );
}

export default function ResearchTerminal() {
  const [section, setSection] = useState<
    "research" | "realtime" | "options" | "data"
  >("research");
  const [sources, setSources] = useState<ResearchSource[]>([]);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    void researchApi
      .sources()
      .then((value) => {
        setSources(value);
        setError(null);
      })
      .catch((failure) => setError(failure.message));
  }, []);
  useEffect(reload, [reload]);
  return (
    <div className="research-terminal">
      <nav className="terminal-navigation" aria-label="工作区">
        <a
          className="terminal-wordmark"
          href="#"
          onClick={(event) => {
            event.preventDefault();
            setSection("research");
          }}
        >
          TraceFang <span>研究终端</span>
        </a>
        <div>
          {(
            [
              {
                id: "research",
                name: "多市场研究",
                icon: <Search size={16} />,
              },
              {
                id: "realtime",
                name: "实时终端",
                icon: <CandlestickChart size={16} />,
              },
              {
                id: "options",
                name: "期权策略",
                icon: <FlaskConical size={16} />,
              },
              { id: "data", name: "数据中心", icon: <Database size={16} /> },
            ] as const
          ).map((item) => (
            <button
              key={item.id}
              aria-current={section === item.id ? "page" : undefined}
              className={section === item.id ? "is-active" : ""}
              onClick={() => setSection(item.id)}
            >
              {item.icon}
              {item.name}
            </button>
          ))}
        </div>
        <small>研究 · 验证 · 决策</small>
      </nav>
      {error && section !== "realtime" ? (
        <div className="research-service-error" role="alert">
          {error}
          <button onClick={reload}>重试连接</button>
        </div>
      ) : null}
      <div className={`research-content section-${section}`}>
        {section === "research" ? (
          <MarketResearch
            sources={sources}
            onDataCenter={() => {
              reload();
              setSection("data");
            }}
          />
        ) : section === "realtime" ? (
          <Suspense fallback={<p>加载实时终端…</p>}>
            <RealtimeApp onOpenOptions={()=>setSection("options")} />
          </Suspense>
        ) : section === "options" ? (
          <Suspense fallback={<p>加载期权工作台…</p>}>
            <OptionsLab />
          </Suspense>
        ) : (
          <DataCenter sources={sources} reload={reload} />
        )}
      </div>
    </div>
  );
}
