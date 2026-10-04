//! Native canonical facts. Imports are isolated generations; capture and projection are separate durability boundaries.
use crate::{native_codec::{StoredBar,StoredQuote,component,identity,prefix_end,signed_key},range_index::{self,DirtyLeaves,FACTS,RangeAggregate,scope,fact_key},persistence_contract::*};
use anyhow::{Context,Result,bail,ensure};
use redb::{Database,ReadOnlyDatabase,Durability,ReadableDatabase,ReadableTable,TableDefinition,WriteTransaction,ReadTransaction};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{path::Path,sync::{Arc,Mutex,atomic::{AtomicBool,Ordering}}};
use tokio::sync::Semaphore;

const GLOBAL:TableDefinition<&str,&str>=TableDefinition::new("store_global_v1");
const VERSIONS:TableDefinition<&str,&[u8]>=TableDefinition::new("generation_versions_v1");
const METADATA:TableDefinition<&[u8],&[u8]>=TableDefinition::new("metadata_v1");
const QUOTES:TableDefinition<&[u8],&[u8]>=TableDefinition::new("latest_quotes_v1");
const EVENTS:TableDefinition<&[u8],&[u8]>=TableDefinition::new("quote_events_v1");
const EVENT_IDENTITIES:TableDefinition<&[u8],u64>=TableDefinition::new("quote_event_identities_v1");
const IMPORTS:TableDefinition<&[u8],&[u8]>=TableDefinition::new("import_batches_v1");
const ERRORS:TableDefinition<&[u8],&[u8]>=TableDefinition::new("projection_errors_v1");
const EXTERNAL:TableDefinition<&[u8],&[u8]>=TableDefinition::new("external_facts_v1");
const EXTERNAL_IDENTITIES:TableDefinition<&[u8],&[u8]>=TableDefinition::new("external_fact_identities_v1");
const READERS:u32=4;
const MAX_PAGE_ROWS:usize=10_001; // One sentinel row for an API page capped at 10,000.
const MAX_BATCH_ROWS:usize=100_000;
#[path="native_store_verified_state_patch.rs"] mod verified_state_patch;

