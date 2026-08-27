# ruff: noqa: RUF001
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

GoldEventTier = Literal["S+", "S", "A", "B"]
GoldEventFamily = Literal[
    "monetary-policy",
    "inflation",
    "employment",
    "growth",
    "geopolitical-risk",
    "financial-risk",
    "official-flow",
    "investment-flow",
    "physical-demand",
    "market-structure",
    "supply",
]
GoldTransmissionChannel = Literal[
    "real-yields",
    "usd",
    "risk",
    "liquidity",
    "central-bank",
    "etf",
    "positioning",
    "physical-demand",
    "supply",
]


@dataclass(frozen=True, slots=True)
class GoldEventTypeDefinition:
    event_type_id: str
    name: str
    family: GoldEventFamily
    baseline_tier: GoldEventTier
    cadence: str
    transmission_channels: tuple[GoldTransmissionChannel, ...]
    direction_rule: str
    official_source_urls: tuple[str, ...]
    us_dominance_trigger: bool = False


@dataclass(frozen=True, slots=True)
class GoldEventFact:
    event_id: str
    event_type_id: str
    title: str
    short_label: str
    country: str
    release_cluster_id: str | None
    scheduled_at: datetime | None
    released_at: datetime | None
    effective_period_start: datetime | None
    effective_period_end: datetime | None
    source_published_at: datetime
    ingested_at: datetime
    revision_vintage: str
    actual: str | None
    consensus: str | None
    previous: str | None
    revised: str | None
    source: str
    source_url: str
    source_tier: Literal["official", "institutional-research", "manual-verified"]
    time_precision: Literal["instant", "date"]
    flow_direction: Literal["inflow", "outflow", "mixed", "unknown"] = "unknown"
    flow_amount: float | None = None
    flow_unit: str | None = None
    note: str | None = None

    @property
    def marker_at(self) -> datetime:
        value = self.released_at or self.scheduled_at or self.effective_period_start
        if value is None:
            raise ValueError(f"gold event {self.event_id!r} does not have a marker time")
        return value


@dataclass(frozen=True, slots=True)
class GoldEventCatalogSnapshot:
    contract_version: str
    generated_at: datetime
    event_types: tuple[GoldEventTypeDefinition, ...]
    facts: tuple[dict[str, object], ...]
    score_methodology: dict[str, object]
    source_precedence: tuple[str, ...]
    limitations: tuple[str, ...]


_FED_CALENDAR = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_BLS_CALENDAR = "https://www.bls.gov/schedule/2026/home.htm"
_BEA_CALENDAR = "https://www.bea.gov/news/schedule/full"
_ISM_CALENDAR = (
    "https://www.ismworld.org/supply-management-news-and-reports/reports/rob-report-calendar/"
)
_CFTC_COT = "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"
_WGC_ETF = "https://www.gold.org/goldhub/data/gold-etfs-holdings-and-flows"
_WGC_CENTRAL_BANKS = (
    "https://www.gold.org/goldhub/research/gold-demand-trends/"
    "gold-demand-trends-full-year-2024/central-banks"
)
_WGC_CENTRAL_BANK_AGREEMENTS = (
    "https://www.gold.org/about-us/what-we-do/central-banks/central-bank-gold-agreements"
)
_WGC_2013 = "https://www.gold.org/goldhub/research/market-update/market-update-q2-2013"
_WGC_BREXIT = (
    "https://www.gold.org/goldhub/research/market-update/"
    "market-update-gold-surges-after-brexit-becomes-reality"
)
_WGC_2020 = (
    "https://www.gold.org/goldhub/research/gold-demand-trends/gold-demand-trends-full-year-2020"
)
_WGC_SVB = "https://www.gold.org/goldhub/research/gold-market-commentary-march-2023"
_IMF_RESERVES = "https://www.elibrary.imf.org/abstract/journals/001/2023/014/article-A001-en.xml"
_INGESTED_AT = datetime(2026, 8, 10, tzinfo=UTC)


