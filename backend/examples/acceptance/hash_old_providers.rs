#[path="../../src/providers/jin10.rs"]pub mod jin10;
#[path="../../src/providers/tonghuashun.rs"]pub mod tonghuashun;
#[path="../../src/providers/shfe.rs"]pub mod shfe;
#[path="../../src/providers/ingress.rs"]pub mod ingress;
#[path="../../src/providers/fuyao.rs"]pub mod fuyao;

use std::{collections::BTreeMap,time::Duration,sync::Arc};
use anyhow::{Result,Context,bail};
use base64::{Engine,engine::general_purpose::STANDARD};
use chrono::Utc;
use serde_json::{Value,json};
use tokio::sync::{mpsc,watch};
use tracefang_core::domain::{QuoteSnapshot,Candle,Instrument};
use sha2::{Digest,Sha256};
use tracefang_core::periods::CapturedSourceCalendar;
use crate::{capture::{ProviderFrame,CapturedFrame},catalog::{Catalog,Definition,provider_code}};

/// Decoder state is bounded by the catalog and is rebuilt from ordered raw frames.
#[derive(Clone)]
pub struct Decoder {
    catalog:Arc<Catalog>, quotes:BTreeMap<String,QuoteSnapshot>,daily:BTreeMap<String,Value>,sessions:BTreeMap<String,Value>,calendar_authorities:BTreeMap<String,CapturedSourceCalendar>,
}
impl Decoder {
    pub fn snapshot(&self)->Result<Value> {Ok(json!({"version":3,"quotes":self.quotes,"daily":self.daily,"sessions":self.sessions,"calendar_authorities":self.calendar_authorities.values().collect::<Vec<_>>()}))}
    /// A bounded projection delta; deliberately not a restorable Decoder CP.
    pub fn calendar_projection_for(&self,position:&tracefang_core::persistence_contract::CapturePosition)->Result<Option<Value>> {
        let rows=self.calendar_authorities.values().filter(|row|row.day.capture_position.as_ref()==Some(position)).collect::<Vec<_>>();
        if rows.is_empty(){Ok(None)}else{Ok(Some(json!({"schema":"calendar-projection-only-v1","calendar_authorities":rows})))}
    }
    pub fn restore(catalog:Arc<Catalog>,snapshot:Value)->Result<Self> {
        let authority_rows:Vec<CapturedSourceCalendar>=if snapshot["version"]==3{serde_json::from_value(snapshot["calendar_authorities"].clone())?}else{vec![]};
        anyhow::ensure!(authority_rows.len()<=catalog.items.len(),"checkpoint calendar exceeds catalog");
        let mut calendar_authorities=BTreeMap::new();
        for row in authority_rows {let code=format!("fuyao:{}:{}",row.day.market,row.day.code);let d=catalog.by_provider(&code).context("checkpoint calendar exact scope unsupported")?;
            anyhow::ensure!(row.source_id=="tonghuashun_futures" && row.symbol==d.instrument.symbol && row.day.capture_position.is_some(),"checkpoint calendar provenance differs");
            let schedule=tracefang_core::periods::MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:None,reference:None,sessions:vec![],authority:Some(tracefang_core::periods::CalendarAuthority{date_exceptions:None,absolute_days:vec![row.day.clone()]})};schedule.validate()?;
            anyhow::ensure!(calendar_authorities.insert(code,row).is_none(),"duplicate checkpoint calendar scope");
        }
        let (quotes,daily,sessions):(BTreeMap<String,QuoteSnapshot>,BTreeMap<String,Value>,BTreeMap<String,Value>)=if snapshot.is_array(){let(q,d):(BTreeMap<String,QuoteSnapshot>,BTreeMap<String,Value>)=serde_json::from_value(snapshot)?;(q,d,BTreeMap::new())}else{
            anyhow::ensure!(snapshot["version"]==2 || snapshot["version"]==3,"unsupported decoder checkpoint version");(serde_json::from_value(snapshot["quotes"].clone())?,serde_json::from_value(snapshot["daily"].clone())?,serde_json::from_value(snapshot["sessions"].clone())?)};
        for (code,quote) in &quotes {
            quote.validate()?;
            let definition=catalog.by_provider(code).context("checkpoint provider is unsupported")?;
            anyhow::ensure!(definition.instrument==quote.instrument,"checkpoint provider and quote differ");
        }
        for code in daily.keys(){catalog.by_provider(code).context("checkpoint daily provider is unsupported")?;}
        for (code,time) in &sessions {let definition=catalog.by_provider(code).context("checkpoint session provider is unsupported")?;let mapping=definition.public_feed.as_ref().context("checkpoint session protocol is unsupported")?;fuyao::validate_trade_time(time,mapping)?;}
        Ok(Self{catalog,quotes,daily,sessions,calendar_authorities})
    }
    pub fn new(catalog:Arc<Catalog>)->Self {Self{catalog,quotes:BTreeMap::new(),daily:BTreeMap::new(),sessions:BTreeMap::new(),calendar_authorities:BTreeMap::new()}}
    pub fn decode_record(&mut self,record:&CapturedFrame)->Result<(Vec<QuoteSnapshot>,Vec<Candle>)> {
        let decoded=self.decode(&record.frame)?;
        if record.frame.channel.starts_with("tonghuashun_") {
            let envelope:Value=serde_json::from_slice(&record.frame.body)?;
            if envelope["kind"]=="trade_time" {let code=envelope["provider_code"].as_str().context("source calendar scope missing")?;
                let definition=self.catalog.by_provider(code).context("source calendar exact scope unsupported")?;let mapping=definition.public_feed.as_ref().context("source calendar protocol unsupported")?;
                let bytes=STANDARD.decode(envelope["content_base64"].as_str().context("source calendar body missing")?)?;
                let time=self.sessions.get(code).context("validated source calendar state missing")?;
                let day=fuyao::absolute_day(time,mapping,record.frame.received_at.timestamp_nanos_opt().context("source calendar receive clock out of bounds")?,hex::encode(Sha256::digest(&bytes)),record.legacy.is_none().then_some(record.accepted_at_ns),Some(record.position.clone()),envelope["request_url"].as_str().map(str::to_owned))?;
                self.calendar_authorities.insert(code.into(),CapturedSourceCalendar{source_id:"tonghuashun_futures".into(),symbol:definition.instrument.symbol.clone(),day});
            }
        }
        let(mut quotes,mut bars)=decoded;
        for q in &mut quotes{durable_provenance(&mut q.source,record);if self.catalog.by_provider(&q.source.provider_symbol).is_some(){self.quotes.insert(q.source.provider_symbol.clone(),q.clone());}}
        for b in &mut bars{durable_provenance(&mut b.source,record);}Ok((quotes,bars))
    }
    pub fn decode(&mut self,frame:&ProviderFrame)->Result<(Vec<QuoteSnapshot>,Vec<Candle>)> {
        if frame.channel.starts_with("jin10_") {
            return jin10::decode_frame(frame,&self.catalog.items.iter().map(|d|d.instrument.clone()).collect::<Vec<_>>());
        }
        if !frame.channel.starts_with("tonghuashun_") {bail!("unsupported captured channel")}
        let envelope:Value=serde_json::from_slice(&frame.body)?;
        let code=envelope["provider_code"].as_str().context("captured HTTP symbol missing")?;
        let definition=self.catalog.by_provider(code).context("captured HTTP symbol unsupported")?;
        if envelope["status_code"].as_u64()!=Some(200) {bail!("captured provider HTTP response failed")}
        let bytes=STANDARD.decode(envelope["content_base64"].as_str().context("captured HTTP body missing")?)?;
        if let Some(mapping)=&definition.public_feed {
            anyhow::ensure!(envelope["protocol"]==mapping.protocol,"captured protocol differs from configured exact source mapping");
            let payload:Value=serde_json::from_slice(&bytes)?;let mut mapping=mapping.clone();
            if let Some(time)=self.sessions.get(code){mapping.trade_time=time.clone();}
            let mut quotes=vec![];let mut bars=vec![];
            match envelope["kind"].as_str().context("captured Fuyao kind missing")? {
                "time"=>{
                    let mut quote=fuyao::parse_quote(&payload,&definition.instrument,&mapping,&definition.name,frame.received_at)?;
                    let time=quote.source.observed_at.timestamp();
                    let membership=mapping.trade_time["trade_hours"].as_array().into_iter().flatten().filter(|v|v["trade_phase"]=="continuous").flat_map(|v|v["phase_range"].as_array().into_iter().flatten()).any(|range|range["begin_time"].as_i64().zip(range["end_time"].as_i64()).is_some_and(|(start,end)|time>=start && time<end));
                    if !membership {quote.source.raw_payload.as_mut().unwrap()["bar_projection"]=json!("suppressed_outside_verified_source_session");}
                    quotes.push(quote);
                },
                "trade_time"=>{
                    anyhow::ensure!(payload["status_code"]==0,"Fuyao trade_time protocol failed");
                    let time=payload["data"]["time_info"].as_array().and_then(|v|v.iter().find(|v|v["market"]==mapping.market && v["code"]==mapping.code)).context("Fuyao exact trade_time unavailable")?;
                    fuyao::validate_trade_time(time,&mapping)?;
                    self.sessions.insert(code.into(),time.clone());
                },
                "minute_last"|"minute_year"=>bars=fuyao::parse_minutes(&payload,&definition.instrument,&mapping,frame.received_at,self.quotes.get(code).map(|q|q.source.observed_at))?,
                _=>bail!("unsupported Fuyao captured response kind"),
            }
            for quote in &mut quotes {decorate(&mut quote.source,frame);self.quotes.insert(code.into(),quote.clone());}
            for bar in &mut bars {decorate(&mut bar.source,frame);}
            return Ok((quotes,bars));
        }
        let text=decode_text(&bytes);
        let payload=tonghuashun::decode_jsonp(&text)?;
        let mut quotes=vec![];let mut bars=vec![];
        match envelope["kind"].as_str().context("captured HTTP kind missing")? {
            "time"=>{
                let mut quote=tonghuashun::parse_quote(&payload,&definition.instrument,code,&definition.name,calendar_mode(definition),frame.received_at)?;
                if let Some(daily)=self.daily.get(code) {
                    // Optional statistics cannot reject an independently valid new price.
                    if let Ok(enriched)=tonghuashun::enrich_daily(quote.clone(),daily,&definition.name){quote=enriched}
                }
                quotes.push(quote);
            },
            "daily_last"=>{
                tonghuashun::validate_daily(&payload,&definition.name)?;
                if let Some(quote)=self.quotes.get(code) {
                    let mut quote=tonghuashun::enrich_daily(quote.clone(),&payload,&definition.name)?;
                    let raw=quote.source.raw_payload.get_or_insert_with(||json!({}));
                    raw["observation_kind"]=json!("supplement");
                    raw["response_kind"]=json!("daily_last");
                    raw["supplement_received_at"]=json!(frame.received_at);
                    quotes.push(quote);
                }
                self.daily.insert(code.into(),payload.clone());
            },
            "minute_last"|"minute_year"=>{
                anyhow::ensure!(envelope["period"]=="61" || envelope["period"]==61,"captured v6 minute authority requires exact period61");
                bars=tonghuashun::parse_minutes(&payload,&definition.instrument,code,&definition.name,line_zone(definition),frame.received_at)?;
            },
            _=>bail!("unsupported captured HTTP response kind"),
        }
        for q in &mut quotes {decorate(&mut q.source,frame);self.quotes.insert(code.into(),q.clone());}
        for b in &mut bars {let raw=b.source.raw_payload.get_or_insert_with(||json!({}));raw["authoritative_input"]=json!({"protocol":"tonghuashun_public_line_v6","provider_code":code,"period":"61","response_kind":envelope["kind"],"file":envelope["file"],"body_sha256":counted_body_digest(&bytes),"capture_position":null});decorate(&mut b.source,frame);}
        Ok((quotes,bars))
    }
}
fn durable_provenance(source:&mut tracefang_core::domain::SourceMetadata,record:&CapturedFrame){
    let raw=source.raw_payload.get_or_insert_with(||json!({}));
    if raw["observation_kind"]=="supplement"{raw["supplement_capture_position"]=json!(record.position);raw["supplement_capture_accepted_at_ns"]=json!(record.legacy.is_none().then(||record.accepted_at_ns.to_string()));return;}
    raw["capture_epoch"]= json!(record.position.epoch);raw["capture_sequence"]=json!(record.position.sequence.to_string());raw["capture_digest"]=json!(record.position.digest);raw["capture_accepted_at_ns"]=json!(record.legacy.is_none().then(||record.accepted_at_ns.to_string()));
    if raw["authoritative_input"].is_object(){raw["authoritative_input"]["capture_position"]=json!(record.position);}
}
fn decorate(source:&mut tracefang_core::domain::SourceMetadata,frame:&ProviderFrame) {
    let raw=source.raw_payload.get_or_insert_with(||json!({}));
    raw["frame_connection_id"]=json!(frame.connection_id);raw["frame_sequence"]=json!(frame.sequence.to_string());
    raw["frame_channel"]=json!(frame.channel);
    if raw["observation_kind"]!="supplement" {
        raw["connection_id"]=json!(frame.connection_id);raw["sequence"]=json!(frame.sequence.to_string());
    }
}
pub fn decode_text(bytes:&[u8])->String {
    match std::str::from_utf8(bytes) {Ok(s)=>s.to_owned(),Err(_)=>encoding_rs::GBK.decode(bytes).0.into_owned()}
}
pub fn calendar_mode(d:&Definition)->&'static str {if d.code.starts_with("AU")||d.code.starts_with("AG"){"session_dates"}else{"trade_date"}}
pub fn line_zone(d:&Definition)->chrono_tz::Tz {
    match d.code.as_str(){"USDIND"=>chrono_tz::UTC,"BRN0Y"=>chrono_tz::Europe::London,"IXIC"=>chrono_tz::America::New_York,_=>chrono_tz::Asia::Shanghai}
}
pub fn http_client()->Result<reqwest::Client>{Ok(reqwest::Client::builder().connect_timeout(Duration::from_secs(5)).timeout(Duration::from_secs(15))
    .user_agent("Mozilla/5.0 TraceFang/0.1").build()?)}

