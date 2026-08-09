import type {
  ExpertDrawing,
  ExpertIndicatorId,
  ExpertIndicatorSeriesView,
  ExpertMarketEvent,
  ExpertPriceLevel,
  ExpertSessionBand,
  ExpertValueZone,
} from "./expertTypes";

export const EXPERT_LAYER_STORAGE_KEY = "market-expert-layers-v1";
export const LEGACY_DRAWING_STORAGE_KEY = "market-expert-drawings-v1";
export const EXPERT_PRICE_LAYER_ID = "layer:price";
export const DEFAULT_DRAWING_LAYER_ID = "layer:drawing:1";

export type ExpertLayerKind = "price" | "drawing" | "indicator" | "annotation";
export type ExpertAnnotationId = "sessions" | "events" | "analysis";

interface ExpertLayerBase {
  id: string;
  name: string;
  visible: boolean;
  order: number;
}

export interface ExpertPriceLayer extends ExpertLayerBase {
  kind: "price";
  visible: true;
}

export interface ExpertDrawingLayer extends ExpertLayerBase {
  kind: "drawing";
  drawings: ExpertDrawing[];
}

export interface ExpertIndicatorLayer extends ExpertLayerBase {
  kind: "indicator";
  indicatorId: ExpertIndicatorId;
  placement: "pane";
  height: number;
}

export interface ExpertAnnotationLayer extends ExpertLayerBase {
  kind: "annotation";
  annotationId: ExpertAnnotationId;
}

export type ExpertLayerDefinition =
  | ExpertPriceLayer
  | ExpertDrawingLayer
  | ExpertIndicatorLayer
  | ExpertAnnotationLayer;

export interface ExpertLayerWorkspace {
  version: 1;
  layers: ExpertLayerDefinition[];
  activeDrawingLayerId: string;
}

export interface ExpertLayerCapabilities {
  canHide: boolean;
  canRename: boolean;
  canDelete: boolean;
  canReorder: boolean;
  canEditContent: boolean;
  canResize: boolean;
}

export type ExpertChartLayer =
  | { kind: "price"; definition: ExpertPriceLayer }
  | { kind: "drawing"; definition: ExpertDrawingLayer }
  | { kind: "indicator"; definition: ExpertIndicatorLayer; series: ExpertIndicatorSeriesView }
  | {
      kind: "annotation";
      definition: ExpertAnnotationLayer;
      sessionBands: readonly ExpertSessionBand[];
      eventMarkers: readonly ExpertMarketEvent[];
      priceLevels: readonly ExpertPriceLevel[];
      valueZones: readonly ExpertValueZone[];
    };

export interface ExpertChartLayerPayloads {
  indicatorSeries: ExpertIndicatorSeriesView;
  sessionBands: readonly ExpertSessionBand[];
  eventMarkers: readonly ExpertMarketEvent[];
  priceLevels: readonly ExpertPriceLevel[];
  valueZones: readonly ExpertValueZone[];
}

const EMPTY_SESSION_BANDS: readonly ExpertSessionBand[] = [];
const EMPTY_EVENT_MARKERS: readonly ExpertMarketEvent[] = [];
const EMPTY_PRICE_LEVELS: readonly ExpertPriceLevel[] = [];
const EMPTY_VALUE_ZONES: readonly ExpertValueZone[] = [];
const MIN_INDICATOR_HEIGHT = 92;
const MAX_INDICATOR_HEIGHT = 280;

const ANNOTATION_NAMES: Record<ExpertAnnotationId, string> = {
  sessions: "交易时段",
  events: "重要事件",
  analysis: "策略标注",
};