def _type(
    event_type_id: str,
    name: str,
    family: GoldEventFamily,
    tier: GoldEventTier,
    cadence: str,
    channels: tuple[GoldTransmissionChannel, ...],
    direction_rule: str,
    sources: tuple[str, ...],
    *,
    us_dominance_trigger: bool = False,
) -> GoldEventTypeDefinition:
    return GoldEventTypeDefinition(
        event_type_id=event_type_id,
        name=name,
        family=family,
        baseline_tier=tier,
        cadence=cadence,
        transmission_channels=channels,
        direction_rule=direction_rule,
        official_source_urls=sources,
        us_dominance_trigger=us_dominance_trigger,
    )


GOLD_EVENT_TYPES: tuple[GoldEventTypeDefinition, ...] = (
    _type(
        "fed-fomc-decision",
        "FOMC 决议、声明与 SEP",
        "monetary-policy",
        "S+",
        "scheduled",
        ("real-yields", "usd", "risk", "liquidity"),
        "先比较政策路径与会前定价；偏鸽通常利多，偏鹰通常利空，金融风险可改变方向。",
        (_FED_CALENDAR,),
    ),
    _type(
        "fed-fomc-press-conference",
        "FOMC 主席发布会",
        "monetary-policy",
        "S+",
        "scheduled",
        ("real-yields", "usd", "risk", "liquidity"),
        "与声明属于同一事件簇，但从主席答问开始单独评估增量信息。",
        (_FED_CALENDAR,),
    ),
    _type(
        "fed-fomc-minutes",
        "FOMC 会议纪要",
        "monetary-policy",
        "S",
        "scheduled",
        ("real-yields", "usd"),
        "与原决议分开记录，比较纪要细节和市场已经消化的政策信息。",
        (_FED_CALENDAR,),
    ),
    _type(
        "fed-emergency-policy",
        "美联储紧急政策与流动性工具",
        "monetary-policy",
        "S+",
        "unscheduled",
        ("real-yields", "usd", "risk", "liquidity"),
        "先判断美元流动性冲击，再判断宽松和避险的持续影响。",
        (_FED_CALENDAR,),
    ),
    _type(
        "us-cpi",
        "美国 CPI",
        "inflation",
        "S+",
        "monthly",
        ("real-yields", "usd"),
        "热数据若推高实际利率和美元通常利空；通胀信誉恶化时中期方向可能相反。",
        (_BLS_CALENDAR,),
        us_dominance_trigger=True,
    ),
    _type(
        "us-employment-situation",
        "美国就业形势报告（大非农）",
        "employment",
        "S+",
        "monthly",
        ("real-yields", "usd", "risk"),
        "同时读取新增就业、失业率、时薪、参与率和修订；弱数据通常利多黄金。",
        (_BLS_CALENDAR,),
        us_dominance_trigger=True,
    ),
    _type(
        "us-pce",
        "美国 PCE 与核心 PCE",
        "inflation",
        "S",
        "monthly",
        ("real-yields", "usd"),
        "作为美联储目标通胀指标，方向取决于政策路径重定价。",
        (_BEA_CALENDAR,),
        us_dominance_trigger=True,
    ),
    _type(
        "us-retail-sales",
        "美国零售销售与控制组",
        "growth",
        "S",
        "monthly",
        ("real-yields", "usd", "risk"),
        "强消费通常推高收益率并压制黄金，衰退环境下反应更强。",
        (_BEA_CALENDAR,),
        us_dominance_trigger=True,
    ),
    _type(
        "us-ism",
        "美国 ISM 制造业/服务业",
        "growth",
        "S",
        "monthly",
        ("real-yields", "usd", "risk"),
        "同时读取增长、就业和价格分项，不以单一总指数判断方向。",
        (_ISM_CALENDAR,),
    ),
    _type(
        "us-gdp-advance",
        "美国 GDP 初值",
        "growth",
        "S",
        "quarterly",
        ("real-yields", "usd", "risk"),
        "增长意外通过政策路径和风险偏好传导；初值权重高于一般修订。",
        (_BEA_CALENDAR,),
        us_dominance_trigger=True,
    ),
    _type(
        "global-major-central-bank",
        "ECB、BoE、BoJ 等主要央行决议",
        "monetary-policy",
        "S",
        "scheduled",
        ("real-yields", "usd", "risk", "liquidity"),
        "通过全球收益率和交叉汇率影响美元金价，需按相对政策意外解释。",
        (),
    ),
    _type(
        "us-labor-secondary",
        "JOLTS、初请、ADP 与其他就业数据",
        "employment",
        "A",
        "weekly-or-monthly",
        ("real-yields", "usd"),
        "只作为就业趋势证据；ADP 不替代官方非农。",
        (_BLS_CALENDAR,),
    ),
    _type(
        "us-inflation-secondary",
        "PPI、ECI、工资与通胀预期",
        "inflation",
        "A",
        "monthly-or-quarterly",
        ("real-yields", "usd"),
        "在通胀交易环境中可动态升级，不设置永久高权重。",
        (_BLS_CALENDAR,),
    ),
    _type(
        "us-growth-secondary",
        "工业产出、耐用品、住房与信心数据",
        "growth",
        "A",
        "monthly",
        ("real-yields", "usd", "risk"),
        "在衰退或软着陆重定价阶段动态升级。",
        (_BEA_CALENDAR,),
    ),
    _type(
        "euro-macro",
        "欧元区 HICP、PMI 与 GDP",
        "growth",
        "A",
        "monthly-or-quarterly",
        ("real-yields", "usd"),
        "主要通过欧洲利率与 EUR/USD 交叉影响美元金价。",
        (),
    ),
    _type(
        "geopolitical-escalation",
        "战争、恐袭与重大地缘升级",
        "geopolitical-risk",
        "S+",
        "unscheduled",
        ("risk", "usd", "liquidity", "physical-demand"),
        "避险通常利多；美元融资或保证金压力可导致先卖黄金、后转强。",
        (_IMF_RESERVES,),
    ),
    _type(
        "sanctions-reserve-freeze",
        "制裁、储备冻结与资本管制",
        "geopolitical-risk",
        "S+",
        "unscheduled",
        ("risk", "central-bank", "usd"),
        "提高黄金作为无信用风险储备资产的结构性需求。",
        (_IMF_RESERVES,),
    ),
    _type(
        "systemic-financial-stress",
        "系统性银行与金融市场危机",
        "financial-risk",
        "S+",
        "unscheduled",
        ("risk", "liquidity", "real-yields", "usd"),
        "初期区分流动性抛售与避险买入，政策响应决定中期方向。",
        (_WGC_SVB,),
    ),
    _type(
        "sovereign-credit-stress",
        "主权违约、评级与债务上限风险",
        "financial-risk",
        "S",
        "unscheduled",
        ("risk", "usd", "real-yields"),
        "方向取决于风险是否集中于美元体系及其政策响应。",
        (_IMF_RESERVES,),
    ),
    _type(
        "official-gold-flow",
        "央行购金、售金与储备调拨",
        "official-flow",
        "S",
        "monthly-or-quarterly",
        ("central-bank", "physical-demand"),
        "以吨数和持续性判断中长期方向；公布日不等于交易发生日。",
        (_WGC_CENTRAL_BANKS,),
    ),
    _type(
        "gold-etf-flow",
        "全球实物黄金 ETF 创建与赎回",
        "investment-flow",
        "S",
        "daily-or-weekly",
        ("etf", "positioning", "liquidity"),
        "持续净流入偏多、持续净流出偏空；与期货仓位交叉验证。",
        (_WGC_ETF,),
    ),
    _type(
        "futures-positioning",
        "黄金期货仓位、未平仓量与期权偏度",
        "investment-flow",
        "A",
        "daily-or-weekly",
        ("positioning", "liquidity"),
        "成交量不是净资金流；COT 有周度滞后，必须记录可得时间。",
        (_CFTC_COT,),
    ),
    _type(
        "china-official-and-physical",
        "中国央行储备、进口配额与上海金溢价",
        "physical-demand",
        "A",
        "monthly-or-policy-event",
        ("central-bank", "physical-demand"),
        "分钟级全球价格发现通常有限，但对中长期官方和实物需求重要。",
        (_WGC_CENTRAL_BANKS,),
    ),
    _type(
        "india-gold-policy",
        "印度黄金关税、预算与进口政策",
        "physical-demand",
        "A",
        "policy-event",
        ("physical-demand",),
        "通过当地溢价、进口量和全球实物需求传导。",
        (),
    ),
    _type(
        "gold-market-dislocation",
        "交易所、交割、基差与流动性异常",
        "market-structure",
        "S",
        "unscheduled",
        ("liquidity", "positioning", "physical-demand"),
        "先区分价格发现异常、交割约束和真实供需短缺。",
        (_CFTC_COT,),
    ),
    _type(
        "gold-market-regime",
        "黄金市场制度与投资渠道改变",
        "market-structure",
        "A",
        "unscheduled",
        ("central-bank", "etf", "physical-demand", "supply"),
        "评估是否永久改变可投资性、官方供给或市场参与结构。",
        (_WGC_CENTRAL_BANK_AGREEMENTS,),
    ),
    _type(
        "gold-supply-shock",
        "矿山、回收金与生产商套保冲击",
        "supply",
        "A",
        "unscheduled-or-quarterly",
        ("supply", "physical-demand"),
        "按供应占比、持续时间和库存缓冲判断，通常慢于宏观冲击。",
        (),
    ),
)

