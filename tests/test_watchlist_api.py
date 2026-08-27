from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from tracefang import api


class WatchlistApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.original_codes = list(api.runtime.watchlist_codes)
        self.original_store = api.runtime.database_store
        api.runtime.watchlist_codes = ["XAUUSD", "XAGUSD"]
        api.runtime.database_store = None

    def tearDown(self) -> None:
        api.runtime.watchlist_codes = self.original_codes
        api.runtime.database_store = self.original_store

    async def test_add_and_remove_update_the_public_watchlist(self) -> None:
        with (
            patch.object(
                api,
                "_source_for_instrument",
                new=AsyncMock(return_value="jin10_client"),
            ),
            patch.object(
                api,
                "_refresh_watchlist_routes",
                new=AsyncMock(),
            ),
        ):
            added = await api.add_watchlist_instrument("usdcnh")
            removed = await api.remove_watchlist_instrument("USDCNH")

        self.assertEqual(
            [item["provider_code"] for item in added],
            ["XAUUSD", "XAGUSD", "USDCNH"],
        )
        self.assertEqual(
            [item["provider_code"] for item in removed],
            ["XAUUSD", "XAGUSD"],
        )

    async def test_cannot_remove_the_last_observed_instrument(self) -> None:
        api.runtime.watchlist_codes = ["XAUUSD"]

        with self.assertRaises(HTTPException) as raised:
            await api.remove_watchlist_instrument("XAUUSD")

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
