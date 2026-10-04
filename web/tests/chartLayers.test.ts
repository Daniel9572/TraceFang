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
  configureVolumeProfile,
  CHART_VOLUME_PROFILE_LAYER_ID,
  deleteDrawingLayer,
  indicatorOverlayLayout,
  moveChartLayer,
  positionIndicatorLayer,
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
  assert.ok(workspace.layers.filter((layer) => layer.kind === "indicator")
    .every((layer) => layer.placement === "overlay"));
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

test("migrates the volume profile and persists its independent visibility, width and opacity", () => {
  const old = createDefaultChartLayerWorkspace([drawing]);
  old.layers = old.layers.filter((layer) => layer.id !== CHART_VOLUME_PROFILE_LAYER_ID);
  const migrated = readChartLayerWorkspace(JSON.stringify(old));
  const layer = migrated.layers.find((candidate) => candidate.id === CHART_VOLUME_PROFILE_LAYER_ID);
  assert.equal(layer?.visible, true);
  assert.deepEqual(layer?.kind === "annotation" ? layer.volumeProfile : null, { width: 120, opacity: 0.3 });
  const configured = setChartLayerVisibility(configureVolumeProfile(migrated, { width: 180, opacity: 0.5 }), CHART_VOLUME_PROFILE_LAYER_ID, false);
  const restored = readChartLayerWorkspace(JSON.stringify(configured));
  const profile = restored.layers.find((candidate) => candidate.id === CHART_VOLUME_PROFILE_LAYER_ID);
  assert.equal(profile?.visible, false);
  assert.deepEqual(profile?.kind === "annotation" ? profile.volumeProfile : null, { width: 180, opacity: 0.5 });
  assert.equal(restored.layers.filter((candidate) => candidate.id === CHART_VOLUME_PROFILE_LAYER_ID).length, 1);
  assert.deepEqual(activeDrawingLayer(restored).drawings, [drawing]);
  const bounded = configureVolumeProfile(restored, { width: 999, opacity: -1 });
  assert.deepEqual(bounded.layers.find((candidate) => candidate.kind === "annotation" && candidate.annotationId === "volume-profile")?.volumeProfile, { width: 240, opacity: 0.1 });
});

test("round-trips rectangle and Fibonacci anchors and migrates invisible legacy colors", () => {
  let workspace = createDefaultChartLayerWorkspace();
  for (const type of ["rectangle", "fibonacci"] as const) {
    workspace = appendDrawingToActiveLayer(workspace, { ...drawing, id: type, type, color: "#e5edf1" });
  }
  const restored = readChartLayerWorkspace(JSON.stringify(workspace));
  const objects = activeDrawingLayer(restored).drawings;
  assert.deepEqual(objects.map((item) => item.type), ["rectangle", "fibonacci"]);
  assert.ok(objects.every((item) => item.color === "#245c8c"));
  assert.deepEqual(objects[0].end, drawing.end);
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
  const migrated = workspace.layers.filter((layer) => layer.kind === "indicator");
  assert.ok(migrated.every((layer) => layer.placement === "overlay"));
  assert.deepEqual(migrated.map((layer) => [layer.indicatorId, layer.height, layer.visible]), [
    ["kdj", 132, true], ["macd", 132, true], ["rsi", 92, false],
  ]);
});

