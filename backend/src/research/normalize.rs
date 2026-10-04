use super::{Query, ResearchError, Result};
use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveDateTime, TimeZone, Utc};
use chrono_tz::{America::New_York, Asia::Shanghai, Tz};
use serde_json::{Value, json};
use std::{collections::BTreeMap, str::FromStr};
use tracefang_core::domain::Decimal;

pub fn decimal(value: &Value) -> Option<Decimal> {
    let text = match value {
        Value::String(s) => s.to_owned(),
        Value::Number(n) => n.to_string(),
        _ => return None,
    };
    Decimal::from_str_exact(&text)
        .or_else(|_| Decimal::from_scientific(&text))
        .ok()
}
pub fn numeric(value:Option<Decimal>)->Value {value.map(|v|Value::String(v.to_string())).unwrap_or(Value::Null)}
pub fn parse_time(value: &str) -> Result<DateTime<Utc>> {
    if let Ok(stamp) = DateTime::parse_from_rfc3339(value) {
        return Ok(stamp.with_timezone(&Utc));
    }
    let local = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .or_else(|_| {
            NaiveDate::parse_from_str(value, "%Y-%m-%d").map(|d| d.and_hms_opt(0, 0, 0).unwrap())
        })
        .map_err(|_| ResearchError::invalid("行情时间格式错误。"))?;
    Shanghai
        .from_local_datetime(&local)
        .single()
        .map(|v| v.with_timezone(&Utc))
        .ok_or_else(|| ResearchError::invalid("行情时间无效。"))
}
fn day_start(date: NaiveDate, zone: Tz) -> DateTime<Utc> {
    zone.from_local_datetime(&date.and_hms_opt(0, 0, 0).unwrap())
        .single()
        .unwrap()
        .with_timezone(&Utc)
}
pub fn history_end(query: &Query, now: DateTime<Utc>) -> DateTime<Utc> {
    let Some(before) = query.before else {
        return now;
    };
    let mut date = before.with_timezone(&Shanghai).date_naive();
    if query.period == "1M" {
        date = date.with_day(1).unwrap();
    }
    if query.period == "1w" {
        date -= Duration::days(i64::from(date.weekday().num_days_from_monday()));
    }
    if query.period == "1M" || query.period == "1w" {
        day_start(date, Shanghai) - Duration::seconds(1)
    } else {
        before - Duration::seconds(1)
    }
}
pub fn interval(period: &str) -> Option<i64> {
    Some(match period {
        "1m" => 60,
        "5m" => 300,
        "15m" => 900,
        "30m" => 1800,
        "1h" => 3600,
        "1d" => 86400,
        "1w" => 604800,
        "1M" => 2678400,
        _ => return None,
    })
}
pub fn period_end(stamp:DateTime<Utc>,query:&Query)->Option<DateTime<Utc>>{
    if query.period.ends_with('m')||query.period.ends_with('h'){return stamp.checked_add_signed(Duration::seconds(interval(&query.period)?));}
    let zone=if query.source=="alpaca"{New_York}else{Shanghai};let local=stamp.with_timezone(&zone).date_naive();
    let next=match query.period.as_str(){"1M"=>NaiveDate::from_ymd_opt(local.year()+i32::from(local.month()==12),local.month()%12+1,1),"1w"=>local.checked_add_signed(Duration::days(7-i64::from(local.weekday().num_days_from_monday()))),_=>local.checked_add_signed(Duration::days(1))};
    next.map(|date|day_start(date,zone))
}
pub fn period_closed(stamp:DateTime<Utc>,query:&Query,now:DateTime<Utc>)->bool{period_end(stamp,query).is_some_and(|end|end<=now)}
pub struct NormalizedBars { pub bars:Vec<Value>, pub rejected:usize, pub deduplicated:usize, pub conflicts:Vec<Value> }
/// Source daily/week/month labels are retained. Calendar ends are explicit
/// interpretation boundaries; they are not asserted source publication clocks.
pub fn time_policy(query:&Query)->Value { json!({"label_role":"source_period_label", "source_label_precision":if query.period.ends_with('m')||query.period.ends_with('h'){"adapter_timestamp"}else{"calendar_date"},"calendar_timezone":if query.source=="alpaca"{"America/New_York"}else{"Asia/Shanghai"},"bucket_end_policy":"next local calendar boundary", "week_month_label_semantics":if query.period=="1w"||query.period=="1M"{"upstream start/end label convention not verified; original label retained"}else{"source date/time retained"},"observed_at_role":"period label; not a measured source publication clock"}) }
pub fn bars(rows: &[Value], query: &Query, now: DateTime<Utc>) -> NormalizedBars {
    let mut normalized = BTreeMap::new();
    let mut rejected = 0;
    let mut deduplicated=0; let mut conflicts=vec![];
    for row in rows {
        let item = (|| -> Result<Option<(DateTime<Utc>, Value)>> {
            let stamp = parse_time(
                row["time"]
                    .as_str()
                    .ok_or_else(|| ResearchError::invalid("行情时间缺失。"))?,
            )?;
            if stamp > now || query.before.is_some_and(|before| stamp >= before) {
                return Ok(None);
            }
            let values = ["open", "high", "low", "close"].map(|key| decimal(&row[key]));
            let [Some(open), Some(high), Some(low), Some(close)] = values else {
                return Err(ResearchError::invalid("价格缺失。"));
            };
            if &low > (&open).min(&close)
                || &high < (&open).max(&close)
                || low > high
                || (query.asset != "future" && low < Decimal::ZERO)
            {
                return Err(ResearchError::invalid("OHLC 价格范围错误。"));
            }
            let volume = if row["volume"].is_null() {
                None
            } else {
                Some(
                    decimal(&row["volume"])
                        .ok_or_else(|| ResearchError::invalid("成交量格式错误。"))?,
                )
            };
            if volume.as_ref().is_some_and(|v| v < &Decimal::ZERO) {
                return Err(ResearchError::invalid("成交量为负。"));
            }
            let interest = decimal(&row["open_interest"]).filter(|v| *v >= Decimal::ZERO);
            let iso = stamp.to_rfc3339();
            let label_only = row["source_payload"]["span_start_unknown"] == true;
            let mut value = json!({"instrument":{"symbol":query.symbol,"asset_class":query.asset,"base":null,
                    "quote":if query.source=="alpaca"{"USD"}else{"CNY"},"venue":null},
                "open_time":iso,"interval":interval(&query.period),"open":numeric(Some(open)),"high":numeric(Some(high)),
                "low":numeric(Some(low)),"close":numeric(Some(close)),"volume":numeric(volume),
                "source":{"provider":query.source,"provider_symbol":query.symbol,"observed_at":iso,"received_at":now},
                "evidence_channel_id":query.source,"state":if !label_only && period_closed(stamp,query,now){"final"}else{"provisional_authoritative"},
                "revision":"1","finalized_at":null,"bucket_end":if label_only{None}else{period_end(stamp,query)}});
            if query.source == "akshare" {
                value["open_interest"] = numeric(interest);
                if row["source_payload"].is_object() { value["source"]["raw_payload"] = row["source_payload"].clone(); }
            }
            Ok(Some((stamp, value)))
        })();
        match item {
            Ok(Some((stamp, item))) => {
                if let Some(previous)=normalized.get(&stamp) {
                    if previous==&item {deduplicated+=1;} else { conflicts.push(json!({"period_label":stamp,"first_sha256":crate::analysis::quant::content_hash(previous).unwrap(),"conflicting_sha256":crate::analysis::quant::content_hash(&item).unwrap(),"rule":"no source revision/order proof; authority publication rejected"})); }
                } else {normalized.insert(stamp, item);}
            }
            Ok(None) => {}
            Err(_) => rejected += 1,
        }
    }
    NormalizedBars {bars:normalized.into_values().collect(),rejected,deduplicated,conflicts}
}
fn sum(values: impl IntoIterator<Item = Decimal>) -> Option<Decimal> {
    values
        .into_iter()
        .try_fold(Decimal::ZERO, Decimal::checked_add)
}
pub fn technical_evidence(bars: &[Value]) -> Value {
    let closed: Vec<_> = bars.iter().filter(|r| r["state"] == "final").collect();
    let closes: Vec<_> = closed.iter().filter_map(|r| decimal(&r["close"])).collect();
    let mut evidence = json!({"closed_bars":closed.len(),"excluded_open_bars":bars.len()-closed.len(),
        "open_interest_last":closed.last().map(|r|r["open_interest"].clone()),"range_20":null,
        "true_range_14_mean":null,"return_20_percent":null});
    for length in [20, 60, 120] {
        let average = if closes.len() >= length {
            sum(closes[closes.len() - length..].iter().cloned())
                .and_then(|v| v.checked_div(Decimal::from(length as u64)))
        } else {
            None
        };
        evidence[format!("sma_{length}")] = numeric(average);
    }
    if closes.len() >= 21 && closes[closes.len() - 21] > Decimal::ZERO {
        evidence["return_20_percent"] = numeric(
            closes
                .last()
                .and_then(|v| v.clone().checked_div(closes[closes.len() - 21].clone()))
                .and_then(|v| v.checked_sub(Decimal::ONE))
                .and_then(|v| v.checked_mul(Decimal::from(100))),
        );
    }
    if closed.len() >= 20 {
        let tail = &closed[closed.len() - 20..];
        evidence["range_20"] = json!({"low":numeric(tail.iter().filter_map(|r|decimal(&r["low"])).min()),
            "high":numeric(tail.iter().filter_map(|r|decimal(&r["high"])).max())});
    }
    if closed.len() >= 15 {
        let ranges: Option<Vec<_>> = closed[closed.len() - 15..]
            .windows(2)
            .map(|pair| {
                let prev = decimal(&pair[0]["close"])?;
                let hi = decimal(&pair[1]["high"])?;
                let lo = decimal(&pair[1]["low"])?;
                Some(
                    hi.clone().checked_sub(lo.clone())?
                        .max(hi.checked_sub(prev.clone())?.abs())
                        .max(lo.checked_sub(prev)?.abs()),
                )
            })
            .collect();
        evidence["true_range_14_mean"] = numeric(
            ranges
                .and_then(sum)
                .and_then(|v| v.checked_div(Decimal::from(14))),
        );
    }
    evidence
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn exact_ohlc_missing_volume_calendar_and_cursor_survive_normalization() {
        let query = Query {
            source: "akshare".into(),
            symbol: "AU0".into(),
            asset: "future".into(),
            period: "1M".into(),
            ..Query::default()
        };
        let rows = vec![
            json!({"time":"2026-09-30","open":"0.123456789123456789","high":"1","low":"0.1","close":"0.5","volume":null}),
            json!({"time":"2026-09-29","open":1,"high":2,"low":3,"close":1}),
        ];
        let normalized = bars(&rows, &query, "2026-10-02T01:00:00Z".parse().unwrap());
        let bars=normalized.bars; let rejected=normalized.rejected;
        assert_eq!(rejected, 1);
        assert_eq!(bars[0]["open"], "0.123456789123456789");
        assert_eq!(bars[0]["state"], "final");
        assert!(bars[0]["volume"].is_null());
        let query = Query {
            before: Some("2026-09-29T16:00:00Z".parse().unwrap()),
            ..query
        };
        assert_eq!(
            history_end(&query, Utc::now()).to_rfc3339(),
            "2026-08-31T15:59:59+00:00"
        );
    }
}

