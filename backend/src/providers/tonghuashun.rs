//! Pure THS public-feed normalization; source clocks never become arrival clocks.
use chrono::{Duration, NaiveDate, NaiveDateTime, TimeZone, Utc};
use chrono_tz::{Asia::Shanghai, Tz};
use rust_decimal::RoundingStrategy;
use serde_json::{Value, json};
use std::str::FromStr;
use sha2::{Digest, Sha256};
use tracefang_core::source_clock::{self, V6Label, UnclassifiedSourcePoint};
use tracefang_core::domain::{
    Candle, CoreError, CoreResult, Decimal, Instrument, QuoteSnapshot, SourceMetadata, Timestamp,
};

fn error(message: impl Into<String>) -> CoreError {
    CoreError(message.into())
}
fn text<'a>(value: &'a Value, key: &str) -> CoreResult<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| error(format!("THS field {key} is missing")))
}
fn decimal(value: &str) -> CoreResult<Decimal> {
    Decimal::from_source_str(value.trim())
        .or_else(|_| Decimal::from_scientific(value.trim()))
        .map_err(|_| error("THS decimal is invalid"))
}
fn number(value: &Value) -> CoreResult<Decimal> {
    match value {
        Value::String(s) => decimal(s),
        Value::Number(n) => decimal(&n.to_string()),
        _ => Err(error("THS numeric field is missing")),
    }
}
fn clock(value: &str) -> CoreResult<u32> {
    if value.len() != 4 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error("THS clock must contain four digits"));
    }
    let hour = u32::from_str(&value[..2]).map_err(|_| error("THS hour is invalid"))?;
    let minute = u32::from_str(&value[2..]).map_err(|_| error("THS minute is invalid"))?;
    if hour > 23 || minute > 59 {
        return Err(error("THS clock is out of range"));
    }
    Ok(hour * 60 + minute)
}
fn date(value: &str) -> CoreResult<NaiveDate> {
    if value.len() != 8 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error("THS date must be YYYYMMDD"));
    }
    NaiveDate::parse_from_str(value, "%Y%m%d").map_err(|_| error("THS date is invalid"))
}
fn timestamp(value: &str, zone: Tz) -> CoreResult<Timestamp> {
    if value.len() != 12 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error("THS minute must be YYYYMMDDHHMM"));
    }
    let local = NaiveDateTime::parse_from_str(value, "%Y%m%d%H%M")
        .map_err(|_| error("THS minute timestamp is invalid"))?;
    zone.from_local_datetime(&local)
        .single()
        .map(|v| v.with_timezone(&Utc))
        .ok_or_else(|| error("THS local timestamp is ambiguous or nonexistent"))
}

pub fn decode_jsonp(input: &str) -> CoreResult<Value> {
    let value = input.trim().trim_end_matches(';');
    let open = value
        .find('(')
        .ok_or_else(|| error("THS JSONP wrapper is missing"))?;
    let close = value
        .rfind(')')
        .ok_or_else(|| error("THS JSONP wrapper is missing"))?;
    let callback = value[..open].trim();
    if close <= open
        || !value[close + 1..].trim().is_empty()
        || callback.is_empty()
        || !callback
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'_')
    {
        return Err(error("THS JSONP callback is invalid"));
    }
    let payload: Value = serde_json::from_str(&value[open + 1..close])
        .map_err(|_| error("THS JSONP contains invalid JSON"))?;
    if !payload.is_object() {
        return Err(error("THS JSONP root must be an object"));
    }
    Ok(payload)
}