_TYPE_BY_ID = {value.event_type_id: value for value in GOLD_EVENT_TYPES}


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _fact(
    event_id: str,
    event_type_id: str,
    title: str,
    short_label: str,
    marker_at: str,
    *,
    country: str = "US",
    release_cluster_id: str | None = None,
    scheduled: bool = True,
    released: bool = True,
    source_published_at: str | None = None,
    source: str,
    source_url: str,
    source_tier: Literal["official", "institutional-research", "manual-verified"],
    time_precision: Literal["instant", "date"] = "instant",
    effective_period_start: str | None = None,
    effective_period_end: str | None = None,
    actual: str | None = None,
    consensus: str | None = None,
    previous: str | None = None,
    revised: str | None = None,
    flow_direction: Literal["inflow", "outflow", "mixed", "unknown"] = "unknown",
    flow_amount: float | None = None,
    flow_unit: str | None = None,
    note: str | None = None,
) -> GoldEventFact:
    if event_type_id not in _TYPE_BY_ID:
        raise ValueError(f"unknown gold event type: {event_type_id}")
    marker = _utc(marker_at)
    known = _utc(source_published_at) if source_published_at else marker
    return GoldEventFact(
        event_id=event_id,
        event_type_id=event_type_id,
        title=title,
        short_label=short_label,
        country=country,
        release_cluster_id=release_cluster_id,
        scheduled_at=marker if scheduled else None,
        released_at=marker if released else None,
        effective_period_start=_utc(effective_period_start) if effective_period_start else None,
        effective_period_end=_utc(effective_period_end) if effective_period_end else None,
        source_published_at=known,
        ingested_at=_INGESTED_AT,
        revision_vintage="initial" if revised is None else "revised",
        actual=actual,
        consensus=consensus,
        previous=previous,
        revised=revised,
        source=source,
        source_url=source_url,
        source_tier=source_tier,
        time_precision=time_precision,
        flow_direction=flow_direction,
        flow_amount=flow_amount,
        flow_unit=flow_unit,
        note=note,
    )


