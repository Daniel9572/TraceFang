import { useRef, useState, type PointerEvent } from "react";
import type { ExpertDrawing, ExpertDrawingPoint } from "./expertTypes";

export interface DrawingProjection {
  drawing: ExpertDrawing;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export function DrawingOverlay({
  items,
  width,
  height,
  enabled,
  locate,
  onUpdate,
}: {
  items: DrawingProjection[];
  width: number;
  height: number;
  enabled: boolean;
  locate: (x: number, y: number) => ExpertDrawingPoint | null;
  onUpdate?: (drawing: ExpertDrawing) => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<{
    id: string;
    anchor: "start" | "end";
    x: number;
    y: number;
  } | null>(null);
  const root = useRef<SVGSVGElement>(null);
  const drag = useRef<{
    drawing: ExpertDrawing;
    anchor: "start" | "end";
  } | null>(null);
  const coordinates = (event: PointerEvent) => {
    const rect = root.current!.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  const finish = (event: PointerEvent<SVGSVGElement>) => {
    const current = drag.current;
    drag.current = null;
    setDraft(null);
    if (!current) return;
    const { x, y } = coordinates(event);
    const point = locate(x, y);
    if (!point) return;
    const updated = { ...current.drawing, [current.anchor]: point };
    if (updated.type === "horizontal") {
      updated.start = { ...updated.start, price: point.price };
      updated.end = { ...updated.end, price: point.price };
    }
    onUpdate?.(updated);
  };
  return (
    <svg
      ref={root}
      className="drawing-objects"
      width={width}
      height={height}
      style={{ height, width }}
      aria-label="已保存的画线；选中后拖动端点编辑"
      onPointerMove={(event) => {
        if (!drag.current) return;
        setDraft({
          id: drag.current.drawing.id,
          anchor: drag.current.anchor,
          ...coordinates(event),
        });
      }}
      onPointerUp={finish}
      onPointerCancel={() => {
        drag.current = null;
        setDraft(null);
      }}
    >
      {items.map(({ drawing, ...position }) => {
        let { x1, y1, x2, y2 } = position;
        if (draft?.id === drawing.id) {
          if (draft.anchor === "start") {
            x1 = draft.x;
            y1 = draft.y;
          } else {
            x2 = draft.x;
            y2 = draft.y;
          }
          if (drawing.type === "horizontal") y1 = y2 = draft.y;
        }
        const pick = () => setSelected(drawing.id);
        const interactive = enabled && Boolean(onUpdate);
        const common = {
          stroke: drawing.color,
          strokeWidth: 2,
          fill: "none",
          onClick: pick,
          style: {
            pointerEvents: interactive
              ? ("stroke" as const)
              : ("none" as const),
            cursor: "pointer",
          },
        };
        const hit = { ...common, stroke: "transparent", strokeWidth: 14 };
        if (drawing.type === "horizontal") {
          x1 = 24;
          x2 = width - 24;
        }
        return (
          <g key={drawing.id}>
            <title>{drawing.label} · 点击选择，拖动端点修改</title>
            {interactive ? (
              drawing.type === "rectangle" ? (
                <rect
                  {...hit}
                  x={Math.min(x1, x2)}
                  y={Math.min(y1, y2)}
                  width={Math.abs(x2 - x1)}
                  height={Math.abs(y2 - y1)}
                />
              ) : drawing.type === "fibonacci" ? (
                [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].map((ratio) => (
                  <line
                    key={ratio}
                    {...hit}
                    x1={x1}
                    x2={x2}
                    y1={y1 + (y2 - y1) * ratio}
                    y2={y1 + (y2 - y1) * ratio}
                  />
                ))
              ) : (
                <line
                  {...hit}
                  x1={drawing.type === "horizontal" ? 0 : x1}
                  x2={drawing.type === "horizontal" ? width : x2}
                  y1={y1}
                  y2={drawing.type === "horizontal" ? y1 : y2}
                />
              )
            ) : null}
            {drawing.type === "horizontal" ? (
              <line {...common} x1={0} x2={width} y1={y1} y2={y1} />
            ) : drawing.type === "rectangle" ? (
              <rect
                {...common}
                x={Math.min(x1, x2)}
                y={Math.min(y1, y2)}
                width={Math.abs(x2 - x1)}
                height={Math.abs(y2 - y1)}
                fill={drawing.color}
                fillOpacity={0.07}
              />
            ) : drawing.type === "fibonacci" ? (
              [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1].map((ratio) => (
                <g key={ratio}>
                  <line
                    {...common}
                    x1={x1}
                    x2={x2}
                    y1={y1 + (y2 - y1) * ratio}
                    y2={y1 + (y2 - y1) * ratio}
                  />
                  <text
                    x={Math.min(x1, x2) + 4}
                    y={y1 + (y2 - y1) * ratio - 4}
                    fill={drawing.color}
                    fontSize={10}
                  >
                    {(ratio * 100).toFixed(1)}%
                  </text>
                </g>
              ))
            ) : (
              <line {...common} x1={x1} y1={y1} x2={x2} y2={y2} />
            )}
            <text
              x={Math.max(4, Math.min(x1, x2) + 5)}
              y={Math.max(12, Math.min(y1, y2) - 6)}
              fill={drawing.color}
              fontSize={11}
            >
              {drawing.label}
              {drawing.type === "horizontal"
                ? ` · ${drawing.start.price.toFixed(2)}`
                : ""}
            </text>
            {interactive && selected === drawing.id
              ? (["start", "end"] as const).map((anchor) => (
                  <circle
                    key={anchor}
                    cx={anchor === "start" ? x1 : x2}
                    cy={anchor === "start" ? y1 : y2}
                    r={6}
                    fill="white"
                    stroke={drawing.color}
                    strokeWidth={2}
                    style={{ pointerEvents: "all", cursor: "grab" }}
                    onPointerDown={(event) => {
                      event.preventDefault();
                      event.stopPropagation();
                      root.current!.setPointerCapture(event.pointerId);
                      drag.current = { drawing, anchor };
                    }}
                  />
                ))
              : null}
          </g>
        );
      })}
    </svg>
  );
}