#[derive(Clone)]enum DatabaseHandle {Writable(Arc<Database>),ReadOnly(Arc<ReadOnlyDatabase>)}
impl DatabaseHandle {fn readable(&self)->&(dyn ReadableDatabase+Send+Sync){match self {Self::Writable(db)=>db.as_ref(),Self::ReadOnly(db)=>db.as_ref()}}fn writable(&self)->Result<&Database>{match self {Self::Writable(db)=>Ok(db.as_ref()),Self::ReadOnly(_)=>bail!("canonical store is read-only")}}}
struct Inner {db:Mutex<Option<DatabaseHandle>>,writer:Arc<Semaphore>,readers:Arc<Semaphore>,closed:AtomicBool,read_only:bool,replay:bool,path:std::path::PathBuf}
#[derive(Clone)]
pub struct Store {inner:Arc<Inner>,generation:Option<String>}
/// A small resumed input is copied while its read view is open. Indicator work
/// starts only after this value has been returned and that transaction is gone.
#[derive(Debug)]
pub struct MaterializedScan {pub batches:Vec<CanonicalScanBatch>,pub summary:CanonicalScanSummary}
#[derive(Debug)]
pub struct MaterializedTailTooLarge;
impl std::fmt::Display for MaterializedTailTooLarge {fn fmt(&self,f:&mut std::fmt::Formatter<'_>)->std::fmt::Result {f.write_str("materialize_tail_too_large")}}
impl std::error::Error for MaterializedTailTooLarge {}
#[derive(Default)]
struct Counts {accepted:u64,unchanged:u64,rejected:u64}
struct BoundedJsonSize {bytes:usize,max:usize}
impl std::io::Write for BoundedJsonSize {
    fn write(&mut self,bytes:&[u8])->std::io::Result<usize> {
        let next=self.bytes.checked_add(bytes.len()).filter(|v|*v<=self.max).ok_or_else(||std::io::Error::other("decoded replay projection exceeds bounded bytes"))?;
        self.bytes=next;Ok(bytes.len())
    }
    fn flush(&mut self)->std::io::Result<()> {Ok(())}
}

fn initial_version(epoch:&str,generation:&str)->SnapshotVersion {
    SnapshotVersion {store_epoch:epoch.into(),commit_id:0,committed_capture:None,schema_version:SCHEMA_VERSION.into(),aggregation_version:AGGREGATION_VERSION.into(),
        projector_version:PROJECTOR_VERSION.into(),catalog_version:"unconfigured".into(),schedule_version:"unconfigured".into(),route_version:"unconfigured".into(),active_generation:generation.into()}
}
fn checked_version(bytes:&[u8])->Result<SnapshotVersion> {
    let v:SnapshotVersion=serde_json::from_slice(bytes)?;
    ensure!(v.schema_version==SCHEMA_VERSION && matches!(v.aggregation_version.as_str(),"tracefang-range-index-v2"|"tracefang-range-index-v3-canonical-coefficient"|"tracefang-range-index-v4-known-clock-coverage") || v.schema_version==SCHEMA_VERSION && v.aggregation_version==AGGREGATION_VERSION,"storage/index version mismatch; rebuild into an inactive generation");Ok(v)
}
fn require_current_index(version:&SnapshotVersion)->Result<()> {ensure!(version.aggregation_version==AGGREGATION_VERSION,"outdated range index; use an inactive index-only rebuild before derived reads");Ok(())}
fn expected(actual:&SnapshotVersion,wanted:&Option<SnapshotVersion>)->Result<()> {
    if let Some(wanted)=wanted {ensure!(serde_json::to_vec(actual)?==serde_json::to_vec(wanted)?,"requested MVCC snapshot is no longer the current read view");}Ok(())
}
fn read_version(tx:&ReadTransaction,generation:Option<&str>)->Result<SnapshotVersion> {
    let global=tx.open_table(GLOBAL)?;let name=match generation {Some(v)=>v.to_owned(),None=>global.get("active")?.context("missing active generation")?.value().to_owned()};
    checked_version(tx.open_table(VERSIONS)?.get(name.as_str())?.context("unknown generation")?.value())
}
fn write_version(tx:&WriteTransaction,generation:Option<&str>)->Result<SnapshotVersion> {
    let global=tx.open_table(GLOBAL)?;let name=match generation {Some(v)=>v.to_owned(),None=>global.get("active")?.context("missing active generation")?.value().to_owned()};
    checked_version(tx.open_table(VERSIONS)?.get(name.as_str())?.context("unknown generation")?.value())
}
fn advance(tx:&WriteTransaction,v:&mut SnapshotVersion)->Result<()> {
    let mut global=tx.open_table(GLOBAL)?;let sequence=global.get("commit_id")?.context("missing commit counter")?.value().parse::<u64>()?;
    v.commit_id=sequence.checked_add(1).context("store commit counter exhausted")?;global.insert("commit_id",v.commit_id.to_string().as_str())?;Ok(())
}
fn save_version(tx:&WriteTransaction,v:&SnapshotVersion)->Result<()> {let bytes=serde_json::to_vec(v)?;tx.open_table(VERSIONS)?.insert(v.active_generation.as_str(),bytes.as_slice())?;Ok(())}
fn named_key(generation:&str,namespace:&str,key:&str)->Vec<u8> {let mut out=Vec::new();for v in [generation,namespace,key] {component(&mut out,v);}out}
fn quote_scope(generation:&str,source:&str,symbol:&str)->Vec<u8> {scope(generation,source,symbol,0)}
fn quote_key(generation:&str,row:&ImportQuoteRow)->Vec<u8> {quote_scope(generation,&row.realtime_source_id,&row.instrument_symbol)}
fn event_key(generation:&str,row:&ImportQuoteRow,ordinal:u64)->Vec<u8> {let mut out=quote_key(generation,row);out.extend(ordinal.to_be_bytes());out}
fn event_identity(generation:&str,row:&ImportQuoteRow)->Vec<u8> {let mut out=quote_key(generation,row);component(&mut out,&row.event_id);out}

impl Store {
    /// Persistent callers supply Application Support/user-data paths; no implicit repository/cache DB.
    pub fn open(path:impl AsRef<Path>)->Result<Self> {Self::open_mode(path,false)}
    /// Only a new or explicitly marked regenerable replay file may use this mode.
    pub fn open_replay(path:impl AsRef<Path>)->Result<Self> {Self::open_mode(path,true)}
    fn open_mode(path:impl AsRef<Path>,replay:bool)->Result<Self> {
        let path=path.as_ref();if let Some(parent)=path.parent().filter(|v|!v.as_os_str().is_empty()) {std::fs::create_dir_all(parent)?;}
        if path.exists() {
            let probe=ReadOnlyDatabase::open(path).context("inspect existing Store role before opening writer")?;
            let tx=probe.begin_read()?;let global=tx.open_table(GLOBAL)?;
            let existing_replay=global.get("store_kind")?.is_some_and(|v|v.value()=="replay_derived_v1");
            ensure!(existing_replay==replay,"live and replay Store roles cannot be interchanged");
        }
        let db=Database::create(path).context("open native canonical store")?;let mut tx=db.begin_write()?;tx.set_durability(Durability::Immediate)?;
        range_index::initialize(&tx)?;
        for definition in [METADATA,QUOTES,EVENTS,IMPORTS,ERRORS,EXTERNAL,EXTERNAL_IDENTITIES] {tx.open_table(definition)?;}
        tx.open_table(EVENT_IDENTITIES)?;
        {
            let mut global=tx.open_table(GLOBAL)?;let mut versions=tx.open_table(VERSIONS)?;
            if global.get("epoch")?.is_none() {
                let epoch=uuid::Uuid::new_v4().to_string();global.insert("epoch",epoch.as_str())?;global.insert("active","live-v1")?;global.insert("commit_id","0")?;
                let bytes=serde_json::to_vec(&initial_version(&epoch,"live-v1"))?;versions.insert("live-v1",bytes.as_slice())?;
            }
            let active=global.get("active")?.context("store active generation")?.value().to_owned();checked_version(versions.get(active.as_str())?.context("active version")?.value())?;
            if global.get("event_ordinal")?.is_none() {global.insert("event_ordinal","0")?;}
            if replay {global.insert("store_kind","replay_derived_v1")?;}
        }
        tx.commit()?;
        Ok(Self {inner:Arc::new(Inner {db:Mutex::new(Some(DatabaseHandle::Writable(Arc::new(db)))),writer:Arc::new(Semaphore::new(1)),readers:Arc::new(Semaphore::new(READERS as usize)),closed:AtomicBool::new(false),read_only:false,replay,path:path.to_owned()}),generation:None})
    }
    pub fn open_read_only(path:impl AsRef<Path>)->Result<Self> {
        let path=path.as_ref();let db=ReadOnlyDatabase::open(path).context("open existing canonical store for read-only shadow")?;let tx=db.begin_read()?;read_version(&tx,None)?;let replay=tx.open_table(GLOBAL)?.get("store_kind")?.is_some_and(|v|v.value()=="replay_derived_v1");drop(tx);
        Ok(Self {inner:Arc::new(Inner {db:Mutex::new(Some(DatabaseHandle::ReadOnly(Arc::new(db)))),writer:Arc::new(Semaphore::new(1)),readers:Arc::new(Semaphore::new(READERS as usize)),closed:AtomicBool::new(false),read_only:true,replay,path:path.to_owned()}),generation:None})
    }
    pub fn read_only(&self)->bool {self.inner.read_only}
    pub fn file_path(&self)->&Path {&self.inner.path}
    /// Use a clean Immediate transaction: opening normal data tables before
    /// persistent_savepoint would make redb reject that transaction as dirty.
    pub async fn create_replay_savepoint(&self)->Result<ReplaySavepoint> {
        ensure!(self.inner.replay && !self.inner.read_only && self.generation.is_none(),"savepoints require an owned writable replay Store");
        let permit=self.inner.writer.clone().acquire_owned().await?;let db=self.database()?;
        tokio::task::spawn_blocking(move|| {
            let _permit=permit;let database=db.writable()?;
            let version={let read=database.begin_read()?;read_version(&read,None)?};
            ensure!(version.committed_capture.is_some(),"replay checkpoint requires a committed capture prefix");
            let mut tx=database.begin_write()?;tx.set_durability(Durability::Immediate)?;
            ensure!(tx.list_persistent_savepoints()?.count()<4,"replay savepoint budget exhausted; release an older checkpoint");
            let savepoint_id=tx.persistent_savepoint()?;tx.commit()?;
            Ok(ReplaySavepoint {savepoint_id,version})
        }).await?
    }
    pub async fn restore_replay_savepoint(&self,id:u64,wanted:SnapshotVersion)->Result<ReplayRestoreReceipt> {
        ensure!(self.inner.replay && !self.inner.read_only && self.generation.is_none(),"savepoints require an owned writable replay Store");
        let permit=self.inner.writer.clone().acquire_owned().await?;let db=self.database()?;
        tokio::task::spawn_blocking(move|| {
            let _permit=permit;let database=db.writable()?;let mut tx=database.begin_write()?;tx.set_durability(Durability::Immediate)?;
            let invalidated_later_ids=tx.list_persistent_savepoints()?.filter(|other|*other>id).collect::<Vec<_>>();
            let savepoint=tx.get_persistent_savepoint(id)?;tx.restore_savepoint(&savepoint)?;
            let version=write_version(&tx,None)?;
            ensure!(version==wanted && version.committed_capture.is_some(),"replay checkpoint version/capture binding differs; restore aborted");
            tx.commit()?;Ok(ReplayRestoreReceipt {version,invalidated_later_ids})
        }).await?
    }
    pub async fn delete_replay_savepoints(&self,ids:Vec<u64>)->Result<Vec<u64>> {
        ensure!(self.inner.replay && !self.inner.read_only && self.generation.is_none(),"savepoints require an owned writable replay Store");
        ensure!(ids.len()<=4,"replay savepoint deletion exceeds budget");
        let permit=self.inner.writer.clone().acquire_owned().await?;let db=self.database()?;
        tokio::task::spawn_blocking(move|| {
            let _permit=permit;let mut tx=db.writable()?.begin_write()?;tx.set_durability(Durability::Immediate)?;let mut deleted=Vec::new();
            for id in ids {if tx.delete_persistent_savepoint(id)?{deleted.push(id);}}tx.commit()?;Ok(deleted)
        }).await?
    }
    fn database(&self)->Result<DatabaseHandle> {ensure!(!self.inner.closed.load(Ordering::Acquire),"store is closed");self.inner.db.lock().map_err(|_|anyhow::anyhow!("store handle poisoned"))?.as_ref().cloned().context("store closed")}
    async fn read<F,T>(&self,operation:F)->Result<T> where F:FnOnce(&(dyn ReadableDatabase+Send+Sync),Option<&str>)->Result<T>+Send+'static,T:Send+'static {
        let permit=self.inner.readers.clone().acquire_owned().await?;let db=self.database()?;let generation=self.generation.clone();
        tokio::task::spawn_blocking(move|| {let _permit=permit;operation(db.readable(),generation.as_deref())}).await?
    }
    async fn write<F,T>(&self,operation:F)->Result<T> where F:FnOnce(&WriteTransaction,&mut SnapshotVersion)->Result<T>+Send+'static,T:Send+'static {
        ensure!(!self.inner.read_only,"canonical store is a read-only shadow");
        let permit=self.inner.writer.clone().acquire_owned().await?;let db=self.database()?;let generation=self.generation.clone();
        tokio::task::spawn_blocking(move|| {let _permit=permit;let mut tx=db.writable()?.begin_write()?;tx.set_durability(Durability::Immediate)?;
            let mut version=write_version(&tx,generation.as_deref())?;let result=operation(&tx,&mut version)?;save_version(&tx,&version)?;tx.commit()?;Ok(result)}).await?
    }
    pub async fn version(&self)->Result<SnapshotVersion> {self.read(|db,generation|read_version(&db.begin_read()?,generation)).await}
    /// Complete source/interval inventory with no full-history JSON allocation.
    pub async fn series_inventory(&self)->Result<Value> {
        self.read(|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let table=tx.open_table(FACTS)?;
            let mut cursor=Vec::new();component(&mut cursor,&version.active_generation);let end=prefix_end(&cursor)?;let mut items=Vec::new();
            while let Some((_,bytes))=table.range(cursor.as_slice()..end.as_slice())?.next().transpose()? {
                let first=StoredBar::decode(bytes.value())?;let row=&first.row;let prefix=scope(&version.active_generation,&row.realtime_source_id,&row.instrument_symbol,row.interval_seconds);let last_key=prefix_end(&prefix)?;
                let last=StoredBar::decode(table.range(prefix.as_slice()..last_key.as_slice())?.next_back().transpose()?.context("series inventory last row")?.1.value())?;
                let count=if row.interval_seconds==60 {range_index::query(&tx,&version.active_generation,&row.realtime_source_id,&row.instrument_symbol,row.open_time_ns,last.row.close_time_ns,false)?.context("series inventory range summary missing")?.total_count}
                    else{let mut count=0u64;for record in table.range(prefix.as_slice()..last_key.as_slice())? {record?;count=count.checked_add(1).context("inventory row count exhausted")?;}count};
                items.push(json!({"symbol":row.instrument_symbol,"source_id":row.realtime_source_id,"interval_seconds":row.interval_seconds,"row_count":count.to_string(),"first_open_time_ns":row.open_time_ns.to_string(),"last_open_time_ns":last.row.open_time_ns.to_string()}));cursor=last_key;
            }Ok(json!({"version":version,"series":items,"complete":true}))
        }).await
    }
    pub async fn healthy(&self)->bool {self.version().await.is_ok()}
    pub async fn timeline(&self,symbol:&str,sources:&[String],before:Option<u64>,limit:usize)->Result<Vec<Value>> {
        ensure!(limit>0 && limit<=20_001 && sources.len()<=64 && sources.len().checked_mul(limit).is_some_and(|v|v<=20_001),"timeline exceeds bounded page dimensions");let symbol=symbol.to_owned();let sources=sources.to_vec();
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let events=tx.open_table(EVENTS)?;let mut rows=Vec::new();
            for source in sources {
                let prefix=quote_scope(&version.active_generation,&source,&symbol);let end=if let Some(before)=before {let mut key=prefix.clone();key.extend(before.to_be_bytes());key}else{prefix_end(&prefix)?};
                for record in events.range(prefix.as_slice()..end.as_slice())?.rev().take(limit) {
                    let (key,bytes)=record?;let fact=StoredQuote::decode(bytes.value())?;let ordinal=u64::from_be_bytes(key.value()[key.value().len()-8..].try_into()?);
                    let mut value=quote_value(&fact)?;value["storage_id"]=json!(ordinal.to_string());value["application_order"]=json!(ordinal.to_string());
                    value["timeline_semantics"]=json!(if fact.capture.is_some(){"durable_capture_application_order"}else{"legacy_import_order"});rows.push((ordinal,value));
                }
            }
            rows.sort_by_key(|r|std::cmp::Reverse(r.0));rows.truncate(limit);Ok(rows.into_iter().map(|r|r.1).collect())
        }).await
    }
    pub async fn latest_quotes(&self)->Result<Vec<Value>> {
        self.read(|db,generation| {let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let mut prefix=Vec::new();component(&mut prefix,&version.active_generation);let end=prefix_end(&prefix)?;
            tx.open_table(QUOTES)?.range(prefix.as_slice()..end.as_slice())?.map(|record| {let (_,bytes)=record?;quote_value(&StoredQuote::decode(bytes.value())?)}).collect()
        }).await
    }
    pub async fn staging(&self,generation:&str)->Result<Self> {
        ensure!(!self.inner.replay,"replay Store cannot create/import legacy generations");
        identity(generation)?;let name=generation.to_owned();let target=name.clone();
        self.write(move|tx,current| {
            ensure!(name!=current.active_generation,"import must use an inactive generation");
            let mut versions=tx.open_table(VERSIONS)?;
            if let Some(bytes)=versions.get(name.as_str())? {let staged=checked_version(bytes.value())?;ensure!(staged.committed_capture.is_none(),"capture generation cannot be reused as an import staging area");}
            else {let bytes=serde_json::to_vec(&initial_version(&current.store_epoch,&name))?;versions.insert(name.as_str(),bytes.as_slice())?;}
            Ok(())
        }).await?;
        Ok(Self {inner:self.inner.clone(),generation:Some(target)})
    }
    /// Select an existing generation without initializing tables or advancing versions.
    pub async fn read_generation(&self,generation:&str)->Result<Self> {
        identity(generation)?;let name=generation.to_owned();let target=name.clone();
        self.read(move|db,_| {read_version(&db.begin_read()?,Some(&name))?;Ok(())}).await?;
        Ok(Self {inner:self.inner.clone(),generation:Some(target)})
    }
    pub async fn verify_index(&self)->Result<Value> {
        self.read(|db,generation| {let tx=db.begin_read()?;let version=read_version(&tx,generation)?;range_index::verify(&tx,&version.active_generation)}).await
    }
    pub async fn index_space_usage(&self)->Result<Value> {
        self.read(|db,generation|{let tx=db.begin_read()?;let version=read_version(&tx,generation)?;range_index::space_usage(&tx,&version.active_generation)}).await
    }
    /// Atomic offline repair. An active/capture generation cannot use this API.
    pub async fn rebuild_staging_index(&self)->Result<Value> {
        ensure!(self.generation.is_some(),"rebuild only an explicitly inactive staging generation");
        self.write(|tx,version| {
            ensure!(version.committed_capture.is_none(),"capture generation cannot be rebuilt through the legacy repair API");
            ensure!(tx.open_table(GLOBAL)?.get("active")?.context("active generation missing")?.value()!=version.active_generation,"cannot rebuild the active generation");
            let proof=range_index::rebuild(tx,&version.active_generation)?;
            version.aggregation_version=AGGREGATION_VERSION.into();advance(tx,version)?;
            let key=named_key(&version.active_generation,"migration","index_verification");tx.open_table(METADATA)?.remove(key.as_slice())?;
            Ok(proof)
        }).await
    }
    /// Explicit offline maintenance for the synthetic integration fixture only.
    /// The CLI holds the capture writer lock and verifies its exact durable tail.
    /// This is never a production recovery or HTTP operation.
    pub async fn rebuild_offline_fixture_index(&self,expected_capture:CapturePosition)->Result<Value> {
        ensure!(!self.inner.replay && self.generation.is_none(),"fixture repair requires the active fixture handle");
        ensure!(self.inner.path.components().any(|v|v.as_os_str()=="TraceFang-validation"),"fixture repair refuses paths outside the explicit validation namespace");
        self.write(move|tx,version| {
            ensure!(version.active_generation=="fixed-quant-fixture-v1","fixture generation differs");
            ensure!(version.committed_capture.as_ref()==Some(&expected_capture),"fixture capture anchor differs");
            ensure!(expected_capture.epoch=="c6e48300-88b2-4adc-8a8b-dc7b78bb0e32" && expected_capture.sequence==4 && expected_capture.digest=="fd001743ee72d60bb104c2d5a86381dc9fa0dd0b0e96219283e8ba9f500a6d01","fixture anchor is outside the reviewed maintenance proof");
            let count_key=named_key(&version.active_generation,"fixture","count");
            let count=tx.open_table(METADATA)?.get(count_key.as_slice())?.map(|v|serde_json::from_slice::<Value>(v.value())).transpose()?.context("verified fixture metadata missing")?;
            ensure!(count.as_u64().is_some_and(|v|v>0 && v<=1_000_000),"fixture count metadata invalid");
            let manifest_key=named_key(&version.active_generation,"migration","verified_manifest");
            let manifest=tx.open_table(METADATA)?.get(manifest_key.as_slice())?.map(|v|serde_json::from_slice::<Value>(v.value())).transpose()?.context("fixture activation verification missing")?;
            ensure!(manifest["complete"]==true && manifest["index_verified"]==true && manifest["generation"]==version.active_generation && manifest["store_epoch"]==version.store_epoch,"fixture verification identity differs");
            let boundary_key=named_key(&version.active_generation,"migration","projection_start_boundary");
            ensure!(tx.open_table(METADATA)?.get(boundary_key.as_slice())?.is_none(),"fixture repair refuses a legacy authority boundary");
            let cp_key=named_key(&version.active_generation,"runtime","decoder_checkpoint");
            let cp=tx.open_table(METADATA)?.get(cp_key.as_slice())?.map(|v|serde_json::from_slice::<Value>(v.value())).transpose()?.context("fixture decoder checkpoint missing")?;
            let upgraded=fixture_decoder_upgrade(cp,&expected_capture)?;
            let mut prefix=Vec::new();component(&mut prefix,&version.active_generation);let end=prefix_end(&prefix)?;let facts=tx.open_table(FACTS)?;
            for entry in facts.range(prefix.as_slice()..end.as_slice())? {let(_,bytes)=entry?;let row=StoredBar::decode(bytes.value())?.row;
                ensure!(row.realtime_source_id=="jin10_client" && matches!(row.instrument_symbol.as_str(),"XAU/USD"|"XAG/USD"|"USD/CNH") && matches!(row.interval_seconds,1|60),"fixture contains a scope requiring actual clock-policy reprojection");
            }
            drop(facts);
            for table in [QUOTES,EVENTS] {for entry in tx.open_table(table)?.range(prefix.as_slice()..end.as_slice())? {let(_,bytes)=entry?;let row=StoredQuote::decode(bytes.value())?.row;
                ensure!(row.realtime_source_id=="jin10_client" && matches!(row.instrument_symbol.as_str(),"XAU/USD"|"XAG/USD"|"USD/CNH"),"fixture quote scope requires actual reprojection");
            }}
            let old_projector=version.projector_version.clone();let mut proof=range_index::rebuild(tx,&version.active_generation)?;
            version.aggregation_version=AGGREGATION_VERSION.into();version.projector_version=PROJECTOR_VERSION.into();advance(tx,version)?;
            let bytes=serde_json::to_vec(&upgraded)?;tx.open_table(METADATA)?.insert(cp_key.as_slice(),bytes.as_slice())?;
            proof["projector_version_before"]=json!(old_projector);proof["projector_version_after"]=json!(version.projector_version);proof["decoder_checkpoint_upgrade"]=json!("verified empty provider caches; no source-clock/calendar scope applies; no raw-prefix seed assertion");
            let maintenance_key=named_key(&version.active_generation,"fixture","offline_policy_maintenance");let bytes=serde_json::to_vec(&json!({"capture_position":expected_capture,"proof":proof,"fixture_only":true}))?;tx.open_table(METADATA)?.insert(maintenance_key.as_slice(),bytes.as_slice())?;
            let key=named_key(&version.active_generation,"migration","index_verification");tx.open_table(METADATA)?.remove(key.as_slice())?;
            ensure!(version.committed_capture.as_ref()==Some(&expected_capture),"fixture maintenance changed capture identity");
            Ok(proof)
        }).await
    }
    /// Activation requires a verified manifest and is intentionally forbidden over a live capture cursor.
    pub async fn activate_staging(&self,generation:&str,verified_manifest:Value)->Result<SnapshotVersion> {self.activate_staging_boundary(generation,verified_manifest,None).await}
    pub async fn activate_staging_with_boundary(&self,generation:&str,verified_manifest:Value,boundary:ProjectionStartBoundary)->Result<SnapshotVersion> {self.activate_staging_boundary(generation,verified_manifest,Some(boundary)).await}
    pub async fn projection_start_boundary(&self)->Result<Option<ProjectionStartBoundary>> {self.metadata("migration","projection_start_boundary").await?.map(serde_json::from_value).transpose().map_err(Into::into)}
    async fn activate_staging_boundary(&self,generation:&str,verified_manifest:Value,boundary:Option<ProjectionStartBoundary>)->Result<SnapshotVersion> {
        ensure!(!self.inner.replay,"replay Store cannot install authority handoffs or activate imports");
        ensure!(self.generation.is_none(),"activate through the active Store handle");
        let name=generation.to_owned();
        self.write(move|tx,current| {
            ensure!(current.committed_capture.is_none(),"offline migration activation required; live capture cursor exists");
            ensure!(name!=current.active_generation,"generation is already active");
            ensure!(verified_manifest["complete"]==true && verified_manifest["index_verified"]==true,"generation activation requires complete fact/index verification");
            let mut staged=checked_version(tx.open_table(VERSIONS)?.get(name.as_str())?.context("staged generation not found")?.value())?;
            ensure!(staged.committed_capture.is_none(),"legacy generation cannot contain a native cursor");
            require_current_index(&staged)?;
            let check_key=named_key(&name,"migration","index_verification");
            let saved=tx.open_table(METADATA)?.get(check_key.as_slice())?.context("staged generation has not been verified")?.value().to_vec();
            let saved:Value=serde_json::from_slice(&saved)?;
            ensure!(saved==verified_manifest && saved["verified_commit_id"].as_str()==Some(&staged.commit_id.to_string()),"staging changed after validation or manifest was not issued by Store");
            if let Some(boundary)=boundary {
                validate_boundary(&boundary,&name,&verified_manifest)?;
                let key=named_key(&name,"migration","projection_start_boundary");let bytes=serde_json::to_vec(&boundary)?;tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;
            }
            advance(tx,&mut staged)?;
            let key=named_key(&name,"migration","verified_manifest");let bytes=serde_json::to_vec(&verified_manifest)?;tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;
            tx.open_table(GLOBAL)?.insert("active",name.as_str())?;save_version(tx,&staged)?;Ok(staged)
        }).await
    }
    pub async fn verify_staging(&self)->Result<Value> {
        ensure!(self.generation.is_some(),"verify an isolated staging generation");
        let (view,mut manifest)=self.read(|db,generation| {let tx=db.begin_read()?;let version=read_version(&tx,generation)?;Ok((version.clone(),range_index::verify(&tx,&version.active_generation)?))}).await?;
        self.write(move|tx,current| {expected(current,&Some(view))?;advance(tx,current)?;manifest["verified_commit_id"]=json!(current.commit_id.to_string());
            manifest["store_epoch"]=json!(current.store_epoch);manifest["generation"]=json!(current.active_generation);
            let key=named_key(&current.active_generation,"migration","index_verification");let bytes=serde_json::to_vec(&manifest)?;tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;Ok(manifest)
        }).await
    }
    pub async fn metadata(&self,namespace:&str,key:&str)->Result<Option<Value>> {
        let namespace=namespace.to_owned();let key=key.to_owned();self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let key=named_key(&version.active_generation,&namespace,&key);
            tx.open_table(METADATA)?.get(key.as_slice())?.map(|v|serde_json::from_slice(v.value()).map_err(Into::into)).transpose()
        }).await
    }
    pub async fn set_metadata(&self,namespace:&str,key:&str,value:Value)->Result<SnapshotVersion> {
        verified_state_patch::require_generic_metadata_key(namespace,key)?;
        let namespace=namespace.to_owned();let key=key.to_owned();self.write(move|tx,version| {
            let bytes=serde_json::to_vec(&value)?;let key=named_key(&version.active_generation,&namespace,&key);tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;
            advance(tx,version)?;Ok(version.clone())
        }).await
    }
    pub async fn update_metadata<F>(&self,namespace:&str,key:&str,update:F)->Result<SnapshotVersion>
    where F:FnOnce(Value)->Result<Value>+Send+'static {
        Ok(self.update_metadata_receipt(namespace,key,update).await?.0)
    }
    /// Return the committed configuration and version from the same transaction.
    pub async fn update_metadata_receipt<F>(&self,namespace:&str,key:&str,update:F)->Result<(SnapshotVersion,Value)>
    where F:FnOnce(Value)->Result<Value>+Send+'static {
        verified_state_patch::require_generic_metadata_key(namespace,key)?;
        let namespace=namespace.to_owned();let key=key.to_owned();self.write(move|tx,version| {
            let key=named_key(&version.active_generation,&namespace,&key);let mut table=tx.open_table(METADATA)?;
            let old=table.get(key.as_slice())?.map(|v|serde_json::from_slice(v.value())).transpose()?.unwrap_or(Value::Null);let new=update(old)?;let bytes=serde_json::to_vec(&new)?;table.insert(key.as_slice(),bytes.as_slice())?;
            if namespace=="routes" {version.route_version=hex::encode(Sha256::digest(&bytes));}
            advance(tx,version)?;Ok((version.clone(),new))
        }).await
    }
    pub async fn configure_versions(&self,catalog_version:String,schedule_version:String)->Result<SnapshotVersion> {
        self.write(move|tx,version| {if version.catalog_version!=catalog_version || version.schedule_version!=schedule_version {
            advance(tx,version)?;version.catalog_version=catalog_version;version.schedule_version=schedule_version;
        }Ok(version.clone())}).await
    }
    pub async fn commit_history_metadata(&self,key:String,state:Value,range:Option<(chrono::DateTime<chrono::Utc>,chrono::DateTime<chrono::Utc>)>,evidence:Value)->Result<SnapshotVersion> {
        self.write(move|tx,version| {
            let mut table=tx.open_table(METADATA)?;let state_key=named_key(&version.active_generation,"series_state",&key);let bytes=serde_json::to_vec(&state)?;table.insert(state_key.as_slice(),bytes.as_slice())?;
            if let Some((start,end))=range {ensure!(start<end,"history coverage interval must be positive");let coverage_key=named_key(&version.active_generation,"coverage",&key);
                let mut coverage:Value=table.get(coverage_key.as_slice())?.map(|v|serde_json::from_slice(v.value())).transpose()?.unwrap_or_else(||json!({"ranges":[]}));
                let mut ranges=coverage["ranges"].as_array().into_iter().flatten().map(|v|Ok((v[0].as_str().context("coverage start")?.parse::<chrono::DateTime<chrono::Utc>>()?,v[1].as_str().context("coverage end")?.parse::<chrono::DateTime<chrono::Utc>>()?))).collect::<Result<Vec<_>>>()?;
                ranges.push((start,end));ranges.sort_unstable();let mut merged:Vec<(chrono::DateTime<chrono::Utc>,chrono::DateTime<chrono::Utc>)>=Vec::new();
                for (lo,hi) in ranges {if let Some(last)=merged.last_mut().filter(|v|v.1>=lo){last.1=last.1.max(hi);}else{merged.push((lo,hi));}}
                coverage["ranges"]=json!(merged);coverage["last_evidence"]=evidence;coverage["semantics"]=json!("final_revision_history");
                let bytes=serde_json::to_vec(&coverage)?;table.insert(coverage_key.as_slice(),bytes.as_slice())?;
            }advance(tx,version)?;Ok(version.clone())
        }).await
    }
    pub async fn lookup_bars(&self,keys:Vec<CanonicalBarKey>)->Result<(SnapshotVersion,Vec<Option<ImportBarRow>>)> {
        ensure!(keys.len()<=MAX_BATCH_ROWS,"lookup exceeds bounded row count");self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let table=tx.open_table(FACTS)?;
            let rows=keys.into_iter().map(|key| {let mut bytes=scope(&version.active_generation,&key.source_id,&key.symbol,key.interval_seconds);bytes.extend(signed_key(key.open_time_ns));table.get(bytes.as_slice())?.map(|v|StoredBar::decode(v.value()).map(|f|{let mut row=f.row;row.source_metadata=projected_source(row.source_metadata,&f.capture,f.commit_id);row})).transpose()}).collect::<Result<_>>()?;Ok((version,rows))
        }).await
    }
    /// Complete event identity lookup, including events superseded in latest state.
    pub async fn lookup_quotes(&self,keys:Vec<CanonicalQuoteKey>)->Result<(SnapshotVersion,Vec<Option<ImportQuoteRow>>)> {
        ensure!(keys.len()<=MAX_BATCH_ROWS,"quote lookup exceeds bounded row count");
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let ids=tx.open_table(EVENT_IDENTITIES)?;let events=tx.open_table(EVENTS)?;
            let rows=keys.into_iter().map(|key|->Result<_> {
                let mut prefix=quote_scope(&version.active_generation,&key.source_id,&key.symbol);let mut identity=prefix.clone();component(&mut identity,&key.event_id);
                let Some(ordinal)=ids.get(identity.as_slice())? else{return Ok(None)};prefix.extend(ordinal.value().to_be_bytes());
                let fact=StoredQuote::decode(events.get(prefix.as_slice())?.context("quote identity index missing event")?.value())?;
                let mut row=fact.row;row.source_metadata=projected_source(row.source_metadata,&fact.capture,fact.commit_id);Ok(Some(row))
            }).collect::<Result<_>>()?;Ok((version,rows))
        }).await
    }
    /// Complete event facts from one MVCC view, including superseded identities.
    /// Empty source AND symbol select this generation; otherwise both are exact.
    /// Callbacks consume bounded typed rows inside the read view, never a full Vec.
    pub async fn canonical_quote_scan<F>(&self,source:String,symbol:String,wanted:SnapshotVersion,batch_rows:usize,mut on_batch:F)->Result<Value>
    where F:FnMut(Vec<ImportQuoteRow>)->Result<()>+Send+'static {
        ensure!(source.is_empty()==symbol.is_empty()&&source.len()<=256&&symbol.len()<=256,"quote scan requires an exact source/symbol pair or an empty whole-generation scope");
        ensure!((1..=1000).contains(&batch_rows),"quote scan batch count outside bounded range");
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&Some(wanted))?;
            let prefix=if source.is_empty(){let mut key=Vec::new();component(&mut key,&version.active_generation);key}else{quote_scope(&version.active_generation,&source,&symbol)};let end=prefix_end(&prefix)?;
            let table=tx.open_table(EVENTS)?;let mut batch=Vec::with_capacity(batch_rows);let mut bytes=0usize;let mut rows=0u64;let mut batches=0u64;
            for entry in table.range(prefix.as_slice()..end.as_slice())? {
                let(_,value)=entry?;let fact=StoredQuote::decode(value.value())?;let mut row=fact.row;
                row.source_metadata=projected_source(row.source_metadata,&fact.capture,fact.commit_id);
                let size=serde_json::to_vec(&row)?.len();ensure!(size<=4*1024*1024,"quote scan single row exceeds 4MiB; no truncated row or complete summary");
                if !batch.is_empty()&&(batch.len()==batch_rows||bytes.checked_add(size).context("quote scan byte count overflow")?>4*1024*1024){on_batch(std::mem::take(&mut batch))?;batches=batches.checked_add(1).context("quote scan batch count exhausted")?;bytes=0;}
                bytes=bytes.checked_add(size).context("quote scan byte count overflow")?;batch.push(row);rows=rows.checked_add(1).context("quote event count exhausted")?;
            }
            if !batch.is_empty(){on_batch(batch)?;batches=batches.checked_add(1).context("quote scan batch count exhausted")?;}
            Ok(json!({"version":version,"row_count":rows.to_string(),"batches":batches.to_string(),"complete":true,"order":"canonical scope then durable application ordinal"}))
        }).await
    }
    /// Small latest-state inventory in one fixed read view. Exceeding either cap
    /// is an error; callers must not treat a truncated inventory as complete.
    pub async fn canonical_latest_quote_rows(&self,wanted:SnapshotVersion)->Result<Vec<ImportQuoteRow>> {
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&Some(wanted))?;
            let mut prefix=Vec::new();component(&mut prefix,&version.active_generation);let end=prefix_end(&prefix)?;
            let table=tx.open_table(QUOTES)?;let mut rows=Vec::new();let mut bytes=0usize;
            for entry in table.range(prefix.as_slice()..end.as_slice())? {
                ensure!(rows.len()<4096,"latest quote inventory exceeds 4096 catalog rows");
                let(_,value)=entry?;let fact=StoredQuote::decode(value.value())?;let mut row=fact.row;row.source_metadata=projected_source(row.source_metadata,&fact.capture,fact.commit_id);
                bytes=bytes.checked_add(serde_json::to_vec(&row)?.len()).context("latest quote inventory size overflow")?;
                ensure!(bytes<=16*1024*1024,"latest quote inventory exceeds 16MiB; no partial inventory returned");rows.push(row);
            }Ok(rows)
        }).await
    }
    pub async fn generation_summary(&self)->Result<Value> {
        self.read(|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;let mut prefix=Vec::new();component(&mut prefix,&version.active_generation);let end=prefix_end(&prefix)?;
            let mut counts=serde_json::Map::new();
            for (label,definition) in [("bar_rows",FACTS),("quote_events",EVENTS),("latest_quotes",QUOTES),("import_receipts",IMPORTS),("metadata_rows",METADATA)] {
                let mut count=0u64;
                for entry in tx.open_table(definition)?.range(prefix.as_slice()..end.as_slice())? {entry?;count=count.checked_add(1).context("generation row count exhausted")?;}
                counts.insert(label.into(),json!(count.to_string()));
            }
            let mut identities=0u64;
            for entry in tx.open_table(EVENT_IDENTITIES)?.range(prefix.as_slice()..end.as_slice())? {entry?;identities=identities.checked_add(1).context("event identity count exhausted")?;}
            counts.insert("quote_event_identities".into(),json!(identities.to_string()));
            Ok(json!({"version":version,"counts":counts,"complete":true}))
        }).await
    }
    async fn import_rows(&self,context:ImportContext,row_offset:u64,bars:Vec<ImportBarRow>,quotes:Vec<ImportQuoteRow>,metadata:Vec<ImportMetadataRow>)->Result<ImportReceipt> {
        ensure!(self.generation.is_some(),"legacy import requires an explicitly isolated staging generation");
        ensure!(bars.len()+quotes.len()+metadata.len()<=MAX_BATCH_ROWS,"import batch exceeds bounded row count");
        identity(&context.origin_id)?;ensure!(context.schema_version==SCHEMA_VERSION,"import contract schema mismatch");
        for row in &metadata {verified_state_patch::require_generic_metadata_key(&row.namespace,&row.key)?;}
        self.write(move|tx,version| {
            let bars=bars.into_iter().map(StoredBar::validate_import).collect::<Result<Vec<_>>>()?;
            for row in &bars {if row.state=="final" && row.finalized_at_ns.is_none() {
                ensure!(row.evidence["fixed_snapshot"]["source_fingerprint"].as_str()==Some(context.source_fingerprint.as_str()),"legacy finality evidence fingerprint differs from import context");
                ensure!(row.evidence["fixed_snapshot"]["snapshot"].as_str().is_some_and(|v|!v.is_empty()) && row.evidence["fixed_snapshot"]["table_sha256"].as_str().is_some_and(|v|hex::decode(v).is_ok_and(|v|v.len()==32)),"legacy finality evidence requires fixed snapshot and table digest");
            }}
            let quotes=quotes.into_iter().map(StoredQuote::validate).collect::<Result<Vec<_>>>()?;
            let body=serde_json::to_vec(&(&context,row_offset,&bars,&quotes,&metadata))?;let digest=hex::encode(Sha256::digest(body));
            let receipt_key=named_key(&version.active_generation,"import",&format!("{}:{}:{}",context.origin_id,context.range_label,row_offset));
            if let Some(previous)=tx.open_table(IMPORTS)?.get(receipt_key.as_slice())? {
                let saved:Value=serde_json::from_slice(previous.value())?;ensure!(saved["digest"].as_str()==Some(&digest),"conflicting retry at the same legacy import offset");
                return Ok(ImportReceipt {version:version.clone(),accepted:0,unchanged:(bars.len()+quotes.len()+metadata.len()) as u64,rejected:0,origin_id:context.origin_id});
            }
            advance(tx,version)?;let mut counts=apply_bars(tx,version,bars,None)?;let q=apply_quotes(tx,version,quotes,None)?;
            counts.accepted+=q.accepted;counts.unchanged+=q.unchanged;counts.rejected+=q.rejected;
            for row in metadata {
                identity(&row.namespace)?;identity(&row.key)?;let key=named_key(&version.active_generation,&row.namespace,&row.key);let bytes=serde_json::to_vec(&row.value)?;tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;
                let evidence_key=named_key(&version.active_generation,"metadata_evidence",&serde_json::to_string(&(&row.namespace,&row.key))?);let bytes=serde_json::to_vec(&json!({"origin":context.origin_id,"evidence":row.evidence}))?;tx.open_table(METADATA)?.insert(evidence_key.as_slice(),bytes.as_slice())?;counts.accepted+=1;
            }
            let receipt=ImportReceipt {version:version.clone(),accepted:counts.accepted,unchanged:counts.unchanged,rejected:counts.rejected,origin_id:context.origin_id.clone()};
            let bytes=serde_json::to_vec(&json!({"context":context,"digest":digest,"receipt":receipt}))?;tx.open_table(IMPORTS)?.insert(receipt_key.as_slice(),bytes.as_slice())?;Ok(receipt)
        }).await
    }
    pub async fn import_bars(&self,batch:ImportBatch<ImportBarRow>)->Result<ImportReceipt> {self.import_rows(batch.context,batch.row_offset,batch.rows,vec![],vec![]).await}
    pub async fn import_quotes(&self,batch:ImportBatch<ImportQuoteRow>)->Result<ImportReceipt> {self.import_rows(batch.context,batch.row_offset,vec![],batch.rows,vec![]).await}
    pub async fn import_metadata(&self,batch:ImportBatch<ImportMetadataRow>)->Result<ImportReceipt> {self.import_rows(batch.context,batch.row_offset,vec![],vec![],batch.rows).await}
    pub async fn commit_rows(&self,position:CapturePosition,bars:Vec<ImportBarRow>,quotes:Vec<ImportQuoteRow>,errors:Vec<Value>)->Result<ProjectionReceipt> {
        self.commit_rows_with_decoder(position,bars,quotes,errors,None).await
    }
    pub async fn commit_rows_with_decoder(&self,position:CapturePosition,bars:Vec<ImportBarRow>,quotes:Vec<ImportQuoteRow>,errors:Vec<Value>,decoder_state:Option<Value>)->Result<ProjectionReceipt> {
        ensure!(!self.inner.replay,"replay Store requires commit_replay_frames");
        ensure!(self.generation.is_none(),"staging generation cannot accept native projection");
        identity(&position.epoch)?;ensure!(position.sequence>0 && hex::decode(&position.digest).is_ok_and(|v|v.len()==32),"invalid capture position");
        self.write(move|tx,version| {
            let mut series=bars.iter().map(|row|(row.realtime_source_id.clone(),row.instrument_symbol.clone(),row.interval_seconds)).collect::<std::collections::BTreeSet<_>>();
            let quote_series=quotes.iter().map(|row|(row.realtime_source_id.clone(),row.instrument_symbol.clone())).collect::<std::collections::BTreeSet<_>>();
            ensure!(series.len()<=256 && quote_series.len()<=256,"frame exceeds canonical receipt series budget");
            let keys=if bars.len()<=512 {bars.iter().map(|row|fact_key(&version.active_generation,row)).collect()}else{Vec::new()};
            let complete=bars.len()<=512;
            if let Some(current)=&version.committed_capture {
                ensure!(current.epoch==position.epoch,"capture epoch changed; explicit recovery required");
                if current.sequence==position.sequence {ensure!(current==&position,"capture position digest conflict");return projection_receipt(tx,version,series,quote_series,keys,complete);}
                ensure!(current.sequence.checked_add(1)==Some(position.sequence),"projection capture gap or out-of-order frame");
            } else {
                let key=named_key(&version.active_generation,"migration","projection_start_boundary");
                if let Some(bytes)=tx.open_table(METADATA)?.get(key.as_slice())? {
                    let boundary:ProjectionStartBoundary=serde_json::from_slice(bytes.value())?;
                    ensure!(boundary.production_terminal,"legacy authority boundary is a rehearsal; native acquisition is disabled");
                    ensure!(position.epoch==boundary.raw_tail.epoch && boundary.raw_tail.sequence.checked_add(1)==Some(position.sequence),"first native projection must immediately follow verified legacy authority baseline");
                }else{ensure!(position.sequence==1,"initial projection requires capture sequence 1; retained gaps cannot be skipped");}
            }
            let bars=bars.into_iter().map(StoredBar::validate).collect::<Result<Vec<_>>>()?;let quotes=quotes.into_iter().map(StoredQuote::validate).collect::<Result<Vec<_>>>()?;
            advance(tx,version)?;if let Some(decoder)=decoder_state.as_ref(){series.extend(apply_calendar_state(tx,version,&position,decoder)?);}
            apply_source_clock_state(tx,version,&position,&bars)?;
            apply_bars(tx,version,bars,Some(position.clone()))?;apply_quotes(tx,version,quotes,Some(position.clone()))?;
            for (index,error) in errors.iter().enumerate() {let key=named_key(&version.active_generation,"projection_error",&format!("{}:{}:{index}",position.epoch,position.sequence));let bytes=serde_json::to_vec(error)?;tx.open_table(ERRORS)?.insert(key.as_slice(),bytes.as_slice())?;}
            if !errors.is_empty(){let key=named_key(&version.active_generation,"runtime","decode_failures");let mut table=tx.open_table(METADATA)?;let old=table.get(key.as_slice())?.map(|v|serde_json::from_slice::<String>(v.value())).transpose()?.map(|v|v.parse::<u64>()).transpose()?.unwrap_or(0);let count=old.checked_add(errors.len().try_into()?).context("decode failure counter exhausted")?;let bytes=serde_json::to_vec(&count.to_string())?;table.insert(key.as_slice(),bytes.as_slice())?;}
            if let Some(decoder)=decoder_state.filter(|v|v["schema"]!="calendar-projection-only-v1") {let key=named_key(&version.active_generation,"runtime","decoder_checkpoint");let bytes=serde_json::to_vec(&json!({"position":position,"decoder":decoder,"projector_version":version.projector_version}))?;ensure!(bytes.len()<=4*1024*1024,"decoder checkpoint exceeds bounded catalog state");tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;}
            version.committed_capture=Some(position);projection_receipt(tx,version,series,quote_series,keys,complete)
        }).await
    }
    pub async fn commit_projection(&self,commit:ProjectionCommit)->Result<ProjectionReceipt> {
        let (position,bars,quotes,errors,decoder)=tokio::task::spawn_blocking(move||->Result<_> {let bars=commit.bars.iter().map(bar_from_value).collect::<Result<Vec<_>>>()?;let quotes=commit.quotes.iter().map(quote_from_value).collect::<Result<Vec<_>>>()?;Ok((commit.position,bars,quotes,commit.errors,commit.decoder_state))}).await??;
        self.commit_rows_with_decoder(position,bars,quotes,errors,decoder).await
    }
    /// One durability commit, with deterministic per-frame logical revisions.
    /// This API is restricted to a disposable replay Store, never live facts.
    pub async fn commit_replay_frames(&self,frames:Vec<ProjectionCommit>)->Result<ProjectionReceipt> {
        ensure!(self.inner.replay && self.generation.is_none(),"replay batching requires an explicitly regenerable replay Store");
        ensure!(!frames.is_empty() && frames.len()<=64,"replay batch must contain 1..64 frames");
        self.write(move|tx,version| {
            let mut dirty=DirtyLeaves::new();let mut series=std::collections::BTreeSet::new();let mut quote_series=std::collections::BTreeSet::new();let mut keys=std::collections::BTreeSet::new();let mut complete=true;
            let last=frames.last().context("empty replay batch")?.position.clone();
            if version.committed_capture.as_ref()==Some(&last) {return projection_receipt(tx,version,series,quote_series,keys.into_iter().collect(),false);}
            let max_bytes=if frames.len()==1 {256*1024*1024}else{4*1024*1024};
            let mut budget=BoundedJsonSize {bytes:0,max:max_bytes};serde_json::to_writer(&mut budget,&frames).context("replay projection batch exceeds byte budget")?;
            for frame in frames {
                let position=frame.position;identity(&position.epoch)?;
                ensure!(position.sequence>0 && hex::decode(&position.digest).is_ok_and(|v|v.len()==32),"invalid replay capture position");
                if let Some(previous)=&version.committed_capture {
                    ensure!(previous.epoch==position.epoch && previous.sequence.checked_add(1)==Some(position.sequence),"replay prefix has a gap, epoch mismatch or reordered frame");
                }else{ensure!(position.sequence==1,"replay starts with empty facts at original capture sequence 1");}
                let bars=frame.bars.iter().map(bar_from_value).map(|r|r.and_then(StoredBar::validate)).collect::<Result<Vec<_>>>()?;
                let quotes=frame.quotes.iter().map(quote_from_value).map(|r|r.and_then(StoredQuote::validate)).collect::<Result<Vec<_>>>()?;
                ensure!(bars.len()<=500_000 && quotes.len()<=100_000,"replay frame exceeds bounded decoded row count");
                for row in &bars {series.insert((row.realtime_source_id.clone(),row.instrument_symbol.clone(),row.interval_seconds));if complete {keys.insert(fact_key(&version.active_generation,row));if keys.len()>512{keys.clear();complete=false;}}}
                for row in &quotes {quote_series.insert((row.realtime_source_id.clone(),row.instrument_symbol.clone()));}
                ensure!(series.len()<=256 && quote_series.len()<=256,"replay batch exceeds affected-series budget");
                // The logical version advances for each source frame, regardless
                // of batch size, so derived bar revisions do not depend on fsync grouping.
                advance(tx,version)?;if let Some(decoder)=frame.decoder_state.as_ref(){series.extend(apply_calendar_state(tx,version,&position,decoder)?);}
                apply_source_clock_state(tx,version,&position,&bars)?;
                apply_bars_deferred(tx,version,bars,Some(position.clone()),&mut dirty)?;apply_quotes(tx,version,quotes,Some(position.clone()))?;
                for(index,error)in frame.errors.iter().enumerate() {let key=named_key(&version.active_generation,"projection_error",&format!("{}:{}:{index}",position.epoch,position.sequence));let bytes=serde_json::to_vec(error)?;tx.open_table(ERRORS)?.insert(key.as_slice(),bytes.as_slice())?;}
                version.committed_capture=Some(position);
            }
            range_index::recompute(tx,&version.active_generation,dirty)?;
            projection_receipt(tx,version,series,quote_series,keys.into_iter().collect(),complete)
        }).await
    }
    pub async fn commit_external_facts(&self,records:Vec<ExternalFactRecord>)->Result<SnapshotVersion> {
        ensure!(records.len()<=256,"external facts batch exceeds bounded scope");
        self.write(move|tx,version| {
            let mut facts=tx.open_table(EXTERNAL)?;let mut ids=tx.open_table(EXTERNAL_IDENTITIES)?;let mut metadata=tx.open_table(METADATA)?;let mut changed=false;
            for row in records {
                ensure!(matches!(row.kind.as_str(),"multi_timeframe"|"volatility"|"positioning") && row.revision>0,"invalid external context kind/revision");
                for text in [&row.scope.instrument_symbol,&row.source,&row.record_id]{identity(text)?;}
                if let Some(source)=&row.scope.market_source_id{identity(source)?;}
                let bytes=serde_json::to_vec(&row)?;ensure!(bytes.len()<=1024*1024,"external fact exceeds metadata byte bound");
                let prefix=external_prefix(&version.active_generation,&row.scope,&row.kind,&row.source)?;
                let mut identity_key=prefix.clone();component(&mut identity_key,&row.record_id);identity_key.extend(row.revision.to_be_bytes());
                if let Some(old)=ids.get(identity_key.as_slice())? {ensure!(old.value()==bytes.as_slice(),"conflicting external fact at identical identity/revision");continue;}
                let known=external_known_at(&row).unwrap_or(i64::MIN);let mut key=prefix;key.extend(signed_key(known));component(&mut key,&row.record_id);key.extend(row.revision.to_be_bytes());
                facts.insert(key.as_slice(),bytes.as_slice())?;ids.insert(identity_key.as_slice(),bytes.as_slice())?;
                let providers_key=named_key(&version.active_generation,"external_providers",&external_scope_kind(&row.scope,&row.kind)?);
                let mut providers=metadata.get(providers_key.as_slice())?.map(|v|serde_json::from_slice::<std::collections::BTreeSet<String>>(v.value())).transpose()?.unwrap_or_default();providers.insert(row.source);
                ensure!(providers.len()<=64,"external provider scope exceeds bounded catalog");let bytes=serde_json::to_vec(&providers)?;metadata.insert(providers_key.as_slice(),bytes.as_slice())?;changed=true;
            }if changed{advance(tx,version)?;}Ok(version.clone())
        }).await
    }
    pub async fn canonical_scan<F>(&self,request:CanonicalScanRequest,batch_rows:usize,on_batch:F)->Result<CanonicalScanSummary>
    where F:FnMut(CanonicalScanBatch)->Result<()>+Send+'static {self.canonical_scan_with_resume(request,batch_rows,None,on_batch).await}
    /// No evaluator callback runs in this read transaction. Overflow yields no
    /// partial input; the caller may export the same expected version instead.
    pub async fn materialize_tail(&self,request:CanonicalScanRequest,period:crate::periods::Period,schedule:Option<crate::periods::MarketSchedule>,resume:ScanResume,max_rows:usize,max_bytes:usize,cancel:Arc<dyn Fn()->bool+Send+Sync>)->Result<MaterializedScan> {
        ensure!(max_rows>0 && max_rows<=4096 && max_bytes>0 && max_bytes<=16*1024*1024,"materialized tail bounds exceed supported limits");
        let output=Arc::new(Mutex::new((Vec::new(),0usize,0usize)));let target=output.clone();
        let collect=move|batch:CanonicalScanBatch|->Result<()> {
            ensure!(!cancel(),"quant_cancelled");
            let mut out=target.lock().map_err(|_|anyhow::anyhow!("materialized tail accumulator poisoned"))?;
            let row_count=out.1.checked_add(batch.rows.len()).context("materialized row count overflow")?;
            if row_count>max_rows {return Err(MaterializedTailTooLarge.into());}
            // The count limit protects typed allocation. A streaming serializer
            // also bounds variable-size raw evidence and the first context.
            let mut bytes=BoundedJsonSize {bytes:out.2,max:max_bytes};
            if let Some(context)=&batch.context {if serde_json::to_writer(&mut bytes,context).is_err(){return Err(MaterializedTailTooLarge.into());}}
            if serde_json::to_writer(&mut bytes,&batch.rows).is_err(){return Err(MaterializedTailTooLarge.into());}
            out.1=row_count;out.2=bytes.bytes;out.0.push(batch);Ok(())
        };
        let summary=if period.is_base(){self.canonical_scan_with_calendar_context(request,256,Some(resume),schedule,collect).await?}
            else{self.canonical_calendar_scan(request,period,schedule,256,Some(resume),collect).await?};
        let batches=std::mem::take(&mut output.lock().map_err(|_|anyhow::anyhow!("materialized tail accumulator poisoned"))?.0);
        Ok(MaterializedScan {batches,summary})
    }
    pub async fn canonical_scan_context(&self,request:CanonicalScanRequest,schedule:Option<crate::periods::MarketSchedule>)->Result<CanonicalScanBatch> {
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&request.expected_version)?;
            let schedule=resolve_calendar(&tx,&version,&request.source_id,&request.symbol,request.end_ns,schedule)?;
            let context=scan_context_schedule(&tx,&version,&request.source_id,&request.symbol,request.interval_seconds,request.end_ns,schedule.as_ref())?;
            Ok(CanonicalScanBatch {version,context:Some(context),row_offset:0,rows:vec![]})
        }).await
    }
    pub async fn canonical_scan_with_resume<F>(&self,request:CanonicalScanRequest,batch_rows:usize,resume:Option<ScanResume>,on_batch:F)->Result<CanonicalScanSummary>
    where F:FnMut(CanonicalScanBatch)->Result<()>+Send+'static {
        self.canonical_scan_with_calendar_context(request,batch_rows,resume,None,on_batch).await
    }
    pub async fn canonical_scan_with_calendar_context<F>(&self,mut request:CanonicalScanRequest,batch_rows:usize,resume:Option<ScanResume>,schedule:Option<crate::periods::MarketSchedule>,mut on_batch:F)->Result<CanonicalScanSummary>
    where F:FnMut(CanonicalScanBatch)->Result<()>+Send+'static {
        ensure!(batch_rows>0 && batch_rows<=MAX_PAGE_ROWS,"scan batch rows outside 1..10000");ensure!(request.start_ns<=request.end_ns,"inverted scan range");
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&request.expected_version)?;
            let schedule=resolve_calendar(&tx,&version,&request.source_id,&request.symbol,request.end_ns,schedule)?;
            let scan=scan_context_schedule(&tx,&version,&request.source_id,&request.symbol,request.interval_seconds,request.end_ns,schedule.as_ref())?;
            if let Some(resume)=resume {
                let current:SeriesVersion=serde_json::from_value(scan.coverage["series_version"].clone()).context("quant_resume_invalid: series proof unavailable")?;
                ensure!(current.series_generation==resume.series_generation && current.correction_epoch==resume.correction_epoch,"quant_resume_invalid: prefix was replaced or corrected");
                request.start_ns=request.start_ns.max(resume.after_ns.checked_add(1).context("quant_resume_invalid: timestamp exhausted")?);
                request.start_ns=request.start_ns.min(request.end_ns);
            }
            let prefix=scope(&version.active_generation,&request.source_id,&request.symbol,request.interval_seconds);
            let mut lo=prefix.clone();lo.extend(signed_key(request.start_ns));let mut hi=prefix;hi.extend(signed_key(request.end_ns));
            let mut hash=Sha256::new();let mut row_count=0u64;let mut batch=Vec::with_capacity(batch_rows);let mut context=Some(scan);
            for record in tx.open_table(FACTS)?.range(lo.as_slice()..hi.as_slice())? {
                let (_,bytes)=record?;let fact=StoredBar::decode(bytes.value())?;let mut row=fact.row;
                row.source_metadata=projected_source(row.source_metadata,&fact.capture,fact.commit_id);if request.final_only && row.state!="final" {continue;}
                hash.update(serde_json::to_vec(&row)?);hash.update(b"\n");batch.push(row);
                if batch.len()==batch_rows {let count=batch.len() as u64;on_batch(CanonicalScanBatch {version:version.clone(),context:context.take(),row_offset:row_count,rows:std::mem::take(&mut batch)})?;row_count+=count;}
            }
            if !batch.is_empty() || context.is_some() {let count=batch.len() as u64;on_batch(CanonicalScanBatch {version:version.clone(),context:context.take(),row_offset:row_count,rows:batch})?;row_count+=count;}
            Ok(CanonicalScanSummary {version,row_count,sha256:hex::encode(hash.finalize()),complete:true})
        }).await
    }
    pub async fn canonical_snapshot(&self,request:CanonicalSnapshotRequest)->Result<CanonicalSnapshot> {
        self.canonical_snapshot_at(request,None,i64::MAX).await
    }
    pub async fn canonical_snapshot_at(&self,request:CanonicalSnapshotRequest,schedule:Option<crate::periods::MarketSchedule>,as_of:i64)->Result<CanonicalSnapshot> {
        let interval=match request.period.as_str() {"1m"=>60,"1s"=>1,_=>bail!("calendar period snapshot requires authoritative bucket bounds; use range_aggregates")};
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&request.expected_version)?;
            let rows=select_rows(&tx,&version,&request.symbol,&request.source_id,interval,&request.selection,request.final_only)?;
            let schedule=resolve_calendar(&tx,&version,&request.source_id,&request.symbol,as_of,schedule)?;
            let context=scan_context_schedule_mode(&tx,&version,&request.source_id,&request.symbol,interval,as_of,schedule.as_ref(),false,None)?;
            Ok(CanonicalSnapshot {version,bars:rows.iter().map(bar_value).collect::<Result<Vec<_>>>()?,quote:context.quote,capabilities:context.capabilities,coverage:context.coverage,semantics:context.semantics,page_timings:None})
        }).await
    }
    pub async fn canonical_period_page(&self,request:CanonicalSnapshotRequest,period:crate::periods::Period,schedule:Option<crate::periods::MarketSchedule>)->Result<CanonicalSnapshot> {
        let as_of=chrono::Utc::now().timestamp_nanos_opt().context("page as-of outside signed ns")?;
        self.canonical_period_page_at(request,period,schedule,as_of).await
    }
    pub async fn canonical_period_page_at(&self,request:CanonicalSnapshotRequest,period:crate::periods::Period,schedule:Option<crate::periods::MarketSchedule>,as_of:i64)->Result<CanonicalSnapshot> {
        if period.is_base(){return self.canonical_snapshot_at(request,schedule,as_of).await;}
        let (before,count)=match request.selection {BarSelection::Latest {count}=>(None,count),BarSelection::Before {before_ns,count}=>(Some(before_ns),count),_=>bail!("calendar range requires bounded calendar scan")};
        ensure!(count>0 && count<=MAX_PAGE_ROWS,"calendar page row count outside bound");
        self.read(move|db,generation| {
            let started=std::time::Instant::now();let mut timings=CanonicalPageTimings::default();
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&request.expected_version)?;
            let schedule=resolve_calendar(&tx,&version,&request.source_id,&request.symbol,as_of,schedule)?;
            let facts=tx.open_table(FACTS)?;let prefix=scope(&version.active_generation,&request.source_id,&request.symbol,60);
            require_current_index(&version)?;let mut cursor=before;let mut bars=Vec::with_capacity(count);
            while bars.len()<count {
                let mut end=prefix.clone();if let Some(cursor)=cursor {end.extend(signed_key(cursor));}else{end=prefix_end(&prefix)?;}
                let Some((_,bytes))=facts.range(prefix.as_slice()..end.as_slice())?.next_back().transpose()? else{break;};
                let last=StoredBar::decode(bytes.value())?;
                if !crate::periods::belongs_to_schedule(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),schedule.as_ref())? {
                    let (previous,_)=crate::periods::session_neighbors(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),schedule.as_ref().context("declared calendar missing")?)?;
                    let Some(previous)=previous else{break;};cursor=Some(previous.timestamp_nanos_opt().context("calendar gap boundary outside signed ns")?);continue;
                }
                let bucket=crate::periods::bucket_for(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,schedule.as_ref())?;
                let lo=bucket.input_start().timestamp_nanos_opt().context("bucket start outside signed ns")?;let hi=bucket.input_end().timestamp_nanos_opt().context("bucket end outside signed ns")?;
                ensure!(lo<=last.row.open_time_ns && last.row.open_time_ns<hi && cursor.is_none_or(|at|lo<at),"calendar page did not advance");
                let open=bucket.start.timestamp_nanos_opt().context("bucket display outside signed ns")?;
                if before.is_none_or(|label|open<label) {
                let query_started=std::time::Instant::now();let value=calendar_query(&tx,&version.active_generation,&request.source_id,&request.symbol,lo,hi,schedule.as_ref())?.value;
                timings.calendar_query_ms+=query_started.elapsed().as_secs_f64()*1000.0;timings.buckets+=1;
                if let Some(value)=value {
                    let dto_started=std::time::Instant::now();
                    let row=aggregate_row(&value,&last.row,&version,period,open,hi,hi<=as_of,crate::periods::calendar_bucket_verified(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,schedule.as_ref())?,crate::periods::calendar_evidence_known_at(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,schedule.as_ref())?.and_then(|at|at.timestamp_nanos_opt()),source_confirmation_unknown(&tx,&version,&request.source_id,&request.symbol)?,as_of)?;
                    if !request.final_only || row.state=="final" {bars.push(bar_value(&StoredBar {row,commit_id:value.last_commit_id,capture:None})?);}
                    timings.canonical_dto_ms+=dto_started.elapsed().as_secs_f64()*1000.0;
                }}cursor=Some(lo);
            }
            bars.reverse();let context_started=std::time::Instant::now();let context=scan_context_schedule_mode(&tx,&version,&request.source_id,&request.symbol,60,as_of,schedule.as_ref(),false,Some(&mut timings))?;
            timings.context_ms=context_started.elapsed().as_secs_f64()*1000.0;timings.read_view_ms=started.elapsed().as_secs_f64()*1000.0;
            Ok(CanonicalSnapshot {version,bars,quote:context.quote,capabilities:context.capabilities,coverage:context.coverage,semantics:context.semantics,page_timings:Some(timings)})
        }).await
    }
    /// Calendar projection and delivery keep one read transaction. Range nodes avoid
    /// reading hundreds of thousands of constituent minutes for each long page.
    pub async fn canonical_calendar_scan<F>(&self,request:CanonicalScanRequest,period:crate::periods::Period,schedule:Option<crate::periods::MarketSchedule>,batch_rows:usize,resume:Option<ScanResume>,mut on_batch:F)->Result<CanonicalScanSummary>
    where F:FnMut(CanonicalScanBatch)->Result<()>+Send+'static {
        ensure!(!period.is_base() && request.interval_seconds==60,"calendar scan requires minute facts and a derived period");
        ensure!(batch_rows>0 && batch_rows<=MAX_PAGE_ROWS && request.start_ns<=request.end_ns,"invalid bounded calendar scan");
        self.read(move|db,generation| {
            let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&request.expected_version)?;
            require_current_index(&version)?;let schedule=resolve_calendar(&tx,&version,&request.source_id,&request.symbol,request.end_ns,schedule)?;
            let scan=scan_context_schedule(&tx,&version,&request.source_id,&request.symbol,60,request.end_ns,schedule.as_ref())?;
            // Selection uses display labels; each selected bucket reads all
            // constituents, including night-session minutes before its label.
            let mut cursor=if request.start_ns==i64::MIN {i64::MIN}else{crate::periods::bucket_for(chrono::DateTime::from_timestamp_nanos(request.start_ns),period,schedule.as_ref())?.input_start().timestamp_nanos_opt().context("calendar start outside signed ns")?};
            if let Some(resume)=&resume {
                let current:SeriesVersion=serde_json::from_value(scan.coverage["series_version"].clone()).context("quant_resume_invalid: series proof unavailable")?;
                ensure!(current.series_generation==resume.series_generation && current.correction_epoch==resume.correction_epoch,"quant_resume_invalid: prefix was replaced or corrected");
                let bucket=crate::periods::bucket_for(chrono::DateTime::from_timestamp_nanos(resume.after_ns),period,schedule.as_ref())?;
                let end=bucket.input_end().timestamp_nanos_opt().context("calendar boundary outside ns domain")?;
                // An append inside a formerly elapsed but incomplete last bucket
                // can revise that bucket without revising a minute already stored.
                let old_watermark=resume.append_watermark_ns.context("quant_resume_invalid: aggregate append proof unavailable")?;
                ensure!(old_watermark.checked_add(range_index::MINUTE_NS).is_some_and(|v|v>=end),"quant_resume_invalid: last cached aggregate may receive new components");
                cursor=cursor.max(end);
            }
            let facts=tx.open_table(FACTS)?;let prefix=scope(&version.active_generation,&request.source_id,&request.symbol,60);
            let mut hi=prefix.clone();hi.extend(signed_key(request.end_ns));
            let mut context=Some(scan);let mut rows=Vec::with_capacity(batch_rows);let mut row_count=0u64;let mut hash=Sha256::new();
            while cursor<request.end_ns {
                let mut lo=prefix.clone();lo.extend(signed_key(cursor));
                let Some((_,bytes))=facts.range(lo.as_slice()..hi.as_slice())?.next().transpose()? else {break;};
                let first=StoredBar::decode(bytes.value())?;
                if !crate::periods::belongs_to_schedule(chrono::DateTime::from_timestamp_nanos(first.row.open_time_ns),schedule.as_ref())? {
                    let (_,next)=crate::periods::session_neighbors(chrono::DateTime::from_timestamp_nanos(first.row.open_time_ns),schedule.as_ref().context("declared calendar missing")?)?;
                    let Some(next)=next else{break;};cursor=next.timestamp_nanos_opt().context("calendar gap boundary outside signed ns")?;continue;
                }
                let bucket=crate::periods::bucket_for(chrono::DateTime::from_timestamp_nanos(first.row.open_time_ns),period,schedule.as_ref())?;
                let start=bucket.input_start().timestamp_nanos_opt().context("calendar start outside signed ns")?;
                let end=bucket.input_end().timestamp_nanos_opt().context("calendar end outside signed ns")?;
                ensure!(end>cursor && end>start,"calendar scan did not advance");
                let display=bucket.start.timestamp_nanos_opt().context("calendar display time outside signed ns")?;
                if display>=request.start_ns && display<request.end_ns {if let Some(aggregate)=calendar_query(&tx,&version.active_generation,&request.source_id,&request.symbol,start,end.min(request.end_ns),schedule.as_ref())?.value {
                    if resume.as_ref().is_none_or(|r|display>r.after_ns) {
                        let row=aggregate_row(&aggregate,&first.row,&version,period,display,end,end<=request.end_ns,crate::periods::calendar_bucket_verified(chrono::DateTime::from_timestamp_nanos(first.row.open_time_ns),period,schedule.as_ref())?,crate::periods::calendar_evidence_known_at(chrono::DateTime::from_timestamp_nanos(first.row.open_time_ns),period,schedule.as_ref())?.and_then(|at|at.timestamp_nanos_opt()),source_confirmation_unknown(&tx,&version,&request.source_id,&request.symbol)?,request.end_ns)?;
                        if !request.final_only || row.state=="final" {hash.update(serde_json::to_vec(&row)?);hash.update(b"\n");rows.push(row);}
                        if rows.len()==batch_rows {let count=rows.len() as u64;on_batch(CanonicalScanBatch {version:version.clone(),context:context.take(),row_offset:row_count,rows:std::mem::take(&mut rows)})?;row_count+=count;}
                    }
                }}
                cursor=end;
            }
            if !rows.is_empty() || context.is_some() {let count=rows.len() as u64;on_batch(CanonicalScanBatch {version:version.clone(),context:context.take(),row_offset:row_count,rows})?;row_count+=count;}
            Ok(CanonicalScanSummary {version,row_count,sha256:hex::encode(hash.finalize()),complete:true})
        }).await
    }
    pub async fn range_aggregates(&self,symbol:&str,source:&str,bounds:Vec<(i64,i64)>,final_only:bool,wanted:Option<SnapshotVersion>)->Result<(SnapshotVersion,Vec<Option<RangeAggregate>>)> {
        ensure!(bounds.len()<=MAX_PAGE_ROWS,"aggregate request exceeds bounded bucket count");let symbol=symbol.to_owned();let source=source.to_owned();
        self.read(move|db,generation| {let tx=db.begin_read()?;let version=read_version(&tx,generation)?;expected(&version,&wanted)?;
            require_current_index(&version)?;let rows=bounds.into_iter().map(|(lo,hi)|range_index::query(&tx,&version.active_generation,&source,&symbol,lo,hi,final_only)).collect::<Result<Vec<_>>>()?;Ok((version,rows))}).await
    }
    pub async fn close(&self)->Result<()> {
        if self.inner.closed.swap(true,Ordering::AcqRel) {return Ok(());}
        let _writer=self.inner.writer.clone().acquire_owned().await?;let _readers=self.inner.readers.clone().acquire_many_owned(READERS).await?;
        self.inner.db.lock().map_err(|_|anyhow::anyhow!("store handle poisoned"))?.take();Ok(())
    }
}

