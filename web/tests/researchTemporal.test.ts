import test from "node:test";
import assert from "node:assert/strict";
import {isResearchLabelOnly,mergeResearchDisplayRows,researchSourceText} from "../src/researchTemporal.ts";
import type { Candle } from "../src/types.ts";
const row=(open_time:string,close:string,volume:string|null=null)=>({open_time,open:close,high:close,low:close,close,volume,open_interest:null} as Candle);
test("unknown spans keep viewing/history while never manufacturing authority",()=>{
 assert.equal(isResearchLabelOnly({authority_snapshot_id:null,authority_unavailable_reason:"source span unknown"}),true);
 assert.equal(isResearchLabelOnly({authority_snapshot_id:"verified",authority_unavailable_reason:null}),false);
 assert.equal(isResearchLabelOnly(null),false);
 const exact="9007199254740993.0000000000000000000000000001";
 const merged=mergeResearchDisplayRows([row("2030-09-30T15:00:00Z",exact,"0")],[row("2030-09-30T14:45:00Z",exact),row("2030-09-30T15:00:00Z",exact,"0")]);
 assert.equal(merged.length,2);assert.equal(merged[1].open_time,"2030-09-30T15:00:00Z");assert.equal(merged[1].close,exact);assert.equal(merged[1].volume,"0");
 assert.throws(()=>mergeResearchDisplayRows(merged,[row("2030-09-30T15:00:00Z",exact,null)]),/修订/);
 assert.throws(()=>mergeResearchDisplayRows(merged,[row("2030-09-30T15:00:00Z","1","0")]),/修订/);
});
test("source labels preserve wide values, tiny fractions and known zero",()=>{
 for(const value of ["9007199254740993.0000000000000000000000000001","0.0000000000000000000000000001","-46.1500","0","18446744073709551615"]) assert.equal(researchSourceText(value,2),value);
 for(const value of [null,undefined,"None","NaN",Infinity]) assert.equal(researchSourceText(value),"—");
});
