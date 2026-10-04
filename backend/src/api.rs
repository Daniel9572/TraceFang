use std::{sync::Arc,time::{Duration,Instant},collections::BTreeMap};
use axum::{Router,Json,extract::{State,Path,Query,WebSocketUpgrade,ws::{WebSocket,Message,CloseFrame}},
    http::StatusCode,response::{IntoResponse,Response},routing::{get,post}};
use chrono::{DateTime,Utc};
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{Value,json};
use tokio::sync::{watch,Mutex};
use tracefang_core::{periods::Period,domain::Instrument};
use crate::{market::Market,ingestion::Acquisition,capture::Capture,pages,providers,analysis};

#[derive(Clone)]
pub struct AppState {
    pub market:Market,pub acquisition:Acquisition,pub capture:Capture,pub shutdown:watch::Receiver<bool>,
    pub http:reqwest::Client,
    pub options_cache:Arc<Mutex<BTreeMap<String,(Instant,Value)>>>,
    pub ai:Arc<analysis::ai::AiService>,
    pub history:Arc<crate::history::History>,
    pub research:crate::research::Research,
}
pub struct ApiError(pub StatusCode,pub String);
impl IntoResponse for ApiError {fn into_response(self)->Response{(self.0,Json(json!({"detail":self.1}))).into_response()}}
impl From<anyhow::Error> for ApiError {fn from(error:anyhow::Error)->Self {
    tracing::error!(%error,"API operation failed");Self(StatusCode::BAD_GATEWAY,"数据服务暂时不可用，请稍后重试".into())
}}
pub type ApiResult=Result<Json<Value>,ApiError>;
fn invalid(detail:impl Into<String>)->ApiError{ApiError(StatusCode::UNPROCESSABLE_ENTITY,detail.into())}
fn definition<'a>(s:&'a AppState,code:&str)->Result<&'a crate::catalog::Definition,ApiError>{s.market.catalog.get(code).map_err(|_|ApiError(StatusCode::NOT_FOUND,"不支持该品种".into()))}
fn checked_period(value:Option<&str>)->Result<Period,ApiError>{Period::parse(value.unwrap_or("1m")).map_err(|_|invalid("不支持该周期"))}
fn checked_calendar(d:&crate::catalog::Definition,period:Period)->Result<(),ApiError>{
    if d.market_schedule_id=="fuyao_unverified" && !period.is_base(){return Err(ApiError(StatusCode::CONFLICT,"该来源的长期交易日历尚未核验；当前可读取来源快照和原始分钟行情".into()))}Ok(())
}
fn count(value:Option<usize>,default:usize,max:usize)->Result<usize,ApiError>{let n=value.unwrap_or(default);if n==0||n>max{Err(invalid(format!("数量须为 1–{max}")))}else{Ok(n)}}

