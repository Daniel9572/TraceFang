//! Append-only exact raw capture. Receipts follow redb Immediate commit, never enqueue.
use std::{path::{Path,PathBuf},sync::{Arc,atomic::{AtomicBool,Ordering}}};
use anyhow::{Context,Result,bail,ensure};
use async_nats::{HeaderMap,jetstream};
use chrono::{DateTime,Utc};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use redb::{Database,Durability,TableDefinition,ReadableDatabase,ReadableTable,ReadableTableMetadata};
use sha2::{Digest,Sha256};
use tokio::sync::{mpsc,oneshot,watch,Semaphore,OwnedSemaphorePermit,Mutex};
pub use tracefang_core::persistence_contract::{CapturePosition,DurableReceipt};

#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
pub struct ProviderFrame {
    pub version:u32,
    pub channel:String,
    pub connection_id:String,
    #[serde(with="tracefang_core::persistence_contract::u64_string")]
    pub sequence:u64,
    pub received_at:DateTime<Utc>,
    pub encoding:String,
    pub body:Vec<u8>,
}

impl ProviderFrame {
    pub fn validate(&self)->Result<()> {
        if self.version==0||self.sequence==0 {bail!("invalid frame version or sequence")}
        for value in [&self.channel,&self.connection_id,&self.encoding] {
            if value.is_empty()||value.len()>256||!value.bytes().all(|v|v.is_ascii_alphanumeric()||v==b'_'||v==b'-') {
                bail!("invalid frame identifier");
            }
        }
        Ok(())
    }
    pub fn headers(&self)->Result<HeaderMap> {
        self.validate()?;
        let mut headers=HeaderMap::new();
        for (key,value) in [
            ("Market-Frame-Version",self.version.to_string()),
            ("Market-Frame-Channel",self.channel.clone()),
            ("Market-Frame-Connection",self.connection_id.clone()),
            ("Market-Frame-Sequence",self.sequence.to_string()),
            ("Market-Frame-Received-At",self.received_at.to_rfc3339()),
            ("Market-Frame-Encoding",self.encoding.clone()),
            ("Nats-Msg-Id",format!("{}:{}:{}",self.channel,self.connection_id,self.sequence)),
        ] { headers.insert(key,value); }
        Ok(headers)
    }
    pub fn from_message(msg:jetstream::message::StreamMessage)->Result<Self> {
        Self::from_parts(&msg.headers,&msg.payload)
    }
    pub fn from_parts(headers:&HeaderMap,payload:&[u8])->Result<Self> {
        let get=|key:&str|->Result<String>{Ok(headers.get(key).context("raw frame header missing")?.to_string())};
        let frame=Self {
            version:get("Market-Frame-Version")?.parse()?,channel:get("Market-Frame-Channel")?,
            connection_id:get("Market-Frame-Connection")?,sequence:get("Market-Frame-Sequence")?.parse()?,
            received_at:get("Market-Frame-Received-At")?.parse()?,encoding:get("Market-Frame-Encoding")?,
            body:payload.to_vec(),
        };
        frame.validate()?;
        Ok(frame)
    }
}


const BODIES:TableDefinition<&str,&[u8]>=TableDefinition::new("capture_bodies_sha256_v1");
const FRAMES:TableDefinition<u64,&[u8]>=TableDefinition::new("capture_frames_v1");
const IDENTITIES:TableDefinition<&str,u64>=TableDefinition::new("capture_identities_v1");
const CLOCK:TableDefinition<&[u8],u64>=TableDefinition::new("capture_received_clock_v1");
const META:TableDefinition<&str,&[u8]>=TableDefinition::new("capture_metadata_v1");
const CHECKPOINTS:TableDefinition<&[u8],&[u8]>=TableDefinition::new("capture_checkpoints_v1");

