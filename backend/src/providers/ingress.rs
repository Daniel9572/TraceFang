//! A work credit is acquired before provider IO and held through raw durability.
use std::sync::{Arc,atomic::{AtomicBool,AtomicU64,Ordering}};
use anyhow::{Result,Context,ensure};
use serde_json::{Value,json};
use tokio::sync::{mpsc,oneshot,Semaphore,OwnedSemaphorePermit};
use crate::capture::ProviderFrame;
use tracefang_core::persistence_contract::DurableReceipt;
pub const MAX_DECODED_BODY:usize=32*1024*1024;
pub const MAX_ENCODED_FRAME:usize=48*1024*1024;
const WORK_CREDIT:u32=128*1024*1024;
const TOTAL_CREDIT:usize=1536*1024*1024;
#[derive(Default)]struct Metrics {reserved:AtomicU64,owned:AtomicU64,peak:AtomicU64,backpressure:AtomicU64,rejected:AtomicU64,work:AtomicU64}
struct Inner {sender:mpsc::Sender<Envelope>,budget:Arc<Semaphore>,closed:AtomicBool,metrics:Metrics}
#[derive(Clone)]pub struct FrameSink {inner:Arc<Inner>}
pub struct WorkReservation {inner:Arc<Inner>,permit:Option<OwnedSemaphorePermit>,reserved:u64,actual:u64}
pub struct Envelope {pub frame:ProviderFrame,pub reply:Option<oneshot::Sender<Result<DurableReceipt>>>,_work:WorkReservation}
impl Drop for WorkReservation {fn drop(&mut self){self.inner.metrics.reserved.fetch_sub(self.reserved,Ordering::AcqRel);self.inner.metrics.owned.fetch_sub(self.actual,Ordering::AcqRel);self.inner.metrics.work.fetch_sub(1,Ordering::AcqRel);}}
impl WorkReservation {
    /// Current owned buffers, including temporary raw/base64/serialized copies.
    pub fn observe(&mut self,bytes:usize)->Result<()> {ensure!(bytes<=self.reserved as usize,"provider work exceeds reserved construction budget");let old=self.actual;self.actual=bytes as u64;
        let current=if self.actual>=old{self.inner.metrics.owned.fetch_add(self.actual-old,Ordering::AcqRel)+self.actual-old}else{self.inner.metrics.owned.fetch_sub(old-self.actual,Ordering::AcqRel)-(old-self.actual)};
        self.inner.metrics.peak.fetch_max(current,Ordering::AcqRel);Ok(())}
    fn finish(mut self,frame:ProviderFrame,reply:Option<oneshot::Sender<Result<DurableReceipt>>>)->Result<Envelope> {
        ensure!(frame.body.len()<=MAX_ENCODED_FRAME,"encoded provider frame exceeds 48MiB limit");
        let bytes=frame.body.capacity()+frame.channel.capacity()+frame.connection_id.capacity()+frame.encoding.capacity()+512;
        self.observe(bytes)?;let retain=u32::try_from(bytes)?.max(1);let release=WORK_CREDIT.checked_sub(retain).context("owned frame exceeds work credit")?;
        if release>0 {drop(self.permit.as_mut().expect("work permit").split(release as usize).context("work permit split")?);self.reserved-=release as u64;self.inner.metrics.reserved.fetch_sub(release as u64,Ordering::AcqRel);}
        Ok(Envelope {frame,reply,_work:self})
    }
}
impl FrameSink {
    pub fn channel()->(Self,mpsc::Receiver<Envelope>) {let(sender,receiver)=mpsc::channel(32);(Self {inner:Arc::new(Inner {sender,budget:Arc::new(Semaphore::new(TOTAL_CREDIT)),closed:AtomicBool::new(false),metrics:Metrics::default()})},receiver)}
    pub async fn reserve(&self)->Result<WorkReservation> {
        ensure!(!self.inner.closed.load(Ordering::Acquire),"provider ingress closed");
        if self.inner.budget.available_permits()<WORK_CREDIT as usize{self.inner.metrics.backpressure.fetch_add(1,Ordering::Relaxed);}
        let permit=self.inner.budget.clone().acquire_many_owned(WORK_CREDIT).await?;
        ensure!(!self.inner.closed.load(Ordering::Acquire),"provider ingress closed");
        self.inner.metrics.reserved.fetch_add(WORK_CREDIT as u64,Ordering::AcqRel);self.inner.metrics.work.fetch_add(1,Ordering::AcqRel);
        Ok(WorkReservation {inner:self.inner.clone(),permit:Some(permit),reserved:WORK_CREDIT as u64,actual:0})
    }
    pub async fn send(&self,frame:ProviderFrame,work:WorkReservation)->Result<()> {
        let envelope=work.finish(frame,None).inspect_err(|_|{self.inner.metrics.rejected.fetch_add(1,Ordering::Relaxed);})?;
        self.inner.sender.send(envelope).await.map_err(|_|anyhow::anyhow!("raw publisher stopped before accepting provider frame"))
    }
    pub async fn append(&self,frame:ProviderFrame,work:WorkReservation)->Result<DurableReceipt> {
        let(tx,rx)=oneshot::channel();let envelope=work.finish(frame,Some(tx)).inspect_err(|_|{self.inner.metrics.rejected.fetch_add(1,Ordering::Relaxed);})?;
        self.inner.sender.send(envelope).await.map_err(|_|anyhow::anyhow!("raw publisher stopped before accepting provider frame"))?;rx.await.context("raw durability receipt task stopped")?
    }
    pub fn close(&self){self.inner.closed.store(true,Ordering::Release);self.inner.budget.close();}
    pub fn status(&self)->Value {let m=&self.inner.metrics;json!({"budget_bytes":TOTAL_CREDIT.to_string(),"work_credit_bytes":WORK_CREDIT.to_string(),"max_decoded_body_bytes":MAX_DECODED_BODY.to_string(),"max_encoded_frame_bytes":MAX_ENCODED_FRAME.to_string(),"reserved_bytes":m.reserved.load(Ordering::Acquire).to_string(),"actual_in_flight_bytes":m.owned.load(Ordering::Acquire).to_string(),"peak_owned_bytes":m.peak.load(Ordering::Acquire).to_string(),"in_flight_work":m.work.load(Ordering::Acquire).to_string(),"backpressure_events":m.backpressure.load(Ordering::Relaxed).to_string(),"rejected_frames":m.rejected.load(Ordering::Relaxed).to_string(),"queue_depth":self.inner.sender.max_capacity()-self.inner.sender.capacity(),"closed":self.inner.closed.load(Ordering::Acquire)})}
}
