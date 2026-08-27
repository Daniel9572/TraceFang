from __future__ import annotations

from zoneinfo import ZoneInfo

from tracefang.domain.errors import InstrumentNotSupportedError
from tracefang.domain.models import Instrument
from tracefang.instruments import (
    BRENT_CRUDE_CONTINUOUS,
    NASDAQ_COMPOSITE,
    SHFE_GOLD_2610,
    SHFE_GOLD_WEIGHTED,
    SHFE_SILVER_2706,
    SHFE_SILVER_WEIGHTED,
    SSE_COMPOSITE,
    US_DOLLAR_INDEX,
)


class TonghuashunFuturesSymbolMapper:
    def __init__(self) -> None:
        self._to_provider = {
            SHFE_GOLD_WEIGHTED: "qh_au8888",
            SHFE_SILVER_WEIGHTED: "qh_ag8888",
            SHFE_GOLD_2610: "qh_au2610",
            SHFE_SILVER_2706: "qh_ag2706",
            US_DOLLAR_INDEX: "wh_USDIND",
            BRENT_CRUDE_CONTINUOUS: "219_BRN0Y",
            SSE_COMPOSITE: "zs_1A0001",
            NASDAQ_COMPOSITE: "88_IXIC",
        }
        self._names = {
            SHFE_GOLD_WEIGHTED: "沪金加权",
            SHFE_SILVER_WEIGHTED: "沪银加权",
            SHFE_GOLD_2610: "沪金2610",
            SHFE_SILVER_2706: "沪银2706",
            US_DOLLAR_INDEX: "美元指数",
            BRENT_CRUDE_CONTINUOUS: "布伦特原油连续",
            SSE_COMPOSITE: "上证指数",
            NASDAQ_COMPOSITE: "纳斯达克综合指数",
        }
        self._digits = {
            SHFE_GOLD_WEIGHTED: 2,
            SHFE_SILVER_WEIGHTED: 0,
            SHFE_GOLD_2610: 2,
            SHFE_SILVER_2706: 0,
            US_DOLLAR_INDEX: 4,
            BRENT_CRUDE_CONTINUOUS: 2,
            SSE_COMPOSITE: 2,
            NASDAQ_COMPOSITE: 3,
        }
        self._line_time_zones = {
            SHFE_GOLD_WEIGHTED: ZoneInfo("Asia/Shanghai"),
            SHFE_SILVER_WEIGHTED: ZoneInfo("Asia/Shanghai"),
            SHFE_GOLD_2610: ZoneInfo("Asia/Shanghai"),
            SHFE_SILVER_2706: ZoneInfo("Asia/Shanghai"),
            US_DOLLAR_INDEX: ZoneInfo("UTC"),
            BRENT_CRUDE_CONTINUOUS: ZoneInfo("Europe/London"),
            SSE_COMPOSITE: ZoneInfo("Asia/Shanghai"),
            NASDAQ_COMPOSITE: ZoneInfo("America/New_York"),
        }
        self._quote_calendar_modes = {
            SHFE_GOLD_WEIGHTED: "session_dates",
            SHFE_SILVER_WEIGHTED: "session_dates",
            SHFE_GOLD_2610: "session_dates",
            SHFE_SILVER_2706: "session_dates",
            US_DOLLAR_INDEX: "trade_date",
            BRENT_CRUDE_CONTINUOUS: "trade_date",
            SSE_COMPOSITE: "trade_date",
            NASDAQ_COMPOSITE: "trade_date",
        }
        self._from_provider = {value.lower(): key for key, value in self._to_provider.items()}

    @property
    def provider_codes(self) -> tuple[str, ...]:
        return tuple(self._to_provider.values())

    @property
    def instruments(self) -> tuple[Instrument, ...]:
        return tuple(self._to_provider)

    def to_provider_code(self, instrument: Instrument) -> str:
        try:
            return self._to_provider[instrument]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"tonghuashun public data has no configured mapping for {instrument.symbol}"
            ) from error

    def from_provider_code(self, code: str) -> Instrument:
        try:
            return self._from_provider[code.lower()]
        except KeyError as error:
            raise InstrumentNotSupportedError(
                f"tonghuashun public code {code!r} has no canonical mapping"
            ) from error

    def expected_name(self, instrument: Instrument) -> str:
        self.to_provider_code(instrument)
        return self._names[instrument]

    def price_digits(self, instrument: Instrument) -> int:
        self.to_provider_code(instrument)
        return self._digits[instrument]

    def line_time_zone(self, instrument: Instrument) -> ZoneInfo:
        self.to_provider_code(instrument)
        return self._line_time_zones[instrument]

    def quote_calendar_mode(self, instrument: Instrument) -> str:
        self.to_provider_code(instrument)
        return self._quote_calendar_modes[instrument]
