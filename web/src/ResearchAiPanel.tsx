import { useEffect, useRef, useState } from "react";
import { marketApi } from "./api";
import {
  reasoningEffortLabel,
  resolveAiPreferences,
} from "./expertAiPreferences";
import type { ExpertAiModel, ExpertAiStatus } from "./expertTypes";
import {
  downloadText,
  researchApi,
  type ResearchJob,
  type ResearchPage,
  type ResearchQuery,
} from "./researchApi";

export function ResearchAiPanel({
  query,
  page,snapshot,
}: {
  query: ResearchQuery;
  page: ResearchPage | null;
  snapshot:import("./quantTypes").QuantSnapshot|null;
}) {
  const [status, setStatus] = useState<ExpertAiStatus | null>(null);
  const [models, setModels] = useState<ExpertAiModel[]>([]);
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState("");
  const [question, setQuestion] = useState(
    "分析当前趋势、关键价位与数据局限，分别给出看多、看空和观望的确认条件。",
  );
  const [job, setJob] = useState<ResearchJob | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const alive = useRef(true);
  const activeJob = useRef<string | null>(null);
  const pollFailures = useRef(0);
  useEffect(() => {
    alive.current = true;
    let disposed = false;
    void marketApi
      .expertAiStatus()
      .then(async (value) => {
        if (disposed) return;
        setStatus(value);
        if (value.state !== "ready") return;
        const catalog = await marketApi.expertAiModels();
        if (disposed) return;
        setModels(catalog.models);
        let preference = {};
        try {
          preference = JSON.parse(
            localStorage.getItem("tracefang.ai.preferences") ?? "{}",
          );
        } catch {
          /* Defaults remain usable. */
        }
        const resolved = resolveAiPreferences(catalog.models, preference ?? {});
        setModel(resolved.model);
        setEffort(resolved.reasoning_effort);
        setError(null);
      })
      .catch((failure) => {
        if (!disposed) setError(String(failure.message ?? failure));
      });
    return () => {
      disposed = true;
      alive.current = false;
      if (activeJob.current)
        void researchApi.cancel(activeJob.current).catch(() => {});
    };
  }, [revision]);
  useEffect(() => {
    if (!job || !["loading", "analyzing"].includes(job.state)) {
      activeJob.current = null;
      return;
    }
    activeJob.current = job.id;
    const controller = new AbortController();
    const timer = window.setTimeout(
      () => {
        void researchApi
          .job(job.id, controller.signal)
          .then((next) => {
            pollFailures.current = 0;
            setError(null);
            setJob(next);
          })
          .catch((failure) => {
            if (controller.signal.aborted) return;
            pollFailures.current += 1;
            setError(`读取分析状态失败：${failure.message}`);
            setJob((current) => (current ? { ...current } : current));
          });
      },
      Math.min(15000, 1200 * 2 ** Math.min(pollFailures.current, 4)),
    );
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [job]);
  const busy =
    sending || job?.state === "loading" || job?.state === "analyzing";
  const run = async () => {
    if(!snapshot||!page?.authority_snapshot_id)return;
    setSending(true);
    setError(null);
    setJob(null);
    pollFailures.current = 0;
    try {
      try {
        localStorage.setItem(
          "tracefang.ai.preferences",
          JSON.stringify({ model, reasoning_effort: effort }),
        );
      } catch {
        /* In-memory preferences still work. */
      }
      const next = await researchApi.analyze(query, question, model, effort,{research_snapshot_id:page.authority_snapshot_id,expected_input_hash:snapshot.evidence.input_hash,parameters:snapshot.evidence.parameters});
      if (!alive.current) {
        void researchApi.cancel(next.id).catch(() => {});
        return;
      }
      activeJob.current = next.id;
      setJob(next);
    } catch (failure) {
      if (alive.current)
        setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      if (alive.current) setSending(false);
    }
  };
  return (
    <div className="research-ai">
      <h2>AI 行情研究</h2>
      <p className="muted">{status?.detail ?? "正在检查本机 Codex 连接…"}</p>
      <label>
        分析问题
        <textarea
          maxLength={8000}
          rows={5}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
        />
      </label>
      <div className="research-prompt-chips">
        {["趋势与失效条件", "数据质量与不足", "多空情景对比"].map((prompt) => (
          <button
            key={prompt}
            onClick={() =>
              setQuestion(`请分析${prompt}，只引用本次行情和已计算指标。`)
            }
          >
            {prompt}
          </button>
        ))}
      </div>
      <label>
        模型
        <select
          value={model}
          onChange={(event) => {
            setModel(event.target.value);
            setEffort(
              models.find((item) => item.model === event.target.value)
                ?.default_reasoning_effort ?? "",
            );
          }}
        >
          {!models.length ? (
            <option value="">等待模型目录</option>
          ) : (
            models.map((item) => (
              <option key={item.model} value={item.model}>
                {item.display_name}
              </option>
            ))
          )}
        </select>
      </label>
      <label>
        推理强度
        <select
          value={effort}
          onChange={(event) => setEffort(event.target.value)}
        >
          {(
            models.find((item) => item.model === model)?.reasoning_efforts ?? []
          ).map((item) => (
            <option key={item} value={item}>
              {reasoningEffortLabel(item)}
            </option>
          ))}
        </select>
      </label>
      <p className="research-notice">
        {page?.authority_unavailable_reason ?? `发送 ${query.symbol} · ${query.period} 的固定研究输入与同版本服务端指标。预热范围以来源证据为准。使用本机 Codex 账户额度。`}
      </p>
      <div className="research-inline-actions">
        <button
          className="primary"
          disabled={
            !snapshot || !page?.authority_snapshot_id || busy ||
            status?.state !== "ready" ||
            !model ||
            !page?.items.length ||
            page.cache_state === "stale"
          }
          onClick={() => void run()}
        >
          {busy ? (job?.stage ?? "正在提交…") : "开始分析"}
        </button>
        {busy && job ? (
          <button
            onClick={() =>
              void researchApi
                .cancel(job.id)
                .then(setJob)
                .catch((failure) => setError(failure.message))
            }
          >
            取消分析
          </button>
        ) : (
          <button onClick={() => setRevision((value) => value + 1)}>
            检测连接
          </button>
        )}
      </div>
      {error || job?.error ? (
        <p role="alert" className="research-error">
          {error ?? job?.error}
        </p>
      ) : null}
      {job?.state === "cancelled" ? <p role="status">分析已取消。</p> : null}
      {job?.result?.analysis ? (
        <article className="research-ai-answer">
          <div className="research-inline-actions">
            <strong>研究结论</strong>
            <button
              onClick={() =>
                downloadText(
                  `${query.symbol}-analysis.md`,
                  `# ${query.symbol} 行情分析\n\n来源：${query.source} / ${query.period} / ${query.adjustment}\n\n证据快照：${job.snapshot_id}\n\n数据截止：${job.data_as_of}\n\n${job.result!.analysis}`,
                )
              }
            >
              导出
            </button>
          </div>
          <small>
            {job.data_as_of} · 快照 {job.snapshot_id} {job.input_hash&&snapshot?.evidence.input_hash!==job.input_hash?" · 当前输入已更新，本次结论保留原版本":""}
          </small>
          <div className="ai-prose">{job.result.analysis}</div>
          <details>
            <summary>查看计算证据</summary>
            <pre>{JSON.stringify(job.evidence, null, 2)}</pre>
          </details>
        </article>
      ) : null}
    </div>
  );
}
