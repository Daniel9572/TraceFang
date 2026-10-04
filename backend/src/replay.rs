//! Disposable deterministic projections. Seeking rebuilds from retained evidence;
//! no current live cache, database bars or future quote is admitted into replay.
use std::{sync::{Arc,atomic::{AtomicBool,Ordering}},time::Duration};
include!(concat!(env!("OUT_DIR"),"/backend_build_inputs.rs"));
use anyhow::{Result,Context,ensure};
use axum::{Router,extract::{State,Path,Query,WebSocketUpgrade,ws::{WebSocket,Message}},response::Response,routing::get};
#[path="replay_checkpoint.rs"] mod checkpoint;
#[path="replay_quant.rs"]mod quant;
#[path="quant_bar_adapter.rs"]pub mod bar_adapter;
use checkpoint::{Checkpoint,ReplayScope,REPLAY_VERSION,state_hash};
use sha2::{Digest,Sha256};
use std::{collections::BTreeMap,sync::LazyLock};
static REPLAY_DECODE:LazyLock<Arc<tokio::sync::Semaphore>>=LazyLock::new(||Arc::new(tokio::sync::Semaphore::new(1)));
use futures_util::StreamExt;
use serde::Deserialize;
use serde_json::{Value,json};
use chrono::{DateTime,Utc};
use tracefang_core::{domain::Instrument,events::{MarketEvent,RealtimeBar,QuoteEvent},reducer::{BarReducer,BarContract},periods::{Period,MarketSchedule,LivePeriodProjector}};
use crate::{api::{AppState,ApiError},catalog::Catalog,capture::{ProviderFrame,Capture,CapturedFrame},providers::Decoder,quotes::QuoteCache,pages};

pub struct Projector {
    decoder:Decoder,reducer:BarReducer,periods:LivePeriodProjector,quotes:QuoteCache,
    catalog:Arc<Catalog>,instrument:Instrument,source:String,period:Period,schedule:MarketSchedule,
    warmup:Option<DateTime<Utc>>,view:BTreeMap<String,Value>,
    facts_mode:bool,cached_context_mode:bool,pending_calendar_context:Option<Value>,pending_bars:Vec<Value>,pending_quotes:Vec<Value>,pending_errors:Vec<Value>,
}
type Decoded=Result<(Vec<tracefang_core::domain::QuoteSnapshot>,Vec<tracefang_core::domain::Candle>)>;
type BarKey=(String,String,i64,DateTime<Utc>);
fn bar_key(bar:&RealtimeBar)->BarKey{(bar.source.provider.clone(),bar.instrument.symbol.clone(),bar.interval_seconds,bar.open_time)}
impl Projector {
    pub fn new(catalog:Arc<Catalog>,instrument:Instrument,source:String,period:Period,schedule:MarketSchedule)->Result<Self>{
        Ok(Self{decoder:Decoder::new(catalog.clone()),catalog,instrument,source,period,schedule,warmup:None,view:BTreeMap::new(),facts_mode:false,cached_context_mode:false,pending_calendar_context:None,pending_bars:vec![],pending_quotes:vec![],pending_errors:vec![],
            quotes:Default::default(),periods:Default::default(),reducer:BarReducer::new(vec![
                BarContract::new("jin10_client","jin10_local",vec!["jin10_web".into()]),
                BarContract::new("tonghuashun_futures","tonghuashun_futures",vec!["tonghuashun_futures".into()]),])?})
    }
    pub fn new_facts(catalog:Arc<Catalog>,instrument:Instrument,source:String,period:Period,schedule:MarketSchedule)->Result<Self>{let mut value=Self::new(catalog,instrument,source,period,schedule)?;value.facts_mode=true;Ok(value)}
    /// Base facts remain complete even when the chart view retains only a bounded page.
    pub fn take_projection(&mut self,position:tracefang_core::persistence_contract::CapturePosition)->Result<tracefang_core::persistence_contract::ProjectionCommit> {
        let mut bars=BTreeMap::new();for bar in std::mem::take(&mut self.pending_bars){let key=(bar["source"]["provider"].to_string(),bar["instrument"]["symbol"].to_string(),bar["interval"].to_string(),bar["open_time"].to_string());bars.insert(key,bar);}
        let decoder_state=if self.cached_context_mode{self.pending_calendar_context.take()}else{Some(self.decoder.snapshot()?)};
        Ok(tracefang_core::persistence_contract::ProjectionCommit{position,bars:bars.into_values().collect(),quotes:std::mem::take(&mut self.pending_quotes),errors:std::mem::take(&mut self.pending_errors),decoder_state})
    }
    fn queue_quote(&mut self,event:&QuoteEvent)->Result<()> {
        if self.facts_mode{let mut value=serde_json::to_value(&event.quote)?;value["event_id"]=json!(event.sample().event_id);value["source"]["provider"]=json!(event.source_id);value["source"]["raw_payload"]["channel"]=json!(event.channel_id);self.pending_quotes.push(value);}Ok(())
    }
    pub fn snapshot(&self)->Result<Value> {
        ensure!(!self.cached_context_mode,"cached reconciliation projector is not a complete Decoder recovery checkpoint");
        ensure!(self.pending_bars.is_empty()&&self.pending_quotes.is_empty()&&self.pending_errors.is_empty(),"replay checkpoint cannot omit uncommitted projection facts");
        Ok(json!({"decoder":self.decoder.snapshot()?,"reducer":self.reducer.snapshot()?,"periods":self.periods.snapshot()?,
            "quotes":self.quotes.snapshot()?,"warmup":self.warmup,"view":self.view,"facts_mode":self.facts_mode}))
    }
    pub fn restore(&mut self,state:Value)->Result<()> {
        ensure!(state["facts_mode"].as_bool().unwrap_or(false)==self.facts_mode,"market-only checkpoint cannot restore complete facts replay");
        self.decoder=Decoder::restore(self.catalog.clone(),state["decoder"].clone())?;
        self.reducer=BarReducer::restore(state["reducer"].clone())?;
        self.periods=LivePeriodProjector::restore(state["periods"].clone())?;
        self.quotes=QuoteCache::restore(state["quotes"].clone())?;
        self.warmup=serde_json::from_value(state["warmup"].clone())?;
        self.view=serde_json::from_value(state["view"].clone())?;Ok(())
    }
    pub fn accept(&mut self,sequence:u64,frame:&ProviderFrame)->Result<Vec<Value>>{self.accept_inner(sequence,frame,None)}
    pub fn accept_record(&mut self,record:&CapturedFrame)->Result<Vec<Value>>{self.accept_inner(record.position.sequence,&record.frame,Some(record))}
    fn accept_inner(&mut self,sequence:u64,frame:&ProviderFrame,record:Option<&CapturedFrame>)->Result<Vec<Value>>{
        let decoded=self.decode_frame(frame,record);self.apply_decoded(sequence,frame,record,decoded,vec![])
    }
    fn decode_frame(&mut self,frame:&ProviderFrame,record:Option<&CapturedFrame>)->Decoded{
        let (mut quotes,mut bars)=if let Some(record)=record{self.decoder.decode_record(record)?}else{self.decoder.decode(frame)?};
        if let Some(record)=record {for quote in &mut quotes{capture_provenance(&mut quote.source,record);}for bar in &mut bars{capture_provenance(&mut bar.source,record);}}
        for quote in &quotes {quote.validate()?;}for bar in &bars{bar.validate()?;}Ok((quotes,bars))
    }
    fn apply_decoded(&mut self,sequence:u64,frame:&ProviderFrame,record:Option<&CapturedFrame>,decoded:Decoded,current:Vec<RealtimeBar>)->Result<Vec<Value>>{
        let mut current=current.into_iter().map(|bar|(bar_key(&bar),bar)).collect::<BTreeMap<_,_>>();
        let base=json!({"stream_sequence":sequence.to_string(),"frame_received_at":frame.received_at,"frame_received_at_ns":frame.received_at.timestamp_nanos_opt().map(|v|v.to_string()),
            "provider_sequence":frame.sequence.to_string(),"connection_id":frame.connection_id,"frame_channel":frame.channel,
            "period_id":self.period.as_str(),"source_id":self.source});
        let mut output=vec![event(&base,"frame",None,None)];
        let (quotes,bars)=match decoded{Ok(v)=>v,Err(error)=>{
            let mut e=event(&base,"decode_error",None,None);e["error"]=json!("原始帧解析失败，未生成价格");if self.facts_mode{
                // Private projection evidence retains the precise failure and raw
                // identity; the public stream keeps its concise display message.
                let mut diagnostic=e.clone();diagnostic["decode_diagnostic"]=json!(error.to_string());
                if let Some(record)=record{diagnostic["capture_position"]=json!(record.position);diagnostic["legacy_origin"]=json!(record.legacy);}
                self.pending_errors.push(diagnostic)
            }output.push(e);return Ok(output)
        }};
        for quote in quotes {
            let changed=self.quotes.accept(quote.clone());
            if quote.instrument==self.instrument {
                if let Some(normalized)=self.reducer.normalize_quote(quote.clone())? {
                    if normalized.source_id==self.source {
                        self.queue_quote(&normalized)?;
                        let transitions=self.apply_market(MarketEvent::Quote(normalized),&mut current)?;
                        self.emit_bars(transitions,&base,&mut output)?;
                        output.push(event(&base,"quote",Some(serde_json::to_value(&quote)?),None));
                    }
                }
            }
            if changed&&self.catalog.get(&self.instrument.symbol)?.quote_kind=="derived" {
                let d=self.catalog.get(&self.instrument.symbol)?;
                if let Ok(view)=self.quotes.view(d,&self.source,frame.received_at,false) {
                    let mut derived:tracefang_core::domain::QuoteSnapshot=serde_json::from_value(view["quote"].clone())?;if let Some(record)=record{capture_provenance(&mut derived.source,record);}
                    let normalized=QuoteEvent{source_id:self.source.clone(),channel_id:"jin10_web".into(),quote:derived,sequence:None};self.queue_quote(&normalized)?;
                    let transitions=self.apply_market(MarketEvent::Quote(normalized),&mut current)?;
                    self.emit_bars(transitions,&base,&mut output)?;
                    output.push(event(&base,"quote",Some(view["quote"].clone()),None));
                }
            }
        }
        for bar in bars {
            if bar.instrument!=self.instrument{continue}
            if let Some(normalized)=self.reducer.normalize_bar(bar)? {
                if normalized.source_id!=self.source{continue}
                let transitions=self.apply_market(MarketEvent::Bar(normalized),&mut current)?;
                self.emit_bars(transitions,&base,&mut output)?;
            }
        }
        if self.facts_mode{if let Some(record)=record{for bar in &mut self.pending_bars{let mut source=serde_json::from_value(bar["source"].clone())?;capture_provenance(&mut source,record);bar["source"]=serde_json::to_value(source)?;}}}
        Ok(output)
    }
    fn apply_market(&mut self,event:MarketEvent,current:&mut BTreeMap<BarKey,RealtimeBar>)->Result<Vec<RealtimeBar>>{
        let keys=match &event {MarketEvent::Bar(v)=>vec![(v.source_id.clone(),v.candle.instrument.symbol.clone(),v.candle.interval_seconds,v.candle.open_time)],MarketEvent::Quote(v)=>[1,60].into_iter().map(|interval|Ok((v.source_id.clone(),v.quote.instrument.symbol.clone(),interval,tracefang_core::reducer::floor_time(v.quote.source.observed_at,interval)?))).collect::<Result<_>>()?};
        let rows=keys.into_iter().filter_map(|key|current.get(&key).cloned()).collect();
        let transitions=self.reducer.apply_with_current(event,rows)?;for row in &transitions{current.insert(bar_key(row),row.clone());}Ok(transitions)
    }
    fn emit_bars(&mut self,bars:Vec<RealtimeBar>,base:&Value,output:&mut Vec<Value>)->Result<()> {
        for bar in bars {
            if self.facts_mode{self.pending_bars.push(serde_json::to_value(&bar)?);}
            let values=if (self.period==Period::S1&&bar.interval_seconds==1)||(self.period==Period::M1&&bar.interval_seconds==60){vec![bar]}
                else if !self.facts_mode&&!self.period.is_base()&&bar.interval_seconds==60{self.periods.accept(bar,Some(&self.schedule),&[self.period])?.into_iter().map(|(_,b)|b).collect()}
                else{vec![]};
            for value in values {
                let first=*self.warmup.get_or_insert(value.open_time);
                // An initial quote cannot establish the full OHLC of its partial bucket.
                if value.open_time==first&&(!self.period.is_base()||value.evidence_channel_id=="jin10_web"){continue}
                let bar=serde_json::to_value(value)?;
                if !self.facts_mode{if let Some(key)=bar["open_time"].as_str(){self.view.insert(key.to_owned(),bar.clone());}}
                while self.view.len()>10000{self.view.pop_first();}
                output.push(event(base,"bar",None,Some(bar)));
            }
        }Ok(())
    }
}
fn capture_provenance(source:&mut tracefang_core::domain::SourceMetadata,record:&CapturedFrame){
    let raw=source.raw_payload.get_or_insert_with(||json!({}));if !raw.is_object(){*raw=json!({"provider_raw_payload":raw.take()});}
    if raw["observation_kind"]=="supplement"{raw["supplement_capture_position"]=json!(record.position);raw["supplement_capture_accepted_at_ns"]=json!(record.legacy.is_none().then(||record.accepted_at_ns.to_string()));return;}
    raw["capture_epoch"]=json!(record.position.epoch);raw["capture_sequence"]=json!(record.position.sequence.to_string());raw["capture_digest"]=json!(record.position.digest);
    raw["capture_accepted_at_ns"]=if record.legacy.is_none(){json!(record.accepted_at_ns.to_string())}else{Value::Null};raw["accepted_at_ns"]=raw["capture_accepted_at_ns"].clone();
    if raw["authoritative_input"].is_object(){raw["authoritative_input"]["capture_position"]=json!(record.position);}
    if let Some(origin)=&record.legacy{raw["imported_at_ns"]=json!(record.accepted_at_ns.to_string());raw["legacy_broker_stored_at_ns"]=json!(origin.broker_stored_at_ns);}
}