pub async fn http_frame(client:&reqwest::Client,d:&Definition,kind:&str,file:&str,connection:&str,sequence:u64)->Result<ProviderFrame>{
    let base=std::env::var("TRACEFANG_THS_BASE_URL").unwrap_or_else(|_|"https://d.10jqka.com.cn".into());
    http_frame_from(client,&base,d,kind,file,connection,sequence).await
}
pub async fn http_frame_reserved(client:&reqwest::Client,d:&Definition,kind:&str,file:&str,connection:&str,sequence:u64,work:&mut ingress::WorkReservation)->Result<ProviderFrame>{
    let base=std::env::var("TRACEFANG_THS_BASE_URL").unwrap_or_else(|_|"https://d.10jqka.com.cn".into());http_frame_accounted(client,&base,d,kind,file,connection,sequence,Some(work)).await
}
async fn http_frame_from(client:&reqwest::Client,base:&str,d:&Definition,kind:&str,file:&str,connection:&str,sequence:u64)->Result<ProviderFrame>{
    http_frame_accounted(client,base,d,kind,file,connection,sequence,None).await
}
async fn http_frame_accounted(client:&reqwest::Client,base:&str,d:&Definition,kind:&str,file:&str,connection:&str,sequence:u64,mut work:Option<&mut ingress::WorkReservation>)->Result<ProviderFrame>{
    let code=provider_code(d);
    let period=if kind=="daily_last"{"01"}else{"61"};
    let url=if kind=="time" {format!("{base}/v6/time/{code}/last.js")}else{format!("{base}/v6/line/{code}/{period}/{file}")};
    let (url,response)=if let Some(mapping)=&d.public_feed {
        let base=std::env::var("TRACEFANG_FUYAO_BASE_URL").unwrap_or_else(|_|"https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1".into());
        let (path,body)=match kind {"time"=>("multi_last_snapshot",fuyao::snapshot_request(mapping)),"trade_time"=>("trade_time",json!({"code_list":[{"market":mapping.market,"codes":[mapping.code]}],"gpid":0})),"minute_last"=>("single_kline",fuyao::minute_request(mapping,300,0)),_=>bail!("Fuyao full-year history capability has not been verified")};
        let url=format!("{}/{path}",base.trim_end_matches('/'));let response=client.post(&url).header("Referer","https://goodsfu.10jqka.com.cn/").json(&body).send().await?;(url,response)
    }else{let response=client.get(&url).header("Referer","https://q.10jqka.com.cn/").send().await?;(url,response)};
    let status=response.status().as_u16();let content_type=response.headers().get("content-type").and_then(|h|h.to_str().ok()).unwrap_or("").to_owned();
    if response.content_length().is_some_and(|n|n>32*1024*1024) {bail!("provider response exceeds frame limit")}
    let mut response=response;let mut body=Vec::new();
    while let Some(chunk)=response.chunk().await? {if body.len()+chunk.len()>32*1024*1024{bail!("provider response exceeds frame limit")}body.extend_from_slice(&chunk);if let Some(work)=work.as_mut(){work.observe(body.capacity()+chunk.len()+16384)?;}}
    let encoded=STANDARD.encode(&body);if let Some(work)=work.as_mut(){work.observe(body.capacity()+encoded.capacity()+16384)?;}drop(body);
    let encoded_capacity=encoded.capacity();let envelope=json!({"version":1,"kind":kind,"provider_code":code,"protocol":d.public_feed.as_ref().map(|m|m.protocol.as_str()),"capability":if kind.starts_with("minute"){"recent_minute_history"}else if kind=="trade_time"{"source_session_evidence"}else{"snapshot"},"request_url":url,"status_code":status,"content_type":content_type,"text_encoding":"utf-8","content_base64":encoded,"period":period,"file":file,"trade_date":null});
    let body=serde_json::to_vec(&envelope)?;if let Some(work)=work.as_mut(){work.observe(body.capacity()+encoded_capacity+16384)?;}drop(envelope);
    if let Some(work)=work.as_mut(){work.observe(body.capacity()+16384)?;}
    anyhow::ensure!(body.len()<=ingress::MAX_ENCODED_FRAME,"encoded HTTP envelope exceeds frame limit");
    Ok(ProviderFrame{version:1,channel:if kind.starts_with("minute"){"tonghuashun_futures_history"}else{"tonghuashun_futures_live"}.into(),
        connection_id:connection.into(),sequence,received_at:Utc::now(),encoding:"tonghuashun_http_v1".into(),
        body})
}

