import {parseReplayNanoseconds,replayNanosecondsIso} from './expertReplay.ts';
import type {Candle} from './types.ts';
/** Expand a changed minute interval to the exact bucket labels already loaded. */
export function loadedInvalidationRange(rows:readonly Candle[],startNs:string,endNs:string):{start:string;end:string}|null{
 if(!/^-?\d+$/.test(startNs)||!/^-?\d+$/.test(endNs))throw new Error('修订通知缺少精确时间范围');
 const start=BigInt(startNs),end=BigInt(endNs);if(end<=start)throw new Error('修订通知时间范围无效');
 let first:bigint|null=null,last:bigint|null=null;
 for(const row of rows){const open=parseReplayNanoseconds(row.open_time);if(open===null)continue;
  const rawEnd=row.source.raw_payload?.bucket_end;const boundary=typeof rawEnd==='string'?parseReplayNanoseconds(rawEnd):null;
  const seconds=Number(row.interval);const close=boundary??(Number.isSafeInteger(seconds)&&seconds>0?open+BigInt(seconds)*1000000000n:null);
  if(close!==null&&open<end&&close>start){first=first===null?open:first<open?first:open;last=last===null?close:last>close?last:close;}
 }
 if(first===null||last===null)return null;return {start:replayNanosecondsIso(first)!,end:replayNanosecondsIso(last)!};
}
