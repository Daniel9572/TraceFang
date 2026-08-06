import {
  ArrowUp,
  CheckCircle2,
  CircleAlert,
  PlugZap,
  RefreshCw,
  X,
} from "lucide-react";

import {
  formatQuotaPercent,
  formatRefreshFrequency,
  quotaTone,
  sourceAccessLabels,
  sourceHealthLabels,
} from "./sourcePresentation";
import type { SourceDescriptor, SourceId } from "./types";

export interface SourceTestFeedback {
  tone: "success" | "error";
  message: string;
}

interface SourceDrawerProps {
  open: boolean;
  sources: SourceDescriptor[];
  busy: boolean;
  notice: string | null;
  contractCode: string;
  selectedSource: SourceId;
  testingSourceId: SourceId | null;
  testResults: Partial<Record<SourceId, SourceTestFeedback>>;
  onClose: () => void;
  onToggle: (source: SourceDescriptor) => void;
  onPrefer: (source: SourceDescriptor) => void;
  onTest: (source: SourceDescriptor) => void;
}

const capabilityLabels: Record<string, string> = {
  quote: "实时报价",
  candles: "分钟 K 线",
  catalog: "品种目录",
  news: "资讯",
  calendar: "财经日历",
};

function formatQuotaReset(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "北京时间自然日重置";
  return `北京时间 ${date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  })} 重置`;
}