test("keeps floating indicators inside one price pane when layers grow or the viewport shrinks", () => {
  const layers = [
    { id: "kdj", height: 280 },
    { id: "macd", height: 132 },
    { id: "rsi", height: 92 },
  ];
  for (const plotHeight of [120, 400, 900]) {
    const layout = indicatorOverlayLayout(layers, plotHeight);
    assert.deepEqual(layout.map((layer) => layer.id), ["kdj", "macd", "rsi"]);
    assert.ok(layout[0].top >= plotHeight * 0.55);
    for (let index = 0; index < layout.length; index += 1) {
      const layer = layout[index];
      assert.ok(layer.top + layer.height <= plotHeight);
      assert.ok(layer.scaleMargins.top + layer.scaleMargins.bottom < 1);
      assert.ok(layer.scaleMargins.top >= 0 && layer.scaleMargins.bottom >= 0);
      if (index > 0) assert.ok(layer.top >= layout[index - 1].top + layout[index - 1].height - 1e-9);
    }
    assert.ok(layout[0].height > layout[1].height);
  }
  assert.deepEqual(indicatorOverlayLayout([], 400), []);
  assert.deepEqual(indicatorOverlayLayout(layers, 0), []);
});

test("moves one indicator vertically, persists its position and keeps it inside resized charts", () => {
  const original = createDefaultChartLayerWorkspace();
  const moved = positionIndicatorLayer(original, "layer:indicator:kdj", 0.2);
  const restored = readChartLayerWorkspace(JSON.stringify(moved));
  const indicators = restored.layers.filter((layer) => layer.kind === "indicator");
  assert.equal(indicators.find((layer) => layer.indicatorId === "kdj")?.verticalPosition, 0.2);
  for (const plotHeight of [120, 400, 900]) {
    const before = indicatorOverlayLayout(original.layers.filter((layer) => layer.kind === "indicator"), plotHeight);
    const after = indicatorOverlayLayout(indicators, plotHeight);
    assert.equal(after[0].top, before[0].top);
    assert.equal(after[2].top, before[2].top);
    assert.ok(after[1].top < before[1].top);
    assert.equal(after[1].top, 0.2 * (plotHeight - after[1].height));
    assert.ok(after[1].top + after[1].height <= plotHeight);
  }
  const clamped = positionIndicatorLayer(moved, "layer:indicator:kdj", 10);
  assert.equal(clamped.layers.find((layer) => layer.id === "layer:indicator:kdj" && layer.kind === "indicator")?.verticalPosition, 1);
  const reset = positionIndicatorLayer(moved, "layer:indicator:kdj", null);
  assert.equal(reset.layers.find((layer) => layer.id === "layer:indicator:kdj" && layer.kind === "indicator")?.verticalPosition, undefined);
  assert.strictEqual(positionIndicatorLayer(moved, CHART_PRICE_LAYER_ID, 0.5), moved);
  const invalid = JSON.parse(JSON.stringify(moved));
  invalid.layers.find((layer: { id: string }) => layer.id === "layer:indicator:kdj").verticalPosition = "broken";
  assert.equal(readChartLayerWorkspace(JSON.stringify(invalid)).layers.find(
    (layer) => layer.id === "layer:indicator:kdj" && layer.kind === "indicator",
  )?.verticalPosition, undefined);
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

test("visibility never hides price and indicator size stays inside the layer range", () => {
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

test("persists new drawing categories, lock and bounded plain text while retaining old objects", () => {
  const types = ["ray", "vertical", "channel", "text", "measure"] as const;
  const drawings = types.map((type) => ({ ...drawing, id: type, type, locked: true,
    ...(type === "channel" ? { widthAnchor: { time: 150, price: 2420 } } : {}),
    ...(type === "text" ? { text: "<script>" + "字".repeat(300) } : {}) }));
  const workspace = readChartLayerWorkspace(JSON.stringify({ ...createDefaultChartLayerWorkspace(),
    layers: [{ kind: "drawing", id: "layer:drawing:1", name: "绘图", visible: true, order: 10, drawings: [drawing, ...drawings,
      { ...drawing, type: "channel", widthAnchor: { time: NaN, price: 123 } }] }] }));
  const saved = activeDrawingLayer(workspace).drawings;
  assert.equal(saved.length, 6);
  assert.deepEqual(saved[0], drawing);
  assert.equal(saved.find((item) => item.type === "text")!.text!.length, 240);
  assert.equal(saved.find((item) => item.type === "channel")!.locked, true);
});
