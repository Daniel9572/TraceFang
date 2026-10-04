//! Read-only closure verification. A raw anchor proves identity, never old projection equivalence.
use anyhow::{Context,Result,ensure};
use serde::{Deserialize,Serialize};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{fs::File,io::{Read,BufRead,BufReader}};
use std::path::{Path,PathBuf};
use tracefang_core::persistence_contract::{CapturePosition,LegacyTailObservation,ProjectionStartBoundary,u64_string,optional_u64_string,i64_string};
use crate::{capture::Capture,legacy_import::{LegacyManifest,file_hash}};

pub struct ClosureEvidenceFiles {
    pub stop_report:PathBuf,
    pub drain_report:PathBuf,
    pub reconciliation_report:Option<PathBuf>,
}
#[derive(Clone,Serialize,Deserialize)]
pub struct StopReport {
    pub schema:String,pub production_terminal:bool,pub stopped_component_ids:Vec<String>,
    pub raw_producers_stopped:bool,
    #[serde(with="i64_string")]pub observed_at_ns:i64,
    #[serde(flatten)]pub details:serde_json::Map<String,Value>,
}
#[derive(Clone,Serialize,Deserialize)]
pub struct DrainReport {
    pub schema:String,pub production_terminal:bool,pub stream:String,pub epoch:String,pub method:String,
    #[serde(with="optional_u64_string")]pub raw_applied_through_legacy:Option<u64>,
    #[serde(with="u64_string")]pub unresolved_frames:u64,
    #[serde(with="i64_string")]pub observed_at_ns:i64,
}
#[derive(Clone,Serialize,Deserialize)]
pub struct ReconciliationReport {
    pub schema:String,pub production_terminal:bool,pub source_manifest_id:String,pub stream:String,pub epoch:String,
    #[serde(with="u64_string")]pub last_sequence:u64,
    pub capture_tail:CapturePosition,pub mapping_sha256:String,
    pub fixed_input_manifest_file:String,pub fixed_input_manifest_sha256:String,
    pub postgres_snapshot:String,pub postgres_source_fingerprint:String,
    #[serde(with="i64_string")]pub started_at_ns:i64,
    #[serde(with="i64_string")]pub completed_at_ns:i64,
    pub early_tail_observation:LegacyTailObservation,
    #[serde(with="u64_string")]pub raw_frames_scanned:u64,
    #[serde(with="u64_string")]pub unresolved_differences:u64,
    pub complete:bool,pub affected_ranges:Vec<Value>,
    pub comparison:ReconciliationArtifact,pub overlay:ReconciliationArtifact,
    pub independent_verification:ReconciliationArtifact,
    pub verified_fact_sha256:String,pub verified_index_sha256:String,
    pub policy:String,pub backend_build_fingerprint:String,pub backend_build_sha256:String,
    pub clock_projection:ClockProjectionEvidence,pub clock_binding:Value,pub sealed_tools:Value,
    pub capture_prefix:Value,pub decode_rejections:Value,pub frame_accounting:FrameAccounting,
    pub classified_rejections:ReconciliationArtifact,pub classification_policy:String,
    pub quote_events_reconciled:bool,pub initial_seed:String,pub all_scopes_complete:bool,
    #[serde(with="u64_string")]pub quote_events_verified:u64,
    pub quote_events_sha256:String,
    #[serde(with="u64_string")]pub latest_quotes_verified:u64,
    pub latest_quotes_sha256:String,
}
pub const COMPOSITE_POLICY:&str="legacy-bars-fixed-authority+clock-projection+retained-raw-overlay-v2";
pub const REJECTION_POLICY:&str="retained-envelope-http502-and-optional-daily-statistics-v1";
#[derive(Clone,Serialize,Deserialize)]
pub struct ClockProjectionEvidence {
    pub manifest:ReconciliationArtifact,pub independent_audit:ReconciliationArtifact,
    pub policy_source:ReconciliationArtifact,pub auditor_source:ReconciliationArtifact,
    pub policy_evidence:Vec<ReconciliationArtifact>,
}
#[derive(Clone,Serialize,Deserialize)]
pub struct FrameAccounting {
    #[serde(with="u64_string")]pub projection_frames:u64,
    #[serde(with="u64_string")]pub no_output_frames:u64,
    #[serde(with="u64_string")]pub classified_rejection_frames:u64,
    #[serde(with="u64_string")]pub unresolved_frames:u64,
}
#[derive(Clone,Serialize,Deserialize)]
pub struct ReconciliationArtifact {
    pub file:String,pub sha256:String,pub complete:bool,
    #[serde(with="u64_string")]pub row_count:u64,
}
const MAX_REPORT_BYTES:u64=8*1024*1024;
const MAX_LEDGER_ROW_BYTES:u64=4*1024*1024;
const MAX_DECODED_LEDGER_BYTES:u64=16*1024*1024*1024;
struct DigestReader<'a> {file:File,hash:&'a mut Sha256,bytes:&'a mut u64}
impl Read for DigestReader<'_>{
    fn read(&mut self,buffer:&mut [u8])->std::io::Result<usize>{
        let count=self.file.read(buffer)?;self.hash.update(&buffer[..count]);*self.bytes=self.bytes.checked_add(count as u64).ok_or_else(||std::io::Error::other("compressed ledger byte count overflow"))?;Ok(count)
    }
}
fn valid_sha(value:&str)->bool{value.len()==64&&value.bytes().all(|v|v.is_ascii_hexdigit()&&!v.is_ascii_uppercase())}
fn private_artifact_path(dir:&Path,name:&str)->Result<PathBuf>{
    let relative=Path::new(name);
    ensure!(!name.is_empty()&&relative.components().count()==1&&relative.file_name().is_some_and(|v|v==name),"reconciliation artifact must be a basename in the fixed archive");
    let path=dir.join(relative);let metadata=std::fs::symlink_metadata(&path)?;
    ensure!(metadata.is_file()&&!metadata.file_type().is_symlink(),"reconciliation artifact must be a regular file, not a symlink");Ok(path)
}
fn evidence_file(path:&Path)->Result<File>{
    let metadata=std::fs::symlink_metadata(path)?;
    ensure!(metadata.is_file()&&!metadata.file_type().is_symlink(),"closure evidence must be a regular file, not a symlink");
    let mut options=std::fs::OpenOptions::new();options.read(true);
    #[cfg(unix)] {use std::os::unix::fs::OpenOptionsExt;options.custom_flags(libc::O_NOFOLLOW);}
    Ok(options.open(path)?)
}
/// Hash and deserialize the same opened bytes; a report cannot change between
/// a pathname checksum and a second, unrelated read. Large ledgers use streaming.
fn checked<T:serde::de::DeserializeOwned>(path:&Path,sha:&str)->Result<T>{
    ensure!(valid_sha(sha),"closure checksum is not canonical SHA256");
    let file=evidence_file(path)?;ensure!(file.metadata()?.len()<=MAX_REPORT_BYTES,"closure JSON report exceeds bounded input");
    let mut bytes=Vec::new();file.take(MAX_REPORT_BYTES+1).read_to_end(&mut bytes)?;
    ensure!(bytes.len() as u64<=MAX_REPORT_BYTES,"closure JSON report grew beyond bounded input");
    ensure!(hex::encode(Sha256::digest(&bytes))==sha,"closure report checksum differs");Ok(serde_json::from_slice(&bytes)?)
}
fn artifact(dir:&Path,evidence:&ReconciliationArtifact)->Result<()> {
    ensure!(evidence.complete&&valid_sha(&evidence.sha256),"reconciliation artifact unfinished or invalid checksum");
    let path=private_artifact_path(dir,&evidence.file)?;let mut reader=evidence_file(&path)?;
    let mut hash=Sha256::new();let mut bytes=vec![0;64*1024];
    loop{let count=reader.read(&mut bytes)?;if count==0{break}hash.update(&bytes[..count]);}
    ensure!(hex::encode(hash.finalize())==evidence.sha256,"reconciliation artifact bytes changed");Ok(())
}
fn number(value:&Value,key:&str)->Result<u64>{
    let text=value[key].as_str().with_context(||format!("missing exact integer field {key}"))?;
    ensure!(!text.is_empty()&&text.bytes().all(|v|v.is_ascii_digit())&&(text=="0"||!text.starts_with('0')),"noncanonical exact integer field {key}");Ok(text.parse()?)
}
fn signed(value:&Value,key:&str)->Result<i64>{let text=value[key].as_str().with_context(||format!("missing signed clock {key}"))?;let parsed:i64=text.parse()?;ensure!(text==parsed.to_string(),"noncanonical signed clock {key}");Ok(parsed)}
fn reference(value:&Value)->Result<ReconciliationArtifact>{Ok(serde_json::from_value(value.clone())?)}
fn artifact_json(dir:&Path,evidence:&ReconciliationArtifact)->Result<Value>{
    ensure!(evidence.complete,"reconciliation JSON artifact unfinished");checked(&private_artifact_path(dir,&evidence.file)?,&evidence.sha256)
}
fn hashes(values:&Value)->Result<std::collections::BTreeSet<String>>{
    let values=values.as_array().context("checksum evidence list absent")?;ensure!(!values.is_empty()&&values.len()<=16,"checksum evidence list outside bounded policy");
    let mut output=std::collections::BTreeSet::new();for value in values{let sha=value["sha256"].as_str().context("evidence SHA missing")?;ensure!(valid_sha(sha)&&output.insert(sha.to_owned()),"invalid or duplicate policy evidence hash");}Ok(output)
}
fn common_binding(report:&ReconciliationReport)->Value{
    json!({"schema":"legacy-reconciliation-binding-v3","policy":report.policy,"backend_build_fingerprint":report.backend_build_fingerprint,
    "fixed_input_manifest_sha256":report.fixed_input_manifest_sha256,"clock_projection_manifest_sha256":report.clock_projection.manifest.sha256,
    "capture_tail":report.capture_tail,"mapping_sha256":report.mapping_sha256,"verified_fact_sha256":report.verified_fact_sha256,"verified_index_sha256":report.verified_index_sha256,
    "quote_events_verified":report.quote_events_verified.to_string(),"quote_events_sha256":report.quote_events_sha256,
    "latest_quotes_verified":report.latest_quotes_verified.to_string(),"latest_quotes_sha256":report.latest_quotes_sha256})
}
fn summary(dir:&Path,evidence:&ReconciliationArtifact,schema:&str,report:&ReconciliationReport)->Result<Value>{
    let value=artifact_json(dir,evidence)?;let binding=common_binding(report);
    ensure!(value["schema"]==schema&&value["complete"]==true&&value["empty_raw_seed"]==true&&number(&value,"unresolved_differences")?==0&&value["binding"]==binding,"reconciliation summary is incomplete or bound to different inputs");
    for(key,expected)in binding.as_object().context("invalid common binding")?{if key!="schema"{ensure!(value[key]==*expected,"reconciliation summary common field differs: {key}");}}
    Ok(value)
}
/// Rows are bounded and streamed. A checksum or caller-supplied row count alone
/// cannot claim that an empty/unfinished ledger contains the compared rows.
fn ledger<F>(dir:&Path,evidence:&ReconciliationArtifact,mut inspect:F)->Result<u64>
where F:FnMut(&Value)->Result<()> {
    ensure!(evidence.complete&&valid_sha(&evidence.sha256),"unfinished ledger or invalid checksum");
    let path=private_artifact_path(dir,&evidence.file)?;let file=evidence_file(&path)?;let file_bytes=file.metadata()?.len();
    let mut hash=Sha256::new();let mut raw_bytes=0u64;
    let digest=DigestReader{file,hash:&mut hash,bytes:&mut raw_bytes};
    // MultiGzDecoder validates the CRC/length trailer of every member and keeps
    // parsing until physical EOF. A single-member decoder could ignore a second
    // member or trailing junk and falsely attest only the first part of a file.
    let decoded:Box<dyn Read+'_>=if evidence.file.ends_with(".gz"){
        Box::new(flate2::bufread::MultiGzDecoder::new(BufReader::with_capacity(64*1024,digest)))
    }else{Box::new(digest)};
    let mut reader=BufReader::with_capacity(64*1024,decoded);
    let mut count=0u64;let mut decoded_bytes=0u64;let mut bytes=Vec::new();
    loop{
        bytes.clear();let size=reader.by_ref().take(MAX_LEDGER_ROW_BYTES+1).read_until(b'\n',&mut bytes)?;
        if size==0{break}ensure!(size as u64<=MAX_LEDGER_ROW_BYTES&&bytes.last()==Some(&b'\n'),"ledger row exceeds 4MiB or is not atomically complete");
        decoded_bytes=decoded_bytes.checked_add(size as u64).context("decoded ledger size overflow")?;
        ensure!(decoded_bytes<=MAX_DECODED_LEDGER_BYTES&&count<evidence.row_count,"ledger decoded size or rows exceed declared/bounded complete evidence");
        let value:Value=serde_json::from_slice(&bytes)?;inspect(&value)?;count=count.checked_add(1).context("ledger row count overflow")?;
    }
    drop(reader);
    ensure!(count==evidence.row_count&&raw_bytes==file_bytes&&hex::encode(hash.finalize())==evidence.sha256,"ledger actual count, complete physical EOF or compressed checksum differs");Ok(count)
}
fn verify_clock(dir:&Path,manifest:&LegacyManifest,report:&ReconciliationReport,seal:&Value)->Result<()> {
    let refs=&report.clock_projection;ensure!(!refs.policy_evidence.is_empty()&&refs.policy_evidence.len()<=16,"clock witnesses outside bounded policy");
    let plan=artifact_json(dir,&refs.manifest)?;let audit=artifact_json(dir,&refs.independent_audit)?;
    artifact(dir,&refs.policy_source)?;artifact(dir,&refs.auditor_source)?;for proof in &refs.policy_evidence{artifact(dir,proof)?;}
    let policy=&plan["policy"]["policy"];
    ensure!(plan["schema"]=="legacy-source-clock-projection-v2"&&plan["policy_version"]=="legacy-bars-fixed-authority+clock-projection-v2"&&plan["complete"]==true&&plan["activation"]==false&&plan["original_source_or_facts_modified"]==false&&plan["quotes_shifted"]==false,"clock projection is incomplete or modified original/quote facts");
    ensure!(plan["source_manifest_id"]==manifest.id&&plan["snapshot"]==manifest.postgres["snapshot"]&&plan["source_fingerprint"]==manifest.postgres["fingerprint"],"clock projection belongs to a different fixed PostgreSQL snapshot");
    ensure!(policy["policy_id"]==tracefang_core::source_clock::THS_V6_SHFE_END_V2&&policy["prior_policy_id"]==tracefang_core::source_clock::LEGACY_V6_OPEN_V1&&policy["shift_quotes"]==false&&policy["received_and_finalized_unchanged"]==true&&policy["source_observed_unchanged"]==true,"clock policy changes original source evidence or quotes");
    let scopes=policy["scopes"].as_array().context("clock policy exact scopes absent")?;ensure!(scopes.len()==4,"clock policy must bind exactly four reviewed scopes");
    for(provider,symbol)in tracefang_core::source_clock::VERIFIED_V6_SCOPES{ensure!(scopes.iter().filter(|v|v["provider_code"]==provider&&v["instrument_symbol"]==symbol&&v["venue"]=="SHFE"&&v["period"]=="61").count()==1,"clock policy scope identity differs");}
    for name in ["candles","realtime_bars"]{
        let table=manifest.tables.iter().find(|v|v.table==name).context("fixed source table descriptor absent")?;
        ensure!(plan["source_tables"][name]==serde_json::to_value(table)?,"clock policy source table differs: {name}");
    }
    let counts=&plan["counts"];let collision=counts.get("collided_input_rows").map(|_|number(counts,"collided_input_rows")).transpose()?.unwrap_or(0);
    let preserved=number(counts,"output_rows")?.checked_add(number(counts,"point_rows")?).and_then(|v|v.checked_add(collision)).context("clock input conservation overflow")?;
    ensure!(number(counts,"input_rows")?==preserved&&number(&plan,"rows")?==number(counts,"output_rows")?&&collision==0,"clock rows do not conserve exact input or contain unresolved collisions");
    for key in ["sha256","original_canonical_file_sha256","original_descriptor_sha256"]{ensure!(plan[key].as_str().is_some_and(valid_sha),"clock descriptor digest absent: {key}");}
    ensure!(audit["schema"]=="independent-clock-projection-audit-v1"&&audit["complete"]==true&&audit["projection_manifest_sha256"]==refs.manifest.sha256&&audit["projected_file_sha256"]==plan["sha256"]&&audit["original_file_sha256"]==plan["original_canonical_file_sha256"]&&audit["policy"]==policy["policy_id"]&&audit["oracle_source_sha256"]==refs.auditor_source.sha256,"independent clock audit does not certify this exact fresh input and auditor");
    let current_source=hex::encode(Sha256::digest(include_bytes!("source_clock.rs")));
    ensure!(refs.policy_source.sha256==current_source&&plan["policy_source_sha256"]==current_source,"clock implementation differs from this compiled policy");
    let copied=refs.policy_evidence.iter().map(|v|json!({"sha256":v.sha256})).collect::<Vec<_>>();let copied=hashes(&json!(copied))?;
    ensure!(hashes(&plan["policy_evidence"])?==copied&&hashes(&seal["clock_policy"]["witnesses"])?==copied,"clock witnesses differ from descriptor or immutable tools seal");
    let binding=&report.clock_binding;
    ensure!(binding["policy_id"]==policy["policy_id"]&&binding["source_manifest_id"]==manifest.id&&binding["postgres_snapshot"]==manifest.postgres["snapshot"]&&binding["postgres_source_fingerprint"]==manifest.postgres["fingerprint"]&&binding["original_canonical_sha256"]==plan["original_canonical_file_sha256"]&&binding["corrected_canonical_sha256"]==plan["sha256"]&&binding["policy_source_sha256"]==current_source&&binding["auditor_sha256"]==refs.auditor_source.sha256&&binding["backend_build_sha256"]==report.backend_build_fingerprint,"clock binding differs from actual plan/audit/build");
    for(key,expected)in [("mapping_manifest",&refs.manifest),("independent_audit",&refs.independent_audit)]{let actual=reference(&binding[key])?;ensure!(actual.file==expected.file&&actual.sha256==expected.sha256&&actual.complete&&actual.row_count==expected.row_count,"clock reference aliases differ");}
    ensure!(hashes(&binding["policy_witnesses"])?==copied&&seal["clock_policy"]["source_sha256"]==current_source&&seal["clock_policy"]["auditor_sha256"]==refs.auditor_source.sha256,"clock binding not sealed");
    let phase=manifest.phases.iter().find(|p|p["phase"]=="source_clock_native_staging").context("clock projection fixed-source import phase missing")?;
    ensure!(binding["original_source_manifest_sha256"]==phase["original_source_manifest_sha256"]&&phase["clock_manifest_sha256"]==refs.manifest.sha256,"fresh original source/clock import phase is different");Ok(())
}
fn verify_seal(dir:&Path,report:&ReconciliationReport)->Result<Value>{
    let reference=&report.sealed_tools;
    let sha=reference["sha256"].as_str().context("tools seal SHA absent")?;
    let seal:Value=checked(&private_artifact_path(dir,reference["file"].as_str().context("tools seal basename absent")?)?,sha)?;
    ensure!(seal["schema"]=="tracefang-migration-tools-v1"&&seal["complete"]==true&&seal["conflict_policy_version"]==COMPOSITE_POLICY&&seal["backend_build_sha256"]==report.backend_build_fingerprint&&reference["backend_build_sha256"]==report.backend_build_fingerprint,"tools seal is incomplete or compiled by a different backend");
    let files=seal["files"].as_array().context("sealed tool inputs absent")?;ensure!(!files.is_empty()&&files.len()<=1024,"tools seal input list outside bounded scope");
    let executables=&seal["executables"];let path=Path::new(executables["reconcile-probe"].as_str().context("sealed complete terminal executor absent")?);
    let executor_sha=reference["executor_sha256"].as_str().context("sealed terminal executor SHA absent")?;ensure!(valid_sha(executor_sha),"sealed executor SHA invalid");
    ensure!(files.iter().filter(|v|v["path"]==json!(path)&&v["sha256"]==executor_sha).count()==1,"terminal executor does not have one exact sealed file identity");
    ensure!(!path.components().any(|v|v.as_os_str()=="rust-target"||v.as_os_str()=="target"),"terminal executor cannot be mutable Cargo output");
    let mut file=evidence_file(path)?;let mut hash=Sha256::new();let mut bytes=vec![0;64*1024];loop{let count=file.read(&mut bytes)?;if count==0{break}hash.update(&bytes[..count]);}
    ensure!(hex::encode(hash.finalize())==executor_sha,"sealed terminal executor bytes changed");
    if report.production_terminal {
        let immutable=std::fs::canonicalize(seal["immutable_helper_directory"].as_str().context("immutable helper directory absent")?)?;
        ensure!(std::fs::canonicalize(path)?.starts_with(&immutable),"sealed executor is outside immutable helper directory");
        #[cfg(unix)] {use std::os::unix::fs::PermissionsExt;ensure!(file.metadata()?.permissions().mode()&0o222==0,"sealed terminal executor remains writable");}
        let current=std::fs::canonicalize(std::env::current_exe()?)?;
        let probe=std::fs::canonicalize(executables["probe"].as_str().context("sealed authority verifier absent")?)?;
        ensure!(current==probe,"authority verifier is not the sealed Release probe");
        let actual=file_hash(&current)?;ensure!(files.iter().filter(|v|v["path"]==json!(current)&&v["sha256"]==actual).count()==1,"authority verifier bytes are not sealed");
    }
    Ok(seal)
}
fn verify_prefix(dir:&Path,manifest:&LegacyManifest,report:&ReconciliationReport)->Result<()> {
    let prefix=&report.capture_prefix;
    let first:CapturePosition=serde_json::from_value(prefix["first"].clone())?;let last:CapturePosition=serde_json::from_value(prefix["last"].clone())?;
    ensure!(first.sequence==1&&first.epoch==report.capture_tail.epoch&&valid_sha(&first.digest)&&last==report.capture_tail&&number(prefix,"frames")?==last.sequence,"retained raw prefix is incomplete or bound to a different capture");
    ensure!(prefix["original_prefix_complete"]==manifest.raw["original_prefix_complete"]&&prefix["original_prefix_complete"].is_boolean(),"retained origin coverage is missing or falsely upgraded");
    let origins=prefix["origin_coverage"].as_array().context("retained legacy origin coverage array absent")?;ensure!(!origins.is_empty()&&origins.len()<=64,"retained origin coverage outside bounded source scope");
    if manifest.raw["original_prefix_complete"]==false{ensure!(origins.iter().any(|v|v["missing_prefix"]==true&&v["initial_state"]=="empty_retained_prefix_no_pg_latest"),"missing original raw prefix was concealed");}
    let evidence=reference(&prefix["spool_audit"])?;let audit=artifact_json(dir,&evidence)?;let spooled=&audit["manifest"];let roundtrip=&audit["original_complete_decoded_roundtrip"];
    ensure!(spooled["schema"]=="retained-global-decoded-spool-v2"&&spooled["complete"]==true&&spooled["source_manifest_sha256"]==report.fixed_input_manifest_sha256&&spooled["build_sha256"]==report.backend_build_fingerprint&&spooled["first"]==prefix["first"]&&spooled["last"]==prefix["last"]&&number(spooled,"frames")?==last.sequence&&spooled["origin_coverage"]==prefix["origin_coverage"]&&spooled["original_prefix_complete"]==prefix["original_prefix_complete"],"spool belongs to a different fixed input, build or retained prefix");
    ensure!(roundtrip["complete"]==true&&number(roundtrip,"frames")?==last.sequence&&roundtrip["expected_sha256"]==roundtrip["actual_sha256"]&&roundtrip["expected_sha256"]==prefix["canonical_decoded_sha256"]&&spooled["canonical_decoded_sha256"]==prefix["canonical_decoded_sha256"]&&prefix["canonical_decoded_sha256"].as_str().is_some_and(valid_sha),"all-row decoded spool roundtrip did not pass");
    ensure!(prefix["spool_file_sha256"].as_str().is_some_and(valid_sha)&&audit["spool_file_sha256"]==prefix["spool_file_sha256"],"spool file byte identity differs");Ok(())
}
fn verify_reopened_summaries(dir:&Path,report:&ReconciliationReport,boundary:&ProjectionStartBoundary,issued:&Value)->Result<()> {
    for(evidence,schema)in [(&report.comparison,"legacy-reconciliation-comparison-v3"),(&report.overlay,"legacy-reconciliation-overlay-v3")]{
        let value=summary(dir,evidence,schema,report)?;let rows=reference(&value["ledger"])?;
        ensure!(rows.row_count==evidence.row_count,"summary compared row count differs from actual ledger");
        ledger(dir,&rows,|row|{ensure!(matches!(row["kind"].as_str(),Some("bar"|"quote")),"unknown comparison or overlay row kind");ensure!(row["state"]!="unresolved","unresolved row remained in successful comparison");Ok(())})?;
    }
    let value=summary(dir,&report.independent_verification,"legacy-reconciliation-reopen-v3",report)?;
    let selected=number(&value,"selected_bar_rows")?.checked_add(number(&value,"selected_quote_rows")?).context("reopened selected count overflow")?;
    ensure!(selected==report.independent_verification.row_count&&value["expected_field_sha256"].as_str().is_some_and(valid_sha)&&value["expected_field_sha256"]==value["actual_field_sha256"],"reopened selected full fields differ or count is incomplete");
    ensure!(value["live_before"].is_object()&&value["live_before"]==value["live_after"]&&value["stage_version"]["active_generation"]==boundary.staging_generation&&value["stage_version"]["committed_capture"].is_null()&&value["stage_version"]["store_epoch"]==issued["store_epoch"]&&value["stage_version"]["commit_id"]==issued["verified_commit_id"],"reopened target changed live generation or is not the verified inactive version");
    ensure!(value["proof"]==*issued&&issued["complete"]==true&&issued["index_verified"]==true&&issued["fact_codec_sha256"]==report.verified_fact_sha256&&issued["index_codec_sha256"]==report.verified_index_sha256,"reopen did not produce the current Store-issued complete proof");
    let source=&value["original_pg_verification"];ensure!(source["complete"]==true&&source["source_manifest_id"]==report.source_manifest_id&&source["postgres_snapshot"]==report.postgres_snapshot&&source["clock_projection_manifest_sha256"]==report.clock_projection.manifest.sha256,"original fixed PostgreSQL fields were not independently verified before overlay");
    let selected_rows=reference(&value["selected_input"])?;ensure!(selected_rows.row_count==selected,"selected input row count differs");
    let mut selected_hash=Sha256::new();
    ledger(dir,&selected_rows,|row|{
        let kind=row["kind"].as_str().context("selected kind absent")?;let key=&row["key"];
        ensure!(key["source"].as_str().is_some_and(|v|!v.is_empty()&&v.len()<=256)&&key["symbol"].as_str().is_some_and(|v|!v.is_empty()&&v.len()<=256),"selected exact source/symbol identity absent");
        match kind {"bar"=>{ensure!(key.as_object().is_some_and(|v|v.len()==4)&&key["interval"].as_u64().is_some_and(|v|v>0&&v<=u32::MAX as u64),"selected bar interval/key shape invalid");signed(key,"open_time_ns")?;},"quote"=>{ensure!(key.as_object().is_some_and(|v|v.len()==3)&&key["event_id"].as_str().is_some_and(|v|!v.is_empty()&&v.len()<=4096),"selected quote event/key shape invalid");},_=>anyhow::bail!("unknown selected exact key kind")}
        let expected=row["expected_sha256"].as_str().context("selected full-row SHA absent")?;ensure!(valid_sha(expected),"selected full-row SHA invalid");selected_hash.update(expected.as_bytes());selected_hash.update(b"\n");Ok(())
    })?;
    ensure!(hex::encode(selected_hash.finalize())==value["expected_field_sha256"],"selected all-field hash stream differs from independent reopened proof");
    for(key,count)in [("quote_event_ledger",report.quote_events_verified),("latest_quote_ledger",report.latest_quotes_verified)]{
        let rows=reference(&value[key])?;ensure!(rows.row_count==count,"full generation quote proof count differs");
        ledger(dir,&rows,|row|{let key=row["key"].as_array().context("full quote proof exact key absent")?;ensure!(row["row_sha256"].as_str().is_some_and(valid_sha)&&key.len()==3&&key[..2].iter().all(|v|v.as_str().is_some_and(|v|!v.is_empty()&&v.len()<=256))&&key[2].as_str().is_some_and(|v|!v.is_empty()&&v.len()<=4096),"full quote proof lacks exact source/symbol/event identity/hash");Ok(())})?;
    }
    Ok(())
}
fn verify_v3(dir:&Path,manifest:&LegacyManifest,report:&ReconciliationReport,boundary:&ProjectionStartBoundary,issued:&Value)->Result<()> {
    let build=tracefang_core::quant_core::results::backend_build_fingerprint();
    ensure!(report.schema=="legacy-reconciliation-v3"&&report.policy==COMPOSITE_POLICY&&boundary.conflict_policy_version==COMPOSITE_POLICY&&report.backend_build_fingerprint==build&&report.backend_build_sha256==build,"v3 composite policy or compiled backend identity differs");
    ensure!(report.quote_events_reconciled&&report.initial_seed=="empty"&&report.all_scopes_complete&&report.classification_policy==REJECTION_POLICY,"raw reconstruction was not empty-seeded, complete, or used an unreviewed classification policy");
    for sha in [&report.quote_events_sha256,&report.latest_quotes_sha256,&report.mapping_sha256,&report.verified_fact_sha256,&report.verified_index_sha256]{ensure!(valid_sha(sha),"exact full-generation proof digest absent");}
    let accounting=&report.frame_accounting;
    let frames=accounting.projection_frames.checked_add(accounting.no_output_frames).and_then(|v|v.checked_add(accounting.classified_rejection_frames)).and_then(|v|v.checked_add(accounting.unresolved_frames)).context("raw frame accounting overflow")?;
    ensure!(frames==report.raw_frames_scanned&&frames==report.capture_tail.sequence&&accounting.unresolved_frames==0,"disjoint frame classes do not cover the complete raw prefix");
    let mut identities=std::collections::BTreeSet::new();ensure!(report.affected_ranges.len()<=1024,"affected scope list exceeds bounded catalog");
    for scope in &report.affected_ranges{
        let symbol=scope["symbol"].as_str().context("affected symbol absent")?;let source=scope["source"].as_str().context("affected source absent")?;
        ensure!(!symbol.is_empty()&&!source.is_empty()&&identities.insert((symbol,source))&&scope["complete"]==true&&number(scope,"raw_frames_scanned")?==frames&&number(scope,"unresolved_differences")?==0,"affected scope is duplicate, incomplete or not processed through the full prefix");
    }
    let rejection=&report.classified_rejections;let alias=&report.decode_rejections;
    ensure!(alias["file"]==rejection.file&&alias["sha256"]==rejection.sha256&&alias["complete"]==true&&number(alias,"row_count")?==rejection.row_count&&number(alias,"classified")?==accounting.classified_rejection_frames&&number(alias,"unresolved")?==0&&rejection.row_count==accounting.classified_rejection_frames,"decode rejection evidence aliases or counts differ");
    let mut previous=0u64;
    ledger(dir,rejection,|row|{
        let position:CapturePosition=serde_json::from_value(row["capture_position"].clone())?;
        ensure!(position.epoch==report.capture_tail.epoch&&position.sequence>previous&&position.sequence<=report.capture_tail.sequence&&valid_sha(&position.digest),"rejected raw frame identity is duplicate, unordered or outside prefix");previous=position.sequence;
        ensure!(row["body_sha256"].as_str().is_some_and(valid_sha)&&row["diagnostic"].as_str().is_some_and(|v|!v.is_empty())&&row["legacy_origin"].is_object()&&row["channel"]=="tonghuashun_futures"&&matches!(row["classification"].as_str(),Some("provider_http_502_no_price"|"optional_daily_statistics_rejected_no_price"))&&number(row,"successful_quote_events")?==0,"classified rejection lacks actual reviewed raw evidence or was counted as quote success");Ok(())
    })?;
    let seal=verify_seal(dir,report)?;verify_clock(dir,manifest,report,&seal)?;verify_prefix(dir,manifest,report)?;verify_reopened_summaries(dir,report,boundary,issued)?;Ok(())
}
fn verify_production_stop(dir:&Path,stop:&StopReport)->Result<()> {
    if !stop.production_terminal{return Ok(())}
    let details=Value::Object(stop.details.clone());let evidence=&details["evidence"];
    let inspection=artifact_json(dir,&reference(&evidence["inspection"])?)?;
    let before=artifact_json(dir,&reference(&evidence["pre_stop_observation"])?)?;
    let attempt=artifact_json(dir,&reference(&evidence["attempt"])?)?;
    ensure!(inspection["schema"]=="legacy-installed-stop-inspection-v1"&&before["schema"]==inspection["schema"]&&inspection["processes_changed"]==false&&before["processes_changed"]==false,"production stop lacks actual independent/fresh inspection");
    for key in ["plist_path","plist_sha256","label","domain","registration","processes","installed_inputs"]{ensure!(before[key]==inspection[key],"production job identity changed before stop: {key}");}
    let domain=details["domain"].as_str().context("stop domain absent")?;let plist=details["plist_sha256"].as_str().context("stop plist SHA absent")?;
    ensure!(valid_sha(plist)&&inspection["domain"]==domain&&inspection["label"]=="com.tracefang.local"&&inspection["plist_sha256"]==plist&&details["plist_preserved"]==true&&details["method"]=="launchd_bootout_exact_job_then_process_tree_and_listener_exit","production stop did not preserve and identify the exact installed launchd job");
    ensure!(inspection["registration"]["registered"]==true&&inspection["ready"]["process_id"]==before["ready"]["process_id"],"production stop lacks the inspected running job/ready process");
    let processes=inspection["processes"].as_array().context("stopped process tree absent")?;ensure!(!processes.is_empty()&&processes.len()<=128,"stopped process tree outside bounded scope");
    let mut ids=std::collections::BTreeSet::from([format!("{domain}:plist:{plist}")]);let mut pids=std::collections::BTreeSet::new();
    for process in processes{
        let pid=process["pid"].as_u64().context("inspected process ID absent")?;let start=process["start_identity_sha256"].as_str().context("inspected process start identity absent")?;
        ensure!(pid>0&&valid_sha(start)&&pids.insert(pid),"invalid or duplicate stopped process identity");ids.insert(format!("pid:{pid}:start:{start}"));
    }
    ensure!(inspection["registration"]["pid"].as_u64().is_some_and(|p|pids.contains(&p))&&inspection["ready"]["process_id"].as_u64().is_some_and(|p|pids.contains(&p))&&stop.stopped_component_ids.iter().cloned().collect::<std::collections::BTreeSet<_>>()==ids,"stop report does not match complete inspected wrapper/child tree");
    ensure!(attempt["schema"]=="legacy-stop-attempt-v1"&&attempt["domain"]==domain&&attempt["plist_sha256"]==plist&&attempt["phase"]=="stopped_process_tree_and_listener_verified"&&attempt["native_providers_started"]==false,"launchd stop attempt has not completed for this installed job");
    let inspected=signed(&inspection,"observed_at_ns")?;let observed=signed(&before,"observed_at_ns")?;let requested=signed(&attempt,"bootout_requested_at_ns")?;
    ensure!(inspected<=observed&&observed<=requested,"producer stop predates its actual inspection");
    let observations=details["stable_no_restart_observations"].as_array().context("stable no-restart observations absent")?;
    ensure!(observations.len()>=2&&observations.len()<=256,"stable no-restart observation count outside bound");let mut previous=requested;
    for observation in observations{let at=signed(observation,"observed_at_ns")?;ensure!(at>previous&&at<=stop.observed_at_ns&&observation["job_registered"]==false&&observation["ready_endpoint_closed"]==true,"old process/listener resurrected or stop observations are not actual ordered clocks");previous=at;}
    Ok(())
}
fn closure_times(snapshot:i64,stop:&StopReport,drain:&DrainReport,report:Option<&ReconciliationReport>,tails:&[LegacyTailObservation])->Result<()> {
    let now=chrono::Utc::now().timestamp_nanos_opt().context("current clock outside signed ns range")?;
    ensure!(snapshot<=now&&stop.observed_at_ns<=now&&drain.observed_at_ns<=now&&tails.iter().all(|v|v.observed_at_ns<=now)&&report.is_none_or(|v|v.completed_at_ns<=now),"closure observation or completion is in the future");
    ensure!(snapshot>=stop.observed_at_ns,"PostgreSQL authority snapshot predates producer stop");
    ensure!(drain.observed_at_ns>=stop.observed_at_ns,"drain confirmation predates producer stop");
    if drain.method=="application_writer_clean_drain" {
        ensure!(snapshot>=drain.observed_at_ns,"PostgreSQL authority snapshot predates completed clean drain");
    }else{
        let report=report.context("independent drain requires reconciliation completion evidence")?;
        ensure!(report.started_at_ns>=snapshot&&report.completed_at_ns>=report.started_at_ns&&drain.observed_at_ns>=report.completed_at_ns,"independent reconciliation must finish after the fixed snapshot and before drain confirmation");
        ensure!(report.early_tail_observation.observed_at_ns>=stop.observed_at_ns&&report.early_tail_observation.observed_at_ns<=snapshot,"early stable tail must be observed after producer stop and before fixed snapshot");
    }
    for observation in tails {ensure!(observation.observed_at_ns>=drain.observed_at_ns,"final stable tail was sampled before drain confirmation");}
    ensure!(tails.last().context("stable tail proof absent")?.observed_at_ns>=snapshot,"stable tail proof ends before fixed PostgreSQL snapshot");Ok(())
}
/// Call before atomic Store activation, and recheck the raw anchor at runtime startup.
/// Files must be actual reviewed stop/drain observations; this verifier never stops services.
pub async fn verify_authority_boundary(dir:&Path,manifest:&LegacyManifest,capture:&Capture,boundary:&ProjectionStartBoundary,store_verification:&Value,files:&ClosureEvidenceFiles)->Result<Value>{
    tracefang_core::native_store::validate_boundary(boundary,&boundary.staging_generation,store_verification)?;
    ensure!(manifest.id==boundary.authority_manifest_id&&manifest.postgres["fingerprint"]==boundary.postgres_source_fingerprint&&manifest.postgres["snapshot"]==boundary.postgres_snapshot,"authority boundary belongs to another PostgreSQL snapshot");
    let dir=dir.to_path_buf();let manifest_sha=boundary.authority_manifest_sha256.clone();let map=manifest.raw["native_mapping"].clone();let map_sha=boundary.legacy_mapping_sha256.clone();
    let persisted:tracefang_core::persistence_contract::CapturePosition=serde_json::from_value(map["last_position"].clone()).context("authority raw mapping has no fixed tail")?;
    ensure!(persisted==boundary.raw_tail&&map["sha256"]==map_sha,"authority anchor differs from fixed source mapping");
    let mapped_file=map["file"].as_str().context("mapping file missing")?.to_owned();private_artifact_path(&dir,&mapped_file)?;
    let archive=dir.clone();tokio::task::spawn_blocking(move||->Result<()>{ensure!(file_hash(&archive.join("manifest.json"))?==manifest_sha,"authority manifest changed after review");ensure!(file_hash(&archive.join(mapped_file))?==map_sha,"fixed original-to-native mapping differs");Ok(())}).await??;
    let record=capture.get_at(&boundary.raw_tail).await?;let origin=record.legacy.context("authority tail is not an imported original envelope")?;
    ensure!(origin.stream==boundary.legacy_stream&&origin.epoch==boundary.legacy_epoch&&origin.sequence.parse::<u64>()?==boundary.legacy_tail_sequence,"legacy tail identity differs from persisted raw envelope");
    let stop:StopReport=checked(&files.stop_report,&boundary.closure.stop_report_sha256)?;
    let drain:DrainReport=checked(&files.drain_report,&boundary.closure.projection_drain_report_sha256)?;
    ensure!(stop.schema=="legacy-stop-evidence-v1"&&stop.production_terminal==boundary.production_terminal&&stop.raw_producers_stopped&&stop.stopped_component_ids==boundary.closure.stopped_component_ids,"producer stop evidence is incomplete or a different scope");
    ensure!(stop.stopped_component_ids.iter().collect::<std::collections::BTreeSet<_>>().len()==stop.stopped_component_ids.len(),"duplicate stopped component identities");
    verify_production_stop(dir.as_path(),&stop)?;
    ensure!(drain.schema=="legacy-drain-evidence-v1"&&drain.production_terminal==boundary.production_terminal&&drain.stream==boundary.legacy_stream&&drain.epoch==boundary.legacy_epoch&&drain.unresolved_frames==0&&drain.raw_applied_through_legacy==boundary.closure.raw_applied_through_legacy,"projection drain evidence differs");
    ensure!(matches!(drain.method.as_str(),"application_writer_clean_drain"|"independent_retained_raw_reconciliation"),"consumer acknowledgment or process stop alone does not prove projection drain");
    let mut reconciliation=None;
    if drain.method=="application_writer_clean_drain" {
        ensure!(drain.raw_applied_through_legacy==Some(boundary.legacy_tail_sequence),"clean application writer did not prove this exact terminal tail");
    }else {
        ensure!(drain.raw_applied_through_legacy.is_none(),"independent reconstruction is not a legacy applied transaction cursor");
        let path=files.reconciliation_report.as_ref().context("unproven old writer drain requires retained-input reconciliation")?;
        let sha=boundary.closure.reconciliation_report_sha256.as_ref().context("reconciliation report hash missing")?;
        let report:ReconciliationReport=checked(path,sha)?;
        ensure!(report.schema=="legacy-reconciliation-v3"&&report.production_terminal==boundary.production_terminal&&report.source_manifest_id==manifest.id&&report.stream==boundary.legacy_stream&&report.epoch==boundary.legacy_epoch&&report.last_sequence==boundary.legacy_tail_sequence&&report.capture_tail==boundary.raw_tail&&report.mapping_sha256==boundary.legacy_mapping_sha256&&report.complete&&report.unresolved_differences==0&&report.raw_frames_scanned==boundary.raw_tail.sequence&&!report.affected_ranges.is_empty(),"affected retained ranges were not completely reconciled");
        ensure!(boundary.conflict_policy_version==COMPOSITE_POLICY,"independent reconciliation requires versioned clock/composite authority policy");
        ensure!(report.postgres_snapshot==boundary.postgres_snapshot&&report.postgres_source_fingerprint==boundary.postgres_source_fingerprint&&report.verified_fact_sha256==boundary.verified_fact_sha256&&report.verified_index_sha256==boundary.verified_index_sha256,"reconciliation is bound to different PostgreSQL input or final facts/index");
        let fixed=ReconciliationArtifact{file:report.fixed_input_manifest_file.clone(),sha256:report.fixed_input_manifest_sha256.clone(),complete:true,row_count:1};artifact(dir.as_path(),&fixed)?;
        let input:LegacyManifest=checked(&private_artifact_path(&dir,&fixed.file)?,&fixed.sha256)?;
        ensure!(input.id==manifest.id&&input.postgres==manifest.postgres&&input.raw["native_mapping"]==manifest.raw["native_mapping"],"frozen reconciliation input differs from authority snapshot/raw mapping");
        verify_v3(dir.as_path(),&input,&report,boundary,store_verification)?;
        ensure!(report.early_tail_observation.stream==boundary.legacy_stream&&report.early_tail_observation.epoch==boundary.legacy_epoch&&report.early_tail_observation.last_sequence==boundary.legacy_tail_sequence,"raw tail moved between producer stop and fixed snapshot");
        reconciliation=Some(report);
    }
    let snapshot=manifest.postgres["captured_at"].as_str().context("snapshot clock missing")?.parse::<chrono::DateTime<chrono::Utc>>()?.timestamp_nanos_opt().context("snapshot clock out of signed ns range")?;
    closure_times(snapshot,&stop,&drain,reconciliation.as_ref(),&boundary.closure.stable_tail_observations)?;
    Ok(json!({"state":if boundary.production_terminal{"verified_terminal_legacy_authority"}else{"verified_isolated_fixture_no_production_authority"},"authority_manifest_id":manifest.id,"raw_anchor":record.position,"legacy_tail_sequence":boundary.legacy_tail_sequence.to_string(),"original_prefix_complete":manifest.raw["original_prefix_complete"],"raw_was_projected_into_pg":false,"committed_capture_assigned":false,"services_changed":false}))
}
#[cfg(test)]mod tests {
 use super::*;
 use crate::capture::{ProviderFrame,LegacyOrigin};
 use tracefang_core::{native_store::Store,persistence_contract::{LegacyClosureEvidence,LegacyTailObservation}};
 fn put(dir:&Path,name:&str,bytes:&[u8],rows:u64)->Result<ReconciliationArtifact>{
  std::fs::write(dir.join(name),bytes)?;Ok(ReconciliationArtifact{file:name.into(),sha256:file_hash(&dir.join(name))?,complete:true,row_count:rows})
 }
 fn put_json(dir:&Path,name:&str,value:&Value,rows:u64)->Result<ReconciliationArtifact>{put(dir,name,&serde_json::to_vec(value)?,rows)}
 fn fixture_v3(dir:&Path,manifest:&mut LegacyManifest,boundary:&mut ProjectionStartBoundary,proof:&Value)->Result<ReconciliationReport>{
  let build=tracefang_core::quant_core::results::backend_build_fingerprint();let empty=hex::encode(Sha256::digest(b""));
  for table in ["candles","realtime_bars"]{let file=format!("{table}.ndjson");put(dir,&file,b"",0)?;manifest.tables.push(crate::legacy_import::TableArchive{table:table.into(),rows:"0".into(),file,sha256:empty.clone(),primary_key:vec![],schema:Value::Null});}
  let original=put_json(dir,"original-fixed-manifest.json",&json!(manifest),1)?;
  let source=put(dir,"clock-policy.rs",include_bytes!("source_clock.rs"),1)?;
  let auditor=put(dir,"clock-auditor.py",b"# isolated deterministic fixture auditor\n",1)?;
  let witness=put_json(dir,"clock-witness.json",&json!({"kind":"isolated-four-scope-witness"}),1)?;
  let scopes=tracefang_core::source_clock::VERIFIED_V6_SCOPES.iter().map(|(provider,symbol)|json!({"provider_code":provider,"instrument_symbol":symbol,"period":"61","venue":"SHFE"})).collect::<Vec<_>>();
  let tables=manifest.tables.iter().map(|v|(v.table.clone(),json!(v))).collect::<serde_json::Map<_,_>>();
  let witnesses=json!([{"file":witness.file,"sha256":witness.sha256}]);
  let plan=json!({"schema":"legacy-source-clock-projection-v2","policy_version":"legacy-bars-fixed-authority+clock-projection-v2","source_manifest_id":manifest.id,"snapshot":manifest.postgres["snapshot"],"source_fingerprint":manifest.postgres["fingerprint"],"complete":true,"activation":false,"original_source_or_facts_modified":false,"quotes_shifted":false,"policy":{"build_sha256":build,"policy":{"policy_id":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"prior_policy_id":tracefang_core::source_clock::LEGACY_V6_OPEN_V1,"shift_quotes":false,"received_and_finalized_unchanged":true,"source_observed_unchanged":true,"scopes":scopes}},"source_tables":tables,"counts":{"input_rows":"0","output_rows":"0","point_rows":"0"},"rows":"0","sha256":empty,"original_canonical_file_sha256":empty,"original_descriptor_sha256":original.sha256,"policy_source_sha256":source.sha256,"policy_evidence":witnesses});
  let plan_ref=put_json(dir,"terminal-clock-manifest.json",&plan,1)?;
  let audit=put_json(dir,"terminal-clock-audit.json",&json!({"schema":"independent-clock-projection-audit-v1","complete":true,"projection_manifest_sha256":plan_ref.sha256,"projected_file_sha256":empty,"original_file_sha256":empty,"policy":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"oracle_source_sha256":auditor.sha256}),1)?;
  manifest.phases.push(json!({"phase":"source_clock_native_staging","original_source_manifest_sha256":original.sha256,"clock_manifest_sha256":plan_ref.sha256}));manifest.save(dir)?;
  boundary.authority_manifest_sha256=file_hash(&dir.join("manifest.json"))?;
  let fixed=put_json(dir,"fixed-input-manifest.json",&json!(manifest),1)?;
  let executor=put(dir,"fixture-terminal-executor",b"isolated test executor bytes",1)?;
  let seal=put_json(dir,"terminal-tools-manifest.json",&json!({"schema":"tracefang-migration-tools-v1","complete":true,"conflict_policy_version":COMPOSITE_POLICY,"backend_build_sha256":build,"files":[{"path":dir.join(&executor.file),"sha256":executor.sha256}],"executables":{"reconcile-probe":dir.join(&executor.file)},"clock_policy":{"source_sha256":source.sha256,"auditor_sha256":auditor.sha256,"witnesses":witnesses}}),1)?;
  let position=boundary.raw_tail.clone();let origin_coverage=json!([{"stream":boundary.legacy_stream,"epoch":boundary.legacy_epoch,"first_sequence":"42","last_sequence":"42","first_native_sequence":"1","missing_prefix":true,"initial_state":"empty_retained_prefix_no_pg_latest"}]);
  let spool=put_json(dir,"terminal-spool-audit.json",&json!({"manifest":{"schema":"retained-global-decoded-spool-v2","complete":true,"source_manifest_sha256":fixed.sha256,"build_sha256":build,"first":position,"last":position,"frames":"1","original_prefix_complete":false,"origin_coverage":origin_coverage,"canonical_decoded_sha256":empty},"original_complete_decoded_roundtrip":{"complete":true,"frames":"1","expected_sha256":empty,"actual_sha256":empty},"spool_file_sha256":empty}),1)?;
  let rejection=put(dir,"terminal-decode-rejections.ndjson",b"",0)?;
  let blank=ReconciliationArtifact{file:"pending.json".into(),sha256:empty.clone(),complete:true,row_count:0};
  let mut report=ReconciliationReport{schema:"legacy-reconciliation-v3".into(),production_terminal:false,source_manifest_id:manifest.id.clone(),stream:boundary.legacy_stream.clone(),epoch:boundary.legacy_epoch.clone(),last_sequence:boundary.legacy_tail_sequence,capture_tail:position.clone(),mapping_sha256:boundary.legacy_mapping_sha256.clone(),fixed_input_manifest_file:fixed.file,fixed_input_manifest_sha256:fixed.sha256,postgres_snapshot:boundary.postgres_snapshot.clone(),postgres_source_fingerprint:boundary.postgres_source_fingerprint.clone(),started_at_ns:1_700_000_001_000_000_000,completed_at_ns:1_700_000_002_000_000_000,early_tail_observation:LegacyTailObservation{stream:boundary.legacy_stream.clone(),epoch:boundary.legacy_epoch.clone(),last_sequence:boundary.legacy_tail_sequence,observed_at_ns:1_700_000_000_500_000_000},raw_frames_scanned:1,unresolved_differences:0,complete:true,affected_ranges:vec![json!({"symbol":"XAU/USD","source":"jin10_client","complete":true,"raw_frames_scanned":"1","unresolved_differences":"0"})],comparison:blank.clone(),overlay:blank.clone(),independent_verification:blank,verified_fact_sha256:boundary.verified_fact_sha256.clone(),verified_index_sha256:boundary.verified_index_sha256.clone(),policy:COMPOSITE_POLICY.into(),backend_build_fingerprint:build.clone(),backend_build_sha256:build.clone(),clock_projection:ClockProjectionEvidence{manifest:plan_ref.clone(),independent_audit:audit.clone(),policy_source:source.clone(),auditor_source:auditor.clone(),policy_evidence:vec![witness]},clock_binding:json!({"policy_id":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"source_manifest_id":manifest.id,"postgres_snapshot":manifest.postgres["snapshot"],"postgres_source_fingerprint":manifest.postgres["fingerprint"],"original_source_manifest_sha256":original.sha256,"original_canonical_sha256":empty,"corrected_canonical_sha256":empty,"mapping_manifest":plan_ref,"independent_audit":audit,"policy_source_sha256":source.sha256,"policy_witnesses":witnesses,"auditor_sha256":auditor.sha256,"backend_build_sha256":build}),sealed_tools:json!({"file":seal.file,"sha256":seal.sha256,"backend_build_sha256":build,"executor_sha256":executor.sha256}),capture_prefix:json!({"first":position,"last":position,"frames":"1","original_prefix_complete":false,"origin_coverage":origin_coverage,"spool_audit":spool,"canonical_decoded_sha256":empty,"spool_file_sha256":empty}),decode_rejections:json!({"file":rejection.file,"sha256":rejection.sha256,"complete":true,"row_count":"0","classified":"0","unresolved":"0"}),frame_accounting:FrameAccounting{projection_frames:0,no_output_frames:1,classified_rejection_frames:0,unresolved_frames:0},classified_rejections:rejection,classification_policy:REJECTION_POLICY.into(),quote_events_reconciled:true,initial_seed:"empty".into(),all_scopes_complete:true,quote_events_verified:0,quote_events_sha256:empty.clone(),latest_quotes_verified:0,latest_quotes_sha256:empty.clone()};
  let ledger=put(dir,"empty-selected.ndjson",b"",0)?;let binding=common_binding(&report);
  let summary_value=|schema:&str|{let mut v=binding.clone();v["schema"]=json!(schema);v["binding"]=binding.clone();v["complete"]=json!(true);v["empty_raw_seed"]=json!(true);v["unresolved_differences"]=json!("0");v["ledger"]=json!(ledger);v};
  report.comparison=put_json(dir,"comparison.json",&summary_value("legacy-reconciliation-comparison-v3"),0)?;
  report.overlay=put_json(dir,"overlay.json",&summary_value("legacy-reconciliation-overlay-v3"),0)?;
  let mut reopen=summary_value("legacy-reconciliation-reopen-v3");reopen["selected_bar_rows"]=json!("0");reopen["selected_quote_rows"]=json!("0");reopen["expected_field_sha256"]=json!(empty);reopen["actual_field_sha256"]=json!(empty);reopen["live_before"]=json!({"commit_id":"0"});reopen["live_after"]=reopen["live_before"].clone();reopen["stage_version"]=json!({"active_generation":boundary.staging_generation,"committed_capture":null,"store_epoch":proof["store_epoch"],"commit_id":proof["verified_commit_id"]});reopen["proof"]=proof.clone();reopen["original_pg_verification"]=json!({"complete":true,"source_manifest_id":manifest.id,"postgres_snapshot":manifest.postgres["snapshot"],"clock_projection_manifest_sha256":report.clock_projection.manifest.sha256});reopen["selected_input"]=json!(ledger);reopen["quote_event_ledger"]=json!(ledger);reopen["latest_quote_ledger"]=json!(ledger);
  report.independent_verification=put_json(dir,"independent-verification.json",&reopen,0)?;Ok(report)
 }
 fn contract_fixture()->Result<(tempfile::TempDir,LegacyManifest,ProjectionStartBoundary,Value,ReconciliationReport)>{
  let dir=tempfile::tempdir()?;let mut manifest=LegacyManifest::new();manifest.postgres=json!({"snapshot":"isolated-snapshot","fingerprint":"a".repeat(64),"captured_at":"2023-11-14T22:13:21Z"});manifest.raw=json!({"original_prefix_complete":false});
  let proof=json!({"complete":true,"index_verified":true,"fact_codec_sha256":"a".repeat(64),"index_codec_sha256":"b".repeat(64),"store_epoch":"isolated-store","generation":"isolated-stage","verified_commit_id":"7"});
  let tail=CapturePosition{epoch:"isolated-capture".into(),sequence:1,digest:"c".repeat(64)};
  let mut boundary=ProjectionStartBoundary{kind:"legacy_import_authority".into(),schema_version:"tracefang-legacy-authority-v1".into(),production_terminal:false,authority_manifest_id:manifest.id.clone(),authority_manifest_sha256:"d".repeat(64),postgres_source_fingerprint:"a".repeat(64),postgres_snapshot:"isolated-snapshot".into(),raw_tail:tail,legacy_stream:"OLD".into(),legacy_epoch:"OLD:epoch".into(),legacy_tail_sequence:42,legacy_mapping_sha256:"e".repeat(64),conflict_policy_version:COMPOSITE_POLICY.into(),staging_generation:"isolated-stage".into(),verified_fact_sha256:"a".repeat(64),verified_index_sha256:"b".repeat(64),closure:LegacyClosureEvidence{stopped_component_ids:vec!["isolated".into()],stop_report_sha256:"f".repeat(64),projection_drain_report_sha256:"f".repeat(64),unresolved_frames:0,raw_applied_through_legacy:None,reconciliation_report_sha256:None,stable_tail_observations:vec![]}};
  let report=fixture_v3(dir.path(),&mut manifest,&mut boundary,&proof)?;Ok((dir,manifest,boundary,proof,report))
 }
 #[test]fn v3_requires_fresh_clock_audit_compiled_policy_and_whole_build()->Result<()> {
  let(dir,manifest,boundary,proof,report)=contract_fixture()?;verify_v3(dir.path(),&manifest,&report,&boundary,&proof)?;
  let mut bad=report.clone();bad.backend_build_fingerprint="a".repeat(64);assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("compiled backend"));
  let mut bad=report.clone();let mut plan=artifact_json(dir.path(),&report.clock_projection.manifest)?;plan["snapshot"]=json!("other-rehearsal");bad.clock_projection.manifest=put_json(dir.path(),"wrong-clock.json",&plan,1)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("different fixed PostgreSQL"));
  let mut bad=report.clone();let mut audit=artifact_json(dir.path(),&report.clock_projection.independent_audit)?;audit["projected_file_sha256"]=json!("a".repeat(64));bad.clock_projection.independent_audit=put_json(dir.path(),"wrong-audit.json",&audit,1)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("independent clock audit"));
  let mut bad=report.clone();bad.clock_projection.policy_source=put(dir.path(),"wrong-policy.rs",b"another implementation",1)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("compiled policy"));
  let mut bad=report.clone();bad.capture_prefix["original_prefix_complete"]=json!(true);assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());Ok(())
 }
 #[test]fn v3_frame_classes_are_disjoint_checked_and_rejections_are_not_quotes()->Result<()> {
  let(dir,manifest,boundary,proof,report)=contract_fixture()?;
  let mut bad=report.clone();bad.frame_accounting.projection_frames=u64::MAX;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("overflow"));
  let mut bad=report.clone();bad.frame_accounting.unresolved_frames=1;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();bad.quote_events_reconciled=false;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();bad.initial_seed="fixed PG latest".into();assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();bad.classification_policy="ignore unknown errors".into();assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();bad.frame_accounting.no_output_frames=0;bad.frame_accounting.classified_rejection_frames=1;
  let mut bytes=serde_json::to_vec(&json!({"capture_position":report.capture_tail,"legacy_origin":{"stream":"OLD"},"channel":"tonghuashun_futures","body_sha256":"a".repeat(64),"diagnostic":"actual rejected response","classification":"provider_http_502_no_price","successful_quote_events":"1"}))?;bytes.push(b'\n');bad.classified_rejections=put(dir.path(),"wrong-rejection.ndjson",&bytes,1)?;bad.decode_rejections=json!({"file":bad.classified_rejections.file,"sha256":bad.classified_rejections.sha256,"complete":true,"row_count":"1","classified":"1","unresolved":"0"});
  assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).unwrap_err().to_string().contains("counted as quote success"));Ok(())
 }
 #[test]fn v3_reopen_summary_and_compact_selected_key_hash_are_actually_checked()->Result<()> {
  let(dir,manifest,boundary,proof,report)=contract_fixture()?;let mut bad=report.clone();let mut reopen=artifact_json(dir.path(),&report.independent_verification)?;
  reopen["live_after"]["commit_id"]=json!("9");bad.independent_verification=put_json(dir.path(),"wrong-reopen.json",&reopen,0)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();let mut comparison=artifact_json(dir.path(),&report.comparison)?;comparison["binding"]["capture_tail"]["sequence"]=json!("2");bad.comparison=put_json(dir.path(),"wrong-comparison.json",&comparison,0)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());
  let mut bad=report.clone();let mut reopen=artifact_json(dir.path(),&report.independent_verification)?;reopen["selected_quote_rows"]=json!("1");
  let expected="a".repeat(64);let mut compact=serde_json::to_vec(&json!({"kind":"quote","key":{"source":"jin10_client","symbol":"XAU/USD","event_id":"u64-and-ns-exact"},"expected_sha256":expected}))?;compact.push(b'\n');
  let selected=put(dir.path(),"one-selected.ndjson",&compact,1)?;let full_stream=hex::encode(Sha256::digest(format!("{expected}\n").as_bytes()));reopen["selected_input"]=json!(selected);reopen["expected_field_sha256"]=json!(full_stream);reopen["actual_field_sha256"]=json!(full_stream);
  bad.independent_verification=put_json(dir.path(),"compact-reopen.json",&reopen,1)?;verify_v3(dir.path(),&manifest,&bad,&boundary,&proof)?;
  let mut bad_shape=reopen.clone();let mut compact=serde_json::to_vec(&json!({"kind":"bar","key":{"source":"s","symbol":"x","interval":60,"open_time_ns":9007199254740993u64},"expected_sha256":expected}))?;compact.push(b'\n');bad_shape["selected_input"]=json!(put(dir.path(),"rounded-key.ndjson",&compact,1)?);bad.independent_verification=put_json(dir.path(),"rounded-reopen.json",&bad_shape,1)?;assert!(verify_v3(dir.path(),&manifest,&bad,&boundary,&proof).is_err());Ok(())
 }
 #[test]fn v3_artifacts_reject_traversal_symlink_truncation_and_oversized_json()->Result<()> {
  let dir=tempfile::tempdir()?;let valid=put(dir.path(),"line.ndjson",b"{}\n",1)?;ledger(dir.path(),&valid,|_|Ok(()))?;
  let mut wrong=valid.clone();wrong.file="../line.ndjson".into();assert!(artifact(dir.path(),&wrong).is_err());
  let wrong=put(dir.path(),"truncated.ndjson",b"{}",1)?;assert!(ledger(dir.path(),&wrong,|_|Ok(())).is_err());
  let mut wrong=valid.clone();wrong.row_count=2;assert!(ledger(dir.path(),&wrong,|_|Ok(())).is_err());
  #[cfg(unix)]{std::os::unix::fs::symlink(dir.path().join("line.ndjson"),dir.path().join("link.ndjson"))?;let mut wrong=valid.clone();wrong.file="link.ndjson".into();assert!(artifact(dir.path(),&wrong).is_err());}
  let large=put(dir.path(),"large.json",&vec![b' ';MAX_REPORT_BYTES as usize+1],0)?;assert!(artifact_json(dir.path(),&large).is_err());Ok(())
 }
 #[test]fn gzip_ledgers_validate_all_members_crc_eof_and_decoded_exact_rows()->Result<()> {
  use std::io::Write;
  let member=|bytes:&[u8]|->Result<Vec<u8>>{let mut encoder=flate2::write::GzEncoder::new(Vec::new(),flate2::Compression::default());encoder.write_all(bytes)?;Ok(encoder.finish()?)};
  let dir=tempfile::tempdir()?;let first=member(b"{\"price\":\"79228162514264337593543950335.0000000000000000000000000001\",\"volume\":null,\"sequence\":\"18446744073709551615\"}\n")?;
  let one=put(dir.path(),"one.ndjson.gz",&first,1)?;let mut rows=vec![];ledger(dir.path(),&one,|value|{rows.push(value.clone());Ok(())})?;assert_eq!(rows[0]["volume"],Value::Null);assert_eq!(rows[0]["sequence"],u64::MAX.to_string());
  let mut multiple=first.clone();multiple.extend(member(b"{\"volume\":\"0\",\"time_ns\":\"-123456789\"}\n")?);
  let both=put(dir.path(),"two.ndjson.gz",&multiple,2)?;let mut rows=vec![];assert_eq!(ledger(dir.path(),&both,|value|{rows.push(value.clone());Ok(())})?,2);assert_eq!(rows[1]["volume"],"0");
  let mut undercount=both;undercount.row_count=1;assert!(ledger(dir.path(),&undercount,|_|Ok(())).is_err());
  let mut junk=multiple.clone();junk.extend(b"not a gzip member");let junk=put(dir.path(),"trailing.ndjson.gz",&junk,2)?;assert!(ledger(dir.path(),&junk,|_|Ok(())).is_err());
  let mut corrupt=first.clone();let footer=corrupt.len()-8;corrupt[footer]^=1;let corrupt=put(dir.path(),"corrupt-crc.ndjson.gz",&corrupt,1)?;assert!(ledger(dir.path(),&corrupt,|_|Ok(())).is_err());
  let truncated=put(dir.path(),"truncated.ndjson.gz",&first[..first.len()-4],1)?;assert!(ledger(dir.path(),&truncated,|_|Ok(())).is_err());
  let mut bad_second=multiple;let last=bad_second.len()-8;bad_second[last]^=1;let bad_second=put(dir.path(),"bad-second-member.ndjson.gz",&bad_second,2)?;assert!(ledger(dir.path(),&bad_second,|_|Ok(())).is_err());
  let empty=put(dir.path(),"empty.ndjson.gz",&member(b"")?,0)?;assert_eq!(ledger(dir.path(),&empty,|_|Ok(()))?,0);
  let too_large=member(&vec![b' ';MAX_LEDGER_ROW_BYTES as usize+1])?;let too_large=put(dir.path(),"too-large-row.ndjson.gz",&too_large,1)?;assert!(ledger(dir.path(),&too_large,|_|Ok(())).is_err());Ok(())
 }
 #[test]fn production_stop_requires_bound_pid_tree_completed_attempt_and_no_restart()->Result<()> {
  let dir=tempfile::tempdir()?;let domain="gui/501/com.tracefang.local";let plist="a".repeat(64);let start="b".repeat(64);
  let inspected=json!({"schema":"legacy-installed-stop-inspection-v1","observed_at_ns":"1700000000000000000","processes_changed":false,"plist_path":"fixture.plist","plist_sha256":plist,"label":"com.tracefang.local","domain":domain,"registration":{"registered":true,"pid":10},"processes":[{"pid":10,"start_identity_sha256":start},{"pid":11,"start_identity_sha256":"c".repeat(64)}],"ready":{"process_id":11},"installed_inputs":[]});
  let inspection=put_json(dir.path(),"inspection.json",&inspected,1)?;let mut before=inspected.clone();before["observed_at_ns"]=json!("1700000000100000000");let pre=put_json(dir.path(),"pre-stop.json",&before,1)?;
  let attempt=put_json(dir.path(),"attempt.json",&json!({"schema":"legacy-stop-attempt-v1","domain":domain,"plist_sha256":plist,"phase":"stopped_process_tree_and_listener_verified","native_providers_started":false,"bootout_requested_at_ns":"1700000000200000000"}),1)?;
  let details=json!({"domain":domain,"method":"launchd_bootout_exact_job_then_process_tree_and_listener_exit","plist_sha256":plist,"plist_preserved":true,"evidence":{"inspection":inspection,"pre_stop_observation":pre,"attempt":attempt},"stable_no_restart_observations":[{"observed_at_ns":"1700000000300000000","job_registered":false,"ready_endpoint_closed":true},{"observed_at_ns":"1700000000400000000","job_registered":false,"ready_endpoint_closed":true}]});
  let stop=StopReport{schema:"legacy-stop-evidence-v1".into(),production_terminal:true,stopped_component_ids:vec![format!("{domain}:plist:{plist}"),format!("pid:10:start:{start}"),format!("pid:11:start:{}","c".repeat(64))],raw_producers_stopped:true,observed_at_ns:1_700_000_000_500_000_000,details:details.as_object().unwrap().clone()};verify_production_stop(dir.path(),&stop)?;
  let mut bad=stop.clone();bad.stopped_component_ids.pop();assert!(verify_production_stop(dir.path(),&bad).is_err());
  let mut bad=stop.clone();bad.details.get_mut("stable_no_restart_observations").unwrap()[1]["job_registered"]=json!(true);assert!(verify_production_stop(dir.path(),&bad).is_err());
  let mut bad=stop;bad.details.remove("evidence");assert!(verify_production_stop(dir.path(),&bad).is_err());Ok(())
 }
 #[tokio::test]async fn closure_checks_actual_anchor_reports_and_rejects_ack_only()->Result<()> {
  let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw.redb"),Default::default())?;
  let frame=ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"boundary-fixture".into(),sequence:u64::MAX,received_at:chrono::DateTime::from_timestamp(1_700_000_000,987654321).unwrap(),encoding:"wire".into(),body:vec![1,2,3]};
  let receipt=capture.append_legacy(&frame,LegacyOrigin{stream:"OLD".into(),epoch:"OLD:epoch".into(),sequence:"42".into(),broker_stored_at_ns:"1700000000999999999".into()}).await?;
  let map_path=dir.path().join("mapping.ndjson");std::fs::write(&map_path,b"fixed mapping evidence\n")?;
  let mut manifest=LegacyManifest::new();manifest.postgres=json!({"snapshot":"fixed-fixture-snapshot","fingerprint":"fixed-fixture-pg","captured_at":"2027-01-15T08:00:01Z"});
  // Exact test clocks use the same ns base; no original frame clock is replaced.
  manifest.postgres["captured_at"]=json!(chrono::DateTime::from_timestamp(1_700_000_001,0).unwrap());
  manifest.raw=json!({"original_prefix_complete":false,"native_mapping":{"file":"mapping.ndjson","sha256":file_hash(&map_path)?,"last_position":receipt.position}});manifest.save(dir.path())?;
  let store=Store::open(dir.path().join("facts.redb"))?;let stage=store.staging("test-authority").await?;let proof=stage.verify_staging().await?;
  let stop=StopReport{schema:"legacy-stop-evidence-v1".into(),production_terminal:false,stopped_component_ids:vec!["isolated-fixture-producer".into()],raw_producers_stopped:true,observed_at_ns:1_700_000_000_000_000_000,details:Default::default()};
  let mut drain=DrainReport{schema:"legacy-drain-evidence-v1".into(),production_terminal:false,stream:"OLD".into(),epoch:"OLD:epoch".into(),method:"application_writer_clean_drain".into(),raw_applied_through_legacy:Some(42),unresolved_frames:0,observed_at_ns:stop.observed_at_ns};
  let files=ClosureEvidenceFiles{stop_report:dir.path().join("stop.json"),drain_report:dir.path().join("drain.json"),reconciliation_report:None};crate::legacy_import::atomic_json(&files.stop_report,&stop)?;crate::legacy_import::atomic_json(&files.drain_report,&drain)?;
  let mut boundary=ProjectionStartBoundary{kind:"legacy_import_authority".into(),schema_version:"tracefang-legacy-authority-v1".into(),production_terminal:false,authority_manifest_id:manifest.id.clone(),authority_manifest_sha256:file_hash(&dir.path().join("manifest.json"))?,postgres_source_fingerprint:"fixed-fixture-pg".into(),postgres_snapshot:"fixed-fixture-snapshot".into(),raw_tail:receipt.position.clone(),legacy_stream:"OLD".into(),legacy_epoch:"OLD:epoch".into(),legacy_tail_sequence:42,legacy_mapping_sha256:file_hash(&map_path)?,conflict_policy_version:"legacy-bars-fixed-authority-v1".into(),staging_generation:"test-authority".into(),verified_fact_sha256:proof["fact_codec_sha256"].as_str().unwrap().into(),verified_index_sha256:proof["index_codec_sha256"].as_str().unwrap().into(),closure:LegacyClosureEvidence{stopped_component_ids:stop.stopped_component_ids.clone(),stop_report_sha256:file_hash(&files.stop_report)?,projection_drain_report_sha256:file_hash(&files.drain_report)?,unresolved_frames:0,raw_applied_through_legacy:Some(42),reconciliation_report_sha256:None,stable_tail_observations:vec![LegacyTailObservation{stream:"OLD".into(),epoch:"OLD:epoch".into(),last_sequence:42,observed_at_ns:1_700_000_002_000_000_000},LegacyTailObservation{stream:"OLD".into(),epoch:"OLD:epoch".into(),last_sequence:42,observed_at_ns:1_700_000_003_000_000_000}]}};
  let checked=verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await?;ensure!(checked["state"]=="verified_isolated_fixture_no_production_authority","fixture claimed terminal production authority");
  store.activate_staging_with_boundary("test-authority",proof.clone(),boundary.clone()).await?;ensure!(store.version().await?.committed_capture.is_none(),"legacy raw was falsely marked projected");
  let mut bad=boundary.clone();bad.raw_tail.digest="a".repeat(64);ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&bad,&proof,&files).await.is_err(),"wrong raw anchor accepted");
  drain.method="consumer_ack_only".into();crate::legacy_import::atomic_json(&files.drain_report,&drain)?;boundary.closure.projection_drain_report_sha256=file_hash(&files.drain_report)?;
  ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"consumer ack was treated as facts transaction proof");
  // Independent reconciliation finishes AFTER the immutable PG input. Its
  // later confirmation clock must never be backfilled into the earlier stop.
  let mut report=fixture_v3(dir.path(),&mut manifest,&mut boundary,&proof)?;
  let reconciliation_path=dir.path().join("reconciliation.json");let files=ClosureEvidenceFiles{reconciliation_report:Some(reconciliation_path.clone()),..files};
  drain.method="independent_retained_raw_reconciliation".into();drain.raw_applied_through_legacy=None;drain.observed_at_ns=1_700_000_003_000_000_000;
  boundary.conflict_policy_version=COMPOSITE_POLICY.into();boundary.closure.raw_applied_through_legacy=None;
  boundary.closure.stable_tail_observations[0].observed_at_ns=1_700_000_004_000_000_000;boundary.closure.stable_tail_observations[1].observed_at_ns=1_700_000_005_000_000_000;
  crate::legacy_import::atomic_json(&files.drain_report,&drain)?;boundary.closure.projection_drain_report_sha256=file_hash(&files.drain_report)?;
  let bind=|boundary:&mut ProjectionStartBoundary,report:&ReconciliationReport|->Result<()>{crate::legacy_import::atomic_json(&reconciliation_path,report)?;boundary.closure.reconciliation_report_sha256=Some(file_hash(&reconciliation_path)?);Ok(())};bind(&mut boundary,&report)?;
  verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await?;
  let valid=report.clone();report.completed_at_ns=drain.observed_at_ns+1;bind(&mut boundary,&report)?;ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"future reconciliation completion accepted as prior drain");
  report=valid.clone();report.fixed_input_manifest_sha256="a".repeat(64);bind(&mut boundary,&report)?;ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"different frozen PG manifest accepted");
  report=valid.clone();report.overlay.complete=false;bind(&mut boundary,&report)?;ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"unfinished overlay accepted");
  report=valid.clone();report.early_tail_observation.last_sequence+=1;bind(&mut boundary,&report)?;ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"moving pre-snapshot tail accepted");
  report=valid;report.unresolved_differences=1;bind(&mut boundary,&report)?;ensure!(verify_authority_boundary(dir.path(),&manifest,&capture,&boundary,&proof,&files).await.is_err(),"unresolved overlay difference accepted");
  store.close().await?;capture.close_and_drain().await?;Ok(())
 }
}