pub fn quant_bars(rows:&[Value])->anyhow::Result<Vec<crate::analysis::quant::QuantBar>>{
 use crate::analysis::{exact::{parse,d},quant::QuantBar};
 rows.iter().map(|row|{let stamp=|value:&Value|->anyhow::Result<DateTime<Utc>>{Ok(value.as_str().ok_or_else(||anyhow::anyhow!("research timestamp missing"))?.parse()?)};let volume=row["volume"].as_str().map(parse).transpose()?;Ok(QuantBar{open_time:stamp(&row["open_time"])?,bucket_end:stamp(&row["bucket_end"])?,open:parse(row["open"].as_str().ok_or_else(||anyhow::anyhow!("exact open missing"))?)?,high:parse(row["high"].as_str().ok_or_else(||anyhow::anyhow!("exact high missing"))?)?,low:parse(row["low"].as_str().ok_or_else(||anyhow::anyhow!("exact low missing"))?)?,close:parse(row["close"].as_str().ok_or_else(||anyhow::anyhow!("exact close missing"))?)?,known_volume_sum:volume.clone().unwrap_or_else(||d("0")),known_volume_count:u64::from(volume.is_some()),component_count:1,volume,state:match row["state"].as_str(){Some("final")=>"final",Some("forming"|"provisional"|"provisional_authoritative")=>"provisional",other=>return Err(anyhow::anyhow!("unknown research finality state: {other:?}"))}.into(),revision:1,observed_at:stamp(&row["source"]["observed_at"])?,received_at:stamp(&row["source"]["received_at"])?,accepted_at:None,finalized_at:None,applied_frame_seq:None,source_precision_ns:None,source_volume_components:None,source_volume_component_groups:Vec::new()})}).collect()
}

