import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  GripVertical,
  RotateCcw,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { chartPeriodById, type ChartPeriodId } from "./chartPeriods";
import {
  defaultPeriodPreferences,
  movePeriod,
  normalizePeriodPreferences,
  reorderPeriodBefore,
  togglePeriodVisibility,
  type PeriodPreferences,
} from "./periodPreferences";

const STORAGE_KEY = "market-analysis.period-toolbar.v1";

interface PeriodToolbarProps {
  selectedId: ChartPeriodId;
  onSelect: (id: ChartPeriodId) => void;
}

type MenuMode = "more" | "edit" | null;

function readPreferences(): PeriodPreferences {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored ? normalizePeriodPreferences(JSON.parse(stored)) : defaultPeriodPreferences();
  } catch {
    return defaultPeriodPreferences();
  }
}

export function PeriodToolbar({ selectedId, onSelect }: PeriodToolbarProps) {
  const [preferences, setPreferences] = useState<PeriodPreferences>(readPreferences);
  const [menuMode, setMenuMode] = useState<MenuMode>(null);
  const [draggingId, setDraggingId] = useState<ChartPeriodId | null>(null);
  const [dragTargetId, setDragTargetId] = useState<ChartPeriodId | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  const orderedPeriods = useMemo(
    () => preferences.order.map((id) => chartPeriodById(id)),
    [preferences.order],
  );
  const visibleIds = useMemo(() => new Set(preferences.visible), [preferences.visible]);
  const visiblePeriods = orderedPeriods.filter((period) => visibleIds.has(period.id));
  const hiddenPeriods = orderedPeriods.filter((period) => !visibleIds.has(period.id));
  const selectedHidden = !visibleIds.has(selectedId);
  const selectedPeriod = chartPeriodById(selectedId);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    } catch {
      // Local persistence is optional; the toolbar remains fully usable without it.
    }
  }, [preferences]);

  useEffect(() => {
    const handleStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY || !event.newValue) return;
      try {
        setPreferences(normalizePeriodPreferences(JSON.parse(event.newValue)));
      } catch {
        // Ignore malformed updates from another tab.
      }
    };
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  useEffect(() => {
    if (menuMode === null) return;
    const closeOutside = (event: PointerEvent | FocusEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setMenuMode(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuMode(null);
    };
    document.addEventListener("pointerdown", closeOutside);
    document.addEventListener("focusin", closeOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOutside);
      document.removeEventListener("focusin", closeOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [menuMode]);

  const selectPeriod = (id: ChartPeriodId) => {
    onSelect(id);
    setMenuMode(null);
  };

  return (
    <div className="period-toolbar" ref={rootRef}>
      <div
        className="period-visible-list"
        role="tablist"
        aria-label="常用图表周期"
        onWheel={(event) => {
          if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) {
            event.currentTarget.scrollLeft += event.deltaY;
          }
        }}
      >
        {visiblePeriods.map((period) => (
          <button
            type="button"
            role="tab"
            key={period.id}
            className={selectedId === period.id ? "is-active" : ""}
            aria-selected={selectedId === period.id}
            title={period.aggregation.kind === "calendar" ? "根据当前可用分钟历史聚合" : undefined}
            onClick={() => selectPeriod(period.id)}
          >
            {period.label}
          </button>
        ))}
      </div>

      <button
        type="button"
        className={`period-more-button ${selectedHidden ? "is-active" : ""}`}
        aria-haspopup="menu"
        aria-expanded={menuMode !== null}
        title={selectedHidden ? `当前周期：${selectedPeriod.label}` : "更多周期"}
        onClick={() => setMenuMode((current) => current === null ? "more" : null)}
      >
        <span>{selectedHidden ? selectedPeriod.label : "更多"}</span>
        <ChevronDown size={12} />
      </button>

      {menuMode === "more" ? (
        <div className="period-more-menu" role="menu" aria-label="更多图表周期">
          <div className="period-popover-head">
            <strong>更多周期</strong>
            <span>{hiddenPeriods.length} 项已收起</span>
          </div>
          <div className="period-hidden-grid">
            {hiddenPeriods.length > 0 ? hiddenPeriods.map((period) => (
              <button
                type="button"
                role="menuitemradio"
                aria-checked={selectedId === period.id}
                key={period.id}
                className={selectedId === period.id ? "is-active" : ""}
                onClick={() => selectPeriod(period.id)}
              >
                {period.label}
              </button>
            )) : <p>所有周期都已显示在工具栏</p>}
          </div>
          <button type="button" className="period-edit-launch" onClick={() => setMenuMode("edit")}>
            <SlidersHorizontal size={14} />
            <span><strong>编辑周期栏</strong><small>显示、隐藏与排序</small></span>
          </button>
        </div>
      ) : null}

      {menuMode === "edit" ? (
        <div className="period-editor" role="dialog" aria-label="编辑周期栏">
          <div className="period-editor-head">
            <div><strong>编辑周期栏</strong><span>已显示 {preferences.visible.length} / {orderedPeriods.length}</span></div>
            <div>
              <button
                type="button"
                className="period-reset-button"
                title="恢复默认周期"
                onClick={() => setPreferences(defaultPeriodPreferences())}
              >
                <RotateCcw size={13} />重置
              </button>
              <button type="button" className="period-editor-close" title="关闭" onClick={() => setMenuMode(null)}>
                <X size={15} />
              </button>
            </div>
          </div>
          <div className="period-editor-guide">拖动调整顺序，也可使用上下按钮</div>
          <div className="period-editor-list">
            {orderedPeriods.map((period, index) => {
              const isVisible = visibleIds.has(period.id);
              const lastVisible = isVisible && preferences.visible.length === 1;
              return (
                <div
                  className={`period-editor-row ${isVisible ? "is-visible" : ""} ${draggingId === period.id ? "is-dragging" : ""} ${dragTargetId === period.id ? "is-drag-target" : ""}`}
                  key={period.id}
                  draggable
                  onDragStart={(event) => {
                    setDraggingId(period.id);
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", period.id);
                  }}
                  onDragOver={(event) => {
                    event.preventDefault();
                    event.dataTransfer.dropEffect = "move";
                    setDragTargetId(period.id);
                  }}
                  onDrop={(event) => {
                    event.preventDefault();
                    if (draggingId) {
                      setPreferences((current) => reorderPeriodBefore(current, draggingId, period.id));
                    }
                    setDraggingId(null);
                    setDragTargetId(null);
                  }}
                  onDragEnd={() => {
                    setDraggingId(null);
                    setDragTargetId(null);
                  }}
                >
                  <GripVertical className="period-drag-handle" size={14} aria-hidden="true" />
                  <span className="period-editor-label"><strong>{period.label}</strong><small>{isVisible ? "工具栏" : "更多"}</small></span>
                  <button
                    type="button"
                    className="period-order-button"
                    aria-label={`上移${period.label}`}
                    disabled={index === 0}
                    onClick={() => setPreferences((current) => movePeriod(current, period.id, -1))}
                  >
                    <ArrowUp size={12} />
                  </button>
                  <button
                    type="button"
                    className="period-order-button"
                    aria-label={`下移${period.label}`}
                    disabled={index === orderedPeriods.length - 1}
                    onClick={() => setPreferences((current) => movePeriod(current, period.id, 1))}
                  >
                    <ArrowDown size={12} />
                  </button>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={isVisible}
                    aria-label={`${isVisible ? "隐藏" : "显示"}${period.label}`}
                    className={`period-visibility-toggle ${isVisible ? "is-on" : ""}`}
                    disabled={lastVisible}
                    title={lastVisible ? "至少保留一个常用周期" : isVisible ? "收进更多" : "显示在工具栏"}
                    onClick={() => setPreferences((current) => togglePeriodVisibility(current, period.id))}
                  >
                    <span />
                  </button>
                </div>
              );
            })}
          </div>
          <div className="period-editor-footer">
            <span>更改已自动保存到本机</span>
            <button type="button" onClick={() => setMenuMode(null)}>完成</button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
