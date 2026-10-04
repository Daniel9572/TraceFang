import {useState} from "react";
import {exactU64} from "./quantFormat";
import {formatReplayTimecode,parseReplayNanoseconds,replayNanosecondsIso,replayTimeBounds,replayTimeSlider,replaySliderTime} from "./expertReplay";
import type {ReplayFrameBounds,ReplayFrameCursor} from "./types";
interface Props{bounds:ReplayFrameBounds|null;cursor:ReplayFrameCursor|null;disabled:boolean;busy:boolean;error:string|null;warning:string|null;chartAt:string|null;onSeekTime:(value:string)=>void;onSeekSequence:(value:string)=>void}
export function ReplayTimeline({bounds,cursor,disabled,busy,error,warning,chartAt,onSeekTime,onSeekSequence}:Props){
 const [drag,setDrag]=useState<number|null>(null),[input,setInput]=useState(""),[sequence,setSequence]=useState(""),[validation,setValidation]=useState<string|null>(null);
 const range=replayTimeBounds(bounds);const logical=cursor?.logical_at_ns??cursor?.received_at_ns??(cursor?parseReplayNanoseconds(cursor.received_at)?.toString()??null:null);
 const commit=(value:number)=>{if(!range)return;setDrag(null);onSeekTime(replaySliderTime(value,range));};
 const label=(at:bigint)=>replayNanosecondsIso(at)?.slice(5,19).replace('T',' ')??'—';
 return <div className="replay-timeline"><div className="replay-timeline-status"><span title="原始消息顺序回放；一个消息可能包含多个报价或历史柱，不能视为完整逐笔">原始消息 / 报价快照</span><strong title={cursor?.received_at??undefined}>{busy?'正在定位…':cursor?formatReplayTimecode(cursor.received_at):bounds?.detail??'等待留存行情'}</strong>{warning?<span title={warning}>⚠</span>:null}</div>
 <input type="range" min={0} max={10000} step={1} aria-label="按时间定位行情回放" value={drag??replayTimeSlider(logical,range)} disabled={disabled||!range} onChange={e=>setDrag(Number(e.target.value))} onPointerUp={e=>commit(Number(e.currentTarget.value))} onPointerCancel={()=>setDrag(null)} onKeyUp={e=>{if(['ArrowLeft','ArrowRight','Home','End','PageUp','PageDown'].includes(e.key))commit(Number(e.currentTarget.value));}}/>
 <div className="replay-time-ticks">{range?[range[0],(range[0]+range[1])/2n,range[1]].map((value,index)=><span key={index} title={replayNanosecondsIso(value)??undefined}>{label(value)}</span>):<span>暂无可定位时间范围</span>}</div>
 <details className="replay-time-details"><summary>精确定位</summary><div><p>时间轴使用不回退的接收逻辑时钟；同一时刻定位到第一条消息，可用消息位置选择下一条。原始接收时间保持原值。</p><form onSubmit={e=>{e.preventDefault();const ns=parseReplayNanoseconds(input);if(ns===null){setValidation('请输入带时区的日期时间，最多9位小数，例如 2026-10-03T12:00:00.123456789Z');return;}setValidation(null);onSeekTime(ns.toString());}}><label>精确时间（含时区）<input value={input} onChange={e=>setInput(e.target.value)} placeholder="2026-10-03T12:00:00.123456789Z"/></label><button disabled={disabled}>定位时间</button></form>
 <form onSubmit={e=>{e.preventDefault();const parsed=exactU64(sequence);if(parsed===null){setValidation('消息位置必须是有效的无符号64位整数');return;}setValidation(null);onSeekSequence(parsed.toString());}}><label>消息位置<input inputMode="numeric" value={sequence} onChange={e=>setSequence(e.target.value)} placeholder={String(cursor?.sequence??'')}/></label><button disabled={disabled}>定位消息</button></form>
 <dl><dt>当前消息位置</dt><dd>{String(cursor?.sequence??'—')}</dd><dt>原始接收时间</dt><dd>{cursor?.received_at??'—'}</dd><dt>时间轴逻辑时间</dt><dd>{logical?replayNanosecondsIso(logical):'—'}</dd><dt>图表最后柱位置</dt><dd>{chartAt??'—'}</dd></dl>{validation||error?<p role="alert">{validation??error}</p>:null}</div></details>
 {error?<span className="replay-timeline-error" title={error}>{error}</span>:null}</div>;
}