def _bls_fact(
    event_id: str,
    event_type_id: Literal["us-cpi", "us-employment-situation"],
    marker_at: str,
    title: str,
) -> GoldEventFact:
    marker = _utc(marker_at)
    released = marker <= _INGESTED_AT
    return _fact(
        event_id,
        event_type_id,
        title,
        "CPI" if event_type_id == "us-cpi" else "非农",
        marker_at,
        released=released,
        source_published_at=marker_at if released else _INGESTED_AT.isoformat(),
        source="U.S. Bureau of Labor Statistics",
        source_url=_BLS_CALENDAR,
        source_tier="official",
        release_cluster_id=f"us-data:{marker.isoformat()}",
    )


_BLS_FACTS_2026 = tuple(
    _bls_fact(event_id, event_type_id, marker_at, title)
    for event_id, event_type_id, marker_at, title in (
        ("bls-nfp-2026-01", "us-employment-situation", "2026-01-09T13:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-01", "us-cpi", "2026-01-13T13:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-02", "us-employment-situation", "2026-02-11T13:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-02", "us-cpi", "2026-02-13T13:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-03", "us-employment-situation", "2026-03-06T13:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-03", "us-cpi", "2026-03-11T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-04", "us-employment-situation", "2026-04-03T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-04", "us-cpi", "2026-04-10T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-05", "us-employment-situation", "2026-05-08T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-05", "us-cpi", "2026-05-12T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-06", "us-employment-situation", "2026-06-05T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-06", "us-cpi", "2026-06-10T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-07", "us-employment-situation", "2026-07-02T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-07", "us-cpi", "2026-07-14T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-08", "us-employment-situation", "2026-08-07T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-08", "us-cpi", "2026-08-12T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-09", "us-employment-situation", "2026-09-04T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-09", "us-cpi", "2026-09-11T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-10", "us-employment-situation", "2026-10-02T12:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-10", "us-cpi", "2026-10-14T12:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-11", "us-employment-situation", "2026-11-06T13:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-11", "us-cpi", "2026-11-10T13:30:00Z", "美国 CPI"),
        ("bls-nfp-2026-12", "us-employment-situation", "2026-12-04T13:30:00Z", "美国非农就业"),
        ("bls-cpi-2026-12", "us-cpi", "2026-12-10T13:30:00Z", "美国 CPI"),
    )
)