#[derive(Default)]
struct ThsQuoteStatus { quote:Option<QuoteSnapshot>, error:Option<String> }
fn retry_delay(base:u64,failures:u32)->Duration {
    Duration::from_secs((base.saturating_mul(1u64<<failures.min(4))).min(60))
}
fn initial_phase(code:&str,period:u64)->Duration {
    let hash=code.bytes().fold(0u64,|value,byte|value.wrapping_mul(31).wrapping_add(byte as u64));
    Duration::from_millis(hash%(period*1000))
}
fn ths_health(values:&BTreeMap<String,ThsQuoteStatus>,now:chrono::DateTime<Utc>)->jin10::ChannelStatus {
    if values.is_empty(){return jin10::ChannelStatus{state:"idle".into(),error:None}}
    let mut fresh=0;let mut stale=0;let mut problems=vec![];
    for (code,value) in values {
        if let Some(error)=&value.error {problems.push(format!("{code}：{error}"));}
        else if let Some(quote)=&value.quote {
            if quote.source.is_fresh(now,30){fresh+=1}else{stale+=1;problems.push(format!("{code}：所选来源报价已过期"));}
        } else {problems.push(format!("{code}：等待有效报价"));}
    }
    let state=if fresh==values.len(){"connected"}else if fresh>0{"degraded"}else if stale>0{"stale"}else{"unavailable"};
    jin10::ChannelStatus{state:state.into(),error:(!problems.is_empty()).then(||problems.join("；"))}
}

