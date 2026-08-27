from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from tracefang.domain.errors import InstrumentNotSupportedError
from tracefang.domain.models import AssetClass, Instrument

TROY_OUNCE_GRAMS = Decimal("31.1034768")

SPOT_GOLD = Instrument(
    symbol="XAU/USD",
    asset_class=AssetClass.SPOT,
    base="XAU",
    quote="USD",
    venue="OTC",
)
SPOT_SILVER = Instrument(
    symbol="XAG/USD",
    asset_class=AssetClass.SPOT,
    base="XAG",
    quote="USD",
    venue="OTC",
)
USD_CNH = Instrument(
    symbol="USD/CNH",
    asset_class=AssetClass.FOREX,
    base="USD",
    quote="CNH",
    venue="OTC",
)
SPOT_GOLD_CNH_PER_GRAM = Instrument(
    symbol="XAU/CNH-GRAM",
    asset_class=AssetClass.SPOT,
    base="XAU",
    quote="CNH",
    venue="DERIVED",
)
SHFE_GOLD_WEIGHTED = Instrument(
    symbol="AU8888",
    asset_class=AssetClass.INDEX,
    base="AU",
    quote="CNY",
    venue="SHFE",
)
SHFE_SILVER_WEIGHTED = Instrument(
    symbol="AG8888",
    asset_class=AssetClass.INDEX,
    base="AG",
    quote="CNY",
    venue="SHFE",
)
SHFE_GOLD_2610 = Instrument(
    symbol="AU2610",
    asset_class=AssetClass.FUTURE,
    base="AU",
    quote="CNY",
    venue="SHFE",
)
SHFE_SILVER_2706 = Instrument(
    symbol="AG2706",
    asset_class=AssetClass.FUTURE,
    base="AG",
    quote="CNY",
    venue="SHFE",
)
US_DOLLAR_INDEX = Instrument(
    symbol="USDIND",
    asset_class=AssetClass.INDEX,
    base="USD",
    quote="BASKET",
    venue="ICE",
)
BRENT_CRUDE_CONTINUOUS = Instrument(
    symbol="BRN0Y",
    asset_class=AssetClass.FUTURE,
    base="BRN",
    quote="USD",
    venue="ICE",
)
SSE_COMPOSITE = Instrument(
    symbol="000001.SH",
    asset_class=AssetClass.INDEX,
    base="SSE",
    quote="CNY",
    venue="SSE",
)
NASDAQ_COMPOSITE = Instrument(
    symbol="IXIC",
    asset_class=AssetClass.INDEX,
    base="NASDAQ",
    quote="USD",
    venue="NASDAQ",
)


@dataclass(frozen=True, slots=True)
class InstrumentDefinition:
    code: str
    name: str
    instrument: Instrument
    price_unit: str
    price_digits: int
    quote_kind: str
    history_available: bool
    dependencies: tuple[Instrument, ...] = ()
    source_ids: tuple[str, ...] = ("jin10_client",)
    market_schedule_id: str = "spot_metals"


INSTRUMENT_CATALOG = (
    InstrumentDefinition(
        code="XAUUSD",
        name="现货黄金",
        instrument=SPOT_GOLD,
        price_unit="美元/盎司",
        price_digits=2,
        quote_kind="direct",
        history_available=True,
    ),
    InstrumentDefinition(
        code="XAGUSD",
        name="现货白银",
        instrument=SPOT_SILVER,
        price_unit="美元/盎司",
        price_digits=3,
        quote_kind="direct",
        history_available=True,
    ),
    InstrumentDefinition(
        code="USDCNH",
        name="美元兑离岸人民币",
        instrument=USD_CNH,
        price_unit="人民币/美元",
        price_digits=4,
        quote_kind="direct",
        history_available=True,
        market_schedule_id="forex",
    ),
    InstrumentDefinition(
        code="XAUCNHG",
        name="国际金(人民币/克)",
        instrument=SPOT_GOLD_CNH_PER_GRAM,
        price_unit="人民币/克",
        price_digits=2,
        quote_kind="derived",
        history_available=False,
        dependencies=(SPOT_GOLD, USD_CNH),
    ),
    InstrumentDefinition(
        code="AU8888",
        name="沪金加权",
        instrument=SHFE_GOLD_WEIGHTED,
        price_unit="人民币/克",
        price_digits=2,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="shfe_metals",
    ),
    InstrumentDefinition(
        code="AG8888",
        name="沪银加权",
        instrument=SHFE_SILVER_WEIGHTED,
        price_unit="人民币/千克",
        price_digits=0,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="shfe_metals",
    ),
    InstrumentDefinition(
        code="AU2610",
        name="沪金2610",
        instrument=SHFE_GOLD_2610,
        price_unit="人民币/克",
        price_digits=2,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="shfe_metals",
    ),
    InstrumentDefinition(
        code="AG2706",
        name="沪银2706",
        instrument=SHFE_SILVER_2706,
        price_unit="人民币/千克",
        price_digits=0,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="shfe_metals",
    ),
    InstrumentDefinition(
        code="USDIND",
        name="美元指数",
        instrument=US_DOLLAR_INDEX,
        price_unit="点",
        price_digits=4,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="usd_index",
    ),
    InstrumentDefinition(
        code="BRN0Y",
        name="布伦特原油连续",
        instrument=BRENT_CRUDE_CONTINUOUS,
        price_unit="美元/桶",
        price_digits=2,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="ice_brent",
    ),
    InstrumentDefinition(
        code="SHCOMP",
        name="上证指数",
        instrument=SSE_COMPOSITE,
        price_unit="点",
        price_digits=2,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="sse",
    ),
    InstrumentDefinition(
        code="IXIC",
        name="纳斯达克综合指数",
        instrument=NASDAQ_COMPOSITE,
        price_unit="点",
        price_digits=3,
        quote_kind="direct",
        history_available=True,
        source_ids=("tonghuashun_futures",),
        market_schedule_id="nasdaq",
    ),
)

DEFAULT_WATCHLIST_CODES = (
    "XAUUSD",
    "XAGUSD",
    "AU8888",
    "AG8888",
    "AU2610",
    "AG2706",
    "USDIND",
    "BRN0Y",
    "SHCOMP",
    "IXIC",
)

_BY_CODE = {item.code: item for item in INSTRUMENT_CATALOG}
_BY_INSTRUMENT = {item.instrument: item for item in INSTRUMENT_CATALOG}
_BY_SYMBOL = {item.instrument.symbol: item for item in INSTRUMENT_CATALOG}


def instrument_definition(code: str) -> InstrumentDefinition:
    try:
        return _BY_CODE[code.upper()]
    except KeyError as error:
        raise InstrumentNotSupportedError(
            f"instrument code {code!r} has no canonical mapping"
        ) from error


def definition_for_instrument(instrument: Instrument) -> InstrumentDefinition:
    try:
        return _BY_INSTRUMENT[instrument]
    except KeyError as error:
        raise InstrumentNotSupportedError(
            f"instrument {instrument.symbol!r} is not in the catalog"
        ) from error


def definition_for_symbol(symbol: str) -> InstrumentDefinition:
    try:
        return _BY_SYMBOL[symbol]
    except KeyError as error:
        raise InstrumentNotSupportedError(
            f"instrument symbol {symbol!r} is not in the catalog"
        ) from error


def direct_requirements(definition: InstrumentDefinition) -> tuple[Instrument, ...]:
    if definition.quote_kind == "direct":
        return (definition.instrument,)
    return definition.dependencies
