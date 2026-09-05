const RECONNECT_BASE_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 15_000;

export interface QuoteStreamCloseLike {
  code: number;
  reason?: string;
}

export interface QuoteStreamCloseDecision {
  retry: boolean;
  delayMs: number | null;
  message: string;
}

function singleLine(value: string | null | undefined): string | null {
  const normalized = value?.replace(/[\r\n]+/g, " ").trim();
  return normalized ? normalized.slice(0, 240) : null;
}

export function quoteStreamReconnectDelay(attempt: number): number {
  const safeAttempt = Number.isFinite(attempt)
    ? Math.max(0, Math.floor(attempt))
    : 0;
  const exponent = Math.min(safeAttempt, 10);
  return Math.min(
    RECONNECT_MAX_DELAY_MS,
    RECONNECT_BASE_DELAY_MS * 2 ** exponent,
  );
}

export function quoteStreamCloseDecision(
  close: QuoteStreamCloseLike,
  attempt: number,
): QuoteStreamCloseDecision {
  const reason = singleLine(close.reason);
  if (close.code === 1008) {
    return {
      retry: false,
      delayMs: null,
      message: reason
        ? `实时订阅请求被本机服务拒绝：${reason}`
        : "实时订阅请求被本机服务拒绝，请检查合约与周期设置",
    };
  }

  const message = close.code === 1012
    ? "TraceFang 本机实时服务正在重启，正在重新连接"
    : close.code === 1006 || close.code === 0
      ? "TraceFang 本机实时服务暂不可达，正在重新连接"
      : "TraceFang 本机实时流连接已中断，正在重新连接";
  return {
    retry: true,
    delayMs: quoteStreamReconnectDelay(attempt),
    message,
  };
}

export function sourceUnavailableMessage(
  sourceName: string,
  error: string | null | undefined,
): string {
  const detail = singleLine(error);
  return detail
    ? `${sourceName}上游行情暂不可用，后台正在恢复：${detail}`
    : `${sourceName}上游行情暂不可用，后台正在恢复`;
}