pub fn spawn_ths(catalog:Arc<Catalog>,subscriptions:watch::Receiver<Vec<Instrument>>,shutdown:watch::Receiver<bool>,frames:ingress::FrameSink)->jin10::ProviderTask {
    let client=match http_client(){Ok(c)=>c,Err(_)=>{
        let (_,status)=watch::channel(jin10::ChannelStatus{state:"unavailable".into(),error:Some("HTTP 客户端初始化失败".into())});
        return jin10::ProviderTask{task:tokio::spawn(async{}),status}
    }};
    let base=std::env::var("TRACEFANG_THS_BASE_URL").unwrap_or_else(|_|"https://d.10jqka.com.cn".into());
    spawn_ths_with_client(catalog,subscriptions,shutdown,frames,client,base)
}
fn spawn_ths_with_client(catalog:Arc<Catalog>,mut subscriptions:watch::Receiver<Vec<Instrument>>,mut shutdown:watch::Receiver<bool>,frames:ingress::FrameSink,client:reqwest::Client,base:String)->jin10::ProviderTask {
    let (health,status)=watch::channel(jin10::ChannelStatus{state:"connecting".into(),error:None});
    let task=tokio::spawn(async move {
        // Six reserved price requests and two supplementary requests keep slow history
        // from consuming every slot. Larger watchlists may still queue; health checks
        // the source clock each second and never presents queued stale prices as live.
        let price_gate=Arc::new(tokio::sync::Semaphore::new(6));
        let supplement_gate=Arc::new(tokio::sync::Semaphore::new(2));
        let values=Arc::new(std::sync::Mutex::new(BTreeMap::<String,ThsQuoteStatus>::new()));
        let mut tasks=BTreeMap::<String,Vec<watch::Sender<bool>>>::new();
        let mut workers=tokio::task::JoinSet::new();
        let mut clock=tokio::time::interval(Duration::from_secs(1));
        clock.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        loop {
            if *shutdown.borrow(){break}
            let desired=subscriptions.borrow_and_update().iter().filter_map(|i|catalog.get(&i.symbol).ok().map(|d|(d.code.clone(),d.clone()))).collect::<BTreeMap<_,_>>();
            let removed=tasks.keys().filter(|key|!desired.contains_key(*key)).cloned().collect::<Vec<_>>();
            for key in removed {
                if let Some(handles)=tasks.remove(&key){for stop in handles{stop.send_replace(true);}}
                values.lock().expect("THS health lock").remove(&key);
            }
            for (key,definition) in desired {
                if tasks.contains_key(&key){continue}
                values.lock().expect("THS health lock").insert(key.clone(),ThsQuoteStatus::default());
                let connection=uuid::Uuid::new_v4().simple().to_string();
                let sequence=Arc::new(std::sync::atomic::AtomicU64::new(0));
                let mut handles=vec![];
                for price in [true,false] {
                    let gate=if price{price_gate.clone()}else{supplement_gate.clone()};
                    let (client,base,catalog,definition,gate,values,health,frames,connection,sequence)=(client.clone(),base.clone(),catalog.clone(),definition.clone(),gate.clone(),values.clone(),health.clone(),frames.clone(),connection.clone(),sequence.clone());
                    let (stop,mut worker_stop)=watch::channel(false);handles.push(stop);let mut worker_shutdown=shutdown.clone();
                    workers.spawn(async move {
                        let period=if price{5}else{30};let mut failures=0u32;
                        let mut delay=initial_phase(&definition.code,period);
                        loop {
                            tokio::select!{_=tokio::time::sleep(delay)=>{},_=worker_stop.changed()=>return,_=worker_shutdown.changed()=>return};
                            if *worker_stop.borrow() || *worker_shutdown.borrow(){return}
                            let mut failed=false;
                            for kind in if price{vec!["time"]}else if definition.public_feed.is_some(){vec!["trade_time","minute_last"]}else{vec!["daily_last","minute_last"]} {
                                let permit=tokio::select!{result=gate.acquire()=>match result{Ok(permit)=>permit,Err(_)=>return},_=worker_stop.changed()=>return,_=worker_shutdown.changed()=>return};
                                let id=sequence.fetch_add(1,std::sync::atomic::Ordering::Relaxed)+1;
                                let mut work=tokio::select!{result=frames.reserve()=>match result{Ok(work)=>work,Err(_)=>return},_=worker_stop.changed()=>return,_=worker_shutdown.changed()=>return};
                                let result=http_frame_accounted(&client,&base,&definition,kind,"last.js",&connection,id,Some(&mut work)).await;
                                drop(permit);
                                match result {
                                    Ok(frame)=>{
                                        if price {
                                            let decoded=Decoder::new(catalog.clone()).decode(&frame).and_then(|(quotes,_)|quotes.into_iter().next().context("HTTP response contained no quote"));
                                            failed|=decoded.is_err();
                                            let mut rows=values.lock().expect("THS health lock");
                                            if let Some(value)=rows.get_mut(&definition.code){match decoded{
                                                Ok(quote)=>{value.quote=Some(quote);value.error=None},
                                                Err(_)=>value.error=Some("行情响应未通过状态、品种或数值验证".into()),
                                            }}
                                            health.send_replace(ths_health(&rows,Utc::now()));
                                        }
                                        if !price{failed|=Decoder::new(catalog.clone()).decode(&frame).is_err();}
                                        // Keep received failure evidence for ordered projection diagnostics.
                                        if frames.send(frame,work).await.is_err(){return}
                                    },
                                    Err(_)=>{
                                        failed=true;
                                        if price {
                                            let mut rows=values.lock().expect("THS health lock");
                                            if let Some(value)=rows.get_mut(&definition.code){value.error=Some("行情请求失败，自动重试中".into());}
                                            health.send_replace(ths_health(&rows,Utc::now()));
                                        }
                                    },
                                }
                            }
                            failures=if failed{failures.saturating_add(1)}else{0};
                            delay=retry_delay(period,failures);
                        }
                    });
                }
                tasks.insert(key,handles);
            }
            health.send_replace(ths_health(&values.lock().expect("THS health lock"),Utc::now()));
            tokio::select! {
                _=shutdown.changed()=>{},
                _=subscriptions.changed()=>{},
                _=clock.tick()=>{},
                _=workers.join_next(),if !workers.is_empty()=>{},
            }
        }
        for stops in tasks.into_values(){for stop in stops{stop.send_replace(true);}}
        while workers.join_next().await.is_some(){}
        health.send_replace(jin10::ChannelStatus{state:"stopped".into(),error:None});
    });
    jin10::ProviderTask{task,status}
}

