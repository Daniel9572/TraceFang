import { useEffect, useRef, useState, type PointerEvent } from "react";
import { drawingMeasure, moveDrawingAnchor, translateDrawing } from "./expertDrawing";
import type { ExpertDrawing, ExpertDrawingPoint } from "./expertTypes";

export interface DrawingProjection {
  drawing: ExpertDrawing;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  x3?: number;
  y3?: number;
}
export type DrawingLocator = (x: number, y: number, modifier?: boolean, raw?: boolean) => ExpertDrawingPoint | null;
export type DrawingProjector = (point: ExpertDrawingPoint) => { x: number; y: number } | null;

export function projectDrawing(drawing: ExpertDrawing, project: DrawingProjector): DrawingProjection | null {
  const start = project(drawing.start);
  const end = project(drawing.end);
  const third = drawing.widthAnchor ? project(drawing.widthAnchor) : null;
  if (!start || !end || (drawing.widthAnchor && !third)) return null;
  return { drawing, x1: start.x, y1: start.y, x2: end.x, y2: end.y,
    ...(third ? { x3: third.x, y3: third.y } : {}) };
}

export function DrawingShape({ item, width, height, hit = false }: {
  item: DrawingProjection; width: number; height: number; hit?: boolean;
}) {
  const { drawing, x1, y1, x2, y2, x3 = x2, y3 = y2 } = item;
  const stroke = hit ? "transparent" : drawing.color;
  const style = { stroke, strokeWidth: hit ? 14 : 2, fill: "none", vectorEffect: "non-scaling-stroke" as const };
  const line = (a: number, b: number, c: number, d: number, key?: number) => <line key={key} {...style} x1={a} y1={b} x2={c} y2={d} />;
  if (drawing.type === "horizontal") return line(0, y1, width, y1);
  if (drawing.type === "vertical") return line(x1, 0, x1, height);
  if (drawing.type === "text") return <text x={x1 + 5} y={y1} fill={hit ? "transparent" : drawing.color} fontSize={12}
    stroke={hit ? "transparent" : "none"} strokeWidth={hit ? 14 : 0} style={{ pointerEvents: hit ? "all" : "none" }}>{drawing.text ?? "文字注释"}</text>;
  if (drawing.type === "rectangle") return <rect {...style} x={Math.min(x1, x2)} y={Math.min(y1, y2)} width={Math.abs(x2 - x1)} height={Math.abs(y2 - y1)} fill={hit ? "none" : drawing.color} fillOpacity={0.07} />;
  if (drawing.type === "fibonacci") return <>{[0, .236, .382, .5, .618, .786, 1].map((ratio) => <g key={ratio}>
    {line(x1, y1 + (y2 - y1) * ratio, x2, y1 + (y2 - y1) * ratio)}
    {!hit ? <text x={Math.min(x1, x2) + 4} y={y1 + (y2 - y1) * ratio - 4} fill={drawing.color} fontSize={10}>{(ratio * 100).toFixed(1)}%</text> : null}
  </g>)}</>;
  if (drawing.type === "ray") {
    const dx = x2 - x1, dy = y2 - y1;
    const distance = Math.hypot(dx, dy);
    const extension = distance > 0 ? (Math.hypot(width, height) + Math.hypot(x1, y1)) / distance : 1;
    return line(x1, y1, x1 + dx * extension, y1 + dy * extension);
  }
  if (drawing.type === "channel") {
    const dx = x2 - x1, dy = y2 - y1;
    const lengthSquared = dx * dx + dy * dy;
    const ratio = lengthSquared ? ((x3 - x1) * -dy + (y3 - y1) * dx) / lengthSquared : 0;
    const ox = -dy * ratio, oy = dx * ratio;
    return <>{line(x1, y1, x2, y2)}{line(x1 + ox, y1 + oy, x2 + ox, y2 + oy)}
      {!hit ? <path d={`M ${x1} ${y1} L ${x2} ${y2} L ${x2 + ox} ${y2 + oy} L ${x1 + ox} ${y1 + oy} Z`} fill={drawing.color} fillOpacity={.07} /> : null}</>;
  }
  if (drawing.type === "measure") return <>{line(x1, y1, x2, y2)}{line(x1, y1, x2, y1)}{line(x2, y1, x2, y2)}</>;
  return line(x1, y1, x2, y2);
}

