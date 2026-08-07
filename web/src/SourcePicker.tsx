import {
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Gauge,
  PlugZap,
  Radio,
  RefreshCw,
  ShieldCheck,
  Timer,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  formatQuotaPercent,
  formatRefreshFrequency,
  quoteServiceLabels,
  quoteServiceNotes,
  quotaTone,
  sourceAccessLabels,
  sourceHealthLabels,
} from "./sourcePresentation";
import type { QuoteServiceTier, SourceDescriptor, SourceId } from "./types";

export interface SourceTestFeedback {
  tone: "success" | "error";
  message: string;
}

interface SourcePickerProps {
  sources: SourceDescriptor[];
  selectedSource: SourceId;
  fallbackLabel: string;
  busy: boolean;
  contractCode: string;
  connectionState: "connecting" | "live" | "unavailable";
  connectionError: string | null;
  testingSourceId: SourceId | null;
  testResults: Partial<Record<SourceId, SourceTestFeedback>>;
  notice: string | null;
  onSelect: (source: SourceDescriptor) => void | Promise<void>;
  onToggle: (source: SourceDescriptor) => void | Promise<void>;
  onTest: (source: SourceDescriptor) => void | Promise<void>;
  onOpenChange?: (open: boolean) => void;
}

const capabilityLabels: Record<string, string> = {
  quote: "报价",
  candles: "K 线",
  catalog: "品种",
  news: "资讯",
  calendar: "日历",
};

const quoteFieldLabels: Record<string, string> = {
  last: "现价",
  change: "涨跌额",
  change_percent: "涨跌幅",
  open: "今开",
  high: "最高",
  low: "最低",
  volume: "成交量",
};

function ServiceTierIcon({ tier, size = 10 }: { tier: QuoteServiceTier; size?: number }) {
  if (tier === "institutional") return <ShieldCheck size={size} strokeWidth={2.2} aria-hidden="true" />;
  if (tier === "enhanced") return <Gauge size={size} strokeWidth={2.2} aria-hidden="true" />;
  if (tier === "standard") return <Radio size={size} strokeWidth={2.2} aria-hidden="true" />;
  return <Timer size={size} strokeWidth={2.2} aria-hidden="true" />;
}

function formatQuotaReset(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "北京时间重置";
  return date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }) + " 重置";
}

function sourceUpdateLabel(source: SourceDescriptor): string {
  if (source.frozen) return "暂停使用";
  if (source.source_kind === "composite") return "双通道事件驱动";
  if (source.quote_service_tier === "enhanced" && source.quote_streaming) return "变化即推送";
  return formatRefreshFrequency(
    source.quote_poll_interval_seconds,
    source.quote_streaming,
  );
}