fn fixture_decoder_upgrade(mut checkpoint:Value,position:&CapturePosition)->Result<Value> {
    ensure!(serde_json::from_value::<CapturePosition>(checkpoint["position"].clone())?==*position,"fixture decoder checkpoint anchor differs");
    ensure!(matches!(checkpoint["projector_version"].as_str(),Some("tracefang-projector-v2-exact")) || checkpoint["projector_version"]==PROJECTOR_VERSION,"fixture checkpoint projector is unsupported");
    let old=&checkpoint["decoder"];
    ensure!(old["version"]==2 || old["version"]==3,"fixture checkpoint decoder schema is unsupported");
    for key in ["quotes","daily","sessions"] {ensure!(old[key].as_object().is_some_and(|v|v.is_empty()),"fixture checkpoint has nonempty provider state requiring actual replay");}
    ensure!(old["calendar_authorities"].is_null() || old["calendar_authorities"].as_array().is_some_and(|v|v.is_empty()),"fixture checkpoint contains captured calendars");
    checkpoint["decoder"]=json!({"version":3,"quotes":{},"daily":{},"sessions":{},"calendar_authorities":[]});checkpoint["projector_version"]=json!(PROJECTOR_VERSION);Ok(checkpoint)
}

pub fn validate_boundary(boundary:&ProjectionStartBoundary,generation:&str,manifest:&Value)->Result<()> {
    let digest=|value:&str|->Result<()> {ensure!(hex::decode(value).is_ok_and(|v|v.len()==32),"legacy boundary requires valid SHA256 evidence");Ok(())};
    ensure!(boundary.kind=="legacy_import_authority" && boundary.schema_version=="tracefang-legacy-authority-v1","unknown legacy authority boundary contract");
    ensure!(!boundary.authority_manifest_id.is_empty() && !boundary.postgres_source_fingerprint.is_empty() && !boundary.postgres_snapshot.is_empty() && !boundary.conflict_policy_version.is_empty(),"incomplete legacy authority identity");
    ensure!(boundary.staging_generation==generation && manifest["generation"]==generation && manifest["fact_codec_sha256"]==boundary.verified_fact_sha256 && manifest["index_codec_sha256"]==boundary.verified_index_sha256,"legacy authority fact/index verification differs from staging");
    for value in [&boundary.authority_manifest_sha256,&boundary.legacy_mapping_sha256,&boundary.verified_fact_sha256,&boundary.verified_index_sha256,&boundary.raw_tail.digest,&boundary.closure.stop_report_sha256,&boundary.closure.projection_drain_report_sha256] {digest(value)?;}
    ensure!(boundary.raw_tail.sequence>0 && boundary.raw_tail.sequence<u64::MAX && !boundary.raw_tail.epoch.is_empty(),"invalid legacy raw tail anchor");
    ensure!(!boundary.closure.stopped_component_ids.is_empty() && boundary.closure.stopped_component_ids.iter().all(|v|!v.is_empty()) && boundary.closure.unresolved_frames==0,"legacy authority requires stopped producers and no unresolved frames");
    ensure!(boundary.closure.raw_applied_through_legacy.is_none_or(|v|v<=boundary.legacy_tail_sequence),"legacy projection cursor is ahead of stable raw tail");
    let drained=boundary.closure.raw_applied_through_legacy==Some(boundary.legacy_tail_sequence);
    if let Some(value)=&boundary.closure.reconciliation_report_sha256 {digest(value)?;}
    ensure!(drained || boundary.closure.reconciliation_report_sha256.is_some(),"legacy tail needs drain completion or explicit independent reconciliation");
    ensure!(boundary.closure.stable_tail_observations.len()>=2,"legacy authority requires repeated stable tail observations");
    let mut previous=None;
    for observation in &boundary.closure.stable_tail_observations {
        ensure!(observation.stream==boundary.legacy_stream && observation.epoch==boundary.legacy_epoch && observation.last_sequence==boundary.legacy_tail_sequence && previous.is_none_or(|time|observation.observed_at_ns>time),"legacy tail observations are inconsistent or not increasing");previous=Some(observation.observed_at_ns);
    }Ok(())
}

