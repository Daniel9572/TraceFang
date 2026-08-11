import { ExternalLink, ShieldCheck, X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

import type {
  ExpertStrategyDefinition,
  ExpertStrategyDetails,
} from "./expertTypes";

interface StrategyDetailDrawerProps {
  strategy: ExpertStrategyDefinition | null;
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

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

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

export function StrategyDetailDrawer({ strategy, onClose }: StrategyDetailDrawerProps) {
  const titleId = useId();
  const descriptionId = useId();
  const drawerRef = useRef<HTMLElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!strategy) return;
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus());

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        drawerRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR) ?? [],
      ).filter((element) => element.offsetParent !== null);
      if (focusable.length === 0) {
        event.preventDefault();
        closeButtonRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", handleKeyDown);
      previousFocusRef.current?.focus();
      previousFocusRef.current = null;
    };
  }, [strategy?.id]);

  if (!strategy) return null;
  const { details } = strategy;

  return (
    <div
      className="strategy-detail-backdrop"
      onPointerDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <aside
        ref={drawerRef}
        className="strategy-detail-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        data-evidence-mode={strategy.evidenceMode}
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
            aria-label={`关闭${strategy.name}策略详情`}
          >
            <X size={17} />
          </button>
          <strong className="strategy-detail-short-name" aria-hidden="true">{strategy.shortName}</strong>
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

        <div className="strategy-detail-scroll">
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
      </aside>
    </div>
  );
}