export function SourcePicker({
  sources,
  selectedSource,
  fallbackLabel,
  busy,
  contractCode,
  connectionState,
  connectionError,
  testingSourceId,
  testResults,
  notice,
  onSelect,
  onToggle,
  onTest,
  onOpenChange,
}: SourcePickerProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const options = useMemo(
    () => sources.filter((source) => source.capabilities.includes("quote")),
    [sources],
  );
  const selected = options.find((source) => source.source_id === selectedSource)
    ?? sources.find((source) => source.source_id === selectedSource);
  const selectedServiceTier = selected?.quote_service_tier ?? "reference";
  const selectedServiceLabel = quoteServiceLabels[selectedServiceTier];
  const selectedPeakQuota = selected?.quotas.reduce(
    (peak, quota) => quota.usage_percent > peak.usage_percent ? quota : peak,
    selected.quotas[0],
  );
  const selectedEnabled = selected?.enabled ?? false;
  const awaitingManualConnection = Boolean(
    selectedEnabled
    && selected?.manual_connection_required
    && !selected.connection_active,
  );
  const connectionLabel = selected?.frozen
    ? "已冻结"
    : !selectedEnabled
      ? "已停用"
      : awaitingManualConnection
      ? "待连接"
      : connectionState === "live"
        ? "在线"
        : connectionState === "connecting"
          ? "连接中"
          : "已停滞";
  const selectedHealth = selected?.frozen
    ? "frozen"
    : !selectedEnabled || awaitingManualConnection
      ? "unknown"
      : connectionState === "live"
      ? "healthy"
      : connectionState === "connecting"
        ? "unknown"
        : "unavailable";

  const setMenuOpen = useCallback((next: boolean) => {
    setOpen(next);
    onOpenChange?.(next);
  }, [onOpenChange]);

  const closeMenu = useCallback((restoreFocus = false) => {
    setMenuOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  }, [setMenuOpen]);

  const openMenu = useCallback((focusFirstControl = false) => {
    if (busy || options.length === 0) return;
    setMenuOpen(true);
    if (focusFirstControl) {
      window.requestAnimationFrame(() => {
        menuRef.current?.querySelector<HTMLButtonElement>("button:not(:disabled)")?.focus();
      });
    }
  }, [busy, options.length, setMenuOpen]);

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

  const enabledCount = options.filter((source) => source.enabled).length;
  const triggerTitle = connectionError ?? [
    selected?.display_name ?? fallbackLabel,
    selectedServiceLabel,
    connectionLabel,
    selected ? sourceUpdateLabel(selected) : "待检测",
  ].join("；");

  return (
    <div
      className={[
        "source-picker",
        "health-" + selectedHealth,
        "tier-" + selectedServiceTier,
        open ? "is-open" : "",
      ].filter(Boolean).join(" ")}
      ref={rootRef}
    >
      <button
        type="button"
        ref={triggerRef}
        className="source-picker-trigger"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls="market-source-console"
        disabled={busy && !open}
        title={triggerTitle}
        onClick={() => {
          if (open) closeMenu();
          else openMenu();
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            openMenu(true);
          } else if (event.key === "Escape" && open) {
            event.preventDefault();
            closeMenu(true);
          }
        }}
      >
        <span className="source-picker-copy">
          <span className="source-picker-name">
            <strong>{selected?.display_name ?? fallbackLabel}</strong>
            {selected && selected.access_model !== "unmetered" ? (
              <span className={"source-access-badge is-" + selected.access_model}>
                {sourceAccessLabels[selected.access_model]}
                {selectedPeakQuota ? " " + formatQuotaPercent(selectedPeakQuota.usage_percent) : ""}
              </span>
            ) : null}
          </span>
          <small className="source-picker-signal-row">
            <span className="source-picker-tier-icon">
              <ServiceTierIcon tier={selectedServiceTier} />
            </span>
            <span className="source-picker-tier-label">{selectedServiceLabel}</span>
            <span className="source-picker-connection">
              <span className="source-picker-status-dot" aria-hidden="true" />
              <span className="source-picker-status-label">{connectionLabel}</span>
            </span>
          </small>
        </span>
        <ChevronDown className="source-picker-chevron" size={14} aria-hidden="true" />
      </button>

      {open ? (
        <div
          id="market-source-console"
          className="source-picker-menu"
          role="dialog"
          aria-label={contractCode + " 实时行情源控制台"}
          ref={menuRef}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              closeMenu(true);
            }
          }}
        >
          <div className="source-console-head">
            <div>
              <span className="source-console-title">
                <strong>实时来源</strong>
                <b>{contractCode}</b>
              </span>
              <small>每个合约独立设置，历史数据统一读取本地库</small>
            </div>
            <span className="source-console-count">{enabledCount}/{options.length} 已启用</span>
          </div>

          <div className="source-console-list">
            {options.map((source) => {
              const isSelected = source.source_id === selectedSource;
              const awaitingConnection = source.enabled
                && source.manual_connection_required
                && !source.connection_active;
              const isTesting = testingSourceId === source.source_id;
              const testResult = testResults[source.source_id];
              const supportsRealtime = source.capabilities.includes("quote");
              const optionHealth = source.frozen
                ? "frozen"
                : !source.enabled || awaitingConnection
                  ? "unknown"
                  : isSelected
                  ? selectedHealth
                  : source.health;
              const optionState = source.frozen
                ? "已冻结"
                : !source.enabled
                  ? "已停用"
                  : awaitingConnection
                  ? "待连接"
                  : isSelected
                    ? connectionLabel
                    : sourceHealthLabels[source.health];
              const quoteQuota = source.quotas.find((quota) => quota.key === "get_quote");
              const candleQuota = source.quotas.find((quota) => quota.key === "get_kline");
              const optionServiceTier = source.quote_service_tier;
              const optionServiceLabel = quoteServiceLabels[optionServiceTier];
              const capabilitySummary = source.capabilities
                .map((capability) => capabilityLabels[capability] ?? capability)
                .join(" · ");
              const accessSummary = source.frozen
                ? "不调用"
                : quoteQuota && candleQuota
                  ? "报 " + quoteQuota.used.toLocaleString("zh-CN") + " · K "
                    + candleQuota.used.toLocaleString("zh-CN")
                  : sourceAccessLabels[source.access_model];
              const cardClassName = [
                "source-console-card",
                "tier-" + optionServiceTier,
                "health-" + optionHealth,
                isSelected ? "is-current" : "",
                source.enabled ? "" : "is-disabled",
                source.frozen ? "is-frozen" : "",
                "kind-" + source.source_kind,
              ].filter(Boolean).join(" ");
              return (
                <section className={cardClassName} key={source.source_id}>
                  <div className="source-console-card-head">
                    <span className="source-console-signal-mark">
                      <ServiceTierIcon tier={optionServiceTier} size={14} />
                    </span>
                    <div className="source-console-identity">
                      <span>
                        <strong>{source.display_name}</strong>
                        {isSelected ? (
                          <b className="source-console-current"><Check size={9} />当前</b>
                        ) : null}
                      </span>
                      <small>
                        <span className="source-console-health-dot" aria-hidden="true" />
                        {optionState}
                        <span aria-hidden="true">·</span>
                        {optionServiceLabel}
                      </small>
                    </div>
                    <div className="source-console-enable">
                      <span>{source.frozen ? "冻结" : source.enabled ? "启用" : "停用"}</span>
                      <button
                        type="button"
                        role="switch"
                        aria-checked={source.enabled}
                        aria-label={source.frozen
                          ? source.display_name + "已冻结"
                          : (source.enabled ? "停用" : "启用") + source.display_name}
                        className={"source-console-switch " + (source.enabled ? "is-on" : "")}
                        onClick={() => void onToggle(source)}
                        disabled={busy || source.frozen}
                      >
                        <span />
                      </button>
                    </div>
                  </div>

                  <p className="source-console-description" title={source.description}>
                    {source.description}
                  </p>

                  <div className="source-console-facts">
                    <span>
                      <small>更新</small>
                      <strong>{sourceUpdateLabel(source)}</strong>
                    </span>
                    <span>
                      <small>运行</small>
                      <strong>{source.frozen ? "归档冻结" : source.source_kind === "composite" ? "显式组合" : source.requires_running_app ? "需桌面端" : "独立通道"}</strong>
                    </span>
                    <span>
                      <small>{source.quotas.length > 0 ? "今日用量" : "访问"}</small>
                      <strong>{accessSummary}</strong>
                    </span>
                  </div>

                  <div className="source-console-meta">
                    <span className={"source-tier-note is-" + optionServiceTier}>
                      {source.frozen ? "能力归档，不参与数据链路" : quoteServiceNotes[optionServiceTier]}
                    </span>
                    <span>{source.structured ? "结构化" : "非结构化"}</span>
                    <span>{capabilitySummary}</span>
                    {source.access_model !== "unmetered" ? (
                      <span className={"source-access-badge is-" + source.access_model}>
                        {sourceAccessLabels[source.access_model]}
                      </span>
                    ) : null}
                  </div>

                  {source.source_kind === "composite" ? (
                    <div className="source-console-composition">
                      <div className="source-console-composition-head">
                        <strong>固定字段归属</strong>
                        <span>{source.component_source_ids.length} 个原始通道 · 分别落库</span>
                      </div>
                      {source.field_ownership.map((ownership) => (
                        <div className="source-console-owner" key={ownership.source_id}>
                          <b>{ownership.source_id}</b>
                          <span>{ownership.fields.map((field) => quoteFieldLabels[field] ?? field).join(" · ")}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {source.quotas.length > 0 ? (
                    <div className="source-console-quotas">
                      <div className="source-console-quota-head">
                        <span>额度</span>
                        <small>{formatQuotaReset(source.quotas[0].resets_at)}</small>
                      </div>
                      {source.quotas.map((quota) => (
                        <div className={"source-console-quota is-" + quotaTone(quota)} key={quota.key}>
                          <span>{quota.label}</span>
                          <span className="source-console-quota-track">
                            <span style={{ width: String(Math.min(100, quota.usage_percent)) + "%" }} />
                          </span>
                          <strong>{formatQuotaPercent(quota.usage_percent)}</strong>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {source.error && optionHealth !== "healthy" ? (
                    <p className="source-console-error">{source.error}</p>
                  ) : null}

                  <div className="source-console-actions">
                    <button
                      type="button"
                      className={"source-console-action source-console-use " + (isSelected ? "is-current" : "")}
                      onClick={() => void onSelect(source)}
                      disabled={busy || source.frozen || !source.enabled || awaitingConnection || !supportsRealtime || isSelected}
                    >
                      {isSelected ? <Check size={13} /> : null}
                      {isSelected ? contractCode + " 当前来源" : "用于 " + contractCode}
                    </button>
                    <button
                      type="button"
                      className="source-console-action source-console-test"
                      onClick={() => void onTest(source)}
                      disabled={busy || source.frozen || testingSourceId !== null || !source.enabled}
                    >
                      {isTesting
                        ? <RefreshCw size={13} className="spin" />
                        : <PlugZap size={13} />}
                      {source.frozen
                        ? "已冻结"
                        : isTesting
                          ? "测试中…"
                          : awaitingConnection
                          ? "连接并测试"
                          : "测试连接"}
                    </button>
                  </div>

                  {testResult ? (
                    <div className={"source-console-result is-" + testResult.tone}>
                      {testResult.tone === "success"
                        ? <CheckCircle2 size={13} />
                        : <CircleAlert size={13} />}
                      <span>{testResult.message}</span>
                    </div>
                  ) : null}
                </section>
              );
            })}
          </div>

          {notice ? (
            <div className="source-console-notice">
              <CircleAlert size={13} />
              <span>{notice}</span>
            </div>
          ) : null}

          <div className="source-console-foot">
            <span aria-hidden="true" />
            切换只作用于 {contractCode}；停用后不会静默改用其他来源。
          </div>
        </div>
      ) : null}
    </div>
  );
}