fn projection_receipt(tx:&WriteTransaction,version:&SnapshotVersion,series:std::collections::BTreeSet<(String,String,u32)>,quote_series:std::collections::BTreeSet<(String,String)>,keys:Vec<Vec<u8>>,complete:bool)->Result<ProjectionReceipt> {
    let facts=tx.open_table(FACTS)?;let mut hot_series=Vec::with_capacity(series.len());let mut series_changes=Vec::new();
    for (source_id,symbol,interval_seconds) in series {
        let stats_key=named_key(&version.active_generation,"series_version",&series_identity(&source_id,&symbol,interval_seconds));
        if let Some(bytes)=tx.open_table(METADATA)?.get(stats_key.as_slice())? {
            let stats:SeriesVersion=serde_json::from_slice(bytes.value())?;
            if stats.last_mutation_commit_id==version.commit_id {
                series_changes.push(CanonicalSeriesChange {source_id:source_id.clone(),symbol:symbol.clone(),interval_seconds,start_ns:stats.revision_start_ns,end_ns:stats.revision_end_ns.checked_add(i64::from(interval_seconds)*1_000_000_000).context("series change range overflow")?,historical_correction:stats.previous_append_watermark_ns.is_some_and(|v|stats.revision_start_ns<v),series_version:serde_json::to_value(&stats)?});
            }
        }
        let prefix=scope(&version.active_generation,&source_id,&symbol,interval_seconds);let end=prefix_end(&prefix)?;
        let mut bars=facts.range(prefix.as_slice()..end.as_slice())?.rev().take(240).map(|entry| {let (_,bytes)=entry?;bar_value(&StoredBar::decode(bytes.value())?)}).collect::<Result<Vec<_>>>()?;
        bars.reverse();hot_series.push(CanonicalHotSeries {source_id,symbol,interval_seconds,bars});
    }
    let changed_bars=keys.into_iter().map(|key|facts.get(key.as_slice())?.map(|bytes|bar_value(&StoredBar::decode(bytes.value())?)).transpose()).collect::<Result<Vec<_>>>()?.into_iter().flatten().collect();
    let quotes=tx.open_table(QUOTES)?;let mut latest_quotes=Vec::new();
    for (source,symbol) in quote_series {let key=quote_scope(&version.active_generation,&source,&symbol);if let Some(bytes)=quotes.get(key.as_slice())? {latest_quotes.push(quote_value(&StoredQuote::decode(bytes.value())?)?);}}
    Ok(ProjectionReceipt {version:version.clone(),hot_series,changed_bars,changed_bars_complete:complete,latest_quotes,series_changes})
}

