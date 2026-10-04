import assert from "node:assert/strict";
import test from "node:test";

import {
  quoteStreamCloseDecision,
  quoteStreamReconnectDelay,
  sourceUnavailableMessage,
} from "../src/quoteStreamConnection.ts";

test("backs off local reconnects without retrying every second forever", () => {
  assert.deepEqual(
    [0, 1, 2, 3, 4, 5, 20].map(quoteStreamReconnectDelay),
    [500, 1_000, 2_000, 4_000, 8_000, 15_000, 15_000],
  );
});

test("labels an abnormal socket close as a local service failure", () => {
  assert.deepEqual(quoteStreamCloseDecision({ code: 1006 }, 2), {
    retry: true,
    delayMs: 2_000,
    message: "TraceFang 本机实时服务暂不可达，正在重新连接",
  });
});

test("does not loop on a policy-rejected subscription", () => {
  assert.deepEqual(
    quoteStreamCloseDecision({ code: 1008, reason: "chart period is not supported" }, 0),
    {
      retry: false,
      delayMs: null,
      message: "实时订阅请求被本机服务拒绝：chart period is not supported",
    },
  );
});

test("keeps upstream source recovery distinct from the local transport", () => {
  assert.equal(
    sourceUnavailableMessage("金十统一行情", "服务器已关闭连接\n正在重连"),
    "金十统一行情上游行情暂不可用，后台正在恢复：服务器已关闭连接 正在重连",
  );
});