const INDICATOR_NAMES: Record<ExpertIndicatorId, string> = {
  kdj: "KDJ 摆动",
  macd: "MACD 动量",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function cleanName(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  const cleaned = value.trim().slice(0, 36);
  return cleaned || fallback;
}

function finiteOrder(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function indicatorHeight(value: unknown): number {
  const height = typeof value === "number" && Number.isFinite(value) ? value : 132;
  return Math.round(Math.min(MAX_INDICATOR_HEIGHT, Math.max(MIN_INDICATOR_HEIGHT, height)));
}

function validDrawing(value: unknown): value is ExpertDrawing {
  if (!isRecord(value) || !isRecord(value.start) || !isRecord(value.end)) return false;
  return typeof value.id === "string"
    && (value.type === "trend" || value.type === "horizontal")
    && Number.isFinite(value.start.time)
    && Number.isFinite(value.start.price)
    && Number.isFinite(value.end.time)
    && Number.isFinite(value.end.price);
}

function parseDrawings(value: unknown): ExpertDrawing[] {
  if (!Array.isArray(value)) return [];
  return value.filter(validDrawing).map((drawing) => ({
    ...drawing,
    color: typeof drawing.color === "string" ? drawing.color : "#e5edf1",
    label: typeof drawing.label === "string" ? drawing.label : "画线",
  }));
}

function canonicalPriceLayer(): ExpertPriceLayer {
  return {
    id: EXPERT_PRICE_LAYER_ID,
    kind: "price",
    name: "现货黄金价格",
    visible: true,
    order: 0,
  };
}

function canonicalAnnotationLayer(annotationId: ExpertAnnotationId, order: number): ExpertAnnotationLayer {
  return {
    id: `layer:annotation:${annotationId}`,
    kind: "annotation",
    annotationId,
    name: ANNOTATION_NAMES[annotationId],
    visible: true,
    order,
  };
}

function canonicalIndicatorLayer(
  indicatorId: ExpertIndicatorId,
  order: number,
): ExpertIndicatorLayer {
  return {
    id: `layer:indicator:${indicatorId}`,
    kind: "indicator",
    indicatorId,
    name: INDICATOR_NAMES[indicatorId],
    visible: true,
    order,
    placement: "pane",
    height: 132,
  };
}

export function sortExpertLayers(
  layers: readonly ExpertLayerDefinition[],
): ExpertLayerDefinition[] {
  return [...layers].sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
}

function normalizeOrders(layers: readonly ExpertLayerDefinition[]): ExpertLayerDefinition[] {
  return sortExpertLayers(layers).map((layer, index) => ({ ...layer, order: index * 10 }));
}

export function createDefaultExpertLayerWorkspace(
  legacyDrawings: readonly ExpertDrawing[] = [],
): ExpertLayerWorkspace {
  return {
    version: 1,
    activeDrawingLayerId: DEFAULT_DRAWING_LAYER_ID,
    layers: normalizeOrders([
      canonicalPriceLayer(),
      canonicalAnnotationLayer("sessions", 10),
      canonicalAnnotationLayer("analysis", 20),
      canonicalAnnotationLayer("events", 30),
      {
        id: DEFAULT_DRAWING_LAYER_ID,
        kind: "drawing",
        name: "画线图层 1",
        visible: true,
        order: 40,
        drawings: [...legacyDrawings],
      },
      canonicalIndicatorLayer("kdj", 50),
      canonicalIndicatorLayer("macd", 60),
    ]),
  };
}

function parseLayer(value: unknown, fallbackOrder: number): ExpertLayerDefinition | null {
  if (!isRecord(value) || typeof value.id !== "string") return null;
  const order = finiteOrder(value.order, fallbackOrder);
  const visible = value.visible !== false;
  if (value.kind === "drawing") {
    return {
      id: value.id,
      kind: "drawing",
      name: cleanName(value.name, "画线图层"),
      visible,
      order,
      drawings: parseDrawings(value.drawings),
    };
  }
  if (value.kind === "indicator" && (value.indicatorId === "kdj" || value.indicatorId === "macd")) {
    return {
      id: value.id,
      kind: "indicator",
      indicatorId: value.indicatorId,
      name: INDICATOR_NAMES[value.indicatorId],
      visible,
      order,
      placement: "pane",
      height: indicatorHeight(value.height),
    };
  }
  if (
    value.kind === "annotation"
    && (value.annotationId === "sessions" || value.annotationId === "events" || value.annotationId === "analysis")
  ) {
    return {
      id: value.id,
      kind: "annotation",
      annotationId: value.annotationId,
      name: ANNOTATION_NAMES[value.annotationId],
      visible,
      order,
    };
  }
  return null;
}

function parseLegacyDrawings(serialized: string | null): ExpertDrawing[] {
  if (!serialized) return [];
  try {
    return parseDrawings(JSON.parse(serialized));
  } catch {
    return [];
  }
}

export function readExpertLayerWorkspace(
  serialized: string | null,
  legacyDrawingsSerialized: string | null = null,
): ExpertLayerWorkspace {
  if (!serialized) return createDefaultExpertLayerWorkspace(parseLegacyDrawings(legacyDrawingsSerialized));
  try {
    const parsed = JSON.parse(serialized) as unknown;
    if (!isRecord(parsed) || parsed.version !== 1 || !Array.isArray(parsed.layers)) {
      return createDefaultExpertLayerWorkspace(parseLegacyDrawings(legacyDrawingsSerialized));
    }
    const uniqueIds = new Set<string>();
    const layers = parsed.layers
      .map((value, index) => parseLayer(value, index * 10))
      .filter((value): value is ExpertLayerDefinition => Boolean(value))
      .filter((layer) => {
        if (uniqueIds.has(layer.id)) return false;
        uniqueIds.add(layer.id);
        return true;
      });

    const drawingLayers = layers.filter((layer): layer is ExpertDrawingLayer => layer.kind === "drawing");
    if (drawingLayers.length === 0) {
      drawingLayers.push({
        id: DEFAULT_DRAWING_LAYER_ID,
        kind: "drawing",
        name: "画线图层 1",
        visible: true,
        order: 40,
        drawings: parseLegacyDrawings(legacyDrawingsSerialized),
      });
      layers.push(drawingLayers[0]);
    }

    const annotations = new Set(
      layers.filter((layer): layer is ExpertAnnotationLayer => layer.kind === "annotation")
        .map((layer) => layer.annotationId),
    );
    for (const annotationId of ["sessions", "analysis", "events"] as const) {
      if (!annotations.has(annotationId)) layers.push(canonicalAnnotationLayer(annotationId, layers.length * 10));
    }
    const indicators = new Set(
      layers.filter((layer): layer is ExpertIndicatorLayer => layer.kind === "indicator")
        .map((layer) => layer.indicatorId),
    );
    for (const indicatorId of ["kdj", "macd"] as const) {
      if (!indicators.has(indicatorId)) layers.push(canonicalIndicatorLayer(indicatorId, layers.length * 10));
    }

    const activeCandidate = typeof parsed.activeDrawingLayerId === "string"
      ? parsed.activeDrawingLayerId
      : null;
    const activeDrawingLayerId = drawingLayers.some((layer) => layer.id === activeCandidate)
      ? activeCandidate as string
      : drawingLayers[0].id;
    return {
      version: 1,
      activeDrawingLayerId,
      layers: normalizeOrders([canonicalPriceLayer(), ...layers]),
    };
  } catch {
    return createDefaultExpertLayerWorkspace(parseLegacyDrawings(legacyDrawingsSerialized));
  }
}

export function expertLayerCapabilities(kind: ExpertLayerKind): ExpertLayerCapabilities {
  if (kind === "price") {
    return {
      canHide: false,
      canRename: false,
      canDelete: false,
      canReorder: false,
      canEditContent: false,
      canResize: false,
    };
  }
  if (kind === "drawing") {
    return {
      canHide: true,
      canRename: true,
      canDelete: true,
      canReorder: true,
      canEditContent: true,
      canResize: false,
    };
  }
  if (kind === "indicator") {
    return {
      canHide: true,
      canRename: false,
      canDelete: false,
      canReorder: true,
      canEditContent: false,
      canResize: true,
    };
  }
  return {
    canHide: true,
    canRename: false,
    canDelete: false,
    canReorder: false,
    canEditContent: false,
    canResize: false,
  };
}

export function activeDrawingLayer(workspace: ExpertLayerWorkspace): ExpertDrawingLayer {
  const layers = sortExpertLayers(workspace.layers)
    .filter((layer): layer is ExpertDrawingLayer => layer.kind === "drawing");
  return layers.find((layer) => layer.id === workspace.activeDrawingLayerId) ?? layers[0];
}

export function setActiveDrawingLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
): ExpertLayerWorkspace {
  return workspace.layers.some((layer) => layer.kind === "drawing" && layer.id === layerId)
    ? { ...workspace, activeDrawingLayerId: layerId }
    : workspace;
}

export function setExpertLayerVisibility(
  workspace: ExpertLayerWorkspace,
  layerId: string,
  visible: boolean,
): ExpertLayerWorkspace {
  let changed = false;
  const layers = workspace.layers.map((layer) => {
    if (layer.id !== layerId || layer.kind === "price" || layer.visible === visible) return layer;
    changed = true;
    return { ...layer, visible };
  });
  return changed ? { ...workspace, layers } : workspace;
}

export function renameExpertDrawingLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
  name: string,
): ExpertLayerWorkspace {
  const layers = workspace.layers.map((layer) => layer.id === layerId && layer.kind === "drawing"
    ? { ...layer, name: cleanName(name, layer.name) }
    : layer);
  return { ...workspace, layers };
}

