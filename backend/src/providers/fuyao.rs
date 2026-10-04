//! Official Fuyao SDK snapshot/minute protocol, with exact source field text.
//! Snapshot observations are not represented as exchange tick coverage.
use std::collections::BTreeMap;
use chrono::{DateTime,Duration,Utc,NaiveDate};
use serde_json::{Value,json};
use tracefang_core::domain::{Candle,CoreError,CoreResult,Decimal,Instrument,QuoteSnapshot,SourceMetadata,Timestamp};
use crate::catalog::PublicFeed;
use sha2::{Digest,Sha256};

const MAX_DECODED_OUTPUT_BYTES:usize=64*1024*1024;
struct OutputBytes {written:usize,limit:usize}
impl std::io::Write for OutputBytes {
    fn write(&mut self,data:&[u8])->std::io::Result<usize>{self.written=self.written.checked_add(data.len()).filter(|v|*v<=self.limit).ok_or_else(||std::io::Error::other("Fuyao decoded output exceeds byte budget"))?;Ok(data.len())}
    fn flush(&mut self)->std::io::Result<()>{Ok(())}
}
#[derive(Default)]struct Quarantine {count:u64,first:Option<i64>,last:Option<i64>,hash:Sha256}
impl Quarantine {
    fn record(&mut self,label:i64,row:Value,reason:&str)->CoreResult<()> {
        let point=json!({"source_label_ms":label.to_string(),"source_row":row,"reason":reason});
        self.hash.update(serde_json::to_vec(&point).map_err(|_|error("cannot hash quarantined source point"))?);self.hash.update(b"\n");
        self.count+=1;self.first=Some(self.first.map_or(label,|v|v.min(label)));self.last=Some(self.last.map_or(label,|v|v.max(label)));Ok(())
    }
    fn summary(&self)->Value{json!({"count":self.count.to_string(),"first_source_label_ms":self.first.map(|v|v.to_string()),"last_source_label_ms":self.last.map(|v|v.to_string()),"points_sha256":hex::encode(self.hash.clone().finalize()),"hash_policy":"exact-field-json-object-newline-parser-order-v1","raw_reference":"original points remain in this captured provider body; frame/capture identity is attached by the projector","classification_policy":"unproved source point or missing following regular interval"})}
}

