"""Regenerate language-neutral golden cases from the existing Python implementation."""
from __future__ import annotations
import json
import sys
from dataclasses import asdict, is_dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
from tracefang.application.realtime_bars import RealtimeBarContract, RealtimeBarService
from tracefang.application.period_bars import (
    PERIOD_DEFINITIONS, PeriodBarService, project_period_bars, _bucket_for,
    _previous_bucket, _materialization_version,
)
from tracefang.application.chart_history import _schedule_cursor_version
from tracefang.domain.market_events import BarState, RealtimeBar, quote_event_id
from tracefang.domain.models import AssetClass, Instrument, QuoteSnapshot, SourceMetadata, Candle

INSTRUMENT = Instrument("XAU/USD", AssetClass.SPOT, "XAU", "USD", "OTC")
START = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def encode(value):
    if is_dataclass(value): return asdict(value)
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, timedelta): return int(value.total_seconds())
    if isinstance(value, Decimal): return str(value)
    raise TypeError(type(value))


def quote(value, seconds, *, received=None, raw=None):
    observed = START + timedelta(seconds=seconds)
    return QuoteSnapshot(INSTRUMENT, Decimal(value), None, None, None, None, None, None,
        SourceMetadata("live-a", "XAUUSD", observed,
            START + timedelta(seconds=received) if received is not None else observed,
            raw))


def candle(value, minute, received, state):
    at = START + timedelta(minutes=minute)
    value = Decimal(value)
    return Candle(INSTRUMENT, timedelta(minutes=1), at, value, value, value, value, None,
        SourceMetadata("history-a", "XAUUSD", at, START+timedelta(seconds=received),
            {"bar_state": state.value}))


def sequence(name, observations):
    service = RealtimeBarService(None, contracts=(RealtimeBarContract(
        source_id="source-a", authoritative_bar_channel_id="history-a", quote_channel_ids=("live-a",)),))
    steps=[]
    for value in observations:
        kind = "quote" if isinstance(value, QuoteSnapshot) else "bar"
        event=service.normalize_quote(value) if kind == "quote" else service.normalize_bar(value)
        transitions=service.apply(event)
        steps.append({"kind":kind,"input":value,"expected":transitions,
            "event_id":quote_event_id(value) if kind=="quote" else None})
    return {"name":name,"steps":steps}


sequences = [
    sequence("every_second_revision", [quote("4200.50",0.1),quote("4210.25",0.8),quote("4200.50",0.9),quote("4220.75",1.1),quote("4220.75",1.2),quote("4300",60.1)]),
    sequence("authority_correction_and_finality", [quote("4215",10),candle("4200",0,20,BarState.PROVISIONAL_AUTHORITATIVE),quote("4210",30),candle("4211",1,65,BarState.PROVISIONAL_AUTHORITATIVE),quote("4300",40),candle("4205",0,180,BarState.PROVISIONAL_AUTHORITATIVE)]),
    sequence("freshness_and_late_delivery", [quote("4210",30),quote("4200",20),quote("9999",-3600,received=40),quote("4212",35,received=41),quote("1234",200,received=42)]),
    sequence("minute_precision", [quote("100",0,received=95,raw={"timestamp_precision_seconds":60}),quote("101",0,received=125,raw={"timestamp_precision_seconds":60})]),
    sequence("transport_event_identity", [quote("4300.00",0.000001,raw={"connection_id":"conn-1","sequence":1}),quote("4300.00",0.000001,received=0.000002,raw={"connection_id":"conn-1","sequence":2}),quote("4300.00",0.000001,raw={"connection_id":"conn-1","sequence":1})]),
]

def bar(at,price,state=BarState.FINAL,revision=1,volume="1"):
    at=datetime.fromisoformat(at).astimezone(UTC)
    price=Decimal(price)
    received=at+timedelta(minutes=1)
    return RealtimeBar(INSTRUMENT,timedelta(minutes=1),at,price,price,price,price,
        Decimal(volume) if volume is not None else None,SourceMetadata("source-a","XAUUSD",at,received),
        evidence_channel_id="history-a",state=state,revision=revision,
        finalized_at=received if state is BarState.FINAL else None)

