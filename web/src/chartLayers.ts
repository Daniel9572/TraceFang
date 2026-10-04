/**
 * Product-wide chart-layer contract shared by normal and expert workspaces.
 *
 * The implementation currently lives in the original expert module so stored
 * workspaces migrate without a destructive rewrite. Consumers must use these
 * generic names; expert mode is a preset and management surface, not a second
 * renderer or layer lifecycle.
 */
export {
  DEFAULT_DRAWING_LAYER_ID,
  LEGACY_DRAWING_STORAGE_KEY,
  EXPERT_LAYER_STORAGE_KEY as LEGACY_EXPERT_LAYER_STORAGE_KEY,
  EXPERT_EVENT_LAYER_ID as CHART_EVENT_LAYER_ID,
  EXPERT_GAP_LAYER_ID as CHART_GAP_LAYER_ID,
  EXPERT_PRICE_LAYER_ID as CHART_PRICE_LAYER_ID,
  EXPERT_SESSION_LAYER_ID as CHART_SESSION_LAYER_ID,
  EXPERT_TREND_LINE_LAYER_ID as CHART_TREND_LINE_LAYER_ID,
  EXPERT_PATTERN_LAYER_ID as CHART_PATTERN_LAYER_ID,
  EXPERT_VOLUME_PROFILE_LAYER_ID as CHART_VOLUME_PROFILE_LAYER_ID,
  activeDrawingLayer,
  addExpertDrawingLayer as addDrawingLayer,
  appendDrawingToActiveLayer,
  buildExpertChartLayers as buildChartLayers,
  clearActiveDrawingLayer,
  configureExpertVolumeProfile as configureVolumeProfile,
  createDefaultExpertLayerWorkspace as createDefaultChartLayerWorkspace,
  deleteExpertDrawingLayer as deleteDrawingLayer,
  expertLayerCapabilities as chartLayerCapabilities,
  moveExpertLayer as moveChartLayer,
  positionExpertIndicatorLayer as positionIndicatorLayer,
  readExpertLayerWorkspace as readChartLayerWorkspace,
  renameExpertDrawingLayer as renameDrawingLayer,
  resizeExpertIndicatorLayer as resizeIndicatorLayer,
  setActiveDrawingLayer,
  setExpertLayerVisibility as setChartLayerVisibility,
  sortExpertLayers as sortChartLayers,
  undoActiveDrawing,
} from "./chartLayerModel.ts";

export type {
  ExpertAnnotationId as ChartAnnotationId,
  ExpertAnnotationLayer as ChartAnnotationLayer,
  ExpertChartLayer as ChartLayer,
  ExpertChartLayerPayloads as ChartLayerPayloads,
  ExpertDrawingLayer as ChartDrawingLayer,
  ExpertIndicatorLayer as ChartIndicatorLayer,
  ExpertLayerCapabilities as ChartLayerCapabilities,
  ExpertLayerDefinition as ChartLayerDefinition,
  ExpertLayerKind as ChartLayerKind,
  ExpertLayerWorkspace as ChartLayerWorkspace,
  ExpertPriceLayer as ChartPriceLayer,
} from "./chartLayerModel.ts";

export const CHART_LAYER_STORAGE_PREFIX = "market-chart-layers-v1";

export function chartLayerStorageKey(scope: string): string {
  return `${CHART_LAYER_STORAGE_PREFIX}:${scope.trim() || "default"}`;
}

/** Transparent studies share the price pane, each with its own vertical scale. */
export function indicatorOverlayLayout(
  layers: readonly { id: string; height: number; verticalPosition?: number }[],
  plotHeight: number,
) {
  if (plotHeight <= 0 || layers.length === 0) return [];
  const total = layers.reduce((sum, layer) => sum + layer.height, 0);
  const factor = Math.min(1, plotHeight * 0.42 / total);
  let defaultTop = plotHeight - total * factor - plotHeight * 0.02;
  return layers.map((layer) => {
    const height = layer.height * factor;
    const top = layer.verticalPosition === undefined
      ? defaultTop
      : Math.min(1, Math.max(0, layer.verticalPosition)) * (plotHeight - height);
    const layout = {
      id: layer.id,
      top,
      height,
      scaleMargins: {
        top: (top + height * 0.24) / plotHeight,
        bottom: (plotHeight - top - height * 0.94) / plotHeight,
      },
    };
    defaultTop += height;
    return layout;
  });
}