#[cfg(test)]
mod tests {
    use super::*;
    use tracefang_core::{domain::Decimal,events::{MarketEvent,quote_event_id},reducer::{BarContract,BarReducer,SeriesKey}};

    fn frame(kind:&str,payload:Value,sequence:u64,received_at:chrono::DateTime<Utc>)->ProviderFrame {
        ProviderFrame{version:1,channel:"tonghuashun_futures_live".into(),connection_id:"same-session".into(),sequence,received_at,encoding:"tonghuashun_http_v1".into(),
            body:serde_json::to_vec(&json!({"kind":kind,"provider_code":"qh_au8888","status_code":200,
                "content_base64":STANDARD.encode(format!("quote({payload});"))})).unwrap()}
    }
    #[test]
    fn daily_enrichment_preserves_price_clock_and_cannot_create_bar_or_price_event() {
        let catalog=Arc::new(Catalog::embedded().unwrap());let d=catalog.by_provider("qh_au8888").unwrap();
        let time=json!({"qh_au8888":{"name":d.name,"date":"20261008","dates":["20260930","20261001","20261008"],
            "tradeTime":["2100-0230","0900-1500"],"pre":"895.20","data":"0230,908.34,0,0,0"}});
        let received="2026-09-30T18:30:10Z".parse().unwrap();
        let mut decoder=Decoder::new(catalog.clone());let mut reducer=BarReducer::new(vec![BarContract::new("tonghuashun_futures","tonghuashun_futures",vec!["tonghuashun_futures".into()])]).unwrap();
        let (quotes,_)=decoder.decode(&frame("time",time.clone(),7,received)).unwrap();let quote=&quotes[0];
        let initial_id=quote_event_id(quote);let event=reducer.normalize_quote(quote.clone()).unwrap().unwrap();
        assert_eq!(event.sequence,Some(7));let before=reducer.apply(MarketEvent::Quote(event)).unwrap();assert!(!before.is_empty());
        let mut cache=crate::quotes::QuoteCache::default();cache.accept(quote.clone());
        let daily=json!({"name":d.name,"data":"20261008,900,910,899,908,100,0"});
        let (supplements,bars)=decoder.decode(&frame("daily_last",daily,8,received+chrono::Duration::seconds(10))).unwrap();
        assert!(bars.is_empty());let supplement=&supplements[0];
        assert_eq!(supplement.source.observed_at,quote.source.observed_at);
        assert_eq!(supplement.source.received_at,quote.source.received_at);
        assert_eq!(supplement.last,quote.last);assert_eq!(supplement.volume,Some(Decimal::from(100)));
        assert_eq!(quote_event_id(supplement),initial_id);
        assert!(tracefang_core::events::is_quote_supplement(supplement));
        assert!(reducer.normalize_quote(supplement.clone()).unwrap().is_none());
        for bar in before {assert!(reducer.latest(&SeriesKey::from_bar(&bar),240).contains(&bar));}
        assert!(cache.accept(supplement.clone()));assert_eq!(cache.get("tonghuashun_futures",&d.instrument.symbol).unwrap().volume,Some(Decimal::from(100)));
        let (next,_)=decoder.decode(&frame("time",time,u64::MAX,received)).unwrap();
        assert_ne!(quote_event_id(&next[0]),initial_id,"same wire timestamp must retain distinct transport observations");
        assert_eq!(reducer.normalize_quote(next[0].clone()).unwrap().unwrap().sequence,Some(u64::MAX));
    }
    #[test]
    fn successful_http_transport_does_not_make_stale_quotes_live() {
        let catalog=Arc::new(Catalog::embedded().unwrap());let d=catalog.by_provider("qh_au8888").unwrap();
        let payload=json!({"qh_au8888":{"name":d.name,"date":"20261008","dates":["20260930","20261001","20261008"],
            "tradeTime":["2100-0230","0900-1500"],"pre":"895.20","data":"0230,908.34,0,0,0"}});
        let now="2026-10-03T13:00:00Z".parse().unwrap();
        let (quotes,_)=Decoder::new(catalog.clone()).decode(&frame("time",payload,1,now)).unwrap();
        let rows=BTreeMap::from([(d.code.clone(),ThsQuoteStatus{quote:Some(quotes[0].clone()),error:None})]);
        assert_eq!(ths_health(&rows,now).state,"stale");
        let mut failed=frame("time",json!({}),2,now);
        let mut body:Value=serde_json::from_slice(&failed.body).unwrap();body["status_code"]=json!(500);failed.body=serde_json::to_vec(&body).unwrap();
        assert!(Decoder::new(catalog).decode(&failed).is_err());
    }

