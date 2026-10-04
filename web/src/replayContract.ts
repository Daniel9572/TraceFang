import { exactU64 } from "./quantFormat.ts";
import type { ReplayFrameBounds, ReplayFrameCursor } from "./types.ts";

/** A malformed capability response must never enter the chart's replay state. */
export function decodeReplayFrameBounds(payload: unknown): ReplayFrameBounds {
  const fail = (field: string): never => { throw new Error(`回放范围接口缺少或返回无效的 ${field}；实时图表仍可使用`); };
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return fail("bounds");
  const value = payload as Record<string, unknown>;
  if (!["ready", "empty", "unavailable"].includes(String(value.state))) return fail("state");
  if (!Array.isArray(value.source_ids) || value.source_ids.some(id => typeof id !== "string" || !id.trim())) return fail("source_ids");
  for (const field of ["first_sequence", "last_sequence"] as const) {
    if (value[field] !== null && exactU64(value[field] as string | number) === null) return fail(field);
  }
  const count = exactU64(value.message_count as string | number);
  if (count === null) return fail("message_count");
  for (const field of ["first_received_at", "last_received_at", "detail"] as const) {
    if (value[field] !== null && typeof value[field] !== "string") return fail(field);
  }
  for (const field of ["first_logical_at_ns", "last_logical_at_ns"] as const) {
    if (value[field] != null && (typeof value[field] !== "string" || !/^-?\d+$/.test(value[field] as string))) return fail(field);
  }
  if (value.state === "ready") {
    const first = exactU64(value.first_sequence as string | number), last = exactU64(value.last_sequence as string | number);
    if (count === 0n || first === null || last === null || first > last || !value.first_received_at || !value.last_received_at) return fail("ready range");
  }
  if (value.state === "empty" && (count !== 0n || value.first_sequence !== null || value.last_sequence !== null || value.source_ids.length !== 0)) return fail("empty range");
  return value as unknown as ReplayFrameBounds;
}

/** Native cursor names describe the captured frame, not a chart bar. */
export function decodeReplayFrameCursor(payload: unknown): ReplayFrameCursor {
  const fail = (field: string): never => { throw new Error(`回放消息接口返回无效的 ${field}`); };
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return fail("cursor");
  const value = payload as Record<string,unknown>;
  const position = value.position as Record<string,unknown> | undefined;
  const sequence = exactU64(value.stream_sequence as string | number);
  if (sequence === null || !position || exactU64(position.sequence as string | number) !== sequence) return fail("position/stream_sequence");
  if (typeof value.frame_received_at !== "string" || !value.frame_received_at || typeof value.received_at_ns !== "string" || !/^-?\d+$/.test(value.received_at_ns)) return fail("frame_received_at/received_at_ns");
  if (typeof value.logical_at_ns !== "string" || !/^-?\d+$/.test(value.logical_at_ns)) return fail("logical_at_ns");
  if (typeof value.frame_channel !== "string" || typeof value.connection_id !== "string" || exactU64(value.provider_sequence as string | number) === null) return fail("frame identity");
  return {sequence:sequence.toString(),received_at:value.frame_received_at,received_at_ns:value.received_at_ns,logical_at_ns:value.logical_at_ns,channel:value.frame_channel,connection_id:value.connection_id,provider_sequence:String(value.provider_sequence)};
}
