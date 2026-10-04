//! Measures the controlled deployed worker, including the HTTP handler's validation boundaries.
#[path="../src/batch_snapshot.rs"] mod batch_snapshot;
#[path="../src/capture.rs"] mod capture;
#[path="../src/columnar_query.rs"] mod columnar_query;
use anyhow::{Result,ensure,Context};
use serde_json::json;
use std::{sync::Arc,path::PathBuf,time::Instant};
use sha2::{Digest,Sha256};
fn never()->batch_snapshot::Cancel{Arc::new(||false)}
fn stats(values:&[f64])->serde_json::Value{let mut sorted=values.to_vec();sorted.sort_by(f64::total_cmp);let at=|p:f64|sorted[((sorted.len() as f64*p).ceil() as usize).saturating_sub(1)];json!({"n":values.len(),"p50_ms":at(0.5),"p95_ms":at(0.95),"min_ms":sorted[0],"max_ms":sorted[sorted.len()-1]})}
#[tokio::main]async fn main()->Result<()>{
 let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==6,"expected snapshot-root snapshot-id start-ns end-ns samples report-file");
 let root=PathBuf::from(&args[0]);let id=args[1].clone();let start=args[2].parse::<i64>()?;let end=args[3].parse::<i64>()?;let samples=args[4].parse::<usize>()?;ensure!((3..=30).contains(&samples),"samples outside 3..30 bound");
 let snapshot=batch_snapshot::read(&root,&id)?;let input=snapshot.clone();let oracle=tokio::task::spawn_blocking(move||columnar_query::exact_aggregate(&input,start,end,&never())).await??;
 let (mut read_ms,mut runtime_ms,mut query_ms,mut total_ms,mut rust_ms)=(Vec::new(),Vec::new(),Vec::new(),Vec::new(),Vec::new());let mut last=None;
 for _ in 0..samples{
  let total=Instant::now();let at=Instant::now();let base=root.clone();let requested=id.clone();let verified=tokio::task::spawn_blocking(move||batch_snapshot::read(&base,&requested)).await??;read_ms.push(at.elapsed().as_secs_f64()*1000.0);
  let at=Instant::now();let runtime=tokio::task::spawn_blocking(columnar_query::Runtime::installed).await??;runtime_ms.push(at.elapsed().as_secs_f64()*1000.0);
  let at=Instant::now();let result=columnar_query::aggregate(verified,start,end,Some(runtime),never()).await?;query_ms.push(at.elapsed().as_secs_f64()*1000.0);total_ms.push(total.elapsed().as_secs_f64()*1000.0);ensure!(result.result==oracle,"native deployed aggregate differs from projected arbitrary-precision oracle");last=Some(result);
 }
 for _ in 0..3{let input=snapshot.clone();let at=Instant::now();let result=tokio::task::spawn_blocking(move||columnar_query::exact_aggregate(&input,start,end,&never())).await??;rust_ms.push(at.elapsed().as_secs_f64()*1000.0);ensure!(result==oracle,"repeated Rust aggregate changed immutable result");}
 let result=last.context("no query result")?;let report=json!({"kind":"actual_controlled_native_columnar_worker","snapshot_id":id,"manifest_schema":snapshot.manifest.schema,"file_sha256":snapshot.manifest.file_sha256,"file_bytes":snapshot.manifest.file_bytes.to_string(),"manifest_rows":snapshot.manifest.summary.row_count.to_string(),"range":{"start_ns":start.to_string(),"end_ns":end.to_string()},"handler_initial_manifest_full_file_verify":stats(&read_ms),"installed_runtime_executable_sha_and_version_startup_verify":stats(&runtime_ms),"aggregate_reverify_worker_spawn_ipc_decode_or_exact_fallback":stats(&query_ms),"handler_end_to_end_without_network":stats(&total_ms),"rust_projected_exact_aggregate_no_repeated_file_validation":stats(&rust_ms),"result_sha256":hex::encode(Sha256::digest(serde_json::to_vec(&oracle)?)),"result":result,"oracle_match_all_samples":true,"scope":"same modules and ordered boundaries as HTTP aggregate; excludes only network transport and JSON HTTP serialization; warm OS cache, native macOS, fleet may build concurrently; not isolated SLO"});
 let mut output=std::fs::File::create(&args[5])?;serde_json::to_writer_pretty(&mut output,&report)?;output.sync_all()?;Ok(())
}