    #[tokio::test]
    async fn slow_or_failed_instrument_does_not_block_another_price_or_claim_healthy() {
        use axum::{Router,routing::get,extract::Path,response::IntoResponse,http::StatusCode};
        let catalog=Arc::new(Catalog::embedded().unwrap());
        let fast=catalog.get("AU8888").unwrap().clone();let slow=catalog.get("AU2610").unwrap().clone();
        let release=Arc::new(tokio::sync::Notify::new());let entered=Arc::new(tokio::sync::Notify::new());
        let handler_catalog=catalog.clone();let handler_release=release.clone();let handler_entered=entered.clone();
        let router=Router::new().route("/v6/time/{code}/last.js",get(move |Path(code):Path<String>|{
            let (catalog,release,entered)=(handler_catalog.clone(),handler_release.clone(),handler_entered.clone());
            async move {
                if code=="qh_au2610" {entered.notify_one();release.notified().await;return (StatusCode::INTERNAL_SERVER_ERROR,"upstream failure".to_owned()).into_response()}
                let d=catalog.by_provider(&code).unwrap();let at=Utc::now().with_timezone(&chrono_tz::Asia::Shanghai);
                let date=at.format("%Y%m%d").to_string();let time=at.format("%H%M").to_string();
                let payload=json!({code:{"name":d.name,"date":date,"dates":[date],"tradeTime":["0000-2359"],"pre":"100","data":format!("{time},120,0,0,0")}});
                (StatusCode::OK,format!("quote({payload});")).into_response()
            }
        })).fallback(||async{(StatusCode::SERVICE_UNAVAILABLE,"supplement unavailable")});
        let listener=tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();let address=listener.local_addr().unwrap();
        let server=tokio::spawn(async move{axum::serve(listener,router).await.unwrap()});
        let (_subscriptions,rx)=watch::channel(vec![slow.instrument,fast.instrument]);
        let (stop,shutdown)=watch::channel(false);let (frames,mut received)=ingress::FrameSink::channel();
        let provider=spawn_ths_with_client(catalog,rx,shutdown,frames,http_client().unwrap(),format!("http://{address}"));
        let observed=tokio::time::timeout(Duration::from_secs(6),async {
            loop {
                let frame=received.recv().await.unwrap().frame;let body:Value=serde_json::from_slice(&frame.body).unwrap();
                if body["kind"]=="time"&&body["provider_code"]=="qh_au8888" {return frame}
            }
        }).await.unwrap();
        assert_eq!(serde_json::from_slice::<Value>(&observed.body).unwrap()["status_code"],200);
        tokio::time::timeout(Duration::from_secs(6),entered.notified()).await.unwrap();
        assert_eq!(provider.status.borrow().state,"degraded");
        release.notify_waiters();
        tokio::time::timeout(Duration::from_secs(1),async {
            loop {let frame=received.recv().await.unwrap().frame;let body:Value=serde_json::from_slice(&frame.body).unwrap();
                if body["kind"]=="time"&&body["provider_code"]=="qh_au2610" {assert_eq!(body["status_code"],500);break}}
        }).await.unwrap();
        assert_eq!(provider.status.borrow().state,"degraded");
        assert!(provider.status.borrow().error.as_ref().unwrap().contains("AU2610"));
        stop.send_replace(true);tokio::time::timeout(Duration::from_secs(1),provider.task).await.unwrap().unwrap();server.abort();
    }

