//! Controlled immutable native research input and exact aggregate API.
use anyhow::{Context,Result,ensure};
use axum::{Router,Json,extract::{State,Path},routing::{get,post}};
use serde::Deserialize;use serde_json::{Value,json};
use std::{sync::{Arc,atomic::{AtomicBool,Ordering}},path::PathBuf};
use crate::{api::{AppState,ApiError},batch_snapshot, columnar_query};
use tracefang_core::{periods::Period,persistence_contract::{CanonicalScanRequest,i64_string}};
pub fn routes()->Router<AppState>{Router::new()
 .route("/api/research/native-snapshots",post(create))
 .route("/api/research/native-snapshots/runtime",get(runtime))
 .route("/api/research/native-snapshots/{id}",get(manifest))
 .route("/api/research/native-snapshots/{id}/aggregate",post(aggregate))}
fn root(state:&AppState)->PathBuf{std::env::var_os("TRACEFANG_BATCH_SNAPSHOTS_DIR").map(PathBuf::from).unwrap_or_else(||state.market.store.file_path().parent().unwrap_or_else(||std::path::Path::new(".")).join("batch-snapshots"))}
struct Cancellation(Arc<AtomicBool>);impl Drop for Cancellation{fn drop(&mut self){self.0.store(true,Ordering::Release)}}
fn cancellation(state:&AppState)->(Cancellation,batch_snapshot::Cancel){let stopped=Arc::new(AtomicBool::new(false));let flag=stopped.clone();let shutdown=state.shutdown.clone();(Cancellation(stopped),Arc::new(move||flag.load(Ordering::Acquire)||*shutdown.borrow()))}
#[derive(Deserialize)]#[serde(deny_unknown_fields)]pub struct CreateRequest{pub code:String,pub source_id:Option<String>,#[serde(default)]pub period:String,#[serde(with="i64_string")]pub start_ns:i64,#[serde(with="i64_string")]pub end_ns:i64,#[serde(default)]pub final_only:bool,pub reference:Option<String>}
pub async fn create(State(state):State<AppState>,Json(request):Json<CreateRequest>)->Result<Json<Value>,ApiError>{
 create_inner(state,request).await.map_err(Into::into)
}
async fn create_inner(state:AppState,request:CreateRequest)->Result<Json<Value>>{
 let definition=state.market.catalog.get(&request.code)?;let source=request.source_id.unwrap_or(state.market.source(&definition.instrument.symbol)?);ensure!(definition.source_ids.contains(&source),"native snapshot source unavailable");let period=Period::parse(if request.period.is_empty(){"1m"}else{&request.period})?;ensure!(period!=Period::Timeline,"native research snapshot requires a bar period");ensure!(request.start_ns<=request.end_ns,"native snapshot range inverted");
 let (_guard,cancel)=cancellation(&state);let root=root(&state);let published=batch_snapshot::publish(&root,&state.market.store,batch_snapshot::Plan{scan:CanonicalScanRequest{symbol:definition.instrument.symbol.clone(),source_id:source,interval_seconds:if period==Period::S1{1}else{60},start_ns:request.start_ns,end_ns:request.end_ns,final_only:request.final_only,expected_version:Some(state.market.store.version().await?)},period,schedule:Some(crate::pages::schedule(&state.market,&request.code)?),resume:None,build:crate::replay::projector_build_hash()},cancel,Arc::new(|phase|tracing::debug!(progress=%phase,"native research snapshot preparation"))).await?;
 if let Some(reference)=request.reference{let base=root.clone();let id=published.manifest.id.clone();tokio::task::spawn_blocking(move||batch_snapshot::pin(&base,&id,&reference)).await.map_err(anyhow::Error::from)??;}
 Ok(Json(json!({"snapshot_id":published.manifest.id,"manifest":published.manifest,"retention":"active readers and explicit research/audit reference pins protected; unreferenced cache versions may be reclaimed"})))
}
pub async fn manifest(State(state):State<AppState>,Path(id):Path<String>)->Result<Json<Value>,ApiError>{let root=root(&state);let published=tokio::task::spawn_blocking(move||batch_snapshot::read(&root,&id)).await.map_err(anyhow::Error::from)??;Ok(Json(serde_json::to_value(published.manifest).map_err(anyhow::Error::from)?))}
#[derive(Deserialize)]#[serde(deny_unknown_fields)]pub struct AggregateRequest{#[serde(with="i64_string")]pub start_ns:i64,#[serde(with="i64_string")]pub end_ns:i64}
pub async fn aggregate(State(state):State<AppState>,Path(id):Path<String>,Json(request):Json<AggregateRequest>)->Result<Json<Value>,ApiError>{
 let (_guard,cancel)=cancellation(&state);let root=root(&state);let published=tokio::task::spawn_blocking(move||batch_snapshot::read(&root,&id)).await.map_err(anyhow::Error::from)??;
 let runtime=tokio::task::spawn_blocking(columnar_query::Runtime::installed).await.map_err(anyhow::Error::from)?.ok();let result=columnar_query::aggregate(published,request.start_ns,request.end_ns,runtime,cancel).await?;Ok(Json(serde_json::to_value(result).map_err(anyhow::Error::from)?))
}
async fn runtime()->Json<Value>{let state=tokio::task::spawn_blocking(columnar_query::Runtime::installed).await;Json(match state{Ok(Ok(runtime))=>json!({"state":"ready","engine":"duckdb","version":"1.5.6","release":runtime.evidence,"scope":"controlled immutable manifest/range; exact Rust fallback for unsupported precision"}),_=>json!({"state":"unavailable","version":"1.5.6","engine":"rust_exact_fallback","reason":"pinned native DuckDB runtime not installed or release verification failed"})})}