fn error(message:impl Into<String>)->CoreError {CoreError(message.into())}
fn integer(value:&Value)->CoreResult<i64>{value.as_i64().or_else(||value.as_str()?.parse().ok()).ok_or_else(||error("Fuyao clock must be a signed exact integer"))}
fn stamp(ms:i64)->CoreResult<Timestamp>{ms.checked_mul(1_000_000).map(DateTime::from_timestamp_nanos).ok_or_else(||error("Fuyao millisecond clock exceeds signed nanosecond range"))}
fn decimal(value:&Value)->CoreResult<Option<Decimal>> {
    let text=match value {Value::Null=>return Ok(None),Value::String(s)if s.trim().is_empty()=>return Ok(None),Value::String(s)=>s.trim().to_owned(),Value::Number(n)=>n.to_string(),_=>return Err(error("Fuyao numeric field is invalid"))};
    Decimal::from_source_str(&text).map(Some).map_err(|_|error("Fuyao numeric field is not an exact decimal"))
}
fn quantity(value:&Value)->CoreResult<Option<Decimal>> {let n=decimal(value)?;if n.as_ref().is_some_and(|v|v<&Decimal::ZERO){return Err(error("Fuyao quantity cannot be negative"))}Ok(n)}
pub fn validate_trade_time(time:&Value,feed:&PublicFeed)->CoreResult<()> {
    if time["market"]!=feed.market || time["code"]!=feed.code || time["time_zone"]!="Asia/Shanghai" {return Err(error("Fuyao session identity or timezone differs"))}
    time["trade_date"].as_str().and_then(|v|v.parse::<NaiveDate>().ok()).ok_or_else(||error("Fuyao exact trading date missing"))?;
    let hours=time["trade_hours"].as_array().filter(|v|!v.is_empty()).ok_or_else(||error("Fuyao source session evidence missing"))?;
    let mut continuous=Vec::new();
    for hour in hours {
        let phase=hour["trade_phase"].as_str().ok_or_else(||error("Fuyao session phase missing"))?;
        for range in hour["phase_range"].as_array().filter(|v|!v.is_empty()).ok_or_else(||error("Fuyao source session ranges missing"))? {
            let begin=integer(&range["begin_time"])?;let end=integer(&range["end_time"])?;
            if begin>=end || begin.checked_mul(1_000_000_000).is_none() || end.checked_mul(1_000_000_000).is_none(){return Err(error("Fuyao session range invalid"))}
            if phase=="continuous"{continuous.push((begin,end));}
        }
    }
    continuous.sort_unstable();
    if continuous.is_empty() || continuous.windows(2).any(|v|v[0].1>v[1].0){return Err(error("Fuyao continuous sessions missing or overlap"))}Ok(())
}
pub fn absolute_day(time:&Value,feed:&PublicFeed,received_at_ns:i64,body_sha256:String,accepted_at_ns:Option<i64>,capture_position:Option<tracefang_core::persistence_contract::CapturePosition>,request_url:Option<String>)->CoreResult<tracefang_core::periods::AbsoluteTradingDay> {
    validate_trade_time(time,feed)?;
    let trade_date=time["trade_date"].as_str().and_then(|v|v.parse::<NaiveDate>().ok()).ok_or_else(||error("source trading date invalid"))?;
    let mut continuous_sessions=vec![];
    for hour in time["trade_hours"].as_array().unwrap().iter().filter(|h|h["trade_phase"]=="continuous") {for range in hour["phase_range"].as_array().unwrap(){continuous_sessions.push(tracefang_core::periods::AbsoluteSession{start:DateTime::from_timestamp_nanos(integer(&range["begin_time"])?*1_000_000_000),end:DateTime::from_timestamp_nanos(integer(&range["end_time"])?*1_000_000_000)});}}
    continuous_sessions.sort_by_key(|v|v.start);
    let day=tracefang_core::periods::AbsoluteTradingDay{market:feed.market.clone(),code:feed.code.clone(),trade_date,continuous_sessions,raw_body_sha256:body_sha256,received_at_ns,accepted_at_ns,capture_position,provenance:json!({"kind":"source_provided_exact_trade_date","source_url":request_url,"source_time_unit":"unix_seconds","time_zone":"Asia/Shanghai","scope_rule":"exact market/code/trade_date only; no recurring weekly inference","source_published_at_unknown":true})};
    let schedule=tracefang_core::periods::MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:None,reference:None,sessions:vec![],authority:Some(tracefang_core::periods::CalendarAuthority{date_exceptions:None,absolute_days:vec![day.clone()]})};schedule.validate()?;Ok(day)
}
fn exact(value:&Value)->Value {match value {Value::Number(n)=>json!(n.to_string()),Value::Array(v)=>Value::Array(v.iter().map(exact).collect()),Value::Object(v)=>Value::Object(v.iter().map(|(k,v)|(k.clone(),exact(v))).collect()),_=>value.clone()}}
fn node<'a>(payload:&'a Value,feed:&PublicFeed,allow_empty:bool)->CoreResult<Option<&'a Value>> {
    if payload["status_code"].as_i64()!=Some(0){return Err(error("Fuyao protocol status is not successful"))}
    let rows=payload["data"]["quote_data"].as_array().ok_or_else(||error("Fuyao quote_data is missing"))?;
    let matches=rows.iter().filter(|r|r["market"]==feed.market && r["code"]==feed.code).collect::<Vec<_>>();
    if matches.len()>1{return Err(error("Fuyao returned duplicate instrument sections"))}
    if let Some(node)=matches.first(){return Ok(Some(node))}
    if allow_empty && rows.is_empty() && payload["data"]["fail_params"].is_null(){return Ok(None)}
    Err(error("Fuyao returned no matching exact market and contract"))
}
fn fields(node:&Value,row:&Value)->CoreResult<BTreeMap<String,Value>> {
    let keys=node["data_fields"].as_array().ok_or_else(||error("Fuyao data_fields missing"))?;
    let values=row.as_array().ok_or_else(||error("Fuyao value row missing"))?;
    if keys.len()!=values.len(){return Err(error("Fuyao row width differs from data_fields"))}
    let mut result=BTreeMap::new();
    for (key,value)in keys.iter().zip(values){let key=key.as_str().ok_or_else(||error("Fuyao field identity is not text"))?;if result.insert(key.into(),value.clone()).is_some(){return Err(error("Fuyao duplicate field identity"))}}
    Ok(result)
}
fn raw(feed:&PublicFeed,fields:&BTreeMap<String,Value>,label_ms:i64)->Value {
    json!({"channel":"tonghuashun_fuyao","protocol":"tonghuashun_fuyao_v1","source_instrument":{"market":feed.market,"code":feed.code},
      "source_precision_ns":"1000000","source_timestamp_ms":label_ms.to_string(),"source_fields":exact(&json!(fields)),
      "wire_time_unit":"milliseconds","published_at":null,"publication_time_unknown":true,
      "field_policy_version":"official-sdk-named-fields-v1","reference_sha256":feed.reference_sha256,
      "capabilities":["snapshot","recent_minute_history"],"tick_coverage":false})
}
pub fn parse_quote(payload:&Value,instrument:&Instrument,feed:&PublicFeed,expected_name:&str,received:Timestamp)->CoreResult<QuoteSnapshot> {
    let node=node(payload,feed,false)?.ok_or_else(||error("Fuyao snapshot is empty"))?;
    let values=node["value"].as_array().filter(|v|v.len()==1).ok_or_else(||error("Fuyao snapshot must contain one exact sample"))?;
    let fields=fields(node,&values[0])?;let get=|key:&str|fields.get(key).unwrap_or(&Value::Null);
    if get("55").as_str()!=Some(expected_name){return Err(error("Fuyao source name differs from verified contract"))}
    let observed_ms=integer(get("1"))?;let mut metadata=raw(feed,&fields,observed_ms);
    metadata["observation_kind"]=json!("snapshot");metadata["response_kind"]=json!("fuyao_snapshot");
    metadata["source_delay"]=node["delay"].clone();metadata["change_basis"]=json!("source_reported; field6 pre is preserved without assuming settlement/close basis");
    metadata["unmapped_fields"]=json!(["14","15","920456"]);
    let quote=QuoteSnapshot {instrument:instrument.clone(),last:decimal(get("10"))?.ok_or_else(||error("Fuyao last is missing"))?,open:decimal(get("7"))?,high:decimal(get("8"))?,low:decimal(get("9"))?,volume:quantity(get("13"))?,change:decimal(get("264648"))?,change_percent:decimal(get("199112"))?,source:SourceMetadata {provider:"tonghuashun_futures".into(),provider_symbol:format!("fuyao:{}:{}",feed.market,feed.code),observed_at:stamp(observed_ms)?,received_at:received,raw_payload:Some(metadata)}};
    quote.validate()?;Ok(quote)
}

