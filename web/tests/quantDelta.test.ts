import test from "node:test";
import assert from "node:assert/strict";
import {mergeQuantSnapshotDelta,QuantDeltaBaseMismatch,type QuantSnapshotDelta} from "../src/quantDelta.ts";
import type {QuantSnapshot} from "../src/quantTypes.ts";
import {quantApi} from "../src/quantApi.ts";
const scope={code:"XAUUSD",source_id:"fixture",period:"1m"};
function sample():QuantSnapshot {
 return {chart_basis_hash:"chart-base",evidence:{schema_version:"v1",calculation_version:"math-v1",rounding_policy:"exact",input_hash:"full-old",snapshot_hash:"snapshot-old",code:scope.code,source_id:scope.source_id,period:scope.period,decision_as_of:"2026-10-04T00:00:00.123456789Z",parameters:{enabled_strategies:["rsi"],rsi_period:14},confirmed_count:1,preview_count:0,warmup_complete:false,semantics:"final_revision_history",token:{store_epoch:"store-a",commit_id:"9007199254740993",schedule_version:"calendar1"}},confirmed:null,bars:[{open_time:"2026-10-03T23:59:00Z",close:"9007199254740993.0000000000000000000000000001"}],series:[{as_of:"2026-10-03T23:59:00Z",decision_at:"2026-10-04T00:00:00Z",indicators:{rsi:"50"}}],name:"exact fixture",unit:"test",executable_contract:false};
}
function delta(value:QuantSnapshot):QuantSnapshotDelta {
 const {bars:_,series:__,chart_basis_hash:___,...snapshot}=value;
 return {base_id:value.chart_basis_hash!,snapshot};
}
for(const scenario of ["no change","quote only","unconfirmed preview metadata","new audit token"]){
 test(`${scenario}: merged delta equals every full field with exact chart arrays`,()=>{
  const base=sample(),full=structuredClone(base);
  if(scenario==="quote only")full.quote={price:"9007199254740993.0000000000000000000000000002",observed_at:"2026-10-04T00:00:00.123456789Z",received_at:"2026-10-04T00:00:00.123456790Z",accepted_at:null,applied_frame_seq:"18446744073709551615"};
  if(scenario==="unconfirmed preview metadata")full.evidence.preview_count=1;
  if(scenario==="new audit token"){full.evidence.token.commit_id="9007199254740994";full.evidence.decision_as_of="2026-10-04T00:00:00.123456790Z";full.evidence.input_hash="full-new";full.evidence.snapshot_hash="snapshot-new";}
  const merged=mergeQuantSnapshotDelta(base,delta(full),full.chart_basis_hash,scope);
  assert.deepEqual(merged,full);assert.strictEqual(merged.bars,base.bars);assert.strictEqual(merged.series,base.series);
 });
}
for(const scenario of ["missing base","lost base id","new confirmation","past correction","calendar change","scope change","parameter change","older commit","nanosecond reordered"]){
 test(`${scenario}: reject delta so caller must request full`,()=>{
  const base=sample(),full=structuredClone(base);let known:QuantSnapshot|null=base;
  if(scenario==="missing base")known=null;
  if(scenario==="lost base id")base.chart_basis_hash=undefined;
  if(["new confirmation","past correction","calendar change"].includes(scenario))full.chart_basis_hash="new-chart-base";
  if(scenario==="scope change")full.evidence.period="1d";
  if(scenario==="parameter change")full.evidence.parameters.rsi_period=10;
  if(scenario==="older commit")full.evidence.token.commit_id="9007199254740992";
  if(scenario==="nanosecond reordered")full.evidence.decision_as_of="2026-10-04T00:00:00.123456788Z";
  assert.throws(()=>mergeQuantSnapshotDelta(known,delta(full),full.chart_basis_hash,scope),QuantDeltaBaseMismatch);
 });
}
test("API requests a full same-job response after rejecting a lost delta base",async()=>{
 const base=sample(),full=structuredClone(base);full.evidence.input_hash="new-full";const wrong=delta(full);wrong.base_id="lost";
 const replies=[{state:"building",job_id:"job1",processed_bars:"0"},{state:"ready",job_id:"job1",chart_basis_hash:"lost",snapshot_delta:wrong},{state:"ready",job_id:"job1",chart_basis_hash:full.chart_basis_hash,snapshot:full}];
 const original=globalThis.fetch,calls:string[]=[];globalThis.fetch=(async(input)=>{calls.push(String(input));return new Response(JSON.stringify(replies.shift()),{status:200,headers:{"Content-Type":"application/json"}});}) as typeof fetch;
 try{assert.deepEqual(await quantApi.snapshot(scope,undefined,undefined,base),full);assert.equal(calls.length,3);assert.ok(calls[1].includes("known_chart_basis_hash=chart-base"));assert.equal(calls[2],"/api/expert/quant/snapshot/jobs/job1");}finally{globalThis.fetch=original;}
});
test("API without a cached base requests a complete compatible snapshot",async()=>{
 const full=sample(),replies=[{state:"building",job_id:"job2",processed_bars:"0"},{state:"ready",job_id:"job2",chart_basis_hash:full.chart_basis_hash,snapshot:full}];
 const original=globalThis.fetch,calls:string[]=[];globalThis.fetch=(async(input)=>{calls.push(String(input));return new Response(JSON.stringify(replies.shift()),{status:200});}) as typeof fetch;
 try{assert.deepEqual(await quantApi.snapshot(scope),full);assert.equal(calls.length,2);assert.ok(!calls[1].includes("known_chart_basis_hash"));}finally{globalThis.fetch=original;}
});