pub fn parse_quote(
    payload: &Value,
    instrument: &Instrument,
    provider_code: &str,
    expected_name: &str,
    calendar_mode: &str,
    received_at: Timestamp,
) -> CoreResult<QuoteSnapshot> {
    let node = payload
        .get(provider_code)
        .filter(|v| v.is_object())
        .ok_or_else(|| error("THS returned a different symbol"))?;
    if text(node, "name")? != expected_name {
        return Err(error("THS returned a different name"));
    }
    let trade_date = text(node, "date")?;
    let trade_day = date(trade_date)?;
    let dates = node
        .get("dates")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| error("THS session dates are missing"))?
        .iter()
        .map(|v| {
            v.as_str()
                .ok_or_else(|| error("THS session date is invalid"))
                .and_then(date)
        })
        .collect::<CoreResult<Vec<_>>>()?;
    let sessions = node
        .get("tradeTime")
        .and_then(Value::as_array)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| error("THS sessions are missing"))?;
    let mut cross_midnight = None;
    for session in sessions {
        let value = session
            .as_str()
            .ok_or_else(|| error("THS session must be text"))?;
        let (start, end) = value
            .split_once('-')
            .ok_or_else(|| error("THS session is invalid"))?;
        let (start, end) = (clock(start)?, clock(end)?);
        if start > end && cross_midnight.is_none() {
            cross_midnight = Some((start, end));
        }
    }
    let mut last = None;
    for row in text(node, "data")?.split(';') {
        let fields: Vec<_> = row.split(',').collect();
        if fields.len() < 5 {
            return Err(error("THS quote row is truncated"));
        }
        let minutes = clock(fields[0])?;
        let price = decimal(fields[1])?;
        last = Some((fields[0], minutes, price));
    }
    let (stamp, minutes, last) = last.ok_or_else(|| error("THS quote has no price"))?;
    let observed_day = match calendar_mode {
        "session_dates" => match cross_midnight {
            Some((start, _)) if minutes >= start => dates[0],
            Some((_, end)) if minutes <= end => *dates.get(1).unwrap_or(&trade_day),
            _ => *dates.last().expect("nonempty dates checked"),
        },
        "trade_date" => {
            if cross_midnight.is_some_and(|(_, end)| minutes <= end) {
                trade_day
                    .checked_add_signed(Duration::days(1))
                    .ok_or_else(|| error("THS date overflow"))?
            } else {
                trade_day
            }
        }
        _ => return Err(error("THS calendar mode is invalid")),
    };
    let observed_at = timestamp(
        &format!("{}{stamp}", observed_day.format("%Y%m%d")),
        Shanghai,
    )?;
    let previous = number(&node["pre"])?;
    let change = &last - &previous;
    // Preserve valid signed prices. The public endpoint does not declare a percentage policy for nonpositive settlement.
    let percent = if previous>Decimal::ZERO {
        Some((change.clone()*Decimal::from(100)).div_to_scale(&previous,2).ok_or_else(||error("THS percent cannot be calculated"))?)
    }else {None};
    let quote = QuoteSnapshot {
        instrument: instrument.clone(),
        last,
        open: None,
        high: None,
        low: None,
        volume: None,
        change: Some(change),
        change_percent: percent,
        source: SourceMetadata {
            provider: "tonghuashun_futures".into(),
            provider_symbol: provider_code.into(),
            observed_at,
            received_at,
            raw_payload: Some(json!({"channel":"tonghuashun_public_time_v6",
                "observation_kind":"snapshot", "frame_channel":"tonghuashun_futures_live",
                "response_kind":"time", "name":expected_name, "trade_date":trade_date,
                "wire_observed_at":observed_at.to_rfc3339(), "wire_time_precision":"minute",
                "bar_clock":"source.observed_at", "timestamp_precision_seconds":60,
                "previous_settlement":previous.to_string(), "percentage_reference":"previous_settlement",
                "percentage_unavailable_reason":if previous<=Decimal::ZERO {Some("nonpositive settlement percentage policy unavailable")}else{None}, "daily_stats_available":false})),
        },
    };
    quote.validate()?;
    Ok(quote)
}

pub fn validate_daily(payload:&Value,expected_name:&str)->CoreResult<()> {
    if text(payload,"name")?!=expected_name{return Err(error("THS daily name differs"))}
    for row in text(payload,"data")?.split(';') {
        let fields=row.split(',').collect::<Vec<_>>();
        if fields.len()<7{return Err(error("THS daily row is truncated"))}
        date(fields[0])?;
        let values=fields[1..6].iter().map(|value|decimal(value)).collect::<CoreResult<Vec<_>>>()?;
        let [open,high,low,close,volume]:[Decimal;5]=values.try_into().map_err(|_|error("THS daily width is invalid"))?;
        if low>high||open<low||open>high||close<low||close>high||volume<Decimal::ZERO {
            return Err(error("THS daily range is invalid"))
        }
    }
    Ok(())
}