#[derive(Debug,Clone)]
pub struct CaptureOptions {
    pub max_frame_bytes:usize,pub queue_frames:usize,pub queue_bytes:usize,
    pub batch_frames:usize,pub batch_bytes:usize,pub min_free_bytes:u64,pub content_addressed_bodies:bool,
}
impl Default for CaptureOptions {
    fn default()->Self {Self{max_frame_bytes:48*1024*1024,queue_frames:32,queue_bytes:64*1024*1024,
        batch_frames:64,batch_bytes:4*1024*1024,min_free_bytes:256*1024*1024,content_addressed_bodies:true}}
}
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
pub struct LegacyOrigin {pub stream:String,pub epoch:String,pub sequence:String,pub broker_stored_at_ns:String}
#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct CapturedFrame {
    pub position:CapturePosition,pub frame:ProviderFrame,pub accepted_at_ns:i64,pub legacy:Option<LegacyOrigin>,pub logical_at_ns:i64,
    #[serde(default,skip_serializing_if="Option::is_none")]pub clock_policy_version:Option<String>,
}
impl CapturedFrame {
    pub fn dto(&self)->Value {json!({"position":self.position,"stream_sequence":self.position.sequence.to_string(),
        "epoch":self.position.epoch,"frame_received_at":self.frame.received_at,
        "received_at_ns":self.frame.received_at.timestamp_nanos_opt().map(|v|v.to_string()),"logical_at_ns":self.logical_at_ns.to_string(),"clock_policy_version":self.clock_policy_version.as_deref().unwrap_or("legacy-max-received-v1"),
        "knowledge_at_ns":if self.legacy.is_none(){self.logical_at_ns.max(self.accepted_at_ns).to_string()}else{self.logical_at_ns.to_string()},
        "accepted_at_ns":if self.legacy.is_none(){Some(self.accepted_at_ns.to_string())}else{None},"imported_at_ns":if self.legacy.is_some(){Some(self.accepted_at_ns.to_string())}else{None},"storage_request_sample_at_ns":self.accepted_at_ns.to_string(),"durable_at_ns":null,"durability":"Immediate receipt follows commit; persisted acceptance time is not fsync completion",
        "frame_channel":self.frame.channel,"connection_id":self.frame.connection_id,
        "provider_sequence":self.frame.sequence.to_string(),"legacy":self.legacy})}
}
#[derive(Debug,Clone,Serialize,Deserialize)]
struct RecordHeader {
    schema:u32,epoch:String,sequence:u64,prior_digest:String,digest:String,logical_at_ns:i64,
    version:u32,channel:String,connection_id:String,provider_sequence:u64,
    received_at:DateTime<Utc>,encoding:String,accepted_at_ns:i64,body_sha256:String,
    legacy:Option<LegacyOrigin>,
    #[serde(default,skip_serializing_if="Option::is_none")]clock_policy_version:Option<String>,
}
#[derive(Debug,Clone,Serialize,Deserialize)]
struct Metadata {schema:u32,epoch:String,last:u64,bytes:u64,#[serde(default)] logical_body_bytes:u64,#[serde(default)] unique_body_bytes:u64,#[serde(default)] unique_bodies:u64,first:u64,origin_prefix_complete:bool,last_digest:String,last_logical_ns:i64,#[serde(default)] legacy_origins:std::collections::BTreeMap<String,LegacyCoverage>}
#[derive(Debug,Clone,Serialize,Deserialize)]
struct LegacyCoverage {
    stream:String,epoch:String,
    #[serde(with="tracefang_core::persistence_contract::u64_string")] first_sequence:u64,
    #[serde(with="tracefang_core::persistence_contract::u64_string")] last_sequence:u64,
    #[serde(with="tracefang_core::persistence_contract::u64_string")] first_native_sequence:u64,
    missing_prefix:bool,initial_state:String,
}
#[derive(Clone)]
pub struct Capture {
    pub name:String,inner:Arc<Inner>,
}
struct Inner {
    database:CaptureDatabase,path:PathBuf,options:CaptureOptions,sender:mpsc::Sender<Command>,
    bytes:Arc<Semaphore>,reads:Arc<Semaphore>,admission:Mutex<()>,accepting:AtomicBool,
    health:watch::Receiver<Value>,readonly_health:Option<watch::Sender<Value>>,
}
#[derive(Clone)]enum CaptureDatabase {Writable(Arc<Database>),ReadOnly(Arc<redb::ReadOnlyDatabase>)}
impl CaptureDatabase {fn begin_read(&self)->std::result::Result<redb::ReadTransaction,redb::TransactionError>{match self{Self::Writable(db)=>db.begin_read(),Self::ReadOnly(db)=>db.begin_read()}}}
struct Pending {frame:ProviderFrame,legacy:Option<LegacyOrigin>,reply:oneshot::Sender<Result<DurableReceipt>>,_permit:OwnedSemaphorePermit}
enum Command {Append(Pending),Checkpoint{key:Vec<u8>,body:Vec<u8>,reply:oneshot::Sender<Result<()>>,_permit:OwnedSemaphorePermit},Close(oneshot::Sender<Result<()>>)}

impl Capture {
    pub async fn connect(path:&str)->Result<Self> {
        ensure!(!path.contains("://"),"native capture requires a local redb path, not a broker URL");
        let path=path.to_owned();tokio::task::spawn_blocking(move||Self::open(path,CaptureOptions::default())).await?
    }
    pub async fn connect_read_only(path:&str)->Result<Self>{ensure!(!path.contains("://"),"read-only native capture requires a local path");let path=path.to_owned();tokio::task::spawn_blocking(move||Self::open_read_only(path)).await?}
    /// Opens the OS file read-only. No table creation, actor, checkpoint or initialization commit.
    pub fn open_read_only(path:impl AsRef<Path>)->Result<Self>{
        let supplied=path.as_ref();let path=if supplied.is_absolute(){supplied.to_path_buf()}else{std::env::current_dir()?.join(supplied)};
        let db=Arc::new(Database::builder().set_cache_size(128*1024*1024).open_read_only(&path)?);let meta=metadata_in(&db.begin_read()?)?;
        ensure!(matches!(meta.schema,1|2),"unsupported capture schema");
        let options=CaptureOptions::default();let (sender,receiver)=mpsc::channel(1);drop(receiver);
        let (writer,health)=watch::channel(json!({"state":"read_only","epoch":meta.epoch,"writes_disabled":true,"durability":"existing immutable raw evidence"}));
        Ok(Self{name:meta.epoch,inner:Arc::new(Inner{database:CaptureDatabase::ReadOnly(db),path,options,bytes:Arc::new(Semaphore::new(0)),reads:Arc::new(Semaphore::new(2)),sender,admission:Mutex::new(()),accepting:AtomicBool::new(true),health,readonly_health:Some(writer)})})
    }
    pub fn open(path:impl AsRef<Path>,options:CaptureOptions)->Result<Self> {
        let supplied=path.as_ref();let path=if supplied.is_absolute(){supplied.to_path_buf()}else{std::env::current_dir()?.join(supplied)};
        ensure!(options.max_frame_bytes>0 && options.queue_bytes>=options.max_frame_bytes && options.queue_bytes<=u32::MAX as usize,
            "invalid capture byte budget");
        ensure!(options.queue_frames>0&&options.batch_frames>0&&options.batch_bytes>0,"invalid capture batch budget");
        let parent=path.parent().context("capture path has no parent")?;std::fs::create_dir_all(parent)?;
        let database=Arc::new(Database::builder().set_cache_size(128*1024*1024).create(&path)?);
        Self::open_database(database,path,options)
    }
    /// Used for the directed shared-writer experiment; deployed raw capture uses its own file.
    pub fn open_database(database:Arc<Database>,path:PathBuf,options:CaptureOptions)->Result<Self> {
        let parent=path.parent().context("capture path has no parent")?;
        ensure!(options.queue_frames>0&&options.batch_frames>0&&options.batch_bytes>0&&options.max_frame_bytes>0&&options.queue_bytes>=options.max_frame_bytes&&options.queue_bytes<=u32::MAX as usize,"invalid capture budget");
        let mut tx=database.begin_write()?;tx.set_durability(Durability::Immediate)?;
        {tx.open_table(BODIES)?;tx.open_table(FRAMES)?;tx.open_table(IDENTITIES)?;tx.open_table(CLOCK)?;tx.open_table(CHECKPOINTS)?;
         let mut table=tx.open_table(META)?;
         if table.get("metadata")?.is_none(){
            let meta=Metadata{schema:2,epoch:uuid::Uuid::new_v4().to_string(),last:0,bytes:0,logical_body_bytes:0,unique_body_bytes:0,unique_bodies:0,first:0,origin_prefix_complete:true,last_digest:String::new(),last_logical_ns:i64::MIN,legacy_origins:Default::default()};
            let bytes=serde_json::to_vec(&meta)?;table.insert("metadata",bytes.as_slice())?;
         }}
        tx.commit()?;sync_directory(parent)?;
        let meta=read_metadata(&database)?;ensure!(matches!(meta.schema,1|2),"unsupported capture schema");
        let (sender,receiver)=mpsc::channel(options.queue_frames);
        let (health_tx,health)=watch::channel(json!({"state":"ready","epoch":meta.epoch,"durability":"redb_immediate"}));
        let db=database.clone();let actor_options=options.clone();let actor_path=path.clone();
        std::thread::Builder::new().name("tracefang-raw-capture".into()).spawn(move||actor(db,actor_path,actor_options,receiver,health_tx))?;
        Ok(Self{name:meta.epoch,inner:Arc::new(Inner{database:CaptureDatabase::Writable(database),path,bytes:Arc::new(Semaphore::new(options.queue_bytes)),
            reads:Arc::new(Semaphore::new(2)),options,sender,admission:Mutex::new(()),accepting:AtomicBool::new(true),health,readonly_health:None})})
    }
    pub fn path(&self)->&Path {&self.inner.path}
    pub fn is_read_only(&self)->bool {self.inner.readonly_health.is_some()}
    pub fn connected(&self)->bool {self.inner.accepting.load(Ordering::Acquire)&&matches!(self.inner.health.borrow()["state"].as_str(),Some("ready"|"read_only"))}
    pub fn status(&self)->Value {self.inner.health.borrow().clone()}
    pub fn status_watch(&self)->watch::Receiver<Value> {self.inner.health.clone()}
    pub async fn append(&self,frame:&ProviderFrame)->Result<DurableReceipt> {self.append_with_origin(frame,None).await}
    pub async fn append_legacy(&self,frame:&ProviderFrame,origin:LegacyOrigin)->Result<DurableReceipt> {
        ensure!(!origin.stream.is_empty()&&origin.stream.len()<=256&&!origin.epoch.is_empty()&&origin.epoch.len()<=512,"invalid legacy origin identity");
        ensure!(origin.sequence.parse::<u64>()?>0,"legacy sequence must be positive");origin.broker_stored_at_ns.parse::<i64>()?;
        self.append_with_origin(frame,Some(origin)).await
    }
    /// Offline archive delivery stays in input order. Every returned receipt follows
    /// its Immediate actor commit; an admission group may span multiple bounded commits.
    pub async fn append_legacy_batch(&self,rows:Vec<(ProviderFrame,LegacyOrigin)>)->Result<Vec<DurableReceipt>> {
        ensure!(!self.is_read_only(),"read-only capture rejects import");
        ensure!(!rows.is_empty()&&rows.len()<=self.inner.options.batch_frames,"invalid ordered raw import group");
        let mut bytes=0usize;
        for (frame,origin) in &rows{
            frame.validate()?;frame.received_at.timestamp_nanos_opt().context("frame receive time exceeds signed ns")?;
            ensure!(frame.body.len()<=self.inner.options.max_frame_bytes,"raw import frame exceeds configured maximum");
            ensure!(!origin.stream.is_empty()&&origin.stream.len()<=256&&!origin.epoch.is_empty()&&origin.epoch.len()<=512,"invalid legacy origin");
            ensure!(origin.sequence.parse::<u64>()?>0,"legacy sequence must be positive");origin.broker_stored_at_ns.parse::<i64>()?;
            bytes=bytes.checked_add(frame.body.len().max(1)).context("import byte budget overflow")?;
        }
        ensure!(rows.len()==1||bytes<=self.inner.options.batch_bytes,"ordered raw import exceeds batch byte budget");
        let mut permit=self.inner.bytes.clone().acquire_many_owned(bytes.try_into()?).await?;
        let gate=self.inner.admission.lock().await;ensure!(self.inner.accepting.load(Ordering::Acquire),"capture is closed");
        let mut replies=vec![];
        for (frame,origin) in rows {
            let owned=permit.split(frame.body.len().max(1)).context("raw import byte permit differs")?;
            let (reply,receive)=oneshot::channel();
            self.inner.sender.send(Command::Append(Pending{frame,legacy:Some(origin),reply,_permit:owned})).await.context("capture actor ended")?;replies.push(receive);
        }
        drop(gate);drop(permit);
        let mut receipts=vec![];let mut failure=None;
        for reply in replies{match reply.await{Ok(Ok(receipt))=>receipts.push(receipt),Ok(Err(error))=>{failure.get_or_insert(error);},Err(error)=>{failure.get_or_insert(anyhow::anyhow!("capture actor ended without import receipt: {error}"));}}}
        if let Some(error)=failure{return Err(error)}Ok(receipts)
    }
    async fn append_with_origin(&self,frame:&ProviderFrame,legacy:Option<LegacyOrigin>)->Result<DurableReceipt> {
        ensure!(!self.is_read_only(),"read-only capture rejects append");
        frame.validate()?;frame.received_at.timestamp_nanos_opt().context("frame receive time exceeds signed nanosecond contract")?;
        ensure!(frame.body.len()<=self.inner.options.max_frame_bytes,"raw frame exceeds configured maximum; retained caller payload must not be truncated");
        let permit=self.inner.bytes.clone().acquire_many_owned(frame.body.len().max(1).try_into()?).await?;
        let gate=self.inner.admission.lock().await;
        ensure!(self.inner.accepting.load(Ordering::Acquire),"capture is closed to new frames");
        let (reply,receipt)=oneshot::channel();
        self.inner.sender.send(Command::Append(Pending{frame:frame.clone(),legacy,reply,_permit:permit})).await.context("capture actor ended")?;
        drop(gate);
        receipt.await.context("capture actor ended without a durable receipt")?
    }
    async fn read<T:Send+'static>(&self,f:impl FnOnce(&CaptureDatabase)->Result<T>+Send+'static)->Result<T> {
        let permit=self.inner.reads.clone().acquire_owned().await?;let db=self.inner.database.clone();
        tokio::task::spawn_blocking(move||{let _permit=permit;f(&db)}).await?
    }
    pub async fn get(&self,sequence:u64)->Result<CapturedFrame> {
        self.read(move|db|get_record(db,sequence)).await
    }
    pub async fn get_at(&self,position:&CapturePosition)->Result<CapturedFrame> {
        let record=self.get(position.sequence).await?;ensure!(&record.position==position,"capture epoch or prefix digest differs");Ok(record)
    }
    /// Half-open local application sequence range. Never silently skips missing evidence.
    pub async fn scan(&self,epoch:&str,start:u64,end:Option<u64>,max_count:usize,max_bytes:usize)->Result<Vec<CapturedFrame>> {
        ensure!(max_count>0&&max_count<=4096&&max_bytes>0,"invalid scan bound");let epoch=epoch.to_owned();
        self.read(move|db|{
            let tx=db.begin_read()?;let table=tx.open_table(FRAMES)?;let bodies=tx.open_table(BODIES)?;let meta=metadata_in(&tx)?;
            ensure!(meta.epoch==epoch,"capture epoch differs; recovery cannot continue");
            ensure!(start>0,"capture sequence must be positive");
            if meta.last==0||start>meta.last {return Ok(vec![])}
            ensure!(start>=meta.first,"capture retention gap: requested {start}, first {}",meta.first);
            let mut rows=vec![];let mut bytes=0usize;let mut expected=start;
            for value in table.range(start..)? {
                let (key,value)=value?;let seq=key.value();if end.is_some_and(|e|seq>=e){break}
                ensure!(seq==expected,"capture evidence gap at {expected}, next is {seq}");
                let restored_bytes=record_body_bytes(value.value(),&bodies)?;
                if rows.len()>=max_count||(!rows.is_empty()&&bytes+restored_bytes>max_bytes){break}
                bytes+=restored_bytes;rows.push(decode_record(value.value(),seq,&bodies)?);
                expected=seq.checked_add(1).unwrap_or(u64::MAX);
            }
            Ok(rows)
        }).await
    }
    pub async fn bounds_typed(&self)->Result<tracefang_core::persistence_contract::CaptureBounds> {
        Ok(serde_json::from_value(self.bounds().await?)?)
    }
    pub async fn bounds(&self)->Result<Value> {
        self.read(|db|{
            let tx=db.begin_read()?;let meta=metadata_in(&tx)?;let table=tx.open_table(FRAMES)?;
            ensure!(table.len()?==meta.last.saturating_sub(meta.first).saturating_add(u64::from(meta.last>0)),"capture bounds contain a gap");
            let first=if meta.first>0{Some(get_record_in(&tx,meta.first)?)}else{None};
            let last=if meta.last>0{Some(get_record_in(&tx,meta.last)?)}else{None};
            Ok(json!({"stream":meta.epoch,"epoch":meta.epoch,"state":if first.is_some(){"ready"}else{"empty"},
                "first_sequence":first.as_ref().map(|v|v.position.sequence.to_string()),"last_sequence":last.as_ref().map(|v|v.position.sequence.to_string()),
                "first_position":first.as_ref().map(|v|&v.position),"last_position":last.as_ref().map(|v|&v.position),
                "message_count":table.len()?.to_string(),"bytes":meta.bytes.to_string(),"logical_body_bytes":meta.logical_body_bytes.to_string(),"unique_body_bytes":meta.unique_body_bytes.to_string(),"unique_bodies":meta.unique_bodies.to_string(),"body_storage":"sha256 content addressing for v2; legacy inline records remain readable; no body GC",
                "first_received_at":first.as_ref().map(|v|v.frame.received_at),"last_received_at":last.as_ref().map(|v|v.frame.received_at),
                "first_received_at_ns":first.as_ref().map(|v|v.frame.received_at.timestamp_nanos_opt().unwrap().to_string()),
                "last_received_at_ns":last.as_ref().map(|v|v.frame.received_at.timestamp_nanos_opt().unwrap().to_string()),
                "first_accepted_at_ns":first.as_ref().filter(|v|v.legacy.is_none()).map(|v|v.accepted_at_ns.to_string()),"last_accepted_at_ns":last.as_ref().filter(|v|v.legacy.is_none()).map(|v|v.accepted_at_ns.to_string()),
                "first_imported_at_ns":first.as_ref().filter(|v|v.legacy.is_some()).map(|v|v.accepted_at_ns.to_string()),"last_imported_at_ns":last.as_ref().filter(|v|v.legacy.is_some()).map(|v|v.accepted_at_ns.to_string()),"first_storage_request_sample_at_ns":first.as_ref().map(|v|v.accepted_at_ns.to_string()),"last_storage_request_sample_at_ns":last.as_ref().map(|v|v.accepted_at_ns.to_string()),
                "first_durable_at_ns":null,"last_durable_at_ns":null,"first_logical_at_ns":first.as_ref().map(|v|v.logical_at_ns.to_string()),"last_logical_at_ns":last.as_ref().map(|v|v.logical_at_ns.to_string()),
                "first_clock_policy_version":first.as_ref().map(|v|v.clock_policy_version.as_deref().unwrap_or("legacy-max-received-v1")),"last_clock_policy_version":last.as_ref().map(|v|v.clock_policy_version.as_deref().unwrap_or("legacy-max-received-v1")),
                "retention":"append_only_no_automatic_eviction","retention_policy":"append_only_no_automatic_eviction","origin_prefix_complete":meta.origin_prefix_complete,"origin_coverage":meta.legacy_origins.values().collect::<Vec<_>>(),"initial_state":"empty captured-prefix state; no legacy latest seed","gaps":[],"detail":null}))
        }).await
    }
    /// First sequence whose monotone max(received_at_ns) clock reaches the target. Same-clock ties choose the first sequence.
    pub async fn locate_time(&self,epoch:&str,received_at_ns:i64,through:u64)->Result<CapturePosition> {
        let epoch=epoch.to_owned();self.read(move|db|{
            let tx=db.begin_read()?;ensure!(metadata_in(&tx)?.epoch==epoch,"capture epoch differs");
            let table=tx.open_table(CLOCK)?;let key=clock_key(received_at_ns,0);
            for row in table.range(key.as_slice()..)?{let (_,v)=row?;if v.value()<=through{return Ok(get_record_in(&tx,v.value())?.position)}}
            bail!("receive timestamp is outside fixed replay input range")
        }).await
    }
    pub async fn checkpoint(&self,scope:&str,through:u64,body:Vec<u8>)->Result<()> {
        ensure!(!self.is_read_only(),"read-only capture rejects checkpoint writes; use isolated replay cache");
        ensure!(body.len()<=self.inner.options.queue_bytes,"replay checkpoint exceeds byte budget");
        let permit=self.inner.bytes.clone().acquire_many_owned(body.len().max(1).try_into()?).await?;
        let gate=self.inner.admission.lock().await;ensure!(self.inner.accepting.load(Ordering::Acquire),"capture is closing");
        let key=checkpoint_key(scope,through);let (reply,receive)=oneshot::channel();
        self.inner.sender.send(Command::Checkpoint{key,body,reply,_permit:permit}).await?;drop(gate);receive.await?
    }
    pub async fn nearest_checkpoint(&self,scope:&str,through:u64)->Result<Option<(u64,Vec<u8>)>> {
        let scope=scope.to_owned();self.read(move|db|{
            let tx=db.begin_read()?;let table=tx.open_table(CHECKPOINTS)?;
            let lo=checkpoint_key(&scope,0);let hi=checkpoint_key(&scope,through);let result=table.range(lo.as_slice()..=hi.as_slice())?.next_back();
            result.map(|r|r.map(|(k,v)|{let key=k.value();(u64::from_be_bytes(key[key.len()-8..].try_into().unwrap()),v.value().to_vec())})).transpose().map_err(Into::into)
        }).await
    }
    pub async fn close_and_drain(&self)->Result<()> {
        if let Some(writer)=&self.inner.readonly_health {self.inner.accepting.store(false,Ordering::Release);writer.send_replace(json!({"state":"closed","drained":true,"read_only":true}));return Ok(())}
        let gate=self.inner.admission.lock().await;
        if self.inner.accepting.swap(false,Ordering::AcqRel) {
            let (reply,receive)=oneshot::channel();self.inner.sender.send(Command::Close(reply)).await?;drop(gate);
            receive.await.context("capture drain actor ended")?
        }else{
            drop(gate);let mut health=self.inner.health.clone();loop {
                match health.borrow()["state"].as_str(){Some("closed")=>return Ok(()),Some("failed")=>bail!("capture failed during drain"),_=>{}}
                health.changed().await.context("capture drain ended without final status")?;
            }
        }
    }
    pub async fn close(&self)->Result<()> {self.close_and_drain().await}
}
fn clock_key(ns:i64,seq:u64)->Vec<u8> {let mut key=((ns as u64)^(1<<63)).to_be_bytes().to_vec();key.extend(seq.to_be_bytes());key}
fn checkpoint_key(scope:&str,seq:u64)->Vec<u8>{let mut key=Sha256::digest(scope.as_bytes()).to_vec();key.extend(seq.to_be_bytes());key}
fn read_metadata(db:&Database)->Result<Metadata>{metadata_in(&db.begin_read()?)}
fn metadata_in(tx:&redb::ReadTransaction)->Result<Metadata>{let table=tx.open_table(META)?;let value=table.get("metadata")?.context("capture metadata missing")?;Ok(serde_json::from_slice(value.value())?)}
fn get_record(db:&CaptureDatabase,sequence:u64)->Result<CapturedFrame>{get_record_in(&db.begin_read()?,sequence)}
fn get_record_in(tx:&redb::ReadTransaction,sequence:u64)->Result<CapturedFrame>{
    let table=tx.open_table(FRAMES)?;let value=table.get(sequence)?.context("capture frame is missing; evidence gap")?;decode_record(value.value(),sequence,&tx.open_table(BODIES)?)
}
fn content_digest(header:&RecordHeader,body:&[u8])->Result<String>{
    let mut value=header.clone();value.digest.clear();let mut hash=Sha256::new();hash.update(serde_json::to_vec(&value)?);if header.schema==1{hash.update(body)}Ok(hex::encode(hash.finalize()))
}
fn encode_record(header:&RecordHeader,body:&[u8])->Result<Vec<u8>>{
    let inline=header.schema==1;let header=serde_json::to_vec(header)?;let mut bytes=(header.len() as u32).to_be_bytes().to_vec();bytes.extend(header);if inline{bytes.extend(body)}Ok(bytes)
}
fn record_header(record:&[u8])->Result<(RecordHeader,usize)>{
    ensure!(record.len()>=4,"truncated capture header");let n=u32::from_be_bytes(record[..4].try_into()?) as usize;
    ensure!(n<=64*1024&&record.len()>=4+n,"invalid capture header length");
    let header:RecordHeader=serde_json::from_slice(&record[4..4+n])?;ensure!(matches!(header.schema,1|2),"unsupported capture record schema");Ok((header,4+n))
}
fn record_body_bytes(record:&[u8],bodies:&impl ReadableTable<&'static str,&'static [u8]>)->Result<usize>{
    let (header,offset)=record_header(record)?;
    if header.schema==1{Ok(record.len()-offset)}else{ensure!(record.len()==offset,"unexpected inline bytes in body reference");Ok(bodies.get(header.body_sha256.as_str())?.context("capture referenced body missing; evidence gap")?.value().len())}
}
fn decode_record(record:&[u8],sequence:u64,bodies:&impl ReadableTable<&'static str,&'static [u8]>)->Result<CapturedFrame>{
    let (header,offset)=record_header(record)?;ensure!(header.sequence==sequence,"capture sequence differs");
    let body=if header.schema==1{record[offset..].to_vec()}else{ensure!(record.len()==offset,"unexpected inline bytes in body reference");bodies.get(header.body_sha256.as_str())?.context("capture referenced body missing; evidence gap")?.value().to_vec()};
    ensure!(hex::encode(Sha256::digest(&body))==header.body_sha256&&content_digest(&header,&body)?==header.digest,"capture checksum differs");
    let frame=ProviderFrame{version:header.version,channel:header.channel,connection_id:header.connection_id,sequence:header.provider_sequence,
        received_at:header.received_at,encoding:header.encoding,body};frame.validate()?;
    Ok(CapturedFrame{position:CapturePosition{epoch:header.epoch,sequence,digest:header.digest},frame,accepted_at_ns:header.accepted_at_ns,legacy:header.legacy,logical_at_ns:header.logical_at_ns,clock_policy_version:header.clock_policy_version})
}

