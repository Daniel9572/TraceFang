import type {
  InstrumentEntry,
  QuoteStreamEvent,
  QuoteView,
  SourceDescriptor,
  SourceId,
} from "./types";

export interface WatchlistQuoteStreamTarget {
  code: string;
  sourceId: SourceId;
}

export function watchlistQuoteStreamTargets(
  instruments: InstrumentEntry[],
  selectedCode: string,
  instrumentSources: Record<string, SourceId>,
  sourceById: ReadonlyMap<SourceId, SourceDescriptor>,
): WatchlistQuoteStreamTarget[] {
  const targets: WatchlistQuoteStreamTarget[] = [];
  const seenCodes = new Set<string>();
  for (const instrument of instruments) {
    const code = instrument.provider_code;
    if (code === selectedCode || seenCodes.has(code)) continue;

    const sourceId = instrumentSources[code] ?? "jin10_client";
    const source = sourceById.get(sourceId);
    if (
      !source?.selectable
      || (source.manual_connection_required && !source.connection_active)
    ) {
      continue;
    }
    seenCodes.add(code);
    targets.push({ code, sourceId });
  }
  return targets;
}

export interface WatchlistQuoteStreamOptions {
  code: string;
  openStream: (code: string) => WebSocket;
  onQuote: (code: string, quote: QuoteView) => void;
}

export function startWatchlistQuoteStream({
  code,
  openStream,
  onQuote,
}: WatchlistQuoteStreamOptions): () => void {
  let disposed = false;
  let socket: WebSocket | null = null;
  let retryTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
  let retryCount = 0;

  const scheduleReconnect = () => {
    if (disposed) return;
    const delay = Math.min(1_000, 100 * 2 ** retryCount);
    retryCount += 1;
    retryTimer = globalThis.setTimeout(connect, delay);
  };

  const connect = () => {
    if (disposed) return;
    let nextSocket: WebSocket;
    try {
      nextSocket = openStream(code);
    } catch {
      scheduleReconnect();
      return;
    }
    socket = nextSocket;
    nextSocket.onopen = () => {
      retryCount = 0;
    };
    nextSocket.onmessage = (message) => {
      if (disposed || socket !== nextSocket) return;
      let event: QuoteStreamEvent;
      try {
        event = JSON.parse(String(message.data)) as QuoteStreamEvent;
      } catch {
        return;
      }
      if (event.kind === "gap") {
        nextSocket.close();
      } else if (event.kind === "quote" && event.quote) {
        onQuote(code, event.quote);
      }
    };
    nextSocket.onerror = () => nextSocket.close();
    nextSocket.onclose = () => {
      if (disposed || socket !== nextSocket) return;
      scheduleReconnect();
    };
  };

  connect();
  return () => {
    disposed = true;
    if (retryTimer !== null) globalThis.clearTimeout(retryTimer);
    socket?.close();
  };
}