export function addExpertDrawingLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
): ExpertLayerWorkspace {
  if (workspace.layers.some((layer) => layer.id === layerId)) return workspace;
  const sorted = sortExpertLayers(workspace.layers);
  const drawingCount = sorted.filter((layer) => layer.kind === "drawing").length;
  const lastDrawingIndex = sorted.reduce(
    (last, layer, index) => layer.kind === "drawing" ? index : last,
    -1,
  );
  const next: ExpertDrawingLayer = {
    id: layerId,
    kind: "drawing",
    name: `画线图层 ${drawingCount + 1}`,
    visible: true,
    order: 0,
    drawings: [],
  };
  sorted.splice(lastDrawingIndex + 1, 0, next);
  return {
    version: 1,
    activeDrawingLayerId: layerId,
    layers: normalizeOrders(sorted),
  };
}

export function deleteExpertDrawingLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
): ExpertLayerWorkspace {
  const drawingLayers = workspace.layers.filter((layer) => layer.kind === "drawing");
  if (drawingLayers.length <= 1 || !drawingLayers.some((layer) => layer.id === layerId)) return workspace;
  const layers = normalizeOrders(workspace.layers.filter((layer) => layer.id !== layerId));
  const nextActive = workspace.activeDrawingLayerId === layerId
    ? layers.find((layer): layer is ExpertDrawingLayer => layer.kind === "drawing")?.id
    : workspace.activeDrawingLayerId;
  return {
    version: 1,
    layers,
    activeDrawingLayerId: nextActive ?? DEFAULT_DRAWING_LAYER_ID,
  };
}

function updateDrawingLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
  update: (layer: ExpertDrawingLayer) => ExpertDrawingLayer,
): ExpertLayerWorkspace {
  let changed = false;
  const layers = workspace.layers.map((layer) => {
    if (layer.id !== layerId || layer.kind !== "drawing") return layer;
    changed = true;
    return update(layer);
  });
  return changed ? { ...workspace, layers } : workspace;
}

export function appendDrawingToActiveLayer(
  workspace: ExpertLayerWorkspace,
  drawing: ExpertDrawing,
): ExpertLayerWorkspace {
  return updateDrawingLayer(workspace, workspace.activeDrawingLayerId, (layer) => ({
    ...layer,
    drawings: [...layer.drawings, drawing],
  }));
}

export function undoActiveDrawing(workspace: ExpertLayerWorkspace): ExpertLayerWorkspace {
  return updateDrawingLayer(workspace, workspace.activeDrawingLayerId, (layer) => ({
    ...layer,
    drawings: layer.drawings.slice(0, -1),
  }));
}

export function clearActiveDrawingLayer(workspace: ExpertLayerWorkspace): ExpertLayerWorkspace {
  return updateDrawingLayer(workspace, workspace.activeDrawingLayerId, (layer) => ({
    ...layer,
    drawings: [],
  }));
}

export function moveExpertLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
  targetLayerId: string,
): ExpertLayerWorkspace {
  if (layerId === targetLayerId) return workspace;
  const sorted = sortExpertLayers(workspace.layers);
  const source = sorted.find((layer) => layer.id === layerId);
  const target = sorted.find((layer) => layer.id === targetLayerId);
  if (!source || !target || source.kind !== target.kind || !expertLayerCapabilities(source.kind).canReorder) {
    return workspace;
  }
  const sameKindPositions = sorted
    .map((layer, index) => ({ layer, index }))
    .filter((entry) => entry.layer.kind === source.kind);
  const orderedKindLayers = sameKindPositions.map((entry) => entry.layer);
  const from = orderedKindLayers.findIndex((layer) => layer.id === layerId);
  const to = orderedKindLayers.findIndex((layer) => layer.id === targetLayerId);
  const [moved] = orderedKindLayers.splice(from, 1);
  orderedKindLayers.splice(to, 0, moved);
  sameKindPositions.forEach((entry, index) => {
    sorted[entry.index] = { ...orderedKindLayers[index], order: entry.index * 10 };
  });
  return { ...workspace, layers: normalizeOrders(sorted) };
}

export function resizeExpertIndicatorLayer(
  workspace: ExpertLayerWorkspace,
  layerId: string,
  height: number,
): ExpertLayerWorkspace {
  const nextHeight = indicatorHeight(height);
  const layers = workspace.layers.map((layer) => layer.id === layerId && layer.kind === "indicator"
    ? { ...layer, height: nextHeight }
    : layer);
  return { ...workspace, layers };
}

export function buildExpertChartLayers(
  workspace: ExpertLayerWorkspace,
  payloads: ExpertChartLayerPayloads,
): ExpertChartLayer[] {
  return sortExpertLayers(workspace.layers).map((definition): ExpertChartLayer => {
    if (definition.kind === "price") return { kind: "price", definition };
    if (definition.kind === "drawing") return { kind: "drawing", definition };
    if (definition.kind === "indicator") {
      return { kind: "indicator", definition, series: payloads.indicatorSeries };
    }
    return {
      kind: "annotation",
      definition,
      sessionBands: definition.annotationId === "sessions" ? payloads.sessionBands : EMPTY_SESSION_BANDS,
      eventMarkers: definition.annotationId === "events" ? payloads.eventMarkers : EMPTY_EVENT_MARKERS,
      priceLevels: definition.annotationId === "analysis" ? payloads.priceLevels : EMPTY_PRICE_LEVELS,
      valueZones: definition.annotationId === "analysis" ? payloads.valueZones : EMPTY_VALUE_ZONES,
    };
  });
}
