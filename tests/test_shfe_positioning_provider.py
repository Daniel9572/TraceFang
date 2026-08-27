from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import httpx

from tracefang.domain.errors import ProviderDataError
from tracefang.domain.market_context import (
    DirectionalInference,
    PositionCountingMethod,
)
from tracefang.infrastructure.providers.shfe_positioning import (
    ShfePositioningProvider,
)


def row(
    contract: str,
    *,
    product: str = "au",
    volume: str,
    open_interest: str,
    open_interest_change: str | None,
) -> dict[str, str]:
    value = {
        "contractname": contract,
        "instrumentid": product,
        "volume": volume,
        "openinterest": open_interest,
        "lastprice": "948.40",
        "updatetime": "2026-08-11 01:36:03",
    }
    if open_interest_change is not None:
        value["openinterestchg"] = open_interest_change
    return value


class ShfePositioningProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_aggregates_real_contracts_and_preserves_public_data_boundaries(self) -> None:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertTrue(request.url.path.endswith("/delaymarket_au.dat"))
            return httpx.Response(
                200,
                json={
                    "delaymarket": [
                        row(
                            "au2608",
                            volume="18",
                            open_interest="2949.00",
                            open_interest_change="-2",
                        ),
                        row(
                            "au2609",
                            volume="1773",
                            open_interest="3416.00",
                            open_interest_change="5",
                        ),
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = ShfePositioningProvider(
                http_client=client,
                utc_clock=lambda: datetime(2026, 8, 10, 18, 6, tzinfo=UTC),
            )
            first = await provider.get_context("au")
            second = await provider.get_context("AU")

        self.assertIs(first, second)
        self.assertEqual(first.contract_count, 2)
        self.assertEqual(first.volume, 1791)
        self.assertEqual(first.open_interest, 6365)
        self.assertEqual(first.open_interest_change, 3)
        self.assertEqual(first.open_interest_change_contracts, 2)
        self.assertEqual(first.counting_method, PositionCountingMethod.SINGLE_SIDE)
        self.assertEqual(first.directional_inference, DirectionalInference.UNAVAILABLE)
        self.assertEqual(first.source.declared_delay, timedelta(minutes=30))
        self.assertEqual(calls, 1)

    async def test_does_not_invent_open_interest_change_when_upstream_omits_it(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "delaymarket": [
                        row(
                            "ag2608",
                            product="ag",
                            volume="224",
                            open_interest="5868",
                            open_interest_change=None,
                        )
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            context = await ShfePositioningProvider(http_client=client).get_context("ag")

        self.assertIsNone(context.open_interest_change)
        self.assertEqual(context.open_interest_change_contracts, 0)

    async def test_rejects_a_contract_from_another_product(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "delaymarket": [
                        row(
                            "au2608",
                            product="ag",
                            volume="1",
                            open_interest="2",
                            open_interest_change="0",
                        )
                    ]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = ShfePositioningProvider(http_client=client)
            with self.assertRaises(ProviderDataError):
                await provider.get_context("au")


if __name__ == "__main__":
    unittest.main()
