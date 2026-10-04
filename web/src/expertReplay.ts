import { exactU64 } from "./quantFormat.ts";
import type { Candle, ExactSequence, ReplayFrameBounds } from "./types";

const REPLAY_TIME_ZONE = "Asia/Shanghai";
export const REPLAY_RATE_LABEL = "ReplayOriginal · 1× 原速";
export const REPLAY_DERIVED_DOMAIN_NOTICE = "回放未提供该历史域；当前实时派生数据已隔离。";

export type ReplayProjectionState = "live" | "stopped" | "playing" | "paused" | "seeking" | "completed";
export function replayProjectionStateLabel(state: ReplayProjectionState): string {
  return {live:"实时",stopped:"回放已停止",playing:"回放中",paused:"回放已暂停",seeking:"正在定位回放",completed:"回放完成"}[state];
}

export interface ReplayStreamOptions {
  period: string;
  startSequence: ExactSequence;
  endSequence: ExactSequence;
  receivedAtNs?: string;
  sourceId?: string;
  paused?: boolean;
}

export interface ReplayProjectionStart extends ReplayStreamOptions {
  candles: Candle[];
  price: null;
}
const REPLAY_TIME_FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: REPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  fractionalSecondDigits: 3,
  hourCycle: "h23",
});

export function formatReplayTimecode(value: string | null): string {
  if (value === null) return "等待精确帧时间";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "等待精确帧时间";
  const parts = Object.fromEntries(
    REPLAY_TIME_FORMATTER.formatToParts(date).map((part) => [part.type, part.value]),
  );
  const fraction = /\.(\d{1,9})(?:Z|[+-]\d\d:\d\d)$/.exec(value)?.[1] ?? "000";
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}.${fraction.padEnd(3,"0")} · ${REPLAY_TIME_ZONE} · UTC+08:00`;
}

/**
 * A replay projector must always start empty at the first retained raw frame.
 * Current chart Bars are deliberately not accepted as input: even finalized
 * Bars may contain provider evidence that arrived after the replay boundary.
 */
export function createReplayProjectionStart(
  bounds: ReplayFrameBounds,
  period: string,
): ReplayProjectionStart | null {
  if (
    bounds.state !== "ready"
    || bounds.first_sequence === null
    || bounds.last_sequence === null
  ) return null;
  return {
    period,
    startSequence: bounds.first_sequence,
    endSequence: bounds.last_sequence,
    candles: [],
    price: null,
  };
}

export function replayStreamQuery(options: ReplayStreamOptions): string {
  if(exactU64(options.startSequence)===null||exactU64(options.endSequence)===null)throw new Error("回放消息位置无效");
  const query=new URLSearchParams({period:options.period,start_sequence:String(options.startSequence),end_sequence:String(options.endSequence)});
  if(options.receivedAtNs!==undefined)query.set("received_at_ns",options.receivedAtNs);
  if(options.sourceId)query.set("source_id",options.sourceId);
  if(options.paused)query.set("paused","true");
  return query.toString();
}

/** Prevents a current live-only snapshot from crossing into replay decisions or UI. */
export function replaySafeLiveDerivedValue<T>(
  replayState: ReplayProjectionState,
  value: T | null,
): T | null {
  return replayState === "live" ? value : null;
}

/** RFC3339 fractions are kept separately: Date supplies only the whole second. */
export function parseReplayNanoseconds(value:string):bigint|null{
 const match=/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$/.exec(value);
 if(!match)return null;
 const hour=Number(match[1].slice(11,13)),minute=Number(match[1].slice(14,16)),second=Number(match[1].slice(17,19));
 if(hour>23||minute>59||second>59|| (match[3]!=="Z"&&(Number(match[3].slice(1,3))>23||Number(match[3].slice(4,6))>59)))return null;
 const millis=Date.parse(`${match[1]}${match[3]}`);if(!Number.isFinite(millis)||millis%1000!==0)return null;
 const year=Number(match[1].slice(0,4)),month=Number(match[1].slice(5,7)),day=Number(match[1].slice(8,10));
 const days=new Date(Date.UTC(year,month,0)).getUTCDate();if(month<1||month>12||day<1||day>days)return null;
 const ns=BigInt(millis/1000)*1000000000n+BigInt((match[2]??"").padEnd(9,"0")||"0");
 return ns>=-9223372036854775808n&&ns<=9223372036854775807n?ns:null;
}
export function replayNanosecondsIso(value:string|bigint):string|null{
 if(typeof value==='string'&&!/^-?\d+$/.test(value))return null;const ns=BigInt(value);let seconds=ns/1000000000n,remainder=ns%1000000000n;if(remainder<0n){seconds-=1n;remainder+=1000000000n;}
 const date=new Date(Number(seconds)*1000);if(!Number.isFinite(date.getTime()))return null;
 return `${date.toISOString().slice(0,-5)}.${remainder.toString().padStart(9,"0")}Z`;
}
export function replayTimeBounds(bounds:ReplayFrameBounds|null):[bigint,bigint]|null{
 if(bounds?.state!=="ready")return null;
 const first=bounds.first_logical_at_ns??(bounds.first_received_at?parseReplayNanoseconds(bounds.first_received_at)?.toString():null);
 const last=bounds.last_logical_at_ns??(bounds.last_received_at?parseReplayNanoseconds(bounds.last_received_at)?.toString():null);
 if(!first||!last||! /^-?\d+$/.test(first)||! /^-?\d+$/.test(last))return null;const result:[bigint,bigint]=[BigInt(first),BigInt(last)];return result[1]>=result[0]?result:null;
}
export function replayTimeSlider(value:string|null,bounds:[bigint,bigint]|null):number{
 if(!bounds||value===null||! /^-?\d+$/.test(value)||bounds[0]===bounds[1])return 0;
 const ratio=(BigInt(value)-bounds[0])*10000n/(bounds[1]-bounds[0]);return Number(ratio<0n?0n:ratio>10000n?10000n:ratio);
}
export function replaySliderTime(value:number,bounds:[bigint,bigint]):string{
 const bounded=BigInt(Math.max(0,Math.min(10000,Math.round(value))));return(bounds[0]+(bounds[1]-bounds[0])*bounded/10000n).toString();
}
export function clampReplaySequence(value:ExactSequence,first:ExactSequence,last:ExactSequence):string{
 const v=exactU64(value),lo=exactU64(first),hi=exactU64(last);if(v===null||lo===null||hi===null||lo>hi)throw new Error("回放消息位置无效");return(v<lo?lo:v>hi?hi:v).toString();
}
