import assert from "node:assert/strict";
import test from "node:test";

import {
  startWatchlistQuoteStream,
  watchlistQuoteStreamTargets,
} from "../src/watchlistStreams.ts";
import type {
  InstrumentEntry,
  QuoteStreamEvent,
  QuoteView,
  SourceDescriptor,
} from "../src/types.ts";

const instruments: InstrumentEntry[] = [
  {
    provider: "canonical",
    provider_code: "XAUUSD",
    name: "现货黄金",
    instrument: { symbol: "XAU/USD", asset_class: "spot", base: "XAU", quote: "USD", venue: "OTC" },
    price_unit: "美元/盎司",
    price_digits: 2,
    quote_kind: "direct",
    history_available: true,
    source_ids: ["jin10_client"],
    dependencies: [],
  },
  {
    provider: "canonical",
    provider_code: "XAGUSD",
    name: "现货白银",
    instrument: { symbol: "XAG/USD", asset_class: "spot", base: "XAG", quote: "USD", venue: "OTC" },
    price_unit: "美元/盎司",
    price_digits: 3,
    quote_kind: "direct",
    history_available: true,
    source_ids: ["jin10_client"],
    dependencies: [],
  },
];

function source(overrides: Partial<SourceDescriptor> = {}): SourceDescriptor {
  return {
    source_id: "jin10_client",
    display_name: "金十客户端行情",
    description: "test",
    capabilities: ["quote"],
    selectable: true,
    delayed: false,
    requires_running_app: false,
    structured: true,
    quote_poll_interval_seconds: 0,
    quote_streaming: true,
    quote_service_tier: "enhanced",
    access_model: "unmetered",
    access_note: null,
    manual_connection_required: false,
    connection_active: true,
    quotas: [],
    health: "healthy",
    state: "connected",
    error: null,
    checked_at: null,
    last_success_at: null,
    ...overrides,
  };
}

function quoteView(): QuoteView {
  return {
    source_id: "jin10_client",
    quote: {
      instrument: { symbol: "XAG/USD", asset_class: "spot", base: "XAG", quote: "USD", venue: "OTC" },
      last: 63.53,
      open: 62.5,
      high: 64,
      low: 62,
      volume: null,
      change: 1.03,
      change_percent: 1.65,
      source: {
        provider: "jin10_client",
        provider_symbol: "XAGUSD",
        observed_at: "2026-08-08T08:00:00Z",
        received_at: "2026-08-08T08:00:00Z",
      },
    },
    quality: "complete",
    unavailable_fields: [],
    stale_fields: [],
    composed_at: "2026-08-08T08:00:00Z",
  };
}

test("keeps every non-selected instrument with a ready source on the watchlist stream", () => {
  const targets = watchlistQuoteStreamTargets(
    instruments,
    "XAUUSD",
    { XAUUSD: "jin10_client", XAGUSD: "jin10_client" },
    new Map([["jin10_client", source()]]),
  );

  assert.deepEqual(targets, [{ code: "XAGUSD", sourceId: "jin10_client" }]);
});

test("does not subscribe a watchlist row whose source is unavailable", () => {
  const targets = watchlistQuoteStreamTargets(
    instruments,
    "XAUUSD",
    { XAUUSD: "jin10_client", XAGUSD: "jin10_client" },
    new Map([["jin10_client", source({ selectable: false })]]),
  );

  assert.deepEqual(targets, []);
});

class FakeWebSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  closeCalls = 0;

  close() {
    this.closeCalls += 1;
  }
}

test("updates an unselected watchlist row from its quote stream", () => {
  const socket = new FakeWebSocket();
  const received: Array<{ code: string; value: QuoteView }> = [];
  const stop = startWatchlistQuoteStream({
    code: "XAGUSD",
    openStream: () => socket as unknown as WebSocket,
    onQuote: (code, value) => received.push({ code, value }),
  });
  const quote = quoteView();
  const event: QuoteStreamEvent = {
    kind: "quote",
    state: "live",
    emitted_at: "2026-08-08T08:00:00Z",
    quote,
    error: null,
  };

  socket.onmessage?.({ data: JSON.stringify(event) } as MessageEvent);

  assert.deepEqual(received, [{ code: "XAGUSD", value: quote }]);
  stop();
  assert.equal(socket.closeCalls, 1);
});