/// A new prefix may establish reviewed authority from the actual captured
/// protocol. One fresh witness never certifies pre-existing unreviewed history.
fn apply_source_clock_state(tx:&WriteTransaction,version:&SnapshotVersion,position:&CapturePosition,rows:&[ImportBarRow])->Result<()> {
    let mut witnesses=std::collections::BTreeMap::<(&str,&str),Value>::new();
    for row in rows {
        let raw=&row.source_metadata["raw_payload"];let proof=&raw["authoritative_input"];
        if row.realtime_source_id!="tonghuashun_futures" || row.interval_seconds!=60 || raw["clock_policy_verified"]!=true || raw["minute_clock_policy"]!=crate::source_clock::THS_V6_SHFE_END_V2 {continue;}
        let provider=proof["provider_code"].as_str().context("reviewed source clock provider missing")?;
        ensure!(crate::source_clock::VERIFIED_V6_SCOPES.contains(&(provider,row.instrument_symbol.as_str())) && proof["protocol"]=="tonghuashun_public_line_v6" && proof["period"]=="61" && matches!(proof["response_kind"].as_str(),Some("minute_year"|"minute_last")),"reviewed source clock protocol/scope differs");
        ensure!(serde_json::from_value::<CapturePosition>(proof["capture_position"].clone())?==*position,"source clock witness differs from durable capture");
        ensure!(proof["body_sha256"].as_str().is_some_and(|s|s.len()==64 && s.bytes().all(|b|b.is_ascii_hexdigit())),"source clock body digest invalid");
        witnesses.entry((&row.realtime_source_id,&row.instrument_symbol)).or_insert_with(||proof.clone());
    }
    let mut metadata=tx.open_table(METADATA)?;
    for ((source,symbol),proof) in witnesses {
        let key=named_key(&version.active_generation,"source_clock",&format!("{source}:{symbol}"));
        if metadata.get(key.as_slice())?.is_some(){continue;}
        let prefix=scope(&version.active_generation,source,symbol,60);let end=prefix_end(&prefix)?;
        let preexisting=tx.open_table(FACTS)?.range(prefix.as_slice()..end.as_slice())?.next().transpose()?.is_some();
        let value=json!({"verified":!preexisting,"policy":crate::source_clock::THS_V6_SHFE_END_V2,"proof":proof,"capture_position":position,"prefix_semantics":if preexisting{"pre-existing authority is not certified by this newer witness"}else{"reviewed captured authority from the empty native prefix"},"unverified_preexisting_history":preexisting});
        let bytes=serde_json::to_vec(&value)?;metadata.insert(key.as_slice(),bytes.as_slice())?;
    }Ok(())
}
fn apply_calendar_state(tx:&WriteTransaction,version:&SnapshotVersion,position:&CapturePosition,decoder:&Value)->Result<std::collections::BTreeSet<(String,String,u32)>> {
    use crate::periods::{CapturedSourceCalendar,CalendarAuthority,MarketSchedule};
    let Some(rows)=decoder.get("calendar_authorities") else{return Ok(Default::default());};
    ensure!(rows.as_array().is_some_and(|v|v.len()<=256),"decoder calendar exceeds bounded scopes");
    let rows:Vec<CapturedSourceCalendar>=serde_json::from_value(rows.clone())?;let mut affected=std::collections::BTreeSet::new();
    let mut metadata=tx.open_table(METADATA)?;
    for row in rows {if row.day.capture_position.as_ref()!=Some(position){continue;}
        ensure!(row.source_id=="tonghuashun_futures" && !row.symbol.is_empty() && row.symbol.eq_ignore_ascii_case(&row.day.code),"captured calendar logical scope invalid");
        let check=MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:None,reference:None,sessions:vec![],authority:Some(CalendarAuthority{date_exceptions:None,absolute_days:vec![row.day.clone()]})};check.validate()?;
        let key=named_key(&version.active_generation,"source_calendar",&format!("{}:{}",row.source_id,row.symbol));
        let mut authority=metadata.get(key.as_slice())?.map(|v|serde_json::from_slice::<CalendarAuthority>(v.value())).transpose()?.unwrap_or_default();
        if let Some(old)=authority.absolute_days.iter_mut().find(|day|day.trade_date==row.day.trade_date){
            ensure!(old.market==row.day.market && old.code==row.day.code,"source calendar exact instrument changed");
            if old.raw_body_sha256==row.day.raw_body_sha256{continue;}*old=row.day.clone();
        }else{ensure!(authority.absolute_days.len()<4096,"source calendar date history exceeds explicit bound");authority.absolute_days.push(row.day.clone());}
        authority.absolute_days.sort_by_key(|day|day.trade_date);let bytes=serde_json::to_vec(&authority)?;ensure!(bytes.len()<=4*1024*1024,"source calendar metadata exceeds bounded bytes");metadata.insert(key.as_slice(),bytes.as_slice())?;
        let stats_key=named_key(&version.active_generation,"series_version",&series_identity(&row.source_id,&row.symbol,60));
        let old_stats=metadata.get(stats_key.as_slice())?.map(|bytes|serde_json::from_slice::<SeriesVersion>(bytes.value())).transpose()?;
        if let Some(mut stats)=old_stats {
            stats.correction_epoch=stats.correction_epoch.checked_add(1).context("calendar correction epoch exhausted")?;stats.previous_append_watermark_ns=Some(stats.append_watermark_ns);stats.last_mutation_commit_id=version.commit_id;
            let prefix=scope(&version.active_generation,&row.source_id,&row.symbol,60);let end=prefix_end(&prefix)?;let facts=tx.open_table(FACTS)?;
            if let Some((_,first))=facts.range(prefix.as_slice()..end.as_slice())?.next().transpose()? {stats.revision_start_ns=StoredBar::decode(first.value())?.row.open_time_ns;stats.revision_end_ns=stats.append_watermark_ns;}
            let bytes=serde_json::to_vec(&stats)?;metadata.insert(stats_key.as_slice(),bytes.as_slice())?;
        }
        affected.insert((row.source_id,row.symbol,60));
    }Ok(affected)
}
fn resolve_calendar(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str,cutoff:i64,mut schedule:Option<crate::periods::MarketSchedule>)->Result<Option<crate::periods::MarketSchedule>> {
    use crate::periods::{CalendarAuthority,MarketSchedule};
    let metadata=tx.open_table(METADATA)?;let key=named_key(&version.active_generation,"source_calendar",&format!("{source}:{symbol}"));
    let stored=metadata.get(key.as_slice())?.map(|v|serde_json::from_slice::<CalendarAuthority>(v.value())).transpose()?;
    if stored.is_some() && schedule.is_none(){schedule=Some(MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:None,reference:Some("captured exact-date source intervals".into()),sessions:vec![],authority:Some(CalendarAuthority::default())});}
    if let Some(schedule)=schedule.as_mut() {
        if let Some(stored)=stored {let authority=schedule.authority.get_or_insert_with(Default::default);
            for day in stored.absolute_days {if let Some(old)=authority.absolute_days.iter_mut().find(|v|v.trade_date==day.trade_date){ensure!(old.market==day.market && old.code==day.code,"same-MVCC calendar exact scope conflict");*old=day;}else{authority.absolute_days.push(day);}}
        }
        if let Some(authority)=schedule.authority.as_mut(){authority.absolute_days.retain(|day|day.received_at_ns.max(day.accepted_at_ns.unwrap_or(i64::MIN))<=cutoff);authority.absolute_days.sort_by_key(|day|day.trade_date);}
        schedule.validate()?;
    }Ok(schedule)
}

