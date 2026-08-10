import assert from "node:assert/strict";
import test from "node:test";

import {
  activeDrawingLayer,
  addDrawingLayer,
  appendDrawingToActiveLayer,
  buildChartLayers,
  CHART_PRICE_LAYER_ID,
  chartLayerStorageKey,
  chartLayerCapabilities,
  createDefaultChartLayerWorkspace,
  deleteDrawingLayer,
  moveChartLayer,
  readChartLayerWorkspace,
  resizeIndicatorLayer,
  setChartLayerVisibility,
} from "../src/chartLayers.ts";
import type { ExpertDrawing, ExpertIndicatorSeriesView } from "../src/expertTypes.ts";

const drawing: ExpertDrawing = {
  id: "drawing:test",
  type: "trend",
  start: { time: 100, price: 2400 },
  end: { time: 200, price: 2410 },
  color: "#fff",
  label: "趋势线",
};

const emptyIndicatorSeries: ExpertIndicatorSeriesView = {
  historyKey: null,
  revision: 1,
  offset: 0,
  length: 0,
  visibleLength: 0,
  changedFrom: 0,
  bars: [],
  macd: { value: [], signal: [], histogram: [] },
  kdj: { k: [], d: [], j: [] },
  rsi: { value: [] },
};

test("scopes the shared layer workspace by instrument rather than display mode", () => {
  assert.equal(chartLayerStorageKey("XAUUSD"), "market-chart-layers-v1:XAUUSD");
  assert.notEqual(chartLayerStorageKey("XAUUSD"), chartLayerStorageKey("XAGUSD"));
});

test("creates an immutable price base with managed drawing and read-only indicator layers", () => {
  const workspace = createDefaultChartLayerWorkspace();
  const price = workspace.layers.find((layer) => layer.kind === "price");
  assert.equal(price?.id, CHART_PRICE_LAYER_ID);
  assert.equal(price?.visible, true);
  assert.equal(chartLayerCapabilities("price").canHide, false);
  assert.equal(chartLayerCapabilities("drawing").canEditContent, true);
  assert.equal(chartLayerCapabilities("indicator").canEditContent, false);
  assert.deepEqual(
    workspace.layers.filter((layer) => layer.kind === "indicator").map((layer) => layer.indicatorId),
    ["rsi", "kdj", "macd"],
  );
  assert.deepEqual(
    workspace.layers.filter((layer) => layer.kind === "indicator").map((layer) => layer.height),
    [92, 92, 92],
  );
  assert.equal(
    workspace.layers.find((layer) => layer.kind === "annotation" && layer.annotationId === "sessions")?.visible,
    false,
  );
  assert.equal(
    workspace.layers.find((layer) => layer.kind === "annotation" && layer.annotationId === "gaps")?.visible,
    false,
  );
  assert.equal(
    workspace.layers.find((layer) => layer.kind === "annotation" && layer.annotationId === "events")?.visible,
    false,
  );
  assert.equal(
    workspace.layers.find((layer) => layer.kind === "annotation" && layer.annotationId === "patterns")?.visible,
    true,
  );
});

test("migrates legacy drawings into the first drawing layer", () => {
  const workspace = readChartLayerWorkspace(null, JSON.stringify([drawing]));
  assert.deepEqual(activeDrawingLayer(workspace).drawings, [drawing]);
});

test("adds RSI without crowding an existing persisted indicator workspace", () => {
  const serialized = JSON.stringify({
    version: 1,
    activeDrawingLayerId: "drawings",
    layers: [
      { id: CHART_PRICE_LAYER_ID, kind: "price", name: "价格", visible: true, order: 0 },
      { id: "drawings", kind: "drawing", name: "A", visible: true, order: 10, drawings: [] },
      { id: "layer:indicator:kdj", kind: "indicator", indicatorId: "kdj", placement: "pane", name: "KDJ", visible: true, order: 20, height: 132 },
      { id: "layer:indicator:macd", kind: "indicator", indicatorId: "macd", placement: "pane", name: "MACD", visible: true, order: 30, height: 132 },
    ],
  });
  const workspace = readChartLayerWorkspace(serialized);
  const rsi = workspace.layers.find((layer) => layer.kind === "indicator" && layer.indicatorId === "rsi");
  assert.equal(rsi?.visible, false);
  assert.equal(rsi?.height, 92);
});