#[cfg(test)] mod authority_contract_tests {
 use super::*;
 #[test] fn duplicates_require_exact_equality_and_conflicts_are_retained(){
  let query=Query{source:"akshare".into(),symbol:"AU0".into(),asset:"future".into(),period:"1d".into(),..Query::default()}; let at="2026-10-02T00:00:00Z".parse().unwrap();
  let row=json!({"time":"2026-09-29","open":"10.0","high":"12","low":"9","close":"11","volume":null}); let mut conflict=row.clone();conflict["close"]=json!("10");
  let identical=bars(&[row.clone(),row.clone()],&query,at);assert_eq!(identical.bars.len(),1);assert_eq!(identical.deduplicated,1);assert!(identical.conflicts.is_empty());
  let forward=bars(&[row.clone(),conflict.clone()],&query,at);let reverse=bars(&[conflict,row],&query,at);assert_eq!(forward.conflicts.len(),1);assert_eq!(reverse.conflicts.len(),1);assert_eq!(forward.conflicts[0]["period_label"],reverse.conflicts[0]["period_label"]);
 }
 #[test] fn research_current_bucket_remains_preview_and_is_valid_exact_input(){
  let query=Query{source:"akshare".into(),symbol:"AU0".into(),asset:"future".into(),period:"1d".into(),..Query::default()};let at="2026-10-02T06:00:00Z".parse().unwrap();let normalized=bars(&[json!({"time":"2026-10-02","open":"10","high":"12","low":"9","close":"11","volume":null})],&query,at);assert_eq!(normalized.bars[0]["state"],"provisional_authoritative");let exact=quant_bars(&normalized.bars).unwrap();assert_eq!(exact[0].state,"provisional");assert!(exact[0].finalized_at.is_none());exact[0].validate().unwrap();
 }
 #[test] fn calendar_labels_keep_source_convention_and_leap_boundaries(){
  let mut query=Query{source:"eastmoney".into(),period:"1M".into(),..Query::default()};
  assert_eq!(period_end(parse_time("2024-02-29").unwrap(),&query).unwrap().to_rfc3339(),"2024-02-29T16:00:00+00:00");
  assert_eq!(period_end(parse_time("2024-02-01").unwrap(),&query).unwrap().to_rfc3339(),"2024-02-29T16:00:00+00:00");
  assert!(time_policy(&query)["week_month_label_semantics"].as_str().unwrap().contains("not verified"));
  query.period="1w".into(); assert_eq!(period_end(parse_time("2024-03-01").unwrap(),&query).unwrap().to_rfc3339(),"2024-03-03T16:00:00+00:00");
  query.source="alpaca".into();query.period="1d".into();assert_eq!(period_end("2024-03-10T05:00:00Z".parse().unwrap(),&query).unwrap().to_rfc3339(),"2024-03-11T04:00:00+00:00");
 }
}