struct CalendarAggregate {value:Option<RangeAggregate>,excluded:u64,earliest:Option<i64>,latest:Option<i64>,unverified:u64,unverified_earliest:Option<i64>,unverified_latest:Option<i64>}
fn calendar_query(tx:&ReadTransaction,generation:&str,source:&str,symbol:&str,start:i64,end:i64,schedule:Option<&crate::periods::MarketSchedule>)->Result<CalendarAggregate> {
    let mut result=CalendarAggregate {value:None,excluded:0,earliest:None,latest:None,unverified:0,unverified_earliest:None,unverified_latest:None};
    let known=crate::periods::calendar_known_ranges(chrono::DateTime::from_timestamp_nanos(start),chrono::DateTime::from_timestamp_nanos(end),schedule)?.into_iter().map(|(lo,hi)|Ok((lo.timestamp_nanos_opt().context("known calendar start outside ns")?,hi.timestamp_nanos_opt().context("known calendar end outside ns")?))).collect::<Result<Vec<_>>>()?;
    let mut cursor=start;
    let mut exclude=|lo:i64,hi:i64|->Result<()> {
        if lo>=hi{return Ok(());}
        let mut edges=vec![lo,hi];for (start,end) in &known {if lo<*start&&*start<hi{edges.push(*start);}if lo<*end&&*end<hi{edges.push(*end);}}edges.sort_unstable();edges.dedup();
        for edge in edges.windows(2){if let Some(value)=range_index::query(tx,generation,source,symbol,edge[0],edge[1],false)? {
            if known.iter().any(|(start,end)|*start<=edge[0]&&edge[1]<=*end){result.excluded=result.excluded.checked_add(value.total_count).context("calendar excluded count overflow")?;result.earliest=Some(result.earliest.map_or(value.first_open_time_ns,|old|old.min(value.first_open_time_ns)));result.latest=Some(result.latest.map_or(value.last_open_time_ns,|old|old.max(value.last_open_time_ns)));}
            else{result.unverified=result.unverified.checked_add(value.total_count).context("calendar unknown count overflow")?;result.unverified_earliest=Some(result.unverified_earliest.map_or(value.first_open_time_ns,|old|old.min(value.first_open_time_ns)));result.unverified_latest=Some(result.unverified_latest.map_or(value.last_open_time_ns,|old|old.max(value.last_open_time_ns)));}
        }}Ok(())
    };
    for (lo,hi) in crate::periods::session_ranges(chrono::DateTime::from_timestamp_nanos(start),chrono::DateTime::from_timestamp_nanos(end),schedule)? {
        let lo=lo.timestamp_nanos_opt().context("calendar start outside signed ns")?;let hi=hi.timestamp_nanos_opt().context("calendar end outside signed ns")?;
        exclude(cursor,lo)?;cursor=hi;
        if let Some(value)=range_index::query(tx,generation,source,symbol,lo,hi,false)? {if let Some(current)=result.value.as_mut(){current.append(value)?;}else{result.value=Some(value);}}
    }
    exclude(cursor,end)?;Ok(result)
}
fn source_confirmation_unknown(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str)->Result<bool>{
    let key=named_key(&version.active_generation,"finalization_unknown",&format!("{source}:{symbol}"));
    Ok(tx.open_table(METADATA)?.get(key.as_slice())?.map(|v|serde_json::from_slice::<bool>(v.value())).transpose()?.unwrap_or(false))
}
fn aggregate_row(value:&RangeAggregate,first:&ImportBarRow,version:&SnapshotVersion,period:crate::periods::Period,open:i64,end:i64,complete_interval:bool,calendar_verified:bool,calendar_known_at:Option<i64>,source_clock_unknown:bool,as_of:i64)->Result<ImportBarRow> {
    let mut source=first.source_metadata.clone();if !source.is_object(){source=json!({});}
    source["provider"]=json!(first.realtime_source_id);source["observed_at"]=json!(stamp(value.source_observed_at_ns));source["received_at"]=json!(stamp(value.received_at_ns));
    source["raw_payload"]=json!({"derivation":"backend_period_projection","period_id":period.as_str(),"bucket_end":stamp(end),"bucket_first_open_time":stamp(value.first_open_time_ns),"component_count":value.total_count.to_string(),"known_volume_count":value.known_volume_count.to_string(),"known_volume_sum":value.known_volume_sum.to_string(),"coverage_contiguous":value.contiguous,"aggregation_version":version.aggregation_version,"calendar_projection_version":"date-authority-session-membership-v3","calendar_bucket_verified":calendar_verified,"bucket_elapsed":complete_interval,"capture_epoch":value.capture_epoch,"capture_sequence":value.applied_frame_sequence.map(|v|v.to_string()),"capture_accepted_at_ns":value.accepted_at_ns.map(|v|v.to_string()),"applied_commit_id":value.last_commit_id.to_string()});
    let availability=value.finalized_at_ns.into_iter().chain(calendar_known_at).max().map(|finalized|end.max(finalized).max(value.received_at_ns).max(value.accepted_at_ns.unwrap_or(i64::MIN)));
    let finalization_unknown=source_clock_unknown || value.final_count<value.total_count || value.finalized_at_ns.is_none();
    let state=if calendar_verified && calendar_known_at.is_none_or(|at|at<=as_of) && complete_interval && value.received_at_ns<=as_of && value.accepted_at_ns.is_none_or(|at|at<=as_of) && availability.is_none_or(|at|at<=as_of) {value.state()}else{"provisional"};
    source["raw_payload"]["calendar_evidence_known_at_ns"]=json!(calendar_known_at.map(|at|at.to_string()));
    source["raw_payload"]["calendar_evidence_clock_policy"]=json!("captured-required-date-received-accepted-max-v1; static policy clock remains unknown");
    source["raw_payload"]["finalization_time_unknown"]=json!(finalization_unknown);
    source["raw_payload"]["component_confirmation_coverage"]=json!(if source_clock_unknown{"scope contains unknown legacy confirmation clocks; per-bucket completeness is not certified"}else{"native fact confirmation clocks follow each component state"});
    source["raw_payload"]["component_finalized_at_ns"]=json!(value.finalized_at_ns.map(|v|v.to_string()));
    source["raw_payload"]["derived_availability_lower_bound_ns"]=json!(availability.map(|v|v.to_string()));
    source["raw_payload"]["finalization_clock_policy"]=json!("derived-availability-max-required-calendar-and-component-clocks-v2");
    source["raw_payload"]["source_publication_time_unknown"]=json!(true);
    source["raw_payload"]["accepted_clock_known_component_count"]=json!(value.accepted_known_count.to_string());
    source["raw_payload"]["accepted_clock_all_components_known"]=json!(value.accepted_known_count==value.total_count);
    crate::source_volume::write(&mut source["raw_payload"],&value.source_volume_components)?;
    Ok(ImportBarRow {instrument_symbol:first.instrument_symbol.clone(),realtime_source_id:first.realtime_source_id.clone(),evidence_channel_id:first.evidence_channel_id.clone(),interval_seconds:((end as i128-open as i128)/1_000_000_000).try_into()?,open_time_ns:open,close_time_ns:end,open:value.open.to_string(),high:value.high.to_string(),low:value.low.to_string(),close:value.close.to_string(),volume:value.volume().map(|v|v.to_string()),revision:value.last_commit_id,received_sequence:value.received_sequence,state:state.into(),finalized_at_ns:if state=="final"{availability}else{None},source_observed_at_ns:value.source_observed_at_ns,received_at_ns:value.received_at_ns,source_metadata:source,evidence:json!({"aggregate_digest":value.digest,"finalization_time_unknown":finalization_unknown,"finalization_clock_policy":"derived-availability-max-required-calendar-and-component-clocks-v2","component_finalized_at_ns":value.finalized_at_ns.map(|v|v.to_string())})})
}