export function SourceDrawer({
  open,
  sources,
  busy,
  notice,
  contractCode,
  selectedSource,
  testingSourceId,
  testResults,
  onClose,
  onToggle,
  onPrefer,
  onTest,
}: SourceDrawerProps) {
  return (
    <>
      <button
        type="button"
        aria-label="关闭行情源管理"
        className={`drawer-scrim ${open ? "is-open" : ""}`}
        onClick={onClose}
      />
      <aside className={`source-drawer ${open ? "is-open" : ""}`} aria-hidden={!open}>
        <div className="drawer-head">
          <div>
            <p className="eyebrow">MARKET FEED</p>
            <h2>行情源管理</h2>
            <p>正在配置 {contractCode} 的实时行情；其他品种保持各自设置。</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} title="关闭">
            <X size={19} />
          </button>
        </div>

        <div className="data-plane-grid">
          <section>
            <strong>实时数据</strong>
            <span>{contractCode} 独立选择来源，驱动报价、当前柱、状态与延迟。</span>
          </section>
          <section>
            <strong>历史数据</strong>
            <span>全局统一读取本地库；缺口在后台优先用免费通道补齐。</span>
          </section>
        </div>

        <div className="drawer-summary">
          <span>{sources.filter((source) => source.enabled).length} 个来源已启用</span>
          <span>{sources.filter((source) => source.connection_active).length} 个连接已激活</span>
        </div>

        <div className="source-list">
          {sources.map((source) => {
            const awaitingConnection = source.manual_connection_required && !source.connection_active;
            const isTesting = testingSourceId === source.source_id;
            const testResult = testResults[source.source_id];
            const healthLabel = awaitingConnection ? "待连接" : sourceHealthLabels[source.health];
            const supportsRealtime = source.capabilities.includes("quote");
            return (
              <section className={`source-card ${selectedSource === source.source_id ? "is-primary" : ""}`} key={source.source_id}>
                <div className="source-card-head">
                  <span className="source-kind">{source.source_id === "jin10_local" ? "LOCAL" : "MCP"}</span>
                  <div className="source-title">
                    <div>
                      <h3>{source.display_name}</h3>
                      {source.access_model !== "unmetered" ? (
                        <span className={`source-access-badge is-${source.access_model}`}>
                          {sourceAccessLabels[source.access_model]}
                        </span>
                      ) : null}
                      {selectedSource === source.source_id ? <span className="primary-badge">{contractCode} 实时源</span> : null}
                      {source.access_model === "unmetered" && source.capabilities.includes("candles") ? (
                        <span className="history-fill-badge">历史补缺优先</span>
                      ) : null}
                    </div>
                    <span className={`health health-${awaitingConnection ? "unknown" : source.health}`}>
                      <span className="health-dot" aria-hidden="true" />
                      {healthLabel}
                    </span>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-checked={source.enabled}
                    aria-label={`${source.enabled ? "停用" : "启用"}${source.display_name}`}
                    className={`switch ${source.enabled ? "is-on" : ""}`}
                    onClick={() => onToggle(source)}
                    disabled={busy}
                  >
                    <span />
                  </button>
                </div>
                <p className="source-description">{source.description}</p>
                <div className="capability-list">
                  {source.capabilities.map((capability) => (
                    <span key={capability}>{capabilityLabels[capability] ?? capability}</span>
                  ))}
                </div>
                <dl className="source-facts">
                  <div>
                    <dt>优先级</dt>
                    <dd>{source.priority}</dd>
                  </div>
                  <div>
                    <dt>行情更新</dt>
                    <dd>{formatRefreshFrequency(source.quote_poll_interval_seconds, source.quote_streaming)}</dd>
                  </div>
                  <div>
                    <dt>采集特性</dt>
                    <dd>{source.structured ? "结构化数据" : "非结构化（禁用）"}</dd>
                  </div>
                  <div>
                    <dt>运行要求</dt>
                    <dd>{source.requires_running_app ? "需要本地软件运行" : "无需桌面软件"}</dd>
                  </div>
                </dl>
                {source.quotas.length > 0 ? (
                  <div className="source-quota-panel">
                    <div className="source-quota-head">
                      <strong>调用额度</strong>
                      <span>{formatQuotaReset(source.quotas[0].resets_at)}</span>
                    </div>
                    <div className="source-quota-list">
                      {source.quotas.map((quota) => {
                        const tone = quotaTone(quota);
                        return (
                          <div className={`source-quota-row is-${tone}`} key={quota.key}>
                            <div>
                              <span>{quota.label}</span>
                              <span>{quota.used.toLocaleString("zh-CN")} / {quota.limit.toLocaleString("zh-CN")}</span>
                              <strong>{formatQuotaPercent(quota.usage_percent)}</strong>
                            </div>
                            <span className="source-quota-track" aria-label={`${quota.label}额度已使用 ${formatQuotaPercent(quota.usage_percent)}`}>
                              <span
                                className={quota.used > 0 ? "has-usage" : ""}
                                style={{ width: `${Math.min(100, quota.usage_percent)}%` }}
                              />
                            </span>
                          </div>
                        );
                      })}
                    </div>
                    <p>{source.access_note}</p>
                  </div>
                ) : null}
                {source.error ? <p className="source-error">{source.error}</p> : null}
                <div className="source-actions">
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onPrefer(source)}
                    disabled={busy || !source.enabled || awaitingConnection || !supportsRealtime || selectedSource === source.source_id}
                    title={!supportsRealtime ? "该来源不提供实时报价" : undefined}
                  >
                    <ArrowUp size={14} />
                    设为 {contractCode} 实时源
                  </button>
                  <button
                    type="button"
                    className="secondary-button test-connection-button"
                    onClick={() => onTest(source)}
                    disabled={busy || testingSourceId !== null || !source.enabled}
                  >
                    {isTesting ? <RefreshCw size={14} className="spin" /> : <PlugZap size={14} />}
                    {isTesting
                      ? "连接中…"
                      : awaitingConnection
                        ? "连接并测试"
                        : "测试连接"}
                  </button>
                </div>
                {testResult ? (
                  <div className={`connection-result is-${testResult.tone}`}>
                    {testResult.tone === "success" ? <CheckCircle2 size={14} /> : <CircleAlert size={14} />}
                    <span>{testResult.message}</span>
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>
        {notice ? <div className="test-toast">{notice}</div> : null}
        <div className="drawer-note">
          实时层不会静默换源：来源断开时只暂停该合约。历史层与实时层独立，页面只读 PostgreSQL 中的全局历史；后台补缺保留每根 K 线的真实来源，不用截图或推测数据。
        </div>
      </aside>
    </>
  );
}
