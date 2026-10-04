//! Runtime native store adapter. Legacy database URLs belong to the isolated importer.
use anyhow::{Context,Result,ensure};
use chrono::DateTime;
use serde_json::{Value,json};
use std::ops::Deref;
use tracefang_core::{domain::{Timestamp,isoformat},persistence_contract::{BarSelection,CanonicalSnapshotRequest},native_store::Store as NativeStore};

#[derive(Clone)]
pub struct Store(NativeStore);
impl Deref for Store {type Target=NativeStore;fn deref(&self)->&NativeStore {&self.0}}
impl Store {
    pub async fn connect(path:&str)->Result<Self> {
        ensure!(!path.contains("://"),"online Store requires a native file path; legacy URLs belong to the migration importer");
        let path=path.to_owned();Ok(Self(tokio::task::spawn_blocking(move||NativeStore::open(path)).await??))
    }
    pub async fn connect_read_only(path:&str)->Result<Self> {let path=path.to_owned();Ok(Self(tokio::task::spawn_blocking(move||NativeStore::open_read_only(path)).await??))}
    pub async fn connect_read_only_generation(path:&str,generation:&str)->Result<Self> {
        let store=Self::connect_read_only(path).await?;Ok(Self(store.0.read_generation(generation).await?))
    }
    pub async fn migrate(&self)->Result<()> {ensure!(self.version().await?.aggregation_version==tracefang_core::persistence_contract::AGGREGATION_VERSION,"runtime requires the current verified aggregation index");Ok(())}
    pub async fn initialize_instruments(&self,instruments:&[Value],defaults:&[String])->Result<()> {
        self.set_metadata("catalog","instruments",json!(instruments)).await?;
        let defaults=defaults.to_vec();self.update_metadata("watchlist","default",move|current|Ok(if current.is_null(){json!(defaults)}else{current})).await?;Ok(())
    }
    pub async fn watchlist(&self)->Result<Vec<String>> {Ok(self.metadata("watchlist","default").await?.map(serde_json::from_value).transpose()?.unwrap_or_default())}
    pub async fn set_watchlist(&self,symbol:&str,add:bool)->Result<(tracefang_core::persistence_contract::SnapshotVersion,Vec<String>)> {
        let symbol=symbol.to_owned();let (version,value)=self.update_metadata_receipt("watchlist","default",move|current| {
            let mut values:Vec<String>=if current.is_null(){vec![]}else{serde_json::from_value(current)?};
            if add && !values.contains(&symbol) {values.push(symbol);}else if !add {values.retain(|v|v!=&symbol);ensure!(!values.is_empty(),"watchlist_minimum_one");}Ok(json!(values))
        }).await?;Ok((version,serde_json::from_value(value)?))
    }
    pub async fn routes(&self)->Result<Vec<Value>> {Ok(self.metadata("routes","realtime").await?.map(serde_json::from_value).transpose()?.unwrap_or_default())}
    pub async fn set_route(&self,symbol:&str,source:&str)->Result<()> {
        let symbol=symbol.to_owned();let source=source.to_owned();self.update_metadata("routes","realtime",move|current| {
            let mut rows:Vec<Value>=if current.is_null(){vec![]}else{serde_json::from_value(current)?};rows.retain(|v|!(v["instrument_symbol"]==symbol && v["capability"]=="realtime"));
            rows.push(json!({"instrument_symbol":symbol,"source_id":source,"capability":"realtime"}));Ok(json!(rows))
        }).await?;Ok(())
    }
    pub async fn set_routes_group(&self,symbols:Vec<String>,source:String)->Result<(tracefang_core::persistence_contract::SnapshotVersion,Vec<Value>)> {
        ensure!(!symbols.is_empty() && symbols.len()<=64,"route group exceeds bounded dependencies");
        let (version,value)=self.update_metadata_receipt("routes","realtime",move|current| {
            let mut rows:Vec<Value>=if current.is_null(){vec![]}else{serde_json::from_value(current)?};
            rows.retain(|v|!(v["capability"]=="realtime" && v["instrument_symbol"].as_str().is_some_and(|s|symbols.iter().any(|symbol|symbol==s))));
            for symbol in symbols {rows.push(json!({"instrument_symbol":symbol,"source_id":source,"capability":"realtime"}));}Ok(json!(rows))
        }).await?;Ok((version,serde_json::from_value(value)?))
    }
    pub async fn bars_before(&self,symbol:&str,source:&str,interval:i32,before:Option<Timestamp>,limit:i64)->Result<Vec<Value>> {
        ensure!(matches!(interval,1|60),"only canonical second/minute facts may be directly paged");let selection=if let Some(before)=before {BarSelection::Before {before_ns:ns(before)?,count:limit.try_into()?}}else{BarSelection::Latest {count:limit.try_into()?}};
        Ok(self.canonical_snapshot(CanonicalSnapshotRequest {symbol:symbol.into(),source_id:source.into(),period:if interval==1{"1s"}else{"1m"}.into(),selection,final_only:false,expected_version:None}).await?.bars)
    }
    pub async fn bars_range(&self,symbol:&str,source:&str,interval:i32,start:Timestamp,end:Timestamp)->Result<Vec<Value>> {
        ensure!(matches!(interval,1|60),"only canonical second/minute facts may be directly read");
        Ok(self.canonical_snapshot(CanonicalSnapshotRequest {symbol:symbol.into(),source_id:source.into(),period:if interval==1{"1s"}else{"1m"}.into(),selection:BarSelection::Range {start_ns:ns(start)?,end_ns:ns(end)?,max_rows:10_000},final_only:false,expected_version:None}).await?.bars)
    }
    pub async fn first_minute(&self,symbol:&str,source:&str)->Result<Option<Timestamp>> {
        let view=self.canonical_snapshot(CanonicalSnapshotRequest {symbol:symbol.into(),source_id:source.into(),period:"1m".into(),selection:BarSelection::Latest {count:1},final_only:false,expected_version:None}).await?;
        view.coverage["first_open_time_ns"].as_str().map(|v|Ok(DateTime::from_timestamp_nanos(v.parse()?))).transpose()
    }
    pub async fn series_state(&self,symbol:&str,source:&str)->Result<Option<Value>> {self.metadata("series_state",&format!("{source}:{symbol}")).await}
    pub async fn aggregates(&self,symbol:&str,source:&str,bounds:&[(Timestamp,Timestamp)])->Result<Vec<Value>> {
        let bounds=bounds.iter().map(|(lo,hi)|Ok((ns(*lo)?,ns(*hi)?))).collect::<Result<Vec<_>>>()?;
        let (_,values)=self.range_aggregates(symbol,source,bounds,false,None).await?;
        Ok(values.into_iter().map(|value|value.map_or_else(||json!({"first_open_time":null}),|v|json!({
            "first_open_time":isoformat(DateTime::from_timestamp_nanos(v.first_open_time_ns)),"open":v.open,"high":v.high,"low":v.low,"close":v.close,"volume":v.volume(),
            "known_volume_sum":v.known_volume_sum,"known_volume_count":v.known_volume_count.to_string(),"all_final":v.final_count==v.total_count,"any_authoritative":v.forming_count<v.total_count,
            "finalized_at":v.finalized_at_ns.map(|ns|isoformat(DateTime::from_timestamp_nanos(ns))),"revision":v.last_commit_id.to_string(),"revision_sum":v.revision_sum,
            "component_count":v.total_count.to_string(),"provider_symbol":symbol,"evidence_channel_id":source,"observed_at":isoformat(DateTime::from_timestamp_nanos(v.source_observed_at_ns)),"received_at":isoformat(DateTime::from_timestamp_nanos(v.received_at_ns)),"coverage_contiguous":v.contiguous
        }))).collect())
    }
}
pub fn ns(timestamp:Timestamp)->Result<i64> {timestamp.timestamp_nanos_opt().context("timestamp outside signed nanosecond domain")}
