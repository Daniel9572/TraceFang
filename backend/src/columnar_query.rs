//! Pinned native DuckDB worker. Public requests select a manifest/range, never SQL.
use anyhow::{Context,Result,ensure};
use std::{path::{Path,PathBuf},sync::{Arc,LazyLock},time::Duration};
use serde::{Serialize,Deserialize};use serde_json::{Value,json};
use crate::batch_snapshot::{self,Published,Cancel};
use tracefang_core::domain::Decimal;
static WORKERS:LazyLock<Arc<tokio::sync::Semaphore>>=LazyLock::new(||Arc::new(tokio::sync::Semaphore::new(2)));
const VERSION:&str="1.5.6";
#[path="file_identity.rs"]mod file_identity;
static RUNTIMES:LazyLock<std::sync::Mutex<std::collections::VecDeque<Runtime>>>=LazyLock::new(||std::sync::Mutex::new(std::collections::VecDeque::new()));
struct VerifiedRuntime{source:file_identity::BoundFile,manifest:file_identity::BoundFile,copy:file_identity::BoundFile,_lease:std::fs::File,_directory:tempfile::TempDir}
#[derive(Clone)]pub struct Runtime{verified:Arc<VerifiedRuntime>,pub evidence:Value}
fn private_permissions(path:&Path,mode:u32)->Result<()>{#[cfg(unix)]{use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(path,std::fs::Permissions::from_mode(mode))?;}#[cfg(not(unix))]{let _=(path,mode);}Ok(())}
fn copy_root()->Result<PathBuf>{
 let root=std::env::temp_dir().join("tracefang-verified-duckdb");std::fs::create_dir_all(&root)?;private_permissions(&root,0o700)?;
 // A kernel-held lease distinguishes a live worker from an orphan left by SIGKILL.
 for entry in std::fs::read_dir(&root)?{let entry=entry?;if !entry.file_type()?.is_dir()||!entry.file_name().to_string_lossy().starts_with("worker-"){continue}
  let lock=std::fs::OpenOptions::new().read(true).write(true).open(entry.path().join(".lease"));if let Ok(lock)=lock{if lock.try_lock().is_ok(){drop(lock);std::fs::remove_dir_all(entry.path())?;}}
 }Ok(root)
}
fn root_bytes(root:&Path)->Result<u64>{let mut bytes=0u64;for directory in std::fs::read_dir(root)?{let directory=directory?;if directory.file_type()?.is_dir(){for file in std::fs::read_dir(directory.path())?{bytes=bytes.checked_add(file?.metadata()?.len()).context("runtime copy budget overflow")?;}}}Ok(bytes)}
impl Runtime{
 pub fn installed()->Result<Self>{
  let executable=if let Some(path)=std::env::var_os("TRACEFANG_DUCKDB_PATH"){PathBuf::from(path)}else{
   let home=PathBuf::from(std::env::var_os("HOME").or_else(||std::env::var_os("USERPROFILE")).context("DuckDB runtime home absent")?);
   let root=if cfg!(target_os="macos"){home.join("Library/Application Support/TraceFang")}else if cfg!(target_os="windows"){std::env::var_os("LOCALAPPDATA").map(PathBuf::from).unwrap_or_else(||home.join("AppData/Local")).join("TraceFang")}else{std::env::var_os("XDG_DATA_HOME").map(PathBuf::from).unwrap_or_else(||home.join(".local/share")).join("tracefang")};
   root.join("runtime/duckdb").join(VERSION).join(if cfg!(windows){"duckdb.exe"}else{"duckdb"})};Self::open(executable)
 }
 pub fn guard(&self)->Result<()>{self.verified.source.guard()?;self.verified.manifest.guard()?;self.verified.copy.guard()?;Ok(())}
 pub fn open(executable:PathBuf)->Result<Self>{
  let executable=executable.canonicalize()?;let mut cache=RUNTIMES.lock().map_err(|_|anyhow::anyhow!("runtime verification cache poisoned"))?;
  if let Some(index)=cache.iter().position(|runtime|runtime.verified.source.path()==executable){let runtime=cache.remove(index).unwrap();runtime.guard()?;cache.push_back(runtime.clone());return Ok(runtime)}
  let source=file_identity::BoundFile::open(&executable)?;let manifest=file_identity::BoundFile::open(&executable.parent().context("DuckDB runtime parent absent")?.join("manifest.json"))?;
  ensure!(manifest.file.metadata()?.len()<=1024*1024&&source.file.metadata()?.len()<=128*1024*1024,"runtime evidence/file byte budget exceeded");let receipt:Value=serde_json::from_reader(manifest.reader()?)?;let expected=receipt["executable_sha256"].as_str().context("runtime file digest absent")?;
  ensure!(receipt["version"]==VERSION&&source.sha256()?==expected,"DuckDB installed version/file digest differs");
  let official=match receipt["platform"].as_str(){Some("osx-arm64")=>"8e0f6825653f8d057922e6147db920bebf072cb41f4b041fd35521c18d7d126e",Some("osx-amd64")=>"ae74c8cd74304bde1d92d941aca37bc72084ee8daa5660913819d8a9e9d29331",Some("linux-amd64")=>"6e89deac1ebbc36eed0291caf8b567b030c7b86ac35998f71854e22b3c5d5e2f",Some("linux-arm64")=>"c544e92c9b7c31fc53c2139802cabd8e2d1b2b3e3f933117f31611239c1402db",Some("windows-amd64")=>"798eae475d07c645ff3b914f7b0e676d06502b5412dd7552638fafed81f9e916",Some("windows-arm64")=>"266052dbf513da86d0d90209db09ec56e79af75b7fbe2d952810526dfc5d2726",_=>anyhow::bail!("DuckDB platform is not a pinned release asset")};
  let official_executable=match receipt["platform"].as_str(){Some("osx-arm64")=>"7d15b2aaf6be05212ada5f99fe79e0b83e63b7ed91e7e422c0b925278e9e0c39",Some("osx-amd64")=>"ad4eda7e81a9f3c2de218c00232dc08a78b819c1c98c0d6d847cf1b42e76c6e8",Some("linux-amd64")=>"61238cfbe9dfeaad4bcfcd8a48f6f7123dae2e9603aaadb1f77a4217aeb8980c",Some("linux-arm64")=>"c0ac0b79e243ee312b3af29c34ac0b94e518aa44c4345036d5d96c1c042c9d48",Some("windows-amd64")=>"2c6a856516a9efb863482a9146242eba5ad919029a082ff773f4770ae0a7816b",Some("windows-arm64")=>"11cc497c1175f858fbc2bead3ccd4f65ea142c6a369e8587d4858debe057620c",_=>anyhow::bail!("DuckDB executable platform not pinned")};ensure!(expected==official_executable,"DuckDB executable differs from verified official archive extraction");
  ensure!(receipt["archive_sha256"]==official&&receipt["version_output"].as_str().is_some_and(|v|v.starts_with("v1.5.6 ")&&v.contains("069cc9f9b5")),"DuckDB release evidence differs");
  while cache.len()>=2{cache.pop_front();}let root=copy_root()?;let bytes=source.file.metadata()?.len();ensure!(bytes<=128*1024*1024&&root_bytes(&root)?.checked_add(bytes).is_some_and(|total|total<=256*1024*1024),"verified runtime copy byte budget exhausted");
  let directory=tempfile::Builder::new().prefix("worker-").tempdir_in(root)?;private_permissions(directory.path(),0o700)?;
  let lease=std::fs::OpenOptions::new().create_new(true).read(true).write(true).open(directory.path().join(".lease"))?;private_permissions(&directory.path().join(".lease"),0o600)?;lease.try_lock().context("private runtime lease unavailable")?;
  let target=directory.path().join(if cfg!(windows){"duckdb.exe"}else{"duckdb"});let mut output=std::fs::OpenOptions::new().create_new(true).write(true).open(&target)?;private_permissions(&target,0o700)?;std::io::copy(&mut source.reader()?,&mut output)?;output.sync_all()?;drop(output);
  let copy=file_identity::BoundFile::open(&target)?;ensure!(copy.sha256()?==expected,"private DuckDB executable copy differs");source.guard()?;manifest.guard()?;
  let version=std::process::Command::new(copy.path()).arg("--version").stdin(std::process::Stdio::null()).output()?;ensure!(version.status.success()&&String::from_utf8(version.stdout)?.trim()==receipt["version_output"].as_str().context("runtime version evidence absent")?,"DuckDB executable startup version differs");
  let runtime=Self{verified:Arc::new(VerifiedRuntime{source,manifest,copy,_lease:lease,_directory:directory}),evidence:receipt};runtime.guard()?;cache.push_back(runtime.clone());Ok(runtime)
 }
}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]pub struct SourceVolumeAggregate {pub known_volume_sum:String,pub known_count:String,pub total_count:String,pub policy:String}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]pub struct Aggregate {
 /// row_count and known_volume_count count bars; component coverage is separate.
 pub row_count:String,pub known_volume_count:String,pub known_component_count:String,pub component_count:String,pub final_count:String,pub volume_complete:bool,
 pub open:Option<String>,pub high:Option<String>,pub low:Option<String>,pub close:Option<String>,pub known_volume_sum:Option<String>,
 pub first_open_time_ns:Option<String>,pub last_open_time_ns:Option<String>,pub max_revision:Option<String>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub source_volume_components:Option<SourceVolumeAggregate>,
 #[serde(default,skip_serializing_if="Vec::is_empty")]pub source_volume_component_groups:Vec<SourceVolumeAggregate>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub source_summary_bars:Option<String>,
 #[serde(default,skip_serializing_if="Option::is_none")]pub source_summary_unavailable_bars:Option<String>,
}
#[derive(Clone,Debug,Serialize,Deserialize)]pub struct QueryResult{pub snapshot_id:String,pub start_ns:String,pub end_ns:String,pub engine:String,pub fallback_reason:Option<String>,pub result:Aggregate,pub runtime:Option<Value>}
fn trusted_range(snapshot:&Published,start:i64,end:i64)->Result<()>{ensure!(start<=end,"columnar range inverted");let scope=&snapshot.manifest.scope["scan"];let lo=scope["start_ns"].as_str().context("manifest range absent")?.parse::<i64>()?;let hi=scope["end_ns"].as_str().context("manifest range absent")?.parse::<i64>()?;ensure!(start>=lo&&end<=hi,"query range exceeds fixed snapshot scope");Ok(())}
fn quick_safe(snapshot:&Published)->bool{snapshot.manifest.source_component_rows.unwrap_or(0)==0&&snapshot.manifest.volume_semantics.as_ref().is_some_and(|proof|proof.nullable_volume_equivalent&&proof.known_component_count.parse::<u128>().is_ok_and(|v|v<=i128::MAX as u128)&&proof.component_count.parse::<u128>().is_ok_and(|v|v<=i128::MAX as u128))&& (snapshot.manifest.summary.row_count==0||(["open","high","low","close","volume"].iter().all(|name|snapshot.manifest.columns.get(*name).is_some_and(|p|p.decimal38_18_unrepresentable==0&&p.known_count.checked_add(p.null_count)==Some(snapshot.manifest.summary.row_count)))&&snapshot.manifest.columns.get("volume").is_some_and(|p|p.sum_decimal38_18_safe)))}
fn quote(value:&str)->String{format!("'{}'",value.replace('\'',"''"))}
fn sql(path:&str,start:i64,end:i64)->Result<String>{
 let path=quote(path);
 Ok(format!("SET threads=2; SET memory_limit='512MiB'; SET max_temp_directory_size='0B'; SET autoinstall_known_extensions=false; SET autoload_known_extensions=false; SET allow_community_extensions=false; SET allowed_paths=[{path}]; SET enable_external_access=false; SET lock_configuration=true; SELECT CAST(count(*) AS VARCHAR) AS row_count,CAST(count(volume_decimal38_18) AS VARCHAR) AS known_volume_count,CAST(coalesce(sum(known_volume_count),0) AS VARCHAR) AS known_component_count,CAST(coalesce(sum(component_count),0) AS VARCHAR) AS component_count,CAST(count(*) FILTER(WHERE state='final') AS VARCHAR) AS final_count, CAST(arg_min(open_decimal38_18,open_time_ns) AS VARCHAR) AS open,CAST(max(high_decimal38_18) AS VARCHAR) AS high,CAST(min(low_decimal38_18) AS VARCHAR) AS low,CAST(arg_max(close_decimal38_18,open_time_ns) AS VARCHAR) AS close,CAST(sum(volume_decimal38_18) AS VARCHAR) AS known_volume_sum,CAST(min(open_time_ns) AS VARCHAR) AS first_open_time_ns,CAST(max(open_time_ns) AS VARCHAR) AS last_open_time_ns,CAST(max(revision) AS VARCHAR) AS max_revision FROM read_parquet({path}) WHERE open_time_ns>={start} AND open_time_ns<{end};"))
}
async fn execute(runtime:Runtime,query:String,cancel:Cancel,data:std::fs::File)->Result<Value>{
 let _permit=WORKERS.clone().try_acquire_owned().context("columnar worker budget exhausted")?;ensure!(!cancel(),"columnar query cancelled");
 // Explicit empty init prevents the CLI from loading a user's ~/.duckdbrc.
 runtime.guard()?;let init=tempfile::NamedTempFile::new()?;let mut command=tokio::process::Command::new(runtime.verified.copy.path());command.args(["-json","-batch","-init"]).arg(init.path()).args([":memory:","-c",&query]).kill_on_drop(true).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::piped()).stderr(std::process::Stdio::piped());
 file_identity::inherit(&data,&mut command);let child=command.spawn()?;let output=child.wait_with_output();tokio::pin!(output);let deadline=tokio::time::sleep(Duration::from_secs(60));tokio::pin!(deadline);
 let result=loop{tokio::select!{result=&mut output=>break result?,_=&mut deadline=>anyhow::bail!("columnar query exceeded 60s budget"),_=tokio::time::sleep(Duration::from_millis(20))=>ensure!(!cancel(),"columnar query cancelled")}};
 runtime.guard()?;ensure!(result.stdout.len()<=1024*1024&&result.stderr.len()<=65536,"columnar worker output exceeds bounded contract");ensure!(result.status.success(),"columnar worker failed: {}",String::from_utf8_lossy(&result.stderr));let rows:Vec<Value>=serde_json::from_slice(&result.stdout)?;ensure!(rows.len()==1,"columnar aggregate result count differs");Ok(rows.into_iter().next().unwrap())
}
fn exact_text(value:&Value)->Result<Option<String>>{Ok(value.as_str().map(|v|Decimal::from_str_exact(v).map(|v|v.to_string())).transpose()?)}
fn from_sql(value:Value)->Result<Aggregate>{let count=value["row_count"].as_str().context("SQL count not string")?.to_owned();let known=value["known_volume_count"].as_str().context("SQL count not string")?.to_owned();let known_components=value["known_component_count"].as_str().context("SQL component count not string")?.to_owned();let components=value["component_count"].as_str().context("SQL component count not string")?.to_owned();Ok(Aggregate{volume_complete:count==known&&known_components==components,row_count:count,known_volume_count:known,known_component_count:known_components,component_count:components,final_count:value["final_count"].as_str().context("SQL count not string")?.into(),open:exact_text(&value["open"])?,high:exact_text(&value["high"])?,low:exact_text(&value["low"])?,close:exact_text(&value["close"])?,known_volume_sum:exact_text(&value["known_volume_sum"])?,first_open_time_ns:value["first_open_time_ns"].as_str().map(str::to_owned),last_open_time_ns:value["last_open_time_ns"].as_str().map(str::to_owned),max_revision:value["max_revision"].as_str().map(str::to_owned),source_volume_components:None,source_volume_component_groups:vec![],source_summary_bars:None,source_summary_unavailable_bars:None})}
pub fn exact_aggregate(snapshot:&Published,start:i64,end:i64,cancel:&Cancel)->Result<Aggregate>{
 trusted_range(snapshot,start,end)?;let (mut count,mut known,mut finals)=(0u64,0u64,0u64);let (mut open,mut high,mut low,mut close,mut sum)=(None,None::<Decimal>,None::<Decimal>,None,None::<Decimal>);let (mut first,mut last,mut revision)=(None,None,None::<u64>);let(mut known_components,mut components)=(num_bigint::BigInt::from(0),num_bigint::BigInt::from(0));
 let mut source_summaries=std::collections::BTreeMap::<String,(Decimal,num_bigint::BigInt,num_bigint::BigInt)>::new();let mut source_bars=0u64;
 batch_snapshot::scan_quant(snapshot,1000,cancel,|batch|{for row in batch.bars{batch_snapshot::validate_volume(&row)?;let at=row.open_time.timestamp_nanos_opt().context("aggregate time outside exact ns")?;if at<start||at>=end{continue}count+=1;let groups=row.source_volume_components.iter().chain(row.source_volume_component_groups.iter());if row.source_volume_components.is_some()||!row.source_volume_component_groups.is_empty(){source_bars+=1;}
 for group in groups{let summary=source_summaries.entry(group.policy.clone()).or_insert_with(||(Decimal::ZERO,num_bigint::BigInt::from(0),num_bigint::BigInt::from(0)));summary.0=summary.0.clone()+Decimal::wide(group.known_volume_sum.clone());summary.1+=group.known_count;summary.2+=group.total_count;}ensure!(source_summaries.len()<=8,"aggregate contains too many distinct source volume policies");finals+=u64::from(row.state=="final");if first.is_none(){first=Some(at);open=Some(Decimal::wide(row.open).to_string());}last=Some(at);close=Some(Decimal::wide(row.close).to_string());let h=Decimal::wide(row.high);let l=Decimal::wide(row.low);if high.as_ref().is_none_or(|old|h>*old){high=Some(h)}if low.as_ref().is_none_or(|old|l<*old){low=Some(l)}known+=u64::from(row.volume.is_some());known_components+=row.known_volume_count;components+=row.component_count;if row.known_volume_count>0{let v=Decimal::wide(row.known_volume_sum);sum=Some(sum.take().map_or(v.clone(),|old|old+v));}revision=Some(revision.map_or(row.revision,|old|old.max(row.revision)));}Ok(())})?;
 let mut groups=source_summaries.into_iter().map(|(policy,(sum,known,total))|SourceVolumeAggregate{known_volume_sum:sum.to_string(),known_count:known.to_string(),total_count:total.to_string(),policy}).collect::<Vec<_>>();let single=if groups.len()==1{groups.pop()}else{None};
 Ok(Aggregate{row_count:count.to_string(),known_volume_count:known.to_string(),known_component_count:known_components.to_string(),component_count:components.to_string(),final_count:finals.to_string(),volume_complete:known==count&&known_components==components,open,high:high.map(|v|v.to_string()),low:low.map(|v|v.to_string()),close,known_volume_sum:sum.map(|v|v.to_string()),first_open_time_ns:first.map(|v|v.to_string()),last_open_time_ns:last.map(|v|v.to_string()),max_revision:revision.map(|v|v.to_string()),source_volume_components:single,source_volume_component_groups:groups,source_summary_bars:(source_bars>0).then(||source_bars.to_string()),source_summary_unavailable_bars:(source_bars>0).then(||(count-source_bars).to_string())})
}
pub async fn aggregate(snapshot:Published,start:i64,end:i64,runtime:Option<Runtime>,cancel:Cancel)->Result<QueryResult>{
 trusted_range(&snapshot,start,end)?;let input=snapshot.clone();tokio::task::spawn_blocking(move||input.guard()).await??;
 let reason=if !quick_safe(&snapshot){Some("source sample policy groups, nullable total volume equivalence, DECIMAL(38,18) values or intermediate SUM cannot be proven exact".to_owned())}else if runtime.is_none(){Some("pinned DuckDB runtime unavailable; exact Rust fallback".to_owned())}else{None};
 let (result,engine,evidence)=if let Some(reason)=reason.clone(){let input=snapshot.clone();let token=cancel.clone();(tokio::task::spawn_blocking(move||exact_aggregate(&input,start,end,&token)).await??,"rust_exact_coefficient_scale",Some(json!({"reason":reason})))}else{let runtime=runtime.unwrap();let data=snapshot.reader()?;let path=file_identity::BoundFile::child_path(&data,&snapshot.directory.join("facts.parquet"))?;let result=execute(runtime.clone(),sql(&path,start,end)?,cancel,data).await?;snapshot.guard()?;(from_sql(result)?,"duckdb-1.5.6-decimal38_18",Some(runtime.evidence))};
 Ok(QueryResult{snapshot_id:snapshot.manifest.id,start_ns:start.to_string(),end_ns:end.to_string(),engine:engine.into(),fallback_reason:reason,result,runtime:evidence})
}

