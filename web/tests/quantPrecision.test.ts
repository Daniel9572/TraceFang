import assert from "node:assert/strict";
import test from "node:test";
import {formatQuantDecimal,multiplyQuantBy100,exactU64,compareSourceRevision} from "../src/quantFormat.ts";
import {clampReplaySequence,parseReplayNanoseconds,replayNanosecondsIso,replaySliderTime,replayTimeSlider,formatReplayTimecode,replayStreamQuery} from "../src/expertReplay.ts";
import {mergeCandleRows} from "../src/api.ts";
import {upsertRealtimeBar} from "../src/chartModel.ts";
import {RealtimeBarStream} from "../src/realtimeBarStream.ts";
import type {Candle} from "../src/types.ts";
const row=(revision:string):Candle=>({instrument:{symbol:"TEST",asset_class:"spot",venue:"fixture"},interval:60,open_time:"2026-10-03T00:00:00Z",open:"100.0000000000000000000000000001",high:"101",low:"99",close:"100",volume:null,source:{provider:"fixture",provider_symbol:"TEST",observed_at:"2026-10-03T00:00:00Z",received_at:"2026-10-03T00:00:00Z"},evidence_channel_id:"fixture",state:"final",finalized_at:"2026-10-03T00:01:00Z",revision});
test("cash presentation preserves wide values and half-even ties without floating money",()=>{
 assert.equal(formatQuantDecimal("79228162514264337593543950335.0000000000000000000000000001",28),"79,228,162,514,264,337,593,543,950,335.0000000000000000000000000001");
 assert.equal(formatQuantDecimal("-2.345",2),"−2.34");assert.equal(formatQuantDecimal("2.355",2),"2.36");assert.equal(formatQuantDecimal("-0.0001",2),"0.00");assert.equal(formatQuantDecimal(null),"—");assert.equal(formatQuantDecimal("1e-9"),"—");assert.equal(multiplyQuantBy100("0.00000000000000000001"),"0.000000000000000001");
});
test("all candle replacement paths compare u64 revisions numerically",()=>{
 for(const [old,newer]of [["9","10"],["9007199254740992","9007199254740993"],["18446744073709551614","18446744073709551615"]]){
   assert.equal(compareSourceRevision(newer,old),1);const before=row(old),after=row(newer);
   assert.equal(upsertRealtimeBar([before],after)[0],after);assert.equal(upsertRealtimeBar([after],before)[0],after);
   assert.equal(mergeCandleRows([before],[after])[0],after);
   const stream=new RealtimeBarStream();assert.equal(stream.publish("fixture",before),true);assert.equal(stream.publish("fixture",after),true);assert.equal(stream.publish("fixture",before),false);
 }
 assert.equal(exactU64(9007199254740992),null);assert.equal(exactU64("18446744073709551616"),null);
});
test("precise ISO seeks retain nine digits, signed ns, offsets and valid calendar boundaries",()=>{
 const exact="2026-10-03T12:00:00.123456789Z";assert.equal(replayNanosecondsIso(parseReplayNanoseconds(exact)!),exact);
 assert.equal(parseReplayNanoseconds("2026-10-03T15:00:00.123456789+03:00"),parseReplayNanoseconds(exact));
 assert.equal(parseReplayNanoseconds("1969-12-31T23:59:59.999999999Z"),-1n);assert.equal(replayNanosecondsIso(-1n),"1969-12-31T23:59:59.999999999Z");
 assert.match(formatReplayTimecode(exact),/20:00:00\.123456789/);
 for(const value of ["2026-02-30T00:00:00Z","2026-10-03T24:00:00Z","2026-10-03T12:00:00.1234567890Z","2026-10-03T12:00:00","3000-01-01T00:00:00Z"])assert.equal(parseReplayNanoseconds(value),null);
});
test("time slider interpolates with integers and exact message positions are separate",()=>{
 const range:[bigint,bigint]=[1791028800123456789n,1791028801123456789n];assert.equal(replaySliderTime(5000,range),"1791028800623456789");assert.equal(replayTimeSlider("1791028800623456789",range),5000);
 assert.equal(clampReplaySequence("9007199254740993","9007199254740992","18446744073709551615"),"9007199254740993");
 const query=replayStreamQuery({period:"1s",startSequence:"9007199254740993",endSequence:"18446744073709551615",receivedAtNs:"1791028800123456789"});assert.match(query,/start_sequence=9007199254740993/);assert.match(query,/received_at_ns=1791028800123456789/);
});