pub fn enrich_daily(
    mut quote: QuoteSnapshot,
    payload: &Value,
    expected_name: &str,
) -> CoreResult<QuoteSnapshot> {
    validate_daily(payload,expected_name)?;
    if text(payload, "name")? != expected_name {
        return Err(error("THS daily name differs"));
    }
    let trade_date = quote
        .source
        .raw("trade_date")
        .and_then(Value::as_str)
        .ok_or_else(|| error("THS quote has no trade date"))?
        .to_owned();
    for row in text(payload, "data")?.split(';').rev() {
        let fields: Vec<_> = row.split(',').collect();
        if fields.len() < 7 {
            return Err(error("THS daily row is truncated"));
        }
        if fields[0] != trade_date {
            continue;
        }
        let values = fields[1..6]
            .iter()
            .map(|v| decimal(v))
            .collect::<CoreResult<Vec<_>>>()?;
        let [open,high,low,close,volume]:[Decimal;5]=values.try_into().map_err(|_|error("THS daily width is invalid"))?;
        if low > high
            || open < low
            || open > high
            || close < low
            || close > high
            || volume < Decimal::ZERO
        {
            return Err(error("THS daily range is invalid"));
        }
        quote.open = Some(open);
        if low <= quote.last && quote.last <= high {
            quote.high = Some(high);
            quote.low = Some(low);
        }
        quote.volume = Some(volume);
        if let Some(raw) = quote.source.raw_payload.as_mut() {
            raw["daily_stats_available"] = json!(true);
        }
        quote.validate()?;
        break;
    }
    Ok(quote)
}

