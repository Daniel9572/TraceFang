//! Directed raw capture experiment and process-recovery probe. No production resources.
#[path="../src/capture.rs"] mod capture;
use capture::{Capture,CaptureOptions,ProviderFrame};
use anyhow::{Result,ensure};
use chrono::{TimeZone,Utc};
use redb::{Database,Durability,TableDefinition};
use serde_json::json;
use std::{sync::{Arc,atomic::{AtomicBool,Ordering}},path::Path,time::{Instant,Duration}};
use sha2::{Digest,Sha256};
const FACTS:TableDefinition<u64,&[u8]>=TableDefinition::new("probe_facts");
fn frame(seq:u64,size:usize)->ProviderFrame {ProviderFrame{version:1,channel:"probe".into(),connection_id:"one".into(),sequence:seq,
 received_at:Utc.timestamp_opt(1_800_000_000,123456789).unwrap(),encoding:"binary".into(),body:vec![(seq%251) as u8;size]}}
fn rss()->u64 {#[cfg(target_os="macos")] {let mut usage=std::mem::MaybeUninit::<libc::rusage>::uninit();unsafe{libc::getrusage(libc::RUSAGE_SELF,usage.as_mut_ptr());usage.assume_init().ru_maxrss as u64}}
 #[cfg(not(target_os="macos"))] {0}}
fn stats(mut rows:Vec<f64>)->serde_json::Value {rows.sort_by(f64::total_cmp);let n=rows.len();json!({"n":n,"p50_ms":rows[n/2],"p95_ms":rows[((n as f64*0.95).ceil() as usize-1).min(n-1)],"samples_ms":rows})}
#[tokio::main] async fn main()->Result<()> {
 let args:Vec<String>=std::env::args().collect();
 if args.get(1).is_some_and(|v|v=="crash-child") {
  let cap=Capture::open(&args[2],CaptureOptions::default())?;let receipt=cap.append(&frame(1,32*1024*1024)).await?;
  std::fs::write(&args[3],serde_json::to_vec(&receipt)?)?;std::fs::File::open(&args[3])?.sync_all()?;
  #[cfg(unix)] unsafe {libc::kill(libc::getpid(),libc::SIGKILL);}
  return Ok(())
 }
 let directory=args.get(1).map(String::as_str).unwrap_or("/Users/daniel/Library/Caches/TraceFang/capture-directed");
 let report=args.get(2).context("usage: capture_probe OWNED_CACHE_DIR REPORT.json")?;
 let directory=Path::new(directory);std::fs::create_dir_all(directory)?;ensure!(std::fs::read_dir(directory)?.next().is_none(),"directed experiment requires a fresh owned directory");
 std::fs::write(directory.join("tracefang-owned-probe.json"),b"{\"purpose\":\"capture-directed\"}")?;
 let mut reports=vec![];
 for shared in [true,false] {
  let prefix=if shared{"shared"}else{"independent"};let db_path=directory.join(format!("{prefix}-facts.redb"));
  let db=Arc::new(Database::builder().set_cache_size(128*1024*1024).create(&db_path)?);
  let cap=if shared{Capture::open_database(db.clone(),db_path.clone(),CaptureOptions::default())?}else{Capture::open(directory.join(format!("{prefix}-raw.redb")),CaptureOptions::default())?};
  let stop=Arc::new(AtomicBool::new(false));let thread_stop=stop.clone();let thread_db=db.clone();
  let writer=std::thread::spawn(move||->Result<Vec<f64>> {
   let mut times=vec![];let mut seq=0u64;let payload=vec![7u8;128];
   while !thread_stop.load(Ordering::Acquire) {
    let start=Instant::now();let mut tx=thread_db.begin_write()?;tx.set_durability(Durability::Immediate)?;
    {let mut facts=tx.open_table(FACTS)?;for _ in 0..1000{seq+=1;facts.insert(seq,payload.as_slice())?;}}
    tx.commit()?;times.push(start.elapsed().as_secs_f64()*1000.0);std::thread::sleep(Duration::from_millis(2));
   }Ok(times)
  });
  let mut small=vec![];let mut large=vec![];let mut last=None;
  for seq in 1..=90 {
   let size=if seq%10==0{32*1024*1024}else{512};let frame=frame(seq,size);let start=Instant::now();
   let receipt=cap.append(&frame).await?;let time=start.elapsed().as_secs_f64()*1000.0;
   ensure!(receipt.confirmed_at_ns>=receipt.accepted_at_ns,"receipt precedes submission");
   let saved=cap.get_at(&receipt.position).await?;ensure!(saved.frame==frame,"raw frame changed");
   ensure!(hex::encode(Sha256::digest(&saved.frame.body))==hex::encode(Sha256::digest(&frame.body)),"body digest changed");
   if size>512{large.push(time)}else{small.push(time)}last=Some(receipt.position);
  }
  cap.close_and_drain().await?;stop.store(true,Ordering::Release);let facts=writer.join().unwrap()?;
  reports.push(json!({"mode":prefix,"small_512_bytes":stats(small),"large_32_mib":stats(large),"facts_batch_1000_immediate":stats(facts),
   "peak_rss_bytes_process_cumulative":rss(),"last_position":last,"raw_roundtrip_passed":true,"capture_options":{"queue_bytes":67108864,"queue_frames":32,"batch_bytes":4194304,"batch_frames":64}}));
 }
 let child_dir=directory.join("kill-raw.redb");let child_receipt=directory.join("kill-receipt.json");
 let status=std::process::Command::new(std::env::current_exe()?).args(["crash-child"]).arg(&child_dir).arg(&child_receipt).status()?;
 ensure!(!status.success(),"child did not die by forced termination");
 let receipt:tracefang_core::persistence_contract::DurableReceipt=serde_json::from_slice(&std::fs::read(child_receipt)?)?;
 let cap=Capture::open(&child_dir,CaptureOptions::default())?;let saved=cap.get_at(&receipt.position).await?;ensure!(saved.frame==frame(1,32*1024*1024),"committed raw changed after kill");cap.close().await?;
 let result=json!({"candidate":"redb 4.3.0 Immediate","scope":"directed shared writer vs independent raw; includes actual full-frame encoding, commit, sync receipt; no vendor ranking",
  "runs":reports,"process_kill_after_receipt":{"passed":true,"frame_bytes":33554432,"position":receipt.position},
  "limits":["native macOS; other agents/OS activity may continue","peak RSS is process cumulative; independent run comes second","process SIGKILL is not physical power-cut or device failure","captured receive time and precommit acceptance differ from postcommit confirmation"]});
 std::fs::write(report,serde_json::to_vec_pretty(&result)?)?;println!("{}",json!({"passed":true,"report":report}));Ok(())
}
use anyhow::Context;
#[cfg(test)] mod tests {
 use super::*;
 #[tokio::test]async fn forty_eight_mib_encoded_envelope_does_not_deadlock_sixty_four_mib_queue()->Result<()> {
  let dir=tempfile::tempdir()?;let cap=Capture::open(dir.path().join("raw"),Default::default())?;
  let large=frame(1,48*1024*1024);let receipt=tokio::time::timeout(Duration::from_secs(60),cap.append(&large)).await??;
  ensure!(cap.get_at(&receipt.position).await?.frame==large,"48MiB encoded raw truncated");
  ensure!(cap.append(&frame(2,48*1024*1024+1)).await.is_err(),"oversized encoded envelope admitted");cap.close().await?;Ok(())
 }
 #[tokio::test]async fn addressed_bodies_preserve_envelopes_scan_budget_and_legacy_inline()->Result<()> {
  let dir=tempfile::tempdir()?;let path=dir.path().join("raw");let cap=Capture::open(&path,Default::default())?;
  let a=frame(1,32*1024*1024);let mut b=a.clone();b.sequence=2;b.received_at=Utc.timestamp_opt(1_800_000_001,987654321).unwrap();
  let ar=cap.append(&a).await?;let br=cap.append(&b).await?;
  let bounds=cap.bounds().await?;ensure!(bounds["message_count"]=="2"&&bounds["unique_bodies"]=="1","body reuse merged envelopes or rewrote body");
  ensure!(bounds["logical_body_bytes"]=="67108864"&&bounds["unique_body_bytes"]=="33554432","content accounting differs");
  ensure!(cap.scan(&ar.position.epoch,1,None,64,4*1024*1024).await?.len()==1,"references bypass restored payload byte bound");
  ensure!(cap.get_at(&ar.position).await?.frame==a&&cap.get_at(&br.position).await?.frame==b,"dedup lost original bytes or clocks");cap.close().await?;drop(cap);
  let reopened=Capture::open(&path,Default::default())?;ensure!(reopened.get_at(&br.position).await?.frame==b,"body reference did not survive reopen");reopened.close().await?;drop(reopened);
  let db=Database::create(&path)?;let mut tx=db.begin_write()?;tx.set_durability(Durability::Immediate)?;
  {let mut bodies=tx.open_table(TableDefinition::<&str,&[u8]>::new("capture_bodies_sha256_v1"))?;bodies.remove(hex::encode(Sha256::digest(&a.body)).as_str())?;}tx.commit()?;drop(db);
  let broken=Capture::open(&path,Default::default())?;ensure!(broken.get(1).await.is_err(),"missing body silently returned an envelope");broken.close().await?;drop(broken);
  let old_path=dir.path().join("inline");let old=Capture::open(&old_path,CaptureOptions{content_addressed_bodies:false,..Default::default()})?;let oldr=old.append(&frame(9,1024)).await?;old.close().await?;drop(old);
  let mixed=Capture::open(&old_path,Default::default())?;ensure!(mixed.get_at(&oldr.position).await?.frame==frame(9,1024),"v1 inline evidence became unreadable");mixed.append(&frame(10,1024)).await?;ensure!(mixed.scan(&oldr.position.epoch,1,None,4,10000).await?.len()==2,"mixed record schemas skipped a frame");mixed.close().await?;Ok(())
 }
 #[tokio::test] async fn exact_identity_epoch_clock_duplicates_and_drain()->Result<()> {
  let dir=tempfile::tempdir()?;let path=dir.path().join("raw.redb");let cap=Capture::open(&path,CaptureOptions::default())?;
  let mut first=frame(u64::MAX,4);let a=cap.append(&first).await?;ensure!(!a.duplicate,"initial append duplicate");
  ensure!(cap.append(&first).await?.duplicate,"retry duplicated raw");
  first.sequence=1;first.received_at=Utc.timestamp_opt(1_799_999_999,9).unwrap();let b=cap.append(&first).await?;
  let rows=cap.scan(&a.position.epoch,1,None,5,100000).await?;ensure!(rows.len()==2&&rows[0].frame.sequence==u64::MAX,"sequence lost");
  ensure!(rows[0].logical_at_ns==rows[1].logical_at_ns,"wallclock rollback changed logical order");
  ensure!(cap.locate_time(&a.position.epoch,rows[0].logical_at_ns,2).await?.sequence==1,"same logical clock tie differs");
  ensure!(cap.scan("wrong-epoch",1,None,5,100).await.is_err(),"wrong epoch accepted");
  let mut wrong=b.position.clone();wrong.digest="wrong".into();ensure!(cap.get_at(&wrong).await.is_err(),"wrong prefix accepted");
  let bounds=cap.bounds().await?;ensure!(bounds["first_received_at_ns"].as_str().unwrap()!=bounds["last_received_at_ns"].as_str().unwrap(),"bounds mislabeled raw clock");
  ensure!(bounds["last_durable_at_ns"].is_null(),"persisted precommit time mislabeled durable");
  cap.close_and_drain().await?;ensure!(cap.append(&frame(3,1)).await.is_err(),"closed capture admitted frame");drop(cap);
  let reopened=Capture::open(&path,CaptureOptions::default())?;ensure!(reopened.get_at(&a.position).await?.frame.sequence==u64::MAX,"identity changed after reopen");reopened.close().await?;Ok(())
 }
 #[tokio::test] async fn failed_identity_survives_unrelated_success_and_sender_drop()->Result<()> {
  let dir=tempfile::tempdir()?;let cap=Capture::open(dir.path().join("raw"),CaptureOptions::default())?;
  cap.append(&frame(1,2)).await?;ensure!(cap.append(&frame(1,3)).await.is_err(),"conflict accepted");
  cap.append(&frame(2,2)).await?;ensure!(cap.status()["state"]=="degraded","successful B erased failed A");
  let mut watch=cap.status_watch();drop(cap);
  tokio::time::timeout(Duration::from_secs(5),async {loop {if watch.borrow()["state"]=="failed" {break}watch.changed().await?;}Ok::<_,anyhow::Error>(())}).await??;
  ensure!(watch.borrow()["drained"]==false,"sender drop erased failure");
  let cap=Capture::open(dir.path().join("retry"),CaptureOptions::default())?;
  cap.append(&frame(1,2)).await?;ensure!(cap.append(&frame(1,3)).await.is_err(),"conflict accepted");
  cap.append(&frame(2,2)).await?;ensure!(cap.status()["state"]=="degraded","unrelated success erased A");
  ensure!(cap.append(&frame(1,2)).await?.duplicate,"correct retry changed identity");
  // Receipt and watch publication are different moments; wait for the actor's publication.
  let mut health=cap.status_watch();tokio::time::timeout(Duration::from_secs(5),async {loop {if health.borrow()["state"]=="ready" {break}health.changed().await?;}Ok::<_,anyhow::Error>(())}).await??;
  cap.close().await?;Ok(())
 }
 #[tokio::test] async fn low_space_and_conflicting_identity_get_no_receipt_or_clean_drain()->Result<()> {
  let dir=tempfile::tempdir()?;let cap=Capture::open(dir.path().join("raw"),CaptureOptions{min_free_bytes:u64::MAX,..Default::default()})?;
  ensure!(cap.append(&frame(1,512)).await.is_err(),"low-space append acknowledged");ensure!(cap.close().await.is_err(),"failed queue reported clean drain");
  let cap=Capture::open(dir.path().join("different"),CaptureOptions::default())?;
  cap.append(&frame(1,2)).await?;ensure!(cap.append(&frame(1,3)).await.is_err(),"conflicting identity acknowledged");ensure!(cap.bounds().await?["message_count"]=="1","conflict changed evidence");ensure!(cap.close().await.is_err(),"conflicting accepted request drain claimed clean");Ok(())
 }
}
