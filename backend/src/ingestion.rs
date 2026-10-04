//! Phased native acquisition: provider work -> durable capture -> atomic facts.
use std::{sync::Arc,time::Duration};
use anyhow::{Result,Context,ensure};
use serde_json::{Value,json};
use tokio::{sync::watch,task::JoinHandle};
use tracefang_core::{domain::Instrument,persistence_contract::CapturePosition};
use crate::{capture::Capture,market::Market,providers::{self,ingress::{FrameSink,Envelope},jin10::{self,ChannelStatus,LocalHandle}}};
#[derive(Clone)]pub struct Acquisition {
    pub frames:FrameSink,pub local:LocalHandle,
    pub web_status:watch::Receiver<ChannelStatus>,pub local_status:watch::Receiver<ChannelStatus>,pub ths_status:watch::Receiver<ChannelStatus>,
    pub projection_status:watch::Receiver<Value>,pub capture_status:watch::Receiver<Value>,
    source_config:Arc<Value>,
    web_subscriptions:watch::Sender<Vec<Instrument>>,ths_subscriptions:watch::Sender<Vec<Instrument>>,
}
pub struct AcquisitionTasks {
    providers:Vec<Option<JoinHandle<()>>>,provider_stop:watch::Sender<bool>,publisher:Option<JoinHandle<Result<()>>>,publisher_stop:watch::Sender<bool>,projector:Option<JoinHandle<Result<()>>>,projector_stop:watch::Sender<bool>,sink:FrameSink,
}
impl AcquisitionTasks {
    /// Handles remain owned by this object if the caller cancels the drain future.
    /// A failed task never prevents collection/drain of the remaining stages.
    pub async fn stop_and_drain(&mut self)->Result<()> {
        let mut failures=Vec::new();self.provider_stop.send_replace(true);
        for slot in &mut self.providers {
            if let Some(task)=slot.as_mut() {if let Err(error)=task.await {failures.push(format!("provider task failed: {error}"));}*slot=None;}
        }
        self.sink.close();self.publisher_stop.send_replace(true);
        if let Some(task)=self.publisher.as_mut() {
            match task.await {Ok(Ok(()))=>{},Ok(Err(error))=>failures.push(format!("raw publisher failed: {error:#}")),Err(error)=>failures.push(format!("raw publisher task failed: {error}"))}self.publisher=None;
        }
        self.projector_stop.send_replace(true);
        if let Some(task)=self.projector.as_mut() {
            match task.await {Ok(Ok(()))=>{},Ok(Err(error))=>failures.push(format!("projection failed: {error:#}")),Err(error)=>failures.push(format!("projection task failed: {error}"))}self.projector=None;
        }
        if self.sink.status()["in_flight_work"]!="0" {failures.push("unpersisted provider work remained after drain".into());}
        ensure!(failures.is_empty(),"{}",failures.join("; "));Ok(())
    }
    /// Deadline fallback is always unclean. No task may outlive persistence close.
    pub async fn abort_and_join(&mut self) {
        tracing::error!(ingress=%self.sink.status(),"aborting acquisition after drain failure; owned unpersisted frames may be discarded");
        self.provider_stop.send_replace(true);self.sink.close();self.publisher_stop.send_replace(true);self.projector_stop.send_replace(true);
        for task in self.providers.iter().filter_map(Option::as_ref){task.abort();}
        if let Some(task)=&self.publisher{task.abort();}if let Some(task)=&self.projector{task.abort();}
        for slot in &mut self.providers {if let Some(task)=slot.take(){let _=task.await;}}
        if let Some(task)=self.publisher.take(){let _=task.await;}if let Some(task)=self.projector.take(){let _=task.await;}
    }
}
fn value_u64(v:&Value)->Option<u64>{v.as_u64().or_else(||v.as_str()?.parse().ok())}
#[cfg(test)]
mod lifecycle_tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize,Ordering};
    #[tokio::test]
    async fn provider_panic_still_drains_publisher_and_projector() {
        let (sink,_receiver)=FrameSink::channel();let(provider_stop,_)=watch::channel(false);
        let(publisher_stop,mut pub_stop)=watch::channel(false);let(projector_stop,mut project_stop)=watch::channel(false);
        let completed=Arc::new(AtomicUsize::new(0));let a=completed.clone();let b=completed.clone();
        let publisher=tokio::spawn(async move {while !*pub_stop.borrow(){pub_stop.changed().await?;}a.fetch_add(1,Ordering::SeqCst);Ok(())});
        let projector=tokio::spawn(async move {while !*project_stop.borrow(){project_stop.changed().await?;}b.fetch_add(1,Ordering::SeqCst);Ok(())});
        let mut tasks=AcquisitionTasks {providers:vec![Some(tokio::spawn(async{panic!("injected provider failure")}))],provider_stop,
            publisher:Some(publisher),publisher_stop,projector:Some(projector),projector_stop,sink:sink.clone()};
        assert!(tasks.stop_and_drain().await.unwrap_err().to_string().contains("provider task failed"));
        assert_eq!(completed.load(Ordering::SeqCst),2);assert!(tasks.providers.iter().all(Option::is_none));assert!(tasks.publisher.is_none()&&tasks.projector.is_none());
        assert_eq!(sink.status()["closed"],true);assert!(sink.reserve().await.is_err());
    }
    #[tokio::test]
    async fn cancelled_drain_retains_handles_until_abort_and_join() {
        let (sink,_receiver)=FrameSink::channel();let(provider_stop,_)=watch::channel(false);
        let(publisher_stop,_)=watch::channel(false);let(projector_stop,_)=watch::channel(false);
        let stopped=Arc::new(AtomicUsize::new(0));
        struct DropProof(Arc<AtomicUsize>);impl Drop for DropProof{fn drop(&mut self){self.0.fetch_add(1,Ordering::SeqCst);}}
        let worker=|flag:Arc<AtomicUsize>|tokio::spawn(async move {let _proof=DropProof(flag);std::future::pending::<()>().await;});
        let provider=worker(stopped.clone());let a=stopped.clone();let b=stopped.clone();
        let publisher=tokio::spawn(async move {let _proof=DropProof(a);std::future::pending::<()>().await;Ok(())});
        let projector=tokio::spawn(async move {let _proof=DropProof(b);std::future::pending::<()>().await;Ok(())});
        tokio::task::yield_now().await;
        let mut tasks=AcquisitionTasks {providers:vec![Some(provider)],provider_stop,publisher:Some(publisher),publisher_stop,projector:Some(projector),projector_stop,sink:sink.clone()};
        assert!(tokio::time::timeout(Duration::from_millis(10),tasks.stop_and_drain()).await.is_err());
        assert!(tasks.providers[0].is_some()&&tasks.publisher.is_some()&&tasks.projector.is_some());
        tasks.abort_and_join().await;assert_eq!(stopped.load(Ordering::SeqCst),3);
        assert!(tasks.providers.iter().all(Option::is_none));assert!(tasks.publisher.is_none()&&tasks.projector.is_none());
        assert_eq!(sink.status()["closed"],true);assert!(sink.reserve().await.is_err());
    }
}
pub async fn validate_recovery(market:&Market,capture:&Capture)->Result<(u64,String,providers::Decoder)> {
    let bounds=capture.bounds().await?;let epoch=bounds["epoch"].as_str().context("capture epoch missing")?.to_owned();
    let first=value_u64(&bounds["first_sequence"]);let last=value_u64(&bounds["last_sequence"]).unwrap_or(0);
    ensure!(bounds["gaps"].as_array().is_some_and(Vec::is_empty),"capture evidence has declared gaps");
    let version=market.store.version().await?;let boundary=market.store.projection_start_boundary().await?;
    if !market.store.read_only(){
        ensure!(version.projector_version==tracefang_core::persistence_contract::PROJECTOR_VERSION,"old projector/source clock checkpoint requires an explicit corrected generation; write recovery is blocked");
        if boundary.is_some(){
            let policy=market.store.metadata("migration","source_clock_policy").await?.context("legacy authority requires a verified source clock mapping manifest")?;
            ensure!(policy["verified"]==true && policy["policy"]==tracefang_core::source_clock::THS_V6_SHFE_END_V2 && policy["mapping_manifest_sha256"].as_str().is_some_and(|v|v.len()==64 && v.bytes().all(|b|b.is_ascii_hexdigit())),"legacy clock correction mapping is unverified");
        }
    }
    let anchor=if let Some(position)=&version.committed_capture {ensure!(position.epoch==epoch,"projection capture epoch differs; explicit recovery required");Some(position.clone())}
        else if let Some(boundary)=boundary {ensure!(boundary.production_terminal || market.store.read_only(),"legacy authority boundary is a rehearsal; acquisition is disabled");ensure!(boundary.raw_tail.epoch==epoch,"legacy authority raw epoch differs");Some(boundary.raw_tail)}else{None};
    let applied=anchor.as_ref().map_or(0,|p|p.sequence);
    ensure!(applied<=last,"committed projection is ahead of raw evidence tail");
    if let Some(anchor)=anchor {capture.get_at(&anchor).await.context("committed/authority raw anchor body or digest failed verification")?;}
    else {ensure!(first.is_none_or(|seq|seq==1) && bounds["origin_prefix_complete"]==true,"initial raw prefix is missing; cannot infer a complete native recovery");}
    let decoder=if let Some(checkpoint)=market.store.metadata("runtime","decoder_checkpoint").await? {
        let position:CapturePosition=serde_json::from_value(checkpoint["position"].clone())?;
        ensure!(version.committed_capture.as_ref()==Some(&position) && checkpoint["projector_version"]==version.projector_version,"decoder checkpoint differs from committed facts prefix");
        providers::Decoder::restore(market.catalog.clone(),checkpoint["decoder"].clone())?
    }else{ensure!(version.committed_capture.is_none(),"committed native facts lack an exact decoder checkpoint; rebuild required");providers::Decoder::new(market.catalog.clone())};
    Ok((applied,epoch,decoder))
}
impl Acquisition {
    pub async fn start(market:Market,capture:Capture)->Result<(Self,AcquisitionTasks)> {
        Self::start_with_acquisition_enabled(market,capture,std::env::var("TRACEFANG_ACQUISITION_ENABLED").as_deref()!=Ok("0")).await
    }
    pub async fn start_with_acquisition_enabled(market:Market,capture:Capture,enabled:bool)->Result<(Self,AcquisitionTasks)> {
        let staged_read_only=market.store.read_only() && std::env::var("TRACEFANG_REHEARSAL_GENERATION").is_ok();
        let (applied,epoch,decoder)=if staged_read_only {
            let version=market.store.version().await?;
            ensure!(version.committed_capture.is_none(),"staged PG fact shadow cannot claim a projected native capture prefix");
            let manifest=market.store.metadata("migration","index_verification").await?.context("staged read-only view requires a verified fact/index manifest")?;
            ensure!(manifest["complete"]==true && manifest["index_verified"]==true && manifest["verified_commit_id"].as_str()==Some(&version.commit_id.to_string()),"staged fact/index manifest is stale or incomplete");
            ensure!(manifest["generation"]==version.active_generation && manifest["store_epoch"]==version.store_epoch,"staged verification manifest identity differs");
            let bounds=capture.bounds().await?;
            let epoch=bounds["epoch"].as_str().context("read-only capture epoch missing")?.to_owned();
            for sequence in [value_u64(&bounds["first_sequence"]),value_u64(&bounds["last_sequence"])].into_iter().flatten(){let record=capture.get(sequence).await?;capture.get_at(&record.position).await.context("read-only retained raw boundary failed body/digest verification")?;}
            (0,epoch,providers::Decoder::new(market.catalog.clone()))
        }else{validate_recovery(&market,&capture).await?};
        if market.store.read_only(){
            let (frames,_receiver)=FrameSink::channel();frames.close();
            let (_w,web_status)=watch::channel(ChannelStatus {state:"read_only".into(),error:None});let local_status=web_status.clone();let ths_status=web_status.clone();
            let (_p,projection_status)=watch::channel(json!({"state":"read_only_shadow","sequence":if staged_read_only{None}else{Some(applied.to_string())},"authority":if staged_read_only{"read_only_staged_pg_snapshot"}else{"rehearsal"},"generation":market.store.version().await?.active_generation,"stage_manifest":market.store.metadata("migration","index_verification").await?,"capture_retained_bounds":capture.bounds().await?,"production_ready":false,"evidence_complete":false,"raw_prefix_projected":false,"detail":"只读核验旧权威事实视图；原始回放保持空起点，没有用最终事实补原始帧前缀"}));
            let (_c,capture_status)=watch::channel(json!({"state":"read_only","epoch":epoch}));
            let (web_subscriptions,_)=watch::channel(vec![]);let(ths_subscriptions,_)=watch::channel(vec![]);
            let(provider_stop,_)=watch::channel(true);let(publisher_stop,_)=watch::channel(true);let(projector_stop,_)=watch::channel(true);
            let tasks=AcquisitionTasks {providers:vec![],provider_stop,publisher:Some(tokio::spawn(async{Ok(())})),publisher_stop,projector:Some(tokio::spawn(async{Ok(())})),projector_stop,sink:frames.clone()};
            return Ok((Self {frames,local:jin10::LocalHandle::disabled(),web_status,local_status,ths_status,projection_status,capture_status,source_config:Arc::new(Value::Null),web_subscriptions,ths_subscriptions},tasks));
        }

        let (web_subscriptions,web_rx)=watch::channel(vec![]);let (ths_subscriptions,ths_rx)=watch::channel(vec![]);
        let (frames,receiver)=FrameSink::channel();let (projection_health,projection_status)=watch::channel(json!({"state":"recovering","sequence":applied.to_string(),"decode_failures":"0"}));
        let (capture_health,capture_status)=watch::channel(json!({"state":"connected"}));
        let (provider_stop,provider_shutdown)=watch::channel(!enabled);
        let web=jin10::spawn_web(web_rx.clone(),provider_shutdown.clone(),frames.clone());
        let(local,local_task)=jin10::spawn_local(web_rx,provider_shutdown.clone(),frames.clone());
        let ths=providers::spawn_ths(market.catalog.clone(),ths_rx,provider_shutdown,frames.clone());
        let (publisher_stop,publish_shutdown)=watch::channel(false);let (projector_stop,project_shutdown)=watch::channel(false);
        let publication=tokio::spawn(publish_frames(capture.clone(),receiver,capture_health,publish_shutdown));
        let projection_market=market.clone();let projection_sender=projection_health.clone();let fatal_stop=provider_stop.clone();
        let projection=tokio::spawn(async move{let outcome=project_frames(projection_market,capture,projection_sender,project_shutdown,applied,epoch,decoder).await;
            if let Err(error)=&outcome{tracing::error!(%error,"native projection blocked; automatic acquisition stopped");projection_health.send_replace(json!({"state":"blocked","detail":"原始证据或事实状态不一致，暂停采集等待恢复"}));fatal_stop.send_replace(true);}outcome});
        let tasks=AcquisitionTasks {providers:vec![Some(web.task),Some(local_task.task),Some(ths.task)],provider_stop,publisher:Some(publication),publisher_stop,projector:Some(projection),projector_stop,sink:frames.clone()};
        let source_config=Arc::new(market.store.metadata("sources","config").await?.unwrap_or(Value::Null));
        let value=Self {frames,local,source_config,web_status:web.status,local_status:local_task.status,ths_status:ths.status,projection_status,capture_status,web_subscriptions,ths_subscriptions};value.reconcile(&market);Ok((value,tasks))
    }
    pub fn reconcile(&self,market:&Market) {
        let codes=market.watchlist.lock().expect("watchlist lock").clone();let mut web=vec![];let mut ths=vec![];
        for code in codes {let Ok(d)=market.catalog.get(&code) else{continue};let Ok(source)=market.source(&d.instrument.symbol) else{continue};if self.source_config["sources"][&source]["enabled"]==false{continue}
            for instrument in std::iter::once(&d.instrument).chain(d.dependencies.iter()) {if instrument==&d.instrument && !d.dependencies.is_empty(){continue}let list=if source=="jin10_client"{&mut web}else{&mut ths};if !list.contains(instrument){list.push(instrument.clone());}}
        }self.web_subscriptions.send_replace(web);self.ths_subscriptions.send_replace(ths);
    }
    pub async fn wait_projected(&self,sequence:u64)->Result<()> {
        let mut status=self.projection_status.clone();tokio::time::timeout(Duration::from_secs(60),async{loop {
            let current=status.borrow().clone();if value_u64(&current["sequence"]).is_some_and(|v|v>=sequence){return Ok(())}
            if current["state"]=="blocked" {anyhow::bail!("projection blocked by an evidence or state consistency failure")}
            status.changed().await.context("projection task stopped")?;
        }}).await.context("projection has not reached requested durable frame")?
    }
}
async fn publish_frames(capture:Capture,mut frames:tokio::sync::mpsc::Receiver<Envelope>,health:watch::Sender<Value>,mut shutdown:watch::Receiver<bool>)->Result<()> {
    loop {
        if *shutdown.borrow(){frames.close();}
        let envelope=tokio::select!{result=frames.recv()=>match result{Some(frame)=>frame,None=>break},_=shutdown.changed()=>{frames.close();continue;}};
        loop {match capture.append(&envelope.frame).await {
            Ok(receipt)=>{health.send_replace(json!({"state":"connected","durable_position":receipt.position,"confirmed_at_ns":receipt.confirmed_at_ns.to_string()}));if let Some(reply)=envelope.reply{let _=reply.send(Ok(receipt));}break;}
            Err(error)=>{tracing::error!(%error,"received raw frame could not be persisted; ingress remains backpressured");health.send_replace(json!({"state":"unavailable","detail":"原始帧保存失败，保留帧并暂停入口"}));tokio::time::sleep(Duration::from_secs(1)).await;}
        }}
        // The remaining envelope/byte credit drops only after Immediate receipt.
    }health.send_replace(json!({"state":"drained"}));Ok(())
}
async fn project_frames(market:Market,capture:Capture,health:watch::Sender<Value>,mut shutdown:watch::Receiver<bool>,mut applied:u64,epoch:String,mut decoder:providers::Decoder)->Result<()> {
    let mut failed=market.store.metadata("runtime","decode_failures").await?.as_ref().and_then(value_u64).unwrap_or(0);
    let mut processed=0u64;
    loop {
        let bounds=capture.bounds_typed().await?;ensure!(bounds.epoch==epoch,"capture epoch changed during projection");let last=bounds.last_sequence.unwrap_or(0);
        ensure!(applied<=last,"projection cursor ahead of evidence tail");
        if applied<last {
            let start=applied.checked_add(1).context("projection sequence exhausted")?;
            let records=capture.scan(&epoch,start,None,32,64*1024*1024).await?;ensure!(!records.is_empty(),"capture evidence gap at next required sequence");
            for record in records {
                ensure!(applied.checked_add(1)==Some(record.position.sequence),"capture scan has a missing or reordered frame");
                health.send_replace(json!({"state":"recovering","sequence":applied.to_string(),"prepared_position":record.position,"target_sequence":last.to_string(),"decode_failures":failed.to_string()}));
                let decode_record=record.clone();let staged=decoder.clone();
                let (candidate,decoded)=tokio::task::spawn_blocking(move||{let mut candidate=staged;let decoded=candidate.decode_record(&decode_record);(candidate,decoded)}).await?;
                match decoded {
                    Ok((quotes,bars))=> {let broadcast=!*shutdown.borrow();market.apply(quotes,bars,record.position.clone(),record.legacy.is_none().then_some(record.accepted_at_ns),broadcast,Some(candidate.snapshot()?)).await?;decoder=candidate;}
                    Err(error)=> {failed=failed.checked_add(1).context("decode failure counter exhausted")?;tracing::warn!(sequence=record.position.sequence,channel=%record.frame.channel,%error,"raw frame quarantined; not a complete market projection");market.record_decode_failure(record.position.clone(),&record.frame.channel,&error.to_string(),Some(decoder.snapshot()?)).await?;}
                }
                applied=record.position.sequence;processed=processed.checked_add(1).context("processed frame counter exhausted")?;
            }
        }
        let caught_up=applied>=last;health.send_replace(json!({"state":if failed>0{"degraded"}else if caught_up{"running"}else{"recovering"},"sequence":applied.to_string(),"committed_position":market.store.version().await?.committed_capture,"target_sequence":last.to_string(),"decode_failures":failed.to_string(),"processed_frames":processed.to_string(),"evidence_complete":failed==0}));
        if *shutdown.borrow() && caught_up {return Ok(())}
        if caught_up {tokio::select!{_=shutdown.changed()=>{},_=tokio::time::sleep(Duration::from_millis(50))=>{}}}
    }
}
