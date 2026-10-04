import { ArrowLeft, ExternalLink, ShieldCheck } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

import type {
  ExpertStrategyDefinition,
  ExpertStrategyDetails,
} from "./expertTypes";

interface StrategyDetailPanelProps {
  strategy: ExpertStrategyDefinition;
  onClose: () => void;
}

const ROLE_LABELS: Record<ExpertStrategyDetails["role"], string> = {
  direction: "方向",
  confirmation: "确认",
  exhaustion: "耗竭",
  rhythm: "节奏",
  structure: "结构",
  "risk-context": "风险背景",
};

const EVIDENCE_LABELS: Record<ExpertStrategyDefinition["evidenceMode"], string> = {
  native: "原生字段",
  proxy: "代理口径",
  conditional: "条件可用",
};

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="strategy-detail-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function DetailList({ values }: { values: readonly string[] }) {
  return (
    <ul>
      {values.map((value, index) => <li key={`${index}:${value}`}>{value}</li>)}
    </ul>
  );
}

export function StrategyDetailPanel({ strategy, onClose }: StrategyDetailPanelProps) {
  const titleId = useId();
  const descriptionId = useId();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());
    scrollRef.current?.scrollTo(0, 0);

    return () => {
      window.cancelAnimationFrame(focusFrame);
      if (previousFocusRef.current?.isConnected) previousFocusRef.current.focus();
      previousFocusRef.current = null;
    };
  }, [strategy.id]);

  const { details } = strategy;

  return (
    <section
      id="expert-strategy-detail"
      className="strategy-detail-panel"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      data-evidence-mode={strategy.evidenceMode}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <header className="strategy-detail-header">
        <div className="strategy-detail-kicker">
          <span>{ROLE_LABELS[details.role]}</span>
          <i />
          <span>{EVIDENCE_LABELS[strategy.evidenceMode]}</span>
          <i />
          <span>{details.version}</span>
        </div>
        <button
          ref={closeButtonRef}
          type="button"
          className="strategy-detail-close"
          onClick={onClose}
          aria-label={`返回策略列表，关闭${strategy.name}详情`}
        >
          <ArrowLeft size={14} />返回
        </button>
        <div className="strategy-detail-title">
          <h2 id={titleId}>{strategy.name}</h2>
          <p id={descriptionId}>{strategy.description}</p>
        </div>
        <div className="strategy-detail-status" aria-label="策略接入状态">
          <span data-ready={details.compositeEligible ? "true" : "false"}>
            合成评分 {details.compositeEligible ? "进入" : "不进入"}
          </span>
          <span data-ready={details.backtestEligible ? "true" : "false"}>
            因果回测 {details.backtestEligible ? "进入" : "不进入"}
          </span>
        </div>
      </header>

      <div ref={scrollRef} className="strategy-detail-scroll">
        <section className="strategy-detail-thesis">
          <span><ShieldCheck size={14} />作用</span>
          <p>{details.principle}</p>
          <dl>
            <div><dt>观察周期</dt><dd>{details.horizon}</dd></div>
            <div><dt>数据来源</dt><dd>{strategy.dataSource}</dd></div>
          </dl>
        </section>

        <div className="strategy-detail-grid">
          <DetailSection title="公式与计算口径"><DetailList values={details.formula} /></DetailSection>
          <DetailSection title="参数"><DetailList values={details.parameters} /></DetailSection>
          <DetailSection title="信号规则"><DetailList values={details.signalRules} /></DetailSection>
          <DetailSection title="必需数据"><DetailList values={details.requiredFields} /></DetailSection>
          <DetailSection title="适用环境"><DetailList values={details.suitableRegimes} /></DetailSection>
          <DetailSection title="边界条件"><DetailList values={details.boundaryConditions} /></DetailSection>
          <DetailSection title="失效条件"><DetailList values={details.invalidation} /></DetailSection>
        </div>

        <DetailSection title="参考依据">
          <div className="strategy-detail-references">
            {details.references.map((reference) => (
              <a
                key={reference.url}
                href={reference.url}
                target="_blank"
                rel="noreferrer"
              >
                <span>
                  <strong>{reference.title}</strong>
                  <small>{reference.publisher}</small>
                </span>
                <ExternalLink size={13} aria-hidden="true" />
                <p>{reference.note}</p>
              </a>
            ))}
          </div>
        </DetailSection>

        <section className="strategy-detail-validation">
          <span>VALIDATION STATUS</span>
          <strong>验证状态</strong>
          <p>{details.validation}</p>
        </section>
      </div>

      <footer className="strategy-detail-footer">
        <span>策略说明不是收益承诺；所有信号均需结合数据时点、成本和风险约束。</span>
        <button type="button" onClick={onClose}>返回策略列表</button>
      </footer>
    </section>
  );
}
