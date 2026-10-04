//! Fixed input equivalence and work-count proof for the same-frame SHA hoist.
#[path="../src/capture.rs"]mod capture;
#[path="../src/catalog.rs"]mod catalog;
#[path="../src/quotes.rs"]mod quotes;
#[path="../src/providers/mod.rs"]mod providers;
#[path="acceptance/hash_old_providers.rs"]mod old_providers;
#[path="acceptance/hash_new_providers.rs"]mod new_providers;
use anyhow::{Context,Result,ensure};
use std::{sync::Arc,path::Path,time::Instant};
use serde_json::json;
use sha2::{Digest,Sha256};
use tracefang_core::persistence_contract::CapturePosition;
fn digest(bytes:&[u8])->String {hex::encode(Sha256::digest(bytes))}
fn main()->Result<()> {
    let args=std::env::args().collect::<Vec<_>>();
    ensure!(args.len()==4,"fixed-envelope fixed-received-at result-file");
    let body=std::fs::read(&args[1])?;
    let received:chrono::DateTime<chrono::Utc>=args[2].parse()?;
    let frame=capture::ProviderFrame {version:1,channel:"tonghuashun_futures_history".into(),connection_id:"fixed-hash-equivalence".into(),sequence:u64::MAX,received_at:received,encoding:"json".into(),body};
    let position=CapturePosition {epoch:"fixed-hash-equivalence-v1".into(),sequence:u64::MAX,digest:digest(&frame.body)};
    let record=capture::CapturedFrame {position,frame,accepted_at_ns:received.timestamp_nanos_opt().context("fixed time out of bounds")?,legacy:None,logical_at_ns:received.timestamp_nanos_opt().unwrap(),clock_policy_version:Some("native-accepted-v1".into())};
    let catalog=Arc::new(catalog::Catalog::embedded()?);
    // This synthetic daily supplement only exercises the zero-bar hot-path guard.
    use base64::Engine;
    let mut zero=record.clone();let mut zero_envelope:serde_json::Value=serde_json::from_slice(&zero.frame.body)?;
    let zero_payload=json!({"name":catalog.by_provider("wh_USDIND").context("fixed provider unsupported")?.name,"data":"20260930,100,101,99,100,0,0"});
    zero_envelope["kind"]=json!("daily_last");zero_envelope["period"]=json!("01");zero_envelope["content_base64"]=json!(base64::engine::general_purpose::STANDARD.encode(format!("fixed_daily({});",zero_payload).as_bytes()));zero.frame.body=serde_json::to_vec(&zero_envelope)?;zero.position.digest=digest(&zero.frame.body);
    let mut zero_old=old_providers::Decoder::new(catalog.clone());let mut zero_new=new_providers::Decoder::new(catalog.clone());let mut zero_production=providers::Decoder::new(catalog.clone());
    let zero_a=zero_old.decode_record(&zero)?;let zero_b=zero_new.decode_record(&zero)?;let zero_c=zero_production.decode_record(&zero)?;
    ensure!(serde_json::to_vec(&zero_a)?==serde_json::to_vec(&zero_b)? && serde_json::to_vec(&zero_b)?==serde_json::to_vec(&zero_c)? && zero_c.1.is_empty(),"synthetic zero-bar daily outputs differ");
    ensure!(zero_old.snapshot()?==zero_new.snapshot()? && zero_new.snapshot()?==zero_production.snapshot()? && old_providers::body_hash_calls()==0 && new_providers::body_hash_calls()==0,"zero-bar daily state or zero-hash work changed");
    let zero_bar_control=json!({"kind":"synthetic valid daily supplement with no cached quote","market_truth":false,"complete_outputs_equal":true,"checkpoints_equal":true,"old_body_hash_calls":0,"new_body_hash_calls":0,"input_sha256":digest(&zero.frame.body)});
    let mut repeated_timings=vec![];
    for repeat in 0..3 {
    let mut old=old_providers::Decoder::new(catalog.clone());let started=Instant::now();let old_output=old.decode_record(&record)?;let old_seconds=started.elapsed().as_secs_f64();
    let mut new=new_providers::Decoder::new(catalog.clone());let started=Instant::now();let new_output=new.decode_record(&record)?;let new_seconds=started.elapsed().as_secs_f64();
    let mut production=providers::Decoder::new(catalog.clone());let started=Instant::now();let actual_output=production.decode_record(&record)?;let production_seconds=started.elapsed().as_secs_f64();
    let old_bytes=serde_json::to_vec(&old_output)?;let new_bytes=serde_json::to_vec(&new_output)?;let actual_bytes=serde_json::to_vec(&actual_output)?;
    ensure!(old_bytes==new_bytes && new_bytes==actual_bytes,"complete old/new/production decoded output differs");
    let old_cp=old.snapshot()?;let new_cp=new.snapshot()?;let actual_cp=production.snapshot()?;
    ensure!(old_cp==new_cp && new_cp==actual_cp,"bounded Decoder checkpoint state differs");
    let rows=actual_output.1.len();ensure!(rows>1000 && old_providers::body_hash_calls()==rows*(repeat+1) && new_providers::body_hash_calls()==repeat+1,"same-body hash work count does not match expected rows-to-one reduction");
    let envelope:serde_json::Value=serde_json::from_slice(&record.frame.body)?;
    let original=base64::engine::general_purpose::STANDARD.decode(envelope["content_base64"].as_str().context("fixed body missing")?)?;let body_digest=digest(&original);
    for bar in &actual_output.1 {let raw=bar.source.raw_payload.as_ref().context("actual source metadata missing")?;ensure!(raw["authoritative_input"]["body_sha256"]==body_digest && raw["authoritative_input"]["capture_position"]==json!(record.position),"source body/position identity changed");}
    repeated_timings.push(json!({"repeat":repeat,"old_decode_seconds":old_seconds,"new_instrumented_decode_seconds":new_seconds,"new_production_decode_seconds":production_seconds,"observed_decode_speedup":old_seconds/production_seconds,"complete_decoded_output_sha256":digest(&actual_bytes),"decoder_checkpoint_sha256":digest(&serde_json::to_vec(&actual_cp)?)}));
    if repeat<2 {continue;}
    let result=json!({"schema":"tracefang-same-frame-body-hash-hoist-actual-acceptance-v1","input_envelope_sha256":digest(&record.frame.body),"input_body_sha256":body_digest,"input_body_bytes":original.len(),"fixed_received_at":received,"bar_rows":rows,"quote_rows":actual_output.0.len(),"complete_decoded_output_sha256":digest(&actual_bytes),"complete_decoded_output_bytes":actual_bytes.len(),"all_old_new_production_output_bytes_equal":true,"all_decoder_checkpoints_equal":true,"decoder_checkpoint":actual_cp,"zero_bar_control":zero_bar_control,"repeated_runs":3,"old_actual_body_sha256_calls":old_providers::body_hash_calls(),"new_actual_body_sha256_calls":new_providers::body_hash_calls(),"old_hash_calls_per_frame":rows,"new_hash_calls_per_frame":1,"repeated_timings":repeated_timings,"timing_context":"same process, fresh Decoder per trial; trial0 old decode is first-process code/data access; subsequent decodes and trials reuse OS/CPU caches; data fixed in memory, no disk/network timing included; old then instrumented new then uninstrumented production order retained","old_body_bytes_hashed":original.len() as u64*rows as u64,"new_body_bytes_hashed":original.len(),"old_decode_seconds":old_seconds,"new_instrumented_decode_seconds":new_seconds,"new_production_decode_seconds":production_seconds,"observed_decode_speedup":old_seconds/production_seconds,"backend_build_fingerprint":tracefang_core::quant_core::results::backend_build_fingerprint(),"network_requests":0,"database_actions":[],"production_actions":[]});
    let path=Path::new(&args[3]);ensure!(!path.exists(),"preserve prior equivalence evidence");let mut file=std::fs::File::create(path)?;serde_json::to_writer_pretty(&mut file,&result)?;file.sync_all()?;println!("{}",result);return Ok(());
    }
    unreachable!()
}
