from __future__ import annotations

import unittest

from tracefang.infrastructure.providers.jin10 import Jin10SymbolMapper
from tracefang.infrastructure.providers.jin10_local.symbols import (
    Jin10LocalSymbolMapper,
)
from tracefang.infrastructure.providers.jin10_web.symbols import Jin10WebSymbolMapper
from tracefang.infrastructure.providers.tonghuashun_futures import (
    TonghuashunFuturesSymbolMapper,
)
from tracefang.instruments import (
    BRENT_CRUDE_CONTINUOUS,
    INSTRUMENT_CATALOG,
    NASDAQ_COMPOSITE,
    SHFE_GOLD_2610,
    SHFE_GOLD_WEIGHTED,
    SHFE_SILVER_2706,
    SHFE_SILVER_WEIGHTED,
    SPOT_GOLD,
    SPOT_GOLD_CNH_PER_GRAM,
    SSE_COMPOSITE,
    US_DOLLAR_INDEX,
    USD_CNH,
    direct_requirements,
    instrument_definition,
)


class InstrumentCatalogTests(unittest.TestCase):
    def test_codes_and_symbols_are_unique(self) -> None:
        self.assertEqual(
            len({item.code for item in INSTRUMENT_CATALOG}),
            len(INSTRUMENT_CATALOG),
        )
        self.assertEqual(
            len({item.instrument.symbol for item in INSTRUMENT_CATALOG}),
            len(INSTRUMENT_CATALOG),
        )

    def test_usdcnh_maps_to_the_realtime_jin10_symbol(self) -> None:
        self.assertEqual(Jin10SymbolMapper().to_provider_code(USD_CNH), "USDCNH")
        self.assertEqual(
            Jin10WebSymbolMapper().to_provider_code(USD_CNH),
            "USDCNH.FXCM",
        )
        self.assertEqual(
            Jin10LocalSymbolMapper().to_provider_code(USD_CNH),
            "USDCNH.FXCM",
        )

    def test_derived_gold_declares_both_direct_requirements(self) -> None:
        definition = instrument_definition("xaucnhg")

        self.assertEqual(definition.instrument, SPOT_GOLD_CNH_PER_GRAM)
        self.assertEqual(definition.quote_kind, "derived")
        self.assertFalse(definition.history_available)
        self.assertEqual(direct_requirements(definition), (SPOT_GOLD, USD_CNH))

    def test_tonghuashun_instruments_use_the_dedicated_public_source(self) -> None:
        mapper = TonghuashunFuturesSymbolMapper()
        gold = instrument_definition("au8888")
        silver = instrument_definition("AG8888")

        self.assertEqual(gold.instrument, SHFE_GOLD_WEIGHTED)
        self.assertEqual(silver.instrument, SHFE_SILVER_WEIGHTED)
        self.assertEqual(gold.source_ids, ("tonghuashun_futures",))
        self.assertEqual(silver.source_ids, ("tonghuashun_futures",))
        self.assertEqual(mapper.to_provider_code(gold.instrument), "qh_au8888")
        self.assertEqual(mapper.to_provider_code(silver.instrument), "qh_ag8888")
        self.assertEqual(direct_requirements(gold), (SHFE_GOLD_WEIGHTED,))

        expected = {
            "AU2610": (SHFE_GOLD_2610, "qh_au2610"),
            "AG2706": (SHFE_SILVER_2706, "qh_ag2706"),
            "USDIND": (US_DOLLAR_INDEX, "wh_USDIND"),
            "BRN0Y": (BRENT_CRUDE_CONTINUOUS, "219_BRN0Y"),
            "SHCOMP": (SSE_COMPOSITE, "zs_1A0001"),
            "IXIC": (NASDAQ_COMPOSITE, "88_IXIC"),
        }
        for code, (instrument, provider_code) in expected.items():
            definition = instrument_definition(code)
            self.assertEqual(definition.instrument, instrument)
            self.assertEqual(definition.source_ids, ("tonghuashun_futures",))
            self.assertEqual(mapper.to_provider_code(instrument), provider_code)


if __name__ == "__main__":
    unittest.main()
