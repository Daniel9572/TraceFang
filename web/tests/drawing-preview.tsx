// Manual browser acceptance fixture. Synthetic candles are local test data, never a production feed.
import { useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import { DrawingControls, replaceDrawing, useDrawingHistory } from "../src/DrawingControls";
import { MarketChart } from "../src/MarketChart";
import { chartPeriodById } from "../src/chartPeriods";
import { appendDrawingToActiveLayer, createDefaultChartLayerWorkspace, readChartLayerWorkspace, setChartLayerVisibility, type ChartLayer, type ChartLayerWorkspace } from "../src/chartLayers";
import type { ExpertDrawingSnapMode, ExpertDrawingTool } from "../src/expertTypes";
import { candles, stream } from "./drawing-fixture";
import "../src/styles.css";
import "../src/research.css";

function Fixture() {
  const [scope, setScope] = useState("TEST-A");
  const [workspaces, setWorkspaces] = useState<Record<string, ChartLayerWorkspace>>({});
  const workspace = useMemo(() => workspaces[scope] ?? readChartLayerWorkspace(localStorage.getItem(`drawing-fixture:${scope}`)), [workspaces, scope]);
  const change = useCallback((update: (current: ChartLayerWorkspace) => ChartLayerWorkspace) => setWorkspaces((current) => {
    const next = update(current[scope] ?? workspace);
    localStorage.setItem(`drawing-fixture:${scope}`, JSON.stringify(next));
    return { ...current, [scope]: next };
  }), [scope, workspace]);
  const history = useDrawingHistory(workspace, change, scope);
  const [tool, setTool] = useState<ExpertDrawingTool | null>(null);
  const [snap, setSnap] = useState<ExpertDrawingSnapMode>("off");
  const [selected, setSelected] = useState<string | null>(null);
  const [continuous, setContinuous] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const [cancelEvidence, setCancelEvidence] = useState("");
  const [cancelMode, setCancelMode] = useState("off");
  useEffect(() => { setTool(null); setSelected(null); }, [scope]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const pointer = (event: PointerEvent) => {
      if (cancelMode === "off" || !(event.target instanceof Element) || !event.target.closest(".drawing-objects,.chart-drawing-surface")) return;
      const pointerId = event.pointerId;
      timer = setTimeout(() => {
        const target = [...document.querySelectorAll<SVGSVGElement | HTMLDivElement>(".drawing-objects,.chart-drawing-surface")].find((item) => item.hasPointerCapture(pointerId));
        setCancelEvidence(`${cancelMode}: pointer=${pointerId}, captured=${Boolean(target)}`);
        if (!target) return;
        if (cancelMode === "cancel") target.dispatchEvent(new PointerEvent("pointercancel", { pointerId, bubbles: true }));
        target.releasePointerCapture(pointerId);
      }, 20);
    };
    document.addEventListener("pointerdown", pointer, true);
    return () => { clearTimeout(timer); document.removeEventListener("pointerdown", pointer, true); };
  }, [cancelMode]);
  const layers: ChartLayer[] = workspace.layers.flatMap((definition) => definition.kind === "drawing" || definition.kind === "price" ? [{ kind: definition.kind, definition } as ChartLayer] : []);
  const drawings = workspace.layers.flatMap((layer) => layer.kind === "drawing" ? layer.drawings : []);
  return <div className="research-terminal" style={{ width: narrow ? 360 : "100%", maxWidth: "100%", margin: "auto", overflow: "auto" }}>
    <header style={{ display: "flex", flexWrap: "wrap", gap: 8, padding: 8 }}><strong>绘图交互测试 · 合成测试行情，非生产数据</strong><select aria-label="指针取消测试" value={cancelMode} onChange={(event) => { setCancelEvidence(""); setCancelMode(event.target.value); }}><option value="off">指针测试关闭</option><option value="cancel">20ms 模拟 pointercancel</option><option value="lost">20ms 释放真实指针捕获</option></select>
      <button onClick={() => setScope(scope === "TEST-A" ? "TEST-B" : "TEST-A")}>切换测试品种 ({scope})</button>
      <button onClick={() => setNarrow(!narrow)}>窄屏测试</button>
      <button onClick={() => history.change((current) => setChartLayerVisibility(current, current.activeDrawingLayerId, !current.layers.find((layer) => layer.id === current.activeDrawingLayerId)!.visible))}>显隐绘图层</button>
      <button onClick={() => { setTool(null); setSelected(null); change(() => createDefaultChartLayerWorkspace()); }}>重置测试对象</button>
    </header>
    <DrawingControls workspace={workspace} tool={tool} onTool={setTool} snap={snap} onSnap={setSnap} history={history}
      selectedId={selected} onSelect={setSelected} continuous={continuous} onContinuous={setContinuous} />
    <div style={{ height: 500, minHeight: 300, position: "relative" }}>
      <MarketChart key={scope} candles={candles} realtimeBarStream={stream} realtimeBarStreamKey={scope} period={chartPeriodById("1m")}
        livePrice={null} referencePrice={null} timelineResolutionSeconds={60} priceDigits={2} marketPhase="closed" marketSchedule={null}
        historyLoading={false} onRequestOlderHistory={async () => ({ state: "exhausted", added: 0, advancedMinutes: 0 })} onRequestHistoryGap={async () => {}} onHover={() => {}}
        layers={layers} drawingTool={tool} drawingSnapMode={snap} selectedDrawingId={selected} onDrawingSelect={setSelected}
        onDrawingUpdate={(drawing) => history.change((current) => replaceDrawing(current, drawing, drawing.id))}
        onDrawingCommit={(drawing) => { history.change((current) => appendDrawingToActiveLayer(current, drawing)); setSelected(drawing.id); if (!continuous) setTool(null); }} />
    </div>
    <output id="test-cancel-evidence">{cancelEvidence}</output><details open><summary>测试对象与锚点 ({drawings.length})</summary><pre id="test-drawings" style={{ whiteSpace: "pre-wrap", fontSize: 11 }}>{JSON.stringify(drawings, null, 2)}</pre></details>
  </div>;
}
createRoot(document.getElementById("root")!).render(<Fixture />);
