import assert from "node:assert/strict";
import test from "node:test";

import {
  dataLatencyMilliseconds,
  formatDataLatency,
  formatQuotaPercent,
  formatRefreshFrequency,
  formatSamplingInterval,
  formatSourceLatency,
  quotaTone,
} from "../src/sourcePresentation.ts";
import type { SourceQuota } from "../src/types.ts";

test("formats configured quote refresh frequencies without hiding precision", () => {
  assert.equal(formatRefreshFrequency(1), "1 秒/次");
  assert.equal(formatRefreshFrequency(65), "65 秒/次");
  assert.equal(formatRefreshFrequency(120), "2 分钟/次");
  assert.equal(formatRefreshFrequency(0.5), "0.5 秒/次");
  assert.equal(formatRefreshFrequency(0, true), "事件推送");
});

test("marks missing or invalid refresh frequency as awaiting detection", () => {
  assert.equal(formatRefreshFrequency(undefined), "待检测");
  assert.equal(formatRefreshFrequency(0), "待检测");
});

test("computes ingestion latency from source timestamps", () => {
  assert.equal(
    dataLatencyMilliseconds("2026-08-06T07:40:14Z", "2026-08-06T07:40:14.132Z"),
    132,
  );
  assert.equal(dataLatencyMilliseconds(null, "2026-08-06T07:40:14.132Z"), null);
  assert.equal(
    dataLatencyMilliseconds("2026-08-06T07:40:15Z", "2026-08-06T07:40:14Z"),
    null,
  );
});

test("formats measured latency separately from configured sampling cadence", () => {
  assert.equal(formatDataLatency(0), "<1ms");
  assert.equal(formatDataLatency(132), "132ms");
  assert.equal(formatDataLatency(1_250), "1.25s");
  assert.equal(formatDataLatency(null), "待采样");
  assert.equal(formatSamplingInterval(0.5), "500ms");
  assert.equal(formatSamplingInterval(65), "65s");
  assert.equal(formatSamplingInterval(120), "2min");
  assert.equal(formatSamplingInterval(0, true), "实时推送");
});

test("does not invent millisecond precision for whole-second source timestamps", () => {
  assert.equal(
    formatSourceLatency("2026-08-06T07:40:14Z", "2026-08-06T07:40:14.449Z"),
    "<1s",
  );
  assert.equal(
    formatSourceLatency("2026-08-06T07:40:14.000Z", "2026-08-06T07:40:14.132Z"),
    "132ms",
  );
});

test("formats compact quota percentages", () => {
  assert.equal(formatQuotaPercent(0), "0%");
  assert.equal(formatQuotaPercent(0.04), "<0.1%");
  assert.equal(formatQuotaPercent(4.25), "4.3%");
  assert.equal(formatQuotaPercent(87.8), "88%");
});

test("raises quota tone only at the configured warning boundary", () => {
  const quota: SourceQuota = {
    key: "get_quote",
    label: "报价",
    used: 1200,
    limit: 1500,
    reserve: 25,
    available: 275,
    usage_percent: 80,
    warning_percent: 80,
    period: "daily",
    resets_at: "2026-08-07T00:00:00+08:00",
    scope: "application_process",
  };
  assert.equal(quotaTone({ ...quota, usage_percent: 79.9 }), "normal");
  assert.equal(quotaTone(quota), "warning");
  assert.equal(quotaTone({ ...quota, available: 0 }), "exhausted");
});
