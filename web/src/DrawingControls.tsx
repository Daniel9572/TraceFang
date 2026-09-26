import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type SetStateAction,
} from "react";
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

export const DRAWING_TOOLS: {
  id: ExpertDrawingTool;
  label: string;
  key: string;
}[] = [
  { id: "trend", label: "趋势线", key: "T" },
  { id: "horizontal", label: "水平线", key: "H" },
  { id: "rectangle", label: "矩形", key: "R" },
  { id: "fibonacci", label: "斐波那契", key: "F" },
];

export function DrawingControls({
  workspace,
  tool,
  onTool,
  snap,
  onSnap,
  history,
  onManageLayers,
}: {
  workspace: ChartLayerWorkspace;
  tool: ExpertDrawingTool | null;
  onTool: (tool: ExpertDrawingTool | null) => void;
  snap: ExpertDrawingSnapMode;
  onSnap: (mode: ExpertDrawingSnapMode) => void;
  history: ReturnType<typeof useDrawingHistory>;
  onManageLayers?: () => void;
}) {
  const [objectId, setObjectId] = useState("");
  const drawings = workspace.layers.flatMap((layer) =>
    layer.kind === "drawing" ? layer.drawings : [],
  );
  const selected = drawings.find((item) => item.id === objectId);
  const chooseTool = (value: ExpertDrawingTool | null) => {
    if (value && !activeDrawingLayer(workspace).visible) {
      history.change({
        ...workspace,
        layers: workspace.layers.map((layer) =>
          layer.id === workspace.activeDrawingLayerId
            ? { ...layer, visible: true }
            : layer,
        ),
      });
    }
    onTool(value);
  };
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input,textarea,select,[contenteditable=true]"))
        return;
      if (event.key === "Escape") {
        onTool(null);
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (event.shiftKey) history.redo();
        else history.undo();
        return;
      }
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const match = DRAWING_TOOLS.find(
        (item) => item.key === event.key.toUpperCase(),
      );
      if (match) {
        event.preventDefault();
        chooseTool(match.id);
      }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  });
  return (
    <div className="drawing-controls" aria-label="画线工具栏">
      <div className="drawing-actions">
        <button
          type="button"
          className={!tool ? "is-active" : ""}
          onClick={() => onTool(null)}
          title="浏览 / Esc"
        >
          选择
        </button>
        {DRAWING_TOOLS.map((item) => (
          <button
            type="button"
            key={item.id}
            className={tool === item.id ? "is-active" : ""}
            aria-pressed={tool === item.id}
            title={`${item.label} (${item.key})`}
            onClick={() => chooseTool(item.id)}
          >
            {item.label}
          </button>
        ))}
        <button
          type="button"
          aria-pressed={snap === "weak"}
          className={snap === "weak" ? "is-active" : ""}
          onClick={() => onSnap(snap === "weak" ? "off" : "weak")}
        >
          磁吸
        </button>
        <button
          type="button"
          disabled={!history.canUndo}
          onClick={history.undo}
          title="⌘/Ctrl Z"
        >
          撤销
        </button>
        <button
          type="button"
          disabled={!history.canRedo}
          onClick={history.redo}
          title="⌘/Ctrl Shift Z"
        >
          重做
        </button>
        {onManageLayers ? (
          <button type="button" onClick={onManageLayers}>
            图层管理
          </button>
        ) : null}
        <select
          aria-label="管理画线对象"
          value={selected?.id ?? ""}
          onChange={(event) => setObjectId(event.target.value)}
        >
          <option value="">对象 ({drawings.length})</option>
          {drawings.map((item, i) => (
            <option key={item.id} value={item.id}>
              {i + 1}. {item.label}
            </option>
          ))}
        </select>
        {selected ? (
          <>
            <input
              aria-label="画线名称"
              key={selected.id + selected.label}
              defaultValue={selected.label}
              maxLength={36}
              onBlur={(event) => {
                if (
                  event.target.value.trim() &&
                  event.target.value !== selected.label
                )
                  history.change(
                    replaceDrawing(
                      workspace,
                      { ...selected, label: event.target.value.trim() },
                      selected.id,
                    ),
                  );
              }}
            />
            <input
              aria-label="画线颜色"
              type="color"
              value={
                selected.color.startsWith("#") ? selected.color : "#245c8c"
              }
              onChange={(event) =>
                history.change(
                  replaceDrawing(
                    workspace,
                    { ...selected, color: event.target.value },
                    selected.id,
                  ),
                )
              }
            />
            <button
              type="button"
              onClick={() =>
                history.change(replaceDrawing(workspace, null, selected.id))
              }
            >
              删除对象
            </button>
          </>
        ) : null}
      </div>
      {tool ? (
        <small role="status">
          {tool === "horizontal"
            ? "点击目标价格"
            : "点击起点与终点，或按住拖动"}{" "}
          · Esc 取消 · 完成后点击对象可拖动端点
        </small>
      ) : null}
    </div>
  );
}