def _fomc_facts(event_id: str, marker_at: str) -> tuple[GoldEventFact, GoldEventFact]:
    marker = _utc(marker_at)
    released = marker <= _INGESTED_AT
    cluster_id = f"fomc:{marker.date().isoformat()}"
    known_at = marker_at if released else _INGESTED_AT.isoformat()
    press_at = marker + timedelta(minutes=30)
    return (
        _fact(
            event_id,
            "fed-fomc-decision",
            "FOMC 利率决议",
            "FOMC",
            marker_at,
            released=released,
            source_published_at=known_at,
            source="Federal Reserve",
            source_url=_FED_CALENDAR,
            source_tier="official",
            release_cluster_id=cluster_id,
            note="声明与 SEP 同时点记录；主席发布会在 30 分钟后作为同簇独立阶段记录。",
        ),
        _fact(
            f"{event_id}-press",
            "fed-fomc-press-conference",
            "FOMC 主席发布会",
            "发布会",
            press_at.isoformat(),
            released=press_at <= _INGESTED_AT,
            source_published_at=known_at,
            source="Federal Reserve",
            source_url=_FED_CALENDAR,
            source_tier="official",
            release_cluster_id=cluster_id,
            note="只评估主席答问相对声明时点带来的增量信息。",
        ),
    )


_FOMC_FACTS_2026 = tuple(
    fact
    for event_id, marker_at in (
        ("fomc-2026-01", "2026-01-28T19:00:00Z"),
        ("fomc-2026-03", "2026-03-18T18:00:00Z"),
        ("fomc-2026-04", "2026-04-29T18:00:00Z"),
        ("fomc-2026-06", "2026-06-17T18:00:00Z"),
        ("fomc-2026-07", "2026-07-29T18:00:00Z"),
        ("fomc-2026-09", "2026-09-16T18:00:00Z"),
        ("fomc-2026-10", "2026-10-28T18:00:00Z"),
        ("fomc-2026-12", "2026-12-09T19:00:00Z"),
    )
    for fact in _fomc_facts(event_id, marker_at)
)