schedules=json.loads((ROOT/"backend/assets/schedules.json").read_text())
shfe=next(value for value in schedules.values() if value.get("trading_day_rule")=="shfe")
now=datetime(2026,10,1,tzinfo=UTC)
rows=[bar("2026-08-10T09:01:00+08:00","100.1"),bar("2026-08-10T10:14:00+08:00","101.2",volume=None),bar("2026-08-10T10:30:00+08:00","102.3"),bar("2026-08-10T21:00:00+08:00","100.5"),bar("2026-08-11T09:00:00+08:00","103.6"),bar("2026-08-11T14:59:00+08:00","101.7")]
periods=[]
for period in PERIOD_DEFINITIONS:
    periods.append({"name":f"shfe_{period}","period":period,"schedule":shfe,"rows":rows,"now":now,"expected":project_period_bars(rows,period_id=period,schedule=shfe,now=now)})
for period in ["1d","1w","1mo"]:
    ny=schedules["spot_metals"]
    dstrows=[bar("2026-03-06T16:59:00-05:00","4999.1"),bar("2026-03-08T18:00:00-04:00","5000.2"),bar("2026-03-09T16:59:00-04:00","5001.3")]
    periods.append({"name":f"ny_dst_{period}","period":period,"schedule":ny,"rows":dstrows,"now":now,"expected":project_period_bars(dstrows,period_id=period,schedule=ny,now=now)})
periods.append({"name":"daily_not_final_before_session_close","period":"1d","schedule":shfe,"rows":[rows[3]],"now":datetime.fromisoformat("2026-08-11T10:00:00+08:00"),"expected":project_period_bars([rows[3]],period_id="1d",schedule=shfe,now=datetime.fromisoformat("2026-08-11T10:00:00+08:00"))})
periods.append({"name":"missing_volume_stays_null","period":"5m","schedule":None,"rows":[replace(rows[0],volume=None)],"now":now,"expected":project_period_bars([replace(rows[0],volume=None)],period_id="5m",schedule=None,now=now)})

live=PeriodBarService(object())
live_cases=[]
for row in [bar("2026-08-10T09:00:00+08:00","100"),bar("2026-08-10T09:01:00+08:00","105"),bar("2026-08-10T09:01:00+08:00","102",revision=2),bar("2026-08-10T09:05:00+08:00","110"),bar("2026-08-10T09:01:00+08:00","109",revision=3)]:
    live_cases.append({"input":row,"expected":[value for period,value in live.accept_live(row,schedule=shfe,period_ids=["5m"])]})

buckets=[]
for schedule_name,schedule in schedules.items():
    for period in ["5m","1h","1d","1w","1mo","1q","1y"]:
        at=datetime.fromisoformat("2026-08-11T09:30:00+08:00").astimezone(UTC)
        bucket=_bucket_for(at,PERIOD_DEFINITIONS[period],schedule)
        previous=_previous_bucket(bucket,PERIOD_DEFINITIONS[period],schedule)
        buckets.append({"name":f"{schedule_name}_{period}","at":at,"period":period,"schedule":schedule,"bucket":bucket,"previous":previous})
versions=[{"schedule":value,"cursor":_schedule_cursor_version(value),"materialization":_materialization_version(value)} for value in [None,*schedules.values()]]
output={"sequences":sequences,"periods":periods,"live":{"schedule":shfe,"period":"5m","steps":live_cases},"buckets":buckets,"versions":versions}
Path(__file__).with_name("golden.json").write_text(json.dumps(output,ensure_ascii=False,indent=2,default=encode)+"\n")
print(f"Generated {len(sequences)} reducer, {len(periods)} period, {len(buckets)} bucket and {len(versions)} hash cases")
