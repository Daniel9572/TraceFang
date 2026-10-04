//! Immutable exact native input. Only bounded encoding runs inside the MVCC scan;
//! consumers read Parquet after that transaction has ended.
use anyhow::{Result,Context,ensure};
use arrow_array::{Array,ArrayRef,RecordBatch,StringArray,Int64Array,UInt64Array,UInt32Array,Decimal128Array};
use arrow_schema::{DataType,Field,Schema};
use parquet::{arrow::{ArrowWriter,arrow_reader::ParquetRecordBatchReaderBuilder},basic::Compression,file::properties::WriterProperties};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{sync::{Arc,Mutex,LazyLock},path::{Path,PathBuf},fs::{self,File},io::Read,collections::BTreeMap};
use num_bigint::BigInt;
use num_traits::{Signed,ToPrimitive,Zero};
use tracefang_core::{native_store::{Store,ScanResume},persistence_contract::{CanonicalScanRequest,CanonicalScanBatch,CanonicalScanContext,CanonicalScanSummary,ImportBarRow,SnapshotVersion},periods::{Period,MarketSchedule},domain::Decimal};
#[path="quant_bar_adapter.rs"]mod bar_adapter;
#[path="file_identity.rs"]mod file_identity;
use tracefang_core::quant_core::quant::{QuantBar,SourceVolumeComponents};
pub type Cancel=Arc<dyn Fn()->bool+Send+Sync>;
pub type Progress=Arc<dyn Fn(Value)+Send+Sync>;
pub const SCHEMA:&str="native-exact-parquet-v3-source-volume-policies";
const PREVIOUS_SCHEMA:&str="native-exact-parquet-v2-projected-quant";
const OLD_SCHEMA:&str="native-exact-parquet-v1";
const MAX_ROW_BYTES:usize=1024*1024;
const BATCH_BYTES:usize=4*1024*1024;
const MAX_FILE_BYTES:u64=4*1024*1024*1024;
static EXPORTS:LazyLock<Arc<tokio::sync::Semaphore>>=LazyLock::new(||Arc::new(tokio::sync::Semaphore::new(1)));
#[derive(Clone)]pub struct Plan {pub scan:CanonicalScanRequest,pub period:Period,pub schedule:Option<MarketSchedule>,pub resume:Option<ScanResume>,pub build:String}
impl Plan{fn value(&self)->Result<Value>{Ok(json!({"scan":self.scan,"period":self.period.as_str(),"calendar":tracefang_core::periods::schedule_version(self.schedule.as_ref())?,"resume":self.resume.as_ref().map(|r|json!({"after_ns":r.after_ns.to_string(),"series_generation":r.series_generation,"correction_epoch":r.correction_epoch.to_string(),"append_watermark_ns":r.append_watermark_ns.map(|v|v.to_string())})),"build":self.build,"schema":SCHEMA}))}}
#[derive(Clone,Debug,Serialize,Deserialize,Default)]pub struct DecimalProof {
 pub null_count:u64,pub known_count:u64,pub decimal38_18_unrepresentable:u64,pub min_scale:Option<i64>,pub max_scale:Option<i64>,pub max_coefficient_digits:usize,pub max_abs_decimal38_18_coefficient:String,pub sum_decimal38_18_safe:bool,
}
#[derive(Clone,Debug,Serialize,Deserialize)]pub struct VolumeProof {pub known_component_count:String,pub component_count:String,pub nullable_volume_equivalent:bool}
#[derive(Clone,Debug,Serialize,Deserialize)]pub struct Manifest {
 pub schema:String,pub id:String,pub scope:Value,pub version:SnapshotVersion,pub context:CanonicalScanContext,pub summary:CanonicalScanSummary,
 pub file:String,pub file_sha256:String,pub file_bytes:u64,pub columns:BTreeMap<String,DecimalProof>,pub canonical_encoding:String,pub published_at:chrono::DateTime<chrono::Utc>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub projected_quant_sha256:Option<String>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub volume_semantics:Option<VolumeProof>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub source_component_rows:Option<u64>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub source_component_semantics:Option<String>,
}
static CACHE_MUTATIONS:LazyLock<Mutex<()>>=LazyLock::new(||Mutex::new(()));
static LEASES:LazyLock<Mutex<BTreeMap<PathBuf,usize>>>=LazyLock::new(||Mutex::new(BTreeMap::new()));
struct Lease(PathBuf);
impl Drop for Lease{fn drop(&mut self){if let Ok(mut leases)=LEASES.lock(){if let Some(count)=leases.get_mut(&self.0){*count-=1;if *count==0{leases.remove(&self.0);}}}}}
fn lease(path:&Path)->Result<Arc<Lease>>{let mut leases=LEASES.lock().map_err(|_|anyhow::anyhow!("snapshot lease registry poisoned"))?;*leases.entry(path.into()).or_default()+=1;Ok(Arc::new(Lease(path.into())))}
static VERIFIED:LazyLock<Mutex<std::collections::VecDeque<Published>>>=LazyLock::new(||Mutex::new(std::collections::VecDeque::new()));
#[derive(Clone)]pub struct Published{pub directory:PathBuf,pub manifest:Manifest,_lease:Arc<Lease>,data:Arc<file_identity::BoundFile>,metadata:Option<Arc<file_identity::BoundFile>>}
impl Published{
 fn new(directory:PathBuf,manifest:Manifest)->Result<Self>{let guard=lease(&directory)?;let data=Arc::new(file_identity::BoundFile::open(&directory.join(&manifest.file))?);let metadata=directory.join("manifest.json");let metadata=if metadata.exists(){Some(Arc::new(file_identity::BoundFile::open(&metadata)?))}else{None};Ok(Self{directory,manifest,_lease:guard,data,metadata})}
 pub fn guard(&self)->Result<()>{self.data.guard()?;if let Some(file)=&self.metadata{file.guard()?;}Ok(())}
 pub fn reader(&self)->Result<File>{self.guard()?;self.data.reader()}
}
fn prune_verified_idle()->Result<()>{let mut cache=VERIFIED.lock().map_err(|_|anyhow::anyhow!("immutable verification cache poisoned"))?;cache.retain(|entry|Arc::strong_count(&entry._lease)>1);Ok(())}

