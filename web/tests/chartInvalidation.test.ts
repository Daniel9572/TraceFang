import test from 'node:test';import assert from 'node:assert/strict';
import {loadedInvalidationRange} from '../src/chartInvalidation.ts';import {parseReplayNanoseconds} from '../src/expertReplay.ts';import type {Candle} from '../src/types.ts';
test('changed minute expands only to loaded exact buckets, including calendar ends',()=>{
 const row=(open:string,end:string):Candle=>({open_time:open,interval:3600,open:'1',high:'2',low:'0',close:'1',volume:null,state:'final',revision:'1',finalized_at:end,instrument:{symbol:'TEST',asset_class:'future',base:null,quote:null,venue:null},source:{provider:'jin10_client',provider_symbol:'TEST',observed_at:open,received_at:end,raw_payload:{bucket_end:end}}});
 const rows=[row('2026-09-28T00:00:00Z','2026-09-28T01:00:00Z'),row('2026-09-28T01:00:00Z','2026-09-28T02:00:00Z')];
 const ns=(s:string)=>parseReplayNanoseconds(s)!.toString();
 assert.deepEqual(loadedInvalidationRange(rows,ns('2026-09-28T00:30:00.000000001Z'),ns('2026-09-28T00:31:00Z')),{start:'2026-09-28T00:00:00.000000000Z',end:'2026-09-28T01:00:00.000000000Z'});
 assert.equal(loadedInvalidationRange(rows,ns('2026-09-29T00:30:00Z'),ns('2026-09-29T00:31:00Z')),null);
 assert.throws(()=>loadedInvalidationRange(rows,'invalid','1'));
});