fn event(base:&Value,kind:&str,quote:Option<Value>,bar:Option<Value>)->Value {
    let mut value=base.clone();value["kind"]=json!(kind);value["quote"]=json!(quote);value["bar"]=json!(bar);value["error"]=Value::Null;exact_public(value)
}

/// Convert exact business numbers for JS; display coordinate approximations stay a frontend concern.
fn exact_public(mut value:Value)->Value {
    fn visit(v:&mut Value){match v {Value::Array(rows)=>for row in rows{visit(row)},Value::Object(map)=>for (key,row) in map{
        if matches!(key.as_str(),"open"|"high"|"low"|"close"|"last"|"volume"|"change"|"change_percent"|"revision"|"sequence"|"received_sequence"|"known_volume_sum")&&row.is_number(){*row=Value::String(row.to_string())}else{visit(row)}
    },_=>{}}}visit(&mut value);value
}
#[derive(Deserialize)]struct ReplayQuery{period:Option<String>,source_id:Option<String>,start_sequence:Option<String>,end_sequence:Option<String>,received_at_ns:Option<String>,paused:Option<bool>}
pub fn router()->Router<AppState>{Router::new().route("/api/replay/stream/{code}",get(upgrade)).route("/api/replay/quant/snapshot",axum::routing::post(quant::endpoint))}
async fn upgrade(State(s):State<AppState>,Path(code):Path<String>,Query(q):Query<ReplayQuery>,ws:WebSocketUpgrade)->Result<Response,ApiError>{
    s.market.catalog.get(&code).map_err(|_|ApiError(axum::http::StatusCode::NOT_FOUND,"不支持该品种".into()))?;
    let period=Period::parse(q.period.as_deref().unwrap_or("1m")).map_err(|e|ApiError(axum::http::StatusCode::UNPROCESSABLE_ENTITY,e.to_string()))?;
    Ok(ws.on_upgrade(move|socket|session(socket,s,code,if period==Period::Timeline{Period::S1}else{period},q)))
}
#[derive(Debug,Deserialize)]#[serde(tag="command",rename_all="snake_case")]
enum Control{Pause,Play,Step,Speed{value:f64},Seek{#[serde(with="tracefang_core::persistence_contract::u64_string")]sequence:u64},SeekTime{#[serde(with="tracefang_core::persistence_contract::i64_string")]received_at_ns:i64},Stop}
struct Playback{paused:bool,speed:f64,steps:u64,seek:Option<u64>,seek_time:Option<i64>,stop:bool}
impl Playback{
    fn new()->Self{Self{paused:false,speed:1.0,steps:0,seek:None,seek_time:None,stop:false}}
    fn control(&mut self,message:Message,first:u64,last:u64)->Result<()> {
        match message{
            Message::Close(_)=>self.stop=true,
            Message::Text(text)=>match serde_json::from_str::<Control>(&text)?{
                Control::Pause=>self.paused=true,Control::Play=>self.paused=false,
                Control::Step=>{self.paused=true;self.steps=self.steps.checked_add(1).context("step counter exhausted")?},
                Control::Speed{value}=>{ensure!(value.is_finite()&&(0.1..=64.0).contains(&value),"speed must be 0.1–64");self.speed=value},
                Control::Seek{sequence}=>{ensure!((first..=last).contains(&sequence),"seek outside retained range");self.seek=Some(sequence);self.seek_time=None},
                Control::SeekTime{received_at_ns}=>{self.seek_time=Some(received_at_ns);self.seek=None},
                Control::Stop=>self.stop=true,
            },_=>{},
        }Ok(())
    }
    fn interrupted(&self)->bool{self.stop||self.seek.is_some()||self.seek_time.is_some()}
}
async fn send(socket:&mut WebSocket,value:Value)->Result<()>{socket.send(Message::Text(value.to_string().into())).await?;Ok(())}
async fn session(mut socket:WebSocket,s:AppState,code:String,period:Period,q:ReplayQuery){
    if let Err(error)=play(&mut socket,&s,&code,period,q).await{
        tracing::debug!(%error,"replay session ended");
        let _=send(&mut socket,json!({"kind":"status","state":"unavailable","error":error.to_string()})).await;
    }
}
fn scope(catalog:&Catalog,instrument:&Instrument,source:&str,period:Period,schedule:&MarketSchedule,epoch:&str)->Result<ReplayScope> {
    Ok(ReplayScope{projector_version:format!("{}:{}",REPLAY_VERSION,projector_build_hash()),catalog_hash:hex::encode(Sha256::digest(serde_json::to_vec(&catalog.items)?)),
        schedule_hash:tracefang_core::periods::schedule_version(Some(schedule))?,instrument:instrument.symbol.clone(),source:source.into(),period:period.as_str().into(),epoch:epoch.into()})
}
pub fn projector_build_hash()->String {
    static HASH:LazyLock<String>=LazyLock::new(||{
        let mut hash=Sha256::new();hash.update(BACKEND_BUILD_CONFIG.as_bytes());for (name,source) in BACKEND_BUILD_INPUTS {
            hash.update((name.len() as u64).to_be_bytes());hash.update(name.as_bytes());
            hash.update((source.len() as u64).to_be_bytes());hash.update(source);
        }hex::encode(hash.finalize())
    });HASH.clone()
}
#[derive(Clone,Default)]struct SeekToken(Arc<AtomicBool>);
impl SeekToken {fn cancel(&self){self.0.store(true,Ordering::Release)}fn check(&self)->Result<()>{ensure!(!self.0.load(Ordering::Acquire),"replay rebuild cancelled");Ok(())}}
struct CancelOnDrop(SeekToken);
impl Drop for CancelOnDrop{fn drop(&mut self){self.0.cancel()}}
fn check_cancel(token:&Option<SeekToken>)->Result<()>{if let Some(token)=token{token.check()?}Ok(())}
/// Continue reading controls while first-prefix decode, snapshot or a queued worker is busy.
/// Dropped old work checks the token at each frame and cannot publish a stale seek result.
async fn with_controls<F,T>(socket:&mut WebSocket,shutdown:&mut tokio::sync::watch::Receiver<bool>,control:&mut Playback,first:u64,last:u64,token:SeekToken,future:F)->Result<Option<T>>
where F:std::future::Future<Output=Result<T>> {
    let _cancel_on_exit=CancelOnDrop(token.clone());tokio::pin!(future);
    loop {tokio::select!{biased;
        _=shutdown.changed()=>{control.stop=true;token.cancel();return Ok(None)},
        incoming=socket.next()=>{match incoming{Some(message)=>{if let Err(error)=control.control(message?,first,last){token.cancel();return Err(error)}},None=>control.stop=true}
            if control.interrupted(){token.cancel();return Ok(None)}},
        result=&mut future=>{token.check()?;return result.map(Some)},
    }}
}

fn prefix_origin_coverage(coverage:&Value,through:u64)->Value {
    let rows=coverage.as_array().into_iter().flatten().filter_map(|row|{
        let first=row["first_native_sequence"].as_str()?.parse::<u64>().ok()?;if first>through{return None}
        let mut prefix=row.clone();prefix.as_object_mut()?.remove("last_sequence");prefix["native_through_sequence"]=json!(through.to_string());Some(prefix)
    }).collect::<Vec<_>>();json!(rows)
}
fn replay_bar(row:tracefang_core::persistence_contract::ImportBarRow,catalog:&Catalog)->Result<RealtimeBar>{
    Ok(RealtimeBar{instrument:catalog.get(&row.instrument_symbol)?.instrument.clone(),interval_seconds:row.interval_seconds.into(),open_time:DateTime::from_timestamp_nanos(row.open_time_ns),
        open:row.open.parse()?,high:row.high.parse()?,low:row.low.parse()?,close:row.close.parse()?,volume:row.volume.as_deref().map(str::parse).transpose()?,source:serde_json::from_value(row.source_metadata)?,
        evidence_channel_id:row.evidence_channel_id,state:serde_json::from_value(json!(row.state))?,revision:row.revision,finalized_at:row.finalized_at_ns.map(DateTime::from_timestamp_nanos)})
}
/// Canonical old facts are consulted before every correction. The overlay retains
/// earlier frames in this uncommitted group, including facts outside the hot 240.
async fn reduce_fact_record(mut projector:Projector,record:CapturedFrame,decoded:Decoded,store:&tracefang_core::native_store::Store,overlay:&mut BTreeMap<BarKey,RealtimeBar>,cancel:&SeekToken)->Result<(Projector,Vec<Value>,tracefang_core::persistence_contract::ProjectionCommit)> {
    let mut keys=BTreeMap::new();
        if let Ok((quotes,bars))=&decoded {
            for quote in quotes {for interval in [1i64,60] {let key=(projector.source.clone(),projector.instrument.symbol.clone(),interval,tracefang_core::reducer::floor_time(quote.source.observed_at,interval)?);keys.insert(key.clone(),tracefang_core::native_store::CanonicalBarKey{source_id:key.0,symbol:key.1,interval_seconds:interval.try_into()?,open_time_ns:key.3.timestamp_nanos_opt().context("replay quote clock outside ns")?});}}
            for bar in bars {if bar.instrument==projector.instrument {let key=(projector.source.clone(),bar.instrument.symbol.clone(),bar.interval_seconds,bar.open_time);keys.insert(key.clone(),tracefang_core::native_store::CanonicalBarKey{source_id:key.0,symbol:key.1,interval_seconds:key.2.try_into()?,open_time_ns:key.3.timestamp_nanos_opt().context("replay bar clock outside ns")?});}}
        }
        let mut current=Vec::new();let keys=keys.into_values().collect::<Vec<_>>();
        for batch in keys.chunks(100_000){cancel.check()?;let (_,rows)=store.lookup_bars(batch.to_vec()).await?;for row in rows.into_iter().flatten(){let bar=replay_bar(row,&projector.catalog)?;if !overlay.contains_key(&bar_key(&bar)){current.push(bar);}}}
        current.extend(overlay.values().cloned());let input=record.clone();let token=cancel.clone();
        let (returned,output,commit)=tokio::task::spawn_blocking(move||->Result<_>{token.check()?;let output=projector.apply_decoded(input.position.sequence,&input.frame,Some(&input),decoded,current)?;let commit=projector.take_projection(input.position)?;token.check()?;Ok((projector,output,commit))}).await??;projector=returned;
        for row in &commit.bars{let bar:RealtimeBar=serde_json::from_value(row.clone())?;overlay.insert(bar_key(&bar),bar);}
    Ok((projector,output,commit))
}
async fn decode_fact_rows(mut projector:Projector,rows:Vec<CapturedFrame>,store:&tracefang_core::native_store::Store,cancel:SeekToken)->Result<(Projector,Vec<Vec<Value>>,Vec<tracefang_core::persistence_contract::ProjectionCommit>)>{
    cancel.check()?;let permit=REPLAY_DECODE.clone().acquire_owned().await?;let mut overlay=BTreeMap::<BarKey,RealtimeBar>::new();let mut events=vec![];let mut commits=vec![];
    for record in rows {
        cancel.check()?;let input=record.clone();let token=cancel.clone();
        let (returned,decoded)=tokio::task::spawn_blocking(move||->Result<_>{token.check()?;let decoded=projector.decode_frame(&input.frame,Some(&input));token.check()?;Ok((projector,decoded))}).await??;
        let(returned,output,commit)=reduce_fact_record(returned,record,decoded,store,&mut overlay,&cancel).await?;projector=returned;events.push(output);commits.push(commit);
    }drop(permit);cancel.check()?;Ok((projector,events,commits))
}
/// Offline reconciliation uses the exact replay decoder/reducer and persisted
/// correction lookups. The caller supplies an empty disposable replay Store,
/// commits every returned frame in original order, and retains its own audit.
/// This never consults the live or imported PG generation as a reducer seed.
pub async fn retained_projection_group(projector:Projector,rows:Vec<CapturedFrame>,store:&tracefang_core::native_store::Store)->Result<(Projector,Vec<tracefang_core::persistence_contract::ProjectionCommit>)>{
    let (projector,_,commits)=decode_fact_rows(projector,rows,store,SeekToken::default()).await?;Ok((projector,commits))
}
/// The global spool already decoded every original frame in order. Each scope
/// consumes all quotes (including dependencies), only selects bars afterwards,
/// and uses the identical canonical correction lookups/reducer. Empty internal
/// frame bodies are never exposed as original raw capture.
pub async fn retained_decoded_group(mut projector:Projector,rows:Vec<(CapturedFrame,Decoded,Option<Value>)>,store:&tracefang_core::native_store::Store)->Result<(Projector,Vec<tracefang_core::persistence_contract::ProjectionCommit>)>{
    let cancel=SeekToken::default();let _permit=REPLAY_DECODE.clone().acquire_owned().await?;let mut overlay=BTreeMap::<BarKey,RealtimeBar>::new();let mut commits=vec![];
    projector.cached_context_mode=true;
    for(record,decoded,calendar_context)in rows {
        ensure!(projector.pending_calendar_context.is_none(),"uncommitted cached calendar delta");
        projector.pending_calendar_context=calendar_context;
        let decoded=decoded.and_then(|(mut quotes,mut bars)|{for quote in &mut quotes{capture_provenance(&mut quote.source,&record);quote.validate()?;}for bar in &mut bars{capture_provenance(&mut bar.source,&record);bar.validate()?;}Ok((quotes,bars))});
        let(returned,_,commit)=reduce_fact_record(projector,record,decoded,store,&mut overlay,&cancel).await?;projector=returned;commits.push(commit);
    }Ok((projector,commits))
}
const REPLAY_FILE_BUDGET:u64=4*1024*1024*1024;
const REPLAY_CACHE_BUDGET:u64=8*1024*1024*1024;
static REPLAY_SESSIONS:LazyLock<Arc<tokio::sync::Semaphore>>=LazyLock::new(||Arc::new(tokio::sync::Semaphore::new(4)));
fn replay_cache_root()->Result<std::path::PathBuf>{
    if let Some(path)=std::env::var_os("TRACEFANG_REPLAY_CACHE_DIR"){return Ok(path.into())}
    let home=std::path::PathBuf::from(std::env::var_os("HOME").context("replay cache requires HOME or TRACEFANG_REPLAY_CACHE_DIR")?);
    Ok(if cfg!(target_os="macos"){home.join("Library/Caches/TraceFang/replay")}else if cfg!(target_os="windows"){std::env::var_os("LOCALAPPDATA").map(std::path::PathBuf::from).unwrap_or_else(||home.join("AppData/Local")).join("TraceFang/replay-cache")}else{std::env::var_os("XDG_CACHE_HOME").map(std::path::PathBuf::from).unwrap_or_else(||home.join(".cache")).join("tracefang/replay")})
}
fn replay_cache_bytes(path:&std::path::Path)->Result<u64>{let mut size=0u64;for entry in std::fs::read_dir(path)?{let entry=entry?;let metadata=entry.metadata()?;size=size.checked_add(if metadata.is_dir(){replay_cache_bytes(&entry.path())?}else{metadata.len()}).context("replay cache byte count exhausted")?;}Ok(size)}
struct FactsRun {
    store:tracefang_core::native_store::Store,projector:Option<Projector>,position:Option<tracefang_core::persistence_contract::CapturePosition>,knowledge_at_ns:Option<i64>,
    accumulator:Option<tracefang_core::quant_core::snapshot::SnapshotAccumulator>,directory:tempfile::TempDir,checkpoints:Vec<FactsCheckpoint>,
}
#[derive(Clone)]struct FactsCheckpoint{saved:tracefang_core::persistence_contract::ReplaySavepoint,scope:String,file:std::path::PathBuf,sha256:String,position:tracefang_core::persistence_contract::CapturePosition}
const FACTS_CP_SCHEMA:&str="replay-complete-facts-quant-v1";
const FACTS_CP_BYTES:usize=16*1024*1024;
impl FactsRun {
    fn open(projector:Projector)->Result<Self>{
        let root=replay_cache_root()?;std::fs::create_dir_all(&root)?;ensure!(replay_cache_bytes(&root)?<REPLAY_CACHE_BUDGET,"replay cache exceeded 8GiB; retained raw remains unchanged; remove inactive disposable caches before retry");
        ensure!(crate::capture::available_bytes(&root)?>4*1024*1024*1024,"replay cannot start with less than 4GiB available disk");
        let directory=tempfile::Builder::new().prefix("session-").tempdir_in(&root)?;let store=tracefang_core::native_store::Store::open_replay(directory.path().join("facts.redb"))?;
        Ok(Self{store,projector:Some(projector),position:None,knowledge_at_ns:None,accumulator:None,directory,checkpoints:vec![]})
    }
    fn check_budget(&self)->Result<()>{let root=self.directory.path().parent().context("replay cache parent absent")?;ensure!(std::fs::metadata(self.directory.path().join("facts.redb"))?.len()<REPLAY_FILE_BUDGET&&replay_cache_bytes(root)?<REPLAY_CACHE_BUDGET,"replay disposable facts byte budget exhausted; raw evidence preserved");ensure!(crate::capture::available_bytes(root)?>4*1024*1024*1024,"replay stopped below 4GiB available disk; raw evidence preserved");Ok(())}
    async fn checkpoint(&mut self,capture:&Capture,scope:&ReplayScope,cancel:SeekToken)->Result<()> {
        cancel.check()?;self.check_budget()?;let position=self.position.clone().context("complete replay checkpoint has no applied prefix")?;capture.get_at(&position).await?;
        if self.checkpoints.iter().any(|cp|cp.position==position){return Ok(())}
        let projector=self.projector.as_ref().context("checkpoint projector unavailable")?.snapshot()?;let accumulator=self.accumulator.as_ref().context("market-only checkpoint cannot restore shared indicators")?.snapshot()?;
        let bounds=capture.bounds().await?;let mut body=json!({"schema":FACTS_CP_SCHEMA,"scope":scope.key()?,"capture_prefix":position,"knowledge_at_ns":self.knowledge_at_ns.map(|v|v.to_string()),"projector":projector,"quant":accumulator,"origin_prefix_complete":bounds["origin_prefix_complete"],"origin_coverage":prefix_origin_coverage(&bounds["origin_coverage"],position.sequence),"initial_seed":"empty retained-prefix; no live/latest seed"});
        ensure!(serde_json::to_vec(&body)?.len()<=FACTS_CP_BYTES,"complete checkpoint state exceeds 16MiB budget");cancel.check()?;
        if self.checkpoints.len()==4{let old=self.checkpoints.remove(0);self.store.delete_replay_savepoints(vec![old.saved.savepoint_id]).await?;std::fs::remove_file(old.file)?;}
        let saved=self.store.create_replay_savepoint().await?;ensure!(saved.version.committed_capture.as_ref()==Some(&position),"facts checkpoint savepoint prefix changed");body["facts_savepoint"]=json!(saved);
        let file=self.directory.path().join(format!("checkpoint-{}.json",saved.savepoint_id));let candidate=file.with_extension("pending");let token=cancel.clone();let contents=serde_json::to_vec(&body)?;if contents.len()>FACTS_CP_BYTES{self.store.delete_replay_savepoints(vec![saved.savepoint_id]).await?;anyhow::bail!("complete checkpoint including facts savepoint exceeds 16MiB budget")}let sha256=hex::encode(Sha256::digest(&contents));let target=file.clone();
        let result=tokio::task::spawn_blocking(move||->Result<()>{use std::io::Write;token.check()?;let mut out=std::fs::File::create(&candidate)?;out.write_all(&contents)?;out.sync_all()?;token.check()?;std::fs::rename(candidate,&target)?;#[cfg(unix)]std::fs::File::open(target.parent().context("checkpoint parent missing")?)?.sync_all()?;Ok(())}).await?;
        if let Err(error)=result.and_then(|_|cancel.check()){let _=self.store.delete_replay_savepoints(vec![saved.savepoint_id]).await;let _=std::fs::remove_file(&file);let _=std::fs::remove_file(file.with_extension("pending"));return Err(error)}
        self.checkpoints.push(FactsCheckpoint{saved,scope:scope.key()?,file,sha256,position});self.check_budget()?;Ok(())
    }
    async fn restore_checkpoint(&mut self,capture:&Capture,scope:&ReplayScope,through:u64,cancel:SeekToken)->Result<Option<u64>>{
        cancel.check()?;let key=scope.key()?;let Some(cp)=self.checkpoints.iter().filter(|cp|cp.position.sequence<=through&&cp.scope==key).max_by_key(|cp|cp.position.sequence).cloned()else{return Ok(None)};
        capture.get_at(&cp.position).await?;let file=cp.file.clone();let expected=cp.sha256.clone();let body=tokio::task::spawn_blocking(move||->Result<Value>{ensure!(std::fs::metadata(&file)?.len()<=FACTS_CP_BYTES as u64,"checkpoint file exceeds byte budget");let bytes=std::fs::read(file)?;ensure!(hex::encode(Sha256::digest(&bytes))==expected,"checkpoint state SHA differs");Ok(serde_json::from_slice(&bytes)?)}).await??;
        ensure!(body["schema"]==FACTS_CP_SCHEMA&&body["scope"]==key&&body["capture_prefix"]==json!(cp.position)&&body["facts_savepoint"]==json!(cp.saved),"complete checkpoint scope/prefix/savepoint differs");
        let accumulator=tracefang_core::quant_core::snapshot::SnapshotAccumulator::restore(body["quant"].clone(),&Default::default())?;let knowledge=body["knowledge_at_ns"].as_str().context("checkpoint knowledge clock absent")?.parse::<i64>()?;
        let mut projector=self.projector.take().context("checkpoint projector in flight")?;projector.restore(body["projector"].clone())?;cancel.check()?;
        let restored=self.store.restore_replay_savepoint(cp.saved.savepoint_id,cp.saved.version.clone()).await?;ensure!(restored.version.committed_capture.as_ref()==Some(&cp.position),"restored facts do not match shared indicator prefix");
        let invalidated=restored.invalidated_later_ids;self.checkpoints.retain(|other|{if invalidated.contains(&other.saved.savepoint_id){let _=std::fs::remove_file(&other.file);false}else{true}});
        self.projector=Some(projector);self.accumulator=Some(accumulator);self.position=Some(cp.position.clone());self.knowledge_at_ns=Some(knowledge);cancel.check()?;Ok(Some(cp.position.sequence))
    }
    async fn apply(&mut self,rows:Vec<CapturedFrame>,cancel:SeekToken)->Result<Vec<Vec<Value>>>{
        cancel.check()?;self.check_budget()?;ensure!(!rows.is_empty(),"empty replay input group");let last=rows.last().unwrap();let position=last.position.clone();
        let knowledge=rows.iter().map(|row|if row.legacy.is_none(){row.logical_at_ns.max(row.accepted_at_ns)}else{row.logical_at_ns}).max().unwrap().max(self.knowledge_at_ns.unwrap_or(i64::MIN));
        let (projector,events,commits)=decode_fact_rows(self.projector.take().context("replay projector already in flight")?,rows,&self.store,cancel.clone()).await?;
        cancel.check()?;
        struct ByteCount(usize);impl std::io::Write for ByteCount{fn write(&mut self,bytes:&[u8])->std::io::Result<usize>{self.0=self.0.checked_add(bytes.len()).ok_or_else(||std::io::Error::other("projection byte count overflow"))?;Ok(bytes.len())}fn flush(&mut self)->std::io::Result<()>{Ok(())}}
        let mut batch=Vec::new();let mut batch_bytes=0usize;let mut receipt=None;
        for commit in commits {let mut count=ByteCount(0);serde_json::to_writer(&mut count,&commit)?;if !batch.is_empty()&&(batch.len()==64||batch_bytes+count.0>4*1024*1024){cancel.check()?;receipt=Some(self.store.commit_replay_frames(std::mem::take(&mut batch)).await?);batch_bytes=0;}batch_bytes+=count.0;batch.push(commit);}
        if !batch.is_empty(){cancel.check()?;receipt=Some(self.store.commit_replay_frames(batch).await?);}cancel.check()?;
        ensure!(receipt.is_some_and(|receipt|receipt.version.committed_capture.as_ref()==Some(&position)),"replay facts transaction did not reach exact input prefix");
        self.projector=Some(projector);self.position=Some(position);self.knowledge_at_ns=Some(knowledge);self.check_budget()?;Ok(events)
    }
    async fn quant_view(&self,scope:&ReplayScope,definition:&crate::catalog::Definition,period:Period,schedule:&MarketSchedule,origin_complete:bool)->Result<quant::ReplayQuantView>{
        Ok(quant::ReplayQuantView{store:self.store.clone(),version:self.store.version().await?,scope:scope.clone(),definition:definition.clone(),period,schedule:schedule.clone(),position:self.position.clone().context("no raw prefix has been applied")?,knowledge_at:DateTime::from_timestamp_nanos(self.knowledge_at_ns.context("replay knowledge clock absent")?),origin_prefix_complete:origin_complete,cancel:SeekToken::default()})
    }
    async fn view_items(&self,definition:&crate::catalog::Definition,period:Period,schedule:&MarketSchedule)->Result<Vec<Value>>{
        let Some(knowledge)=self.knowledge_at_ns else{return Ok(vec![])};
        let request=tracefang_core::persistence_contract::CanonicalSnapshotRequest{symbol:definition.instrument.symbol.clone(),source_id:self.projector.as_ref().context("replay projector unavailable")?.source.clone(),period:period.as_str().into(),selection:tracefang_core::persistence_contract::BarSelection::Before{before_ns:knowledge,count:300},final_only:false,expected_version:Some(self.store.version().await?)};
        let snapshot=if period.is_base(){self.store.canonical_snapshot(request).await?}else{self.store.canonical_period_page_at(request,period,Some(schedule.clone()),knowledge).await?};
        snapshot.bars.into_iter().map(|row|{let value=crate::catalog::database_bar(row,&definition.instrument)?;Ok(exact_public(serde_json::to_value(value)?))}).collect()
    }
}
async fn rebuild_facts(capture:&Capture,projector:Projector,scope:&ReplayScope,first:u64,target:u64,cancel:SeekToken)->Result<(FactsRun,u64)>{
    if target==first{return Ok((FactsRun::open(projector)?,0))}
    rebuild_facts_inclusive(capture,projector,scope,first,target.checked_sub(1).context("replay target underflow")?,cancel).await
}
async fn rebuild_facts_inclusive(capture:&Capture,projector:Projector,scope:&ReplayScope,first:u64,through:u64,cancel:SeekToken)->Result<(FactsRun,u64)>{
    cancel.check()?;let mut run=FactsRun::open(projector)?;let mut next=first;let mut decoded=0;
    loop{cancel.check()?;let rows=capture.scan(&scope.epoch,next,through.checked_add(1),64,4*1024*1024).await?;ensure!(!rows.is_empty(),"raw replay evidence ends before target");let last=rows.last().unwrap().position.sequence;ensure!(last<=through,"replay scan crossed inclusive target");decoded+=rows.len() as u64;run.apply(rows,cancel.clone()).await?;if last==through{break}next=last.checked_add(1).context("replay cursor exhausted")?;}
    cancel.check()?;Ok((run,decoded))
}
async fn rebuild_facts_cached(capture:&Capture,projector:Projector,scope:&ReplayScope,first:u64,through:u64,cached:Option<FactsRun>,cancel:SeekToken)->Result<(FactsRun,u64,Option<u64>)>{
    if let Some(mut run)=cached{if let Some(restored)=run.restore_checkpoint(capture,scope,through,cancel.clone()).await?{let mut decoded=0;let mut last=restored;
        while last<through{cancel.check()?;let rows=capture.scan(&scope.epoch,last.checked_add(1).context("checkpoint tail cursor exhausted")?,through.checked_add(1),64,4*1024*1024).await?;ensure!(!rows.is_empty(),"checkpoint raw tail contains a gap");last=rows.last().unwrap().position.sequence;decoded+=rows.len() as u64;run.apply(rows,cancel.clone()).await?;}
        return Ok((run,decoded,Some(restored)))
    }}let (run,decoded)=rebuild_facts_inclusive(capture,projector,scope,first,through,cancel).await?;Ok((run,decoded,None))
}
/// Offline fixed-prefix evidence from retained raw only. Never imports live facts.
pub async fn audit_prefix(capture:&Capture,catalog:Arc<Catalog>,code:&str,source:&str,through:u64)->Result<Value>{
    let bounds=capture.bounds().await?;let first=bounds["first_sequence"].as_str().context("empty retained evidence")?.parse::<u64>()?;let last=bounds["last_sequence"].as_str().context("tail absent")?.parse::<u64>()?;ensure!((first..=last).contains(&through),"audit prefix outside retained evidence");
    let definition=catalog.get(code)?;ensure!(definition.source_ids.iter().any(|id|id==source),"audit source unavailable");let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;let scope=scope(&catalog,&definition.instrument,source,Period::M1,&schedule,&capture.name)?;
    let started=std::time::Instant::now();let (mut run,decoded)=rebuild_facts_inclusive(capture,Projector::new_facts(catalog.clone(),definition.instrument.clone(),source.into(),Period::M1,schedule.clone())?,&scope,first,through,SeekToken::default()).await?;let build_ms=started.elapsed().as_secs_f64()*1000.0;
    println!("{}",json!({"phase":"actual_prefix_facts_complete","decoded_frames":decoded.to_string(),"through":through.to_string(),"build_ms":build_ms}));
    let generation=run.store.generation_summary().await?;let view=Arc::new(run.quant_view(&scope,definition,Period::M1,&schedule,bounds["origin_prefix_complete"]==true).await?);let started=std::time::Instant::now();let accumulator=quant::calculate(view,Default::default(),None).await?;let quant_ms=started.elapsed().as_secs_f64()*1000.0;let current=accumulator.current()?;
    let started=std::time::Instant::now();let state=accumulator.snapshot()?;let encoded=serde_json::to_vec(&state)?;let checkpoint_serialization_ms=started.elapsed().as_secs_f64()*1000.0;let restored=tracefang_core::quant_core::snapshot::SnapshotAccumulator::restore(state,&Default::default())?.current()?;ensure!(restored.evidence.input_hash==current.evidence.input_hash&&restored.evidence.snapshot_hash==current.evidence.snapshot_hash,"actual quant checkpoint roundtrip changed hashes");
    run.accumulator=Some(accumulator);let started=std::time::Instant::now();run.checkpoint(capture,&scope,SeekToken::default()).await?;let complete_checkpoint_ms=started.elapsed().as_secs_f64()*1000.0;let complete_checkpoint_bytes=std::fs::metadata(&run.checkpoints.last().context("complete checkpoint absent")?.file)?.len();
    let started=std::time::Instant::now();ensure!(run.restore_checkpoint(capture,&scope,through,SeekToken::default()).await?==Some(through),"actual facts+quant checkpoint did not restore exact target");let complete_checkpoint_restore_ms=started.elapsed().as_secs_f64()*1000.0;ensure!(run.accumulator.as_ref().unwrap().current()?.evidence.snapshot_hash==current.evidence.snapshot_hash,"actual facts/quant checkpoint changed business hash");
    let scan=run.store.canonical_scan_with_resume(tracefang_core::persistence_contract::CanonicalScanRequest{symbol:definition.instrument.symbol.clone(),source_id:source.into(),interval_seconds:60,start_ns:i64::MIN,end_ns:run.knowledge_at_ns.context("audit knowledge missing")?,final_only:false,expected_version:Some(run.store.version().await?)},1000,None,|_|Ok(())).await?;
    let report=json!({"kind":"actual_retained_original_prefix_complete_facts_and_shared_quant","scope":scope,"prefix":run.position,"decoded_frames":decoded.to_string(),"origin_prefix_complete":bounds["origin_prefix_complete"],"origin_coverage":prefix_origin_coverage(&bounds["origin_coverage"],through),"initial_seed":"empty retained-prefix; no live/legacy PG/latest seed","knowledge_at_ns":run.knowledge_at_ns.map(|v|v.to_string()),"build_ms":build_ms,"shared_quant_ms":quant_ms,"facts_generation":generation,"canonical_minute_scan":scan,"derived_file_bytes":std::fs::metadata(run.directory.path().join("facts.redb"))?.len().to_string(),"quant_snapshot":current,"quant_checkpoint_bytes":encoded.len().to_string(),"quant_checkpoint_serialization_ms":checkpoint_serialization_ms,"quant_checkpoint_roundtrip":true,"complete_checkpoint_ms":complete_checkpoint_ms,"complete_checkpoint_bytes":complete_checkpoint_bytes.to_string(),"complete_checkpoint_restore_ms":complete_checkpoint_restore_ms,"complete_facts_quant_checkpoint_roundtrip":true,"performance_scope":"native offline fixed-prefix; concurrent fleet builds; not a UI SLO"});run.store.close().await?;Ok(report)
}
async fn await_completed_controls(socket:&mut WebSocket,shutdown:&mut tokio::sync::watch::Receiver<bool>,control:&mut Playback,first:u64,last:u64)->Result<()>{
    while !control.interrupted(){tokio::select!{_=shutdown.changed()=>{control.stop=true;},incoming=socket.next()=>{match incoming{Some(message)=>control.control(message?,first,last)?,None=>control.stop=true}}}}
    Ok(())
}
async fn decode_rows(mut projector:Projector,rows:Vec<CapturedFrame>,scope:ReplayScope,_origin_complete:bool,origin_coverage:Value,cancel:Option<SeekToken>)->Result<(Projector,Vec<Vec<Value>>,Vec<(u64,Vec<u8>)>)> {
    check_cancel(&cancel)?;
    let permit=REPLAY_DECODE.clone().acquire_owned().await?;
    tokio::task::spawn_blocking(move||{
        let _permit=permit;check_cancel(&cancel)?;let mut events=vec![];let mut checkpoints=vec![];
        for row in rows {
            check_cancel(&cancel)?;events.push(projector.accept_record(&row)?);
            if row.position.sequence%256==0 {
                let coverage=prefix_origin_coverage(&origin_coverage,row.position.sequence);
                let prefix_complete=coverage.as_array().unwrap().iter().all(|v|v["missing_prefix"]!=true);
                let mut seed=Checkpoint::new(scope.clone(),row.position.clone(),prefix_complete,projector.snapshot()?);seed.origin_coverage=coverage;
                checkpoints.push((row.position.sequence,serde_json::to_vec(&seed)?));check_cancel(&cancel)?;
            }
        }
        check_cancel(&cancel)?;Ok((projector,events,checkpoints))
    }).await?
}
async fn projector_state(projector:Projector)->Result<(Projector,Value)> {
    let permit=REPLAY_DECODE.clone().acquire_owned().await?;
    tokio::task::spawn_blocking(move||{let _permit=permit;let state=projector.snapshot()?;Ok((projector,state))}).await?
}
/// Builds only through target-1. A valid checkpoint bounds later seeks to a short raw tail.
async fn rebuild(capture:&Capture,projector:Projector,scope:&ReplayScope,first:u64,target:u64,origin_complete:bool)->Result<(Projector,u64,Option<u64>)> {
    rebuild_controlled(capture,projector,scope,first,target,origin_complete,None).await
}
async fn rebuild_controlled(capture:&Capture,mut projector:Projector,scope:&ReplayScope,first:u64,target:u64,origin_complete:bool,cancel:Option<SeekToken>)->Result<(Projector,u64,Option<u64>)> {
    check_cancel(&cancel)?;
    let mut next=first;let mut restored=None;
    if target>first {
        if let Some((at,bytes))=capture.nearest_checkpoint(&scope.key()?,target-1).await? {
            let bound_scope=scope.clone();let worker_cancel=cancel.clone();let permit=REPLAY_DECODE.clone().acquire_owned().await?;
            let (result,position)=tokio::task::spawn_blocking(move||->Result<_>{
                let _permit=permit;check_cancel(&worker_cancel)?;let seed:Checkpoint=serde_json::from_slice(&bytes)?;seed.validate(&bound_scope,target)?;
                ensure!(seed.through.sequence==at,"checkpoint key and input position differ");projector.restore(seed.state)?;check_cancel(&worker_cancel)?;Ok((projector,seed.through))
            }).await??;capture.get_at(&position).await?;projector=result;
            next=at.checked_add(1).context("checkpoint cursor exhausted")?;restored=Some(at);
        }
    }
    let origin_coverage=capture.bounds().await?["origin_coverage"].clone();
    let mut decoded=0u64;
    while next<target {
        check_cancel(&cancel)?;let rows=capture.scan(&scope.epoch,next,Some(target),64,4*1024*1024).await?;
        ensure!(!rows.is_empty(),"raw replay evidence ends before target");let last=rows.last().unwrap().position.sequence;decoded+=rows.len() as u64;
        let (result,_,checkpoints)=decode_rows(projector,rows,scope.clone(),origin_complete,origin_coverage.clone(),cancel.clone()).await?;projector=result;
        for (at,body) in checkpoints{check_cancel(&cancel)?;if !capture.is_read_only(){capture.checkpoint(&scope.key()?,at,body).await?;}}
        next=last.checked_add(1).context("replay cursor exhausted")?;
    }
    check_cancel(&cancel)?;Ok((projector,decoded,restored))
}
async fn play(socket:&mut WebSocket,s:&AppState,code:&str,period:Period,q:ReplayQuery)->Result<()> {
    let d=s.market.catalog.get(code)?;let source=q.source_id.clone().unwrap_or(s.market.source(&d.instrument.symbol)?);let schedule=pages::schedule(&s.market,code)?;
    play_inputs(socket,&s.capture,s.market.catalog.clone(),s.shutdown.clone(),code,period,q,source,schedule).await
}
async fn play_inputs(socket:&mut WebSocket,capture:&Capture,catalog:Arc<Catalog>,mut shutdown:tokio::sync::watch::Receiver<bool>,code:&str,period:Period,q:ReplayQuery,source:String,schedule:MarketSchedule)->Result<()> {
    let bounds=capture.bounds().await?;let epoch=bounds["epoch"].as_str().context("capture epoch missing")?.to_owned();
    let first=bounds["first_sequence"].as_str().context("recorded frame stream is empty")?.parse::<u64>()?;
    let captured_last=bounds["last_sequence"].as_str().context("capture tail missing")?.parse::<u64>()?;
    let last=q.end_sequence.as_deref().map(str::parse).transpose()?.unwrap_or(captured_last);
    ensure!(last>=first&&last<=captured_last,"invalid replay end sequence");let watermark=capture.get(last).await?.position;
    let initial_time=q.received_at_ns.as_deref().map(str::parse::<i64>).transpose()?;
    let target=if let Some(at)=initial_time{capture.locate_time(&epoch,at,last).await?.sequence}else{q.start_sequence.as_deref().map(str::parse).transpose()?.unwrap_or(first)};
    ensure!((first..=last).contains(&target),"invalid replay start sequence");
    let d=catalog.get(code)?;
    ensure!(d.source_ids.contains(&source),"source does not support replay instrument");
    let scope=scope(&catalog,&d.instrument,&source,period,&schedule,&epoch)?;
    let origin_complete=bounds["origin_prefix_complete"].as_bool().unwrap_or(false);let mut control=Playback::new();control.paused=q.paused.unwrap_or(false);control.seek=Some(target);
    let session_id=uuid::Uuid::new_v4().to_string();let _session_permit=REPLAY_SESSIONS.clone().try_acquire_owned().context("replay session budget exhausted")?;let _session_guard=quant::SessionGuard(session_id.clone());let mut cached_run=None;
    'runs: while !control.stop {
        let requested_time=control.seek_time.take();
        let target=if let Some(at)=requested_time{capture.locate_time(&epoch,at,last).await?.sequence}else{control.seek.take().unwrap_or(target)};
        quant::clear(&session_id);
        let projector=Projector::new_facts(catalog.clone(),d.instrument.clone(),source.clone(),period,schedule.clone())?;
        send(socket,json!({"kind":"status","session_id":session_id,"state":"seeking","reset":true,"epoch":epoch,"source_id":source,"period_id":period.as_str(),
            "start_sequence":first.to_string(),"end_sequence":last.to_string(),"target_sequence":target.to_string(),"target_received_at_ns":requested_time.or(initial_time).map(|v|v.to_string()),
            "run_input_watermark":watermark,"replay_policy":"original_received_order","origin_prefix_complete":origin_complete,
            "warmup_incomplete":!origin_complete,"origin_coverage":bounds["origin_coverage"],"clock_policy":"authoritative capture sequence; monotone knowledge=max(received,native accepted); legacy imported clock excluded; time ties select first sequence"})).await?;
        let token=SeekToken::default();
        let Some((mut run,decoded,restored_checkpoint))=with_controls(socket,&mut shutdown,&mut control,first,last,token.clone(),rebuild_facts_cached(&capture,projector,&scope,first,target,cached_run.take(),token.clone())).await? else {continue 'runs};
        let mut quant_snapshot=None;
        if run.position.is_some(){
            let view=Arc::new(run.quant_view(&scope,d,period,&schedule,origin_complete).await?);let token=SeekToken::default();let mut owned_view=run.quant_view(&scope,d,period,&schedule,origin_complete).await?;owned_view.cancel=token.clone();
            let Some(accumulator)=with_controls(socket,&mut shutdown,&mut control,first,last,token,quant::calculate(Arc::new(owned_view),Default::default(),run.accumulator.take())).await? else{continue 'runs};
            quant_snapshot=Some(accumulator.current()?);run.accumulator=Some(accumulator);quant::publish(&session_id,Arc::try_unwrap(view).map_err(|_|anyhow::anyhow!("unexpected replay view ownership"))?)?;
        }
        let token=SeekToken::default();let Some(())=with_controls(socket,&mut shutdown,&mut control,first,last,token.clone(),run.checkpoint(capture,&scope,token)).await?else{continue 'runs};
        let snapshot_state=run.projector.as_ref().context("replay projector unavailable")?.snapshot()?;
        let actual=capture.get(target).await?;
        let items=run.view_items(d,period,&schedule).await?;
        send(socket,json!({"kind":"snapshot","session_id":session_id,"state":if control.paused{"paused"}else{"playing"},"paused":control.paused,"items":exact_public(json!(items)),"stream_sequence":target.to_string(),
            "actual_received_at_ns":actual.frame.received_at.timestamp_nanos_opt().map(|v|v.to_string()),"logical_at_ns":actual.logical_at_ns.to_string(),"knowledge_at_ns":run.knowledge_at_ns.map(|v|v.to_string()),
            "checkpoint_sequence":restored_checkpoint.map(|v|v.to_string()),"checkpoint_capability":"complete_facts_savepoint_and_shared_quant_state","decoded_tail_frames":decoded.to_string(),"state_hash":state_hash(&snapshot_state),"quant_snapshot":quant_snapshot,
            "scope":scope,"input_watermark":run.position,"run_input_watermark":watermark,"origin_coverage":bounds["origin_coverage"],"warmup_from_sequence":first.to_string(),"warmup_through_sequence":run.position.as_ref().map(|v|v.sequence.to_string()),"warmup_incomplete":!origin_complete,"view_limit":300,"facts_semantics":"complete_captured_prefix_from_empty_seed"})).await?;
        send(socket,json!({"kind":"status","session_id":session_id,"state":if control.paused{"paused"}else{"playing"},"paused":control.paused,"stream_sequence":target.to_string(),"input_watermark":run.position})).await?;
        if target==last{send(socket,json!({"kind":"status","session_id":session_id,"state":"completed","stream_sequence":last.to_string(),"input_watermark":run.position})).await?;await_completed_controls(socket,&mut shutdown,&mut control,first,last).await?;cached_run=Some(run);continue 'runs;}
        let mut next=target.checked_add(1).context("inclusive replay cursor exhausted")?;let mut previous_clock=Some(actual.logical_at_ns);
        loop {
            if control.interrupted(){break}
            let rows=capture.scan(&epoch,next,last.checked_add(1),64,4*1024*1024).await?;ensure!(!rows.is_empty(),"capture gap during fixed replay");
            for row in rows {
                if row.position.sequence>last{break}
                // Poll control even for equal timestamps; stepping still admits exactly one raw frame.
                if !control.paused {
                    tokio::select!{biased;_=shutdown.changed()=>{control.stop=true;},incoming=socket.next()=>{match incoming{Some(m)=>control.control(m?,first,last)?,None=>control.stop=true}},_=tokio::task::yield_now()=>{}}
                    if control.interrupted(){break}
                }
                let mut remaining=previous_clock.map(|at:i64|((row.logical_at_ns as i128-at as i128).max(0) as f64)/1_000_000_000.0).unwrap_or(0.0);
                loop {
                    if control.interrupted(){break}
                    if control.steps>0{control.steps-=1;break}
                    if !control.paused&&remaining<=0.0{break}
                    let start=tokio::time::Instant::now();let playing=!control.paused;let speed=control.speed;
                    let wait=if control.paused{Duration::from_secs(60)}else{Duration::from_secs_f64((remaining/speed).min(60.0))};
                    tokio::select!{_=shutdown.changed()=>{control.stop=true;},incoming=socket.next()=>{match incoming{Some(m)=>control.control(m?,first,last)?,None=>control.stop=true}},_=tokio::time::sleep(wait)=>{}}
                    if playing{remaining=(remaining-start.elapsed().as_secs_f64()*speed).max(0.0)}
                }
                if control.interrupted(){break}
                quant::clear(&session_id);let token=SeekToken::default();
                let Some(mut outputs)=with_controls(socket,&mut shutdown,&mut control,first,last,token.clone(),run.apply(vec![row.clone()],token)).await? else{continue 'runs};
                let mut owned_view=run.quant_view(&scope,d,period,&schedule,origin_complete).await?;let token=SeekToken::default();owned_view.cancel=token.clone();
                let Some(accumulator)=with_controls(socket,&mut shutdown,&mut control,first,last,token,quant::calculate(Arc::new(owned_view),Default::default(),run.accumulator.take())).await? else{continue 'runs};
                let quant_snapshot=accumulator.current()?;run.accumulator=Some(accumulator);quant::publish(&session_id,run.quant_view(&scope,d,period,&schedule,origin_complete).await?)?;
                if row.position.sequence%256==0||row.position.sequence==last{let token=SeekToken::default();let Some(())=with_controls(socket,&mut shutdown,&mut control,first,last,token.clone(),run.checkpoint(capture,&scope,token)).await?else{continue 'runs};}
                let events=outputs.pop().context("raw frame produced no replay envelope")?;
                for mut value in events {
                    value["session_id"]=json!(session_id);value["position"]=json!(row.position);value["logical_at_ns"]=json!(row.logical_at_ns.to_string());value["knowledge_at_ns"]=json!(run.knowledge_at_ns.map(|v|v.to_string()));
                    value["accepted_at_ns"]=if row.legacy.is_none(){json!(row.accepted_at_ns.to_string())}else{Value::Null};if row.legacy.is_some(){value["imported_at_ns"]=json!(row.accepted_at_ns.to_string());}value["input_watermark"]=json!(row.position);value["run_input_watermark"]=json!(watermark);
                    if value["kind"]=="frame"{value["quant_snapshot"]=json!(quant_snapshot);}send(socket,value).await?;
                }
                if !period.is_base(){send(socket,json!({"kind":"period_snapshot","session_id":session_id,"items":run.view_items(d,period,&schedule).await?,"input_watermark":row.position,"knowledge_at_ns":run.knowledge_at_ns.map(|v|v.to_string())})).await?;}
                previous_clock=Some(row.logical_at_ns);
                if row.position.sequence==last {
                    let state=run.projector.as_ref().context("replay projector unavailable")?.snapshot()?;
                    send(socket,json!({"kind":"status","session_id":session_id,"state":"completed","stream_sequence":last.to_string(),"state_hash":state_hash(&state),"input_watermark":watermark})).await?;await_completed_controls(socket,&mut shutdown,&mut control,first,last).await?;cached_run=Some(run);continue 'runs;
                }
                next=row.position.sequence.checked_add(1).context("replay cursor exhausted")?;
            }
        }
        cached_run=Some(run);
    }Ok(())
}
#[cfg(test)]mod tests{
    use super::*;
    #[test]fn build_digest_covers_replay_exact_types_dependencies_and_configuration(){
        for required in ["src/replay.rs","src/replay_checkpoint.rs","src/domain.rs","src/exact.rs","src/events.rs","src/analysis/evaluator.rs","src/analysis/snapshot.rs","assets/catalog.json","assets/schedules.json","assets/expert-strategies.json","assets/gold-events.json","src/research/config.json","assets/schema.sql","Cargo.lock","Cargo.toml","build.rs"]{
            assert!(BACKEND_BUILD_INPUTS.iter().any(|(path,_)|*path==required),"missing build input {required}");
        }assert_eq!(projector_build_hash().len(),64);assert!(BACKEND_BUILD_CONFIG.contains("rustc="));
    }
    #[tokio::test]async fn stop_new_seek_and_disconnect_cancel_long_first_seek_without_old_result()->Result<()> {
        use futures_util::{SinkExt,TryStreamExt};
        let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw"),Default::default())?;
        futures_util::stream::iter((1..=1025).map(|sequence|{let capture=capture.clone();async move{capture.append(&recorded_quote(sequence)).await}})).buffer_unordered(32).try_collect::<Vec<_>>().await?;
        let catalog=Arc::new(Catalog::embedded()?);let definition=catalog.get("XAUUSD")?.clone();
        let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;
        let boundary=scope(&catalog,&definition.instrument,"jin10_client",Period::M1,&schedule,&capture.name)?;
        for command in ["stop","seek","disconnect"] {
            // A long first seek queued behind the single decoder must remain cancellable.
            let held=REPLAY_DECODE.clone().acquire_owned().await?;
            let (done,finished)=tokio::sync::oneshot::channel();let done=Arc::new(tokio::sync::Mutex::new(Some(done)));
            let (shutdown_send,shutdown)=tokio::sync::watch::channel(false);
            let c=capture.clone();let cat=catalog.clone();let instrument=definition.instrument.clone();let sched=schedule.clone();let scope=boundary.clone();
            let app=Router::new().route("/cancel",get(move|ws:WebSocketUpgrade|{
                let (c,cat,instrument,sched,scope,done,mut shutdown)=(c.clone(),cat.clone(),instrument.clone(),sched.clone(),scope.clone(),done.clone(),shutdown.clone());
                async move{ws.on_upgrade(move|mut socket|async move{
                    let token=SeekToken::default();let mut control=Playback::new();
                    send(&mut socket,json!({"kind":"ready"})).await.unwrap();
                    let projector=Projector::new(cat,instrument,"jin10_client".into(),Period::M1,sched).unwrap();
                    let result=with_controls(&mut socket,&mut shutdown,&mut control,1,1025,token.clone(),rebuild_controlled(&c,projector,&scope,1,1025,true,Some(token.clone()))).await;
                    if let Some(done)=done.lock().await.take(){let _=done.send((result.map(|v|v.is_none()),control.stop,control.seek,token.check().is_err()));}
                })}
            }));
            let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await?;let address=listener.local_addr()?;let server=tokio::spawn(async move{axum::serve(listener,app).await});
            let (mut client,_)=tokio_tungstenite::connect_async(format!("ws://{address}/cancel")).await?;
            tokio::time::timeout(Duration::from_secs(2),client.next()).await?.context("ready missing")??;
            if command=="disconnect"{client.close(None).await?}else{let text=if command=="stop"{r#"{"command":"stop"}"#}else{r#"{"command":"seek","sequence":"11"}"#};client.send(tokio_tungstenite::tungstenite::Message::Text(text.into())).await?;}
            let (cancelled,stopped,seek,token_cancelled)=tokio::time::timeout(Duration::from_secs(2),finished).await??;
            ensure!(cancelled?&&token_cancelled,"old seek result escaped {command}");if command=="seek"{ensure!(seek==Some(11)&&!stopped,"new seek target lost")}else{ensure!(stopped,"stop/disconnect not observed")}
            drop(held);let _=shutdown_send.send(true);server.abort();
            ensure!(capture.nearest_checkpoint(&boundary.key()?,1024).await?.is_none(),"cancelled first seek published a checkpoint");
        }
        capture.close().await?;Ok(())
    }
    fn recorded_quote(seq:u64)->ProviderFrame {
        let symbol="XAUUSD.GOODS";let mut body=10005_u16.to_le_bytes().to_vec();body.extend((symbol.len() as u16).to_le_bytes());body.extend(symbol.as_bytes());
        body.extend((1_800_000_000u32+(seq as u32)*60).to_le_bytes());body.extend((4_000_000_000i64+seq as i64*100_000).to_le_bytes());body.extend(3_999_000_000i64.to_le_bytes());
        ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"replay-proof".into(),sequence:seq,
            received_at:DateTime::from_timestamp(1_800_000_000+seq as i64*60,123456789).unwrap(),encoding:"wire".into(),body}
    }
    fn recorded_history(seq:u64,first:i64,count:usize,price:i64,received:DateTime<Utc>)->Result<ProviderFrame>{
        use std::io::Write;use base64::Engine;
        let mut zipped=flate2::write::GzEncoder::new(Vec::new(),flate2::Compression::fast());
        for index in 0..count {for value in [first+index as i64*60,price+100_000_000,price,price-100_000_000,price,67]{zipped.write_all(&value.to_le_bytes())?;}}
        let body=json!({"provider_code":"XAUUSD.GOODS","file":{"file_name":"replay-test-fixed","record_count":count,"start_timestamp":null,"end_timestamp":null},"body_base64":base64::engine::general_purpose::STANDARD.encode(zipped.finish()?)});
        Ok(ProviderFrame{version:1,channel:"jin10_history".into(),connection_id:"facts-prefix-proof".into(),sequence:seq,received_at:received,encoding:"gzip-json".into(),body:serde_json::to_vec(&body)?})
    }
    #[tokio::test]async fn complete_prefix_facts_old_revision_and_quant_are_exact_without_live_or_future_seed()->Result<()> {
        use tracefang_core::native_store::CanonicalBarKey;
        let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw"),Default::default())?;
        let received=DateTime::from_timestamp(1_800_030_000,123456789).unwrap();
        capture.append(&recorded_history(1,1_800_000_000,320,4_000_000_000,received)?).await?;
        let mut empty=recorded_quote(2);empty.channel="jin10_local".into();empty.encoding="session-decrypted".into();empty.received_at=received-chrono::Duration::nanoseconds(1);empty.body=vec![0,0];capture.append(&empty).await?;
        let correction=capture.append(&recorded_history(3,1_800_000_000,1,4_200_000_000,received)?).await?;
        capture.append(&recorded_history(4,1_800_050_000,1,9_000_000_000,received+chrono::Duration::seconds(1))?).await?;
        let catalog=Arc::new(Catalog::embedded()?);let definition=catalog.get("XAUUSD")?.clone();let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;
        let scope=scope(&catalog,&definition.instrument,"jin10_client",Period::M1,&schedule,&capture.name)?;
        let new=||Projector::new_facts(catalog.clone(),definition.instrument.clone(),"jin10_client".into(),Period::M1,schedule.clone());
        let (mut run,decoded)=rebuild_facts(&capture,new()?,&scope,1,3,SeekToken::default()).await?;ensure!(decoded==2,"initial facts omitted prefix");
        let first=quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),None).await?;
        ensure!(first.current()?.evidence.confirmed_count==320,"full prefix silently used a view page");
        run.apply(vec![capture.get_at(&correction.position).await?],SeekToken::default()).await?;
        let key=CanonicalBarKey{source_id:"jin10_client".into(),symbol:definition.instrument.symbol.clone(),interval_seconds:60,open_time_ns:1_800_000_000_000_000_000};
        let (_,rows)=run.store.lookup_bars(vec![key]).await?;let row=rows[0].as_ref().context("old corrected minute missing")?;
        ensure!(row.revision==2&&row.close=="4200"&&row.source_metadata["raw_payload"]["capture_sequence"]=="3","old canonical correction/finality availability lost outside 240-row hot state");
        let hot=quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),Some(first)).await?.current()?;
        let cold=quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),None).await?.current()?;
        ensure!(hot.evidence.input_hash==cold.evidence.input_hash&&hot.evidence.input_count==cold.evidence.input_count&&hot.evidence.confirmed_count==320,"revision resume differed from cold exact facts scan");
        let (rebuilt,_) =rebuild_facts(&capture,new()?,&scope,1,4,SeekToken::default()).await?;
        let repeated=quant::calculate(Arc::new(rebuilt.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),None).await?.current()?;
        ensure!(repeated.evidence.token.store_epoch!=cold.evidence.token.store_epoch&&repeated.evidence.input_hash==cold.evidence.input_hash&&repeated.evidence.snapshot_hash==cold.evidence.snapshot_hash,"regenerable physical store identity changed business hash");
        let summary=run.store.generation_summary().await?;ensure!(summary["counts"]["bar_rows"]=="320"&&run.position.as_ref()==Some(&correction.position),"future frame leaked into fixed facts prefix");
        run.store.close().await?;rebuilt.store.close().await?;capture.close_and_drain().await?;Ok(())
    }
    #[tokio::test]async fn websocket_paused_seek_is_inclusive_first_last_and_step_applies_exactly_one_frame()->Result<()>{
        use futures_util::SinkExt;
        let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw"),Default::default())?;let received=DateTime::from_timestamp(1_800_030_000,123456789).unwrap();
        for sequence in 1..=3{capture.append(&recorded_history(sequence,1_800_000_000,1,4_000_000_000+sequence as i64*100_000_000,received)?).await?;}
        let catalog=Arc::new(Catalog::embedded()?);let definition=catalog.get("XAUUSD")?.clone();let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;let (stop,shutdown)=tokio::sync::watch::channel(false);
        let c=capture.clone();let app=Router::new().route("/api/replay/stream/XAUUSD",get(move|ws:WebSocketUpgrade|{let (capture,catalog,schedule,shutdown)=(c.clone(),catalog.clone(),schedule.clone(),shutdown.clone());async move{ws.on_upgrade(move|mut socket|async move{let query=ReplayQuery{period:None,source_id:Some("jin10_client".into()),start_sequence:Some("1".into()),end_sequence:None,received_at_ns:None,paused:Some(true)};let _=play_inputs(&mut socket,&capture,catalog,shutdown,"XAUUSD",Period::M1,query,"jin10_client".into(),schedule).await;})}}));
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await?;let address=listener.local_addr()?;let server=tokio::spawn(async move{axum::serve(listener,app).await});
        let (mut client,_)=tokio_tungstenite::connect_async(format!("ws://{address}/api/replay/stream/XAUUSD?period=1m&source_id=jin10_client&start_sequence=1&paused=true")).await?;
        async fn next_kind(client:&mut tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,kind:&str)->Result<Value>{loop{let message=tokio::time::timeout(Duration::from_secs(15),client.next()).await?.context("replay socket closed")??;if let tokio_tungstenite::tungstenite::Message::Text(text)=message{let value:Value=serde_json::from_str(&text)?;ensure!(value["state"]!="unavailable","replay failed: {}",value);if value["kind"]==kind{return Ok(value)}}}}
        let first=next_kind(&mut client,"snapshot").await?;ensure!(first["stream_sequence"]=="1"&&first["input_watermark"]["sequence"]=="1"&&first["quant_snapshot"]["evidence"]["token"]["committed_frame_seq"]=="1","first paused snapshot shows an unapplied cursor");
        ensure!(first["state"]=="paused"&&first["paused"]==true,"snapshot left paused UI in seeking");let paused=next_kind(&mut client,"status").await?;ensure!(paused["state"]=="paused"&&paused["input_watermark"]["sequence"]=="1","seek omitted the explicit completed paused state");
        let expected_clock=capture.get(1).await?.logical_at_ns.to_string();ensure!(first["logical_at_ns"]==expected_clock,"seek clock belongs to a different prefix");
        client.send(tokio_tungstenite::tungstenite::Message::Text(r#"{"command":"step"}"#.into())).await?;let stepped=next_kind(&mut client,"frame").await?;ensure!(stepped["input_watermark"]["sequence"]=="2"&&stepped["quant_snapshot"]["evidence"]["token"]["committed_frame_seq"]=="2","step did not apply the next exact frame");
        client.send(tokio_tungstenite::tungstenite::Message::Text(r#"{"command":"seek","sequence":"3"}"#.into())).await?;let last=next_kind(&mut client,"snapshot").await?;ensure!(last["stream_sequence"]=="3"&&last["input_watermark"]["sequence"]=="3"&&last["quant_snapshot"]["evidence"]["token"]["committed_frame_seq"]=="3","last paused seek omitted its target frame");
        client.send(tokio_tungstenite::tungstenite::Message::Text(r#"{"command":"seek","sequence":"1"}"#.into())).await?;let repeated=next_kind(&mut client,"snapshot").await?;ensure!(repeated["quant_snapshot"]["evidence"]["input_hash"]==first["quant_snapshot"]["evidence"]["input_hash"],"future frames/seek changed the first cursor business hash");ensure!(repeated["checkpoint_sequence"]=="1"&&repeated["decoded_tail_frames"]=="0","paused reverse seek did not restore complete facts and shared quant checkpoint");
        client.send(tokio_tungstenite::tungstenite::Message::Text(r#"{"command":"stop"}"#.into())).await?;client.close(None).await?;let _=stop.send(true);server.abort();capture.close_and_drain().await?;Ok(())
    }
    #[tokio::test]async fn complete_facts_quant_checkpoint_short_tail_and_reverse_seek_match_cold_exact_prefix()->Result<()>{
        let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw"),Default::default())?;let received=DateTime::from_timestamp(1_800_030_000,123456789).unwrap();capture.append(&recorded_history(1,1_800_000_000,320,4_000_000_000,received)?).await?;
        let mut empty=recorded_quote(2);empty.channel="jin10_local".into();empty.encoding="session-decrypted".into();empty.body=vec![0,0];empty.received_at=received;capture.append(&empty).await?;capture.append(&recorded_history(3,1_800_000_000,1,4_200_000_000,received)?).await?;capture.append(&recorded_history(4,1_800_050_000,1,9_000_000_000,received+chrono::Duration::seconds(1))?).await?;
        let catalog=Arc::new(Catalog::embedded()?);let definition=catalog.get("XAUUSD")?.clone();let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;let scope=scope(&catalog,&definition.instrument,"jin10_client",Period::M1,&schedule,&capture.name)?;let new=||Projector::new_facts(catalog.clone(),definition.instrument.clone(),"jin10_client".into(),Period::M1,schedule.clone());
        let (mut run,_) =rebuild_facts_inclusive(&capture,new()?,&scope,1,2,SeekToken::default()).await?;run.accumulator=Some(quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),None).await?);let before=run.accumulator.as_ref().unwrap().current()?;run.checkpoint(&capture,&scope,SeekToken::default()).await?;
        run.apply(vec![capture.get(3).await?],SeekToken::default()).await?;run.accumulator=Some(quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),run.accumulator.take()).await?);let corrected=run.accumulator.as_ref().unwrap().current()?;run.checkpoint(&capture,&scope,SeekToken::default()).await?;ensure!(run.checkpoints.len()==2,"complete CP metadata absent");
        let restored=run.restore_checkpoint(&capture,&scope,2,SeekToken::default()).await?;ensure!(restored==Some(2)&&run.checkpoints.len()==1,"reverse restore failed to invalidate future facts/quant CP");ensure!(run.accumulator.as_ref().unwrap().current()?.evidence.snapshot_hash==before.evidence.snapshot_hash&&run.store.version().await?.committed_capture==Some(capture.get(2).await?.position),"quant restored separately from canonical facts prefix");
        let (mut run,decoded,cp)=rebuild_facts_cached(&capture,new()?,&scope,1,3,Some(run),SeekToken::default()).await?;ensure!(decoded==1&&cp==Some(2),"complete facts CP decoded more than bounded raw tail");run.accumulator=Some(quant::calculate(Arc::new(run.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),run.accumulator.take()).await?);let hot=run.accumulator.as_ref().unwrap().current()?;
        let (cold,_) =rebuild_facts_inclusive(&capture,new()?,&scope,1,3,SeekToken::default()).await?;let oracle=quant::calculate(Arc::new(cold.quant_view(&scope,&definition,Period::M1,&schedule,true).await?),Default::default(),None).await?.current()?;ensure!(hot.evidence.snapshot_hash==oracle.evidence.snapshot_hash&&hot.evidence.input_hash==oracle.evidence.input_hash&&hot.evidence.input_count==oracle.evidence.input_count&&hot.evidence.input_hash==corrected.evidence.input_hash,"short-tail correction/preview count differs from empty-seed full prefix");
        ensure!(run.store.generation_summary().await?["counts"]["bar_rows"]=="320"&&run.projector.as_ref().unwrap().view.is_empty(),"facts CP copied a truncated chart view or future row");run.store.close().await?;cold.store.close().await?;capture.close().await?;Ok(())
    }
    #[tokio::test]async fn checkpoint_short_tail_matches_empty_prefix_and_future_does_not_leak()->Result<()> {
        let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw.redb"),Default::default())?;
        let catalog=Arc::new(Catalog::embedded()?);let d=catalog.get("XAUUSD")?;
        let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&d.market_schedule_id].clone())?;
        let mut positions=vec![];for seq in 1..=330 {positions.push(capture.append(&recorded_quote(seq)).await?.position);}
        let boundary=scope(&catalog,&d.instrument,"jin10_client",Period::M1,&schedule,&positions[0].epoch)?;
        let empty=||Projector::new(catalog.clone(),d.instrument.clone(),"jin10_client".into(),Period::M1,schedule.clone());
        let (first,decoded,restored)=rebuild(&capture,empty()?,&boundary,1,320,true).await?;
        ensure!(decoded==319&&restored.is_none(),"first seek unexpectedly skipped retained evidence");
        let expected=state_hash(&first.snapshot()?);
        let (second,decoded,restored)=rebuild(&capture,empty()?,&boundary,1,320,true).await?;
        ensure!(restored==Some(256)&&decoded==63,"checkpoint seek decoded the entire prefix");
        ensure!(state_hash(&second.snapshot()?)==expected,"checkpoint restore changed exact replay state");
        capture.append(&recorded_quote(331)).await?;
        let (third,_,_)=rebuild(&capture,empty()?,&boundary,1,320,true).await?;
        ensure!(state_hash(&third.snapshot()?)==expected,"future append changed historical state");
        let mut independent=empty()?;for seq in 1..320{independent.accept_record(&capture.get(seq).await?)?;}
        ensure!(state_hash(&independent.snapshot()?)==expected,"same raw prefix differed from independent evaluator");
        let mut changed=boundary.clone();changed.source="tonghuashun_futures".into();ensure!(capture.nearest_checkpoint(&changed.key()?,319).await?.is_none(),"source reused another seed");
        capture.close().await?;Ok(())
    }
    #[test]fn playback_rejects_unsafe_speed_and_bounds_and_retains_u64(){
        let mut p=Playback::new();assert!(p.control(Message::Text(r#"{"command":"speed","value":0}"#.into()),10,20).is_err());
        assert!(p.control(Message::Text(r#"{"command":"seek","sequence":"9"}"#.into()),10,20).is_err());
        p.control(Message::Text(r#"{"command":"step"}"#.into()),10,20).unwrap();assert!(p.paused);assert_eq!(p.steps,1);
        p.control(Message::Text(r#"{"command":"seek","sequence":"18446744073709551615"}"#.into()),1,u64::MAX).unwrap();assert_eq!(p.seek,Some(u64::MAX));
    }
}