/// Source points remain in component lineage; only separately proved opening
/// points join the first regular interval. Other opening points are quarantined.
pub fn parse_minutes(payload:&Value,instrument:&Instrument,feed:&PublicFeed,received:Timestamp,source_as_of:Option<Timestamp>)->CoreResult<Vec<Candle>> {
    parse_minutes_bounded(payload,instrument,feed,received,source_as_of,MAX_DECODED_OUTPUT_BYTES)
}
fn parse_minutes_bounded(payload:&Value,instrument:&Instrument,feed:&PublicFeed,received:Timestamp,source_as_of:Option<Timestamp>,max_output_bytes:usize)->CoreResult<Vec<Candle>> {
    if feed.minute_clock_policy!="fuyao-interval-end-v1"{return Err(error("Fuyao minute clock policy has not been verified"))}
    let Some(node)=node(payload,feed,true)? else{return Ok(vec![])};
    let rows=node["value"].as_array().ok_or_else(||error("Fuyao minute rows missing"))?;
    if rows.len()>100_000{return Err(error("Fuyao minute frame exceeds bounded rows"))}
    let starts=feed.trade_time["trade_hours"].as_array().into_iter().flatten().filter(|h|h["trade_phase"]=="continuous")
        .flat_map(|h|h["phase_range"].as_array().into_iter().flatten()).map(|r|integer(&r["begin_time"])).collect::<CoreResult<Vec<_>>>()?;
    let verified_trade_date=feed.trade_time["trade_date"].as_str().and_then(|v|v.parse::<NaiveDate>().ok());
    let ranges=feed.trade_time["trade_hours"].as_array().into_iter().flatten().filter(|h|h["trade_phase"]=="continuous").flat_map(|h|h["phase_range"].as_array().into_iter().flatten()).map(|r|Ok((integer(&r["begin_time"])?,integer(&r["end_time"])?))).collect::<CoreResult<Vec<_>>>()?;
    let calendar_sha=hex::encode(Sha256::digest(serde_json::to_vec(&feed.trade_time).map_err(|_|error("source calendar encoding failed"))?));
    let mut regular=Vec::<Candle>::new();let mut pending=BTreeMap::<i64,(Value,Decimal,Option<Decimal>)>::new();let mut unclassified=Quarantine::default();let mut previous=None;let mut output=OutputBytes{written:0,limit:max_output_bytes};
    for row in rows {
        let fields=fields(node,row)?;let get=|key:&str|fields.get(key).unwrap_or(&Value::Null);let label_ms=integer(get("1"))?;let label=stamp(label_ms)?;
        if previous.is_some_and(|v|label_ms<=v){return Err(error("Fuyao minute labels are duplicated or out of order"))}previous=Some(label_ms);
        if label_ms.rem_euclid(60_000)!=0{return Err(error("Fuyao minute end label is not aligned"))}
        let open=decimal(get("7"))?.ok_or_else(||error("Fuyao minute open missing"))?;let high=decimal(get("8"))?.ok_or_else(||error("Fuyao minute high missing"))?;let low=decimal(get("9"))?.ok_or_else(||error("Fuyao minute low missing"))?;let close=decimal(get("11"))?.ok_or_else(||error("Fuyao minute close missing"))?;let volume=quantity(get("13"))?;
        let mut metadata=raw(feed,&fields,label_ms);metadata["observation_kind"]=json!("authoritative_bar");metadata["history_file"]=json!("fuyao_single_kline_min_1");metadata["minute_clock_policy"]=json!(feed.minute_clock_policy);
        let opening=starts.contains(&label.timestamp());
        let calendar_known=ranges.iter().any(|(start,end)|*start<=label.timestamp() && label.timestamp()<=*end);
        metadata["source_calendar_trade_date"]=json!(verified_trade_date);metadata["source_calendar_time_info_sha256"]=json!(calendar_sha);metadata["source_calendar_verified_for_label"]=json!(calendar_known);
        if !calendar_known {metadata["source_point_classification"]=json!("unverified_without_exact_date_calendar; raw row retained without opening fold");}
        if opening && open==high && open==low && open==close {
            let original=exact(row);
            if feed.auction_fold_verified_trade_date==verified_trade_date && feed.auction_fold_verified_open_seconds.contains(&label.timestamp().rem_euclid(86400)){pending.insert(label_ms,(original,open,volume));}
            else{unclassified.record(label_ms,original,"source opening point has no independently proved aggregation policy")?;}
            continue;
        }
        let mut bar=Candle {instrument:instrument.clone(),interval_seconds:60,open_time:label.checked_sub_signed(Duration::seconds(60)).ok_or_else(||error("Fuyao interval start overflow"))?,open,high,low,close,volume,source:SourceMetadata {provider:"tonghuashun_futures".into(),provider_symbol:format!("fuyao:{}:{}",feed.market,feed.code),observed_at:label,received_at:received,raw_payload:None}};
        metadata["source_interval_end"]=json!(label);metadata["source_interval_end_ns"]=json!(label.timestamp_nanos_opt().map(|v|v.to_string()));metadata["source_label_semantics"]=json!("interval_end");metadata["canonical_interval_semantics"]=json!("[open,end)");metadata["source_row"]=exact(row);
        metadata["bar_state"]=json!("provisional_authoritative");
        // Completion needs another source interval or a known source clock, never
        // just the local receipt clock. The reducer sees the actual confirmation.
        if source_as_of.is_some_and(|at|at>=label && at<=received){metadata["bar_state"]=json!("final");metadata["finality_evidence"]=json!({"kind":"source_clock_reached_interval_end","source_as_of":source_as_of});}
        if let Some((point,price,volume))=pending.remove(&(label_ms-60_000)) {
            bar.open=price.clone();bar.high=bar.high.max(price.clone());bar.low=bar.low.min(price);
            let known_count=u64::from(volume.is_some())+u64::from(bar.volume.is_some());
            let known_sum=volume.clone().unwrap_or(Decimal::ZERO)+bar.volume.clone().unwrap_or(Decimal::ZERO);
            bar.volume=match(volume,bar.volume){(Some(a),Some(b))=>Some(a+b),_=>None};
            metadata["components"]=json!([{"kind":"independent_opening_source_point","source_label_ms":(label_ms-60_000).to_string(),"row":point},{"kind":"regular_source_minute","source_label_ms":label_ms.to_string(),"row":exact(row)}]);
            metadata["auction_policy"]=json!("fuyao-source-min5-proved-first-interval-fold-v1");
            metadata["source_component_count"]=json!("2");metadata["source_component_known_volume_count"]=json!(known_count.to_string());metadata["source_component_known_volume_sum"]=json!(known_sum.to_string());
            metadata["source_volume_components"]=json!({"known_volume_sum":known_sum.to_string(),"known_count":known_count.to_string(),"total_count":"2","policy":tracefang_core::source_volume::FUYAO_INTERVAL});
        }
        if metadata["source_volume_components"].is_null(){metadata["source_volume_components"]=json!({"known_volume_sum":bar.volume.as_ref().map(ToString::to_string).unwrap_or_else(||"0".into()),"known_count":u64::from(bar.volume.is_some()).to_string(),"total_count":"1","policy":tracefang_core::source_volume::FUYAO_INTERVAL});}
        bar.source.raw_payload=Some(metadata);bar.validate()?;
        // The bound is checked before retaining each emitted row. Reserve a
        // constant-size quarantine reference; never clone all orphan points.
        serde_json::to_writer(&mut output,&bar).map_err(|_|error("Fuyao decoded output exceeds bounded 64MiB"))?;
        std::io::Write::write_all(&mut output,&[0;1024]).map_err(|_|error("Fuyao decoded output exceeds bounded 64MiB"))?;
        regular.push(bar);
    }
    for (label,(row,_,_))in pending {unclassified.record(label,row,"opening source point has no following regular interval in this response")?;}
    if unclassified.count>0{
        let summary=unclassified.summary();
        if serde_json::to_vec(&summary).map_err(|_|error("cannot encode quarantine reference"))?.len()>1024{return Err(error("Fuyao quarantine reference exceeds byte budget"))}
        if regular.is_empty(){return Err(error(format!("Fuyao source points quarantined without regular intervals: {}",summary)))}
        for bar in &mut regular {bar.source.raw_payload.as_mut().unwrap()["source_point_quarantine_ref"]=summary.clone();}
    }
    Ok(regular)
}

