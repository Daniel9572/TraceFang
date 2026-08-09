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
  EXPERT_PRICE_LAYER_ID as CHART_PRICE_LAYER_ID,
  activeDrawingLayer,
  addExpertDrawingLayer as addDrawingLayer,
  appendDrawingToActiveLayer,
  buildExpertChartLayers as buildChartLayers,
  clearActiveDrawingLayer,
  createDefaultExpertLayerWorkspace as createDefaultChartLayerWorkspace,
  deleteExpertDrawingLayer as deleteDrawingLayer,
  expertLayerCapabilities as chartLayerCapabilities,
  moveExpertLayer as moveChartLayer,
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
