import { Check, ChevronDown, ChevronRight, SlidersHorizontal } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  dataLatencyMilliseconds,
  formatQuotaPercent,
  formatSamplingInterval,
  formatSourceLatency,
  sourceAccessLabels,
  sourceHealthLabels,
} from "./sourcePresentation";
import type { SourceDescriptor, SourceId } from "./types";

interface SourcePickerProps {
  sources: SourceDescriptor[];
  selectedSource: SourceId;
  fallbackLabel: string;
  busy: boolean;
  connectionState: "connecting" | "live" | "unavailable";
  connectionError: string | null;
  quoteObservedAt: string | null;
  quoteReceivedAt: string | null;
  onSelect: (source: SourceDescriptor) => void | Promise<void>;
  onManage: () => void;
}

export function SourcePicker({
  sources,
  selectedSource,
  fallbackLabel,
  busy,
  connectionState,
  connectionError,
  quoteObservedAt,
  quoteReceivedAt,
  onSelect,
  onManage,
}: SourcePickerProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const manageRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const options = useMemo(
    () => sources.filter(
      (source) => source.capabilities.includes("quote") && source.capabilities.includes("candles"),
    ),
    [sources],
  );
  const selected = options.find((source) => source.source_id === selectedSource)
    ?? sources.find((source) => source.source_id === selectedSource);
  const samplingInterval = formatSamplingInterval(
    selected?.quote_poll_interval_seconds,
    selected?.quote_streaming,
  );
  const selectedLatencyMs = dataLatencyMilliseconds(quoteObservedAt, quoteReceivedAt);
  const selectedLatency = formatSourceLatency(quoteObservedAt, quoteReceivedAt);
  const samplingDescription = selected?.quote_streaming
    ? "更新方式 实时推送"
    : `采样间隔 ${samplingInterval}`;
  const selectedPeakQuota = selected?.quotas.reduce(
    (peak, quota) => quota.usage_percent > peak.usage_percent ? quota : peak,
    selected.quotas[0],
  );
  const awaitingManualConnection = Boolean(
    selected?.manual_connection_required && !selected.connection_active,
  );
  const connectionLabel = awaitingManualConnection
    ? "待连接"
    : connectionState === "live"
      ? "在线"
      : connectionState === "connecting"
        ? "连接中"
        : "已停滞";
  const selectedHealth = awaitingManualConnection
    ? "unknown"
    : connectionState === "live"
      ? "healthy"
      : connectionState === "connecting"
        ? "unknown"
        : "unavailable";
  const triggerMetric = awaitingManualConnection
    ? selectedPeakQuota
      ? `今日用量 ${formatQuotaPercent(selectedPeakQuota.usage_percent)}`
      : "需要主动连接"
    : connectionState === "live"
      ? selectedLatencyMs === null
        ? "等待延迟样本"
        : `延迟 ${selectedLatency}`
      : connectionState === "connecting"
        ? "等待首帧"
        : "数据停滞";

  const focusOption = useCallback((index: number) => {
    if (options.length === 0) return;
    const nextIndex = (index + options.length) % options.length;
    setActiveIndex(nextIndex);
    window.requestAnimationFrame(() => optionRefs.current[nextIndex]?.focus());
  }, [options.length]);

  const openMenu = useCallback((direction: "selected" | "first" | "last" = "selected") => {
    if (busy || options.length === 0) return;
    const selectedIndex = Math.max(0, options.findIndex((source) => source.source_id === selectedSource));
    const nextIndex = direction === "first"
      ? 0
      : direction === "last"
        ? options.length - 1
        : selectedIndex;
    setOpen(true);
    focusOption(nextIndex);
  }, [busy, focusOption, options, selectedSource]);

  const closeMenu = useCallback((restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  }, []);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    const handleFocusIn = (event: FocusEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) closeMenu();
    };
    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("focusin", handleFocusIn);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("focusin", handleFocusIn);
    };
  }, [closeMenu, open]);

  const chooseSource = (source: SourceDescriptor) => {
    closeMenu(true);
    if (source.source_id !== selectedSource) void onSelect(source);
  };

  const manageSources = () => {
    closeMenu();
    onManage();
  };

  return (
    <div className={`source-picker health-${selectedHealth} ${open ? "is-open" : ""}`} ref={rootRef}>
      <button
        type="button"
        ref={triggerRef}
        className="source-picker-trigger"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls="market-source-options"
        disabled={busy}
        title={connectionError ?? `${selected?.display_name ?? fallbackLabel}；${connectionLabel}；${selectedLatencyMs === null ? "暂无延迟样本" : `估算采集延迟 ${selectedLatency}`}；${samplingDescription}`}
        onClick={() => {
          if (open) closeMenu();
          else openMenu();
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            openMenu(event.key === "ArrowDown" ? "first" : "last");
          }
        }}
      >
        <span className="source-picker-copy">
          <span className="source-picker-name">
            <strong>{selected?.display_name ?? fallbackLabel}</strong>
            {selected && selected.access_model !== "unmetered" ? (
              <span className={`source-access-badge is-${selected.access_model}`}>
                {sourceAccessLabels[selected.access_model]}
                {selectedPeakQuota ? ` ${formatQuotaPercent(selectedPeakQuota.usage_percent)}` : ""}
              </span>
            ) : null}
          </span>
          <small>
            <span className="source-picker-status-dot" aria-hidden="true" />
            <span className="source-picker-status-label">{connectionLabel}</span>
            <span aria-hidden="true">·</span>
            <span>{triggerMetric}</span>
          </small>
        </span>
        <ChevronDown className="source-picker-chevron" size={14} aria-hidden="true" />
      </button>

      {open ? (
        <div
          id="market-source-options"
          className="source-picker-menu"
          role="listbox"
          aria-label="行情源"
          onKeyDown={(event) => {
            if (event.key === "ArrowDown") {
              event.preventDefault();
              focusOption(activeIndex + 1);
            } else if (event.key === "ArrowUp") {
              event.preventDefault();
              focusOption(activeIndex - 1);
            } else if (event.key === "Home") {
              event.preventDefault();
              focusOption(0);
            } else if (event.key === "End") {
              event.preventDefault();
              focusOption(options.length - 1);
            } else if (event.key === "Escape") {
              event.preventDefault();
              closeMenu(true);
            } else if (event.key === "Tab") {
              const focusedOption = optionRefs.current.findIndex((node) => node === document.activeElement);
              if (event.shiftKey) {
                if (document.activeElement === manageRef.current) {
                  event.preventDefault();
                  focusOption(options.length - 1);
                } else if (focusedOption > 0) {
                  event.preventDefault();
                  focusOption(focusedOption - 1);
                } else if (focusedOption === 0) {
                  event.preventDefault();
                  closeMenu(true);
                }
              } else if (focusedOption >= 0 && focusedOption < options.length - 1) {
                event.preventDefault();
                focusOption(focusedOption + 1);
              } else if (focusedOption === options.length - 1) {
                event.preventDefault();
                manageRef.current?.focus();
              }
            }
          }}
        >
          <div className="source-picker-menu-head">
            <strong>行情来源</strong>
            <span>延迟 / 今日用量</span>
          </div>
          <div className="source-picker-options">
            {options.map((source, index) => {
              const isSelected = source.source_id === selectedSource;
              const awaitingConnection = source.manual_connection_required && !source.connection_active;
              const optionHealth = awaitingConnection
                ? "unknown"
                : isSelected
                  ? selectedHealth
                  : source.health;
              const optionState = awaitingConnection
                ? "待连接"
                : isSelected
                  ? connectionLabel
                  : sourceHealthLabels[source.health];
              const quoteQuota = source.quotas.find((quota) => quota.key === "get_quote");
              const candleQuota = source.quotas.find((quota) => quota.key === "get_kline");
              const optionPeakQuota = source.quotas.reduce(
                (peak, quota) => quota.usage_percent > peak.usage_percent ? quota : peak,
                source.quotas[0],
              );
              const optionSampling = formatSamplingInterval(
                source.quote_poll_interval_seconds,
                source.quote_streaming,
              );
              const hasLiveLatency = isSelected
                && optionState === "在线"
                && selectedLatencyMs !== null;
              const optionMetric = hasLiveLatency
                ? selectedLatency
                : quoteQuota && candleQuota
                  ? `报 ${quoteQuota.used.toLocaleString("zh-CN")} · K ${candleQuota.used.toLocaleString("zh-CN")}`
                  : optionPeakQuota
                    ? `${optionPeakQuota.used.toLocaleString("zh-CN")} / ${optionPeakQuota.limit.toLocaleString("zh-CN")}`
                  : source.access_model === "metered"
                    ? "按调用"
                    : optionSampling;
              const optionMetricLabel = hasLiveLatency
                ? "采集延迟"
                : optionPeakQuota
                  ? `最高用量 · ${formatQuotaPercent(optionPeakQuota.usage_percent)}`
                  : source.access_model === "metered"
                    ? "计费通道"
                    : "采样间隔";
              return (
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  key={source.source_id}
                  ref={(node) => { optionRefs.current[index] = node; }}
                  className={`source-picker-option health-${optionHealth} access-${source.access_model} ${isSelected ? "is-selected" : ""}`}
                  disabled={!source.enabled || awaitingConnection}
                  title={awaitingConnection ? "请先在行情源管理中连接并测试此限额来源" : undefined}
                  onFocus={() => setActiveIndex(index)}
                  onClick={() => chooseSource(source)}
                >
                  <span className="source-option-dot" aria-hidden="true" />
                  <span className="source-option-copy">
                    <strong>{source.display_name}</strong>
                    <small>
                      {optionState}
                      {` · ${source.access_model !== "unmetered"
                        ? sourceAccessLabels[source.access_model]
                        : source.quote_streaming
                          ? optionSampling
                          : `采样 ${optionSampling}`}`}
                    </small>
                  </span>
                  <span className="source-option-metric">
                    <strong>{optionMetric}</strong>
                    <small>{optionMetricLabel}</small>
                  </span>
                  <Check className="source-option-check" size={14} aria-hidden="true" />
                </button>
              );
            })}
          </div>
          <button
            type="button"
            ref={manageRef}
            className="source-picker-manage"
            onClick={manageSources}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                manageSources();
              }
            }}
          >
            <SlidersHorizontal size={14} aria-hidden="true" />
            <span><strong>管理行情源</strong><small>连接、额度与数据范围</small></span>
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </div>
      ) : null}
    </div>
  );
}
