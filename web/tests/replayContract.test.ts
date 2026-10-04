import assert from "node:assert/strict";
import test from "node:test";
import { decodeReplayFrameBounds, decodeReplayFrameCursor } from "../src/replayContract.ts";
import { replayStreamQuery } from "../src/expertReplay.ts";

const empty = { state: "empty", first_sequence: null, last_sequence: null, message_count: "0", first_received_at: null, last_received_at: null, source_ids: [], detail: null };

test("empty capture exposes an explicit empty source capability list", () => {
  const bounds = decodeReplayFrameBounds(empty);
  assert.equal(bounds.source_ids.includes("jin10_client"), false);
  assert.equal(bounds.state, "empty");
});
test("missing replay sources are rejected before entering component state", () => {
  const { source_ids: _missing, ...actualOldResponse } = empty;
  assert.throws(() => decodeReplayFrameBounds(actualOldResponse), /source_ids/);
  assert.throws(() => decodeReplayFrameBounds({ ...empty, source_ids: null }), /source_ids/);
});
test("ready capture preserves exact u64 and nanosecond positions", () => {
  const actual = { ...empty, state: "ready", first_sequence: "9007199254740993", last_sequence: "18446744073709551615", message_count: "2", first_received_at: "2026-10-03T01:00:00.123456789Z", last_received_at: "2026-10-03T01:00:00.123456789Z", first_logical_at_ns: "1790992800123456789", last_logical_at_ns: "1790992800123456789", source_ids: ["jin10_client"] };
  assert.deepEqual(decodeReplayFrameBounds(actual), actual);
  assert.throws(() => decodeReplayFrameBounds({ ...actual, first_sequence: 9007199254740993 }), /first_sequence/);
});
test("native REST cursor adapts frame names and never clears the exact UI position", () => {
  const wire={stream_sequence:"9007199254740993",position:{sequence:"9007199254740993",epoch:"capture-fixed",digest:"fixed"},frame_received_at:"2026-10-03T02:00:00.123456789Z",received_at_ns:"1790992800123456789",logical_at_ns:"1790992800123456790",frame_channel:"jin10_history",connection_id:"fixed-protocol",provider_sequence:"18446744073709551615"};
  const value=decodeReplayFrameCursor(wire);assert.equal(value.sequence,wire.stream_sequence);assert.equal(value.received_at,wire.frame_received_at);assert.equal(value.logical_at_ns,wire.logical_at_ns);assert.equal(value.channel,"jin10_history");assert.equal(value.provider_sequence,wire.provider_sequence);
  assert.throws(()=>decodeReplayFrameCursor({...wire,position:{sequence:"1"}}),/position/);
});
test("paused seek rebuild explicitly preserves pause in its WebSocket request",()=>{
  assert.equal(new URLSearchParams(replayStreamQuery({period:"5m",startSequence:"1",endSequence:"4",paused:true})).get("paused"),"true");
});