pub fn snapshot_request(feed:&PublicFeed)->Value {json!({"code_list":[{"market":feed.market,"codes":[feed.code]}],"trade_class":"intraday","data_fields":["1","6","7","8","9","10","11","13","14","15","19","55","199112","264648","920456","65558"],"lang":"zh-cn","gpid":0})}
pub fn minute_request(feed:&PublicFeed,count:u32,end_ms:i64)->Value {json!({"code_list":[{"market":feed.market,"codes":[feed.code]}],"trade_class":"intraday","time_period":"min_1","trade_date":-1,"begin_time":-(i64::from(count)),"end_time":end_ms,"adjust_type":"actual","gpid":0})}

pub fn five_minute_request(feed:&PublicFeed,count:u32,end_ms:i64)->Value {
    let mut request=minute_request(feed,count,end_ms);request["time_period"]=json!("min_5");request
}

/// As-reported source prices have no proved support interval or completion clock.
/// This parser deliberately produces neither Candle nor a canonical Bar event.
pub fn parse_reported_five_minutes(payload:&Value,feed:&PublicFeed,request:&Value)->CoreResult<Value> {
    let count=request["begin_time"].as_i64().and_then(i64::checked_neg).filter(|v|*v>0&&*v<=100).ok_or_else(||error("source-period count must be 1–100"))?;
    let end=integer(&request["end_time"])?;
    if end<0 || request!=&five_minute_request(feed,count as u32,end){return Err(error("source-period request does not match exact min_5 identity and adjustment"))}
    if !payload["data"]["fail_params"].is_null(){return Err(error("source-period request contains failed parameters"))}
    let Some(section)=node(payload,feed,true)? else{return Ok(json!({"rows":[],"source_delay":null,"source_response_state":"empty"}))};
    let keys=section["data_fields"].as_array().filter(|v|v.len()<=64).ok_or_else(||error("source-period fields exceed bounds"))?;
    fields(section,&Value::Array(vec![Value::Null;keys.len()]))?;
    let input=section["value"].as_array().ok_or_else(||error("source-period rows missing"))?;
    if input.len()>count as usize{return Err(error("source-period response exceeds requested row limit"))}
    let mut rows=Vec::with_capacity(input.len());
    for (index,row) in input.iter().enumerate() {
        let values=fields(section,row)?;
        let label=values.get("1").ok_or_else(||error("source-period label missing"))?;
        let label_ms=integer(label)?;
        let mut output=json!({"record_kind":"source_reported_period_price_row","row_index":index,
            "source_label":match label {Value::String(s)=>s.clone(),_=>label_ms.to_string()},
            "source_label_unit":"milliseconds","source_label_role":"reported_kline_label; interval role unknown",
            "label_utc_display":stamp(label_ms)?,"source_fields":exact(&json!(values)),
            "observed_at":null,"published_at":null,"support_interval_start":null,"support_interval_end":null,
            "finality":"unknown","finalized_at":null,"source_reported_change":null,
            "source_reported_change_percent":null,"source_reported_price_change_basis":null,
            "canonical_bar":false,"min1_authority_input":false});
        let mut presence=serde_json::Map::new();
        for key in ["1","7","8","9","11","13","19"] {presence.insert(key.into(),json!(values.contains_key(key)));}
        output["field_presence"]=Value::Object(presence);
        for (key,name) in [("7","open"),("8","high"),("9","low"),("11","close"),("13","volume"),("19","turnover")] {
            let value=values.get(key).unwrap_or(&Value::Null);
            output[name]=if decimal(value)?.is_none(){Value::Null}else{exact(value)};
        }
        rows.push(output);
    }
    Ok(json!({"source_response_state":if rows.is_empty(){"empty"}else{"rows"},"source_delay":section["delay"],"rows":rows}))
}