pub fn router(state:AppState)->Router {
    Router::new()
        .merge(crate::replay::router())
        .nest("/api/research",crate::research::router())
        .merge(crate::analysis::context::router())
        .merge(crate::analysis::service::router())
        .merge(crate::columnar_api::routes())
        .route("/api/ready",get(ready)).route("/api/health",get(health))
        .route("/api/instruments",get(instruments)).route("/api/sources",get(sources))
        .route("/api/sources/{source}/test",post(test_source))
        .route("/api/watchlist",get(watchlist)).route("/api/watchlist/{code}",post(add_watchlist).delete(remove_watchlist))
        .route("/api/instruments/{code}/source",get(instrument_source).put(update_source))
        .route("/api/quotes/{code}",get(quote)).route("/api/quotes/{code}/last",get(last_quote))
        .route("/api/bars/{code}",get(bars)).route("/api/bars/{code}/range",get(bars_range)).route("/api/candles/{code}",get(candles))
        .route("/api/source-period-prices/{code}",post(source_period_prices))
        .route("/api/source-period-prices/{code}/{reference}",get(source_period_reference))
        .route("/api/bars/{code}/history",post(history_page)).route("/api/candles/{code}/backfill",post(backfill))
        .route("/api/timeline/{code}",get(timeline))
        .route("/api/stream/quotes/{code}",get(stream))
        .route("/api/replay/frames",get(replay_bounds)).route("/api/replay/cursor",get(replay_cursor))
        .route("/api/expert/options/gold",get(options))
        .route("/api/expert/options/products",get(option_products))
        .route("/api/expert/options/{product}",get(product_options))
        .route("/api/{*path}",get(missing).post(missing).put(missing).delete(missing))
        .with_state(state)
}
async fn missing()->ApiError{ApiError(StatusCode::NOT_FOUND,"API route not found".into())}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SourcePeriodQuery {source_id:String,period:String,limit:Option<usize>}
async fn source_period_prices(State(s):State<AppState>,Path(code):Path<String>,Json(query):Json<SourcePeriodQuery>)->ApiResult {
    let d=definition(&s,&code)?;
    if query.source_id!="tonghuashun_futures" || query.period!="min_5" || !d.source_ids.contains(&query.source_id){return Err(invalid("来源五分钟价格须使用该品种已配置的同花顺来源"))}
    let limit=count(query.limit,100,100)?;
    s.research.source_period_prices(d,limit,s.market.store.read_only()).await.map(Json).map_err(|e|ApiError(e.status,e.detail))
}
async fn source_period_reference(State(s):State<AppState>,Path((code,reference)):Path<(String,String)>)->ApiResult {
    let d=definition(&s,&code)?;
    s.research.source_period_reference(&d.code,&reference).await.map(Json).map_err(|e|ApiError(e.status,e.detail))
}
pub fn build_info()->Value {
    json!({"runtime":"rust","version":env!("CARGO_PKG_VERSION"),"backend_build_fingerprint":tracefang_core::quant_core::results::backend_build_fingerprint(),"backend_build_config":tracefang_core::quant_core::results::backend_build_config(),
        "persistence_schema":tracefang_core::persistence_contract::SCHEMA_VERSION,"aggregation_version":tracefang_core::persistence_contract::AGGREGATION_VERSION,
        "precision_policy":{"facts":"exact_decimal_lexeme","add_multiply":"arbitrary_width_exact","division_sqrt":"28_decimal_places_half_even","public_decimals":"decimal_strings","public_u64_ns":"integer_strings"}})
}
pub fn data_directory()->anyhow::Result<std::path::PathBuf> {
    use anyhow::Context;
    if let Ok(path)=std::env::var("TRACEFANG_DATA_DIR"){return Ok(path.into())}
    #[cfg(target_os="macos")] {return Ok(std::path::PathBuf::from(std::env::var("HOME").context("user home unavailable")?).join("Library/Application Support/TraceFang"));}
    #[cfg(target_os="windows")] {return Ok(std::path::PathBuf::from(std::env::var("LOCALAPPDATA").context("local application data unavailable")?).join("TraceFang"));}
    #[cfg(not(any(target_os="macos",target_os="windows")))] {let base=std::env::var("XDG_DATA_HOME").map(std::path::PathBuf::from).unwrap_or(std::path::PathBuf::from(std::env::var("HOME").context("user home unavailable")?).join(".local/share"));Ok(base.join("TraceFang"))}
}
async fn ready(State(s):State<AppState>)->Json<Value>{
    let db=s.market.persistence.borrow().clone();let acquisition=s.acquisition.projection_status.borrow().clone();
    let capture=s.capture.connected()&&s.acquisition.capture_status.borrow()["state"]=="connected";
    let running=s.acquisition.projection_status.has_changed().is_ok() && acquisition["state"]=="running" && acquisition["evidence_complete"]==true;
    let healthy=db["state"]=="healthy"&&running&&capture;
    let version=s.market.store.version().await.ok();
    let raw_bounds=s.capture.bounds().await.ok();
    Json(json!({"process_id":std::process::id(),"runtime":"rust","version":env!("CARGO_PKG_VERSION"),"backend_build_fingerprint":tracefang_core::quant_core::results::backend_build_fingerprint(),"build_info":build_info(),"paths":{"data":data_directory().ok(),"store":s.market.store.file_path(),"capture":s.capture.path()},"read_only":s.market.store.read_only(),"status":if s.market.store.read_only(){"read_only_shadow"}else if healthy{"ok"}else{"degraded"},"production_ready":healthy && !s.market.store.read_only(),"authority":if s.market.store.read_only(){acquisition["authority"].clone()}else{json!("native_or_verified_legacy_boundary")},"snapshot_version":version,"generation":version.as_ref().map(|v|v.active_generation.clone()),"stage_manifest":acquisition["stage_manifest"],"capture_retained_bounds":raw_bounds,"projection_start_boundary":s.market.store.projection_start_boundary().await.ok().flatten(),
        "database":db,"acquisition":{"state":if running{"running"}else{"unavailable"},"projection":acquisition},"capture":{"state":if capture{"connected"}else{"unavailable"}},"ingress":s.acquisition.frames.status()}))
}
async fn health(State(s):State<AppState>)->Json<Value>{
    let sources=source_descriptors(&s);let database=s.market.persistence.borrow().clone();
    let projection=s.acquisition.projection_status.borrow().clone();let healthy=database["state"]=="healthy" && projection["state"]=="running" && projection["evidence_complete"]==true && sources.iter().any(|v|v["health"]=="healthy");
    Json(json!({"status":if healthy{"ok"}else{"degraded"},"runtime":"rust","process_id":std::process::id(),"sources":sources,"database":database,"ingress":s.acquisition.frames.status(),
        "acquisition":{"state":s.acquisition.projection_status.borrow()["state"],"routes":s.market.routes.lock().expect("routes lock").clone()},
        "history":{"mode":"realtime_source_bound_cache","governance":"frozen","cross_source_fallback":false,"upstream_calls_on_read":false,
            "live_bar_count":s.market.state.lock().expect("state lock").reducer.live_count(),"recovery":s.acquisition.projection_status.borrow().clone()}}))
}
fn source_descriptors(s:&AppState)->Vec<Value>{
    [("jin10_client","金十客户端",s.acquisition.web_status.borrow().clone()),
        ("tonghuashun_futures","同花顺公开行情",s.acquisition.ths_status.borrow().clone())].into_iter().map(|(id,name,status)|{
        let streaming=id=="jin10_client";let connected=status.state=="connected";
        let transport_active=connected||(!streaming&&matches!(status.state.as_str(),"degraded"|"stale"));
        json!({"source_id":id,"display_name":name,"description":if streaming{"实时价格与同源历史行情"}else{"公开快照与分钟历史；各报价通道保留独立身份和时间精度"},
            "capabilities":["quote","candles"],"history_backfill_configured":true,"selectable":true,"delayed":!streaming,
            "requires_running_app":streaming,"structured":true,"quote_poll_interval_seconds":if streaming{None}else{Some(5)},
            "quote_timestamp_precision_seconds":if streaming{json!(1)}else{Value::Null},"quote_streaming":streaming,"quote_service_tier":"public","channels":if streaming{json!(["jin10_web","jin10_local"])}else{json!([{"channel":"tonghuashun_public_time_v6","timestamp_precision_ns":"60000000000","capabilities":["snapshot","minute_history"]},{"channel":"tonghuashun_fuyao","timestamp_precision_ns":"1000000","capabilities":["snapshot","recent_minute_history"],"exchange_tick_coverage":false}])},
            "access_model":if streaming{"local_session"}else{"public"},"access_note":null,"manual_connection_required":false,
            "connection_active":transport_active,"quotas":[],"health":if connected{"healthy"}else if status.state=="degraded"{"degraded"}else{"unavailable"},"state":status.state,
            "error":status.error,"checked_at":Utc::now(),"last_success_at":null})
    }).collect()
}
async fn sources(State(s):State<AppState>)->Json<Value>{Json(json!(source_descriptors(&s)))}
async fn instruments(State(s):State<AppState>)->Json<Value>{Json(json!(s.market.catalog.items.iter().map(|d|s.market.catalog.public(d)).collect::<Vec<_>>()))}
fn watchlist_payload(s:&AppState)->Value{json!(s.market.watchlist.lock().expect("watchlist lock").iter().filter_map(|c|s.market.catalog.get(c).ok()).map(|d|s.market.catalog.public(d)).collect::<Vec<_>>())}
async fn watchlist(State(s):State<AppState>)->Json<Value>{Json(watchlist_payload(&s))}
async fn add_watchlist(State(s):State<AppState>,Path(code):Path<String>)->ApiResult{
    definition(&s,&code)?;s.market.change_watchlist(&code,true).await?;
    s.acquisition.reconcile(&s.market);Ok(Json(watchlist_payload(&s)))
}
async fn remove_watchlist(State(s):State<AppState>,Path(code):Path<String>)->ApiResult{
    definition(&s,&code)?;
    s.market.change_watchlist(&code,false).await.map_err(|error|if error.to_string().contains("watchlist_minimum_one"){ApiError(StatusCode::CONFLICT,"观察列表至少保留一个品种".into())}else{error.into()})?;
    s.acquisition.reconcile(&s.market);Ok(Json(watchlist_payload(&s)))
}
async fn instrument_source(State(s):State<AppState>,Path(code):Path<String>)->ApiResult{
    let d=definition(&s,&code)?;Ok(Json(json!({"code":d.code,"source_id":s.market.source(&d.instrument.symbol)?})))
}
#[derive(Deserialize)]#[serde(deny_unknown_fields)]struct SourceUpdate{source_id:String}
async fn update_source(State(s):State<AppState>,Path(code):Path<String>,Json(update):Json<SourceUpdate>)->ApiResult{
    let d=definition(&s,&code)?;
    if !d.source_ids.contains(&update.source_id){return Err(ApiError(StatusCode::BAD_REQUEST,"该品种不支持此数据源".into()))}
    let version=s.market.change_source(&code,&update.source_id).await?;
    s.acquisition.reconcile(&s.market);Ok(Json(json!({"code":d.code,"source_id":update.source_id,"snapshot_version":version})))
}
async fn quote(State(s):State<AppState>,Path(code):Path<String>)->ApiResult{definition(&s,&code)?;Ok(Json(s.market.quote_view(&code,false)?))}
async fn last_quote(State(s):State<AppState>,Path(code):Path<String>)->ApiResult{definition(&s,&code)?;Ok(Json(s.market.quote_view(&code,true)?))}
#[derive(Deserialize,Default)]pub struct BarQuery{pub period:Option<String>,pub before:Option<i64>,pub cursor:Option<String>,pub page_size:Option<usize>}
async fn bars(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<BarQuery>)->Result<Response,ApiError>{
    let d=definition(&s,&code)?;let source=s.market.source(&d.instrument.symbol)?;let period=checked_period(q.period.as_deref())?;
    checked_calendar(d,period)?;
    let schedule=pages::schedule(&s.market,&code)?;
    let boundary=pages::resolve_boundary(q.cursor.as_deref(),q.before,&d.instrument,&source,period,Some(&schedule)).map_err(|e|invalid(e.to_string()))?;
    let page=pages::chart_page(&s.market,&code,period,boundary,count(q.page_size,500,10000)?).await?;
    let timings=page.timings.clone();let rows=page.items.len();let encode_started=Instant::now();
    let body=serde_json::to_vec(&pages::page_payload(page,&d.instrument,&source,period,Some(&schedule))?).map_err(anyhow::Error::from)?;
    let encoded_ms=encode_started.elapsed().as_secs_f64()*1000.0;
    let mut headers=axum::http::HeaderMap::new();headers.insert(axum::http::header::CONTENT_TYPE,axum::http::HeaderValue::from_static("application/json"));
    headers.insert("server-timing",axum::http::HeaderValue::from_str(&format!("store;dur={:.3}, read_view;dur={:.3}, calendar_query;dur={:.3}, calendar_coverage;dur={:.3}, page_context;dur={:.3}, canonical_dto;dur={:.3}, page_dto;dur={:.3}, response_encode;dur={:.3}",timings.store_ms,timings.read_view_ms,timings.calendar_query_ms,timings.calendar_coverage_ms,(timings.context_ms-timings.calendar_coverage_ms).max(0.0),timings.canonical_dto_ms,timings.page_dto_ms,encoded_ms)).expect("finite ASCII timing header"));
    headers.insert("x-tracefang-page-rows",axum::http::HeaderValue::from_str(&rows.to_string()).expect("bounded row count header"));
    Ok((headers,body).into_response())
}
#[derive(Deserialize)]struct BarRangeQuery {period:Option<String>,source_id:String,start:DateTime<Utc>,end:DateTime<Utc>,max_rows:Option<usize>}
async fn bars_range(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<BarRangeQuery>)->ApiResult {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,CanonicalScanRequest,BarSelection};
    let d=definition(&s,&code)?;let period=checked_period(q.period.as_deref())?;
    checked_calendar(d,period)?;
    if period==Period::Timeline || !d.source_ids.contains(&q.source_id){return Err(invalid("无效的周期或数据源"));}
    let limit=count(q.max_rows,10000,10000)?;let start=crate::store::ns(q.start)?;let end=crate::store::ns(q.end)?;
    if start>=end{return Err(invalid("范围须为递增的半开区间"));}
    let (version,values,semantics)=if period.is_base() {
        let view=s.market.store.canonical_snapshot(CanonicalSnapshotRequest {symbol:d.instrument.symbol.clone(),source_id:q.source_id.clone(),period:period.as_str().into(),selection:BarSelection::Range {start_ns:start,end_ns:end,max_rows:limit},final_only:false,expected_version:None}).await?;
        (view.version,view.bars,view.semantics)
    }else{
        let output=Arc::new(std::sync::Mutex::new(Vec::new()));let captured=output.clone();
        let summary=s.market.store.canonical_calendar_scan(CanonicalScanRequest {symbol:d.instrument.symbol.clone(),source_id:q.source_id.clone(),interval_seconds:60,start_ns:start,end_ns:end,final_only:false,expected_version:None},period,Some(pages::schedule(&s.market,&code)?),512,None,move|batch| {
            let mut rows=captured.lock().map_err(|_|anyhow::anyhow!("range materialization lock poisoned"))?;
            anyhow::ensure!(rows.len()+batch.rows.len()<=limit,"range exceeds max_rows; narrow the range or continue by bounded calendar pages");
            for row in batch.rows {rows.push(tracefang_core::native_store::bar_to_value(row)?);}Ok(())
        }).await?;
        let values=std::mem::take(&mut *output.lock().map_err(|_|invalid("范围读取失败"))?);
        (summary.version,values,"final_revision_history".into())
    };
    let items=values.into_iter().map(|v|crate::catalog::database_bar(v,&d.instrument)).collect::<anyhow::Result<Vec<_>>>()?;
    Ok(Json(json!({"items":items,"snapshot_version":version,"semantics":semantics,"complete":true,"source_id":q.source_id,"period_id":period.as_str(),"start_ns":start.to_string(),"end_ns":end.to_string()})))
}
#[derive(Deserialize)]struct HistoryQuery{period:Option<String>,cursor:String,count_back:Option<usize>}
async fn history_page(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<HistoryQuery>)->ApiResult{
    let d=definition(&s,&code)?;let source=s.market.source(&d.instrument.symbol)?;let period=checked_period(q.period.as_deref())?;
    let schedule=pages::schedule(&s.market,&code)?;
    let before=pages::resolve_boundary(Some(&q.cursor),None,&d.instrument,&source,period,Some(&schedule))
        .map_err(|e|invalid(e.to_string()))?.ok_or_else(||invalid("cursor 不能为空"))?;
    Ok(Json(s.history.ensure_older(&code,period,before,count(q.count_back,240,10000)?).await?))
}
#[derive(Deserialize)]struct BackfillQuery{time:i64,count:Option<usize>,#[serde(default)]revalidate:bool}
async fn backfill(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<BackfillQuery>)->ApiResult{
    let d=definition(&s,&code)?;if !d.history_backfill_supported{return Err(ApiError(StatusCode::CONFLICT,"该换算品种不提供历史回补".into()))}
    let time=DateTime::from_timestamp(q.time,0).ok_or_else(||invalid("time 超出范围"))?;
    Ok(Json(json!(s.history.backfill(&code,time,count(q.count,1000,10000)?,q.revalidate).await?)))
}
#[derive(Deserialize)]struct CandleQuery{count:Option<usize>,time:Option<i64>}
async fn candles(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<CandleQuery>)->ApiResult{
    let d=definition(&s,&code)?;let n=count(q.count,100,2000)?;let source=s.market.source(&d.instrument.symbol)?;
    if let Some(time)=q.time {
        let start=DateTime::from_timestamp(time,0).ok_or_else(||invalid("time 超出范围"))?;
        let rows=s.market.store.bars_range(&d.instrument.symbol,&source,60,start,start+chrono::Duration::minutes(n as i64)).await?;
        let bars=rows.into_iter().map(|v|crate::catalog::database_bar(v,&d.instrument)).collect::<anyhow::Result<Vec<_>>>()?;Ok(Json(json!(bars)))
    }else{Ok(Json(json!(pages::chart_page(&s.market,&code,Period::M1,None,n).await?.items)))}
}
#[derive(Deserialize)]struct TimelineQuery{cursor:Option<u64>,page_size:Option<usize>}
async fn timeline(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<TimelineQuery>)->ApiResult{
    let d=definition(&s,&code)?;let source=s.market.source(&d.instrument.symbol)?;let limit=count(q.page_size,20000,20000)?;
    if q.cursor.is_some_and(|v|v<1){return Err(invalid("cursor 须为正数"))}
    let channels=if source=="jin10_client"{vec!["jin10_web".into()]}else{vec![source.clone()]};
    let mut rows=s.market.store.timeline(&d.instrument.symbol,&channels,q.cursor,limit+1).await?;
    let has_more=rows.len()>limit;if has_more {rows.pop();}
    let next=rows.last().map(|v|v["storage_id"].clone());
    rows.reverse();
    let items=rows.into_iter().map(|r|json!({"source_id":source,"channel_id":r["source_id"],"event_id":r["event_id"],"instrument":d.instrument,
        "provider_symbol":r["provider_symbol"],"observed_at":r["observed_at"],"received_at":r["received_at"],"value":r["last"],"observation_kind":r["observation_kind"],"storage_id":r["storage_id"],"application_order":r["application_order"],"applied_capture":r["applied_capture"],"timeline_semantics":r["timeline_semantics"]})).collect::<Vec<_>>();
    Ok(Json(json!({"source_id":source,"items":items,"next_cursor":next,"has_more":has_more})))
}
#[derive(Deserialize)]struct SourceTestQuery{code:Option<String>}
async fn test_source(State(s):State<AppState>,Path(source):Path<String>,Query(q):Query<SourceTestQuery>)->ApiResult{
    let started=Instant::now();let code=q.code.as_deref().unwrap_or("XAUUSD");let d=definition(&s,code)?;
    if !d.source_ids.contains(&source){return Err(ApiError(StatusCode::BAD_REQUEST,"此来源不支持该品种".into()))}
    if source!="tonghuashun_futures" || s.market.store.read_only(){
        return Ok(Json(json!({"source_id":source,"code":d.code,"state":"not_tested","validation_performed":false,"data_fresh":false,"latency_ms":null,"detail":if s.market.store.read_only(){"只读核验模式未启动采集验证"}else{"此来源尚未提供独立主动验证；现有其它来源报价不能作为验证结果"}})));
    }
    let mut work=s.acquisition.frames.reserve().await?;
    let frame=providers::http_frame_reserved(&s.http,d,"time","last.js",&uuid::Uuid::new_v4().simple().to_string(),1,&mut work).await?;
    // Decode the exact probe for its own evidence; it is still captured before
    // reporting either valid price or an upstream/decode failure.
    let decoded=providers::Decoder::new(s.market.catalog.clone()).decode(&frame);
    let receipt=s.acquisition.frames.append(frame,work).await?;s.acquisition.wait_projected(receipt.position.sequence).await?;
    let quote=decoded.as_ref().ok().and_then(|(quotes,_)|quotes.iter().find(|quote|quote.instrument==d.instrument && quote.source.provider==source));
    let fresh=quote.is_some_and(|q|q.source.is_fresh(Utc::now(),12));
    let version=s.market.store.version().await?;
    let bars=s.market.hot_bars(&d.instrument.symbol,&source,60)?;
    Ok(Json(json!({"source_id":source,"code":d.code,"state":if quote.is_none(){"unavailable"}else if fresh{"connected"}else{"stale"},"validation_performed":true,"validation_kind":"captured_price_probe","capture_position":receipt.position,"projection_version":version,"detail":decoded.as_ref().err().map(|e|e.to_string()),
        "history_backfill_configured":true,"history_validated":false,"data_fresh":fresh,"last":quote.map(|v|v.last.to_string()),"observed_at":quote.map(|v|v.source.observed_at),"received_at":quote.map(|v|v.source.received_at),
        "latency_ms":started.elapsed().as_millis().max(1).to_string(),"kline_points":bars.len(),"kline_open_time":bars.last().map(|b|b.open_time)})))
}
async fn replay_bounds(State(s):State<AppState>)->ApiResult{
    let mut bounds=s.capture.bounds().await?;
    let sources=if bounds["state"]=="empty"{Vec::new()}else{s.market.catalog.items.iter().flat_map(|d|d.source_ids.iter().cloned()).collect::<std::collections::BTreeSet<_>>().into_iter().collect::<Vec<_>>()};
    bounds["source_ids"]=json!(sources);bounds["source_scope_semantics"]=json!("configured decodable replay scopes; captured instrument coverage is separately evidenced");
    Ok(Json(bounds))
}
#[derive(Deserialize)]struct CursorQuery{sequence:u64}
async fn replay_cursor(State(s):State<AppState>,Query(q):Query<CursorQuery>)->ApiResult{
    if q.sequence==0{return Err(invalid("sequence 须为正数"))}
    let frame=s.capture.get(q.sequence).await.map_err(|_|ApiError(StatusCode::NOT_FOUND,"原始帧不存在或已超出保留期".into()))?;
    Ok(Json(frame.dto()))
}
async fn options(State(s):State<AppState>)->ApiResult{Ok(Json(options_snapshot(&s).await?))}
async fn option_products()->Json<Value>{Json(json!({"provider_id":"shfe_official_delayed","delivery_mode":"exchange_delayed","items":providers::shfe::OPTION_PRODUCTS,"capabilities":["product_delayed_snapshot","official_contract_metadata"],"quote_coverage":"reported per product snapshot; master membership alone is not quote availability"}))}
async fn product_options(State(s):State<AppState>,Path(product):Path<String>)->ApiResult{
    if !providers::shfe::OPTION_PRODUCTS.contains(&product.as_str()){return Err(invalid("不支持该上期所期权产品"));}
    Ok(Json(options_product_snapshot(&s,&product).await?))
}
pub async fn options_snapshot(s:&AppState)->anyhow::Result<Value>{
    options_product_snapshot(s,"au").await
}
pub async fn options_product_snapshot(s:&AppState,product:&str)->anyhow::Result<Value>{
    cached_option_product(&s.options_cache,product,||async {
    let base=std::env::var("TRACEFANG_SHFE_BASE_URL").unwrap_or_else(|_|"https://www.shfe.com.cn".into());
    match providers::shfe::fetch_chain(&s.http,&base,product).await {
        Ok(chain)=>analysis::options::product_snapshot(product,Some(&chain),None,Utc::now()),
        Err(_)=>analysis::options::product_snapshot(product,None,Some("上期所期权行情获取失败"),Utc::now()),
    }.map_err(anyhow::Error::from)}).await
}
async fn cached_option_product<F,Fut>(cache:&Arc<Mutex<BTreeMap<String,(Instant,Value)>>>,product:&str,fetch:F)->anyhow::Result<Value>
where F:FnOnce()->Fut,Fut:std::future::Future<Output=anyhow::Result<Value>> {
    anyhow::ensure!(providers::shfe::OPTION_PRODUCTS.contains(&product),"unsupported SHFE option product");
    // The fixed product set bounds single-flight state. A slow product owns
    // only its own gate; the shared cache lock never covers network I/O.
    static GATES:std::sync::OnceLock<BTreeMap<&'static str,Mutex<()>>>=std::sync::OnceLock::new();
    let gates=GATES.get_or_init(||providers::shfe::OPTION_PRODUCTS.iter().map(|product|(*product,Mutex::new(()))).collect());
    {let values=cache.lock().await;if let Some((at,value))=values.get(product){if at.elapsed()<Duration::from_secs(60){return Ok(value.clone())}}}
    let _product=gates.get(product).expect("validated fixed option product").lock().await;
    {let values=cache.lock().await;if let Some((at,value))=values.get(product){if at.elapsed()<Duration::from_secs(60){return Ok(value.clone())}}}
    let snapshot=fetch().await?;
    cache.lock().await.insert(product.into(),(Instant::now(),snapshot.clone()));Ok(snapshot)
}
#[cfg(test)]mod options_concurrency_tests {
    use super::*;
    #[tokio::test]async fn slow_product_does_not_block_another_product_and_same_product_is_deduplicated()->anyhow::Result<()> {
        let cache=Arc::new(Mutex::new(BTreeMap::new()));let started=Arc::new(tokio::sync::Notify::new());let release=Arc::new(tokio::sync::Notify::new());let requests=Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let first=tokio::spawn({let cache=cache.clone();let started=started.clone();let release=release.clone();let requests=requests.clone();async move {cached_option_product(&cache,"au",||async {requests.fetch_add(1,std::sync::atomic::Ordering::SeqCst);started.notify_one();release.notified().await;Ok(json!({"product":"au","response":"one"}))}).await}});
        started.notified().await;
        let second=tokio::spawn({let cache=cache.clone();let requests=requests.clone();async move {cached_option_product(&cache,"au",||async {requests.fetch_add(1,std::sync::atomic::Ordering::SeqCst);Ok(json!({"response":"unexpected duplicate"}))}).await}});
        let different=tokio::time::timeout(Duration::from_millis(200),cached_option_product(&cache,"ag",||async {Ok(json!({"product":"ag"}))})).await??;
        assert_eq!(different["product"],"ag");assert!(!first.is_finished());release.notify_one();let a=first.await??;let b=second.await??;
        assert_eq!(a,b);assert_eq!(requests.load(std::sync::atomic::Ordering::SeqCst),1);Ok(())
    }
}
async fn stream(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<BarQuery>,ws:WebSocketUpgrade)->Result<Response,ApiError>{
    definition(&s,&code)?;let mut period=checked_period(q.period.as_deref())?;if period==Period::Timeline{period=Period::S1;}
    Ok(ws.on_upgrade(move|socket|live_socket(socket,s,code,period)))
}
async fn send(socket:&mut WebSocket,value:Value)->bool{socket.send(Message::Text(value.to_string().into())).await.is_ok()}
async fn live_socket(mut socket:WebSocket,mut s:AppState,code:String,period:Period){
    let Ok(d)=s.market.catalog.get(&code) else{return};let instrument:Instrument=d.instrument.clone();
    let Ok(source)=s.market.source(&instrument.symbol) else{return};
    let mut receiver=s.market.streams.subscribe(&source,&instrument.symbol,period.as_str());
    if let Err(error)=pages::prepare_live_period(&s.market,&code,period).await {
        tracing::warn!(%error,"live period preparation failed");
        send(&mut socket,json!({"kind":"status","state":"unavailable","error":"历史前缀准备失败","period_id":period.as_str()})).await;return;
    }
    if let Ok(quote)=s.market.quote_view(&code,true){if !send(&mut socket,json!({"kind":"quote","state":if quote["stale_fields"].as_array().is_some_and(|a|a.iter().any(|v|v=="last")){"unavailable"}else{"live"},"quote":quote,"period_id":period.as_str(),"emitted_at":Utc::now()})).await{return}}
    let mut delivered=0u64;let mut heartbeat=tokio::time::interval(Duration::from_secs(10));
    loop {
        tokio::select!{
            _=s.shutdown.changed()=>{let _=socket.send(Message::Close(Some(CloseFrame{code:1012,reason:"service restart".into()}))).await;break},
            inbound=socket.next()=>match inbound {
                Some(Ok(Message::Ping(v)))=>{if socket.send(Message::Pong(v)).await.is_err(){break}},
                Some(Ok(Message::Close(_)))|None|Some(Err(_))=>break,_=>{},
            },
            event=receiver.recv()=>match event {
                Ok(value)=>{delivered=value["delivery_sequence"].as_str().and_then(|v|v.parse().ok()).or_else(||value["delivery_sequence"].as_u64()).unwrap_or(delivered);
                    if value["kind"]=="period_tail_changed" && !period.is_base(){match pages::chart_page(&s.market,&code,period,None,2).await {
                        Ok(page)=>{for bar in &page.items {if !send(&mut socket,json!({"kind":"bar","state":"committed","period_id":period.as_str(),"bar":bar,"snapshot_version":page.snapshot_version})).await{return}}},
                        Err(_)=>{if !send(&mut socket,json!({"kind":"status","state":"unavailable","error":"周期事实读取失败"})).await{break}},
                    }}else if !send(&mut socket,value).await{break}},
                Err(tokio::sync::broadcast::error::RecvError::Lagged(count))=>{
                    let to=delivered.saturating_add(count);
                    if !send(&mut socket,json!({"kind":"gap","state":"unavailable","period_id":period.as_str(),"gap_from_sequence":delivered.saturating_add(1).to_string(),"gap_to_sequence":to.to_string(),"error":"客户端接收落后，请重新读取本周期","emitted_at":Utc::now()})).await{break}delivered=to;
                },Err(_)=>break,
            },
            _=heartbeat.tick()=>{
                let live=s.market.quote_view(&code,false).is_ok();
                if !send(&mut socket,json!({"kind":"status","state":if live{"live"}else{"unavailable"},"error":if live{None}else{Some("所选来源当前没有新鲜报价")},"period_id":period.as_str(),"emitted_at":Utc::now()})).await{break}
            },
        }
    }
}