fn hash(value:&impl Serialize)->Result<String>{Ok(hex::encode(Sha256::digest(serde_json::to_vec(value)?)))}
fn cancelled(cancel:&Cancel)->Result<()>{ensure!(!cancel(),"cancelled before immutable snapshot publication");Ok(())}
fn sync_dir(path:&Path)->Result<()>{#[cfg(unix)]File::open(path)?.sync_all()?;Ok(())}
fn file_hash(path:&Path)->Result<String>{let mut file=File::open(path)?;let mut hash=Sha256::new();let mut bytes=[0u8;65536];loop{let n=file.read(&mut bytes)?;if n==0{break}hash.update(&bytes[..n]);}Ok(hex::encode(hash.finalize()))}
fn valid_id(id:&str)->bool{id.len()==64&&id.bytes().all(|b|b.is_ascii_digit()||(b'a'..=b'f').contains(&b))}
fn directory_bytes(path:&Path)->Result<u64>{let mut total=0u64;for entry in fs::read_dir(path)?{let entry=entry?;let metadata=entry.metadata()?;total=total.checked_add(if metadata.is_dir(){directory_bytes(&entry.path())?}else{metadata.len()}).context("snapshot directory byte count exhausted")?;}Ok(total)}
/// Durable research/simulation/audit references survive process restart. Cache
/// pointers are accelerators; only explicit pins and active readers prevent GC.
pub fn pin(root:&Path,id:&str,reference:&str)->Result<()>{ensure!(!reference.is_empty()&&reference.len()<=1024,"snapshot reference label outside bound");let snapshot=read(root,id)?;let _guard=CACHE_MUTATIONS.lock().map_err(|_|anyhow::anyhow!("snapshot cache mutation registry poisoned"))?;let pins=root.join("pins");fs::create_dir_all(&pins)?;let name=hash(&reference)?;let target=pins.join(format!("{name}.json"));if target.exists(){let previous:Value=serde_json::from_reader(File::open(&target)?)?;ensure!(previous["snapshot_id"]==id&&previous["reference"]==reference,"durable snapshot reference already binds another immutable input; explicitly unpin before replacement");return Ok(())}let candidate=pins.join(format!(".pending-{}",uuid::Uuid::new_v4()));let mut file=File::create(&candidate)?;serde_json::to_writer(&mut file,&json!({"snapshot_id":snapshot.manifest.id,"reference":reference}))?;file.sync_all()?;fs::rename(candidate,target)?;sync_dir(&pins)?;Ok(())}
pub fn unpin(root:&Path,reference:&str)->Result<()>{let _guard=CACHE_MUTATIONS.lock().map_err(|_|anyhow::anyhow!("snapshot cache mutation registry poisoned"))?;let pins=root.join("pins");let path=pins.join(format!("{}.json",hash(&reference)?));if path.exists(){fs::remove_file(path)?;sync_dir(&pins)?;}Ok(())}
pub fn gc_to(root:&Path,budget:u64,min_free:u64)->Result<Value>{
 let _guard=CACHE_MUTATIONS.lock().map_err(|_|anyhow::anyhow!("snapshot cache mutation registry poisoned"))?;fs::create_dir_all(root)?;prune_verified_idle()?;let root=root.canonicalize()?;let pins=root.join("pins");let mut protected=std::collections::BTreeSet::new();if pins.exists(){for entry in fs::read_dir(pins)?{let entry=entry?;if entry.path().extension().is_some_and(|v|v=="json"){let pin:Value=serde_json::from_reader(File::open(entry.path())?)?;let id=pin["snapshot_id"].as_str().context("durable snapshot pin missing id")?;ensure!(valid_id(id),"durable snapshot pin id invalid");protected.insert(id.to_owned());}}}
 let mut candidates=Vec::new();for entry in fs::read_dir(&root)?{let entry=entry?;let name=entry.file_name().to_string_lossy().into_owned();if valid_id(&name)&&entry.file_type()?.is_dir(){candidates.push((entry.metadata()?.modified()?,entry.path(),name));}}candidates.sort_by_key(|v|v.0);let mut removed=Vec::new();
 for (_,path,id) in candidates{if directory_bytes(&root)?<=budget&&crate::capture::available_bytes(&root)?>=min_free{break}if protected.contains(&id){continue}let leases=LEASES.lock().map_err(|_|anyhow::anyhow!("snapshot lease registry poisoned"))?;if leases.contains_key(&path){continue}
  // Hold the registry while removing; a reader registers before opening files.
  fs::remove_dir_all(&path)?;for entry in fs::read_dir(&root)?{let entry=entry?;let name=entry.file_name().to_string_lossy().into_owned();if name.starts_with("reference-")&&name.ends_with(".json"){let value:String=serde_json::from_reader(File::open(entry.path())?)?;if value==id{fs::remove_file(entry.path())?;}}}removed.push(id);drop(leases);
 }sync_dir(&root)?;let bytes=directory_bytes(&root)?;let available=crate::capture::available_bytes(&root)?;ensure!(bytes<=budget&&available>=min_free,"snapshot budget exhausted by active/durable references or external disk use; source and pinned versions preserved");Ok(json!({"removed_unreferenced_snapshots":removed,"cache_bytes":bytes.to_string(),"available_bytes":available.to_string()}))
}
fn schema_v1()->Arc<Schema>{
 let mut fields=vec![Field::new("instrument_symbol",DataType::Utf8,false),Field::new("source_id",DataType::Utf8,false),Field::new("evidence_channel_id",DataType::Utf8,false),Field::new("interval_seconds",DataType::UInt32,false),Field::new("open_time_ns",DataType::Int64,false),Field::new("close_time_ns",DataType::Int64,false)];
 for name in ["open","high","low","close","volume"]{let nullable=name=="volume";fields.extend([Field::new(format!("{name}_coefficient"),DataType::Utf8,nullable),Field::new(format!("{name}_scale"),DataType::Int64,nullable),Field::new(format!("{name}_decimal38_18"),DataType::Decimal128(38,18),true)]);}
 fields.extend([Field::new("revision",DataType::UInt64,false),Field::new("received_sequence",DataType::UInt64,true),Field::new("state",DataType::Utf8,false),Field::new("finalized_at_ns",DataType::Int64,true),Field::new("source_observed_at_ns",DataType::Int64,false),Field::new("received_at_ns",DataType::Int64,false),Field::new("source_metadata_json",DataType::Utf8,false),Field::new("evidence_json",DataType::Utf8,false)]);Arc::new(Schema::new(fields))
}
fn schema_v2()->Arc<Schema>{let mut fields=schema_v1().fields().iter().map(|f|f.as_ref().clone()).collect::<Vec<_>>();fields.extend([Field::new("known_volume_sum_coefficient",DataType::Utf8,false),Field::new("known_volume_sum_scale",DataType::Int64,false),Field::new("known_volume_count",DataType::UInt64,false),Field::new("component_count",DataType::UInt64,false),Field::new("capture_accepted_at_ns",DataType::Int64,true),Field::new("applied_frame_sequence",DataType::UInt64,true),Field::new("source_precision_ns",DataType::UInt64,true)]);Arc::new(Schema::new(fields))}
fn schema()->Arc<Schema>{let mut fields=schema_v2().fields().iter().map(|f|f.as_ref().clone()).collect::<Vec<_>>();fields.extend([
 Field::new("source_volume_sum_coefficient",DataType::Utf8,true),Field::new("source_volume_sum_scale",DataType::Int64,true),Field::new("source_volume_known_count",DataType::UInt64,true),Field::new("source_volume_total_count",DataType::UInt64,true),Field::new("source_volume_policy",DataType::Utf8,true),Field::new("source_volume_groups_json",DataType::Utf8,false)]);Arc::new(Schema::new(fields))}
fn schema_for(version:&str)->Arc<Schema>{if version==OLD_SCHEMA{schema_v1()}else if version==PREVIOUS_SCHEMA{schema_v2()}else{schema()}}
fn decimal<'a>(row:&'a ImportBarRow,name:&str)->Option<&'a str>{match name{"open"=>Some(&row.open),"high"=>Some(&row.high),"low"=>Some(&row.low),"close"=>Some(&row.close),"volume"=>row.volume.as_deref(),_=>None}}
fn parts(value:&str)->Result<(String,i64,Option<i128>)>{
 let exact=Decimal::from_str_exact(value)?;let (coefficient,scale)=exact.as_bigdecimal().as_bigint_and_exponent();
 let shift=18i128-scale as i128;
 let quick=if coefficient.is_zero(){Some(0)}else if (0..=38).contains(&shift){let scaled=&coefficient*BigInt::from(10u8).pow(shift as u32);if scaled.abs()<BigInt::from(10u8).pow(38){scaled.to_i128()}else{None}}else{None};
 Ok((coefficient.to_string(),scale,quick))
}
fn record_batch(rows:&[ImportBarRow],proofs:&mut BTreeMap<String,DecimalProof>)->Result<RecordBatch>{
 let mut arrays:Vec<ArrayRef>=vec![Arc::new(StringArray::from_iter_values(rows.iter().map(|r|r.instrument_symbol.as_str()))),Arc::new(StringArray::from_iter_values(rows.iter().map(|r|r.realtime_source_id.as_str()))),Arc::new(StringArray::from_iter_values(rows.iter().map(|r|r.evidence_channel_id.as_str()))),Arc::new(UInt32Array::from_iter_values(rows.iter().map(|r|r.interval_seconds))),Arc::new(Int64Array::from_iter_values(rows.iter().map(|r|r.open_time_ns))),Arc::new(Int64Array::from_iter_values(rows.iter().map(|r|r.close_time_ns)))];
 for name in ["open","high","low","close","volume"]{
  let mut coefficients=Vec::with_capacity(rows.len());let mut scales=Vec::with_capacity(rows.len());let mut quick=Vec::with_capacity(rows.len());let proof=proofs.entry(name.into()).or_default();
  for row in rows{match decimal(row,name){Some(value)=>{let (coefficient,scale,fast)=parts(value)?;proof.known_count+=1;proof.min_scale=Some(proof.min_scale.map_or(scale,|v|v.min(scale)));proof.max_scale=Some(proof.max_scale.map_or(scale,|v|v.max(scale)));proof.max_coefficient_digits=proof.max_coefficient_digits.max(coefficient.trim_start_matches('-').len());if let Some(fast)=fast{let bound=fast.unsigned_abs();let old=proof.max_abs_decimal38_18_coefficient.parse::<u128>().unwrap_or(0);proof.max_abs_decimal38_18_coefficient=old.max(bound).to_string();}else{proof.decimal38_18_unrepresentable+=1;}coefficients.push(Some(coefficient));scales.push(Some(scale));quick.push(fast);},None=>{proof.null_count+=1;coefficients.push(None);scales.push(None);quick.push(None);}}}
  arrays.push(Arc::new(StringArray::from(coefficients)));arrays.push(Arc::new(Int64Array::from(scales)));arrays.push(Arc::new(Decimal128Array::from(quick).with_precision_and_scale(38,18)?));
 }
 arrays.extend([Arc::new(UInt64Array::from_iter_values(rows.iter().map(|r|r.revision))) as ArrayRef,Arc::new(UInt64Array::from(rows.iter().map(|r|r.received_sequence).collect::<Vec<_>>())),Arc::new(StringArray::from_iter_values(rows.iter().map(|r|r.state.as_str()))),Arc::new(Int64Array::from(rows.iter().map(|r|r.finalized_at_ns).collect::<Vec<_>>())),Arc::new(Int64Array::from_iter_values(rows.iter().map(|r|r.source_observed_at_ns))),Arc::new(Int64Array::from_iter_values(rows.iter().map(|r|r.received_at_ns))),Arc::new(StringArray::from(rows.iter().map(|r|serde_json::to_string(&r.source_metadata)).collect::<std::result::Result<Vec<_>,_>>()?)),Arc::new(StringArray::from(rows.iter().map(|r|serde_json::to_string(&r.evidence)).collect::<std::result::Result<Vec<_>,_>>()?))]);
 let quant=rows.iter().map(bar_adapter::bar_ref).collect::<Result<Vec<_>>>()?;for bar in &quant{validate_volume(bar)?;}let sums=quant.iter().map(|bar|bar.known_volume_sum.normalized().as_bigint_and_exponent()).collect::<Vec<_>>();arrays.extend([Arc::new(StringArray::from_iter_values(sums.iter().map(|(v,_)|v.to_string()))) as ArrayRef,Arc::new(Int64Array::from_iter_values(sums.iter().map(|(_,scale)|*scale))),Arc::new(UInt64Array::from_iter_values(quant.iter().map(|v|v.known_volume_count))),Arc::new(UInt64Array::from_iter_values(quant.iter().map(|v|v.component_count))),Arc::new(Int64Array::from(quant.iter().map(|v|v.accepted_at.map(|at|at.timestamp_nanos_opt().context("accepted time outside ns")).transpose()).collect::<Result<Vec<_>>>()?)),Arc::new(UInt64Array::from(quant.iter().map(|v|v.applied_frame_seq).collect::<Vec<_>>())),Arc::new(UInt64Array::from(quant.iter().map(|v|v.source_precision_ns).collect::<Vec<_>>() ))]);
 let sums=quant.iter().map(|bar|bar.source_volume_components.as_ref().map(|v|v.known_volume_sum.normalized().as_bigint_and_exponent())).collect::<Vec<_>>();
 arrays.extend([Arc::new(StringArray::from(sums.iter().map(|v|v.as_ref().map(|(n,_)|n.to_string())).collect::<Vec<_>>())) as ArrayRef,Arc::new(Int64Array::from(sums.iter().map(|v|v.as_ref().map(|(_,scale)|*scale)).collect::<Vec<_>>())),Arc::new(UInt64Array::from(quant.iter().map(|v|v.source_volume_components.as_ref().map(|v|v.known_count)).collect::<Vec<_>>())),Arc::new(UInt64Array::from(quant.iter().map(|v|v.source_volume_components.as_ref().map(|v|v.total_count)).collect::<Vec<_>>())),Arc::new(StringArray::from(quant.iter().map(|v|v.source_volume_components.as_ref().map(|v|v.policy.as_str())).collect::<Vec<_>>())),Arc::new(StringArray::from(quant.iter().map(|v|serde_json::to_string(&sorted_groups(&v.source_volume_component_groups))).collect::<std::result::Result<Vec<_>,_>>()?))]);
 Ok(RecordBatch::try_new(schema(),arrays)?)
}
fn value<'a,T:Array+'static>(batch:&'a RecordBatch,name:&str)->Result<&'a T>{batch.column_by_name(name).context("snapshot column absent")?.as_any().downcast_ref::<T>().context("snapshot column type differs")}
fn string(batch:&RecordBatch,name:&str,row:usize)->Result<String>{let column=value::<StringArray>(batch,name)?;ensure!(!column.is_null(row),"required snapshot string is NULL");Ok(column.value(row).into())}
fn signed(batch:&RecordBatch,name:&str,row:usize)->Result<i64>{let column=value::<Int64Array>(batch,name)?;ensure!(!column.is_null(row),"required snapshot time is NULL");Ok(column.value(row))}
fn decode_decimal(batch:&RecordBatch,name:&str,row:usize)->Result<Option<String>>{
 let coefficient=value::<StringArray>(batch,&format!("{name}_coefficient"))?;let scale=value::<Int64Array>(batch,&format!("{name}_scale"))?;ensure!(coefficient.is_null(row)==scale.is_null(row),"snapshot decimal NULL bitmap differs");if coefficient.is_null(row){return Ok(None)}
 Ok(Some(Decimal::wide(bigdecimal::BigDecimal::new(coefficient.value(row).parse()?,scale.value(row))).to_string()))
}
fn decode(batch:&RecordBatch,row:usize)->Result<ImportBarRow>{
 let sequence=value::<UInt64Array>(batch,"received_sequence")?;let finalized=value::<Int64Array>(batch,"finalized_at_ns")?;
 Ok(ImportBarRow{instrument_symbol:string(batch,"instrument_symbol",row)?,realtime_source_id:string(batch,"source_id",row)?,evidence_channel_id:string(batch,"evidence_channel_id",row)?,interval_seconds:value::<UInt32Array>(batch,"interval_seconds")?.value(row),open_time_ns:signed(batch,"open_time_ns",row)?,close_time_ns:signed(batch,"close_time_ns",row)?,open:decode_decimal(batch,"open",row)?.context("open NULL")?,high:decode_decimal(batch,"high",row)?.context("high NULL")?,low:decode_decimal(batch,"low",row)?.context("low NULL")?,close:decode_decimal(batch,"close",row)?.context("close NULL")?,volume:decode_decimal(batch,"volume",row)?,revision:value::<UInt64Array>(batch,"revision")?.value(row),received_sequence:(!sequence.is_null(row)).then(||sequence.value(row)),state:string(batch,"state",row)?,finalized_at_ns:(!finalized.is_null(row)).then(||finalized.value(row)),source_observed_at_ns:signed(batch,"source_observed_at_ns",row)?,received_at_ns:signed(batch,"received_at_ns",row)?,source_metadata:serde_json::from_str(&string(batch,"source_metadata_json",row)?)?,evidence:serde_json::from_str(&string(batch,"evidence_json",row)?)?})
}
struct Encoder{writer:Option<ArrowWriter<File>>,context:Option<CanonicalScanContext>,version:Option<SnapshotVersion>,columns:BTreeMap<String,DecimalProof>,rows:u64,root:PathBuf,quant_hash:Sha256,known_components:BigInt,components:BigInt,nullable_volume_equivalent:bool,source_component_rows:u64}
impl Encoder{fn batch(&mut self,batch:CanonicalScanBatch,cancel:&Cancel,progress:&Progress)->Result<()>{cancelled(cancel)?;if let Some(version)=&self.version{ensure!(serde_json::to_value(version)?==serde_json::to_value(&batch.version)?,"snapshot MVCC changed during export");}else{self.version=Some(batch.version.clone());}if let Some(context)=batch.context{ensure!(self.context.is_none(),"snapshot metadata repeated");self.context=Some(context);}
 let mut rows=Vec::new();let mut bytes=0;for row in batch.rows{let size=serde_json::to_vec(&row)?.len();ensure!(size<=MAX_ROW_BYTES,"snapshot row exceeds 1MiB exact metadata bound");if !rows.is_empty()&&(rows.len()==4096||bytes+size>BATCH_BYTES){self.write(&rows)?;rows.clear();bytes=0;cancelled(cancel)?;}bytes+=size;rows.push(row);}if !rows.is_empty(){self.write(&rows)?;}progress(json!({"phase":"exporting_immutable_input","exported_rows":self.rows.to_string(),"evaluated_rows":"0"}));cancelled(cancel)}
 fn write(&mut self,rows:&[ImportBarRow])->Result<()>{ensure!(crate::capture::available_bytes(&self.root)?>4*1024*1024*1024,"immutable export stopped below 4GiB available disk; source unchanged");let batch=record_batch(rows,&mut self.columns)?;for row in rows{let bar=bar_adapter::bar_ref(row)?;validate_volume(&bar)?;self.source_component_rows+=u64::from(bar.source_volume_components.is_some()||!bar.source_volume_component_groups.is_empty());hash_quant(&mut self.quant_hash,&bar)?;self.known_components+=bar.known_volume_count;self.components+=bar.component_count;self.nullable_volume_equivalent&=bar.volume.is_some()||bar.known_volume_count==0;}let writer=self.writer.as_mut().context("snapshot writer ended")?;writer.write(&batch)?;if writer.in_progress_size()>=BATCH_BYTES{writer.flush()?;}ensure!(writer.inner().metadata()?.len()<=MAX_FILE_BYTES,"snapshot file exceeds 4GiB bound");self.rows+=rows.len() as u64;Ok(())}
}
pub async fn publish(root:&Path,store:&Store,plan:Plan,cancel:Cancel,progress:Progress)->Result<Published>{
 cancelled(&cancel)?;let _permit=EXPORTS.clone().acquire_owned().await?;cancelled(&cancel)?;ensure!(plan.scan.expected_version.is_some(),"immutable export requires an exact expected MVCC version");let scope=plan.value()?;fs::create_dir_all(root)?;let root=root.canonicalize()?;
 let key=hash(&scope)?;let pointer=root.join(format!("reference-{key}.json"));if pointer.exists(){let id:String=serde_json::from_reader(File::open(&pointer)?)?;let base=root.clone();let requested=id.clone();let existing=tokio::task::spawn_blocking(move||read(&base,&requested)).await??;ensure!(existing.manifest.scope==scope,"snapshot reference scope differs");progress(json!({"phase":"reusing_immutable_input","exported_rows":existing.manifest.summary.row_count.to_string(),"evaluated_rows":"0","snapshot_id":id}));return Ok(existing)}
 let base=root.clone();tokio::task::spawn_blocking(move||gc_to(&base,4*1024*1024*1024,4*1024*1024*1024)).await??;
 let temporary=tempfile::Builder::new().prefix(".pending-").tempdir_in(&root)?;let file=temporary.path().join("facts.parquet");
 let writer=ArrowWriter::try_new(File::create(&file)?,schema(),Some(WriterProperties::builder().set_compression(Compression::SNAPPY).set_max_row_group_row_count(Some(4096)).set_max_row_group_bytes(Some(BATCH_BYTES)).build()))?;
 let encoder=Arc::new(Mutex::new(Encoder{writer:Some(writer),context:None,version:None,columns:BTreeMap::new(),rows:0,root:root.clone(),quant_hash:Sha256::new(),known_components:BigInt::from(0),components:BigInt::from(0),nullable_volume_equivalent:true,source_component_rows:0}));let worker=encoder.clone();let token=cancel.clone();let progress_hook=progress.clone();
 let accept=move|batch|worker.lock().map_err(|_|anyhow::anyhow!("snapshot encoder poisoned"))?.batch(batch,&token,&progress_hook);
 let summary=if plan.period.is_base(){store.canonical_scan_with_calendar_context(plan.scan.clone(),1000,plan.resume.clone(),plan.schedule.clone(),accept).await?}else{store.canonical_calendar_scan(plan.scan.clone(),plan.period,plan.schedule.clone(),1000,plan.resume.clone(),accept).await?};
 // The Store transaction has now ended. Verification and expensive research do
 // not pin obsolete redb pages while market writes continue.
 let root=root.to_owned();tokio::task::spawn_blocking(move||->Result<Published>{
 cancelled(&cancel)?;progress(json!({"phase":"verifying_immutable_input","exported_rows":summary.row_count.to_string(),"evaluated_rows":"0"}));
 let mut encoder=encoder.lock().map_err(|_|anyhow::anyhow!("snapshot encoder poisoned"))?;ensure!(summary.complete&&encoder.rows==summary.row_count,"incomplete immutable export");encoder.writer.take().context("snapshot writer absent")?.close()?;File::open(&file)?.sync_all()?;
 for proof in encoder.columns.values_mut(){let bound:BigInt=proof.max_abs_decimal38_18_coefficient.parse().unwrap_or_default();proof.sum_decimal38_18_safe=proof.decimal38_18_unrepresentable==0&&bound*BigInt::from(proof.known_count)<BigInt::from(10u8).pow(38);}
 let mut manifest=Manifest{schema:SCHEMA.into(),id:String::new(),scope,version:summary.version.clone(),context:encoder.context.take().context("immutable scan metadata missing")?,summary,file:"facts.parquet".into(),file_sha256:file_hash(&file)?,file_bytes:fs::metadata(&file)?.len(),columns:std::mem::take(&mut encoder.columns),canonical_encoding:"ImportBarRow ordered JSON SHA256; projected QuantBar length-prefixed normalized coefficient+scale and exact fixed ns/u64 binary SHA256 v1; DECIMAL(38,18) only proven optional accelerator".into(),published_at:chrono::Utc::now(),projected_quant_sha256:Some(hex::encode(encoder.quant_hash.clone().finalize())),volume_semantics:Some(VolumeProof{known_component_count:encoder.known_components.to_string(),component_count:encoder.components.to_string(),nullable_volume_equivalent:encoder.nullable_volume_equivalent}),source_component_rows:Some(encoder.source_component_rows),source_component_semantics:Some("Independent source sample sum/count/policy; canonical minute coverage unchanged. Groups are never added across differing policies. v1/v2 do not contain this projected evidence.".into())};drop(encoder);
 let candidate=Published::new(temporary.path().into(),manifest.clone())?;verify_rows(&candidate,&cancel)?;cancelled(&cancel)?;gc_to(&root,8*1024*1024*1024,4*1024*1024*1024)?;manifest.id=hash(&manifest)?;
 let output=temporary.path().join("manifest.json");let mut out=File::create(&output)?;serde_json::to_writer(&mut out,&manifest)?;out.sync_all()?;sync_dir(temporary.path())?;cancelled(&cancel)?;
 let target=root.join(&manifest.id);ensure!(!target.exists(),"immutable snapshot publication collision");let protection=lease(&target)?;let path=temporary.keep();fs::rename(&path,&target)?;sync_dir(&root)?;
 let temp_pointer=root.join(format!(".reference-{}",uuid::Uuid::new_v4()));let mut out=File::create(&temp_pointer)?;serde_json::to_writer(&mut out,&manifest.id)?;out.sync_all()?;fs::rename(&temp_pointer,&pointer)?;sync_dir(&root)?;
 progress(json!({"phase":"immutable_input_ready","snapshot_id":manifest.id,"exported_rows":manifest.summary.row_count.to_string(),"evaluated_rows":"0"}));let published=Published::new(target,manifest)?;drop(protection);remember(&published)?;Ok(published)
 }).await?
}
fn remember(snapshot:&Published)->Result<()> {
 let mut cache=VERIFIED.lock().map_err(|_|anyhow::anyhow!("immutable verification cache poisoned"))?;
 cache.retain(|entry|entry.directory!=snapshot.directory);cache.push_back(snapshot.clone());while cache.len()>8{cache.pop_front();}Ok(())
}
/// Full manifest/file validation occurs on first open. Reuse retains open handles
/// and checks exact path/device/inode/size/mtime/ctime before and after consumption.
pub fn read(root:&Path,id:&str)->Result<Published>{
 ensure!(valid_id(id),"invalid native snapshot id");let directory=root.canonicalize()?.join(id);
 let protection=lease(&directory)?;ensure!(directory.canonicalize()?==directory,"immutable snapshot directory escapes cache root");
 {let mut cache=VERIFIED.lock().map_err(|_|anyhow::anyhow!("immutable verification cache poisoned"))?;
  if let Some(index)=cache.iter().position(|entry|entry.directory==directory){let existing=cache.remove(index).unwrap();existing.guard()?;cache.push_back(existing.clone());return Ok(existing)}
 }
 let metadata=Arc::new(file_identity::BoundFile::open(&directory.join("manifest.json"))?);
 ensure!(metadata.path()==directory.join("manifest.json"),"manifest path escapes immutable directory");
 ensure!(metadata.file.metadata()?.len()<=1024*1024,"immutable manifest byte budget exceeded");let manifest:Manifest=serde_json::from_reader(metadata.reader()?)?;
 ensure!((manifest.schema==SCHEMA||manifest.schema==PREVIOUS_SCHEMA||manifest.schema==OLD_SCHEMA)&&manifest.id==id&&manifest.file=="facts.parquet","native snapshot identity/schema/path differs");
 let mut body=manifest.clone();body.id.clear();ensure!(hash(&body)?==id,"native snapshot manifest hash differs");
 let data=Arc::new(file_identity::BoundFile::open(&directory.join(&manifest.file))?);
 ensure!(data.path()==directory.join(&manifest.file),"data path escapes immutable directory");
 ensure!(data.file.metadata()?.len()==manifest.file_bytes&&data.sha256()?==manifest.file_sha256,"native snapshot file bytes differ");
 let published=Published{directory,manifest,_lease:protection,data,metadata:Some(metadata)};published.guard()?;remember(&published)?;Ok(published)
}
pub fn scan<F>(snapshot:&Published,batch_rows:usize,cancel:&Cancel,mut accept:F)->Result<CanonicalScanSummary>where F:FnMut(CanonicalScanBatch)->Result<()>{
 ensure!((1..=4096).contains(&batch_rows),"immutable scan batch rows outside 1..4096");cancelled(cancel)?;let file=snapshot.reader()?;let reader=ParquetRecordBatchReaderBuilder::try_new(file)?.with_batch_size(batch_rows).build()?;
 let mut context=Some(snapshot.manifest.context.clone());let mut count=0u64;let mut hash=Sha256::new();let mut previous=None;
 for batch in reader {cancelled(cancel)?;let batch=batch?;ensure!(batch.schema()==schema_for(&snapshot.manifest.schema),"immutable Arrow schema differs");let mut rows=Vec::new();let mut bytes=0usize;for index in 0..batch.num_rows(){let row=decode(&batch,index)?;ensure!(previous.is_none_or(|v|row.open_time_ns>v),"immutable canonical rows reordered/duplicated");previous=Some(row.open_time_ns);let encoded=serde_json::to_vec(&row)?;ensure!(encoded.len()<=MAX_ROW_BYTES,"immutable canonical row exceeds bound");if !rows.is_empty()&&bytes+encoded.len()>BATCH_BYTES{let n=rows.len() as u64;accept(CanonicalScanBatch{version:snapshot.manifest.version.clone(),context:context.take(),row_offset:count,rows:std::mem::take(&mut rows)})?;count+=n;bytes=0;cancelled(cancel)?;}hash.update(&encoded);hash.update(b"\n");bytes+=encoded.len();rows.push(row);}if !rows.is_empty(){let n=rows.len() as u64;accept(CanonicalScanBatch{version:snapshot.manifest.version.clone(),context:context.take(),row_offset:count,rows})?;count+=n;}}
 if context.is_some(){accept(CanonicalScanBatch{version:snapshot.manifest.version.clone(),context:context.take(),row_offset:0,rows:vec![]})?;}
 ensure!(count==snapshot.manifest.summary.row_count&&hex::encode(hash.finalize())==snapshot.manifest.summary.sha256,"immutable canonical content count/hash differs from fixed MVCC export");cancelled(cancel)?;snapshot.guard()?;Ok(snapshot.manifest.summary.clone())
}
fn verify_rows(snapshot:&Published,cancel:&Cancel)->Result<()>{scan(snapshot,1000,cancel,|_|Ok(()))?;scan_quant(snapshot,1000,cancel,|_|Ok(()))?;Ok(())}

fn hash_decimal(hash:&mut Sha256,value:&bigdecimal::BigDecimal){let (coefficient,scale)=value.normalized().as_bigint_and_exponent();let text=coefficient.to_string();hash.update((text.len() as u64).to_be_bytes());hash.update(text.as_bytes());hash.update(scale.to_be_bytes());}
fn hash_optional_u64(hash:&mut Sha256,value:Option<u64>){match value{Some(value)=>{hash.update([1]);hash.update(value.to_be_bytes());},None=>hash.update([0])}}
fn hash_optional_time(hash:&mut Sha256,value:Option<chrono::DateTime<chrono::Utc>>)->Result<()>{match value{Some(value)=>{hash.update([1]);hash.update(value.timestamp_nanos_opt().context("projected time outside ns")?.to_be_bytes());},None=>hash.update([0])}Ok(())}
fn hash_quant(hash:&mut Sha256,bar:&QuantBar)->Result<()>{
 hash.update(b"QuantBar-binary-v1\0");for time in [bar.open_time,bar.bucket_end,bar.observed_at,bar.received_at]{hash.update(time.timestamp_nanos_opt().context("projected time outside ns")?.to_be_bytes());}for value in [&bar.open,&bar.high,&bar.low,&bar.close,&bar.known_volume_sum]{hash_decimal(hash,value)}match &bar.volume{Some(volume)=>{hash.update([1]);hash_decimal(hash,volume)},None=>hash.update([0])}
 for value in [bar.known_volume_count,bar.component_count,bar.revision]{hash.update(value.to_be_bytes());}hash.update((bar.state.len() as u64).to_be_bytes());hash.update(bar.state.as_bytes());hash_optional_time(hash,bar.accepted_at)?;hash_optional_time(hash,bar.finalized_at)?;hash_optional_u64(hash,bar.applied_frame_seq);hash_optional_u64(hash,bar.source_precision_ns);
 // Absent new evidence preserves the original v2 binary digest. Explicit
 // policy evidence is appended and therefore cannot reuse its old row hash.
 if let Some(value)=&bar.source_volume_components{hash.update(b"source-components-v1\0");hash_source(hash,value);}if !bar.source_volume_component_groups.is_empty(){hash.update(b"source-groups-v1\0");hash.update((bar.source_volume_component_groups.len() as u64).to_be_bytes());for value in sorted_groups(&bar.source_volume_component_groups){hash_source(hash,value);}}Ok(())
}
fn sorted_groups(values:&[SourceVolumeComponents])->Vec<&SourceVolumeComponents>{let mut groups=values.iter().collect::<Vec<_>>();groups.sort_by(|a,b|a.policy.cmp(&b.policy));groups}
fn hash_source(hash:&mut Sha256,value:&SourceVolumeComponents){hash_decimal(hash,&value.known_volume_sum);hash.update(value.known_count.to_be_bytes());hash.update(value.total_count.to_be_bytes());hash.update((value.policy.len() as u64).to_be_bytes());hash.update(value.policy.as_bytes());}
fn decode_source(batch:&RecordBatch,row:usize)->Result<(Option<SourceVolumeComponents>,Vec<SourceVolumeComponents>)>{
 if batch.column_by_name("source_volume_groups_json").is_none(){return Ok((None,vec![]))} // Old v2 evidence is unknown, not reconstructed from metadata.
 let sum=direct_decimal(batch,"source_volume_sum",row)?;let known=optional_unsigned(batch,"source_volume_known_count",row)?;let total=optional_unsigned(batch,"source_volume_total_count",row)?;let policy=value::<StringArray>(batch,"source_volume_policy")?;let policy=(!policy.is_null(row)).then(||policy.value(row).to_owned());
 ensure!(sum.is_some()==known.is_some()&&sum.is_some()==total.is_some()&&sum.is_some()==policy.is_some(),"source component NULL bitmaps differ");
 let single=sum.map(|known_volume_sum|SourceVolumeComponents{known_volume_sum,known_count:known.unwrap(),total_count:total.unwrap(),policy:policy.unwrap()});
 let groups=string(batch,"source_volume_groups_json",row)?;ensure!(groups.len()<=8192,"source policy groups exceed projected byte bound");let mut groups:Vec<SourceVolumeComponents>=serde_json::from_str(&groups)?;groups.sort_by(|a,b|a.policy.cmp(&b.policy));Ok((single,groups))
}
fn direct_decimal(batch:&RecordBatch,name:&str,row:usize)->Result<Option<bigdecimal::BigDecimal>>{let coefficient=value::<StringArray>(batch,&format!("{name}_coefficient"))?;let scale=value::<Int64Array>(batch,&format!("{name}_scale"))?;ensure!(coefficient.is_null(row)==scale.is_null(row),"projected decimal NULL bitmap differs");if coefficient.is_null(row){return Ok(None)}Ok(Some(bigdecimal::BigDecimal::new(coefficient.value(row).parse()?,scale.value(row))))}
fn optional_signed(batch:&RecordBatch,name:&str,row:usize)->Result<Option<i64>>{let column=value::<Int64Array>(batch,name)?;Ok((!column.is_null(row)).then(||column.value(row)))}
fn optional_unsigned(batch:&RecordBatch,name:&str,row:usize)->Result<Option<u64>>{let column=value::<UInt64Array>(batch,name)?;Ok((!column.is_null(row)).then(||column.value(row)))}
fn decode_quant(batch:&RecordBatch,row:usize)->Result<QuantBar>{let time=chrono::DateTime::from_timestamp_nanos;let state=string(batch,"state",row)?;let(source_volume_components,source_volume_component_groups)=decode_source(batch,row)?;Ok(QuantBar{
 open_time:time(signed(batch,"open_time_ns",row)?),bucket_end:time(signed(batch,"close_time_ns",row)?),open:direct_decimal(batch,"open",row)?.context("projected open NULL")?,high:direct_decimal(batch,"high",row)?.context("projected high NULL")?,low:direct_decimal(batch,"low",row)?.context("projected low NULL")?,close:direct_decimal(batch,"close",row)?.context("projected close NULL")?,volume:direct_decimal(batch,"volume",row)?,known_volume_sum:direct_decimal(batch,"known_volume_sum",row)?.context("projected known volume sum NULL")?,known_volume_count:value::<UInt64Array>(batch,"known_volume_count")?.value(row),component_count:value::<UInt64Array>(batch,"component_count")?.value(row),state:match state.as_str(){"final"=>"final","forming"|"provisional_quote"=>"forming",_=>"provisional"}.into(),revision:value::<UInt64Array>(batch,"revision")?.value(row),observed_at:time(signed(batch,"source_observed_at_ns",row)?),received_at:time(signed(batch,"received_at_ns",row)?),accepted_at:optional_signed(batch,"capture_accepted_at_ns",row)?.map(time),finalized_at:optional_signed(batch,"finalized_at_ns",row)?.map(time),applied_frame_seq:optional_unsigned(batch,"applied_frame_sequence",row)?,source_precision_ns:optional_unsigned(batch,"source_precision_ns",row)?,source_volume_components,source_volume_component_groups})}
pub struct QuantBatch{pub version:SnapshotVersion,pub context:Option<CanonicalScanContext>,pub row_offset:u64,pub bars:Vec<QuantBar>}
pub fn project_bar(row:&ImportBarRow)->Result<QuantBar>{bar_adapter::bar_ref(row)}
pub fn validate_volume(bar:&QuantBar)->Result<()>{if let Some(value)=&bar.source_volume_components{value.validate()?;}ensure!(bar.source_volume_components.is_none()||bar.source_volume_component_groups.is_empty(),"single/group source evidence are mutually exclusive");ensure!(bar.source_volume_component_groups.len()<=8,"source policy group count exceeds bound");let mut policies=std::collections::BTreeSet::new();for value in &bar.source_volume_component_groups{value.validate()?;ensure!(policies.insert(&value.policy),"duplicate source policy group");}ensure!(bar.component_count>0&&bar.known_volume_count<=bar.component_count,"canonical volume component counts inconsistent");if let Some(volume)=&bar.volume{ensure!(bar.known_volume_count==bar.component_count&&volume==&bar.known_volume_sum,"known total volume differs from exact component coverage/sum");}else{ensure!(bar.known_volume_count<bar.component_count,"unknown total volume incorrectly claims complete components");}ensure!(bar.known_volume_count>0||bar.known_volume_sum.is_zero(),"all unknown volume has nonzero known sum");Ok(())}
/// Reads only analytic columns. Full source/evidence JSON remains independently
/// audited at publication and bound by the immutable file digest.
pub fn scan_quant<F>(snapshot:&Published,batch_rows:usize,cancel:&Cancel,mut accept:F)->Result<CanonicalScanSummary>where F:FnMut(QuantBatch)->Result<()>{
 ensure!((1..=4096).contains(&batch_rows),"projected scan rows outside 1..4096");if snapshot.manifest.schema==OLD_SCHEMA{return scan(snapshot,batch_rows,cancel,|batch|{let bars=batch.rows.iter().map(|row|->Result<_>{let mut bar=bar_adapter::bar_ref(row)?;bar.source_volume_components=None;bar.source_volume_component_groups.clear();Ok(bar)}).collect::<Result<Vec<_>>>()?;accept(QuantBatch{version:batch.version,context:batch.context,row_offset:batch.row_offset,bars})})}
 cancelled(cancel)?;let builder=ParquetRecordBatchReaderBuilder::try_new(snapshot.reader()?)?;
 let columns=builder.schema().fields().iter().enumerate().filter_map(|(index,field)|{let name=field.name().as_str();(!matches!(name,"instrument_symbol"|"source_id"|"evidence_channel_id"|"interval_seconds"|"received_sequence"|"source_metadata_json"|"evidence_json")&&!name.ends_with("_decimal38_18")).then_some(index)}).collect::<Vec<_>>();let projection=parquet::arrow::ProjectionMask::roots(builder.parquet_schema(),columns);let reader=builder.with_projection(projection).with_batch_size(batch_rows).build()?;
 let mut context=Some(snapshot.manifest.context.clone());let mut count=0u64;let mut hash=Sha256::new();let mut previous=None;
 for batch in reader{cancelled(cancel)?;let batch=batch?;let mut bars=Vec::with_capacity(batch.num_rows());for row in 0..batch.num_rows(){let bar=decode_quant(&batch,row)?;validate_volume(&bar)?;ensure!(previous.is_none_or(|time|bar.open_time>time),"projected rows reordered/duplicated");previous=Some(bar.open_time);hash_quant(&mut hash,&bar)?;bars.push(bar);}let rows=bars.len() as u64;accept(QuantBatch{version:snapshot.manifest.version.clone(),context:context.take(),row_offset:count,bars})?;count+=rows;}
 if context.is_some(){accept(QuantBatch{version:snapshot.manifest.version.clone(),context:context.take(),row_offset:0,bars:vec![]})?;}
 ensure!(count==snapshot.manifest.summary.row_count&&snapshot.manifest.projected_quant_sha256.as_deref()==Some(hex::encode(hash.finalize()).as_str()),"projected canonical count/binary hash differs from fixed audit export");cancelled(cancel)?;snapshot.guard()?;Ok(snapshot.manifest.summary.clone())
}

#[cfg(test)]mod source_tests {
 use super::*;
 fn row()->ImportBarRow{ImportBarRow{instrument_symbol:"S".into(),realtime_source_id:"source".into(),evidence_channel_id:"native".into(),interval_seconds:60,open_time_ns:0,close_time_ns:60_000_000_000,open:"1".into(),high:"1".into(),low:"1".into(),close:"1".into(),volume:None,revision:1,received_sequence:None,state:"final".into(),finalized_at_ns:Some(60_000_000_000),source_observed_at_ns:0,received_at_ns:60_000_000_000,source_metadata:json!({"provider":"source","raw_payload":{"source_volume_component_groups":[{"known_volume_sum":"0","known_count":"0","total_count":"1","policy":"a"},{"known_volume_sum":"2.0000000000000000000000000001","known_count":"1","total_count":"2","policy":"z"}]}}),evidence:json!({})}}
 #[test]fn projected_group_digest_ignores_order_and_canonical_input_requires_sorted_policies()->Result<()>{
  let first=row();let mut reverse=first.clone();reverse.source_metadata["raw_payload"]["source_volume_component_groups"].as_array_mut().unwrap().reverse();
  ensure!(record_batch(&[reverse],&mut BTreeMap::new()).is_err(),"unsorted canonical source policies accepted");let mut duplicate=first.clone();duplicate.source_metadata["raw_payload"]["source_volume_component_groups"][1]["policy"]=json!("a");ensure!(record_batch(&[duplicate],&mut BTreeMap::new()).is_err(),"duplicate canonical source policy accepted");
  let a=record_batch(&[first],&mut BTreeMap::new())?;let groups:Value=serde_json::from_str(&string(&a,"source_volume_groups_json",0)?)?;ensure!(groups[0]["policy"]=="a"&&groups[1]["policy"]=="z","projected columns changed canonical policy order");
  let mut one=decode_quant(&a,0)?;let mut h1=Sha256::new();hash_quant(&mut h1,&one)?;one.source_volume_component_groups.reverse();let mut h2=Sha256::new();hash_quant(&mut h2,&one)?;ensure!(h1.finalize()==h2.finalize(),"projected binary hash depends on policy ordering");Ok(())
 }
 #[test]fn prior_v2_columns_do_not_invent_new_source_evidence_from_full_metadata()->Result<()>{
  let batch=record_batch(&[row()],&mut BTreeMap::new())?;let schema=schema_v2();let prior=RecordBatch::try_new(schema.clone(),batch.columns()[..schema.fields().len()].to_vec())?;
  let old=decode_quant(&prior,0)?;ensure!(old.source_volume_components.is_none()&&old.source_volume_component_groups.is_empty(),"old v2 invented a new source policy summary");
  let mut empty_new=decode_quant(&batch,0)?;empty_new.source_volume_component_groups.clear();let mut a=Sha256::new();let mut b=Sha256::new();hash_quant(&mut a,&old)?;hash_quant(&mut b,&empty_new)?;ensure!(a.finalize()==b.finalize(),"None new evidence broke old v2 projected digest");Ok(())
 }
}
