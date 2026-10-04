import { useEffect, useRef, useState } from "react";
import { marketApi } from "./api";
import { downloadText } from "./researchApi";
import type { SourcePeriodPriceReference, SourcePeriodPriceRow, SourcePeriodReferenceScope } from "./types";

function sourceValue(row: SourcePeriodPriceRow, key: string, name: "open" | "high" | "low" | "close" | "volume" | "turnover"): string {
  if (!row.field_presence[key]) return "未提供";
  return row[name] ?? (row.source_fields[key] === null ? "源NULL" : "源空值");
}

export function SourcePeriodReference({ scope }: { scope: SourcePeriodReferenceScope }) {
  const [packet, setPacket] = useState<SourcePeriodPriceReference | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const readSource = async () => {
    const controller = new AbortController();
    pending.current?.abort(); pending.current = controller;
    setBusy(true); setError(null); setPacket(null);
    try {
      const value = await marketApi.sourcePeriodPrices(scope.code, controller.signal);
      if (controller.signal.aborted) return;
      if (value.code !== scope.code || value.source_id !== scope.mapping.source_id || value.requested_source_period !== "min_5"
        || value.source_instrument.market !== scope.mapping.market || value.source_instrument.code !== scope.mapping.code
        || !Array.isArray(value.rows) || value.rows.length > 100
        || value.rows.some(row => [row.open, row.high, row.low, row.close, row.volume, row.turnover].some(n => n !== null && typeof n !== "string"))) {
        throw new Error("来源参考与当前品种或精确价格格式不一致。");
      }
      setPacket(value);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "来源价格读取失败。");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };
  return <details className="source-period-reference">
    <summary>来源五分钟价格</summary>
    <section className="source-period-reference-panel" aria-label="来源五分钟原值参考">
      <header><strong>{scope.code} · 来源五分钟原值</strong><button type="button" disabled={busy} onClick={() => void readSource()}>{busy ? "读取中…" : packet ? "重新读取" : "读取来源原价"}</button><button type="button" aria-label="关闭来源五分钟价格" onClick={event => event.currentTarget.closest("details")?.removeAttribute("open")}>关闭</button></header>
      <p>同花顺 {scope.mapping.market}:{scope.mapping.code} · min_5 · 原价（actual）。独立参考，不参与当前合成图、指标或模拟。</p>
      <p>源标签仅作UTC机械显示。行情时刻、五分钟起止区间、完成状态及源涨跌未知；数量单位未核实。</p>
      {error ? <p role="alert" className="source-period-reference-error">{error}</p> : null}
      {packet ? <>
        <p>{packet.delivery_mode === "fixed_original_body" ? "固定原文回读 · 原始采集时刻" : "实际采集时刻"}：{packet.source_evidence.received_at}</p>
        {packet.source_response_state === "empty" ? <p role="status">来源未返回价格记录。</p> : <div className="source-period-reference-table"><table>
          <thead><tr><th>源标签（UTC显示）</th><th>开</th><th>高</th><th>低</th><th>收</th><th>原数量</th><th>原成交额</th></tr></thead>
          <tbody>{packet.rows.map(row => <tr key={row.row_index}>
            <td title={`源字段1：${row.source_label} 毫秒；不是行情观察时刻。`}>{row.label_utc_display}<small>{row.source_label}</small></td>
            <td>{sourceValue(row,"7","open")}</td><td>{sourceValue(row,"8","high")}</td><td>{sourceValue(row,"9","low")}</td><td>{sourceValue(row,"11","close")}</td>
            <td>{sourceValue(row,"13","volume")}</td><td>{sourceValue(row,"19","turnover")}</td>
          </tr>)}</tbody>
        </table></div>}
        <details className="source-period-reference-evidence"><summary>请求与原文凭据</summary>
          <p>正文SHA256：{packet.source_evidence.body_sha256}</p>
          <p>归档版本：{packet.reference_id}</p>
          <pre>{JSON.stringify(packet.source_evidence.request, null, 2)}</pre>
          <button type="button" onClick={() => downloadText(`${scope.code}-source-min5-${packet.reference_id.slice(0,12)}.json`, JSON.stringify(packet,null,2), "application/json")}>下载原值与完整原文凭据</button>
        </details>
      </> : null}
    </section>
  </details>;
}
