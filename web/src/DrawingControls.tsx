import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type SetStateAction,
} from "react";
import { ArrowUpRight, CaseSensitive, Circle, Columns3, GitCommitHorizontal, Layers3, LockKeyhole, Magnet, Minus, MousePointer2, Redo2, Ruler, Square, TrendingUp, Trash2, Undo2, UnlockKeyhole, type LucideIcon } from "lucide-react";
import { activeDrawingLayer, type ChartLayerWorkspace } from "./chartLayers";
import type {
  ExpertDrawing,
  ExpertDrawingSnapMode,
  ExpertDrawingTool,
} from "./expertTypes";

export function replaceDrawing(
  workspace: ChartLayerWorkspace,
  drawing: ExpertDrawing | null,
  id: string,
): ChartLayerWorkspace {
  return {
    ...workspace,
    layers: workspace.layers.map((layer) =>
      layer.kind === "drawing"
        ? {
            ...layer,
            drawings: layer.drawings.flatMap((item) =>
              item.id === id ? (drawing ? [drawing] : []) : [item],
            ),
          }
        : layer,
    ),
  };
}

export function useDrawingHistory(
  workspace: ChartLayerWorkspace,
  onChange: (
    update: (current: ChartLayerWorkspace) => ChartLayerWorkspace,
  ) => void,
  scope: string,
) {
  const current = useRef(workspace);
  current.current = workspace;
  const stacks = useRef<{
    scope: string;
    past: ChartLayerWorkspace[];
    future: ChartLayerWorkspace[];
  }>({ scope, past: [], future: [] });
  if (stacks.current.scope !== scope)
    stacks.current = { scope, past: [], future: [] };
  const [, render] = useState(0);
  const change = useCallback(
    (update: SetStateAction<ChartLayerWorkspace>) => {
      const next =
        typeof update === "function" ? update(current.current) : update;
      if (next === current.current) return;
      stacks.current.past = [
        ...stacks.current.past.slice(-39),
        current.current,
      ];
      stacks.current.future = [];
      current.current = next;
      onChange(() => next);
      render((value) => value + 1);
    },
    [onChange],
  );
  const move = (direction: "past" | "future") => {
    const target = stacks.current[direction].pop();
    if (!target) return;
    stacks.current[direction === "past" ? "future" : "past"].push(
      current.current,
    );
    current.current = target;
    onChange(() => target);
    render((value) => value + 1);
  };
  return {
    change,
    undo: () => move("past"),
    redo: () => move("future"),
    canUndo: stacks.current.past.length > 0,
    canRedo: stacks.current.future.length > 0,
  };
}

export const DRAWING_TOOLS: { id: ExpertDrawingTool; label: string; key: string; icon: LucideIcon }[] = [
  { id: "trend", label: "趋势线", key: "T", icon: TrendingUp },
  { id: "ray", label: "射线", key: "A", icon: ArrowUpRight },
  { id: "horizontal", label: "水平线", key: "H", icon: Minus },
  { id: "vertical", label: "垂直线", key: "V", icon: GitCommitHorizontal },
  { id: "channel", label: "平行通道", key: "C", icon: Columns3 },
  { id: "rectangle", label: "矩形区域", key: "R", icon: Square },
  { id: "fibonacci", label: "斐波那契回撤", key: "F", icon: Layers3 },
  { id: "text", label: "文字注释", key: "N", icon: CaseSensitive },
  { id: "measure", label: "价格/时间测量", key: "M", icon: Ruler },
];

