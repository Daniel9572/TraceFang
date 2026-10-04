import type {QuantSnapshot} from "./quantTypes";

export interface QuantSnapshotDelta {
  base_id:string;
  snapshot:Omit<QuantSnapshot,"bars"|"series"|"chart_basis_hash">;
}
export class QuantDeltaBaseMismatch extends Error {}
function canonical(value:unknown):string {
  if(Array.isArray(value))return `[${value.map(canonical).join(",")}]`;
  if(value!==null&&typeof value==="object")return `{${Object.keys(value).sort().map(key=>`${JSON.stringify(key)}:${canonical((value as Record<string,unknown>)[key])}`).join(",")}}`;
  return JSON.stringify(value)??"undefined";
}
function instant(value:string):bigint {
  const match=/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(value);
  if(!match)throw new QuantDeltaBaseMismatch("快照时刻无法精确比较");
  const seconds=Date.parse(`${match[1]}Z`);
  if(!Number.isSafeInteger(seconds))throw new QuantDeltaBaseMismatch("快照时刻不可用");
  return BigInt(seconds)*1_000_000n+BigInt((match[2]??"").padEnd(9,"0"));
}
function commit(value:unknown):bigint {
  if(typeof value!=="string"||!/^\d{1,20}$/.test(value))throw new QuantDeltaBaseMismatch("快照提交身份缺失");
  return BigInt(value);
}
/** A delta always carries fresh complete audit metadata; only chart arrays are reused. */
export function mergeQuantSnapshotDelta(base:QuantSnapshot|null,delta:QuantSnapshotDelta,basis:string|undefined,scope:{code:string;source_id?:string;period:string}):QuantSnapshot {
  const meta=delta.snapshot;
  if(!base?.chart_basis_hash||base.chart_basis_hash!==delta.base_id||basis!==delta.base_id)throw new QuantDeltaBaseMismatch("图表缓存身份不匹配");
  if(meta.evidence.code!==scope.code||meta.evidence.period!==scope.period||(scope.source_id!==undefined&&meta.evidence.source_id!==scope.source_id)||meta.evidence.code!==base.evidence.code||meta.evidence.period!==base.evidence.period||meta.evidence.source_id!==base.evidence.source_id)throw new QuantDeltaBaseMismatch("图表缓存范围已变化");
  if(canonical(meta.evidence.parameters)!==canonical(base.evidence.parameters)||meta.evidence.token.store_epoch!==base.evidence.token.store_epoch||commit(meta.evidence.token.commit_id)<commit(base.evidence.token.commit_id)||instant(meta.evidence.decision_as_of)<instant(base.evidence.decision_as_of))throw new QuantDeltaBaseMismatch("快照乱序或参数已变化");
  return {...meta,bars:base.bars,series:base.series,chart_basis_hash:basis};
}
