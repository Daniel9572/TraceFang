import {
  Activity,
  CandlestickChart,
  Eye,
  EyeOff,
  GripVertical,
  LockKeyhole,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { useMemo, useState, type DragEvent, type KeyboardEvent } from "react";

import {
  chartLayerCapabilities,
  sortChartLayers,
  type ChartDrawingLayer,
  type ChartLayerDefinition,
  type ChartLayerKind,
  type ChartLayerWorkspace,
} from "./chartLayers";

interface ChartLayerManagerProps {
  workspace: ChartLayerWorkspace;
  onClose: () => void;
  onAddDrawingLayer: () => void;
  onSelectDrawingLayer: (layerId: string) => void;
  onToggleLayer: (layerId: string, visible: boolean) => void;
  onRenameDrawingLayer: (layerId: string, name: string) => void;
  onDeleteDrawingLayer: (layerId: string) => void;
  onMoveLayer: (layerId: string, targetLayerId: string) => void;
  onResizeIndicatorLayer: (layerId: string, height: number) => void;
}

const GROUPS: Array<{ kind: ChartLayerKind; label: string }> = [
  { kind: "price", label: "行情底座" },
  { kind: "drawing", label: "用户画线" },
  { kind: "indicator", label: "只读指标" },
  { kind: "annotation", label: "系统标注" },
];

function layerDetail(layer: ChartLayerDefinition): string {
  if (layer.kind === "price") return "固定 · 不可隐藏";
  if (layer.kind === "drawing") return `${layer.drawings.length} 条画线`;
  if (layer.kind === "indicator") return `${layer.height}px · 共享时间轴`;
  if (layer.annotationId === "sessions") return "由资金主导策略输出，可单独隐藏";
  if (layer.annotationId === "gaps") return "由跳空视觉策略输出，仅标记复市首点";
  if (layer.annotationId === "events") return "由数据/事件策略输出：FOMC / 非农 / CPI / 央行购金";
  return "支撑压力 / POC / FVG";
}

export function ChartLayerManager({
  workspace,
  onClose,
  onAddDrawingLayer,
  onSelectDrawingLayer,
  onToggleLayer,
  onRenameDrawingLayer,
  onDeleteDrawingLayer,
  onMoveLayer,
  onResizeIndicatorLayer,
}: ChartLayerManagerProps) {
  const [draggedLayerId, setDraggedLayerId] = useState<string | null>(null);
  const [editingLayerId, setEditingLayerId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const sortedLayers = useMemo(() => sortChartLayers(workspace.layers), [workspace.layers]);
  const drawingLayerCount = sortedLayers.filter((layer) => layer.kind === "drawing").length;

  const beginRename = (layer: ChartDrawingLayer) => {
    setEditingLayerId(layer.id);
    setDraftName(layer.name);
  };
  const commitRename = () => {
    if (editingLayerId !== null) onRenameDrawingLayer(editingLayerId, draftName);
    setEditingLayerId(null);
  };
  const reorderWithKeyboard = (event: KeyboardEvent<HTMLButtonElement>, layer: ChartLayerDefinition) => {
    if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown")) return;
    const siblings = sortedLayers.filter((candidate) => candidate.kind === layer.kind);
    const index = siblings.findIndex((candidate) => candidate.id === layer.id);
    const target = siblings[index + (event.key === "ArrowUp" ? -1 : 1)];
    if (!target) return;
    event.preventDefault();
    onMoveLayer(layer.id, target.id);
  };
  const startDrag = (event: DragEvent<HTMLElement>, layer: ChartLayerDefinition) => {
    if (!chartLayerCapabilities(layer.kind).canReorder) return;
    setDraggedLayerId(layer.id);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-chart-layer", layer.id);
  };
  const dropLayer = (event: DragEvent<HTMLElement>, target: ChartLayerDefinition) => {
    const sourceId = draggedLayerId ?? event.dataTransfer.getData("application/x-chart-layer");
    const source = sortedLayers.find((layer) => layer.id === sourceId);
    if (!source || source.kind !== target.kind) return;
    event.preventDefault();
    onMoveLayer(source.id, target.id);
    setDraggedLayerId(null);
  };

  return (
    <section className="expert-layer-manager" aria-label="图层管理">
      <header>
        <div>
          <span>LAYERS</span>
          <strong>图层管理</strong>
        </div>
        <button type="button" onClick={onClose} aria-label="关闭图层管理"><X size={15} /></button>
      </header>
      <div className="expert-layer-manager-body">
        {GROUPS.map((group) => {
          const groupLayers = sortedLayers.filter((layer) => layer.kind === group.kind);
          if (groupLayers.length === 0) return null;
          return (
            <section className={`expert-layer-group is-${group.kind}`} key={group.kind}>
              <header>
                <span>{group.label}</span>
                {group.kind === "drawing" ? (
                  <button type="button" onClick={onAddDrawingLayer} title="新建画线图层">
                    <Plus size={12} />新建
                  </button>
                ) : <em>{groupLayers.length}</em>}
              </header>
              <div>
                {groupLayers.map((layer) => {
                  const capabilities = chartLayerCapabilities(layer.kind);
                  const active = layer.kind === "drawing" && layer.id === workspace.activeDrawingLayerId;
                  return (
                    <article
                      className={`expert-layer-row ${active ? "is-active" : ""} ${layer.visible ? "" : "is-hidden"}`}
                      data-layer-kind={layer.kind}
                      key={layer.id}
                      onDragEnd={() => setDraggedLayerId(null)}
                      onDragOver={(event) => {
                        const source = sortedLayers.find((candidate) => candidate.id === draggedLayerId);
                        if (source?.kind === layer.kind) event.preventDefault();
                      }}
                      onDrop={(event) => dropLayer(event, layer)}
                    >
                      {capabilities.canReorder ? (
                        <button
                          type="button"
                          className="expert-layer-grip"
                          draggable
                          aria-label={`拖动 ${layer.name}；Alt 加上下方向键也可排序`}
                          onDragStart={(event) => startDrag(event, layer)}
                          onKeyDown={(event) => reorderWithKeyboard(event, layer)}
                        >
                          <GripVertical size={13} />
                        </button>
                      ) : <span className="expert-layer-lock"><LockKeyhole size={12} /></span>}
                      <div
                        className="expert-layer-identity"
                        onClick={() => { if (layer.kind === "drawing") onSelectDrawingLayer(layer.id); }}
                        onKeyDown={(event) => {
                          if (layer.kind === "drawing" && (event.key === "Enter" || event.key === " ")) {
                            event.preventDefault();
                            onSelectDrawingLayer(layer.id);
                          }
                        }}
                        role={layer.kind === "drawing" ? "button" : undefined}
                        tabIndex={layer.kind === "drawing" ? 0 : undefined}
                        title={layer.kind === "drawing" ? "设为当前画线图层" : layerDetail(layer)}
                      >
                        <span className="expert-layer-kind-icon">
                          {layer.kind === "price" ? <CandlestickChart size={14} /> : layer.kind === "indicator" ? <Activity size={14} /> : null}
                        </span>
                        <span>
                          {editingLayerId === layer.id && layer.kind === "drawing" ? (
                            <input
                              autoFocus
                              value={draftName}
                              maxLength={36}
                              onChange={(event) => setDraftName(event.target.value)}
                              onBlur={commitRename}
                              onClick={(event) => event.stopPropagation()}
                              onKeyDown={(event) => {
                                if (event.key === "Enter") commitRename();
                                if (event.key === "Escape") setEditingLayerId(null);
                              }}
                            />
                          ) : <strong>{layer.name}</strong>}
                          <small>{layerDetail(layer)}</small>
                        </span>
                      </div>
                      <div className="expert-layer-actions">
                        {capabilities.canRename && layer.kind === "drawing" ? (
                          <button type="button" onClick={() => beginRename(layer)} title="重命名图层"><Pencil size={12} /></button>
                        ) : null}
                        {capabilities.canHide ? (
                          <button
                            type="button"
                            aria-pressed={!layer.visible}
                            onClick={() => onToggleLayer(layer.id, !layer.visible)}
                            title={layer.visible ? "隐藏图层" : "显示图层"}
                          >
                            {layer.visible ? <Eye size={13} /> : <EyeOff size={13} />}
                          </button>
                        ) : <span title="基础价格层始终显示"><LockKeyhole size={12} /></span>}
                        {capabilities.canDelete ? (
                          <button
                            type="button"
                            disabled={drawingLayerCount <= 1}
                            onClick={() => onDeleteDrawingLayer(layer.id)}
                            title={drawingLayerCount <= 1 ? "至少保留一个画线图层" : "删除图层"}
                          >
                            <Trash2 size={12} />
                          </button>
                        ) : null}
                      </div>
                      {layer.kind === "indicator" ? (
                        <div className="expert-layer-height">
                          <span>窗格高度</span>
                          <button
                            type="button"
                            aria-label={`缩小 ${layer.name} 窗格`}
                            onClick={() => onResizeIndicatorLayer(layer.id, layer.height - 12)}
                          >−</button>
                          <input
                            type="range"
                            min="92"
                            max="280"
                            value={layer.height}
                            aria-label={`${layer.name}窗格高度`}
                            onChange={(event) => onResizeIndicatorLayer(layer.id, Number(event.target.value))}
                          />
                          <button
                            type="button"
                            aria-label={`放大 ${layer.name} 窗格`}
                            onClick={() => onResizeIndicatorLayer(layer.id, layer.height + 12)}
                          >+</button>
                          <em>{layer.height}</em>
                        </div>
                      ) : null}
                    </article>
                  );
                })}
              </div>
            </section>
          );
        })}
      </div>
      <footer>
        <span><i />活动画线层</span>
        <span>拖动排序 · 分隔线缩放指标</span>
      </footer>
    </section>
  );
}