    #[test]
    fn malformed_daily_before_or_after_price_never_poison_price_decode() {
        let catalog=Arc::new(Catalog::embedded().unwrap());let d=catalog.by_provider("qh_au8888").unwrap();
        let time=json!({"qh_au8888":{"name":d.name,"date":"20261008","dates":["20260930","20261001","20261008"],
            "tradeTime":["2100-0230","0900-1500"],"pre":"895.20","data":"0230,908.34,0,0,0"}});
        let received="2026-09-30T18:30:10Z".parse().unwrap();
        for malformed in [json!({"name":"wrong","data":"20261008,900,910,899,908,100,0"}),json!({"name":d.name,"data":"20261008,900"})] {
            let mut decoder=Decoder::new(catalog.clone());
            assert!(decoder.decode(&frame("daily_last",malformed.clone(),1,received)).is_err());assert!(decoder.daily.is_empty());
            let (first,_)=decoder.decode(&frame("time",time.clone(),2,received)).unwrap();assert_eq!(first.len(),1);
            assert!(decoder.decode(&frame("daily_last",malformed,3,received)).is_err());
            let (next,_)=decoder.decode(&frame("time",time.clone(),4,received)).unwrap();assert_eq!(next[0].last,first[0].last);
        }
    }
    #[test]
    fn polling_phases_are_stable_and_failure_backoff_is_bounded_and_resets() {
        assert_eq!(initial_phase("AU8888",5),initial_phase("AU8888",5));
        assert_ne!(initial_phase("AU8888",5),initial_phase("AU2610",5));
        assert!(initial_phase("AU8888",5)<Duration::from_secs(5));
        assert_eq!(retry_delay(5,0),Duration::from_secs(5));
        assert_eq!(retry_delay(5,1),Duration::from_secs(10));
        assert_eq!(retry_delay(5,2),Duration::from_secs(20));
        assert_eq!(retry_delay(5,10),Duration::from_secs(60));
        assert_eq!(retry_delay(5,0),Duration::from_secs(5));
    }

}

static BODY_HASH_CALLS:std::sync::atomic::AtomicUsize=std::sync::atomic::AtomicUsize::new(0);
pub fn body_hash_calls()->usize {BODY_HASH_CALLS.load(std::sync::atomic::Ordering::Relaxed)}
fn counted_body_digest(bytes:&[u8])->String {BODY_HASH_CALLS.fetch_add(1,std::sync::atomic::Ordering::Relaxed);hex::encode(Sha256::digest(bytes))}