export function DrawingControls({ workspace, tool, onTool, snap, onSnap, history, onManageLayers,
  selectedId, onSelect, continuous, onContinuous }: {
  workspace: ChartLayerWorkspace;
  tool: ExpertDrawingTool | null;
  onTool: (tool: ExpertDrawingTool | null) => void;
  snap: ExpertDrawingSnapMode;
  onSnap: (mode: ExpertDrawingSnapMode) => void;
  history: ReturnType<typeof useDrawingHistory>;
  onManageLayers?: () => void;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  continuous: boolean;
  onContinuous: (value: boolean) => void;
}) {
  const drawings = workspace.layers.flatMap((layer) => layer.kind === "drawing" ? layer.drawings : []);
  const selected = drawings.find((item) => item.id === selectedId);
  const chooseTool = (value: ExpertDrawingTool | null) => {
    if (value && !activeDrawingLayer(workspace).visible) {
      history.change({ ...workspace, layers: workspace.layers.map((layer) =>
        layer.id === workspace.activeDrawingLayerId ? { ...layer, visible: true } : layer) });
    }
    onTool(value);
  };
  const remove = () => {
    if (!selected) return;
    history.change(replaceDrawing(workspace, null, selected.id));
    onSelect(null);
  };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement && target.closest("input,textarea,select,[contenteditable]")) return;
      if (event.key === "Escape") { onTool(null); return; }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) history.redo(); else history.undo();
        return;
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        if (selected) { event.preventDefault(); remove(); }
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const match = DRAWING_TOOLS.find((item) => item.key === event.key.toUpperCase());
      if (match) { event.preventDefault(); chooseTool(match.id); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });
  return <div className="drawing-controls" aria-label="画线工具栏">
    <div className="drawing-actions">
      <button type="button" className={!tool ? "is-active" : ""} onClick={() => onTool(null)} title="浏览 / Esc"><MousePointer2 size={15} />浏览</button>
      {DRAWING_TOOLS.map(({ icon: Icon, ...item }) => <button type="button" key={item.id}
        className={tool === item.id ? "is-active" : ""} aria-pressed={tool === item.id}
        title={`${item.label} (${item.key})`} onClick={() => chooseTool(item.id)}><Icon size={15} />{item.label}</button>)}
      <label className="drawing-snap"><Magnet size={15} /><select aria-label="磁吸模式" value={snap} onChange={(event) => onSnap(event.target.value as ExpertDrawingSnapMode)}>
        <option value="off">磁吸关闭</option><option value="weak">弱磁吸</option><option value="strong">强磁吸</option>
      </select></label>
      <button type="button" className={continuous ? "is-active" : ""} aria-pressed={continuous} onClick={() => onContinuous(!continuous)} title="完成后保留当前工具"><Circle size={15} />连续绘制</button>
      <button type="button" disabled={!history.canUndo} onClick={history.undo} title="⌘/Ctrl Z"><Undo2 size={15} />撤销</button>
      <button type="button" disabled={!history.canRedo} onClick={history.redo} title="⌘/Ctrl Shift Z"><Redo2 size={15} />重做</button>
      {onManageLayers ? <button type="button" onClick={onManageLayers}><Layers3 size={15} />图层管理</button> : null}
      <select aria-label="管理画线对象" value={selected?.id ?? ""} onChange={(event) => onSelect(event.target.value || null)}>
        <option value="">对象 ({drawings.length})</option>
        {drawings.map((item, i) => <option key={item.id} value={item.id}>{i + 1}. {item.label}{item.locked ? " · 已锁定" : ""}</option>)}
      </select>
    </div>
    {selected ? <div className="drawing-object-actions">
      <input aria-label="画线名称" key={"name:" + selected.id + selected.label} defaultValue={selected.label} maxLength={36}
        onBlur={(event) => { const label = event.target.value.trim(); if (label && label !== selected.label) history.change(replaceDrawing(workspace, { ...selected, label }, selected.id)); }} />
      {selected.type === "text" ? <input aria-label="注释文字" key={"text:" + selected.id + selected.text} defaultValue={selected.text ?? "文字注释"} maxLength={240}
        onBlur={(event) => { if (event.target.value !== selected.text) history.change(replaceDrawing(workspace, { ...selected, text: event.target.value.slice(0, 240) }, selected.id)); }} /> : null}
      <input aria-label="画线颜色" type="color" value={selected.color.startsWith("#") ? selected.color : "#245c8c"}
        onChange={(event) => history.change(replaceDrawing(workspace, { ...selected, color: event.target.value }, selected.id))} />
      <button type="button" aria-pressed={selected.locked === true} onClick={() => history.change(replaceDrawing(workspace, { ...selected, locked: !selected.locked }, selected.id))}>
        {selected.locked ? <UnlockKeyhole size={15} /> : <LockKeyhole size={15} />}{selected.locked ? "解锁对象" : "锁定对象"}</button>
      <button type="button" onClick={remove}><Trash2 size={15} />删除对象</button>
    </div> : null}
    {tool ? <small role="status">{tool === "channel" ? "点击基线两点，再点击通道宽度" : ["horizontal", "vertical", "text"].includes(tool) ? "点击放置锚点" : "点击起点与终点，或按住拖动"} · Esc 取消 · Ctrl/⌘ 临时切换磁吸</small> : null}
  </div>;
}