const MAX_MINUTE_OUTPUT_BYTES:usize=64*1024*1024;
struct OutputBytes(usize);
impl std::io::Write for OutputBytes {
    fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{self.0=self.0.checked_add(bytes.len()).filter(|v|*v<=MAX_MINUTE_OUTPUT_BYTES).ok_or_else(||std::io::Error::other("THS decoded output exceeds byte budget"))?;Ok(bytes.len())}
    fn flush(&mut self)->std::io::Result<()>{Ok(())}
}
pub fn parse_minutes(
    payload: &Value,
    instrument: &Instrument,
    provider_code: &str,
    expected_name: &str,
    time_zone: Tz,
    received_at: Timestamp,
) -> CoreResult<Vec<Candle>> {
    if payload.get("name").is_some() && text(payload, "name")? != expected_name {
        return Err(error("THS minute name differs"));
    }
    let mut result: Vec<Candle> = Vec::new();
    let mut previous_label=None;let mut output=OutputBytes(0);
    let reviewed=source_clock::verified_v6_scope(provider_code,instrument);
    let mut point_count=0u64;let mut unresolved_count=0u64;let mut nonflat_count=0u64;let mut point_first=None;let mut point_last=None;let mut point_hash=Sha256::new();
    let data=text(payload,"data")?;
    if data.split(';').count()>100_000 {return Err(error("THS minute frame exceeds bounded rows"));}
    for (row_index,row) in data.split(';').enumerate() {
        let fields: Vec<_> = row.split(',').collect();
        if fields.len() < 7 {
            return Err(error("THS minute row is truncated"));
        }
        let label = timestamp(fields[0], time_zone)?;
        if previous_label.is_some_and(|at|at>=label) {return Err(error("THS minute rows are not strictly ordered"));}
        previous_label=Some(label);
        let mut open_time=label;
        let prices = fields[1..5]
            .iter()
            .map(|v| decimal(v))
            .collect::<CoreResult<Vec<_>>>()?;
        let volume = if fields[5].trim().is_empty() {
            None
        } else {
            Some(decimal(fields[5])?)
        };
        let mut metadata=json!({"channel":"tonghuashun_public_line_v6","protocol":"tonghuashun_public_line_v6","source_period":"61","interval_seconds":60,
            "time_zone":time_zone.name(),"observation_kind":"history","source_label":fields[0],"source_label_ns":label.timestamp_nanos_opt().map(|v|v.to_string()),
            "publication_time_unknown":true});
        if reviewed {
            match source_clock::classify_v6_label(provider_code,instrument,label)? {
                V6Label::Regular{canonical_open,interval_end}=>{
                    open_time=canonical_open;metadata["minute_clock_policy"]=json!(source_clock::THS_V6_SHFE_END_V2);metadata["clock_policy_verified"]=json!(true);
                    metadata["source_label_semantics"]=json!("interval_end");metadata["canonical_interval_semantics"]=json!("[open,end)");
                    metadata["source_interval_end"]=json!(interval_end);metadata["source_interval_end_ns"]=json!(interval_end.timestamp_nanos_opt().map(|v|v.to_string()));
                    metadata["bar_state"]=json!("provisional_authoritative");
                },
                V6Label::SourcePoint{reason}=>{
                    let flat=prices.iter().all(|v|v==&prices[0]);
                    let unproved_opening=flat && reason=="session_start_source_point_aggregation_unverified";
                    let point=UnclassifiedSourcePoint{provider_code:provider_code.into(),source_label:fields[0].into(),source_label_ns:label.timestamp_nanos_opt().ok_or_else(||error("THS point timestamp exceeds ns range"))?,source_row_index:row_index as u64,
                        source_row_sha256:hex::encode(Sha256::digest(row.as_bytes())),policy:source_clock::THS_V6_SHFE_END_V2.into(),reason:if flat{reason.into()}else{format!("{reason}; nonflat_source_point_unresolved")}};
                    point_hash.update(serde_json::to_vec(&point).map_err(|_|error("cannot encode THS point witness"))?);point_hash.update(b"\n");point_count+=1;unresolved_count+=u64::from(!unproved_opening);nonflat_count+=u64::from(!flat);
                    point_first=Some(point_first.map_or(label,|v:Timestamp|v.min(label)));point_last=Some(point_last.map_or(label,|v:Timestamp|v.max(label)));
                    // Validate original point facts before quarantining them.
                    Candle{instrument:instrument.clone(),interval_seconds:60,open_time:label,open:prices[0].clone(),high:prices[1].clone(),low:prices[2].clone(),close:prices[3].clone(),volume:volume.clone(),source:SourceMetadata{provider:"tonghuashun_futures".into(),provider_symbol:provider_code.into(),observed_at:label,received_at,raw_payload:None}}.validate()?;
                    continue;
                }
            }
        }else{
            metadata["minute_clock_policy"]=json!(source_clock::LEGACY_V6_OPEN_V1);metadata["source_label_semantics"]=json!("unverified_legacy_label_as_open");
            metadata["clock_policy_verified"]=json!(false);
        }
        let candle = Candle {
            instrument: instrument.clone(),
            interval_seconds: 60,
            open_time,
            open: prices[0].clone(),
            high: prices[1].clone(),
            low: prices[2].clone(),
            close: prices[3].clone(),
            volume,
            source: SourceMetadata {
                provider: "tonghuashun_futures".into(),
                provider_symbol: provider_code.into(),
                observed_at: label,
                received_at,
                raw_payload: Some(metadata),
            },
        };
        candle.validate()?;
        serde_json::to_writer(&mut output,&candle).map_err(|_|error("THS decoded output exceeds byte budget"))?;
        // Reserve bounded quarantine summary/capture lineage before retaining it.
        std::io::Write::write_all(&mut output,&[0u8;1024]).map_err(|_|error("THS decoded output exceeds byte budget"))?;
        result.push(candle);
    }
    if point_count>0 {
        let summary=json!({"policy":source_clock::THS_V6_SHFE_END_V2,"count":point_count.to_string(),"unresolved_nonflat_count":nonflat_count.to_string(),"unresolved_point_count":unresolved_count.to_string(),"first_source_label":point_first,"last_source_label":point_last,
            "points_sha256":hex::encode(point_hash.finalize()),"hash_policy":"typed-unclassified-source-point-json-newline-parser-order-v1","raw_reference":"original provider body and row offsets remain in capture/archive","coverage_complete":false,"reason":"source-point aggregation is unverified; points are not canonical minutes"});
        if result.is_empty() || unresolved_count>0{return Err(error(format!("THS unresolved source points retained in capture: {summary}")));}
        for bar in &mut result{bar.source.raw_payload.as_mut().unwrap()["source_point_quarantine_ref"]=summary.clone();}
    }
    Ok(result)
}