fn actor(db:Arc<Database>,path:PathBuf,options:CaptureOptions,mut receive:mpsc::Receiver<Command>,health:watch::Sender<Value>) {
    let mut pending=None;let mut failed=std::collections::BTreeSet::new();
    loop {
        let Some(command)=pending.take().or_else(||receive.blocking_recv()) else{break};
        match command {
            Command::Append(first)=>{
                let mut batch=vec![first];let mut bytes=batch[0].frame.body.len();
                while batch.len()<options.batch_frames&&bytes<options.batch_bytes {
                    match receive.try_recv(){Ok(Command::Append(next))=>{
                        if bytes+next.frame.body.len()>options.batch_bytes{pending=Some(Command::Append(next));break}
                        bytes+=next.frame.body.len();batch.push(next)
                    },Ok(other)=>{pending=Some(other);break},Err(_)=>break}
                }
                let result=commit_batch(&db,&path,&options,&batch);
                match result {
                    Ok(receipts)=>{for (p,receipt) in batch.into_iter().zip(receipts){failed.remove(&pending_identity(&p));let _=p.reply.send(Ok(receipt));}
                        health.send_replace(json!({"state":if failed.is_empty(){"ready"}else{"degraded"},"unresolved_frames":failed.len(),"durability":"redb_immediate"}));},
                    Err(error)=>{let detail=format!("{error:#}");for p in &batch{failed.insert(pending_identity(p));}
                        health.send_replace(json!({"state":"degraded","detail":detail,"unresolved_frames":failed.len(),"durability":"no_receipt_for_failed_batch"}));
                        for p in batch{let _=p.reply.send(Err(anyhow::anyhow!(detail.clone())));}}
                }
            },
            Command::Checkpoint{key,body,reply,_permit}=>{let result=(||->Result<()> {
                check_space(&path,options.min_free_bytes,body.len() as u64)?;
                let mut tx=db.begin_write()?;tx.set_durability(Durability::Immediate)?;
                {let mut table=tx.open_table(CHECKPOINTS)?;let existing=table.get(key.as_slice())?.map(|v|v.value().to_vec());
                 if let Some(existing)=existing {ensure!(existing==body,"immutable replay checkpoint conflicts")}else{table.insert(key.as_slice(),body.as_slice())?;}}
                tx.commit()?;Ok(())
            })();let _=reply.send(result);},
            Command::Close(reply)=>{if failed.is_empty(){health.send_replace(json!({"state":"closed","drained":true}));let _=reply.send(Ok(()));}
                else{health.send_replace(json!({"state":"failed","drained":false,"unresolved_frames":failed.len()}));let _=reply.send(Err(anyhow::anyhow!("capture has {} unresolved failed frames; no clean drain",failed.len())));}return},
        }
    }
    health.send_replace(json!({"state":if failed.is_empty(){"closed"}else{"failed"},"drained":failed.is_empty(),"unresolved_frames":failed.len()}));
}
fn pending_identity(p:&Pending)->String {
    let provider=format!("{}:{}:{}",p.frame.channel,p.frame.connection_id,p.frame.sequence);
    match &p.legacy {Some(origin)=>format!("legacy:{}:{}:{}:{provider}",origin.stream,origin.epoch,origin.sequence),None=>provider}
}
fn commit_batch(db:&Database,path:&Path,options:&CaptureOptions,batch:&[Pending])->Result<Vec<DurableReceipt>> {
    check_space(path,options.min_free_bytes,batch.iter().map(|p|p.frame.body.len() as u64).sum())?;
    let mut tx=db.begin_write()?;tx.set_durability(Durability::Immediate)?;
    let mut receipts=vec![];
    {let mut meta_table=tx.open_table(META)?;let raw=meta_table.get("metadata")?.context("capture metadata missing")?.value().to_vec();
     let mut meta:Metadata=serde_json::from_slice(&raw)?;let mut frames=tx.open_table(FRAMES)?;let mut bodies=tx.open_table(BODIES)?;let mut ids=tx.open_table(IDENTITIES)?;let mut clock=tx.open_table(CLOCK)?;
     for p in batch {
        let identity=pending_identity(p);
        if let Some(seq)=ids.get(identity.as_str())?.map(|v|v.value()) {
            let existing=decode_record(frames.get(seq)?.context("capture identity points to missing evidence")?.value(),seq,&bodies)?;
            ensure!(existing.frame==p.frame,"conflicting payload for the same provider frame identity");
            if p.legacy.is_some(){ensure!(existing.legacy==p.legacy,"legacy identity metadata differs")}
            receipts.push(DurableReceipt{position:existing.position,received_at_ns:p.frame.received_at.timestamp_nanos_opt().unwrap(),accepted_at_ns:existing.accepted_at_ns,confirmed_at_ns:0,duplicate:true});continue
        }
        let sequence=meta.last.checked_add(1).context("capture global u64 sequence exhausted")?;
        let prior_digest=meta.last_digest.clone();
        let accepted_at_ns=Utc::now().timestamp_nanos_opt().context("capture clock exceeds nanosecond contract")?;
        let received_ns=p.frame.received_at.timestamp_nanos_opt().unwrap();
        let logical_at_ns=meta.last_logical_ns.max(received_ns).max(if p.legacy.is_none(){accepted_at_ns}else{received_ns});
        let mut header=RecordHeader{schema:if options.content_addressed_bodies{2}else{1},epoch:meta.epoch.clone(),sequence,prior_digest,digest:String::new(),logical_at_ns,version:p.frame.version,
            channel:p.frame.channel.clone(),connection_id:p.frame.connection_id.clone(),provider_sequence:p.frame.sequence,
            received_at:p.frame.received_at,encoding:p.frame.encoding.clone(),accepted_at_ns,body_sha256:hex::encode(Sha256::digest(&p.frame.body)),legacy:p.legacy.clone(),clock_policy_version:if p.legacy.is_none(){Some("native-max-received-accepted-v1".into())}else{None}};
        header.digest=content_digest(&header,&p.frame.body)?;
        if options.content_addressed_bodies {
            let existing=bodies.get(header.body_sha256.as_str())?.map(|v|v.value()==p.frame.body.as_slice());
            if let Some(matches)=existing {ensure!(matches,"body hash collision or corrupt content-addressed payload");}
            else {bodies.insert(header.body_sha256.as_str(),p.frame.body.as_slice())?;meta.unique_body_bytes=meta.unique_body_bytes.checked_add(p.frame.body.len() as u64).context("capture body bytes overflow")?;meta.unique_bodies+=1;meta.bytes=meta.bytes.checked_add(p.frame.body.len() as u64).context("capture body bytes overflow")?;}
        }
        meta.logical_body_bytes=meta.logical_body_bytes.checked_add(p.frame.body.len() as u64).context("capture logical body bytes overflow")?;meta.schema=2;
        let encoded=encode_record(&header,&p.frame.body)?;
        frames.insert(sequence,encoded.as_slice())?;ids.insert(identity.as_str(),sequence)?;
        if p.legacy.is_some(){let provider_identity=format!("{}:{}:{}",p.frame.channel,p.frame.connection_id,p.frame.sequence);
            if ids.get(provider_identity.as_str())?.is_none(){ids.insert(provider_identity.as_str(),sequence)?;}}

        let clock_key=clock_key(logical_at_ns,sequence);clock.insert(clock_key.as_slice(),sequence)?;
        if let Some(origin)=&p.legacy {
            let legacy_sequence=origin.sequence.parse::<u64>()?;
            if let Some(coverage)=meta.legacy_origins.get_mut(&origin.epoch) {
                ensure!(legacy_sequence==coverage.last_sequence.checked_add(1).context("legacy sequence exhausted")?,"legacy capture range has a gap or backward append");
                coverage.last_sequence=legacy_sequence;
            }else{
                if legacy_sequence>1{meta.origin_prefix_complete=false}
                meta.legacy_origins.insert(origin.epoch.clone(),LegacyCoverage{stream:origin.stream.clone(),epoch:origin.epoch.clone(),first_sequence:legacy_sequence,last_sequence:legacy_sequence,
                    first_native_sequence:sequence,missing_prefix:legacy_sequence>1,initial_state:"empty_retained_prefix_no_pg_latest".into()});
            }
        }
        if meta.first==0 {meta.first=sequence}
        meta.last=sequence;meta.last_digest=header.digest.clone();meta.last_logical_ns=logical_at_ns;meta.bytes=meta.bytes.checked_add(encoded.len() as u64).context("capture byte counter overflow")?;
        receipts.push(DurableReceipt{position:CapturePosition{epoch:meta.epoch.clone(),sequence,digest:header.digest},
            received_at_ns:p.frame.received_at.timestamp_nanos_opt().unwrap(),accepted_at_ns,confirmed_at_ns:0,duplicate:false});
     }
     let bytes=serde_json::to_vec(&meta)?;meta_table.insert("metadata",bytes.as_slice())?;}
    tx.commit()?;let confirmed=Utc::now().timestamp_nanos_opt().context("capture acknowledgment clock exceeds nanosecond contract")?;
    for receipt in &mut receipts {receipt.confirmed_at_ns=confirmed}
    Ok(receipts)
}
fn sync_directory(path:&Path)->Result<()> {
    #[cfg(unix)] {std::fs::File::open(path)?.sync_all()?;}
    Ok(())
}
pub fn available_bytes(path:&Path)->Result<u64> {
    #[cfg(unix)] {
        use std::os::unix::ffi::OsStrExt;
        let path=std::ffi::CString::new(path.as_os_str().as_bytes())?;
        let mut stat=std::mem::MaybeUninit::<libc::statvfs>::uninit();
        ensure!(unsafe{libc::statvfs(path.as_ptr(),stat.as_mut_ptr())}==0,"cannot inspect capture disk capacity");
        let stat=unsafe{stat.assume_init()};return Ok((stat.f_bavail as u64).saturating_mul(stat.f_frsize as u64));
    }
    #[cfg(windows)] {
        use std::os::windows::ffi::OsStrExt;
        #[link(name="kernel32")] unsafe extern "system" {
            fn GetDiskFreeSpaceExW(directory:*const u16,available:*mut u64,total:*mut u64,free:*mut u64)->i32;
        }
        let directory=if path.is_file(){path.parent().context("capture directory missing")?}else{path};
        let mut wide=directory.as_os_str().encode_wide().collect::<Vec<_>>();ensure!(!wide.contains(&0),"directory contains NUL");wide.push(0);
        let mut available=0u64;ensure!(unsafe{GetDiskFreeSpaceExW(wide.as_ptr(),&mut available,std::ptr::null_mut(),std::ptr::null_mut())}!=0,
            "cannot inspect capture disk capacity: {}",std::io::Error::last_os_error());return Ok(available);
    }
    #[cfg(not(any(unix,windows)))] bail!("capture disk guard is unsupported on this platform")
}
fn check_space(path:&Path,reserve:u64,payload:u64)->Result<()> {
    let available=available_bytes(path)?;
    ensure!(available>=reserve.saturating_add(payload.saturating_mul(2)),"capture disk low: {available} available bytes; preserve evidence and free/archive space before retry");Ok(())
}