GOLD_EVENT_FACTS: tuple[GoldEventFact, ...] = (
    _fact(
        "washington-agreement-1999",
        "gold-market-regime",
        "华盛顿黄金协议限制欧洲央行无序售金",
        "售金协议",
        "1999-09-26T00:00:00Z",
        country="GLOBAL",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_CENTRAL_BANK_AGREEMENTS,
        source_tier="institutional-research",
        time_precision="date",
        note="制度性降低官方售金不确定性；日期标记不代表单一成交时点。",
    ),
    _fact(
        "global-financial-crisis-2008",
        "systemic-financial-stress",
        "雷曼破产与全球美元流动性危机",
        "雷曼危机",
        "2008-09-15T00:00:00Z",
        country="GLOBAL",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_2020,
        source_tier="institutional-research",
        time_precision="date",
        note="黄金曾受保证金和美元流动性需求拖累，随后受避险和政策宽松支持。",
    ),
    _fact(
        "gold-liquidation-2013-04",
        "gold-etf-flow",
        "黄金 ETF 流出与杠杆清算踩踏",
        "ETF踩踏",
        "2013-04-12T00:00:00Z",
        country="GLOBAL",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_2013,
        source_tier="institutional-research",
        time_precision="date",
        flow_direction="outflow",
        note="代表性清算窗口为 4 月 12—16 日，不将整段行情归因于单一消息。",
    ),
    _fact(
        "brexit-result-2016",
        "geopolitical-escalation",
        "英国脱欧公投结果",
        "Brexit",
        "2016-06-24T00:00:00Z",
        country="GB",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_BREXIT,
        source_tier="institutional-research",
        time_precision="date",
    ),
    _fact(
        "covid-global-pandemic-2020",
        "systemic-financial-stress",
        "COVID-19 全球疫情与大规模政策宽松",
        "COVID",
        "2020-03-11T00:00:00Z",
        country="GLOBAL",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_2020,
        source_tier="institutional-research",
        time_precision="date",
        note="需将初期流动性抛售和后续宽松、ETF 流入分阶段评估。",
    ),
    _fact(
        "russia-reserve-freeze-2022",
        "sanctions-reserve-freeze",
        "俄罗斯外汇储备冻结改变官方储备安全偏好",
        "储备冻结",
        "2022-02-28T00:00:00Z",
        country="GLOBAL",
        scheduled=False,
        source="International Monetary Fund",
        source_url=_IMF_RESERVES,
        source_tier="institutional-research",
        time_precision="date",
    ),
    _fact(
        "svb-failure-2023",
        "systemic-financial-stress",
        "硅谷银行关闭引发美国银行体系压力",
        "SVB",
        "2023-03-10T00:00:00Z",
        scheduled=False,
        source="World Gold Council",
        source_url=_WGC_SVB,
        source_tier="institutional-research",
        time_precision="date",
    ),
    _fact(
        "wgc-central-bank-gold-2026-07",
        "official-gold-flow",
        "全球央行 2026 年 5 月净购金 41 吨",
        "央行金",
        "2026-07-02T12:00:00Z",
        country="GLOBAL",
        source="World Gold Council",
        source_url=(
            "https://www.gold.org/goldhub/gold-focus/2026/07/"
            "central-bank-gold-statistics-central-banks-remain-committed-gold"
        ),
        source_tier="institutional-research",
        time_precision="date",
        effective_period_start="2026-05-01T00:00:00Z",
        effective_period_end="2026-06-01T00:00:00Z",
        actual="41",
        flow_direction="inflow",
        flow_amount=41,
        flow_unit="tonnes",
        note="公布时间与交易发生月份严格分离。",
    ),
    *_BLS_FACTS_2026,
    *_FOMC_FACTS_2026,
    _fact(
        "fomc-minutes-2026-08",
        "fed-fomc-minutes",
        "FOMC 会议纪要",
        "纪要",
        "2026-08-19T18:00:00Z",
        released=False,
        source_published_at=_INGESTED_AT.isoformat(),
        source="Federal Reserve",
        source_url=_FED_CALENDAR,
        source_tier="official",
        release_cluster_id="fomc:2026-07-29",
        note="会议纪要是独立公布事件，不与原决议时间合并。",
    ),
)