pub async fn fetch_jsonp(client: &reqwest::Client, url: &str) -> CoreResult<Value> {
    let response = client
        .get(url)
        .timeout(std::time::Duration::from_secs(12))
        .header("Referer", "https://q.10jqka.com.cn/")
        .send()
        .await
        .map_err(|e| error(format!("THS transport: {e}")))?
        .error_for_status()
        .map_err(|e| error(format!("THS response: {e}")))?;
    let text = response
        .text()
        .await
        .map_err(|e| error(format!("THS response body: {e}")))?;
    decode_jsonp(&text)
}

pub async fn fetch_quote(
    client: &reqwest::Client,
    base_url: &str,
    instrument: &Instrument,
    provider_code: &str,
    expected_name: &str,
    calendar_mode: &str,
) -> CoreResult<QuoteSnapshot> {
    let base = base_url.trim_end_matches('/');
    let payload = fetch_jsonp(client, &format!("{base}/v6/time/{provider_code}/last.js")).await?;
    let quote = parse_quote(
        &payload,
        instrument,
        provider_code,
        expected_name,
        calendar_mode,
        Utc::now(),
    )?;
    match fetch_jsonp(
        client,
        &format!("{base}/v6/line/{provider_code}/01/last.js"),
    )
    .await
    {
        Ok(daily) => enrich_daily(quote.clone(), &daily, expected_name).or(Ok(quote)),
        Err(_) => Ok(quote),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tracefang_core::domain::AssetClass;
    fn instrument() -> Instrument {
        Instrument {
            symbol: "AU2610".into(),
            asset_class: AssetClass::Future,
            base: Some("AU".into()),
            quote: Some("CNY".into()),
            venue: Some("SHFE".into()),
        }
    }
    #[test]
    fn quotes_use_source_session_clock_and_daily_does_not_change_it() {
        let payload = json!({"qh_au2610":{"name":"沪金2610","date":"20261008",
            "dates":["20260930","20261001","20261008"],"tradeTime":["2100-0230","0900-1500"],
            "pre":"895.20","data":"0230,908.34,0,0,0"}});
        let received = "2026-10-02T13:00:00Z".parse().unwrap();
        let quote = parse_quote(
            &payload,
            &instrument(),
            "qh_au2610",
            "沪金2610",
            "session_dates",
            received,
        )
        .unwrap();
        assert_eq!(
            quote.source.observed_at.to_rfc3339(),
            "2026-09-30T18:30:00+00:00"
        );
        assert_eq!(quote.change, Some(decimal("13.14").unwrap()));
        assert_eq!(quote.change_percent, Some(decimal("1.47").unwrap()));
        let daily = json!({"name":"沪金2610","data":"20261008,900,910,899,908,0,0"});
        let enriched = enrich_daily(quote.clone(), &daily, "沪金2610").unwrap();
        assert_eq!(enriched.volume, Some(Decimal::ZERO));
        assert_eq!(enriched.source.observed_at, quote.source.observed_at);
    }
    #[test]
    fn lines_validate_symbol_time_range_order_and_missing_volume() {
        let received = Utc::now();
        let payload = decode_jsonp(
            "history({\"name\":\"沪金2610\",\"data\":\"202609301500,900,910,890,905,,0\"});",
        )
        .unwrap();
        let bars = parse_minutes(
            &payload,
            &instrument(),
            "qh_au2610",
            "沪金2610",
            Shanghai,
            received,
        )
        .unwrap();
        assert_eq!(bars[0].volume, None);
        assert_eq!(bars[0].open_time.to_rfc3339(), "2026-09-30T06:59:00+00:00");
        assert!(
            parse_minutes(
                &payload,
                &instrument(),
                "qh_au2610",
                "Other",
                Shanghai,
                received
            )
            .is_err()
        );
        assert!(decode_jsonp("f({});alert(1)").is_err());
        let bad = json!({"data":"202609301500,900,910,890,920,1,0"});
        assert!(
            parse_minutes(
                &bad,
                &instrument(),
                "qh_au2610",
                "沪金2610",
                Shanghai,
                received
            )
            .is_err()
        );
    }
    #[test]
    fn reviewed_end_labels_preserve_evidence_clocks_and_future_tail_stays_provisional() {
        use tracefang_core::{reducer::{BarReducer,BarContract,SeriesKey},events::{MarketEvent,BarState}};
        let received="2026-09-30T06:06:07Z";
        let received:Timestamp=received.parse().unwrap();
        let payload=json!({"data":"202609301406,900,901,899,900,0,0;202609301407,900,901,899,901,,0;202609301408,901,902,900,901,1,0"});
        let input=parse_minutes(&payload,&instrument(),"qh_au2610","沪金2610",Shanghai,received).unwrap();
        assert_eq!(input[0].open_time,"2026-09-30T06:05:00Z".parse::<Timestamp>().unwrap());
        assert_eq!(input[1].open_time,"2026-09-30T06:06:00Z".parse::<Timestamp>().unwrap());
        assert_eq!(input[1].source.observed_at,"2026-09-30T06:07:00Z".parse::<Timestamp>().unwrap());
        assert_eq!(input[1].source.received_at,received);
        let mut reducer=BarReducer::new(vec![BarContract::new("ths","tonghuashun_futures",vec!["tonghuashun_futures".into()])]).unwrap();
        for candle in input {let event=reducer.normalize_bar(candle).unwrap().unwrap();reducer.apply(MarketEvent::Bar(event)).unwrap();}
        let key=SeriesKey{source_id:"ths".into(),instrument:instrument(),interval_seconds:60};
        let bars=reducer.latest(&key,10);assert_eq!(bars.len(),3);
        assert_eq!(bars[0].state,BarState::Final);assert_eq!(bars[0].finalized_at,Some(received));
        for bar in &bars[1..]{assert_eq!(bar.state,BarState::ProvisionalAuthoritative);assert_eq!(bar.finalized_at,None);}
        assert_eq!(bars[0].source.raw("minute_clock_policy"),Some(&json!(source_clock::THS_V6_SHFE_END_V2)));
        assert_eq!(bars[0].source.raw("source_label"),Some(&json!("202609301406")));
        // A historical out-of-order row below a future watermark is still not
        // confirmed before its own actual interval end.
        let candle=parse_minutes(&json!({"data":"202609301407,900,901,899,900,0,0"}),&instrument(),"qh_au2610","沪金2610",Shanghai,received).unwrap().remove(0);
        reducer.apply(MarketEvent::Bar(reducer.normalize_bar(candle).unwrap().unwrap())).unwrap();
        assert_eq!(reducer.latest(&key,10)[1].state,BarState::ProvisionalAuthoritative);
    }
    #[test]
    fn unclassified_start_points_are_bounded_references_and_nonflat_is_unresolved() {
        let received="2026-09-30T07:01:00Z".parse().unwrap();
        let points=json!({"data":"202609300900,900,900,900,900,20,0;202609300901,900,901,899,901,1,0"});
        let bars=parse_minutes(&points,&instrument(),"qh_au2610","沪金2610",Shanghai,received).unwrap();
        assert_eq!(bars.len(),1);assert_eq!(bars[0].volume,Some(Decimal::ONE));
        let evidence=bars[0].source.raw("source_point_quarantine_ref").unwrap();
        assert_eq!(evidence["count"],"1");assert_eq!(evidence["coverage_complete"],false);assert_eq!(evidence["unresolved_nonflat_count"],"0");
        assert!(serde_json::to_vec(evidence).unwrap().len()<1024);
        assert!(parse_minutes(&json!({"data":"202609300900,900,901,899,900,20,0;202609300901,900,901,899,901,1,0"}),&instrument(),"qh_au2610","沪金2610",Shanghai,received).unwrap_err().0.contains("unresolved"));
        assert!(parse_minutes(&json!({"data":"202609301200,900,900,900,900,20,0;202609301406,900,901,899,900,0,0"}),&instrument(),"qh_au2610","沪金2610",Shanghai,received).unwrap_err().0.contains("unresolved"));
        let legacy=parse_minutes(&json!({"data":"202609301406,900,901,899,900,0,0"}),&instrument(),"qh_other","沪金2610",Shanghai,received).unwrap();
        assert_eq!(legacy[0].open_time,legacy[0].source.observed_at);assert_eq!(legacy[0].source.raw("clock_policy_verified"),Some(&json!(false)));
    }
}