#[cfg(test)]
#[path="fuyao_source_period_tests.rs"]
mod reported_period_tests;

#[cfg(test)]mod tests {
    use super::*;
    #[test]fn identical_wall_hour_on_another_date_is_not_folded_or_claimed_verified(){
        let d=definition();let feed=d.public_feed.as_ref().unwrap();let opening=1790731800000i64-86400000;
        let payload=minute_payload(feed,json!([[opening,"100","100","100","100","2",0],[opening+60000,"101","103","99","102",null,0]]));
        let bars=parse_minutes(&payload,&d.instrument,feed,stamp(opening+120000).unwrap(),None).unwrap();assert_eq!(bars.len(),2);assert!(bars[0].source.raw("components").is_none());assert_eq!(bars[0].source.raw("source_calendar_verified_for_label"),Some(&json!(false)));assert_eq!(bars[1].volume,None);
        let mut next=feed.clone();let next_day="2026-10-01";next.trade_time["trade_date"]=json!(next_day);
        for hour in next.trade_time["trade_hours"].as_array_mut().unwrap(){for r in hour["phase_range"].as_array_mut().unwrap(){for k in ["begin_time","end_time"]{r[k]=json!(r[k].as_i64().unwrap()+86400);}}}
        let label=1790731800000i64+86400000;let payload=minute_payload(&next,json!([[label,"100","100","100","100","2",0],[label+60000,"101","103","99","102","1",0]]));
        let bars=parse_minutes(&payload,&d.instrument,&next,stamp(label+120000).unwrap(),None).unwrap();assert_eq!(bars.len(),1);assert_eq!(bars[0].volume,Some(Decimal::ONE));assert_eq!(bars[0].source.raw("source_point_quarantine_ref").unwrap()["count"],"1","a fold proof for Sep30 cannot be extrapolated to Oct1");
    }
    fn definition()->crate::catalog::Definition {crate::catalog::Catalog::embedded().unwrap().get("IC2612").unwrap().clone()}
    fn minute_payload(feed:&PublicFeed,rows:Value)->Value {json!({"status_code":0,"data":{"fail_params":null,"quote_data":[{"market":feed.market,"code":feed.code,"data_fields":["1","7","8","9","11","13","19"],"value":rows}]}})}
    #[test]fn price_and_optional_statistics_keep_exact_signed_values_and_unknowns(){
        let d=definition();let feed=d.public_feed.as_ref().unwrap();let now:Timestamp="2026-09-30T02:00:00Z".parse().unwrap();
        let mut payload=json!({"status_code":0,"data":{"quote_data":[{"market":feed.market,"code":feed.code,"data_fields":["1","7","8","9","10","13","55","264648","199112","14"],"value":[[1790733600000i64,"-2.0000000000000000000000000001","0","-3","-2.0000000000000000000000000001",null,d.name,"-0.0000000000000000000000000001",null,"340282366920938463463374607431768211455"]]}]}});
        let quote=parse_quote(&payload,&d.instrument,feed,&d.name,now).unwrap();assert_eq!(quote.last.to_string(),"-2.0000000000000000000000000001");assert_eq!(quote.volume,None);assert_eq!(quote.change_percent,None);assert_eq!(quote.source.observed_at.timestamp_nanos_opt(),Some(1790733600000000000));
        assert_eq!(quote.source.raw("source_fields").unwrap()["14"],"340282366920938463463374607431768211455");
        payload["data"]["quote_data"][0]["code"]=json!("different");assert!(parse_quote(&payload,&d.instrument,feed,&d.name,now).is_err());
    }
    #[test]fn opening_point_has_lineage_and_null_quantity_does_not_become_zero(){
        let d=definition();let feed=d.public_feed.as_ref().unwrap();let now:Timestamp="2026-09-30T02:00:00Z".parse().unwrap();let opening=1790731800000i64;
        let payload=minute_payload(feed,json!([[opening,"100","100","100","100","2",0],[opening+60000,"101","103","99","102",null,0],[opening+120000,"102","102","102","102","0",0]]));
        let bars=parse_minutes(&payload,&d.instrument,feed,now,None).unwrap();assert_eq!(bars.len(),2);assert_eq!(bars[0].open_time,stamp(opening).unwrap());assert_eq!(bars[0].open,Decimal::from(100));assert_eq!(bars[0].volume,None);assert_eq!(bars[1].volume,Some(Decimal::ZERO));
        assert_eq!(bars[0].source.raw("components").unwrap().as_array().unwrap().len(),2);assert_eq!(bars[0].source.raw("source_component_known_volume_sum"),Some(&json!("2")));
        assert_eq!(bars[1].source.raw("bar_state"),Some(&json!("provisional_authoritative")),"receipt alone does not finalize a source tail");
        let mut duplicate=payload.clone();duplicate["data"]["quote_data"][0]["value"][2][0]=json!(opening+60000);assert!(parse_minutes(&duplicate,&d.instrument,feed,now,None).is_err());
        let missing=minute_payload(feed,json!([[opening,"100","100","100","100","2",0]]));assert!(parse_minutes(&missing,&d.instrument,feed,now,None).unwrap_err().to_string().contains("quarantined"));
        let correction=minute_payload(feed,json!([[opening,"100","100","100","100","3",0],[opening+60000,"101","103","99","102","0",0]]));
        assert_eq!(parse_minutes(&correction,&d.instrument,feed,now,None).unwrap()[0].volume,Some(Decimal::from(3)),"same source point revised in a new frame is a fresh full reconstruction");
    }
    #[test]fn future_label_and_missing_opening_proof_remain_explicit(){
        let d=definition();let mut feed=d.public_feed.unwrap();let label=1790731860000i64;let now=stamp(label-30000).unwrap();
        let payload=minute_payload(&feed,json!([[label,"0","0","0","0","0",0]]));let bars=parse_minutes(&payload,&d.instrument,&feed,now,Some(now)).unwrap();assert_eq!(bars[0].source.raw("bar_state"),Some(&json!("provisional_authoritative")));assert_eq!(bars[0].open,Decimal::ZERO);
        feed.auction_fold_verified_open_seconds.clear();let p=minute_payload(&feed,json!([[label-60000,"0","0","0","0","1",0],[label,"0","0","0","0","0",0]]));let bars=parse_minutes(&p,&d.instrument,&feed,now,None).unwrap();assert_eq!(bars.len(),1);assert_eq!(bars[0].volume,Some(Decimal::ZERO));assert_eq!(bars[0].source.raw("source_point_quarantine_ref").unwrap()["count"],"1");assert!(bars[0].source.raw("unclassified_source_points").is_none());
    }
    #[test]fn many_orphans_share_a_bounded_reference_instead_of_copying_all_points(){
        let d=definition();let mut feed=d.public_feed.unwrap();feed.auction_fold_verified_open_seconds.clear();
        let opening=1790731800000i64;let mut rows=vec![];
        for day in 0..500 {let label=opening+day*86400000;rows.push(json!([label,"100","100","100","100",null,0]));rows.push(json!([label+60000,"100","102","99","101","0",0]));}
        let bars=parse_minutes(&minute_payload(&feed,json!(rows)),&d.instrument,&feed,stamp(opening).unwrap(),None).unwrap();
        assert_eq!(bars.len(),999);let reference=bars[0].source.raw("source_point_quarantine_ref").unwrap();
        assert_eq!(reference["count"],"1");assert_eq!(reference["points_sha256"].as_str().unwrap().len(),64);
        assert!(serde_json::to_vec(reference).unwrap().len()<=1024);
        for bar in &bars {assert_eq!(bar.source.raw("source_point_quarantine_ref"),Some(reference));assert!(bar.source.raw("unclassified_source_points").is_none());}
        assert!(serde_json::to_vec(&bars).unwrap().len()<999*4096,"output grows with regular rows, not regular rows times orphan rows");
    }
    #[test]fn output_byte_limit_rejects_before_retaining_an_over_budget_row(){
        let d=definition();let feed=d.public_feed.as_ref().unwrap();let label=1790731860000i64;let now=stamp(label+60000).unwrap();
        let payload=minute_payload(feed,json!([[label,"100","102","99","101",null,0]]));
        let one=parse_minutes(&payload,&d.instrument,feed,now,None).unwrap();let budget=serde_json::to_vec(&one[0]).unwrap().len()+1024;
        assert!(parse_minutes_bounded(&payload,&d.instrument,feed,now,None,budget).is_ok());
        assert!(parse_minutes_bounded(&payload,&d.instrument,feed,now,None,budget-1).unwrap_err().to_string().contains("bounded"));
        let mut two=payload.clone();two["data"]["quote_data"][0]["value"].as_array_mut().unwrap().push(json!([label+60000,"100","102","99","101",null,0]));
        assert!(parse_minutes_bounded(&two,&d.instrument,feed,now,None,budget).is_err());
    }
}