test("repairs a tampered hidden price layer and filters unknown layers", () => {
  const serialized = JSON.stringify({
    version: 1,
    activeDrawingLayerId: "drawings",
    layers: [
      { id: CHART_PRICE_LAYER_ID, kind: "price", name: "hidden", visible: false, order: 90 },
      { id: "drawings", kind: "drawing", name: "A", visible: true, order: 20, drawings: [] },
      { id: "bad", kind: "remote-script", visible: true, order: 0 },
    ],
  });
  const workspace = readChartLayerWorkspace(serialized);
  assert.equal(workspace.layers[0].kind, "price");
  assert.equal(workspace.layers[0].visible, true);
  assert.equal(workspace.layers.some((layer) => layer.id === "bad"), false);
});

test("manages multiple drawing layers without allowing the final drawing layer to disappear", () => {
  let workspace = createDefaultChartLayerWorkspace();
  workspace = addDrawingLayer(workspace, "layer:drawing:2");
  workspace = appendDrawingToActiveLayer(workspace, drawing);
  assert.equal(activeDrawingLayer(workspace).id, "layer:drawing:2");
  assert.equal(activeDrawingLayer(workspace).drawings.length, 1);

  workspace = deleteDrawingLayer(workspace, "layer:drawing:2");
  assert.equal(workspace.layers.filter((layer) => layer.kind === "drawing").length, 1);
  const unchanged = deleteDrawingLayer(workspace, activeDrawingLayer(workspace).id);
  assert.strictEqual(unchanged, workspace);
});

test("visibility never hides price and indicator size stays inside the pane range", () => {
  let workspace = createDefaultChartLayerWorkspace();
  workspace = setChartLayerVisibility(workspace, CHART_PRICE_LAYER_ID, false);
  assert.equal(workspace.layers.find((layer) => layer.kind === "price")?.visible, true);
  workspace = setChartLayerVisibility(workspace, "layer:indicator:kdj", false);
  assert.equal(workspace.layers.find((layer) => layer.id === "layer:indicator:kdj")?.visible, false);
  workspace = resizeIndicatorLayer(workspace, "layer:indicator:macd", 10_000);
  assert.equal(
    workspace.layers.find((layer) => layer.id === "layer:indicator:macd" && layer.kind === "indicator")?.height,
    280,
  );
});

test("reorders only layers of the same movable kind", () => {
  let workspace = createDefaultChartLayerWorkspace();
  workspace = moveChartLayer(workspace, "layer:indicator:macd", "layer:indicator:kdj");
  assert.deepEqual(
    workspace.layers.filter((layer) => layer.kind === "indicator").map((layer) => layer.indicatorId),
    ["rsi", "macd", "kdj"],
  );
  const unchanged = moveChartLayer(workspace, "layer:indicator:macd", workspace.activeDrawingLayerId);
  assert.strictEqual(unchanged, workspace);
});

test("builds one ordered render registry without coupling indicator visibility to strategy data", () => {
  const workspace = setChartLayerVisibility(
    createDefaultChartLayerWorkspace(),
    "layer:indicator:kdj",
    false,
  );
  const layers = buildChartLayers(workspace, {
    indicatorSeries: emptyIndicatorSeries,
    sessionBands: [],
    eventMarkers: [],
    priceLevels: [],
    valueZones: [],
    trendLines: [],
    pricePatterns: [],
    marketStructureEvents: [],
    overlaySeries: [],
  });
  const kdj = layers.find((layer) => layer.definition.id === "layer:indicator:kdj");
  assert.ok(kdj && kdj.definition.kind === "indicator");
  assert.equal(kdj.definition.visible, false);
  assert.strictEqual(kdj.series, emptyIndicatorSeries);
});
