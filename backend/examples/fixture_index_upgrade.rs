//! Explicit maintenance of the stopped synthetic fixture. No production path.
#[path="../src/capture.rs"]mod capture;
use anyhow::{Context,Result,ensure};
use serde_json::json;
use sha2::{Digest,Sha256};
use std::{path::Path,io::Read};
use tracefang_core::{native_store::Store,persistence_contract::CapturePosition};

fn file_hash(path:&Path)->Result<String>{let mut file=std::fs::File::open(path)?;let mut hash=Sha256::new();let mut buf=[0u8;65536];loop{let n=file.read(&mut buf)?;if n==0{break}hash.update(&buf[..n]);}Ok(hex::encode(hash.finalize()))}
async fn validate_tail(raw:&capture::Capture,position:&CapturePosition)->Result<String>{
    let bounds=raw.bounds_typed().await?;
    ensure!(bounds.epoch==position.epoch,"fixture raw epoch differs");
    ensure!(bounds.last_sequence==Some(position.sequence),"fixture has pending raw frames or a missing committed tail");
    raw.get_at(position).await?;
    let rows=raw.scan(&position.epoch,1,position.sequence.checked_add(1),16,64*1024*1024).await?;
    ensure!(rows.len() as u64==position.sequence && rows.iter().enumerate().all(|(i,row)|row.position.sequence==i as u64+1),"fixture raw prefix is incomplete");
    let mut hash=Sha256::new();for row in rows {
        ensure!(row.frame.channel=="fixed_quant_fixture" || row.frame.channel.starts_with("jin10_"),"fixture raw prefix contains a source requiring clock/calendar reprojection");
        hash.update(serde_json::to_vec(&row)?);hash.update(b"\n");
    }Ok(hex::encode(hash.finalize()))
}
#[tokio::main]
async fn main()->Result<()>{
    let args:Vec<_>=std::env::args().collect();
    let facts=Path::new(args.get(1).context("existing validation facts.redb required")?);
    let raw_path=Path::new(args.get(2).context("existing validation capture.redb required")?);
    ensure!(facts.components().any(|v|v.as_os_str()=="TraceFang-validation") && facts.parent()==raw_path.parent(),"fixture tool refuses non-validation or split directories");
    ensure!(facts.is_file() && raw_path.is_file(),"fixture must already exist; no new files are created");
    let raw_physical_before=file_hash(raw_path)?;
    let facts_physical_before=file_hash(facts)?;
    let copied_header_recovery=if args.get(3).map(String::as_str)==Some("--recover-reviewed-clone") {
        ensure!(facts==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/facts.redb") && raw_path==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/capture.redb"),"copied-header recovery refuses paths outside the exact reviewed clone");
        ensure!(facts_physical_before=="561ba6fd2f606eb9b3ca29806f688578c424bdf7ddcc18f4fd2b6de7c5609cf9","copied-header recovery requires the exact reviewed source facts hash");
        // Recover only redb's interrupted-close bookkeeping, without any application transaction.
        drop(redb::Database::open(facts).context("recover reviewed clone redb header")?);
        Some(json!({"exact_reviewed_clone":true,"open_and_drop_only":true,"application_write_transactions":"0","facts_sha256_before":facts_physical_before,"facts_sha256_after_recovery":file_hash(facts)?}))
    }else{None};
    if args.get(3).map(String::as_str)==Some("--inspect-reviewed-clone") {
        ensure!(facts==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/facts.redb") && raw_path==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/capture.redb"),"read-only inspection refuses paths outside the exact reviewed clone");
        let probe=Store::open_read_only(facts)?;let result=json!({"inspection_only":true,"version":probe.version().await?,"decoder_checkpoint":probe.metadata("runtime","decoder_checkpoint").await?,"fixture_count":probe.metadata("fixture","count").await?,"verified_manifest":probe.metadata("migration","verified_manifest").await?});probe.close().await?;
        ensure!(file_hash(facts)?==facts_physical_before && file_hash(raw_path)?==raw_physical_before,"read-only clone inspection changed physical bytes");
        println!("{}",serde_json::to_string_pretty(&result)?);return Ok(())
    }
    // Capture owns redb's exclusive writer lease. Its initialization transaction
    // may change physical DB bookkeeping, so compare every canonical frame too.
    // No append command is sent during this offline maintenance operation.
    let raw=capture::Capture::open(raw_path,capture::CaptureOptions::default()).context("fixture capture is still owned by a writer")?;
    let probe=Store::open_read_only(facts)?;let before=probe.version().await?;probe.close().await?;
    let position=before.committed_capture.clone().context("fixture capture anchor is missing")?;
    ensure!(position.epoch=="c6e48300-88b2-4adc-8a8b-dc7b78bb0e32" && position.sequence==4 && position.digest=="fd001743ee72d60bb104c2d5a86381dc9fa0dd0b0e96219283e8ba9f500a6d01","this maintenance invocation is limited to the reviewed fixture anchor");
    let raw_semantic_before=validate_tail(&raw,&position).await?;
    let store=Store::open(facts)?;
    ensure!(store.version().await?==before,"fixture changed before the exclusive facts lock");
    let legacy_empty_decoder_conversion=if args.get(3).map(String::as_str)==Some("--upgrade-reviewed-empty-decoder") {
        ensure!(facts==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/facts.redb") && raw_path==Path::new("/Users/daniel/Library/Application Support/TraceFang-validation/quant-final28-D1-7f61f225-b708-455a-980d-d9307dccab3e/capture.redb"),"empty decoder conversion refuses paths outside the exact reviewed clone");
        ensure!(facts_physical_before=="07e7712aa97f14888a5c62566b846feff3fc80ede14e92ce9a12eeb09e0f8cf6" && before.commit_id==30 && before.store_epoch=="061ea0b4-0673-4231-b4e2-c882340d7da7" && before.active_generation=="fixed-quant-fixture-v1","empty decoder conversion requires the exact inspected clone identity");
        let mut checkpoint=store.metadata("runtime","decoder_checkpoint").await?.context("reviewed decoder checkpoint missing")?;
        ensure!(checkpoint==json!({"decoder":[{},{}],"position":position,"projector_version":"tracefang-projector-v2-exact"}),"empty decoder conversion refuses any other captured state");
        let original=checkpoint.clone();checkpoint["decoder"]=json!({"version":2,"quotes":{},"daily":{},"sessions":{}});
        // Decoder::restore accepts the legacy (quotes,daily) tuple; its missing sessions are empty.
        let converted=store.set_metadata("runtime","decoder_checkpoint",checkpoint.clone()).await?;
        ensure!(converted.store_epoch==before.store_epoch && converted.active_generation==before.active_generation && converted.committed_capture==before.committed_capture && converted.commit_id==before.commit_id+1,"empty decoder conversion changed fixture identity");
        Some(json!({"original":original,"converted":checkpoint,"before_version":before,"after_version":converted,"empty_provider_state_preserved":true,"facts_or_index_write":false}))
    }else{None};
    let rebuilt=store.rebuild_offline_fixture_index(position.clone()).await?;
    let generation=store.read_generation("fixed-quant-fixture-v1").await?;
    let verified=generation.verify_staging().await?;
    ensure!(rebuilt["fact_codec_sha256"]==verified["fact_codec_sha256"],"fixture facts changed during derived rebuild");
    let after=store.version().await?;
    ensure!(before.store_epoch==after.store_epoch && before.active_generation==after.active_generation && before.committed_capture==after.committed_capture,"fixture identity or raw cursor changed");
    store.close().await?;
    let reopened=Store::open_read_only(facts)?;
    let proof=reopened.verify_index().await?;
    ensure!(proof["fact_codec_sha256"]==verified["fact_codec_sha256"] && proof["index_codec_sha256"]==verified["index_codec_sha256"],"fixture reopen verification differs");
    reopened.close().await?;
    let raw_semantic_after=validate_tail(&raw,&position).await?;
    ensure!(raw_semantic_before==raw_semantic_after,"fixture raw canonical frames changed");
    raw.close_and_drain().await?;
    let raw_physical_after=file_hash(raw_path)?;
    let facts_physical_after=file_hash(facts)?;
    println!("{}",serde_json::to_string_pretty(&json!({"fixture_only":true,"copied_header_recovery":copied_header_recovery,"legacy_empty_decoder_conversion":legacy_empty_decoder_conversion,"before":before,"after":after,"rebuild":rebuilt,"verified":verified,"reopen":proof,"fact_bytes_unchanged":true,"facts_physical_sha256_before":facts_physical_before,"facts_physical_sha256_after":facts_physical_after,"raw_canonical_sha256_before":raw_semantic_before,"raw_canonical_sha256_after":raw_semantic_after,"raw_physical_sha256_before":raw_physical_before,"raw_physical_sha256_after":raw_physical_after,"raw_physical_bytes_unchanged":raw_physical_before==raw_physical_after,"cursor_unchanged":true,"activation_changed":false}))?);Ok(())
}

#[cfg(test)]mod tests{
    use super::*;
    #[tokio::test]async fn wrong_epoch_and_pending_tail_are_rejected(){
        let dir=tempfile::tempdir().unwrap();let raw=capture::Capture::open(dir.path().join("capture.redb"),capture::CaptureOptions::default()).unwrap();
        let frame=|sequence|capture::ProviderFrame{version:1,channel:"fixed_quant_fixture".into(),connection_id:"validation".into(),sequence,received_at:chrono::Utc::now(),encoding:"json".into(),body:b"{}".to_vec()};
        let receipt=raw.append(&frame(1)).await.unwrap();let mut wrong=receipt.position.clone();wrong.epoch="wrong-epoch".into();assert!(validate_tail(&raw,&wrong).await.unwrap_err().to_string().contains("epoch"));
        raw.append(&frame(2)).await.unwrap();assert!(validate_tail(&raw,&receipt.position).await.unwrap_err().to_string().contains("pending"));raw.close_and_drain().await.unwrap();
    }
}
