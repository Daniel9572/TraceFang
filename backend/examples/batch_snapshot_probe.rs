#[path="../src/batch_snapshot.rs"]mod batch_snapshot;
#[path="../src/capture.rs"]mod capture;
#[path="../src/columnar_query.rs"]mod columnar_query;
use anyhow::{Result,Context,ensure};
use std::{sync::{Arc,Mutex},path::PathBuf};
use tracefang_core::{native_store::Store,persistence_contract::*,periods::Period};
use serde_json::{json,Value};
fn never()->batch_snapshot::Cancel{Arc::new(||false)}
fn progress()->batch_snapshot::Progress{Arc::new(|_|{})}
async fn plan(store:&Store,symbol:&str,source:&str)->Result<batch_snapshot::Plan>{Ok(batch_snapshot::Plan{scan:CanonicalScanRequest{symbol:symbol.into(),source_id:source.into(),interval_seconds:60,start_ns:i64::MIN,end_ns:i64::MAX,final_only:false,expected_version:Some(store.version().await?)},period:Period::M1,schedule:None,resume:None,build:tracefang_core::quant_core::results::backend_build_fingerprint()})}
#[tokio::main]async fn main()->Result<()>{
 let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==6,"expected facts-file generation symbol source snapshot-root report-file");
 let store=Store::open_read_only(&args[0])?.read_generation(&args[1]).await?;let plan=plan(&store,&args[2],&args[3]).await?;let root=PathBuf::from(&args[4]);let started=std::time::Instant::now();
 let published=batch_snapshot::publish(&root,&store,plan.clone(),never(),progress()).await?;let publish_ms=started.elapsed().as_secs_f64()*1000.0;
 store.close().await?; // Explicitly close authority before consuming immutable rows.
 let snapshot=published.clone();let started=std::time::Instant::now();let summary=tokio::task::spawn_blocking(move||batch_snapshot::scan(&snapshot,1000,&never(),|_|Ok(()))).await??;let scan_ms=started.elapsed().as_secs_f64()*1000.0;
 let reopened=batch_snapshot::read(&root,&published.manifest.id)?;ensure!(summary.sha256==reopened.manifest.summary.sha256,"reopen digest differs");let projected=reopened.clone();let started=std::time::Instant::now();let projected_summary=tokio::task::spawn_blocking(move||batch_snapshot::scan_quant(&projected,1000,&never(),|_|Ok(()))).await??;let projected_scan_ms=started.elapsed().as_secs_f64()*1000.0;ensure!(serde_json::to_value(&projected_summary)?==serde_json::to_value(&summary)?,"projected scan changed immutable summary");
 let report=json!({"kind":"actual_native_parquet_fixed_mvcc","source_file":args[0],"generation":args[1],"symbol":args[2],"source_id":args[3],"manifest":published.manifest,"snapshot_directory":published.directory,"publish_ms":publish_ms,"whole_audit_scan_ms":scan_ms,"projected_analytic_scan_ms":projected_scan_ms,"authority_closed_before_consume":true,"precision":"exact arbitrary-width coefficient + scale; optional Decimal38_18 never substitutes unrepresentable values","environment":"native macOS; parallel fleet builds; offline export/verification timings are not read SLO"});
 let mut file=std::fs::File::create(&args[5])?;serde_json::to_writer_pretty(&mut file,&report)?;file.sync_all()?;Ok(())
}
#[cfg(test)]mod tests{
 use super::*;
 fn row(minute:i64,price:&str,volume:Option<&str>,revision:u64)->ImportBarRow{let ns=minute*60_000_000_000;ImportBarRow{instrument_symbol:"SIGNED".into(),realtime_source_id:"source".into(),evidence_channel_id:"native".into(),interval_seconds:60,open_time_ns:ns,close_time_ns:ns+60_000_000_000,open:price.into(),high:price.into(),low:price.into(),close:price.into(),volume:volume.map(str::to_owned),revision,received_sequence:Some(u64::MAX),state:"final".into(),finalized_at_ns:Some(ns+60_000_000_000),source_observed_at_ns:9_007_199_254_740_993,received_at_ns:9_007_199_254_740_999,source_metadata:json!({"provider":"source","provider_symbol":"SIGNED","raw_payload":null}),evidence:json!({"line":"immutable-original"})}}
 fn batch(offset:u64,rows:Vec<ImportBarRow>)->ImportBatch<ImportBarRow>{ImportBatch{context:ImportContext{origin_id:"fixed".into(),source_fingerprint:"evidence".into(),schema_version:SCHEMA_VERSION.into(),range_label:"bars".into(),legacy_cursor:None,expected_sha256:None},row_offset:offset,rows}}
 fn collect(snapshot:&batch_snapshot::Published)->Result<Vec<Value>>{let mut rows=Vec::new();batch_snapshot::scan(snapshot,2,&never(),|batch|{rows.extend(batch.rows.into_iter().map(|row|serde_json::to_value(row).unwrap()));Ok(())})?;Ok(rows)}
 #[tokio::test]async fn exact_wide_null_ns_and_u64_survive_parquet_reopen_and_reuse()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;let wide="79228162514264337593543950335.0000000000000000000000000001";
  store.import_bars(batch(0,vec![row(-1,"-1690",Some(wide),u64::MAX),row(0,"0",None,1),row(1,"1.0000000000000000000000000001",Some("0"),1)])).await?;
  let root=dir.path().join("snapshots");let p=plan(&store,"SIGNED","source").await?;let published=batch_snapshot::publish(&root,&store,p.clone(),never(),progress()).await?;
  ensure!(published.manifest.summary.row_count==3&&published.manifest.columns["volume"].null_count==1,"NULL rows lost");ensure!(published.manifest.columns["volume"].decimal38_18_unrepresentable==1&&!published.manifest.columns["volume"].sum_decimal38_18_safe,"wide value incorrectly enabled SQL aggregate");
  let rows=collect(&published)?;ensure!(rows[0]["volume"]==wide&&rows[0]["revision"]==u64::MAX.to_string()&&rows[0]["received_sequence"]==u64::MAX.to_string(),"wide numeric/u64 changed");ensure!(rows[0]["source_observed_at_ns"]=="9007199254740993"&&rows[0]["open_time_ns"]=="-60000000000","ns narrowed");ensure!(rows[1]["volume"].is_null()&&rows[2]["volume"]=="0","NULL became zero");
  let mut projected=Vec::new();batch_snapshot::scan_quant(&published,2,&never(),|batch|{projected.extend(batch.bars.into_iter().map(|bar|serde_json::to_value(bar).unwrap()));Ok(())})?;let oracle=rows.iter().map(|row|{let row:ImportBarRow=serde_json::from_value(row.clone()).unwrap();serde_json::to_value(batch_snapshot::project_bar(&row).unwrap()).unwrap()}).collect::<Vec<_>>();ensure!(projected==oracle&&published.manifest.projected_quant_sha256.is_some(),"projected analytic columns differ from fully audited exact adapter");
  let reused=batch_snapshot::publish(&root,&store,p,never(),progress()).await?;ensure!(reused.manifest.id==published.manifest.id,"same exact authority did not reuse immutable version");store.close().await?;let reopened=batch_snapshot::read(&root,&published.manifest.id)?;ensure!(collect(&reopened)?==rows,"rows changed after authority closed/reopen");Ok(())
 }
 #[tokio::test]async fn correction_creates_new_version_old_rows_stay_immutable_and_stale_mvcc_rejects()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;store.import_bars(batch(0,vec![row(0,"1",Some("0"),1)])).await?;let root=dir.path().join("snapshots");let oldplan=plan(&store,"SIGNED","source").await?;let old=batch_snapshot::publish(&root,&store,oldplan.clone(),never(),progress()).await?;
  store.import_bars(batch(1,vec![row(0,"2",None,2)])).await?;let new=batch_snapshot::publish(&root,&store,plan(&store,"SIGNED","source").await?,never(),progress()).await?;ensure!(new.manifest.id!=old.manifest.id&&collect(&old)?[0]["close"]=="1"&&collect(&new)?[0]["close"]=="2","correction rewrote old input");
  // Existing immutable old version is valid; an uncached stale MVCC request is not.
  let mut stale=oldplan;stale.build="different-build".into();ensure!(batch_snapshot::publish(&root,&store,stale,never(),progress()).await.is_err(),"stale expected version silently exported newer data");ensure!(!std::fs::read_dir(&root)?.any(|entry|entry.unwrap().file_name().to_string_lossy().starts_with(".pending-")),"failed candidate left pending artifact");store.close().await?;Ok(())
 }
 #[tokio::test]async fn cancellation_never_publishes_partial_and_empty_metadata_is_preserved()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;store.import_bars(batch(0,(0..1500).map(|minute|row(minute,"1",None,1)).collect())).await?;let root=dir.path().join("cancel");let cancelled=Arc::new(std::sync::atomic::AtomicBool::new(false));let flag=cancelled.clone();let cancel:batch_snapshot::Cancel=Arc::new(move||flag.load(std::sync::atomic::Ordering::Acquire));let hook:batch_snapshot::Progress=Arc::new(move|value|if value["phase"]=="exporting_immutable_input"{cancelled.store(true,std::sync::atomic::Ordering::Release)});
  ensure!(batch_snapshot::publish(&root,&store,plan(&store,"SIGNED","source").await?,cancel,hook).await.is_err(),"cancelled partial input published");ensure!(std::fs::read_dir(&root)?.next().is_none(),"cancelled publication leaked files");
  let empty=batch_snapshot::publish(&dir.path().join("empty"),&store,plan(&store,"ABSENT","source").await?,never(),progress()).await?;let mut seen=false;batch_snapshot::scan(&empty,100,&never(),|batch|{ensure!(batch.rows.is_empty()&&batch.context.is_some(),"empty metadata lost");seen=true;Ok(())})?;ensure!(seen&&empty.manifest.summary.row_count==0,"empty range omitted context");store.close().await?;Ok(())
 }
 #[tokio::test]async fn manifest_path_or_file_tampering_is_rejected()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;let root=dir.path().join("snapshots");let published=batch_snapshot::publish(&root,&store,plan(&store,"ABSENT","source").await?,never(),progress()).await?;
  ensure!(batch_snapshot::read(&root,"../facts.redb").is_err(),"arbitrary path accepted");use std::io::Write;std::fs::OpenOptions::new().append(true).open(published.directory.join("facts.parquet"))?.write_all(b"tamper")?;ensure!(batch_snapshot::read(&root,&published.manifest.id).is_err(),"mutated parquet accepted");store.close().await?;Ok(())
 }
 #[cfg(unix)]#[tokio::test]async fn verification_cache_rejects_replace_inplace_truncate_and_manifest_edit()->Result<()>{
  use std::io::{Write,Seek,SeekFrom};
  for change in ["replace","inplace","truncate","manifest"]{
   let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;let root=dir.path().join("snapshots");let published=batch_snapshot::publish(&root,&store,plan(&store,"ABSENT","source").await?,never(),progress()).await?;
   let warm=batch_snapshot::read(&root,&published.manifest.id)?;ensure!(collect(&warm)?.is_empty(),"warm exact rows differ");let path=published.directory.join(if change=="manifest"{"manifest.json"}else{"facts.parquet"});
   match change{"replace"=>{let copy=path.with_extension("replacement");std::fs::copy(&path,&copy)?;std::fs::rename(copy,&path)?;},"inplace"=>{let mut file=std::fs::OpenOptions::new().write(true).open(&path)?;file.seek(SeekFrom::Start(4))?;file.write_all(b"x")?;file.sync_all()?;},"truncate"=>std::fs::OpenOptions::new().write(true).open(&path)?.set_len(8)?,"manifest"=>std::fs::OpenOptions::new().append(true).open(&path)?.write_all(b" ")?,_=>unreachable!()}
   ensure!(batch_snapshot::read(&root,&published.manifest.id).is_err()&&collect(&warm).is_err(),"changed file reused old validation: {change}");store.close().await?;
  }Ok(())
 }
 #[tokio::test]async fn wide_aggregate_uses_exact_fallback_and_preserves_unknown_volume()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;let wide="79228162514264337593543950335.0000000000000000000000000001";
  store.import_bars(batch(0,vec![row(-1,"-1",Some(wide),1),row(0,"0",None,1),row(1,"1",Some("0.0000000000000000000000000001"),1)])).await?;
  let published=batch_snapshot::publish(&dir.path().join("snapshots"),&store,plan(&store,"SIGNED","source").await?,never(),progress()).await?;store.close().await?;
  let result=columnar_query::aggregate(published,-60_000_000_000,120_000_000_000,None,never()).await?;ensure!(result.engine=="rust_exact_coefficient_scale"&&result.result.known_volume_sum.as_deref()==Some("79228162514264337593543950335.0000000000000000000000000002")&&!result.result.volume_complete&&result.result.row_count=="3"&&result.result.known_volume_count=="2","wide SUM truncated or unknown volume hidden");Ok(())
 }
 #[tokio::test]async fn day_week_partial_unknown_and_known_zero_keep_bar_and_component_coverage_separate()->Result<()>{
  for volumes in [[Some("2"),None,Some("0")],[None,None,None],[Some("0"),Some("0"),Some("0")]] {
   let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;store.import_bars(batch(0,volumes.iter().enumerate().map(|(minute,volume)|row(minute as i64,"1",*volume,1)).collect())).await?;
   for period in [Period::D1,Period::W1] {let mut p=plan(&store,"SIGNED","source").await?;p.period=period;let published=batch_snapshot::publish(&dir.path().join("snapshots"),&store,p,never(),progress()).await?;let result=columnar_query::aggregate(published,i64::MIN,i64::MAX,None,never()).await?.result;
    let known=volumes.iter().filter(|v|v.is_some()).count();ensure!(result.row_count=="1"&&result.component_count=="3"&&result.known_component_count==known.to_string()&&result.known_volume_count==if known==3{"1"}else{"0"},"day/week counts confused bars and source components");ensure!(result.volume_complete==(known==3)&&result.known_volume_sum.as_deref()==match known{0=>None,2=>Some("2"),3=>Some("0"),_=>unreachable!()},"partial, all unknown or known zero SUM changed");
   }store.close().await?;
  }Ok(())
 }
 #[tokio::test]async fn source_policy_partial_unknown_zero_groups_and_correction_survive_cold_projection()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;
  let component=|sum:&str,known:u64,total:u64,policy:&str|json!({"known_volume_sum":sum,"known_count":known.to_string(),"total_count":total.to_string(),"policy":policy});
  let mut partial=row(0,"1",None,1);partial.source_metadata["raw_payload"]=json!({"source_volume_components":component("2",1,2,"fuyao-minute-interval-samples-v1")});
  let mut unknown=row(1,"1",None,1);unknown.source_metadata["raw_payload"]=json!({"source_volume_components":component("0",0,2,"fuyao-minute-interval-samples-v1")});
  let mut zero=row(2,"1",Some("0"),1);zero.source_metadata["raw_payload"]=json!({"source_volume_components":component("0",1,1,"canonical-minute-fallback-v1")});
  store.import_bars(batch(0,vec![partial.clone(),unknown,zero])).await?;
  let root=dir.path().join("snapshots");let published=batch_snapshot::publish(&root,&store,plan(&store,"SIGNED","source").await?,never(),progress()).await?;
  let mut projected=Vec::new();batch_snapshot::scan_quant(&published,2,&never(),|b|{projected.extend(b.bars);Ok(())})?;
  ensure!(projected[0].component_count==1&&projected[0].known_volume_count==0&&projected[0].volume.is_none()&&projected[0].source_volume_components.as_ref().unwrap().known_count==1,"source sample count replaced minute count");
  let result=columnar_query::aggregate(published.clone(),i64::MIN,i64::MAX,None,never()).await?.result;ensure!(result.component_count=="3"&&result.known_component_count=="1"&&result.known_volume_count=="1"&&result.known_volume_sum.as_deref()==Some("0"),"source partial volume changed canonical minute NULL policy");
  ensure!(result.source_volume_components.is_none()&&result.source_volume_component_groups.len()==2,"unlike source policies added together");let fold=result.source_volume_component_groups.iter().find(|g|g.policy=="fuyao-minute-interval-samples-v1").unwrap();ensure!(fold.known_volume_sum=="2"&&fold.known_count=="1"&&fold.total_count=="4","source known partial or unknown coverage lost");
  partial.revision=2;partial.source_metadata["raw_payload"]["source_volume_components"]=component("2.0000000000000000000000000001",1,2,"fuyao-minute-interval-samples-v1");store.import_bars(batch(3,vec![partial])).await?;
  let corrected=batch_snapshot::publish(&root,&store,plan(&store,"SIGNED","source").await?,never(),progress()).await?;store.close().await?;
  let cold=batch_snapshot::read(&root,&corrected.manifest.id)?;let cold_result=columnar_query::exact_aggregate(&cold,i64::MIN,i64::MAX,&never())?;ensure!(cold_result.source_volume_component_groups.iter().any(|g|g.known_volume_sum=="2.0000000000000000000000000001"),"wide source component sum truncated after correction/reopen");ensure!(columnar_query::exact_aggregate(&published,i64::MIN,i64::MAX,&never())?==result,"correction altered earlier immutable source evidence");Ok(())
 }
 #[tokio::test]async fn gc_preserves_durable_audit_pins_and_active_readers_then_reclaims_released_versions()->Result<()>{
  let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;let root=dir.path().join("snapshots");let mut p=plan(&store,"ABSENT","source").await?;
  let audit=batch_snapshot::publish(&root,&store,p.clone(),never(),progress()).await?;let audit_id=audit.manifest.id.clone();batch_snapshot::pin(&root,&audit_id,"audit-fixed-input-1")?;drop(audit);
  p.build="version2".into();let released=batch_snapshot::publish(&root,&store,p.clone(),never(),progress()).await?;let released_id=released.manifest.id.clone();ensure!(batch_snapshot::pin(&root,&released_id,"audit-fixed-input-1").is_err(),"existing audit reference silently rebound to new input");batch_snapshot::pin(&root,&audit_id,"audit-fixed-input-1")?;drop(released);
  p.build="version3".into();let active=batch_snapshot::publish(&root,&store,p,never(),progress()).await?;let active_id=active.manifest.id.clone();
  ensure!(batch_snapshot::gc_to(&root,0,0).is_err(),"GC ignored referenced bytes when budget impossible");ensure!(root.join(&audit_id).is_dir()&&root.join(&active_id).is_dir()&&!root.join(released_id).exists(),"GC deleted audit/active input or failed to reclaim released version");ensure!(collect(&active)?.is_empty(),"active reader broken during GC");drop(active);batch_snapshot::unpin(&root,"audit-fixed-input-1")?;batch_snapshot::gc_to(&root,0,0)?;ensure!(!root.join(audit_id).exists()&&!root.join(active_id).exists(),"released files not reclaimed");store.close().await?;Ok(())
 }
 #[tokio::test]#[ignore="requires installed pinned native DuckDB; run installer then --ignored"]async fn pinned_duckdb_matches_rust_exact_oracle_and_rejects_scope_escape()->Result<()>{
  let runtime=columnar_query::Runtime::installed()?;let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?.staging("isolated").await?;store.import_bars(batch(0,vec![row(-1,"-1690",Some("1.2"),1),row(0,"0",None,2),row(1,"3",Some("0"),3)])).await?;let root=dir.path().join("snapshots");let mut scope=plan(&store,"SIGNED","source").await?;scope.scan.start_ns=-60_000_000_000;scope.scan.end_ns=120_000_000_000;let published=batch_snapshot::publish(&root,&store,scope,never(),progress()).await?;store.close().await?;
  let oracle=columnar_query::exact_aggregate(&published,-60_000_000_000,120_000_000_000,&never())?;let result=columnar_query::aggregate(published.clone(),-60_000_000_000,120_000_000_000,Some(runtime.clone()),never()).await?;ensure!(result.engine=="duckdb-1.5.6-decimal38_18"&&result.result==oracle,"DuckDB Decimal result differs from exact canonical Rust");ensure!(columnar_query::aggregate(published,i64::MIN,120_000_000_000,Some(runtime),never()).await.is_err(),"manifest range escape accepted");Ok(())
 }
}
