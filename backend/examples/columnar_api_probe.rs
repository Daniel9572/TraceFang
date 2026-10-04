//! HTTP integration of immutable native input and controlled exact aggregates.
#[path="../src/capture.rs"]mod capture;
#[path="../src/catalog.rs"]mod catalog;
#[path="../src/batch_snapshot.rs"]mod batch_snapshot;
#[path="../src/columnar_query.rs"]mod columnar_query;
#[path="../src/columnar_api.rs"]mod columnar_api;
mod replay{pub fn projector_build_hash()->String{tracefang_core::quant_core::results::backend_build_fingerprint()}}
mod api{
 use std::sync::Arc;use axum::{http::StatusCode,response::{IntoResponse,Response}};
 #[derive(Clone)]pub struct Market{pub catalog:Arc<crate::catalog::Catalog>,pub store:tracefang_core::native_store::Store}
 impl Market{pub fn source(&self,code:&str)->anyhow::Result<String>{Ok(self.catalog.get(code)?.source_ids[0].clone())}}
 #[derive(Clone)]pub struct AppState{pub market:Market,pub shutdown:tokio::sync::watch::Receiver<bool>}
 pub struct ApiError(pub StatusCode,pub String);impl From<anyhow::Error>for ApiError{fn from(e:anyhow::Error)->Self{Self(StatusCode::BAD_REQUEST,e.to_string())}}impl IntoResponse for ApiError{fn into_response(self)->Response{(self.0,self.1).into_response()}}
}
mod pages{pub fn schedule(m:&crate::api::Market,code:&str)->anyhow::Result<tracefang_core::periods::MarketSchedule>{Ok(serde_json::from_value(m.catalog.schedules[&m.catalog.get(code)?.market_schedule_id].clone())?)}}
fn main(){}
#[cfg(test)]mod tests{
 use super::*;use anyhow::{Result,ensure};use serde_json::{json,Value};use tracefang_core::persistence_contract::*;
 #[tokio::test]async fn actual_http_publishes_pins_reopens_and_exactly_aggregates_wide_native_rows()->Result<()>{
  let dir=tempfile::tempdir()?;let store=tracefang_core::native_store::Store::open(dir.path().join("facts.redb"))?.staging("http-isolated").await?;
  let row=|minute:i64,volume:Option<&str>|{let time=minute*60_000_000_000;ImportBarRow{instrument_symbol:"XAU/USD".into(),realtime_source_id:"jin10_client".into(),evidence_channel_id:"jin10_local".into(),interval_seconds:60,open_time_ns:time,close_time_ns:time+60_000_000_000,open:"1".into(),high:"1".into(),low:"1".into(),close:"1".into(),volume:volume.map(str::to_owned),revision:1,received_sequence:Some(u64::MAX),state:"final".into(),finalized_at_ns:Some(time+60_000_000_000),source_observed_at_ns:9_007_199_254_740_993,received_at_ns:9_007_199_254_740_999,source_metadata:json!({"provider":"jin10_client","provider_symbol":"XAUUSD.GOODS","raw_payload":null}),evidence:Value::Null}};
  store.import_bars(ImportBatch{context:ImportContext{origin_id:"http-fixture".into(),source_fingerprint:"fixed".into(),schema_version:SCHEMA_VERSION.into(),range_label:"bars".into(),legacy_cursor:None,expected_sha256:None},row_offset:0,rows:vec![row(0,Some("79228162514264337593543950335.0000000000000000000000000001")),row(1,None)]}).await?;
  let (_stop,shutdown)=tokio::sync::watch::channel(false);let state=api::AppState{market:api::Market{catalog:std::sync::Arc::new(catalog::Catalog::embedded()?),store:store.clone()},shutdown};let app=columnar_api::routes().with_state(state);let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await?;let addr=listener.local_addr()?;let server=tokio::spawn(async move{axum::serve(listener,app).await});let client=reqwest::Client::new();let base=format!("http://{addr}/api/research/native-snapshots");
  let response=client.post(&base).json(&json!({"code":"XAUUSD","source_id":"jin10_client","period":"1m","start_ns":"0","end_ns":"120000000000","final_only":false,"reference":"simulation-fixed-http-fixture"})).send().await?;let status=response.status();let output:Value=response.json().await?;ensure!(status.is_success(),"snapshot HTTP failed: {}",output);let id=output["snapshot_id"].as_str().unwrap();ensure!(output["manifest"]["summary"]["row_count"]=="2","HTTP omitted full range");
  let manifest:Value=client.get(format!("{base}/{id}")).send().await?.error_for_status()?.json().await?;ensure!(manifest["id"]==id,"reopen HTTP manifest changed");let result:Value=client.post(format!("{base}/{id}/aggregate")).json(&json!({"start_ns":"0","end_ns":"120000000000"})).send().await?.error_for_status()?.json().await?;ensure!(result["engine"]=="rust_exact_coefficient_scale"&&result["result"]["known_volume_sum"]=="79228162514264337593543950335.0000000000000000000000000001"&&result["result"]["volume_complete"]==false,"HTTP wide/NULL aggregate differs");ensure!(client.post(format!("{base}/{id}/aggregate")).json(&json!({"start_ns":"-1","end_ns":"120000000000","sql":"SELECT 1"})).send().await?.status().is_client_error(),"arbitrary SQL field accepted");
  server.abort();store.close().await?;Ok(())
 }
}
