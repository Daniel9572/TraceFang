import type {
  QuoteServiceTier,
  SourceAccessModel,
  SourceDescriptor,
  SourceQuota,
} from "./types";

export const sourceHealthLabels: Record<SourceDescriptor["health"], string> = {
  healthy: "可用",
  degraded: "降级",
  unavailable: "暂不可用",
  unconfigured: "待配置",
  unknown: "待检测",
};

export const sourceAccessLabels: Record<SourceAccessModel, string> = {
  unmetered: "不限额",
  limited: "限额",
  metered: "计费",
};

export const quoteServiceLabels: Record<QuoteServiceTier, string> = {
  institutional: "机构专业级",
  enhanced: "优质分析级",
  standard: "标准分析级",
  reference: "低频参考级",
};

export const quoteServiceNotes: Record<QuoteServiceTier, string> = {
  institutional: "适合专业交易与低延迟执行",
  enhanced: "适合常规分析",
  standard: "适合一般实时观察",
  reference: "适合快照与校验",
};

function formatNumber(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, "");
}

export function dataLatencyMilliseconds(
  observedAt: string | null | undefined,
  receivedAt: string | null | undefined,
): number | null {
  if (!observedAt || !receivedAt) return null;
  const observed = Date.parse(observedAt);
  const received = Date.parse(receivedAt);
  const latency = received - observed;
  if (!Number.isFinite(latency) || latency < 0) return null;
  return Math.round(latency);
}

export function formatDataLatency(milliseconds: number | null | undefined): string {
  if (milliseconds === null
    || milliseconds === undefined
    || !Number.isFinite(milliseconds)
    || milliseconds < 0) {
    return "待采样";
  }
  if (milliseconds < 1) return "<1ms";
  if (milliseconds < 1_000) return `${Math.round(milliseconds)}ms`;
  if (milliseconds < 60_000) return `${formatNumber(milliseconds / 1_000)}s`;
  return `${formatNumber(milliseconds / 60_000)}min`;
}

export function formatSourceLatency(
  observedAt: string | null | undefined,
  receivedAt: string | null | undefined,
): string {
  const milliseconds = dataLatencyMilliseconds(observedAt, receivedAt);
  if (milliseconds === null) return "待采样";

  const hasSubsecondPrecision = Boolean(observedAt && /T\d{2}:\d{2}:\d{2}\.\d+/.test(observedAt));
  if (hasSubsecondPrecision) return formatDataLatency(milliseconds);
  if (milliseconds < 1_000) return "<1s";
  return `约${Math.max(1, Math.round(milliseconds / 1_000))}s`;
}

export function formatSamplingInterval(
  seconds: number | null | undefined,
  streaming = false,
): string {
  if (streaming) return "实时推送";
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return "待检测";
  }
  if (seconds < 1) return `${Math.round(seconds * 1_000)}ms`;
  if (seconds >= 3_600 && seconds % 3_600 === 0) return `${formatNumber(seconds / 3_600)}h`;
  if (seconds >= 60 && seconds % 60 === 0) return `${formatNumber(seconds / 60)}min`;
  return `${formatNumber(seconds)}s`;
}

export function formatRefreshFrequency(
  seconds: number | null | undefined,
  streaming = false,
): string {
  if (streaming) return "事件推送";
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) {
    return "待检测";
  }

  if (seconds >= 3600 && seconds % 3600 === 0) {
    return `${formatNumber(seconds / 3600)} 小时/次`;
  }
  if (seconds >= 60 && seconds % 60 === 0) {
    return `${formatNumber(seconds / 60)} 分钟/次`;
  }
  return `${formatNumber(seconds)} 秒/次`;
}

export function formatQuotaPercent(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0%";
  if (value < 0.1) return "<0.1%";
  if (value < 10) return `${value.toFixed(1).replace(/\.0$/, "")}%`;
  return `${Math.round(value)}%`;
}

export function quotaTone(quota: SourceQuota): "normal" | "warning" | "exhausted" {
  if (quota.available <= 0 || quota.usage_percent >= 100) return "exhausted";
  if (quota.usage_percent >= quota.warning_percent) return "warning";
  return "normal";
}
