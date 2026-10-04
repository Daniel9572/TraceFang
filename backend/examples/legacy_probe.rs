//! Read-only legacy archive tool; offline tests reuse its exact adapters.
#[path = "../src/capture.rs"]
mod capture;
#[path = "../src/legacy_boundary.rs"]
mod legacy_boundary;
#[path = "../src/legacy_import.rs"]
mod legacy_import;
#[path = "../src/legacy_reconcile.rs"]
mod legacy_reconcile;
#[path = "../src/legacy_verify.rs"]
mod legacy_verify;
use anyhow::{Context, Result, ensure};
use legacy_import::LegacyManifest;
use legacy_import::LegacySink;
use std::path::Path;
use tracefang_core::{
    native_store::Store,
    persistence_contract::{ImportBarRow, ImportBatch, ImportMetadataRow, ImportQuoteRow},
};
impl LegacySink for Store {
    fn bars(
        &self,
        batch: ImportBatch<ImportBarRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move { Ok(serde_json::to_value(self.import_bars(batch).await?)?) })
    }
    fn quotes(
        &self,
        batch: ImportBatch<ImportQuoteRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move { Ok(serde_json::to_value(self.import_quotes(batch).await?)?) })
    }
    fn metadata(
        &self,
        batch: ImportBatch<ImportMetadataRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move { Ok(serde_json::to_value(self.import_metadata(batch).await?)?) })
    }
}
struct BudgetStore {
    store: Store,
    path: std::path::PathBuf,
    cap: u64,
}
impl BudgetStore {
    fn check(&self) -> Result<()> {
        ensure!(
            capture::available_bytes(self.path.parent().context("facts path parent missing")?)?
                > 4 * 1024 * 1024 * 1024,
            "inactive import stopped: available disk below 4GiB; preserved source and receipts"
        );
        ensure!(
            std::fs::metadata(&self.path)?.len() < self.cap,
            "inactive import stopped: target exceeded 16GiB file budget; preserved source and receipts"
        );
        Ok(())
    }
}
impl LegacySink for BudgetStore {
    fn bars(
        &self,
        batch: ImportBatch<ImportBarRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move {
            self.check()?;
            Ok(serde_json::to_value(self.store.import_bars(batch).await?)?)
        })
    }
    fn quotes(
        &self,
        batch: ImportBatch<ImportQuoteRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move {
            self.check()?;
            Ok(serde_json::to_value(
                self.store.import_quotes(batch).await?,
            )?)
        })
    }
    fn metadata(
        &self,
        batch: ImportBatch<ImportMetadataRow>,
    ) -> std::pin::Pin<Box<dyn std::future::Future<Output = Result<serde_json::Value>> + Send + '_>>
    {
        Box::pin(async move {
            self.check()?;
            Ok(serde_json::to_value(
                self.store.import_metadata(batch).await?,
            )?)
        })
    }
}