#[cfg(test)]mod verification_tests{
 use super::*;
 #[test]fn counterfeit_executable_and_matching_receipt_cannot_replace_official_pin()->Result<()>{
  use sha2::{Digest,Sha256};let dir=tempfile::tempdir()?;let executable=dir.path().join("duckdb");let bytes=b"jointly altered executable";std::fs::write(&executable,bytes)?;
  let receipt=json!({"version":"1.5.6","platform":"osx-arm64","archive_sha256":"8e0f6825653f8d057922e6147db920bebf072cb41f4b041fd35521c18d7d126e","executable_sha256":hex::encode(Sha256::digest(bytes)),"version_output":"v1.5.6 (Variegata) 069cc9f9b5"});std::fs::write(dir.path().join("manifest.json"),serde_json::to_vec(&receipt)?)?;
  let error=Runtime::open(executable).err().context("counterfeit runtime accepted")?;ensure!(error.to_string().contains("verified official archive extraction"),"counterfeit rejected after execution instead of by trusted pin: {error}");Ok(())
 }
 #[test]#[ignore="requires installed pinned native DuckDB"]fn runtime_cache_rejects_changed_executable_and_receipt()->Result<()>{
  use std::io::Write;let installed=Runtime::installed()?;
  for manifest_change in [false,true]{
   let dir=tempfile::tempdir()?;let executable=dir.path().join(if cfg!(windows){"duckdb.exe"}else{"duckdb"});std::fs::copy(installed.verified.source.path(),&executable)?;std::fs::copy(installed.verified.manifest.path(),dir.path().join("manifest.json"))?;
   let runtime=Runtime::open(executable.clone())?;runtime.guard()?;let reused=Runtime::open(executable.clone())?;ensure!(Arc::ptr_eq(&runtime.verified,&reused.verified),"unchanged runtime repeated startup verification");
   #[cfg(unix)]{let metadata=std::fs::metadata(runtime.verified.copy.path())?;use std::os::unix::fs::PermissionsExt;ensure!(metadata.permissions().mode()&0o777==0o700,"private executable permission differs");
    if manifest_change{std::fs::OpenOptions::new().append(true).open(dir.path().join("manifest.json"))?.write_all(b" ")?;}else{let replacement=dir.path().join("replacement");std::fs::copy(&executable,&replacement)?;std::fs::rename(replacement,&executable)?;}
    ensure!(runtime.guard().is_err()&&Runtime::open(executable).is_err(),"runtime mutation reused old evidence");
   }
  }Ok(())
 }
 #[tokio::test]#[ignore="requires installed pinned native DuckDB"]async fn worker_cancellation_releases_capacity_and_preserves_verified_runtime()->Result<()>{
  let runtime=Runtime::installed()?;let cancelled=Arc::new(std::sync::atomic::AtomicBool::new(false));let flag=cancelled.clone();let token:Cancel=Arc::new(move||flag.load(std::sync::atomic::Ordering::Acquire));let file=tempfile::tempfile()?;
  let task=tokio::spawn(execute(runtime.clone(),"SET threads=1; SELECT CAST(sum(i) AS VARCHAR) AS total FROM range(100000000000) t(i)".into(),token,file));
  tokio::time::sleep(Duration::from_millis(50)).await;cancelled.store(true,std::sync::atomic::Ordering::Release);ensure!(tokio::time::timeout(Duration::from_secs(2),task).await??.is_err(),"cancelled worker stayed active");ensure!(WORKERS.available_permits()==2,"cancelled worker leaked capacity");runtime.guard()?;Ok(())
 }
}
