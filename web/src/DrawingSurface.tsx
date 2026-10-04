import { useEffect, useRef, useState, type PointerEvent } from "react";
import { DRAWING_TOOLS } from "./DrawingControls";
import { DrawingShape, projectDrawing, type DrawingLocator, type DrawingProjector } from "./DrawingOverlay";
import type { ExpertDrawing, ExpertDrawingPoint, ExpertDrawingTool } from "./expertTypes";

export function DrawingSurface({ tool, width, height, locate, project, onCommit, scope }: {
  tool: ExpertDrawingTool; width: number; height: number; locate: DrawingLocator; project: DrawingProjector;
  onCommit: (drawing: ExpertDrawing) => void; scope: string;
}) {
  const points = useRef<ExpertDrawingPoint[]>([]);
  const pointer = useRef<{ id: number; x: number; y: number; first: boolean } | null>(null);
  const [preview, setPreview] = useState<ExpertDrawing | null>(null);
  const root = useRef<HTMLDivElement>(null);
  const cancel = () => { points.current = []; pointer.current = null; setPreview(null); };
  useEffect(() => { cancel(); }, [tool, scope]);
  useEffect(() => {
    const key = (event: KeyboardEvent) => { if (event.key === "Escape") cancel(); };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, []);
  const coordinates = (event: PointerEvent) => {
    const bounds = root.current!.getBoundingClientRect();
    return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
  };
  const makeDrawing = (anchors: ExpertDrawingPoint[]): ExpertDrawing => {
    const start = anchors[0], end = anchors[1] ?? start;
    return { id: "preview", type: tool, start,
      end: tool === "horizontal" ? { ...end, price: start.price } : tool === "vertical" ? { ...end, time: start.time } : end,
      ...(tool === "channel" ? { widthAnchor: anchors[2] ?? end } : {}),
      ...(tool === "text" ? { text: "文字注释" } : {}), color: "#245c8c", label: DRAWING_TOOLS.find((item) => item.id === tool)!.label };
  };
  const required = ["horizontal", "vertical", "text"].includes(tool) ? 1 : tool === "channel" ? 3 : 2;
  const finish = (anchors: ExpertDrawingPoint[]) => {
    const drawing = makeDrawing(anchors);
    cancel();
    onCommit({ ...drawing, id: `drawing:${crypto.randomUUID()}` });
  };
  const move = (event: PointerEvent) => {
    if (!points.current.length) return;
    const { x, y } = coordinates(event);
    const point = locate(x, y, event.ctrlKey || event.metaKey);
    if (point) setPreview(makeDrawing(required === 1 ? [point] : [...points.current, point]));
  };
  const item = preview ? projectDrawing(preview, project) : null;
  return <div ref={root} className="chart-drawing-surface" data-tool={tool} style={{ width, height, right: "auto", bottom: "auto" }}
    role="application" aria-label={tool === "channel" ? "点击基线两点与宽度锚点" : required === 1 ? "点击放置绘图锚点" : "点击起点和终点，或拖动绘制"} tabIndex={0}
    onPointerDown={(event) => {
      if (event.button !== 0) return;
      event.preventDefault(); event.stopPropagation();
      const position = coordinates(event);
      const point = locate(position.x, position.y, event.ctrlKey || event.metaKey);
      if (!point) return;
      pointer.current = { id: event.pointerId, ...position, first: !points.current.length };
      event.currentTarget.setPointerCapture(event.pointerId);
      if (!points.current.length) { points.current = [point]; setPreview(makeDrawing([point])); }
    }} onPointerMove={move} onPointerUp={(event) => {
      const active = pointer.current;
      if (!active || active.id !== event.pointerId) return;
      const { x, y } = coordinates(event);
      const point = locate(x, y, event.ctrlKey || event.metaKey);
      pointer.current = null;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      if (!point) { cancel(); return; }
      if (required === 1) { finish([point]); return; }
      if (active.first && Math.hypot(x - active.x, y - active.y) < 4) return;
      if (points.current.length === 1 && point.time === points.current[0].time && point.price === points.current[0].price) return;
      const next = [...points.current, point];
      if (next.length >= required) finish(next);
      else { points.current = next; setPreview(makeDrawing(next)); }
    }} onPointerCancel={cancel} onLostPointerCapture={() => { if (pointer.current) cancel(); }}>
    {item ? <svg className="chart-drawing-preview" width={width} height={height} aria-hidden="true"><DrawingShape item={item} width={width} height={height} /></svg> : null}
  </div>;
}
