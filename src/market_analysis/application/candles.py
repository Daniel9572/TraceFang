from __future__ import annotations

from datetime import UTC, timedelta

from market_analysis.domain.models import Candle, QuoteSnapshot, SourceMetadata


class MinuteCandleBuilder:
    """Builds accurate minute OHLC rows from ordered structured quote frames."""

    def __init__(self) -> None:
        self._current: dict[tuple[str, str], Candle] = {}

    def update(self, quote: QuoteSnapshot) -> Candle | None:
        observed_at = quote.source.observed_at.astimezone(UTC)
        open_time = observed_at.replace(second=0, microsecond=0)
        key = (quote.instrument.symbol, quote.source.provider)
        current = self._current.get(key)
        if current is not None and open_time < current.open_time:
            return None

        if current is None or open_time > current.open_time:
            open_price = quote.last
            high = quote.last
            low = quote.last
        else:
            open_price = current.open
            high = max(current.high, quote.last)
            low = min(current.low, quote.last)

        candle = Candle(
            instrument=quote.instrument,
            interval=timedelta(minutes=1),
            open_time=open_time,
            open=open_price,
            high=high,
            low=low,
            close=quote.last,
            volume=None,
            source=SourceMetadata(
                provider=quote.source.provider,
                provider_symbol=quote.source.provider_symbol,
                observed_at=quote.source.observed_at,
                received_at=quote.source.received_at,
                raw_payload={"derived_from": "structured_quote_stream"},
            ),
        )
        self._current[key] = candle
        return candle
