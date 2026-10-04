//! Chart pages are exclusively read from one committed native MVCC view.
use crate::{catalog::database_bar,market::Market};
use anyhow::{Result,Context,bail};
use base64::{Engine,engine::general_purpose::URL_SAFE_NO_PAD};
use chrono::{TimeZone,Utc};
use serde::Serialize;
use serde_json::{Value,json};
use std::collections::BTreeMap;
use tracefang_core::{domain::{Instrument,Timestamp,isoformat},events::RealtimeBar,
    periods::{MarketSchedule,Period,canonical_json_ascii,schedule_version},reducer::merge_for_read,
    persistence_contract::{SnapshotVersion,CanonicalSnapshotRequest,BarSelection,CanonicalPageTimings}};
#[derive(Debug,Clone,Serialize)]
pub struct PeriodPage {pub period_id:String,pub items:Vec<RealtimeBar>,pub next_before:Option<Timestamp>,pub has_more:bool,pub snapshot_version:SnapshotVersion,pub semantics:String,pub coverage:Value,#[serde(skip)]pub timings:CanonicalPageTimings}
pub fn schedule(market:&Market,code:&str)->Result<MarketSchedule> {let d=market.catalog.get(code)?;Ok(serde_json::from_value(market.catalog.schedules.get(&d.market_schedule_id).context("market schedule")?.clone())?)}
pub fn merge_rows(
    database: Vec<RealtimeBar>,
    hot: Vec<RealtimeBar>,
    before: Option<Timestamp>,
) -> Vec<RealtimeBar> {
    let mut rows: BTreeMap<Timestamp, RealtimeBar> = database
        .into_iter()
        .filter(|v| before.is_none_or(|at| v.open_time < at))
        .map(|v| (v.open_time, v))
        .collect();
    for bar in hot
        .into_iter()
        .filter(|v| before.is_none_or(|at| v.open_time < at))
    {
        let current = rows.remove(&bar.open_time);
        // Database may already contain this exact hot revision. Re-merging an
        // identical final Bar must not manufacture a revision increment.
        let value = if current.as_ref() == Some(&bar) {
            bar
        } else {
            merge_for_read(current, bar)
        };
        rows.insert(value.open_time, value);
    }
    rows.into_values().collect()
}

pub async fn chart_page(market:&Market,code:&str,period:Period,before:Option<Timestamp>,page_size:usize)->Result<PeriodPage> {
    if !(1..=10000).contains(&page_size){bail!("page_size must be between 1 and 10000")}
    let d=market.catalog.get(code)?;let source=market.source(&d.instrument.symbol)?;
    let selection=if let Some(before)=before {BarSelection::Before {before_ns:crate::store::ns(before)?,count:page_size+1}}else{BarSelection::Latest {count:page_size+1}};
    let started=std::time::Instant::now();
    let view=market.store.canonical_period_page(CanonicalSnapshotRequest {symbol:d.instrument.symbol.clone(),source_id:source,period:if period==Period::Timeline{"1s"}else{period.as_str()}.into(),selection,final_only:false,expected_version:None},period,Some(schedule(market,code)?)).await?;
    let mut timings=view.page_timings.unwrap_or_default();timings.store_ms=started.elapsed().as_secs_f64()*1000.0;let dto_started=std::time::Instant::now();
    let mut rows=view.bars;let has_more=rows.len()>page_size;if has_more{rows.drain(..rows.len()-page_size);}
    let next_before=rows.first().map(|r|r["open_time"].as_str().context("canonical display label")).transpose()?.map(str::parse).transpose()?;
    let items=rows.into_iter().map(|row|database_bar(row,&d.instrument)).collect::<Result<Vec<_>>>()?;
    timings.page_dto_ms=dto_started.elapsed().as_secs_f64()*1000.0;
    Ok(PeriodPage {period_id:period.as_str().into(),items,next_before,has_more,snapshot_version:view.version,semantics:view.semantics,coverage:view.coverage,timings})
}
pub async fn prepare_live_period(market:&Market,code:&str,period:Period)->Result<Vec<RealtimeBar>> {Ok(chart_page(market,code,period,None,1).await?.items)}
pub fn encode_cursor(
    instrument: &Instrument,
    source: &str,
    period: Period,
    schedule: Option<&MarketSchedule>,
    before: Timestamp,
) -> Result<String> {
    let value = json!({"v":1,"instrument":instrument.symbol,"source":source,"period":period.as_str(),"schedule":schedule_version(schedule)?,"before":isoformat(before)});
    Ok(URL_SAFE_NO_PAD.encode(canonical_json_ascii(&value).as_bytes()))
}
pub fn resolve_boundary(
    cursor: Option<&str>,
    before: Option<i64>,
    instrument: &Instrument,
    source: &str,
    period: Period,
    schedule: Option<&MarketSchedule>,
) -> Result<Option<Timestamp>> {
    if cursor.is_some() && before.is_some() {
        bail!("provide cursor or before, not both")
    }
    let Some(cursor) = cursor else {
        return before
            .map(|value| {
                Utc.timestamp_opt(value, 0)
                    .single()
                    .context("invalid chart page boundary")
            })
            .transpose();
    };
    if cursor.len() > 4096 {
        bail!("invalid chart page cursor")
    }
    let bytes = URL_SAFE_NO_PAD
        .decode(cursor.trim_end_matches('='))
        .context("invalid chart page cursor")?;
    let payload: Value = serde_json::from_slice(&bytes).context("invalid chart page cursor")?;
    if payload["v"] != 1 {
        bail!("invalid chart page cursor")
    }
    if payload["instrument"] != instrument.symbol
        || payload["source"] != source
        || payload["period"] != period.as_str()
        || payload["schedule"] != schedule_version(schedule)?
    {
        bail!("chart cursor belongs to another dataset")
    }
    Ok(Some(
        payload["before"]
            .as_str()
            .context("invalid chart page cursor")?
            .parse()
            .context("invalid chart page cursor")?,
    ))
}
pub fn page_payload(
    page: PeriodPage,
    instrument: &Instrument,
    source: &str,
    period: Period,
    schedule: Option<&MarketSchedule>,
) -> Result<Value> {
    let cursor = page
        .next_before
        .map(|at| encode_cursor(instrument, source, period, schedule, at))
        .transpose()?;
    let local_status = if page.items.is_empty() {
        "empty"
    } else {
        "ready"
    };
    let mut value = serde_json::to_value(page)?;
    value["next_cursor"] = json!(cursor);
    value["local_status"] = json!(local_status);
    Ok(value)
}
