//! Private reusable decode cache; no production write, import or activation.
#[path="../src/capture.rs"]mod capture;
#[path="../src/catalog.rs"]mod catalog;
#[path="../src/quotes.rs"]mod quotes;
#[path="../src/providers/mod.rs"]mod providers;
#[path="../src/legacy_import.rs"]mod legacy_import;
#[path="../src/legacy_spool.rs"]mod legacy_spool;
use anyhow::{Result,ensure,Context};use std::path::Path;use serde_json::json;
#[tokio::main]async fn main()->Result<()>{
 if std::env::args().nth(1).as_deref()==Some("identity"){println!("{}",json!({"schema":"tracefang-migration-tool-identity-v1","tool":"legacy_spool_probe","backend_build_sha256":tracefang_core::quant_core::results::backend_build_fingerprint(),"version":env!("CARGO_PKG_VERSION")}));return Ok(())}
 let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==6,"build|audit raw-file fixed-input-manifest through spool-file report-file; source is read only");let started=std::time::Instant::now();let raw=capture::Capture::open_read_only(&args[1])?;let through:u64=args[3].parse()?;let wanted=raw.get(through).await?.position;
 let source_sha256=legacy_import::file_hash(Path::new(&args[2]))?;let build_sha256=tracefang_core::quant_core::results::backend_build_fingerprint();let path=Path::new(&args[4]);std::fs::create_dir_all(path.parent().context("spool directory missing")?)?;
 let build_started=std::time::Instant::now();let created=if args[0]=="build"{Some(legacy_spool::build(&raw,path,through,source_sha256.clone(),build_sha256.clone()).await?)}else{ensure!(args[0]=="audit","unsupported decoded spool mode");None};let build_ms=build_started.elapsed().as_secs_f64()*1000.;raw.close_and_drain().await?;
 let audit_started=std::time::Instant::now();let reader=legacy_spool::Reader::open(path,&source_sha256,&build_sha256,&wanted)?;let proof=reader.audit_all()?;let reopen_audit_ms=audit_started.elapsed().as_secs_f64()*1000.;let manifest=reader.manifest.clone();drop(reader);
 let result=json!({"kind":"single_global_ordered_raw_decode_exact_spool","manifest":manifest,"original_complete_decoded_roundtrip":proof,"spool_file":path,"spool_file_sha256":legacy_import::file_hash(path)?,"spool_file_bytes":std::fs::metadata(path)?.len().to_string(),"created":created.is_some(),"build_ms":build_ms,"reopen_all_rows_audit_ms":reopen_audit_ms,"total_ms":started.elapsed().as_secs_f64()*1000.,"production_modified":false,"authority_boundary_created":false,"original_raw_replaced":false,"limitations":"Regenerable decoded evidence only; selected scope projection, PG authority comparison and quote reconciliation are separate gates. Whole frame sequence and cross-source decoder state preserved; no original raw prefilter."});legacy_import::atomic_json(Path::new(&args[5]),&result)?;println!("{}",json!({"report":args[5],"frames":manifest.frames.to_string(),"complete_decoded_roundtrip":true}));Ok(())
}