SCORE_METHODOLOGY: dict[str, object] = {
    "shock": {
        "label": "短期冲击分",
        "weights": {
            "realized_volatility": 35,
            "surprise": 20,
            "volume_and_liquidity": 20,
            "cross_asset_confirmation": 15,
            "cross_venue_breadth": 10,
        },
        "windows_seconds": (60, 300, 1800, 7200),
        "rule": "各窗口相对同周期历史波动做稳健标准化；缺失证据必须降低覆盖率。",
    },
    "regime": {
        "label": "中长期定价分",
        "weights": {
            "persistence": 30,
            "durable_fund_flow": 25,
            "macro_repricing": 20,
            "historical_reliability": 15,
            "policy_irreversibility": 10,
        },
        "windows_seconds": (14400, 86400, 432000, 1728000),
        "rule": "同时报告分值和证据覆盖率；价格、成交量不得冒充净资金流。",
    },
    "tiers": {"S+": (85, 100), "S": (70, 84), "A": (55, 69), "B": (40, 54)},
}

SOURCE_PRECEDENCE = (
    "official-agency",
    "central-bank-or-exchange",
    "multilateral-or-world-gold-council",
    "financial-institution-research",
    "news-or-manual-verification",
)

LIMITATIONS = (
    "当前内置事实是经核验的骨架，不声称穷尽所有历史事件。",
    "尚未接入美元、实际利率、ETF、COT、期权和跨市场实时证据时，评分必须显示覆盖率。",
    "央行和实物资金流可能延迟公布；effective_period 与 source_published_at 不得互换。",
    "金融机构价格目标和主观预测不作为事件事实或固定评分权重。",
)


def _public_fact(value: GoldEventFact) -> dict[str, object]:
    event_type = _TYPE_BY_ID[value.event_type_id]
    result = asdict(value)
    result["marker_at"] = value.marker_at
    result["family"] = event_type.family
    result["baseline_tier"] = event_type.baseline_tier
    result["transmission_channels"] = event_type.transmission_channels
    result["direction_rule"] = event_type.direction_rule
    result["us_dominance_trigger"] = event_type.us_dominance_trigger
    return result


def gold_event_catalog_snapshot(
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    as_of: datetime | None = None,
    generated_at: datetime | None = None,
) -> GoldEventCatalogSnapshot:
    for label, value in (("start", start), ("end", end), ("as_of", as_of)):
        if value is not None and value.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
    if start is not None and end is not None and end <= start:
        raise ValueError("end must be later than start")

    facts: list[GoldEventFact] = []
    for fact in GOLD_EVENT_FACTS:
        marker_at = fact.marker_at
        if start is not None and marker_at < start:
            continue
        if end is not None and marker_at >= end:
            continue
        if as_of is not None and fact.source_published_at > as_of:
            continue
        facts.append(fact)
    facts.sort(key=lambda value: (value.marker_at, value.event_id))
    return GoldEventCatalogSnapshot(
        contract_version="gold-events-v1",
        generated_at=generated_at or datetime.now(UTC),
        event_types=GOLD_EVENT_TYPES,
        facts=tuple(_public_fact(value) for value in facts),
        score_methodology=SCORE_METHODOLOGY,
        source_precedence=SOURCE_PRECEDENCE,
        limitations=LIMITATIONS,
    )