export function DrawingOverlay({ items, width, height, enabled, locate, project, onUpdate, selectedId, onSelect, scope }: {
  items: DrawingProjection[]; width: number; height: number; enabled: boolean;
  locate: DrawingLocator; project: DrawingProjector; onUpdate?: (drawing: ExpertDrawing) => void;
  selectedId: string | null; onSelect: (id: string | null) => void; scope: string;
}) {
  const [draft, setDraft] = useState<ExpertDrawing | null>(null);
  const root = useRef<SVGSVGElement>(null);
  const drag = useRef<{ drawing: ExpertDrawing; anchor: "start" | "end" | "widthAnchor" | "move"; x: number; y: number; pointerId: number; moved: boolean; draft: ExpertDrawing | null } | null>(null);
  const cancel = () => { drag.current = null; setDraft(null); };
  useEffect(() => { cancel(); }, [scope, enabled]);
  useEffect(() => {
    if (drag.current && !items.some((item) => item.drawing === drag.current?.drawing && !item.drawing.locked)) cancel();
  }, [items]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") cancel(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  const coordinates = (event: PointerEvent) => {
    const rect = root.current!.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const update = (event: PointerEvent) => {
    const current = drag.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const { x, y } = coordinates(event);
    current.moved ||= Math.hypot(x - current.x, y - current.y) >= 3;
    if (!current.moved) return;
    let next: ExpertDrawing | null;
    if (current.anchor === "move") {
      const snapped = locate(x, y, event.ctrlKey || event.metaKey);
      const snappedPosition = snapped ? project(snapped) : null;
      const dx = (snappedPosition?.x ?? x) - current.x;
      const dy = (snappedPosition?.y ?? y) - current.y;
      if (current.drawing.type === "horizontal") {
        const initial = locate(current.x, current.y, false, true);
        const target = locate(current.x, current.y + dy, false, true);
        next = initial && target ? { ...current.drawing,
          start: { ...current.drawing.start, price: current.drawing.start.price + target.price - initial.price },
          end: { ...current.drawing.end, price: current.drawing.end.price + target.price - initial.price } } : null;
      } else next = translateDrawing(current.drawing, dx, dy, project, (a, b) => locate(a, b, false, true));
    } else {
      const point = locate(x, y, event.ctrlKey || event.metaKey);
      next = point ? moveDrawingAnchor(current.drawing, current.anchor, point) : null;
    }
    current.draft = next;
    setDraft(next);
  };
  const begin = (event: PointerEvent, drawing: ExpertDrawing, anchor: "start" | "end" | "widthAnchor" | "move") => {
    if (!enabled || event.button !== 0) return;
    event.preventDefault(); event.stopPropagation(); onSelect(drawing.id);
    if (drawing.locked || !onUpdate) return;
    root.current!.setPointerCapture(event.pointerId);
    drag.current = { drawing, anchor, ...coordinates(event), pointerId: event.pointerId, moved: false, draft: null };
  };
  const finish = (event: PointerEvent<SVGSVGElement>) => {
    update(event);
    const current = drag.current;
    cancel();
    if (root.current?.hasPointerCapture(event.pointerId)) root.current.releasePointerCapture(event.pointerId);
    if (current?.moved && current.draft) onUpdate?.(current.draft);
  };
  return <svg ref={root} className="drawing-objects" width={width} height={height} style={{ height, width }}
    aria-label="已保存的画线；选中后拖动锚点或整体移动" onPointerMove={update} onPointerUp={finish} onPointerCancel={cancel} onLostPointerCapture={cancel}>
    {[...items.filter((item) => item.drawing.id !== selectedId), ...items.filter((item) => item.drawing.id === selectedId)].map((original) => {
      const item = draft?.id === original.drawing.id ? projectDrawing(draft, project) ?? original : original;
      const { drawing, x1, y1, x2, y2, x3, y3 } = item;
      const selected = selectedId === drawing.id;
      const measure = drawing.type === "measure" ? drawingMeasure(drawing) : null;
      const date = (time: number) => new Date(time * 1000).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
      return <g key={drawing.id} data-drawing-id={drawing.id} data-drawing-type={drawing.type} data-selected={selected} data-locked={Boolean(drawing.locked)}>
        <title>{drawing.label}{drawing.locked ? " · 已锁定" : " · 拖动图形移动，选中后可编辑锚点"}{measure ? ` · ${date(drawing.start.time)} → ${date(drawing.end.time)}` : ""}</title>
        {enabled ? <g role="button" tabIndex={0} aria-label={`选择${drawing.label}`} style={{ pointerEvents: "stroke", cursor: drawing.locked ? "pointer" : "grab" }}
          onPointerDown={(event) => begin(event, drawing, "move")} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(drawing.id); } }}>
          <DrawingShape item={item} width={width} height={height} hit />
        </g> : null}
        <g style={{ pointerEvents: "none" }}><DrawingShape item={item} width={width} height={height} />
          {drawing.type !== "text" ? <text x={Math.max(4, Math.min(x1, x2) + 5)} y={Math.max(12, Math.min(y1, y2) - 6)} fill={drawing.color} fontSize={11}>{drawing.label}{drawing.type === "horizontal" ? ` · ${drawing.start.price.toFixed(2)}` : ""}{drawing.locked ? " · 已锁定" : ""}</text> : null}
          {measure ? <text x={Math.max(4, Math.min(width - 200, x1 + 5))} y={Math.max(28, Math.min(height - 45, y1 + 20))} fill={drawing.color} fontSize={10}>
            <tspan x={Math.max(4, Math.min(width - 200, x1 + 5))}>Δ {measure.change.toFixed(2)} · {measure.percent === null ? "—" : `${measure.percent.toFixed(2)}%`} · {Number(measure.elapsedSeconds.toFixed(1))}s</tspan>
            <tspan x={Math.max(4, Math.min(width - 200, x1 + 5))} dy={14}>{date(drawing.start.time)}</tspan><tspan x={Math.max(4, Math.min(width - 200, x1 + 5))} dy={14}>→ {date(drawing.end.time)}</tspan>
          </text> : null}
        </g>
        {enabled && selected && !drawing.locked ? (["start", ...(["horizontal", "vertical", "text"].includes(drawing.type) ? [] : ["end"]), ...(drawing.type === "channel" ? ["widthAnchor"] : [])] as const).map((anchor) => <circle
          key={anchor} data-anchor={anchor} cx={anchor === "start" ? x1 : anchor === "end" ? x2 : x3} cy={anchor === "start" ? y1 : anchor === "end" ? y2 : y3} r={6}
          fill="white" stroke={drawing.color} strokeWidth={2} style={{ pointerEvents: "all", cursor: "grab", touchAction: "none" }}
          onPointerDown={(event) => begin(event, drawing, anchor as "start" | "end" | "widthAnchor")} />) : null}
      </g>;
    })}
  </svg>;
}