fn apply_bars(tx:&WriteTransaction,version:&SnapshotVersion,rows:Vec<ImportBarRow>,capture:Option<CapturePosition>)->Result<Counts> {
    let mut dirty=DirtyLeaves::new();let counts=apply_bars_deferred(tx,version,rows,capture,&mut dirty)?;
    range_index::recompute(tx,&version.active_generation,dirty)?;Ok(counts)
}
fn apply_bars_deferred(tx:&WriteTransaction,version:&SnapshotVersion,rows:Vec<ImportBarRow>,capture:Option<CapturePosition>,dirty:&mut DirtyLeaves)->Result<Counts> {
    ensure!(version.aggregation_version==AGGREGATION_VERSION,"outdated derived index; rebuild inactive generation before adding facts");
    let mut counts=Counts::default();
    let mut changes=std::collections::BTreeMap::<(String,String,u32),(bool,i64,i64)>::new();
    let mut point_summaries=std::collections::BTreeMap::<(String,String),Value>::new();
    {
        let mut facts=tx.open_table(FACTS)?;
        for mut row in rows {
            if let Some(summary)=row.source_metadata["raw_payload"].get("source_point_quarantine_ref") {
                let key=(row.realtime_source_id.clone(),row.instrument_symbol.clone());
                let target=point_summaries.entry(key).or_insert(Value::Null);
                merge_point_coverage(target,summary)?;
            }
            if row.state=="final" && row.finalized_at_ns.is_none() {let key=named_key(&version.active_generation,"finalization_unknown",&format!("{}:{}",row.realtime_source_id,row.instrument_symbol));tx.open_table(METADATA)?.insert(key.as_slice(),b"true".as_slice())?;}
            let key=fact_key(&version.active_generation,&row);let existing=facts.get(key.as_slice())?.map(|v|StoredBar::decode(v.value())).transpose()?;
            if let Some(previous)=existing {
                if serde_json::to_vec(&row)?==serde_json::to_vec(&previous.row)? {counts.unchanged+=1;continue;}
                if capture.is_some() {
                    // Connection-local source sequences and a trimmed hot reducer cannot rank persistent authority.
                    // A newer durable application receives a canonical revision from the persistent previous fact.
                    ensure!(!(previous.row.state=="final" && matches!(row.state.as_str(),"forming"|"provisional_quote")),"quote-derived draft cannot replace a final authority");
                    let reported=row.revision;row.revision=previous.row.revision.checked_add(1).context("canonical bar revision exhausted; frame retained for explicit recovery")?.max(reported);
                    if !row.evidence.is_object() {row.evidence=json!({"original_evidence":row.evidence});}
                    row.evidence.as_object_mut().expect("wrapped evidence").insert("reported_projector_revision".into(),json!(reported.to_string()));
                } else {
                    let order=row.revision.cmp(&previous.row.revision);
                    if order.is_lt() {counts.rejected+=1;continue;}
                    ensure!(!order.is_eq(),"conflicting imported bar contents at identical legacy revision");
                }
            }
            let series=(row.realtime_source_id.clone(),row.instrument_symbol.clone(),row.interval_seconds);
            let stats_key=named_key(&version.active_generation,"series_version",&series_identity(&series.0,&series.1,series.2));
            let prior=tx.open_table(METADATA)?.get(stats_key.as_slice())?.map(|v|serde_json::from_slice::<SeriesVersion>(v.value())).transpose()?;
            let is_correction=prior.as_ref().is_some_and(|v|row.open_time_ns<=v.append_watermark_ns);
            changes.entry(series).and_modify(|v| {v.0|=is_correction;v.1=v.1.min(row.open_time_ns);v.2=v.2.max(row.open_time_ns);}).or_insert((is_correction,row.open_time_ns,row.open_time_ns));
            range_index::dirty(&row,dirty);let fact=StoredBar {row,commit_id:version.commit_id,capture:capture.clone()};let bytes=fact.encode()?;facts.insert(key.as_slice(),bytes.as_slice())?;counts.accepted+=1;
        }
    }
    let mut metadata=tx.open_table(METADATA)?;
    for ((source,symbol),summary) in point_summaries {
        let key=named_key(&version.active_generation,"source_points",&format!("{source}:{symbol}"));
        let mut current=metadata.get(key.as_slice())?.map(|v|serde_json::from_slice::<Value>(v.value())).transpose()?.unwrap_or(Value::Null);
        merge_point_coverage(&mut current,&summary)?;
        let bytes=serde_json::to_vec(&current)?;metadata.insert(key.as_slice(),bytes.as_slice())?;
    }
    for ((source,symbol,interval),(correction,first,last)) in changes {
        let key=named_key(&version.active_generation,"series_version",&series_identity(&source,&symbol,interval));
        let old=metadata.get(key.as_slice())?.map(|v|serde_json::from_slice::<SeriesVersion>(v.value())).transpose()?;
        let previous_watermark=old.as_ref().map(|v|v.append_watermark_ns);
        let previous_bounds=old.as_ref().filter(|v|v.last_mutation_commit_id==version.commit_id).map(|v|(v.revision_start_ns,v.revision_end_ns));
        let mut stats=old.unwrap_or_else(||SeriesVersion {series_generation:uuid::Uuid::new_v4().to_string(),correction_epoch:0,append_watermark_ns:last,last_mutation_commit_id:0,revision_start_ns:first,revision_end_ns:last,previous_append_watermark_ns:None});
        if correction {stats.correction_epoch=stats.correction_epoch.checked_add(1).context("series correction epoch exhausted")?;}
        stats.previous_append_watermark_ns=previous_watermark;stats.append_watermark_ns=stats.append_watermark_ns.max(last);stats.last_mutation_commit_id=version.commit_id;stats.revision_start_ns=previous_bounds.map_or(first,|v|v.0.min(first));stats.revision_end_ns=previous_bounds.map_or(last,|v|v.1.max(last));
        let bytes=serde_json::to_vec(&stats)?;metadata.insert(key.as_slice(),bytes.as_slice())?;
    }Ok(counts)
}
/// Frame summaries may overlap. Report the maximum proved frame count as a
/// lower bound, never sum repeated annual files as newly observed source events.
fn merge_point_coverage(target:&mut Value,summary:&Value)->Result<()> {
    let number=|key:&str|summary[key].as_str().and_then(|v|v.parse::<u64>().ok()).or_else(||summary[key].as_u64());
    let count=number("count").or_else(||number("known_unclassified_point_count_lower_bound")).context("source point count missing")?;
    ensure!(count<=100_000,"source point summary exceeds bounded frame count");
    let clock=|iso:&str,ms:&str,ns:&str|->Result<Option<i64>> {
        if let Some(value)=summary[ns].as_str(){return Ok(Some(value.parse()?));}
        if let Some(value)=summary[ms].as_str(){return Ok(Some(value.parse::<i64>()?.checked_mul(1_000_000).context("source point ms overflow")?));}
        summary[iso].as_str().map(|v|v.parse::<chrono::DateTime<chrono::Utc>>().context("source point clock")?.timestamp_nanos_opt().context("source point ns overflow")).transpose()
    };
    let first=clock("first_source_label","first_source_label_ms","first_source_label_ns")?;
    let last=clock("last_source_label","last_source_label_ms","last_source_label_ns")?;
    let old_number=|key:&str|target[key].as_str().and_then(|v|v.parse::<u64>().ok());
    let old_clock=|key:&str|target[key].as_str().and_then(|v|v.parse::<i64>().ok());
    let lower=count.max(old_number("known_unclassified_point_count_lower_bound").unwrap_or(0));
    let first=first.into_iter().chain(old_clock("first_source_label_ns")).min();
    let last=last.into_iter().chain(old_clock("last_source_label_ns")).max();
    let mut references=target["frame_summary_sha256_samples"].as_array().cloned().unwrap_or_default();
    for hash in summary["frame_summary_sha256_samples"].as_array().into_iter().flatten(){if !references.contains(hash){references.push(hash.clone());}}
    if let Some(hash)=summary["points_sha256"].as_str(){ensure!(hash.len()==64 && hash.bytes().all(|b|b.is_ascii_hexdigit()),"invalid source point digest");if !references.contains(&json!(hash)){references.push(json!(hash));}}
    references.sort_by(|a,b|a.as_str().cmp(&b.as_str()));references.truncate(8);
    *target=json!({"known_unclassified_point_count_lower_bound":lower.to_string(),"count_semantics":"maximum unique points in a witnessed frame; overlapping frames are not summed; exact scope ledger remains in migration/raw evidence",
        "first_source_label_ns":first.map(|v|v.to_string()),"last_source_label_ns":last.map(|v|v.to_string()),"frame_summary_sha256_samples":references,
        "source_event_coverage_complete":false,"canonical_minute_values_exact":true,"reason":"unclassified source points remain outside canonical minutes; their quantity is neither zero nor folded without independent policy"});Ok(())
}
#[derive(Clone,Debug,serde::Serialize,serde::Deserialize)]
pub struct SeriesVersion {
    pub series_generation:String,
    #[serde(with="u64_string")] pub correction_epoch:u64,
    #[serde(with="i64_string")] pub append_watermark_ns:i64,
    #[serde(with="u64_string")] pub last_mutation_commit_id:u64,
    #[serde(with="i64_string")] pub revision_start_ns:i64,
    #[serde(with="i64_string")] pub revision_end_ns:i64,
    #[serde(default,with="optional_i64_string")]pub previous_append_watermark_ns:Option<i64>,
}
#[derive(Clone,Debug)]
pub struct ScanResume {pub after_ns:i64,pub series_generation:String,pub correction_epoch:u64,pub append_watermark_ns:Option<i64>}
#[derive(Clone,Debug)]
pub struct CanonicalBarKey {pub source_id:String,pub symbol:String,pub interval_seconds:u32,pub open_time_ns:i64}
#[derive(Clone,Debug)]
pub struct CanonicalQuoteKey {pub source_id:String,pub symbol:String,pub event_id:String}
fn series_identity(source:&str,symbol:&str,interval:u32)->String {serde_json::to_string(&(source,symbol,interval)).expect("string tuple")}
fn quote_identity_body(row:&ImportQuoteRow)->Result<Vec<u8>> {
    let mut row=row.clone();
    if let Some(raw)=row.source_metadata.get_mut("raw_payload").and_then(Value::as_object_mut) {
        // Local application provenance may change when the identical upstream
        // event is redelivered. It is not a new event or a source-content conflict.
        for key in ["capture_epoch","capture_sequence","capture_digest","capture_accepted_at_ns","applied_commit_id","statistics_evidence"] {raw.remove(key);}
    }
    Ok(serde_json::to_vec(&row)?)
}
fn apply_quotes(tx:&WriteTransaction,version:&SnapshotVersion,rows:Vec<ImportQuoteRow>,capture:Option<CapturePosition>)->Result<Counts> {
    let mut counts=Counts::default();let mut latest=tx.open_table(QUOTES)?;let mut events=tx.open_table(EVENTS)?;let mut identities=tx.open_table(EVENT_IDENTITIES)?;
    let mut global=tx.open_table(GLOBAL)?;let mut ordinal=global.get("event_ordinal")?.context("event ordinal counter")?.value().parse::<u64>()?;
    for mut row in rows {
        let key=quote_key(&version.active_generation,&row);let previous=latest.get(key.as_slice())?.map(|v|StoredQuote::decode(v.value())).transpose()?;
        let mut price_capture=capture.clone();
        if row.is_supplement {
            let Some(previous)=previous.as_ref() else {counts.rejected+=1;continue;};
            ensure!(row.event_id==previous.row.event_id && row.observed_at_ns==previous.row.observed_at_ns && row.received_at_ns==previous.row.received_at_ns && row.price==previous.row.price,"supplement cannot change price event identity or clocks");
            let supplement_metadata=row.source_metadata.clone();
            row.source_metadata=previous.row.source_metadata.clone();
            if !row.source_metadata.is_object(){row.source_metadata=json!({"original_metadata":row.source_metadata});}
            if !row.source_metadata["raw_payload"].is_object(){row.source_metadata["raw_payload"]=json!({});}
            row.source_metadata["raw_payload"]["statistics_evidence"]=json!({"applied_capture":capture,"source_metadata":supplement_metadata});
            price_capture=previous.capture.clone();
        }
        let bytes=StoredQuote {row:row.clone(),commit_id:version.commit_id,capture:price_capture}.encode()?;
        if !row.is_supplement {
            let identity=event_identity(&version.active_generation,&row);
            if let Some(old)=identities.get(identity.as_slice())? {let event=event_key(&version.active_generation,&row,old.value());let old=StoredQuote::decode(events.get(event.as_slice())?.context("event identity index missing fact")?.value())?;ensure!(quote_identity_body(&old.row)?==quote_identity_body(&row)?,"quote event identity conflict");counts.unchanged+=1;continue;}
            ordinal=ordinal.checked_add(1).context("event order counter exhausted")?;let event=event_key(&version.active_generation,&row,ordinal);identities.insert(identity.as_slice(),ordinal)?;
            events.insert(event.as_slice(),bytes.as_slice())?;
        }
        // This transaction is in durable application order. Local receive clocks and connection-local sequences cannot rank equal source times.
        let is_newer=previous.as_ref().is_none_or(|old|row.observed_at_ns>=old.row.observed_at_ns);
        if is_newer {latest.insert(key.as_slice(),bytes.as_slice())?;}counts.accepted+=1;
    }global.insert("event_ordinal",ordinal.to_string().as_str())?;Ok(counts)
}
fn select_rows(tx:&ReadTransaction,version:&SnapshotVersion,symbol:&str,source:&str,interval:u32,selection:&BarSelection,final_only:bool)->Result<Vec<StoredBar>> {
    let prefix=scope(&version.active_generation,source,symbol,interval);let mut lo=prefix.clone();let mut hi=prefix_end(&prefix)?;
    let (limit,reverse,strict)=match *selection {
        BarSelection::Latest {count} => (count,true,false),BarSelection::Before {before_ns,count} => {hi=prefix.clone();hi.extend(signed_key(before_ns));(count,true,false)},
        BarSelection::Range {start_ns,end_ns,max_rows} => {ensure!(start_ns<=end_ns,"inverted snapshot range");lo.extend(signed_key(start_ns));hi=prefix;hi.extend(signed_key(end_ns));(max_rows,false,true)}
    };
    ensure!(limit>0 && limit<=MAX_PAGE_ROWS,"snapshot row bound outside 1..10000");let facts=tx.open_table(FACTS)?;let mut range=facts.range(lo.as_slice()..hi.as_slice())?;let mut out=Vec::with_capacity(limit);
    loop {
        let record=if reverse {range.next_back()}else{range.next()};let Some(record)=record else {break;};let (_,bytes)=record?;let fact=StoredBar::decode(bytes.value())?;
        if final_only && fact.row.state!="final" {continue;}
        if out.len()==limit {ensure!(!strict,"snapshot range exceeds max_rows; use canonical_scan for a complete same-MVCC range");break;}
        out.push(fact);
    }
    if reverse {out.reverse();}Ok(out)
}
fn external_scope_kind(scope:&ExternalFactScope,kind:&str)->Result<String>{Ok(hex::encode(Sha256::digest(serde_json::to_vec(&(scope,kind))?)))}
fn external_prefix(generation:&str,scope:&ExternalFactScope,kind:&str,source:&str)->Result<Vec<u8>> {let mut prefix=named_key(generation,"external",&external_scope_kind(scope,kind)?);component(&mut prefix,source);Ok(prefix)}
fn external_known_at(row:&ExternalFactRecord)->Option<i64>{Some(row.received_at_ns?.max(row.observed_at_ns.unwrap_or(i64::MIN)).max(row.published_at_ns.unwrap_or(i64::MIN)))}
fn select_external(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str,cutoff:i64)->Result<Vec<ExternalFactRecord>> {
    let facts=tx.open_table(EXTERNAL)?;let metadata=tx.open_table(METADATA)?;let mut selected=std::collections::BTreeMap::<(String,String),ExternalFactRecord>::new();
    for market_source_id in [None,Some(source.to_owned())] {let scope=ExternalFactScope {instrument_symbol:symbol.into(),market_source_id};
        for kind in ["multi_timeframe","volatility","positioning"] {
            let key=named_key(&version.active_generation,"external_providers",&external_scope_kind(&scope,kind)?);
            let providers=metadata.get(key.as_slice())?.map(|v|serde_json::from_slice::<std::collections::BTreeSet<String>>(v.value())).transpose()?.unwrap_or_default();ensure!(providers.len()<=64,"external provider catalog exceeds read bound");
            for provider in providers {let prefix=external_prefix(&version.active_generation,&scope,kind,&provider)?;let end=if let Some(after)=cutoff.checked_add(1){let mut hi=prefix.clone();hi.extend(signed_key(after));hi}else{prefix_end(&prefix)?};
                if let Some((_,bytes))=facts.range(prefix.as_slice()..end.as_slice())?.next_back().transpose()? {let row:ExternalFactRecord=serde_json::from_slice(bytes.value())?;
                    let key=(kind.to_owned(),provider);let replace=selected.get(&key).is_none_or(|old|external_known_at(&row)>=external_known_at(old));if replace{selected.insert(key,row);}
                }
            }
        }
    }Ok(selected.into_values().collect())
}
fn scan_context_schedule(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str,interval:u32,cutoff:i64,schedule:Option<&crate::periods::MarketSchedule>)->Result<CanonicalScanContext> {
    scan_context_schedule_mode(tx,version,source,symbol,interval,cutoff,schedule,true,None)
}
fn scan_context_schedule_mode(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str,interval:u32,cutoff:i64,schedule:Option<&crate::periods::MarketSchedule>,analysis_context:bool,timings:Option<&mut CanonicalPageTimings>)->Result<CanonicalScanContext> {
    let quotes=tx.open_table(QUOTES)?;
    let mut channel=if source=="jin10_client" {"jin10_web"}else{source};
    let key=quote_scope(&version.active_generation,channel,symbol);let mut quote=quotes.get(key.as_slice())?.map(|v|StoredQuote::decode(v.value()).and_then(|r|quote_value(&r))).transpose()?;
    if quote.is_none() && source=="jin10_client" {
        let key=quote_scope(&version.active_generation,source,symbol);
        quote=quotes.get(key.as_slice())?.map(|v|StoredQuote::decode(v.value()).and_then(|r|quote_value(&r))).transpose()?;channel=source;
    }
    if source=="jin10_client" {if let Some(value)=quote.as_mut() {
        value["logical_source_id"]=json!(source);value["price_evidence_channel_id"]=json!(channel);
        let key=quote_scope(&version.active_generation,"jin10_local",symbol);
        if let Some(bytes)=quotes.get(key.as_slice())? {let supplement=quote_value(&StoredQuote::decode(bytes.value())?)?;
            for field in ["open","high","low","volume"] {value[field]=supplement[field].clone();}
            value["statistics_evidence"]=json!({"channel":"jin10_local","observed_at_ns":supplement["observed_at_ns"],"received_at_ns":supplement["received_at_ns"],"applied_capture":supplement["applied_capture"]});
        }
    }}
    let metadata=tx.open_table(METADATA)?;
    let value=|namespace:&str,key:&str|->Result<Value> {let key=named_key(&version.active_generation,namespace,key);Ok(metadata.get(key.as_slice())?.map(|v|serde_json::from_slice(v.value())).transpose()?.unwrap_or(Value::Null))};
    let prefix=scope(&version.active_generation,source,symbol,interval);let last=prefix_end(&prefix)?;let facts=tx.open_table(FACTS)?;let mut range=facts.range(prefix.as_slice()..last.as_slice())?;
    let first=range.next().transpose()?.map(|(_,v)|StoredBar::decode(v.value())).transpose()?;let last=range.next_back().transpose()?.map(|(_,v)|StoredBar::decode(v.value())).transpose()?;
    let mut coverage=json!({"first_open_time_ns":first.as_ref().map(|v|v.row.open_time_ns.to_string()),"last_open_time_ns":last.as_ref().or(first.as_ref()).map(|v|v.row.open_time_ns.to_string()),"history":value("coverage",&format!("{source}:{symbol}"))?,"warmup_complete":false,"series_version":value("series_version",&series_identity(source,symbol,interval))?,"projection_start_boundary":value("migration","projection_start_boundary")?});
    let points=value("source_points",&format!("{source}:{symbol}"))?;
    if !points.is_null(){coverage["source_point_coverage"]=points.clone();coverage["source_event_coverage_complete"]=json!(false);}
    let mut clock=value("source_clock",&format!("{source}:{symbol}"))?;
    if clock.is_null() && source=="tonghuashun_futures" && ["AU2610","AU8888","AG2706","AG8888","IXIC","BRN0Y","USDIND","000001.SH"].contains(&symbol){clock=json!({"verified":false,"reason":"legacy_generation_source_interval_label_semantics_unverified","policy":"legacy-v6-label-as-open-v1"});}
    if !clock.is_null(){coverage["source_clock"]=clock.clone();}
    let mut capabilities=value("capabilities",&format!("{source}:{symbol}"))?;
    if value("finalization_unknown",&format!("{source}:{symbol}"))?==true {if !capabilities.is_array(){capabilities=json!([]);}if let Some(items)=capabilities.as_array_mut(){items.push(json!("finalization_time_unknown"));}}
    if clock["verified"]==false{if !capabilities.is_array(){capabilities=json!([]);}capabilities.as_array_mut().unwrap().push(json!("source_clock_unverified"));}
    let mut external_facts=if analysis_context{select_external(tx,version,source,symbol,cutoff)?}else{vec![]};
    if analysis_context && clock["verified"]==false{external_facts.push(ExternalFactRecord{scope:ExternalFactScope{instrument_symbol:symbol.into(),market_source_id:Some(source.into())},kind:"source_clock_coverage".into(),source:source.into(),record_id:hex::encode(Sha256::digest(serde_json::to_vec(&clock)?)),revision:version.commit_id.max(1),observed_at_ns:None,published_at_ns:None,received_at_ns:None,value:clock,unavailable_reason:Some("source_interval_label_semantics_unverified".into()),provenance:json!({"snapshot_version":version,"original_archive_unchanged":true})});}
    if analysis_context && !points.is_null(){
        external_facts.push(ExternalFactRecord{scope:ExternalFactScope{instrument_symbol:symbol.into(),market_source_id:Some(source.into())},kind:"source_point_coverage".into(),source:source.into(),record_id:hex::encode(Sha256::digest(serde_json::to_vec(&points)?)),revision:version.commit_id.max(1),observed_at_ns:None,published_at_ns:None,received_at_ns:None,value:points,unavailable_reason:Some("source_event_coverage_incomplete".into()),provenance:json!({"snapshot_version":version,"clock_policy":"source points retained independently of normalized minutes","original_point_body":"capture/archive","count_is_lower_bound":true})});
    }
    if let Some(schedule)=schedule {
        let prefix=scope(&version.active_generation,source,symbol,60);let hi=prefix_end(&prefix)?;let mut minutes=facts.range(prefix.as_slice()..hi.as_slice())?;
        let first=minutes.next().transpose()?.map(|(_,v)|StoredBar::decode(v.value())).transpose()?;
        let last=minutes.next_back().transpose()?.map(|(_,v)|StoredBar::decode(v.value())).transpose()?;
        let bounds=first.as_ref().zip(last.as_ref().or(first.as_ref())).and_then(|(first,last)|last.row.open_time_ns.checked_add(range_index::MINUTE_NS).map(|end|(first.row.open_time_ns,end.min(cutoff)))).filter(|(lo,hi)|lo<hi);
        let coverage_started=std::time::Instant::now();let stats=if let Some((lo,hi))=bounds {calendar_query(tx,&version.active_generation,source,symbol,lo,hi,Some(schedule))?}else{CalendarAggregate{value:None,excluded:0,earliest:None,latest:None,unverified:0,unverified_earliest:None,unverified_latest:None}};
        if let Some(timings)=timings{timings.calendar_coverage_ms=coverage_started.elapsed().as_secs_f64()*1000.0;}
        let calendar=json!({"projection_version":"date-authority-session-membership-v3","excluded_outside_schedule":stats.excluded.to_string(),"unverified_calendar_minutes":stats.unverified.to_string(),"unverified_earliest_ns":stats.unverified_earliest.map(|v|v.to_string()),"unverified_latest_ns":stats.unverified_latest.map(|v|v.to_string()),"calendar_authority":schedule.authority,"calendar_revision_semantics":"final_revision_history stores the latest captured revision per exact trading date; an unavailable earlier revision stays unknown. Original replay resolves calendar from its own capture prefix","earliest_ns":stats.earliest.map(|v|v.to_string()),"latest_ns":stats.latest.map(|v|v.to_string()),"schedule_version":crate::periods::schedule_version(Some(schedule))?,"scope_start_ns":bounds.map(|v|v.0.to_string()),"scope_end_ns":bounds.map(|v|v.1.to_string()),"complete":stats.excluded==0 && stats.unverified==0,"reason":if stats.unverified>0{Some("calendar dates or years are not covered by captured/verified authority; raw and base minutes are retained without inferred recurring membership")}else if stats.excluded>0{Some("stored source minutes have no membership in the declared schedule; raw and base facts are retained, derived coverage is incomplete")}else{None}});
        coverage["calendar_projection"]=calendar.clone();
        if analysis_context {
        let provenance=json!({"snapshot_version":version,"schedule_version":crate::periods::schedule_version(Some(schedule))?,"fact_policy":"retain raw and canonical minute facts; exclude only from all derived periods"});
        external_facts.push(ExternalFactRecord{scope:ExternalFactScope{instrument_symbol:symbol.into(),market_source_id:Some(source.into())},kind:"calendar_coverage".into(),source:source.into(),record_id:hex::encode(Sha256::digest(serde_json::to_vec(&(&calendar,&provenance))?)),revision:version.commit_id.max(1),observed_at_ns:None,published_at_ns:None,received_at_ns:None,value:calendar,unavailable_reason:(stats.excluded>0 || stats.unverified>0).then(||"derived_calendar_coverage_incomplete".into()),provenance});
        external_facts.push(derive_multi_timeframe(tx,version,source,symbol,cutoff,schedule)?);
        }
    }
    Ok(CanonicalScanContext {quote,capabilities,coverage,semantics:"final_revision_history".into(),external_facts})
}
fn stamp(ns:i64)->String {crate::domain::isoformat(chrono::DateTime::from_timestamp_nanos(ns))}
fn derive_multi_timeframe(tx:&ReadTransaction,version:&SnapshotVersion,source:&str,symbol:&str,cutoff:i64,schedule:&crate::periods::MarketSchedule)->Result<ExternalFactRecord> {
    use crate::periods::{Period,bucket_for};
    require_current_index(version)?;let facts=tx.open_table(FACTS)?;let prefix=scope(&version.active_generation,source,symbol,60);
    let mut horizons=Vec::new();let mut known=None::<i64>;let mut observed=None::<i64>;let mut nonempty=false;
    for period in [Period::H1,Period::D1,Period::W1] {
        let mut cursor=cutoff;let mut bars=Vec::new();let mut examined=0usize;
        while bars.len()<20 && examined<512 {
            examined+=1;let mut hi=prefix.clone();hi.extend(signed_key(cursor));
            let Some((_,bytes))=facts.range(prefix.as_slice()..hi.as_slice())?.next_back().transpose()? else{break};
            let last=StoredBar::decode(bytes.value())?;
            if !crate::periods::belongs_to_schedule(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),Some(schedule))? {
                let (previous,_)=crate::periods::session_neighbors(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),schedule)?;
                let Some(previous)=previous else{break;};cursor=previous.timestamp_nanos_opt().context("context calendar gap outside signed ns")?;continue;
            }
            let bucket=bucket_for(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,Some(schedule))?;
            let lo=bucket.input_start().timestamp_nanos_opt().context("context bucket start outside signed ns")?;let end=bucket.input_end().timestamp_nanos_opt().context("context bucket end outside signed ns")?;
            ensure!(lo<cursor,"multi-timeframe context did not advance");cursor=lo;
            if end>cutoff || !crate::periods::calendar_bucket_verified(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,Some(schedule))? {continue;}
            let Some(aggregate)=calendar_query(tx,&version.active_generation,source,symbol,lo,end,Some(schedule))?.value else{continue};
            let calendar_known=crate::periods::calendar_evidence_known_at(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,Some(schedule))?.and_then(|at|at.timestamp_nanos_opt());
            let available=aggregate.received_at_ns.max(aggregate.accepted_at_ns.unwrap_or(i64::MIN)).max(aggregate.finalized_at_ns.unwrap_or(i64::MIN)).max(calendar_known.unwrap_or(i64::MIN));
            if aggregate.state()!="final" || available>cutoff || aggregate.source_observed_at_ns>cutoff {continue;}
            let display=bucket.start.timestamp_nanos_opt().context("context display outside signed ns")?;
            let row=aggregate_row(&aggregate,&last.row,version,period,display,end,true,true,crate::periods::calendar_evidence_known_at(chrono::DateTime::from_timestamp_nanos(last.row.open_time_ns),period,Some(schedule))?.and_then(|at|at.timestamp_nanos_opt()),source_confirmation_unknown(tx,version,source,symbol)?,cutoff)?;
            known=Some(known.map_or(available,|v|v.max(available)));observed=Some(observed.map_or(aggregate.source_observed_at_ns,|v|v.max(aggregate.source_observed_at_ns)));nonempty=true;
            bars.push(serde_json::to_value(row)?);
        }
        bars.reverse();horizons.push(json!({"period_id":period.as_str(),"closed_only":true,"bars":bars,"history_search_bounded":examined>=512,"unavailable_reason":if bars.is_empty(){Some("no elapsed fully confirmed bucket available at decision cutoff")}else{None}}));
    }
    let value=json!({"schema":"canonical-multi-timeframe-v1","horizons":horizons,"semantics":"final_revision_history","decision_cutoff_ns":cutoff.to_string()});
    let provenance=json!({"derivation":"same_mvcc_minute_range_index","snapshot_version":version,"market_source_id":source,"instrument_symbol":symbol,"schedule_version":crate::periods::schedule_version(Some(schedule))?,"aggregation_version":version.aggregation_version,"decimal_policy":"exact finite OHLC and known-volume summaries; no float conversion","finalization_time_unknown":"preserved per bucket; legacy final-history semantics do not assert an original confirmation clock"});
    let record_id=hex::encode(Sha256::digest(serde_json::to_vec(&(&value,&provenance))?));
    Ok(ExternalFactRecord {scope:ExternalFactScope {instrument_symbol:symbol.into(),market_source_id:Some(source.into())},kind:"multi_timeframe".into(),source:source.into(),record_id,revision:version.commit_id.max(1),observed_at_ns:observed,published_at_ns:None,received_at_ns:known,value,unavailable_reason:(!nonempty).then(||"no closed canonical horizon is available in this MVCC view".into()),provenance})
}
fn projected_source(mut source:Value,capture:&Option<CapturePosition>,commit:u64)->Value {
    if !source.is_object() {source=json!({"original_metadata":source});}
    if !source["raw_payload"].is_object() {source["raw_payload"]=json!({"original_payload":source["raw_payload"]});}
    source["raw_payload"]["applied_commit_id"]=json!(commit.to_string());
    if let Some(capture)=capture {source["raw_payload"]["capture_epoch"]=json!(capture.epoch);source["raw_payload"]["capture_sequence"]=json!(capture.sequence.to_string());source["raw_payload"]["capture_digest"]=json!(capture.digest);}source
}
pub(crate) fn bar_value(fact:&StoredBar)->Result<Value> {
    let r=&fact.row;let mut value=serde_json::to_value(r)?;let object=value.as_object_mut().context("bar object")?;
    object.insert("open_time".into(),json!(stamp(r.open_time_ns)));object.insert("close_time".into(),json!(stamp(r.close_time_ns)));object.insert("interval".into(),json!(r.interval_seconds));
    object.insert("observed_at".into(),json!(stamp(r.source_observed_at_ns)));object.insert("received_at".into(),json!(stamp(r.received_at_ns)));object.insert("finalized_at".into(),json!(r.finalized_at_ns.map(stamp)));
    let mut source=projected_source(r.source_metadata.clone(),&fact.capture,fact.commit_id);
    if r.state=="final" && r.finalized_at_ns.is_none() {source["raw_payload"]["canonical_legacy_finality"]=r.evidence.clone();}
    source["observed_at"]=json!(stamp(r.source_observed_at_ns));source["received_at"]=json!(stamp(r.received_at_ns));if source["provider"].is_null() {source["provider"]=json!(r.realtime_source_id);}if source["provider_symbol"].is_null(){source["provider_symbol"]=json!(r.instrument_symbol);}
    object.insert("provider_symbol".into(),source["provider_symbol"].clone());object.insert("raw_payload".into(),source["raw_payload"].clone());object.insert("source".into(),source);
    object.insert("applied_commit_id".into(),json!(fact.commit_id.to_string()));object.insert("applied_capture".into(),serde_json::to_value(&fact.capture)?);Ok(value)
}
pub fn bar_to_value(row:ImportBarRow)->Result<Value> {
    let raw=&row.source_metadata["raw_payload"];
    let commit_id=raw["applied_commit_id"].as_str().and_then(|v|v.parse().ok()).unwrap_or(0);
    let capture=match(raw["capture_epoch"].as_str(),raw["capture_sequence"].as_str(),raw["capture_digest"].as_str()) {
        (Some(epoch),Some(sequence),Some(digest))=>Some(CapturePosition {epoch:epoch.into(),sequence:sequence.parse()?,digest:digest.into()}),_=>None,
    };
    bar_value(&StoredBar {row,commit_id,capture})
}
fn quote_value(fact:&StoredQuote)->Result<Value> {
    let r=&fact.row;let mut value=serde_json::to_value(r)?;let object=value.as_object_mut().context("quote object")?;
    let mut source=projected_source(r.source_metadata.clone(),&fact.capture,fact.commit_id);
    source["observed_at"]=json!(stamp(r.observed_at_ns));source["received_at"]=json!(stamp(r.received_at_ns));if source["provider"].is_null() {source["provider"]=json!(r.realtime_source_id);}if source["provider_symbol"].is_null(){source["provider_symbol"]=json!(r.instrument_symbol);}
    object.insert("last".into(),json!(r.price));object.insert("source_id".into(),json!(r.realtime_source_id));object.insert("source".into(),source.clone());
    object.insert("observed_at".into(),json!(stamp(r.observed_at_ns)));object.insert("received_at".into(),json!(stamp(r.received_at_ns)));object.insert("provider_symbol".into(),source["provider_symbol"].clone());object.insert("raw_payload".into(),source["raw_payload"].clone());
    if let Some(stats)=r.statistics.as_object() {for (key,value) in stats {object.insert(key.clone(),value.clone());}}
    object.insert("applied_commit_id".into(),json!(fact.commit_id.to_string()));object.insert("applied_capture".into(),serde_json::to_value(&fact.capture)?);Ok(value)
}
fn text(value:&Value)->Result<String> {match value {Value::String(v)=>Ok(v.clone()),Value::Number(v)=>Ok(v.to_string()),_=>bail!("missing exact decimal or identity")}}
fn ns(value:&Value)->Result<i64> {if let Some(text)=value.as_str() {if let Ok(v)=text.parse::<i64>() {return Ok(v);}return text.parse::<chrono::DateTime<chrono::Utc>>()?.timestamp_nanos_opt().context("timestamp outside signed nanoseconds");}value.as_i64().context("timestamp required")}
fn ordinal(value:&Value)->Result<u64> {if let Some(text)=value.as_str() {Ok(text.parse()?)}else{value.as_u64().context("unsigned sequence required")}}
#[cfg(test)]
#[path="native_store_tests.rs"]
mod tests;
pub fn bar_from_value(value:&Value)->Result<ImportBarRow> {
    if value.get("open_time_ns").is_some() {return serde_json::from_value(value.clone()).map_err(Into::into);}
    let source=&value["source"];let interval=value.get("interval_seconds").unwrap_or(&value["interval"]).as_u64().context("bar interval")?;
    let open_time=ns(&value["open_time"])?;let close_time=open_time.checked_add(i64::try_from(interval)?.checked_mul(1_000_000_000).context("interval overflow")?).context("close time overflow")?;
    Ok(ImportBarRow {instrument_symbol:text(&value["instrument"]["symbol"] )?,realtime_source_id:text(&source["provider"] )?,evidence_channel_id:text(&value["evidence_channel_id"] )?,interval_seconds:interval.try_into()?,open_time_ns:open_time,close_time_ns:close_time,
        open:text(&value["open"] )?,high:text(&value["high"] )?,low:text(&value["low"] )?,close:text(&value["close"] )?,volume:if value["volume"].is_null(){None}else{Some(text(&value["volume"] )?)},revision:ordinal(&value["revision"] )?,
        received_sequence:source["raw_payload"].get("sequence").map(ordinal).transpose()?,state:text(&value["state"] )?,finalized_at_ns:if value["finalized_at"].is_null(){None}else{Some(ns(&value["finalized_at"] )?)},source_observed_at_ns:ns(&source["observed_at"] )?,received_at_ns:ns(&source["received_at"] )?,source_metadata:source.clone(),evidence:json!({"instrument":value["instrument"]})})
}
pub fn quote_from_value(value:&Value)->Result<ImportQuoteRow> {
    if value.get("observed_at_ns").is_some() && value.get("price").is_some() {return serde_json::from_value(value.clone()).map_err(Into::into);}
    let source=&value["source"];let optional=|key:&str|->Result<Option<String>> {if value[key].is_null(){Ok(None)}else{Ok(Some(text(&value[key])?))}};
    Ok(ImportQuoteRow {instrument_symbol:text(&value["instrument"]["symbol"] )?,realtime_source_id:text(&source["provider"] )?,evidence_channel_id:source["raw_payload"]["channel"].as_str().unwrap_or(source["provider"].as_str().unwrap_or("unknown")).into(),event_id:text(&value["event_id"] )?,price:text(&value["last"] )?,bid:optional("bid")?,ask:optional("ask")?,volume:optional("volume")?,observed_at_ns:ns(&source["observed_at"] )?,received_at_ns:ns(&source["received_at"] )?,source_sequence:source["raw_payload"].get("sequence").map(ordinal).transpose()?,source_metadata:source.clone(),statistics:json!({"open":value["open"],"high":value["high"],"low":value["low"],"change":value["change"],"change_percent":value["change_percent"]}),is_supplement:source["raw_payload"]["observation_kind"]=="supplement",evidence:json!({"instrument":value["instrument"]})})
}
