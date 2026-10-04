//! Exercises the production replay projector without the production Store/server wiring.
#[path="../src/capture.rs"] mod capture;
#[path="../src/catalog.rs"] mod catalog;
#[path="../src/providers/mod.rs"] mod providers;
#[path="../src/quotes.rs"] mod quotes;
#[path="../src/replay.rs"] mod replay;
mod api {
 use std::sync::Arc;
 use axum::{http::StatusCode,response::{IntoResponse,Response}};
 use crate::{catalog::Catalog,capture::Capture};
 #[derive(Clone)]pub struct Market{pub catalog:Arc<Catalog>,pub store:tracefang_core::native_store::Store}
 impl Market{pub fn source(&self,code:&str)->anyhow::Result<String>{Ok(self.catalog.get(code)?.source_ids[0].clone())}}
 #[derive(Clone)]pub struct AppState{pub market:Market,pub capture:Capture,pub shutdown:tokio::sync::watch::Receiver<bool>}
 pub struct ApiError(pub StatusCode,pub String);
 impl From<anyhow::Error> for ApiError{fn from(error:anyhow::Error)->Self{Self(StatusCode::BAD_GATEWAY,error.to_string())}}
 impl IntoResponse for ApiError{fn into_response(self)->Response{(self.0,self.1).into_response()}}
}
mod pages {
 pub fn schedule(market:&crate::api::Market,code:&str)->anyhow::Result<tracefang_core::periods::MarketSchedule>{
  Ok(serde_json::from_value(market.catalog.schedules[&market.catalog.get(code)?.market_schedule_id].clone())?)
 }
}
fn main(){}