fn facts_identity(path: &Path) -> Result<serde_json::Value> {
    let metadata = std::fs::metadata(path)?;
    ensure!(metadata.is_file(), "facts must be a regular file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        Ok(
            serde_json::json!({"dev":metadata.dev(),"inode":metadata.ino(),"bytes":metadata.len(),"mtime_ns":metadata.mtime() as i128*1_000_000_000+metadata.mtime_nsec() as i128,"ctime_ns":metadata.ctime() as i128*1_000_000_000+metadata.ctime_nsec() as i128}),
        )
    }
    #[cfg(not(unix))]
    {
        Ok(
            serde_json::json!({"bytes":metadata.len(),"modified":format!("{:?}",metadata.modified()?)}),
        )
    }
}
fn read_small_json(path: &Path) -> Result<(serde_json::Value, String)> {
    use sha2::Digest;
    use std::io::Read;
    let mut file = std::fs::File::open(path)?;
    let before = facts_identity(path)?;
    let expected_bytes = file.metadata()?.len();
    ensure!(
        file.metadata()?.len() <= 4 * 1024 * 1024,
        "readback input exceeds4MiB"
    );
    let mut bytes = vec![];
    file.read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 == expected_bytes
            && expected_bytes == file.metadata()?.len()
            && before == facts_identity(path)?,
        "readback input changed or cloud read incomplete"
    );
    Ok((
        serde_json::from_slice(&bytes)?,
        hex::encode(sha2::Sha256::digest(bytes)),
    ))
}
fn exact_count(value: &serde_json::Value) -> Result<u64> {
    if let Some(value) = value.as_u64() {
        return Ok(value);
    }
    Ok(value.as_str().context("exact count missing")?.parse()?)
}
fn proof_binds_version(proof: &serde_json::Value, version: &serde_json::Value) -> Result<()> {
    ensure!(
        proof["complete"] == true && proof["index_verified"] == true,
        "complete issued proof required"
    );
    for (proof_key, version_key) in [
        ("store_epoch", "store_epoch"),
        ("generation", "active_generation"),
        ("schema_version", "schema_version"),
        ("aggregation_version", "aggregation_version"),
        ("verified_commit_id", "commit_id"),
    ] {
        ensure!(
            !proof[proof_key].is_null() && proof[proof_key] == version[version_key],
            "issued proof version differs: {proof_key}"
        );
    }
    Ok(())
}
fn validate_state_readback(basis: &serde_json::Value, actual: &serde_json::Value) -> Result<()> {
    let source = &basis["source"];
    let plan = &basis["clock"];
    let oracle = &basis["oracle"];
    let repair = &basis["repair"];
    let initial = &basis["initial"];
    let full = &basis["full_reopen"];
    let receipt = &actual["repair_receipt"];
    let proof = &actual["proof"];
    let clock = &actual["clock"];
    let descriptor_sha = &basis["clock_manifest_sha256"];
    ensure!(
        source["id"].is_string()
            && plan["complete"] == true
            && plan["schema"] == "legacy-source-clock-projection-v2"
            && plan["source_manifest_id"] == source["id"]
            && plan["snapshot"] == source["postgres"]["snapshot"]
            && plan["policy"]["policy"]["policy_id"]
                == tracefang_core::source_clock::THS_V6_SHFE_END_V2,
        "clock descriptor source/policy differs"
    );
    ensure!(
        oracle["schema"] == "independent-clock-series-state-v1"
            && oracle["complete"] == true
            && oracle["source_manifest_id"] == source["id"]
            && oracle["snapshot"] == source["postgres"]["snapshot"]
            && oracle["clock_manifest_sha256"] == *descriptor_sha
            && oracle["canonical_sha256"] == plan["sha256"]
            && oracle["all_canonical_rows_scanned"] == plan["rows"]
            && oracle["policy"] == tracefang_core::source_clock::THS_V6_SHFE_END_V2,
        "complete bound state oracle required"
    );
    ensure!(
        full["complete"] == true
            && full["closed"] == true
            && full["helper_exit_code"] == 0
            && full["facts_activation"] == false
            && full["production_modified"] == false,
        "prior full-data reopen gate incomplete"
    );
    for key in ["bar_rows", "quote_rows", "quote_event_identities"] {
        ensure!(
            exact_count(&full[key])? > 0,
            "prior full-data count missing: {key}"
        );
    }
    ensure!(
        initial["complete"] == true
            && initial["activated"] == false
            && initial["before"] == initial["after"]
            && initial["clock_manifest_sha256"] == *descriptor_sha
            && actual["active_version"] == initial["before"]
            && actual["active_version"]["committed_capture"].is_null(),
        "active authority changed or original import gate differs"
    );
    ensure!(
        repair["phase"] == "runtime_clock_series_state_repair"
            && repair["state"] == "complete_inactive"
            && repair["activated"] == false
            && receipt == &repair["receipt"]
            && receipt["schema"] == "verified-clock-series-state-repair-v1"
            && receipt["complete"] == true
            && receipt["facts_quotes_index_written"] == false
            && receipt["initial_full_verification_reused_only_for_metadata_transaction"] == true
            && receipt["clock_manifest_sha256"] == *descriptor_sha
            && receipt["before"] == full["version"]
            && receipt["after"] == actual["staged_version"]
            && actual["staged_version"]["committed_capture"].is_null(),
        "stored repair receipt or staged version differs"
    );
    ensure!(
        actual["staged_version"]["active_generation"]
            == format!("legacy-{}", source["id"].as_str().unwrap())
            && actual["active_version"]["active_generation"]
                != actual["staged_version"]["active_generation"],
        "repaired stage is active or wrong generation"
    );
    proof_binds_version(&initial["proof"], &receipt["before"])?;
    proof_binds_version(proof, &receipt["after"])?;
    let mut expected_proof = initial["proof"].clone();
    expected_proof["verified_commit_id"] = receipt["after"]["commit_id"].clone();
    ensure!(
        *proof == expected_proof
            && *proof == receipt["proof"]
            && *proof == repair["proof"]
            && proof["fact_rows"] == full["bar_rows"],
        "original fact/index proof changed outside verified commit"
    );
    for key in ["fact_codec_sha256", "index_codec_sha256"] {
        ensure!(
            proof[key]
                .as_str()
                .is_some_and(|s| hex::decode(s).is_ok_and(|bytes| bytes.len() == 32)),
            "issued codec digest invalid"
        );
    }
    let mut expected_after = receipt["before"].clone();
    expected_after["commit_id"] = serde_json::json!(
        exact_count(&receipt["before"]["commit_id"])?
            .checked_add(1)
            .context("commit overflow")?
            .to_string()
    );
    ensure!(
        expected_after == receipt["after"],
        "repair changed version outside one metadata commit"
    );
    ensure!(
        clock["verified"] == true
            && clock["policy"] == tracefang_core::source_clock::THS_V6_SHFE_END_V2
            && clock["source_manifest_id"] == source["id"]
            && clock["snapshot"] == source["postgres"]["snapshot"]
            && clock["mapping_manifest_sha256"] == *descriptor_sha
            && clock["policy_source_sha256"] == plan["policy_source_sha256"]
            && clock["original_archive_sha256"] == plan["original_canonical_file_sha256"],
        "stored verified clock basis differs"
    );
    let scopes = oracle["scopes"]
        .as_array()
        .context("four oracle scopes missing")?;
    let repaired = receipt["states"]
        .as_array()
        .context("four repaired scopes missing")?;
    ensure!(
        scopes.len() == 4
            && repaired.len() == 4
            && actual["states"]
                .as_object()
                .context("stored states missing")?
                .len()
                == 4,
        "exactly four state scopes required"
    );
    let mut seen = std::collections::BTreeSet::new();
    for scope in scopes {
        let candidate: tracefang_core::reducer::SeriesState =
            serde_json::from_value(scope["candidate"].clone())?;
        let symbol = &candidate.instrument_symbol;
        ensure!(
            tracefang_core::source_clock::VERIFIED_V6_SCOPES
                .iter()
                .any(|(_, s)| *s == symbol)
                && seen.insert(symbol.clone())
                && candidate.realtime_source_id == "tonghuashun_futures"
                && candidate.interval_seconds == 60
                && candidate.history_floor.is_none()
                && candidate.tail_checked_through.is_none()
                && candidate.tail_checked_at.is_none(),
            "oracle invented state authority or duplicate scope"
        );
        let key = format!("tonghuashun_futures:{symbol}");
        let stored: tracefang_core::reducer::SeriesState =
            serde_json::from_value(actual["states"][&key].clone())?;
        let row = repaired
            .iter()
            .find(|r| r["key"] == key)
            .context("repair state missing")?;
        let reported: tracefang_core::reducer::SeriesState =
            serde_json::from_value(row["state"].clone())?;
        ensure!(
            stored == candidate && reported == candidate,
            "stored state differs from oracle/receipt"
        );
        for count in [
            "authority_rows",
            "confirmed_authority_rows",
            "final_clock_unknown_rows",
        ] {
            ensure!(
                exact_count(&row[count])? == exact_count(&scope["derivation"][count])?,
                "repair count differs: {count}"
            );
        }
        if let Some(evidence) = actual["state_evidence"].get(&key) {
            ensure!(
                evidence["basis_clock_manifest_sha256"] == *descriptor_sha
                    && evidence["evidence"]["oracle_sha256"] == basis["oracle_sha256"],
                "stored state evidence differs"
            );
        }
    }
    Ok(())
}
async fn clock_state_readback(args: &[String]) -> Result<()> {
    ensure!(
        args.len() == 10,
        "clock-state-readback source-dir facts-file clock-dir state-oracle repair-report basis-store-proof-report basis-full-reopen-report output-report"
    );
    let report = Path::new(&args[9]);
    ensure!(!report.exists(), "fresh readback report required");
    let paths = [
        ("source", Path::new(&args[2]).join("manifest.json")),
        (
            "clock",
            Path::new(&args[4]).join("canonical-bars-clock-v2.manifest.json"),
        ),
        ("oracle", args[5].clone().into()),
        ("repair", args[6].clone().into()),
        ("initial", args[7].clone().into()),
        ("full_reopen", args[8].clone().into()),
    ];
    let mut basis = serde_json::json!({});
    let mut hashes = serde_json::json!({});
    for (role, path) in &paths {
        let (value, sha) = read_small_json(path)?;
        basis[*role] = value;
        hashes[*role] = serde_json::json!({"file":path,"sha256":sha});
    }
    basis["clock_manifest_sha256"] = hashes["clock"]["sha256"].clone();
    basis["oracle_sha256"] = hashes["oracle"]["sha256"].clone();
    let facts = Path::new(&args[3]);
    let before = facts_identity(facts)?;
    let store = Store::open_read_only_bounded(facts, 128 * 1024 * 1024)?;
    let attempt=async {
        let stage=store.read_generation(&format!("legacy-{}",basis["source"]["id"].as_str().context("source id missing")?)).await?;
        let mut states=serde_json::json!({}); let mut evidence=serde_json::json!({});
        for (_,symbol) in tracefang_core::source_clock::VERIFIED_V6_SCOPES {
            let key=format!("tonghuashun_futures:{symbol}"); states[&key]=stage.metadata("series_state",&key).await?.context("stored state missing")?;
            if let Some(row)=stage.metadata("metadata_evidence",&serde_json::to_string(&("series_state",&key))?).await? { evidence[&key]=row; }
        }
        let actual=serde_json::json!({"active_version":store.version().await?,"staged_version":stage.version().await?,"states":states,"state_evidence":evidence,"proof":stage.metadata("migration","index_verification").await?.context("issued proof missing")?,"repair_receipt":stage.metadata("migration","series_state_clock_repair").await?.context("repair receipt missing")?,"clock":stage.metadata("migration","source_clock_policy").await?.context("verified clock missing")?});
        validate_state_readback(&basis,&actual)?; Ok::<_,anyhow::Error>(actual)
    }.await;
    store.close().await?;
    let actual = attempt?;
    let after = facts_identity(facts)?;
    ensure!(before == after, "RO readback changed facts identity");
    for (role, path) in &paths {
        ensure!(
            read_small_json(path)?.1
                == hashes[*role]["sha256"]
                    .as_str()
                    .context("input digest missing")?,
            "readback input changed"
        );
    }
    legacy_import::atomic_json(
        report,
        &serde_json::json!({"schema":"corrected-clock-series-state-readback-v1","complete":true,"verification_scope":"independent RO metadata/proof/version reopen; prior full-data oracle and Store atomic metadata-only transaction reused","input_file_sha256":hashes,"reused_full_data_gates":{"initial_store_report":basis["initial"],"complete_source_reopen_report":basis["full_reopen"]},"active_version":actual["active_version"],"staged_version":actual["staged_version"],"four_states":actual["states"],"state_evidence":actual["state_evidence"],"issued_proof":actual["proof"],"stored_repair_receipt":actual["repair_receipt"],"facts_identity_before":before,"facts_identity_after":after,"facts_identity_unchanged":true,"read_cache_bytes":"134217728","facts_or_events_rows_rescanned":"0","fact_index_sha_recomputed":false,"activation":false,"production_modified":false,"closed":true}),
    )?;
    Ok(())
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    let mode=args.get(1).context("usage: legacy_probe inventory|export|raw-import|raw-verify|raw-bounds|facts-import DIRECTORY [TARGET_REDB]")?;
    if mode == "identity" {
        println!(
            "{}",
            serde_json::json!({"schema":"tracefang-migration-tool-identity-v1","tool":"legacy_probe","backend_build_sha256":tracefang_core::quant_core::results::backend_build_fingerprint(),"version":env!("CARGO_PKG_VERSION")})
        );
        return Ok(());
    }
    if mode == "clock-state-readback" {
        return clock_state_readback(&args).await;
    }
    let dir = Path::new(args.get(2).context("archive directory required")?);
    std::fs::create_dir_all(dir)?;
    if mode == "clock-state-repair" {
        ensure!(
            args.len() == 9,
            "clock-state-repair source-dir facts-file clock-dir progress-dir independent-clock-audit state-oracle report-file"
        );
        let clock = Path::new(&args[4]);
        let progress = Path::new(&args[5]);
        let audit = Path::new(&args[6]);
        let oracle_path = Path::new(&args[7]);
        let report = Path::new(&args[8]);
        let original: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let plan = legacy_import::checked_clock_projection(dir, clock, audit, &original)?;
        let oracle: serde_json::Value = serde_json::from_slice(&std::fs::read(oracle_path)?)?;
        ensure!(
            oracle["schema"] == "independent-clock-series-state-v1"
                && oracle["complete"] == true
                && oracle["source_manifest_id"] == original.id
                && oracle["snapshot"] == original.postgres["snapshot"]
                && oracle["clock_manifest_sha256"] == plan["_descriptor_sha256"]
                && oracle["canonical_sha256"] == plan["sha256"]
                && oracle["all_canonical_rows_scanned"] == plan["rows"]
                && oracle["policy"] == tracefang_core::source_clock::THS_V6_SHFE_END_V2,
            "state oracle is not bound to this complete fixed clock input"
        );
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(progress.join("manifest.json"))?)?;
        ensure!(
            manifest.id == original.id && manifest.postgres == original.postgres,
            "progress belongs to another source"
        );
        let rows=oracle["scopes"].as_array().context("scope states missing")?.iter().map(|scope|->Result<_>{
   let candidate:tracefang_core::reducer::SeriesState=serde_json::from_value(scope["candidate"].clone())?;let before:tracefang_core::reducer::SeriesState=serde_json::from_value(scope["original"]["row"].clone())?;
   let derivation=&scope["derivation"];let mut counts=serde_json::json!({});for key in ["authority_rows","confirmed_authority_rows","final_clock_unknown_rows"]{counts[key]=serde_json::json!(derivation[key].as_u64().context("oracle basis count must be exact integer")?.to_string());}
   Ok(ImportMetadataRow{namespace:"series_state".into(),key:format!("tonghuashun_futures:{}",candidate.instrument_symbol),value:serde_json::to_value(candidate)?,evidence:serde_json::json!({"original_state":before,"original_pg_state":scope["original"],"clock_state_derivation":counts,"boundary_ref":derivation["boundary_ref"],"maximum_received_ref":derivation["received_ref"],"oracle_file":oracle_path,"oracle_sha256":legacy_import::file_hash(oracle_path)?,"semantics":"corrected fixed final-history authority only; unknown confirmations and provisional tails do not advance watermark","history_floor":"unknown continuous coverage","tail_check":"no corrected independent tailcheck available"})})
  }).collect::<Result<Vec<_>>>()?;
        let store = Store::open(&args[3])?;
        let live_before = store.version().await?;
        let stage = store
            .read_generation(&format!("legacy-{}", manifest.id))
            .await?;
        let proof = stage
            .metadata("migration", "index_verification")
            .await?
            .context("initial staging verification missing")?;
        let started = std::time::Instant::now();
        let repaired = stage
            .import_verified_series_state_metadata(
                rows,
                proof,
                std::fs::read(clock.join("canonical-bars-clock-v2.manifest.json"))?,
            )
            .await?;
        ensure!(
            store.version().await? == live_before,
            "state repair changed live authority"
        );
        manifest.phases.push(serde_json::json!({"phase":"runtime_clock_series_state_repair","state":"complete_inactive","proof":repaired["proof"],"receipt":repaired,"elapsed_ms":started.elapsed().as_secs_f64()*1000.,"activated":false}));
        manifest.save(progress)?;
        legacy_import::atomic_json(
            report,
            &manifest.phases.last().context("repair phase missing")?,
        )?;
        store.close().await?;
        return Ok(());
    }
    if mode == "facts-clock-import" {
        ensure!(
            args.len() == 9,
            "facts-clock-import source-dir fresh-facts-file clock-dir fresh-progress-dir independent-clock-audit report-file"
        );
        let target = std::path::PathBuf::from(&args[3]);
        let clock = Path::new(&args[4]);
        let progress = Path::new(&args[5]);
        let audit = Path::new(&args[6]);
        let report = Path::new(&args[7]);
        // The last argument is an explicit bytes cap: source-derived rehearsal may
        // measure a tighter bound, but no stop authority follows from this admission.
        let cap: u64 = args[8].parse()?;
        ensure!(
            cap > 0 && cap <= 16 * 1024 * 1024 * 1024,
            "invalid inactive facts file cap"
        );
        ensure!(
            !target.exists() && !progress.exists(),
            "corrected import requires fresh isolated facts/progress paths"
        );
        std::fs::create_dir_all(target.parent().context("new facts parent missing")?)?;
        ensure!(
            capture::available_bytes(target.parent().unwrap())? > cap + 4 * 1024 * 1024 * 1024,
            "fresh inactive stage cap plus4GiB reserve does not fit; production unchanged"
        );
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        legacy_import::checked_clock_projection(dir, clock, audit, &manifest)?;
        let original_manifest_sha = legacy_import::file_hash(&dir.join("manifest.json"))?;
        std::fs::create_dir_all(progress)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(progress, std::fs::Permissions::from_mode(0o700))?;
        }
        // Stable source aliases make every manifest/table offset independently reopenable.
        // Never link the mutable progress manifest to the original manifest.
        let mut files = manifest
            .tables
            .iter()
            .map(|v| v.file.clone())
            .collect::<Vec<_>>();
        files.extend(
            manifest
                .configs
                .iter()
                .filter_map(|v| v["file"].as_str().map(str::to_owned)),
        );
        files.extend(
            [
                manifest.raw["file"].as_str(),
                manifest.raw["native_mapping"]["file"].as_str(),
            ]
            .into_iter()
            .flatten()
            .map(str::to_owned),
        );
        for file in files {
            ensure!(
                Path::new(&file)
                    .file_name()
                    .is_some_and(|v| v == std::ffi::OsStr::new(&file)),
                "source alias escapes archive"
            );
            std::fs::hard_link(dir.join(&file), progress.join(file))?;
        }
        manifest.phases.push(serde_json::json!({"phase":"source_clock_native_staging","state":"started","original_source_manifest":dir.join("manifest.json"),"original_source_manifest_sha256":original_manifest_sha,"clock_manifest":clock.join("canonical-bars-clock-v2.manifest.json"),"clock_manifest_sha256":legacy_import::file_hash(&clock.join("canonical-bars-clock-v2.manifest.json"))?,"production_modified":false}));
        manifest.save(progress)?;
        let store = Store::open(&target)?;
        let before = store.version().await?;
        let generation = format!("legacy-{}", manifest.id);
        let stage = store.staging(&generation).await?;
        let budget = BudgetStore {
            store: stage.clone(),
            path: target.clone(),
            cap,
        };
        let started = std::time::Instant::now();
        legacy_import::import_clock_tables(dir, clock, progress, audit, &mut manifest, &budget)
            .await?;
        let proof = stage.verify_staging().await?;
        let after = store.version().await?;
        ensure!(
            before.active_generation == after.active_generation
                && before.committed_capture == after.committed_capture
                && after.committed_capture.is_none(),
            "corrected staging changed live authority"
        );
        ensure!(
            legacy_import::file_hash(&dir.join("manifest.json"))? == original_manifest_sha,
            "corrected import modified original source manifest"
        );
        let result = serde_json::json!({"phase":"corrected_clock_inactive_staging","complete":true,"target":target,"generation":generation,"facts_file_bytes":std::fs::metadata(&target)?.len().to_string(),"facts_file_cap_bytes":cap.to_string(),"proof":proof,"before":before,"after":after,"total_ms":started.elapsed().as_secs_f64()*1000.,"original_source_manifest_sha256":original_manifest_sha,"clock_manifest_sha256":legacy_import::file_hash(&clock.join("canonical-bars-clock-v2.manifest.json"))?,"progress_manifest":progress.join("manifest.json"),"production_terminal":false,"activated":false,"independent_reopen_bars_and_quotes":"required separately; index self-consistency alone is not source mapping proof"});
        manifest.phases.push(result.clone());
        manifest.save(progress)?;
        legacy_import::atomic_json(report, &result)?;
        store.close().await?;
        println!(
            "corrected source-clock facts verified inactive; no activation or production stop"
        );
        return Ok(());
    }
    if mode == "tail-observe" {
        let _ = dotenvy::from_filename(".env.local");
        let _ = dotenvy::dotenv();
        let url = std::env::var("TRACEFANG_LEGACY_NATS_URL")
            .or_else(|_| std::env::var("TRACEFANG_NATS_URL"))
            .context("legacy NATS URL not configured")?;
        let name =
            std::env::var("TRACEFANG_NATS_STREAM").unwrap_or_else(|_| "MARKET_RAW_FRAMES".into());
        let client = async_nats::ConnectOptions::new()
            .connection_timeout(std::time::Duration::from_secs(3))
            .connect(url)
            .await
            .map_err(|_| anyhow::anyhow!("legacy NATS read connection unavailable"))?;
        let mut stream = async_nats::jetstream::new(client.clone())
            .get_stream(&name)
            .await?;
        let info = stream.info().await?;
        let observation = tracefang_core::persistence_contract::LegacyTailObservation {
            stream: name.clone(),
            epoch: format!("{}:{}", name, info.created.unix_timestamp_nanos()),
            last_sequence: info.state.last_sequence,
            observed_at_ns: chrono::Utc::now()
                .timestamp_nanos_opt()
                .context("observation clock outside ns")?,
        };
        legacy_import::atomic_json(
            Path::new(args.get(3).context("tail observation report required")?),
            &observation,
        )?;
        client.drain().await?;
        println!("read-only legacy tail observation saved");
        return Ok(());
    }
    if mode == "boundary-verify" || mode == "boundary-activate" {
        let manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let spec: serde_json::Value = serde_json::from_slice(&std::fs::read(
            args.get(3).context("handoff request file required")?,
        )?)?;
        let boundary: tracefang_core::persistence_contract::ProjectionStartBoundary =
            serde_json::from_value(spec["boundary"].clone())?;
        let files = legacy_boundary::ClosureEvidenceFiles {
            stop_report: spec["stop_report"]
                .as_str()
                .context("stop evidence path required")?
                .into(),
            drain_report: spec["drain_report"]
                .as_str()
                .context("drain evidence path required")?
                .into(),
            reconciliation_report: spec["reconciliation_report"].as_str().map(Into::into),
        };
        let facts = spec["facts_path"].as_str().context("facts path required")?;
        let raw = spec["capture_path"]
            .as_str()
            .context("capture path required")?;
        let cap = capture::Capture::open_read_only(raw)?;
        let store = if mode == "boundary-activate" {
            Store::open(facts)?
        } else {
            Store::open_read_only(facts)?
        };
        let stage = store.read_generation(&boundary.staging_generation).await?;
        let current = stage.verify_index().await?;
        let verification = stage
            .metadata("migration", "index_verification")
            .await?
            .context("Store-issued staged verification missing")?;
        for key in [
            "fact_codec_sha256",
            "index_codec_sha256",
            "complete",
            "index_verified",
        ] {
            ensure!(
                current[key] == verification[key],
                "staged verification no longer matches current {key}"
            );
        }
        ensure!(
            verification["verified_commit_id"].as_str()
                == Some(stage.version().await?.commit_id.to_string().as_str()),
            "stage changed since verification"
        );
        let proof = legacy_boundary::verify_authority_boundary(
            dir,
            &manifest,
            &cap,
            &boundary,
            &verification,
            &files,
        )
        .await?;
        let before = store.version().await?;
        if mode == "boundary-activate" {
            ensure!(
                boundary.production_terminal,
                "rehearsal request cannot activate terminal runtime"
            );
            store
                .activate_staging_with_boundary(
                    &boundary.staging_generation,
                    verification,
                    boundary.clone(),
                )
                .await?;
        }
        let report = serde_json::json!({"phase":mode,"proof":proof,"before":before,"after":store.version().await?,"boundary":store.projection_start_boundary().await?,"providers_started":false,"legacy_services_stopped_by_this_tool":false});
        legacy_import::atomic_json(
            Path::new(
                spec["report_path"]
                    .as_str()
                    .context("result report required")?,
            ),
            &report,
        )?;
        store.close().await?;
        cap.close_and_drain().await?;
        println!("authority request validated; receipt saved; providers unchanged");
        return Ok(());
    }
    if mode == "facts-verify" || mode == "quotes-verify" {
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let store = Store::open_read_only_bounded(
            args.get(3)
                .context("reopened inactive facts path required")?,
            128 * 1024 * 1024,
        )?;
        let stage = store
            .read_generation(&format!("legacy-{}", manifest.id))
            .await?;
        let proof = if mode == "quotes-verify" {
            serde_json::json!({"quotes":legacy_verify::verify_quotes(dir,&manifest,&stage).await?})
        } else {
            serde_json::json!({"bars":legacy_verify::verify_bars(dir,&manifest,&stage).await?})
        };
        manifest.phases.push(serde_json::json!({"phase":"postgres_independent_verify","proof":proof,"activation":false,"store_open_mode":"OS read only"}));
        manifest.save(dir)?;
        store.close().await?;
        println!("fixed source independently verified after reopen");
        return Ok(());
    }
    if mode == "clock-bars-verify" {
        ensure!(
            args.len() == 6,
            "clock-bars-verify source-dir facts-file clock-dir report-file"
        );
        let manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let store = Store::open_read_only_bounded(&args[3], 128 * 1024 * 1024)?;
        let stage = store
            .read_generation(&format!("legacy-{}", manifest.id))
            .await?;
        let proof =
            legacy_verify::verify_clock_bars(Path::new(&args[4]), &manifest, &stage).await?;
        legacy_import::atomic_json(Path::new(&args[5]), &proof)?;
        store.close().await?;
        return Ok(());
    }
    if mode == "metadata-repair" {
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let store = Store::open(
            args.get(3)
                .context("existing inactive facts path required")?,
        )?;
        let generation = format!("legacy-{}", manifest.id);
        let staging = store.staging(&generation).await?;
        let rows = legacy_import::runtime_metadata_rows(dir, &manifest)?;
        let context = tracefang_core::persistence_contract::ImportContext {
            origin_id: manifest.id.clone(),
            source_fingerprint: manifest.postgres["fingerprint"]
                .as_str()
                .context("fixed fingerprint missing")?
                .into(),
            schema_version: tracefang_core::persistence_contract::SCHEMA_VERSION.into(),
            range_label: "runtime_metadata_v2_normalized_ranges".into(),
            legacy_cursor: None,
            expected_sha256: None,
        };
        let receipt = staging
            .import_metadata(ImportBatch {
                context,
                row_offset: 0,
                rows,
            })
            .await?;
        let proof = staging.verify_staging().await?;
        manifest.phases.push(serde_json::json!({"phase":"runtime_metadata_repair","state":"complete_inactive","receipt":receipt,"proof":proof,"coverage_ranges":"normalized RFC3339 pair arrays; exact original rows remain in metadata evidence and source archive","activation":false}));
        manifest.save(dir)?;
        store.close().await?;
        return Ok(());
    }
    if mode == "raw-bounds" {
        let cap = capture::Capture::open_read_only(
            args.get(3).context("existing raw redb path required")?,
        )?;
        let bounds = cap.bounds().await?;
        cap.close_and_drain().await?;
        println!(
            "{}",
            serde_json::to_string_pretty(
                &serde_json::json!({"output_schema":"capture-bounds-clock-v2","resampled_at":chrono::Utc::now(),"bounds":bounds,"scope":"bounds output only; no reimport and no original-clock alteration"})
            )?
        );
        return Ok(());
    }
    if mode == "facts-import" {
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let target = std::path::PathBuf::from(args.get(3).context("new facts redb path required")?);
        let store = Store::open(&target)?;
        let live_before = store.version().await?;
        let generation = format!("legacy-{}", manifest.id);
        let staging = store.staging(&generation).await?;
        manifest.phases.push(serde_json::json!({"phase":"postgres_native_staging","generation":generation,"live_before":live_before,"state":"started","activation":false}));
        manifest.save(dir)?;
        let budget = BudgetStore {
            store: staging.clone(),
            path: target,
            cap: 16 * 1024 * 1024 * 1024,
        };
        match legacy_import::import_tables(dir, &mut manifest, &budget).await {
            Ok(()) => {
                let proof = staging.verify_staging().await?;
                let live_after = store.version().await?;
                ensure!(
                    live_before.active_generation == live_after.active_generation
                        && live_before.committed_capture == live_after.committed_capture,
                    "staged import changed live cursor or generation"
                );
                manifest.phases.push(serde_json::json!({"phase":"postgres_native_staging","generation":generation,"state":"verified_inactive","proof":proof,"live_after":live_after,"activation":false}));
                manifest.save(dir)?;
                store.close().await?;
                println!("historical facts staged and verified; production unchanged");
            }
            Err(error) => {
                manifest.state = "facts_import_failed".into();
                manifest.phases.push(serde_json::json!({"phase":"postgres_native_staging","generation":generation,"state":"failed_inactive","activation":false,"detail":format!("{error:#}")}));
                manifest.save(dir)?;
                let _ = store.close().await;
                return Err(error);
            }
        }
        return Ok(());
    }
    if mode == "raw-verify" {
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let cap = capture::Capture::open_read_only(
            args.get(3).context("existing raw redb path required")?,
        )?;
        legacy_import::verify_raw_import(dir, &mut manifest, &cap).await?;
        cap.close_and_drain().await?;
        println!("every raw envelope verified after reopen; original prefix remains incomplete");
        return Ok(());
    }
    if mode == "raw-import" {
        let mut manifest: LegacyManifest =
            serde_json::from_slice(&std::fs::read(dir.join("manifest.json"))?)?;
        let cap = capture::Capture::open(
            args.get(3).context("new raw redb path required")?,
            Default::default(),
        )?;
        let outcome = legacy_import::import_raw_archive(dir, &mut manifest, &cap).await;
        match outcome {
            Ok(()) => {
                cap.close_and_drain().await?;
                println!("raw fixed range imported; production unchanged");
            }
            Err(error) => {
                manifest.state = "raw_import_failed".into();
                manifest.phases.push(serde_json::json!({"phase":"raw_native_import","state":"failed","detail":format!("{error:#}")}));
                manifest.save(dir)?;
                let _ = cap.close().await;
                return Err(error);
            }
        }
        return Ok(());
    }
    ensure!(
        mode == "inventory" || mode == "export" || mode == "terminal-export",
        "unsupported mode"
    );
    let _ = dotenvy::from_filename(".env.local");
    let _ = dotenvy::dotenv();
    let pg = std::env::var("TRACEFANG_LEGACY_DATABASE_URL")
        .or_else(|_| std::env::var("TRACEFANG_DATABASE_URL"))
        .context("legacy database URL not configured")?;
    let nats = std::env::var("TRACEFANG_LEGACY_NATS_URL")
        .or_else(|_| std::env::var("TRACEFANG_NATS_URL"))
        .context("legacy NATS URL not configured")?;
    let stream =
        std::env::var("TRACEFANG_NATS_STREAM").unwrap_or_else(|_| "MARKET_RAW_FRAMES".into());
    let base = if mode == "terminal-export" {
        let path = Path::new(
            args.get(3)
                .context("verified previous source manifest required")?,
        );
        Some((
            path.to_path_buf(),
            serde_json::from_slice::<LegacyManifest>(&std::fs::read(path)?)?,
        ))
    } else {
        None
    };
    let after = base
        .as_ref()
        .map(|(_, manifest)| {
            serde_json::from_value::<legacy_import::LegacyCursor>(
                manifest.raw["incremental_after"].clone(),
            )
        })
        .transpose()?;
    let mut manifest = LegacyManifest::new();
    manifest.save(dir)?;
    let outcome=async {
  legacy_import::export_postgres(&pg,dir,&mut manifest,mode=="inventory").await?;
  legacy_import::export_nats(&nats,&stream,dir,&mut manifest,after.as_ref(),mode=="inventory").await?;
  if let Some((base_path,previous))=&base {
   manifest.raw["incremental_base"]=serde_json::json!({"source_manifest_id":previous.id,"source_manifest_path":base_path,"source_manifest_sha256":legacy_import::file_hash(base_path)?,"raw":previous.raw,"imported_prefix_is_complete":false});
   manifest.raw["original_prefix_complete"]=previous.raw["original_prefix_complete"].clone();manifest.raw["origin_first_sequence"]=previous.raw["origin_first_sequence"].as_str().map(str::to_owned).or_else(||previous.raw["first_sequence"].as_str().map(str::to_owned)).into();
   manifest.phases.push(serde_json::json!({"phase":"terminal_inputs_export","old_services_stopped_by_this_tool":false,"stop_and_drain_evidence_required_separately":true,"base_source_manifest_id":previous.id,"raw_incremental_only":true}));
  }
  let config=std::env::var("TRACEFANG_SOURCE_CONFIG").unwrap_or_else(|_|"data/sources.json".into());legacy_import::archive_config(Path::new(&config),dir,&mut manifest)?;
  manifest.state=if mode=="inventory"{"inventory_complete"}else{"fixed_inputs_exported"}.into();manifest.save(dir)?;Ok::<_,anyhow::Error>(())
 }.await;
    if let Err(error) = outcome {
        manifest.state = "incomplete".into();
        manifest.phases.push(serde_json::json!({"phase":mode,"state":"failed","detail":"read/export failed; secrets omitted; preserved prior successful phases"}));
        manifest.save(dir)?;
        return Err(error);
    }
    println!(
        "{}: {} table schemas retained; production unchanged",
        manifest.state,
        manifest.tables.len()
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tracefang_core::persistence_contract::{BarSelection, CanonicalSnapshotRequest};
    #[test]
    fn state_readback_rejects_changed_or_missing_bound_evidence() -> Result<()> {
        let policy = tracefang_core::source_clock::THS_V6_SHFE_END_V2;
        let version = json!({"active_generation":"legacy-source","store_epoch":"epoch","schema_version":"schema","aggregation_version":"aggregation","commit_id":"9","committed_capture":null});
        let mut after = version.clone();
        after["commit_id"] = json!("10");
        let mut live = version.clone();
        live["active_generation"] = json!("live-v1");
        live["commit_id"] = json!("0");
        let old_proof = json!({"complete":true,"index_verified":true,"generation":"legacy-source","store_epoch":"epoch","schema_version":"schema","aggregation_version":"aggregation","verified_commit_id":"9","fact_rows":"20","index_nodes":"2","fact_codec_sha256":"a".repeat(64),"index_codec_sha256":"b".repeat(64)});
        let mut proof = old_proof.clone();
        proof["verified_commit_id"] = json!("10");
        let clock = json!({"verified":true,"policy":policy,"source_manifest_id":"source","snapshot":"snapshot","mapping_manifest_sha256":"c".repeat(64),"policy_source_sha256":"d".repeat(64),"original_archive_sha256":"e".repeat(64)});
        let mut scopes = vec![];
        let mut repaired = vec![];
        let mut states = json!({});
        for (provider, symbol) in tracefang_core::source_clock::VERIFIED_V6_SCOPES {
            let candidate = json!({"realtime_source_id":"tonghuashun_futures","instrument_symbol":symbol,"upstream_channel_id":"tonghuashun_futures","provider_symbol":provider,"interval":60,"latest_authoritative_open_time":"2026-10-01T00:00:00Z","authoritative_through":"2026-10-01T00:01:00Z","history_floor":null,"tail_checked_through":null,"tail_checked_at":null,"evidence_version":format!("{policy}:{}","c".repeat(64)),"updated_at":"2026-10-01T00:02:00Z"});
            let key = format!("tonghuashun_futures:{symbol}");
            states[&key] = candidate.clone();
            scopes.push(json!({"candidate":candidate,"derivation":{"authority_rows":5,"confirmed_authority_rows":4,"final_clock_unknown_rows":1}}));
            repaired.push(json!({"key":key,"state":candidate,"authority_rows":"5","confirmed_authority_rows":"4","final_clock_unknown_rows":"1"}));
        }
        let receipt = json!({"schema":"verified-clock-series-state-repair-v1","complete":true,"before":version,"after":after,"proof":proof,"states":repaired,"clock_manifest_sha256":"c".repeat(64),"facts_quotes_index_written":false,"initial_full_verification_reused_only_for_metadata_transaction":true});
        let basis = json!({"source":{"id":"source","postgres":{"snapshot":"snapshot"}},"clock":{"complete":true,"schema":"legacy-source-clock-projection-v2","source_manifest_id":"source","snapshot":"snapshot","policy":{"policy":{"policy_id":policy}},"policy_source_sha256":"d".repeat(64),"original_canonical_file_sha256":"e".repeat(64),"sha256":"f".repeat(64),"rows":"20"},"clock_manifest_sha256":"c".repeat(64),"oracle_sha256":"1".repeat(64),"oracle":{"schema":"independent-clock-series-state-v1","complete":true,"source_manifest_id":"source","snapshot":"snapshot","clock_manifest_sha256":"c".repeat(64),"canonical_sha256":"f".repeat(64),"all_canonical_rows_scanned":"20","policy":policy,"scopes":scopes},"repair":{"phase":"runtime_clock_series_state_repair","state":"complete_inactive","activated":false,"receipt":receipt,"proof":proof},"initial":{"complete":true,"activated":false,"before":live,"after":live,"clock_manifest_sha256":"c".repeat(64),"proof":old_proof},"full_reopen":{"complete":true,"closed":true,"helper_exit_code":0,"facts_activation":false,"production_modified":false,"bar_rows":"20","quote_rows":"10","quote_event_identities":"10","version":version}});
        let actual = json!({"active_version":live,"staged_version":after,"states":states,"state_evidence":{},"proof":proof,"repair_receipt":receipt,"clock":clock});
        validate_state_readback(&basis, &actual)?;
        for pointer in [
            "/oracle/clock_manifest_sha256",
            "/repair/receipt/after/commit_id",
            "/initial/proof/fact_codec_sha256",
            "/full_reopen/complete",
            "/oracle/scopes/0/derivation/confirmed_authority_rows",
        ] {
            let mut wrong = basis.clone();
            *wrong.pointer_mut(pointer).unwrap() = json!("invalid");
            ensure!(
                validate_state_readback(&wrong, &actual).is_err(),
                "changed basis accepted: {pointer}"
            );
        }
        for pointer in [
            "/states/tonghuashun_futures:AU2610/authoritative_through",
            "/staged_version/commit_id",
            "/active_version/commit_id",
        ] {
            let mut wrong = actual.clone();
            *wrong.pointer_mut(pointer).unwrap() = json!("invalid");
            ensure!(
                validate_state_readback(&basis, &wrong).is_err(),
                "changed stored boundary accepted: {pointer}"
            );
        }
        Ok(())
    }
    #[tokio::test]
    async fn read_only_capture_never_writes_and_native_clock_includes_acceptance() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("capture.redb");
        let cap = capture::Capture::open(&path, Default::default())?;
        let frame = capture::ProviderFrame {
            version: 1,
            channel: "jin10_web".into(),
            connection_id: "readonly-proof".into(),
            sequence: 1,
            received_at: chrono::DateTime::from_timestamp(1_700_000_000, 123456789).unwrap(),
            encoding: "wire".into(),
            body: vec![1, 2, 3],
        };
        let legacy = cap
            .append_legacy(
                &frame,
                capture::LegacyOrigin {
                    stream: "OLD".into(),
                    epoch: "OLD:epoch".into(),
                    sequence: "42".into(),
                    broker_stored_at_ns: "1700000000999999999".into(),
                },
            )
            .await?;
        let row = cap.get_at(&legacy.position).await?;
        ensure!(
            row.logical_at_ns == frame.received_at.timestamp_nanos_opt().unwrap()
                && row.dto()["accepted_at_ns"].is_null(),
            "legacy import clock entered old knowledge timeline"
        );
        let mut native = frame.clone();
        native.sequence = 2;
        let receipt = cap.append(&native).await?;
        let record = cap.get_at(&receipt.position).await?;
        ensure!(
            record.logical_at_ns >= record.accepted_at_ns
                && record.clock_policy_version.as_deref()
                    == Some("native-max-received-accepted-v1"),
            "current native frame excluded by logical knowledge cutoff"
        );
        cap.close_and_drain().await?;
        drop(cap);
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        let before = legacy_import::file_hash(&path)?;
        let ro = capture::Capture::open_read_only(&path)?;
        ensure!(
            ro.get_at(&legacy.position).await?.frame == frame
                && ro.get_at(&receipt.position).await?.frame == native,
            "read-only original changed"
        );
        ensure!(
            tokio::time::timeout(std::time::Duration::from_millis(100), ro.append(&native))
                .await?
                .is_err(),
            "read-only append blocked or wrote"
        );
        ensure!(
            ro.checkpoint("scope", 1, vec![0]).await.is_err(),
            "read-only checkpoint wrote"
        );
        let bounds = ro.bounds().await?;
        ensure!(
            bounds["first_accepted_at_ns"].is_null() && bounds["first_imported_at_ns"].is_string(),
            "legacy bounds misnamed import clock"
        );
        ro.close_and_drain().await?;
        drop(ro);
        ensure!(
            legacy_import::file_hash(&path)? == before,
            "read-only file bytes changed"
        );
        Ok(())
    }
    #[tokio::test]
    async fn final_history_import_stays_inactive_and_retries_exactly() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let mut manifest = LegacyManifest::new();
        manifest.postgres =
            json!({"fingerprint":"read-only-fixed-snapshot","snapshot":"test-repeatable-snapshot"});
        let row = json!({"instrument_symbol":"XAU/USD","source_id":"jin10_local","interval_seconds":60,"open_time":"2026-10-03T00:00:00Z","close_time":"2026-10-03T00:01:00Z",
   "open":"0.0000000000000000000000000001","high":"12345678901234567890123456789.1","low":"0.0000000000000000000000000001","close":"12345678901234567890123456789.1","volume":null,"revision":"18446744073709551615","observed_at":"2026-10-03T00:01:00Z","received_at":"2026-10-03T00:01:00.123456Z"});
        let file = "fact.ndjson";
        std::fs::write(dir.path().join(file), format!("{}\n", row))?;
        manifest.tables.push(legacy_import::TableArchive {
            table: "candles".into(),
            rows: "1".into(),
            file: file.into(),
            sha256: legacy_import::file_hash(&dir.path().join(file))?,
            primary_key: vec![],
            schema: json!({"precision":"original declared type"}),
        });
        manifest
            .phases
            .push(json!({"phase":"postgres_export","state":"complete"}));
        let store = Store::open(dir.path().join("facts.redb"))?;
        let stage = store.staging(&format!("legacy-{}", manifest.id)).await?;
        let query = || CanonicalSnapshotRequest {
            symbol: "XAU/USD".into(),
            source_id: "jin10_client".into(),
            period: "1m".into(),
            selection: BarSelection::Latest { count: 10 },
            final_only: true,
            expected_version: None,
        };
        legacy_import::import_tables(dir.path(), &mut manifest, &stage).await?;
        let snapshot = stage.canonical_snapshot(query()).await?;
        ensure!(snapshot.bars.len() == 1, "historical fact missing");
        ensure!(
            snapshot.bars[0]["open"] == "0.0000000000000000000000000001"
                && snapshot.bars[0]["volume"].is_null(),
            "exact decimal or unknown volume changed"
        );
        ensure!(
            snapshot.version.committed_capture.is_none(),
            "legacy ack was assigned a native cursor"
        );
        ensure!(
            store.canonical_snapshot(query()).await?.bars.is_empty(),
            "staging became live"
        );
        let hash = tracefang_core::periods::canonical_json_ascii(&json!(snapshot.bars));
        legacy_import::import_tables(dir.path(), &mut manifest, &stage).await?;
        ensure!(
            tracefang_core::periods::canonical_json_ascii(&json!(
                stage.canonical_snapshot(query()).await?.bars
            )) == hash,
            "retry changed facts"
        );
        stage.verify_staging().await?;
        ensure!(
            store.version().await?.committed_capture.is_none(),
            "verification installed a native cursor"
        );
        store.close().await?;
        Ok(())
    }
}
