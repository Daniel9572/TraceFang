// Real ExpertModeWorkspace and MarketChart composition, with explicit local synthetic test inputs.
import { useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { ExpertModeWorkspace } from "../src/ExpertModeWorkspace";
import { chartLayerStorageKey, readChartLayerWorkspace, type ChartLayerWorkspace } from "../src/chartLayers";
import type { ChartPeriodId } from "../src/chartPeriods";
import type { ExpertIndicatorSeriesView } from "../src/expertTypes";
import { candles, stream } from "./drawing-fixture";
import "../src/styles.css";
import "../src/research.css";
const indicatorSeries: ExpertIndicatorSeriesView = { historyKey: null, revision: 1, offset: 0, length: 0, visibleLength: 0,
  changedFrom: 0, bars: [], macd: { value: [], signal: [], histogram: [] }, kdj: { k: [], d: [], j: [] }, rsi: { value: [] } };
function Fixture() {
  const [code, setCode] = useState("TEST-DRAWING-A");
  const [period, setPeriod] = useState<ChartPeriodId>("1m");
  const [workspaces, setWorkspaces] = useState<Record<string, ChartLayerWorkspace>>({});
  const workspace = useMemo(() => workspaces[code] ?? readChartLayerWorkspace(localStorage.getItem(chartLayerStorageKey(code))), [workspaces, code]);
  const update = useCallback((change: (value: ChartLayerWorkspace) => ChartLayerWorkspace) => setWorkspaces((current) => {
    const existing = current[code] ?? readChartLayerWorkspace(localStorage.getItem(chartLayerStorageKey(code)));
    const next = change(existing);
    return next === existing ? current : { ...current, [code]: next };
  }), [code]);
  useEffect(() => { if (workspaces[code]) localStorage.setItem(chartLayerStorageKey(code), JSON.stringify(workspaces[code])); }, [workspaces, code]);
  // Match the production ResearchTerminal ancestry as well as its imported styles.
  return <div className="research-terminal"><pre id="test-product-state" hidden>{JSON.stringify({ code, storageKey: chartLayerStorageKey(code), drawings: workspace.layers.flatMap((layer) => layer.kind === "drawing" ? layer.drawings : []) })}</pre><div className="research-content section-realtime"><ExpertModeWorkspace code={code} instrumentName="合成测试行情 · 非生产" unit="测试价格"
    candles={candles} realtimeBarStream={stream} realtimeBarStreamKey={code} periodId={period} livePrice={Number(candles.at(-1)!.close)}
    change={null} changePercent={null} observedAt={candles.at(-1)!.open_time} referencePrice={null} timelineResolutionSeconds={60}
    priceDigits={2} marketPhase="closed" marketSchedule={null} sourceLabel="固定测试数据" sourceId="test-fixture" sourceState="waiting"
    liveIndicatorSeries={indicatorSeries} marketEvents={[]} marketEventsLoading={false} marketEventsError={null}
    layerWorkspace={workspace} onLayerWorkspaceChange={update} historyLoading={false} historyActivityVisible={false}
    loading={false} error={null} onPeriodChange={setPeriod} onRequestOlderHistory={async () => ({ state: "exhausted", added: 0, advancedMinutes: 0 })}
    onRequestHistoryGap={async () => {}} onOpenMarket={() => setCode(code === "TEST-DRAWING-A" ? "TEST-DRAWING-B" : "TEST-DRAWING-A")} /></div></div>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
