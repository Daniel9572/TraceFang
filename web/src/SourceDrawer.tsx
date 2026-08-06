import {
  ArrowUp,
  CheckCircle2,
  CircleAlert,
  Database,
  MonitorUp,
  PlugZap,
  RefreshCw,
  X,
} from "lucide-react";

import type { SourceDescriptor, SourceId } from "./types";

type ConcreteSourceId = Exclude<SourceId, "auto">;

export interface SourceTestFeedback {
  tone: "success" | "error";
  message: string;
}

interface SourceDrawerProps {
  open: boolean;
  sources: SourceDescriptor[];
  busy: boolean;
  notice: string | null;
  testingSourceId: ConcreteSourceId | null;
  testResults: Partial<Record<ConcreteSourceId, SourceTestFeedback>>;
  onClose: () => void;
  onRefresh: () => void;
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

const healthLabels: Record<SourceDescriptor["health"], string> = {
  healthy: "可用",
  degraded: "降级",
  unavailable: "暂不可用",
  unconfigured: "待配置",
  unknown: "待检测",
};

export function SourceDrawer({
  open,
  sources,
  busy,
  notice,
  testingSourceId,
  testResults,
  onClose,
  onRefresh,
  onToggle,
  onPrefer,
  onTest,
}: SourceDrawerProps) {
  return (
    <>
      <button
        type="button"
        aria-label="关闭数据源管理"
        className={`drawer-scrim ${open ? "is-open" : ""}`}
        onClick={onClose}
      />
      <aside className={`source-drawer ${open ? "is-open" : ""}`} aria-hidden={!open}>
        <div className="drawer-head">
          <div>
            <p className="eyebrow">DATA ROUTING</p>
            <h2>数据来源管理</h2>
            <p>按能力、优先级和健康状态选择，不把分析逻辑绑定到具体接口。</p>
          </div>
          <button type="button" className="icon-button" onClick={onClose} title="关闭">
            <X size={19} />
          </button>
        </div>

        <div className="drawer-summary">
          <span>{sources.filter((source) => source.enabled).length} 个来源已启用</span>
          <button type="button" className="text-button" onClick={onRefresh} disabled={busy}>
            <RefreshCw size={14} className={busy ? "spin" : ""} />
            重新检测
          </button>
        </div>

        <div className="source-list">
          {sources.map((source, index) => {
            const SourceIcon = source.requires_running_app ? MonitorUp : Database;
            const healthy = source.health === "healthy";
            const isTesting = testingSourceId === source.source_id;
            const testResult = testResults[source.source_id];
            return (
              <section className={`source-card ${index === 0 && source.enabled ? "is-primary" : ""}`} key={source.source_id}>
                <div className="source-card-head">
                  <div className="source-icon">
                    <SourceIcon size={20} />
                  </div>
                  <div className="source-title">
                    <div>
                      <h3>{source.display_name}</h3>
                      {index === 0 && source.enabled ? <span className="primary-badge">默认报价</span> : null}
                    </div>
                    <span className={`health health-${source.health}`}>
                      {healthy ? <CheckCircle2 size={13} /> : <CircleAlert size={13} />}
                      {healthLabels[source.health]}
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
                    <dt>采集特性</dt>
                    <dd>{source.delayed ? "略有延迟" : "结构化实时"}</dd>
                  </div>
                  <div>
                    <dt>运行要求</dt>
                    <dd>{source.requires_running_app ? "软件运行且窗口未最小化" : "无需桌面软件"}</dd>
                  </div>
                </dl>
                {source.error ? <p className="source-error">{source.error}</p> : null}
                <div className="source-actions">
                  <button
                    type="button"
                    className="secondary-button"
                    onClick={() => onPrefer(source)}
                    disabled={busy || !source.enabled || index === 0}
                  >
                    <ArrowUp size={14} />
                    设为默认
                  </button>
                  <button
                    type="button"
                    className="secondary-button test-connection-button"
                    onClick={() => onTest(source)}
                    disabled={busy || testingSourceId !== null || !source.enabled}
                  >
                    {isTesting ? <RefreshCw size={14} className="spin" /> : <PlugZap size={14} />}
                    {isTesting ? "测试中…" : "测试连接"}
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
          默认按“本地软件 → 官方 MCP”回退。只有本地不可用、手动强制官方源或主动测试官方连接时，才会使用官方报价额度；K 线仍由官方 MCP 提供。
        </div>
      </aside>
    </>
  );
}
