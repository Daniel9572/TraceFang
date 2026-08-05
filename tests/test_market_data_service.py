import unittest
from datetime import UTC, datetime
from decimal import Decimal

from market_analysis.application.services import MarketDataService
from market_analysis.domain.errors import ProviderUnavailableError
from market_analysis.domain.models import QuoteSnapshot, SourceMetadata
from market_analysis.infrastructure.providers.jin10.symbols import SPOT_GOLD


class FailingProvider:
    name = "primary"

    async def get_quote(self, _instrument):
        raise ProviderUnavailableError("offline")

    async def get_candles(self, _instrument, *, start=None, count=100):
        raise ProviderUnavailableError("offline")


class WorkingProvider:
    name = "fallback"

    async def get_quote(self, instrument):
        now = datetime.now(UTC)
        return QuoteSnapshot(
            instrument=instrument,
            last=Decimal("4201"),
            open=Decimal("4200"),
            high=Decimal("4202"),
            low=Decimal("4199"),
            volume=None,
            change=Decimal("1"),
            change_percent=Decimal("0.02"),
            source=SourceMetadata(
                provider=self.name,
                provider_symbol="GOLD",
                observed_at=now,
                received_at=now,
            ),
        )

    async def get_candles(self, _instrument, *, start=None, count=100):
        return ()


class MarketDataServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_next_provider_after_expected_provider_failure(self) -> None:
        service = MarketDataService(
            quote_providers=[FailingProvider(), WorkingProvider()],
            candle_providers=[FailingProvider(), WorkingProvider()],
        )
        quote = await service.get_quote(SPOT_GOLD)
        self.assertEqual(quote.source.provider, "fallback")


if __name__ == "__main__":
    unittest.main()
