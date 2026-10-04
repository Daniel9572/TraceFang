//! Terminal composite selection from a complete, globally decoded retained prefix.
//! Writes only the explicitly inactive target; never activates or starts providers.
#![recursion_limit = "512"]
#[path = "../src/capture.rs"]
mod capture;
#[path = "../src/catalog.rs"]
mod catalog;
#[path = "../src/legacy_import.rs"]
mod legacy_import;
#[path = "../src/legacy_reconcile.rs"]
mod legacy_reconcile;
#[path = "../src/legacy_spool.rs"]
mod legacy_spool;
#[path = "../src/legacy_verify.rs"]
mod legacy_verify;
#[path = "../src/providers/mod.rs"]
mod providers;
#[path = "../src/quotes.rs"]
mod quotes;
#[path = "../src/replay.rs"]
mod replay;
mod api {
    use axum::{
        http::StatusCode,
        response::{IntoResponse, Response},
    };
    use std::sync::Arc;
    #[derive(Clone)]
    pub struct Market {
        pub catalog: Arc<crate::catalog::Catalog>,
        pub store: tracefang_core::native_store::Store,
    }
    impl Market {
        pub fn source(&self, code: &str) -> anyhow::Result<String> {
            Ok(self.catalog.get(code)?.source_ids[0].clone())
        }
    }
    #[derive(Clone)]
    pub struct AppState {
        pub market: Market,
        pub capture: crate::capture::Capture,
        pub shutdown: tokio::sync::watch::Receiver<bool>,
    }
    pub struct ApiError(pub StatusCode, pub String);
    impl From<anyhow::Error> for ApiError {
        fn from(e: anyhow::Error) -> Self {
            Self(StatusCode::BAD_GATEWAY, e.to_string())
        }
    }
    impl IntoResponse for ApiError {
        fn into_response(self) -> Response {
            (self.0, self.1).into_response()
        }
    }
}
mod pages {
    pub fn schedule(
        market: &crate::api::Market,
        code: &str,
    ) -> anyhow::Result<tracefang_core::periods::MarketSchedule> {
        Ok(serde_json::from_value(
            market.catalog.schedules[&market.catalog.get(code)?.market_schedule_id].clone(),
        )?)
    }
}
use anyhow::{Context, Result, ensure};
use legacy_reconcile::{BarChoice, QuoteChoice};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{BufRead, BufReader, BufWriter, Read, Write},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
use tracefang_core::{
    native_store::{CanonicalBarKey, CanonicalQuoteKey, Store},
    periods::Period,
    persistence_contract::*,
};
const POLICY: &str = "legacy-bars-fixed-authority+clock-projection+retained-raw-overlay-v2";
const CLASSIFICATION: &str = "retained-envelope-http502-and-optional-daily-statistics-v1";
const MIB: usize = 1024 * 1024;
const GIB: u64 = 1024 * 1024 * 1024;
static EVIDENCE_BYTES: AtomicU64 = AtomicU64::new(0);
fn ns() -> Result<i64> {
    chrono::Utc::now()
        .timestamp_nanos_opt()
        .context("terminal clock outside exact ns")
}
fn read(path: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&std::fs::read(path)?)?)
}
fn text(v: &Value, key: &str) -> Result<String> {
    Ok(v[key]
        .as_str()
        .with_context(|| format!("missing exact {key}"))?
        .into())
}
fn digest(v: &Value) -> Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(v)?)))
}
fn private_file(path: &Path) -> Result<File> {
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        file.set_permissions(std::fs::Permissions::from_mode(0o600))?;
    }
    Ok(file)
}
struct CountedFile {
    file: File,
}
impl Write for CountedFile {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        EVIDENCE_BYTES
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |used| {
                used.checked_add(bytes.len() as u64)
                    .filter(|v| *v <= GIB - 32 * MIB as u64)
            })
            .map_err(|_| {
                std::io::Error::other(
                    "terminal aggregate encoded evidence exceeds1GiB; input preserved",
                )
            })?;
        self.file.write(bytes)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.file.flush()
    }
}
fn ledger_reader(path: &Path) -> Result<BufReader<Box<dyn Read + Send>>> {
    let file = File::open(path)?;
    let input: Box<dyn Read + Send> = if path.extension().is_some_and(|v| v == "gz") {
        Box::new(flate2::read::MultiGzDecoder::new(file))
    } else {
        Box::new(file)
    };
    Ok(BufReader::new(input))
}
fn stable_hash(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let before = file.metadata()?;
    ensure!(before.is_file(), "hash input is not regular file");
    let mut hash = Sha256::new();
    let mut bytes = vec![0; 64 * 1024];
    let mut count = 0u64;
    loop {
        let n = file.read(&mut bytes)?;
        if n == 0 {
            break;
        }
        count = count
            .checked_add(n as u64)
            .context("input byte count exhausted")?;
        hash.update(&bytes[..n]);
    }
    let after = file.metadata()?;
    let current = std::fs::metadata(path)?;
    ensure!(
        count == before.len()
            && before.len() == after.len()
            && before.len() == current.len()
            && before.modified()? == after.modified()?
            && before.modified()? == current.modified()?,
        "hash input changed or read length differs from stat (cloud data not hydrated)"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            before.dev() == after.dev()
                && before.ino() == after.ino()
                && before.dev() == current.dev()
                && before.ino() == current.ino(),
            "hash input identity changed"
        );
    }
    Ok(hex::encode(hash.finalize()))
}
struct Ledger {
    path: PathBuf,
    out: BufWriter<flate2::write::GzEncoder<CountedFile>>,
    rows: u64,
}
impl Ledger {
    fn new(root: &Path, name: &str) -> Result<Self> {
        let path = root.join(if name.ends_with(".ndjson") {
            format!("{name}.gz")
        } else {
            name.into()
        });
        Ok(Self {
            out: BufWriter::new(flate2::write::GzEncoder::new(
                CountedFile {
                    file: private_file(&path)?,
                },
                flate2::Compression::fast(),
            )),
            path,
            rows: 0,
        })
    }
    fn push(&mut self, v: Value) -> Result<()> {
        let bytes = serde_json::to_vec(&v)?;
        ensure!(
            bytes.len() <= 4 * MIB,
            "evidence row exceeds bounded4MiB; preserved input"
        );
        self.out.write_all(&bytes)?;
        self.out.write_all(b"\n")?;
        self.rows += 1;
        ensure!(
            std::fs::metadata(&self.path)?.len() < GIB,
            "terminal evidence ledger exceeds1GiB"
        );
        Ok(())
    }
    fn finish(mut self) -> Result<(PathBuf, u64)> {
        self.out.flush()?;
        let encoded = self
            .out
            .into_inner()
            .map_err(|e| e.into_error())?
            .finish()?;
        encoded.file.sync_all()?;
        Ok((self.path, self.rows))
    }
}
fn artifact(path: &Path, rows: u64, binding: &Value) -> Result<Value> {
    Ok(
        json!({"file":path.file_name().context("artifact basename missing")?.to_string_lossy(),"sha256":stable_hash(path)?,"complete":true,"row_count":rows.to_string(),"binding":binding}),
    )
}
fn summary_manifest(
    root: &Path,
    name: &str,
    schema: &str,
    ledger: &Path,
    rows: u64,
    binding: &Value,
) -> Result<PathBuf> {
    let mut value = binding.clone();
    value["schema"] = json!(schema);
    value["binding"] = binding.clone();
    value["complete"] = json!(true);
    value["unresolved_differences"] = json!("0");
    value["empty_raw_seed"] = json!(true);
    value["ledger"] = artifact(ledger, rows, binding)?;
    let path = root.join(name);
    legacy_import::atomic_json(&path, &value)?;
    Ok(path)
}
fn budget(path: &Path, cap: u64) -> Result<()> {
    ensure!(
        capture::available_bytes(path.parent().context("target parent missing")?)? > 4 * GIB,
        "terminal phase below4GiB free; evidence preserved"
    );
    if path.exists() {
        ensure!(
            std::fs::metadata(path)?.len() <= cap,
            "terminal phase file cap exceeded; inactive target preserved"
        );
    }
    Ok(())
}

#[cfg(test)]
mod terminal_tests {
    use super::*;
    #[test]
    fn gzip_reader_checks_all_members_and_rejects_bad_eof() -> Result<()> {
        fn member(bytes: &[u8]) -> Result<Vec<u8>> {
            let mut out = flate2::write::GzEncoder::new(vec![], flate2::Compression::fast());
            out.write_all(bytes)?;
            Ok(out.finish()?)
        }
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("proof.ndjson.gz");
        let mut bytes = member(b"{\"row\":1}\n")?;
        bytes.extend(member(b"{\"row\":2}\n")?);
        std::fs::write(&path, &bytes)?;
        ensure!(
            ledger_reader(&path)?
                .lines()
                .collect::<std::io::Result<Vec<_>>>()?
                .len()
                == 2,
            "second member omitted"
        );
        for bad in [
            bytes[..bytes.len() - 1].to_vec(),
            [bytes.clone(), b"garbage".to_vec()].concat(),
        ] {
            std::fs::write(&path, bad)?;
            ensure!(
                ledger_reader(&path)?
                    .lines()
                    .collect::<std::io::Result<Vec<_>>>()
                    .is_err(),
                "invalid gzip EOF accepted"
            );
        }
        Ok(())
    }
    fn quote(event: &str, observed: i64) -> ImportQuoteRow {
        ImportQuoteRow {
            instrument_symbol: "XAU/USD".into(),
            realtime_source_id: "jin10_client".into(),
            evidence_channel_id: "jin10_web".into(),
            event_id: event.into(),
            price: "12345678901234567890.123456789".into(),
            bid: None,
            ask: None,
            volume: None,
            observed_at_ns: observed,
            received_at_ns: observed + 123,
            source_sequence: Some(u64::MAX),
            source_metadata: json!({"provider":"jin10_web","provider_symbol":"XAUUSD.GOODS","raw_payload":{"capture_epoch":"fixture-raw","capture_sequence":"1","capture_digest":"a".repeat(64)}}),
            statistics: json!({"open":null,"high":null,"low":null,"change":"-0.000000001","change_percent":null}),
            is_supplement: false,
            evidence: json!({"original_source":"immutable-fixture"}),
        }
    }
    fn manifest() -> legacy_import::LegacyManifest {
        let mut m = legacy_import::LegacyManifest::new();
        m.postgres = json!({"fingerprint":"a".repeat(64),"snapshot":"fixed-fixture"});
        m
    }
    #[tokio::test]
    async fn selected_missing_events_apply_and_reopen_all_exact_fields() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let path = dir.path().join("facts.redb");
        let m = manifest();
        let db = Store::open(&path)?;
        let live = db.version().await?;
        let generation = format!("legacy-{}", m.id);
        let stage = db.staging(&generation).await?;
        let pg = quote("original", 1_800_000_000_000_000_000);
        stage
            .import_quotes(ImportBatch {
                context: context(&m, "fixture_pg")?,
                row_offset: 0,
                rows: vec![pg.clone()],
            })
            .await?;
        let version = stage.version().await?;
        let mut latest = stage
            .canonical_latest_quote_rows(version.clone())
            .await?
            .into_iter()
            .map(|r| {
                (
                    (r.realtime_source_id.clone(), r.instrument_symbol.clone()),
                    r,
                )
            })
            .collect();
        let mut comparison = Ledger::new(dir.path(), "comparison.ndjson")?;
        let mut overlay = Ledger::new(dir.path(), "overlay.ndjson")?;
        let mut counts = Counts::default();
        let new = quote("new", pg.observed_at_ns + 1);
        select_quotes(
            &stage,
            &version,
            vec![pg, new],
            &m,
            &mut counts,
            &mut comparison,
            &mut overlay,
            &mut latest,
        )
        .await?;
        ensure!(
            counts.quotes == 2
                && counts.preserved == 1
                && counts.overlay == 1
                && counts.unresolved == 0,
            "event selection counts differ"
        );
        let (expected_path, _) = comparison.finish()?;
        let (overlay_path, _) = overlay.finish()?;
        let value: Value =
            serde_json::from_str(&ledger_reader(&overlay_path)?.lines().next().unwrap()?)?;
        let row: ImportQuoteRow = serde_json::from_value(value["after"].clone())?;
        let mut rows = vec![row];
        flush(&stage, &m, &mut vec![], &mut rows, &mut 0).await?;
        stage.verify_staging().await?;
        ensure!(
            db.version().await? == live,
            "overlay changed live generation"
        );
        db.close().await?;
        let reopened = Store::open_read_only_bounded(&path, 16 * MIB)?;
        let stage = reopened.read_generation(&generation).await?;
        let proof = verify_selected(&stage, &expected_path).await?;
        ensure!(
            proof["selected_quote_rows"] == "2"
                && proof["expected_field_sha256"] == proof["actual_field_sha256"],
            "exact selected event reopen differs"
        );
        let quotes = final_quote_proof(&stage, dir.path(), &latest).await?;
        ensure!(
            quotes["quote_events_verified"] == "2" && quotes["latest_quotes_verified"] == "1",
            "full event/latest accounting differs"
        );
        ensure!(
            stage.version().await?.committed_capture.is_none() && reopened.version().await? == live,
            "reopen changed authority/cursor"
        );
        reopened.close().await?;
        Ok(())
    }
    #[tokio::test]
    async fn same_clock_new_identity_conflict_does_not_write_candidate() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let m = manifest();
        let db = Store::open(dir.path().join("facts.redb"))?;
        let stage = db.staging(&format!("legacy-{}", m.id)).await?;
        let pg = quote("original", 1_800_000_000_000_000_000);
        stage
            .import_quotes(ImportBatch {
                context: context(&m, "fixture_pg")?,
                row_offset: 0,
                rows: vec![pg.clone()],
            })
            .await?;
        let version = stage.version().await?;
        let mut latest = stage
            .canonical_latest_quote_rows(version.clone())
            .await?
            .into_iter()
            .map(|r| {
                (
                    (r.realtime_source_id.clone(), r.instrument_symbol.clone()),
                    r,
                )
            })
            .collect();
        let mut raw = pg;
        raw.event_id = "unproved-new-identity".into();
        raw.price = "12345678901234567890.123456790".into();
        let mut comparison = Ledger::new(dir.path(), "comparison.ndjson")?;
        let mut overlay = Ledger::new(dir.path(), "overlay.ndjson")?;
        let mut counts = Counts::default();
        select_quotes(
            &stage,
            &version,
            vec![raw],
            &m,
            &mut counts,
            &mut comparison,
            &mut overlay,
            &mut latest,
        )
        .await?;
        ensure!(
            counts.unresolved == 1 && counts.overlay == 0 && stage.version().await? == version,
            "ambiguous same-clock event altered candidate"
        );
        ensure!(
            overlay.finish()?.1 == 0 && counts.unresolved == 1,
            "conflict published successful overlay"
        );
        comparison.finish()?;
        db.close().await?;
        Ok(())
    }
    #[tokio::test]
    async fn middle_import_conflict_rolls_back_complete_batch() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let m = manifest();
        let db = Store::open(dir.path().join("facts.redb"))?;
        let live = db.version().await?;
        let stage = db.staging(&format!("legacy-{}", m.id)).await?;
        let old = quote("same-event", 1_800_000_000_000_000_000);
        stage
            .import_quotes(ImportBatch {
                context: context(&m, "fixture_pg")?,
                row_offset: 0,
                rows: vec![old.clone()],
            })
            .await?;
        let before = stage.version().await?;
        let first = quote("must-rollback", old.observed_at_ns + 1);
        let mut conflict = old;
        conflict.price = "1".into();
        let result = stage
            .import_quotes(ImportBatch {
                context: context(&m, "failed_overlay")?,
                row_offset: 0,
                rows: vec![first.clone(), conflict],
            })
            .await;
        ensure!(
            result.is_err() && stage.version().await? == before && db.version().await? == live,
            "middle failure did not preserve versions"
        );
        ensure!(
            stage.lookup_quotes(vec![quote_key(&first)]).await?.1[0].is_none(),
            "earlier row of failed transaction leaked"
        );
        db.close().await?;
        Ok(())
    }
    #[tokio::test]
    async fn later_statistics_overlay_preserves_price_identity_and_clocks() -> Result<()> {
        let dir = tempfile::tempdir()?;
        let m = manifest();
        let db = Store::open(dir.path().join("facts.redb"))?;
        let stage = db.staging(&format!("legacy-{}", m.id)).await?;
        let old = quote("price-event", 1_800_000_000_000_000_000);
        stage
            .import_quotes(ImportBatch {
                context: context(&m, "fixture_pg")?,
                row_offset: 0,
                rows: vec![old.clone()],
            })
            .await?;
        let previous = stage
            .canonical_latest_quote_rows(stage.version().await?)
            .await?
            .pop()
            .unwrap();
        let mut retained = previous.clone();
        retained.is_supplement = true;
        retained.statistics["change"] = json!("-0.000000002");
        let tail = CapturePosition {
            epoch: "fixture-raw".into(),
            sequence: 2,
            digest: "b".repeat(64),
        };
        let supplement_metadata = json!({"provider":"jin10_web","provider_symbol":"XAUUSD.GOODS","raw_payload":{"supplement_received_at":chrono::DateTime::from_timestamp_nanos(old.received_at_ns+1).to_rfc3339(),"capture_epoch":"fixture-raw","capture_sequence":"2","capture_digest":"b".repeat(64)}});
        retained.source_metadata["raw_payload"]["statistics_evidence"] =
            json!({"applied_capture":tail,"source_metadata":supplement_metadata});
        let (input, expected) = select_latest_supplement(&previous, &retained, &tail)?
            .context("valid supplement omitted")?;
        stage
            .import_quotes(ImportBatch {
                context: context(&m, "statistics_overlay")?,
                row_offset: 0,
                rows: vec![input],
            })
            .await?;
        let actual = stage
            .canonical_latest_quote_rows(stage.version().await?)
            .await?
            .pop()
            .unwrap();
        ensure!(
            semantic(json!(actual)) == semantic(json!(expected)),
            "typed supplement import differs from independent selected fields"
        );
        ensure!(
            actual.event_id == old.event_id
                && actual.observed_at_ns == old.observed_at_ns
                && actual.received_at_ns == old.received_at_ns
                && actual.price == old.price,
            "statistics shifted price event or clocks"
        );
        ensure!(
            stage.generation_summary().await?["counts"]["quote_events"] == "1",
            "supplement invented price event"
        );
        retained.received_at_ns += 1;
        ensure!(
            select_latest_supplement(&previous, &retained, &tail).is_err(),
            "supplement price-clock mutation accepted"
        );
        db.close().await?;
        Ok(())
    }
}
fn context(manifest: &legacy_import::LegacyManifest, label: &str) -> Result<ImportContext> {
    Ok(ImportContext {
        origin_id: manifest.id.clone(),
        source_fingerprint: text(&manifest.postgres, "fingerprint")?,
        schema_version: SCHEMA_VERSION.into(),
        range_label: label.into(),
        legacy_cursor: None,
        expected_sha256: None,
    })
}
fn semantic(mut v: Value) -> Value {
    if let Some(raw) = v["source_metadata"]["raw_payload"].as_object_mut() {
        raw.remove("applied_commit_id");
    }
    v
}
fn bar_key(r: &ImportBarRow) -> CanonicalBarKey {
    CanonicalBarKey {
        source_id: r.realtime_source_id.clone(),
        symbol: r.instrument_symbol.clone(),
        interval_seconds: r.interval_seconds,
        open_time_ns: r.open_time_ns,
    }
}
fn bar_key_value(r: &ImportBarRow) -> Value {
    json!({"source":r.realtime_source_id,"symbol":r.instrument_symbol,"interval":r.interval_seconds,"open_time_ns":r.open_time_ns.to_string()})
}
fn quote_key_value(r: &ImportQuoteRow) -> Value {
    json!({"source":r.realtime_source_id,"symbol":r.instrument_symbol,"event_id":r.event_id})
}
fn quote_key(r: &ImportQuoteRow) -> CanonicalQuoteKey {
    CanonicalQuoteKey {
        source_id: r.realtime_source_id.clone(),
        symbol: r.instrument_symbol.clone(),
        event_id: r.event_id.clone(),
    }
}
fn source_name(source: &str) -> &str {
    if source.starts_with("jin10_") {
        "jin10_client"
    } else {
        source
    }
}
fn checked_reference(root: &Path, name: &str, input: &Path) -> Result<()> {
    let path = root.join(name);
    ensure!(
        stable_hash(&path)? == stable_hash(input)?,
        "terminal copied source differs: {name}"
    );
    Ok(())
}
fn checked_copy(root: &Path, name: &str, input: &Path) -> Result<Value> {
    let path = root.join(name);
    ensure!(
        stable_hash(&path)? == stable_hash(input)?,
        "terminal copied artifact differs: {name}"
    );
    read(&path)
}
fn seal(root: &Path, input: &Path, build: &str) -> Result<(Value, String)> {
    let value = checked_copy(root, "terminal-tools-manifest.json", input)?;
    ensure!(
        value["schema"] == "tracefang-migration-tools-v1"
            && value["complete"] == true
            && value["backend_build_sha256"] == build,
        "tools seal belongs to another build or is incomplete"
    );
    let executable = std::fs::canonicalize(std::env::current_exe()?)?;
    let executable_sha = stable_hash(&executable)?;
    let files = value["files"].as_array().context("sealed files missing")?;
    for entry in files {
        let path = PathBuf::from(text(entry, "path")?);
        ensure!(
            !path
                .components()
                .any(|v| matches!(v.as_os_str().to_str(), Some("target" | "rust-target"))),
            "sealed input remains in mutable Cargo target"
        );
        ensure!(
            stable_hash(&path)? == text(entry, "sha256")?,
            "sealed migration input changed"
        );
    }
    ensure!(
        files.iter().any(|v| v["path"]
            .as_str()
            .is_some_and(|p| std::fs::canonicalize(p).ok().as_ref() == Some(&executable))
            && v["sha256"] == executable_sha),
        "current executor is not the sealed exact binary"
    );
    Ok((value, executable_sha))
}
#[derive(Default)]
struct Counts {
    bars: u64,
    quotes: u64,
    preserved: u64,
    overlay: u64,
    missing_bars: u64,
    missing_quotes: u64,
    unresolved: u64,
    latest_statistics: u64,
}
/// Verify the delta and every immutable base dependency against this same
/// native capture. The reusable importer audit writes its phase receipt only
/// into a disposable private directory, never into the original source.
async fn raw_lineage(
    root: &Path,
    input: &legacy_import::LegacyManifest,
    capture: &capture::Capture,
    tail: &CapturePosition,
) -> Result<Value> {
    let mut chain = vec![(
        root.to_path_buf(),
        input.clone(),
        stable_hash(&root.join("fixed-input-manifest.json"))?,
    )];
    let mut ids = BTreeSet::new();
    ids.insert(input.id.clone());
    loop {
        let previous = chain.last().unwrap();
        let base = &previous.1.raw["incremental_base"];
        if base.is_null() {
            break;
        }
        ensure!(
            chain.len() < 32,
            "raw dependency chain exceeds bounded32 sources"
        );
        let path = PathBuf::from(text(base, "source_manifest_path")?);
        let sha = stable_hash(&path)?;
        ensure!(
            sha == text(base, "source_manifest_sha256")?,
            "raw incremental base manifest changed"
        );
        let manifest: legacy_import::LegacyManifest = serde_json::from_reader(File::open(&path)?)?;
        ensure!(
            manifest.id == text(base, "source_manifest_id")?
                && json!(manifest.raw) == base["raw"]
                && ids.insert(manifest.id.clone()),
            "raw incremental base identity differs or is cyclic"
        );
        ensure!(
            manifest.raw["stream"] == input.raw["stream"]
                && manifest.raw["epoch"] == input.raw["epoch"],
            "raw dependency changes legacy stream/epoch"
        );
        chain.push((
            path.parent().context("base source parent missing")?.into(),
            manifest,
            sha,
        ));
    }
    chain.reverse();
    let mut next = 1u64;
    let mut maps = BTreeSet::new();
    let mut proofs = vec![];
    for (dir, mut manifest, manifest_sha) in chain {
        for config in &manifest.configs {
            let name = text(config, "file")?;
            ensure!(
                Path::new(&name)
                    .file_name()
                    .is_some_and(|v| v == name.as_str()),
                "raw dependency config escapes source"
            );
            ensure!(
                stable_hash(&dir.join(name))? == text(config, "sha256")?,
                "raw dependency configuration changed"
            );
        }
        let map = &manifest.raw["native_mapping"];
        let sha = text(map, "sha256")?;
        let last: CapturePosition = serde_json::from_value(map["last_position"].clone())?;
        let count = text(map, "frames")?.parse::<u64>()?;
        let first = last
            .sequence
            .checked_add(1)
            .and_then(|v| v.checked_sub(count))
            .context("raw dependency range overflow")?;
        ensure!(
            last.epoch == tail.epoch && last.sequence <= tail.sequence && first > 0,
            "raw dependency capture range differs"
        );
        if !maps.insert(sha.clone()) {
            ensure!(
                manifest.raw["exported_frames"] == "0"
                    && last.sequence.checked_add(1) == Some(next),
                "duplicated mapping is not a verified empty terminal delta"
            );
            continue;
        }
        ensure!(
            first == next,
            "raw dependency omitted/reordered retained native prefix"
        );
        let file = text(map, "file")?;
        ensure!(
            Path::new(&file)
                .file_name()
                .is_some_and(|v| v == file.as_str()),
            "raw dependency mapping escapes source"
        );
        let mapping = dir.join(&file);
        ensure!(
            stable_hash(&mapping)? == sha,
            "raw dependency mapping bytes changed"
        );
        let archive_name = text(&manifest.raw, "file")?;
        ensure!(
            Path::new(&archive_name)
                .file_name()
                .is_some_and(|v| v == archive_name.as_str()),
            "raw dependency archive escapes source"
        );
        let archive = dir.join(archive_name);
        ensure!(
            stable_hash(&archive)? == text(&manifest.raw, "sha256")?,
            "raw dependency source archive changed"
        );
        let scratch = tempfile::Builder::new()
            .prefix("terminal-raw-audit-")
            .tempdir_in(root)?;
        std::fs::hard_link(&mapping, scratch.path().join(&file))?;
        legacy_import::verify_raw_import(scratch.path(), &mut manifest, capture).await?;
        let audit = manifest
            .phases
            .last()
            .context("raw independent envelope audit absent")?
            .clone();
        proofs.push(json!({"source_manifest_id":manifest.id,"source_manifest_directory":dir,"source_manifest_sha256":manifest_sha,"raw_archive_sha256":manifest.raw["sha256"],"mapping_sha256":sha,"first_native_sequence":first.to_string(),"last_position":last,"frames":count.to_string(),"proof":audit}));
        next = last
            .sequence
            .checked_add(1)
            .context("raw prefix exhausted")?;
    }
    ensure!(
        next == tail
            .sequence
            .checked_add(1)
            .context("terminal raw cursor exhausted")?,
        "incremental lineage did not cover every retained native envelope"
    );
    let proof = json!({"schema":"legacy-terminal-raw-lineage-v1","complete":true,"first_native_sequence":"1","last_position":tail,"frames":tail.sequence.to_string(),"fixed_input_manifest_sha256":stable_hash(&root.join("fixed-input-manifest.json"))?,"original_prefix_complete":input.raw["original_prefix_complete"],"dependencies":proofs});
    legacy_import::atomic_json(&root.join("terminal-raw-lineage.json"), &proof)?;
    Ok(proof)
}
fn witness(
    manifest: &legacy_import::LegacyManifest,
    old: Option<Value>,
    raw: Value,
    reason: &str,
) -> Result<Value> {
    Ok(
        json!({"source_manifest_id":manifest.id,"original_pg_sha256":old.as_ref().map(digest).transpose()?,"raw_row_sha256":digest(&raw)?,"reported_revision":raw["revision"],"capture_position":{"epoch":raw["source_metadata"]["raw_payload"]["capture_epoch"],"sequence":raw["source_metadata"]["raw_payload"]["capture_sequence"],"digest":raw["source_metadata"]["raw_payload"]["capture_digest"]},"reason":reason,"original_pg_lineage":old.as_ref().map(|v|v["evidence"].clone()),"retained_source_lineage":raw["evidence"],"initial_seed":"empty"}),
    )
}
async fn select_bars(
    pg: &Store,
    version: &SnapshotVersion,
    batch: Vec<ImportBarRow>,
    manifest: &legacy_import::LegacyManifest,
    counts: &mut Counts,
    comparison: &mut Ledger,
    overlay: &mut Ledger,
) -> Result<()> {
    let (v, old) = pg.lookup_bars(batch.iter().map(bar_key).collect()).await?;
    ensure!(
        &v == version && batch.len() == old.len(),
        "fixed authority changed or bounded lookup count differs"
    );
    for (raw, old) in batch.into_iter().zip(old) {
        counts.bars += 1;
        let before = old.as_ref().map(serde_json::to_value).transpose()?;
        let raw_value = json!(raw);
        let (reason, mut selected, changed) = match legacy_reconcile::choose_bar(
            old.as_ref(),
            &raw,
        )? {
            BarChoice::Preserve(reason) => {
                counts.preserved += 1;
                (
                    reason,
                    old.clone().context("preserved absent authority")?,
                    false,
                )
            }
            BarChoice::Replace { row, reason } => {
                counts.overlay += 1;
                counts.missing_bars += u64::from(old.is_none());
                (reason, row, true)
            }
            BarChoice::Unresolved(reason) => {
                counts.unresolved += 1;
                comparison.push(json!({"kind":"bar","state":"unresolved","reason":reason,"key":json!(bar_key_value(&raw)),"before_sha256":before.as_ref().map(digest).transpose()?,"retained_sha256":digest(&raw_value)?}))?;
                continue;
            }
        };
        let mut proof = witness(manifest, before.clone(), raw_value, reason)?;
        proof["canonical_revision"] = json!(selected.revision.to_string());
        if changed {
            selected.evidence["retained_raw_reconciliation"] = proof.clone();
            overlay.push(json!({"kind":"bar","before":before,"after":selected,"witness":proof}))?;
        }
        comparison.push(json!({"kind":"bar","state":"selected","reason":reason,"key":bar_key_value(&selected),"expected_sha256":digest(&semantic(json!(selected)))?,"witness":proof}))?;
    }
    Ok(())
}
async fn select_quotes(
    pg: &Store,
    version: &SnapshotVersion,
    batch: Vec<ImportQuoteRow>,
    manifest: &legacy_import::LegacyManifest,
    counts: &mut Counts,
    comparison: &mut Ledger,
    overlay: &mut Ledger,
    latest: &mut BTreeMap<(String, String), ImportQuoteRow>,
) -> Result<()> {
    let (v, old) = pg
        .lookup_quotes(batch.iter().map(quote_key).collect())
        .await?;
    ensure!(
        &v == version && batch.len() == old.len(),
        "fixed quote authority changed or lookup count differs"
    );
    for (raw, old) in batch.into_iter().zip(old) {
        counts.quotes += 1;
        let before = old.as_ref().map(serde_json::to_value).transpose()?;
        let raw_value = json!(raw);
        let scope = (
            raw.realtime_source_id.clone(),
            raw.instrument_symbol.clone(),
        );
        if old.is_none()
            && latest.get(&scope).is_some_and(|prior| {
                prior.observed_at_ns == raw.observed_at_ns
                    && !same_latest_contents(prior, &raw).unwrap_or(false)
            })
        {
            counts.unresolved += 1;
            comparison.push(json!({"kind":"quote","state":"unresolved","reason":"missing_event_conflicts_with_same_source_clock_latest_authority","key":quote_key_value(&raw),"retained_sha256":digest(&raw_value)?}))?;
            continue;
        }
        let (reason, mut selected, changed) = match legacy_reconcile::choose_quote(
            old.as_ref(),
            &raw,
        )? {
            QuoteChoice::Preserve(reason) => {
                counts.preserved += 1;
                (
                    reason,
                    old.clone().context("preserved absent quote")?,
                    false,
                )
            }
            QuoteChoice::Insert { row, reason } => {
                counts.overlay += 1;
                counts.missing_quotes += 1;
                (reason, row, true)
            }
            QuoteChoice::Unresolved(reason) => {
                counts.unresolved += 1;
                comparison.push(json!({"kind":"quote","state":"unresolved","reason":reason,"key":json!(quote_key_value(&raw)),"before_sha256":before.as_ref().map(digest).transpose()?,"retained_sha256":digest(&raw_value)?}))?;
                continue;
            }
        };
        let proof = witness(manifest, before.clone(), raw_value, reason)?;
        if changed {
            selected.evidence["retained_raw_reconciliation"] = proof.clone();
            if latest
                .get(&scope)
                .is_none_or(|prior| selected.observed_at_ns >= prior.observed_at_ns)
            {
                latest.insert(scope, selected.clone());
            }
            overlay
                .push(json!({"kind":"quote","before":before,"after":selected,"witness":proof}))?;
        }
        comparison.push(json!({"kind":"quote","state":"selected","reason":reason,"key":quote_key_value(&selected),"expected_sha256":digest(&semantic(json!(selected)))?,"witness":proof}))?;
    }
    Ok(())
}
fn same_latest_contents(a: &ImportQuoteRow, b: &ImportQuoteRow) -> Result<bool> {
    let mut a = a.clone();
    let mut b = b.clone();
    for row in [&mut a, &mut b] {
        row.event_id = "same-source-clock".into();
        row.received_at_ns = 0;
        row.source_sequence = None;
    }
    Ok(
        legacy_reconcile::quote_semantic_fields(&a)?
            == legacy_reconcile::quote_semantic_fields(&b)?,
    )
}
fn supplement_clock(row: &ImportQuoteRow) -> Option<i64> {
    let value = &row.source_metadata["raw_payload"]["statistics_evidence"]["source_metadata"]["raw_payload"]
        ["supplement_received_at"];
    value
        .as_str()?
        .parse::<chrono::DateTime<chrono::Utc>>()
        .ok()?
        .timestamp_nanos_opt()
}
/// Latest statistics are a separate dimension from immutable price events.
/// This predicts the documented typed import supplement transform, never a
/// Store codec or index write: price identity/clocks remain fixed and only a
/// later, capture-bound statistics observation can replace them.
fn select_latest_supplement(
    previous: &ImportQuoteRow,
    retained: &ImportQuoteRow,
    tail: &CapturePosition,
) -> Result<Option<(ImportQuoteRow, ImportQuoteRow)>> {
    ensure!(
        previous.instrument_symbol == retained.instrument_symbol
            && previous.realtime_source_id == retained.realtime_source_id,
        "latest comparison crosses scope"
    );
    if !retained.is_supplement || retained.event_id != previous.event_id {
        return Ok(None);
    }
    let mut price = retained.clone();
    price.statistics = previous.statistics.clone();
    price.is_supplement = previous.is_supplement;
    ensure!(
        same_latest_contents(previous, &price)?
            && retained.received_at_ns == previous.received_at_ns
            && retained.source_sequence == previous.source_sequence,
        "supplement would change price contents or clocks"
    );
    if retained.statistics == previous.statistics {
        return Ok(None);
    }
    let proof = &retained.source_metadata["raw_payload"]["statistics_evidence"];
    let position: CapturePosition = serde_json::from_value(proof["applied_capture"].clone())
        .context("supplement lacks actual retained capture identity")?;
    ensure!(
        position.epoch == tail.epoch
            && position.sequence > 0
            && position.sequence <= tail.sequence
            && position.digest.len() == 64,
        "supplement capture outside terminal prefix"
    );
    let received =
        supplement_clock(retained).context("supplement has no independent received clock")?;
    ensure!(
        received > supplement_clock(previous).unwrap_or(previous.received_at_ns),
        "changed statistics have no proved later observation"
    );
    let mut input = retained.clone();
    input.source_metadata = proof["source_metadata"].clone();
    if let Some(raw) = input.source_metadata["raw_payload"].as_object_mut() {
        raw.remove("applied_commit_id");
    }
    let mut expected = input.clone();
    expected.source_metadata = previous.source_metadata.clone();
    if !expected.source_metadata["raw_payload"].is_object() {
        expected.source_metadata["raw_payload"] = json!({});
    }
    expected.source_metadata["raw_payload"]["statistics_evidence"] =
        json!({"applied_capture":null,"source_metadata":input.source_metadata});
    Ok(Some((input, expected)))
}
async fn final_quote_proof(
    store: &Store,
    root: &Path,
    expected_latest: &BTreeMap<(String, String), ImportQuoteRow>,
) -> Result<Value> {
    let version = store.version().await?;
    let mut ledger = Ledger::new(root, "terminal-final-quote-events.ndjson")?;
    let mut hash = Sha256::new();
    let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
    let scan_store = store.clone();
    let scan_version = version.clone();
    let producer = tokio::spawn(async move {
        scan_store
            .canonical_quote_scan(
                String::new(),
                String::new(),
                scan_version,
                1000,
                move |batch| {
                    sender
                        .blocking_send(batch)
                        .context("final quote proof receiver ended")
                },
            )
            .await
    });
    let mut count = 0u64;
    while let Some(batch) = receiver.recv().await {
        for row in batch {
            let full = semantic(json!(row));
            hash.update(serde_json::to_vec(&full)?);
            hash.update(b"\n");
            ledger.push(json!({"key":[row.realtime_source_id,row.instrument_symbol,row.event_id],"row_sha256":digest(&full)?}))?;
            count += 1;
        }
    }
    let scanned = producer.await??;
    ensure!(
        scanned["complete"] == true && scanned["row_count"] == json!(count.to_string()),
        "full final event scan count differs"
    );
    let (path, rows) = ledger.finish()?;
    ensure!(rows == count, "full event ledger count differs");
    let mut latest = store.canonical_latest_quote_rows(version.clone()).await?;
    latest.sort_by(|a, b| {
        (&a.realtime_source_id, &a.instrument_symbol)
            .cmp(&(&b.realtime_source_id, &b.instrument_symbol))
    });
    ensure!(
        latest.len() == expected_latest.len(),
        "reopened latest-state count differs from independently selected authority"
    );
    let mut latest_ledger = Ledger::new(root, "terminal-final-latest-quotes.ndjson")?;
    let mut latest_hash = Sha256::new();
    for row in latest {
        let expected = expected_latest
            .get(&(
                row.realtime_source_id.clone(),
                row.instrument_symbol.clone(),
            ))
            .context("unexpected reopened latest quote scope")?;
        let full = semantic(json!(row));
        ensure!(
            full == semantic(json!(expected)),
            "reopened latest quote fields/identity/lineage differ from independently selected state"
        );
        latest_hash.update(serde_json::to_vec(&full)?);
        latest_hash.update(b"\n");
        latest_ledger
            .push(json!({"key":[row.realtime_source_id,row.instrument_symbol,row.event_id],"row_sha256":digest(&full)?}))?;
    }
    let (latest_path, latest_rows) = latest_ledger.finish()?;
    ensure!(
        store.version().await? == version,
        "final quote proof snapshot changed"
    );
    Ok(
        json!({"quote_events_verified":count.to_string(),"quote_events_sha256":hex::encode(hash.finalize()),"latest_quotes_verified":latest_rows.to_string(),"latest_quotes_sha256":hex::encode(latest_hash.finalize()),"quote_event_ledger":{"path":path,"rows":rows.to_string()},"latest_quote_ledger":{"path":latest_path,"rows":latest_rows.to_string()},"latest_independent_exact_equivalence":true}),
    )
}
async fn apply_overlay(
    stage: &Store,
    path: &Path,
    manifest: &legacy_import::LegacyManifest,
    target: &Path,
) -> Result<u64> {
    let mut bars = vec![];
    let mut quotes = vec![];
    let mut bytes = 0usize;
    let mut offset = 0u64;
    let mut count = 0u64;
    for line in ledger_reader(path)?.lines() {
        let line = line?;
        if bytes + line.len() > 4 * MIB || bars.len() + quotes.len() == 1000 {
            flush(stage, manifest, &mut bars, &mut quotes, &mut offset).await?;
            bytes = 0;
            budget(target, 16 * GIB)?;
        }
        bytes += line.len();
        let row: Value = serde_json::from_str(&line)?;
        match row["kind"].as_str() {
            Some("bar") => bars.push(serde_json::from_value(row["after"].clone())?),
            Some("quote") => quotes.push(serde_json::from_value(row["after"].clone())?),
            _ => anyhow::bail!("unknown overlay row kind"),
        };
        count += 1;
    }
    flush(stage, manifest, &mut bars, &mut quotes, &mut offset).await?;
    budget(target, 16 * GIB)?;
    Ok(count)
}
async fn flush(
    stage: &Store,
    manifest: &legacy_import::LegacyManifest,
    bars: &mut Vec<ImportBarRow>,
    quotes: &mut Vec<ImportQuoteRow>,
    offset: &mut u64,
) -> Result<()> {
    for (kind, n) in [("bar", bars.len()), ("quote", quotes.len())] {
        if n == 0 {
            continue;
        }
        let receipt = if kind == "bar" {
            stage
                .import_bars(ImportBatch {
                    context: context(manifest, "terminal_retained_overlay_bars_v3")?,
                    row_offset: *offset,
                    rows: std::mem::take(bars),
                })
                .await?
        } else {
            stage
                .import_quotes(ImportBatch {
                    context: context(manifest, "terminal_retained_overlay_quotes_v3")?,
                    row_offset: *offset,
                    rows: std::mem::take(quotes),
                })
                .await?
        };
        ensure!(
            receipt.rejected == 0 && receipt.accepted == n as u64,
            "overlay import did not accept every selected row"
        );
        *offset += n as u64;
    }
    Ok(())
}
async fn verify_selected(store: &Store, path: &Path) -> Result<Value> {
    let mut expected = Sha256::new();
    let mut actual = Sha256::new();
    let mut batch = vec![];
    let (mut bar_count, mut quote_count, mut bytes) = (0u64, 0u64, 0usize);
    let version = store.version().await?;
    for line in ledger_reader(path)?.lines() {
        let line = line?;
        if bytes + line.len() > 4 * MIB || batch.len() == 1000 {
            let (b, q) = verify_batch(
                store,
                &version,
                std::mem::take(&mut batch),
                &mut expected,
                &mut actual,
            )
            .await?;
            bar_count += b;
            quote_count += q;
            bytes = 0;
        }
        bytes += line.len();
        batch.push(serde_json::from_str::<Value>(&line)?);
    }
    let (b, q) = verify_batch(store, &version, batch, &mut expected, &mut actual).await?;
    bar_count += b;
    quote_count += q;
    let expected = hex::encode(expected.finalize());
    let actual = hex::encode(actual.finalize());
    ensure!(
        expected == actual,
        "reopened complete selected fields differ"
    );
    Ok(
        json!({"complete":true,"selected_bar_rows":bar_count.to_string(),"selected_quote_rows":quote_count.to_string(),"expected_field_sha256":expected,"actual_field_sha256":actual,"field_hash_algorithm":"SHA256 over newline-delimited full exact-row SHA256; excludes only internal applied_commit_id","stage_version":version}),
    )
}
async fn verify_batch(
    store: &Store,
    version: &SnapshotVersion,
    rows: Vec<Value>,
    expected: &mut Sha256,
    actual: &mut Sha256,
) -> Result<(u64, u64)> {
    let mut bar_keys = vec![];
    let mut quote_keys = vec![];
    for row in &rows {
        let key = &row["key"];
        match row["kind"].as_str() {
            Some("bar") => bar_keys.push(CanonicalBarKey {
                source_id: text(key, "source")?,
                symbol: text(key, "symbol")?,
                interval_seconds: key["interval"]
                    .as_u64()
                    .context("bar interval missing")?
                    .try_into()?,
                open_time_ns: text(key, "open_time_ns")?.parse()?,
            }),
            Some("quote") if row["dimension"] != "latest_statistics" => {
                quote_keys.push(CanonicalQuoteKey {
                    source_id: text(key, "source")?,
                    symbol: text(key, "symbol")?,
                    event_id: text(key, "event_id")?,
                })
            }
            Some("quote") => {}
            _ => anyhow::bail!("selected row kind invalid"),
        }
    }
    let (v, bars) = store.lookup_bars(bar_keys).await?;
    ensure!(&v == version, "reopened bar snapshot changed");
    let (v, quotes) = store.lookup_quotes(quote_keys).await?;
    ensure!(&v == version, "reopened quote snapshot changed");
    let latest_count = rows
        .iter()
        .filter(|r| r["dimension"] == "latest_statistics")
        .count() as u64;
    let latest = if latest_count > 0 {
        store
            .canonical_latest_quote_rows(version.clone())
            .await?
            .into_iter()
            .map(|r| {
                (
                    (r.realtime_source_id.clone(), r.instrument_symbol.clone()),
                    r,
                )
            })
            .collect::<BTreeMap<_, _>>()
    } else {
        BTreeMap::new()
    };
    let (b, q) = (bars.len() as u64, quotes.len() as u64 + latest_count);
    let mut bars = bars.into_iter();
    let mut quotes = quotes.into_iter();
    for row in rows {
        let value = if row["kind"] == "bar" {
            json!(
                bars.next()
                    .flatten()
                    .context("selected bar missing after reopen")?
            )
        } else if row["dimension"] == "latest_statistics" {
            json!(
                latest
                    .get(&(text(&row["key"], "source")?, text(&row["key"], "symbol")?))
                    .context("selected latest quote missing after reopen")?
            )
        } else {
            json!(
                quotes
                    .next()
                    .flatten()
                    .context("selected quote missing after reopen")?
            )
        };
        let hash = digest(&semantic(value))?;
        ensure!(
            row["expected_sha256"] == hash,
            "reopened selected exact fields or lineage differ"
        );
        expected.update(text(&row, "expected_sha256")?.as_bytes());
        expected.update(b"\n");
        actual.update(hash.as_bytes());
        actual.update(b"\n");
    }
    Ok((b, q))
}

#[tokio::main]
async fn main() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let build = tracefang_core::quant_core::results::backend_build_fingerprint();
    if args.first().is_some_and(|v| v == "identity") {
        println!(
            "{}",
            json!({"schema":"tracefang-migration-tool-identity-v1","tool":"legacy_terminal_reconcile","backend_build_sha256":build,"version":env!("CARGO_PKG_VERSION")})
        );
        return Ok(());
    }
    ensure!(
        args.len() == 12
            && args[0] == "reconcile-v3"
            && args[9] == "--production-terminal"
            && args[10] == "--tools-manifest",
        "reconcile-v3 progress-dir facts raw clock-dir spool clock-audit early-tail report --production-terminal --tools-manifest seal"
    );
    let root = Path::new(&args[1]);
    let target = Path::new(&args[2]);
    let clock = Path::new(&args[4]);
    let report = Path::new(&args[8]);
    ensure!(
        report.parent().map(std::fs::canonicalize).transpose()?
            == Some(std::fs::canonicalize(root)?),
        "terminal report must reside in progress authority directory"
    );
    ensure!(
        !report.exists(),
        "terminal report already exists; preserve earlier execution evidence"
    );
    budget(target, 16 * GIB)?;
    let started = std::time::Instant::now();
    let started_at = ns()?;
    let fixed = root.join("fixed-input-manifest.json");
    let fixed_sha = stable_hash(&fixed)?;
    let input: legacy_import::LegacyManifest = serde_json::from_reader(File::open(&fixed)?)?;
    let mut manifest: legacy_import::LegacyManifest =
        serde_json::from_reader(File::open(root.join("manifest.json"))?)?;
    ensure!(
        manifest.id == input.id && manifest.postgres == input.postgres && manifest.raw == input.raw,
        "mutable progress no longer belongs to fixed terminal input"
    );
    let source_phase = input
        .phases
        .iter()
        .find(|p| p["phase"] == "source_clock_native_staging")
        .context("fresh corrected-source import phase missing")?;
    let original_path = PathBuf::from(text(source_phase, "original_source_manifest")?);
    let source = original_path
        .parent()
        .context("original source parent missing")?;
    ensure!(
        stable_hash(&original_path)? == text(source_phase, "original_source_manifest_sha256")?,
        "original PG source changed"
    );
    let original: legacy_import::LegacyManifest =
        serde_json::from_reader(File::open(&original_path)?)?;
    ensure!(
        original.id == input.id && original.postgres == input.postgres,
        "fresh PG source differs from terminal input"
    );
    let plan =
        legacy_import::checked_clock_projection(source, clock, Path::new(&args[6]), &original)?;
    checked_copy(
        root,
        "terminal-clock-manifest.json",
        &clock.join("canonical-bars-clock-v2.manifest.json"),
    )?;
    checked_copy(root, "terminal-clock-audit.json", Path::new(&args[6]))?;
    let (sealed, executable_sha) = seal(root, Path::new(&args[11]), &build)?;
    ensure!(
        plan["policy"]["build_sha256"] == build || plan["policy"]["backend_build_sha256"] == build,
        "clock helper fingerprint differs from current backend"
    );
    let sealed_files = sealed["files"]
        .as_array()
        .context("seal input list missing")?;
    let policy_source = text(&plan, "policy_source_sha256")?;
    ensure!(
        sealed_files.iter().any(|v| v["sha256"] == policy_source),
        "clock policy source absent from seal"
    );
    for proof in plan["policy_evidence"]
        .as_array()
        .context("clock witnesses missing")?
    {
        ensure!(
            sealed_files.iter().any(|v| v["sha256"] == proof["sha256"]),
            "clock policy witness absent from seal"
        );
    }
    let auditor_sha = sealed_files
        .iter()
        .find(|v| {
            v["role"] == "clock_auditor"
                || v["path"]
                    .as_str()
                    .is_some_and(|p| p.ends_with("verify_clock_projection.py"))
        })
        .context("independent root auditor absent from seal")?["sha256"]
        .clone();
    let early: LegacyTailObservation = serde_json::from_reader(File::open(&args[7])?)?;
    let snapshot = text(&input.postgres, "captured_at")?
        .parse::<chrono::DateTime<chrono::Utc>>()?
        .timestamp_nanos_opt()
        .context("fixed snapshot clock outside ns")?;
    ensure!(
        early.observed_at_ns <= snapshot
            && snapshot <= started_at
            && json!(early.stream) == input.raw["stream"]
            && json!(early.epoch) == input.raw["epoch"]
            && json!(early.last_sequence.to_string()) == input.raw["incremental_after"]["sequence"],
        "early tail/snapshot/terminal input identities differ"
    );
    let tail: CapturePosition =
        serde_json::from_value(input.raw["native_mapping"]["last_position"].clone())?;
    ensure!(tail.sequence > 0, "empty terminal retained prefix");
    let capture = capture::Capture::open_read_only(&args[3])?;
    let actual_tail = capture.get_at(&tail).await?;
    let origin = actual_tail
        .legacy
        .as_ref()
        .context("terminal capture lacks original legacy identity")?;
    ensure!(
        origin.stream == early.stream
            && origin.epoch == early.epoch
            && origin.sequence == early.last_sequence.to_string(),
        "native/legacy terminal tail mapping differs"
    );
    let raw_dependency_proof = raw_lineage(root, &input, &capture, &tail).await?;
    let reader = legacy_spool::Reader::open(Path::new(&args[5]), &fixed_sha, &build, &tail)?;
    ensure!(
        reader.manifest.first.sequence == 1 && reader.manifest.frames == tail.sequence,
        "retained native prefix is not complete1..tail"
    );
    let spool_proof = reader.audit_all()?;
    let spool_audit = read(&root.join("terminal-spool-audit.json"))?;
    ensure!(
        spool_audit["manifest"] == json!(reader.manifest)
            && spool_audit["original_complete_decoded_roundtrip"] == spool_proof
            && spool_audit["spool_file_sha256"] == stable_hash(Path::new(&args[5]))?,
        "copied spool audit belongs to another complete decode"
    );
    let catalog = Arc::new(catalog::Catalog::embedded()?);
    let mut scopes = BTreeSet::<(String, String)>::new();
    let mut rejections = Ledger::new(root, "terminal-decode-rejections.ndjson")?;
    let (mut output_frames, mut empty_frames, mut classified, mut unknown) =
        (0u64, 0u64, 0u64, 0u64);
    for sequence in 1..=tail.sequence {
        let (record, decoded, calendar) = reader.decoded(sequence, None)?;
        match decoded {
            Ok((quotes, bars)) => {
                if let Some(calendar) = &calendar {
                    for row in calendar["calendar_authorities"]
                        .as_array()
                        .context("calendar projection authority rows missing")?
                    {
                        scopes.insert((text(row, "symbol")?, text(row, "source_id")?));
                    }
                }
                if quotes.is_empty() && bars.is_empty() && calendar.is_none() {
                    empty_frames += 1
                } else {
                    output_frames += 1
                }
                for quote in &quotes {
                    scopes.insert((
                        quote.instrument.symbol.clone(),
                        source_name(&quote.source.provider).into(),
                    ));
                    for definition in &catalog.items {
                        if definition.quote_kind == "derived"
                            && definition
                                .dependencies
                                .iter()
                                .any(|i| i == &quote.instrument)
                        {
                            for source in &definition.source_ids {
                                scopes
                                    .insert((definition.instrument.symbol.clone(), source.clone()));
                            }
                        }
                    }
                }
                for bar in bars {
                    scopes.insert((
                        bar.instrument.symbol.clone(),
                        source_name(&bar.source.provider).into(),
                    ));
                }
            }
            Err(error) => {
                let original = capture.get_at(&record.position).await?;
                let envelope = serde_json::from_slice(&original.frame.body).unwrap_or(Value::Null);
                let diagnostic = error.to_string();
                let class = legacy_reconcile::classify_rejection(
                    &record.frame.channel,
                    &envelope,
                    &diagnostic,
                );
                if class.is_some() {
                    classified += 1
                } else {
                    unknown += 1
                }
                rejections.push(json!({"capture_position":record.position,"legacy_origin":record.legacy,"channel":record.frame.channel,"body_sha256":hex::encode(Sha256::digest(&original.frame.body)),"diagnostic":diagnostic,"classification":class,"successful_quote_events":"0"}))?;
            }
        }
    }
    let (rejection_path, rejection_rows) = rejections.finish()?;
    ensure!(
        output_frames + empty_frames + classified + unknown == tail.sequence,
        "disjoint frame accounting differs"
    );
    ensure!(
        !scopes.is_empty(),
        "no affected scopes; cannot claim reconciliation"
    );
    let pg = Store::open_read_only_bounded(target, 128 * MIB)?;
    let live_before = pg.version().await?;
    let generation = format!("legacy-{}", input.id);
    ensure!(
        live_before.active_generation != generation && live_before.committed_capture.is_none(),
        "target already active or has native projection cursor"
    );
    let authority = pg.read_generation(&generation).await?;
    let pg_version = authority.version().await?;
    ensure!(
        pg_version.committed_capture.is_none(),
        "fixed source stage already claims ordinary projection"
    );
    ensure!(
        input
            .phases
            .iter()
            .any(|p| p["phase"] == "runtime_clock_series_state_repair"
                && p["state"] == "complete_inactive"),
        "fresh four-scope corrected state repair required"
    );
    // All original PG fields are independently checked before any overlay write.
    let source_bars = legacy_verify::verify_clock_bars(clock, &original, &authority).await?;
    let source_quotes = legacy_verify::verify_quotes(root, &input, &authority).await?;
    let base_counts = authority.generation_summary().await?;
    let mut expected_latest = authority
        .canonical_latest_quote_rows(pg_version.clone())
        .await?
        .into_iter()
        .map(|r| {
            (
                (r.realtime_source_id.clone(), r.instrument_symbol.clone()),
                r,
            )
        })
        .collect::<BTreeMap<_, _>>();
    let mut comparison = Ledger::new(root, "terminal-comparison.ndjson")?;
    let mut overlay = Ledger::new(root, "terminal-overlay.ndjson")?;
    let mut ranges = vec![];
    let mut totals = Counts::default();
    totals.unresolved = unknown;
    for (symbol, source_id) in scopes {
        let definition = catalog.get(&symbol)?.clone();
        ensure!(
            definition.source_ids.contains(&source_id),
            "decoded affected scope absent from current catalog source contract"
        );
        let schedule =
            serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;
        let scratch = tempfile::Builder::new()
            .prefix("terminal-scope-")
            .tempdir_in(target.parent().context("scope parent missing")?)?;
        let derived_path = scratch.path().join("facts.redb");
        let derived = Store::open_replay(&derived_path)?;
        let mut projector = replay::Projector::new_facts(
            catalog.clone(),
            definition.instrument,
            source_id.clone(),
            Period::M1,
            schedule,
        )?;
        let mut effective = 0u64;
        // Bounded ordered groups retain cross-source state while avoiding one
        // durable transaction per envelope. A complete single large frame is
        // kept whole, with an explicit decoded-residency limit.
        let mut rows = vec![];
        let mut decoded_bytes = 0usize;
        let mut projected_frames = 0u64;
        for sequence in 1..=tail.sequence {
            budget(&derived_path, 4 * GIB)?;
            let row = reader.decoded(sequence, Some(&symbol))?;
            let size = match &row.1 {
                Ok((q, b)) => {
                    serde_json::to_vec(&json!({"quotes":q,"bars":b,"calendar":row.2}))?.len()
                }
                Err(e) => e.to_string().len(),
            };
            ensure!(
                size <= 512 * MIB,
                "decoded scope frame exceeds512MiB residency bound"
            );
            if !rows.is_empty() && (rows.len() == 64 || decoded_bytes + size > 4 * MIB) {
                let n = rows.len();
                let (returned, commits) =
                    replay::retained_decoded_group(projector, std::mem::take(&mut rows), &derived)
                        .await?;
                projector = returned;
                ensure!(
                    commits.len() == n,
                    "scope projection dropped retained envelopes"
                );
                projected_frames += n as u64;
                effective += commits
                    .iter()
                    .filter(|c| !c.bars.is_empty() || !c.quotes.is_empty())
                    .count() as u64;
                derived.commit_replay_frames(commits).await?;
                decoded_bytes = 0;
            }
            decoded_bytes += size;
            rows.push(row);
        }
        if !rows.is_empty() {
            let n = rows.len();
            let (returned, commits) =
                replay::retained_decoded_group(projector, rows, &derived).await?;
            projector = returned;
            ensure!(
                commits.len() == n,
                "scope projection dropped final retained envelopes"
            );
            projected_frames += n as u64;
            effective += commits
                .iter()
                .filter(|c| !c.bars.is_empty() || !c.quotes.is_empty())
                .count() as u64;
            derived.commit_replay_frames(commits).await?;
        }
        drop(projector);
        ensure!(
            projected_frames == tail.sequence,
            "scope did not consume every retained envelope"
        );
        let derived_version = derived.version().await?;
        ensure!(
            derived_version.committed_capture.as_ref() == Some(&tail),
            "scope projector did not consume complete fixed prefix"
        );
        let mut counts = Counts::default();
        for interval in [1, 60] {
            let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
            let scan = CanonicalScanRequest {
                symbol: symbol.clone(),
                source_id: source_id.clone(),
                interval_seconds: interval,
                start_ns: i64::MIN,
                end_ns: i64::MAX,
                final_only: false,
                expected_version: Some(derived_version.clone()),
            };
            let input_store = derived.clone();
            let producer = tokio::spawn(async move {
                input_store
                    .canonical_scan(scan, 1000, move |batch| {
                        sender
                            .blocking_send(batch.rows)
                            .context("bar comparison receiver ended")
                    })
                    .await
            });
            while let Some(batch) = receiver.recv().await {
                select_bars(
                    &authority,
                    &pg_version,
                    batch,
                    &input,
                    &mut counts,
                    &mut comparison,
                    &mut overlay,
                )
                .await?;
            }
            producer.await??;
        }
        let (sender, mut receiver) = tokio::sync::mpsc::channel(2);
        let input_store = derived.clone();
        let source_copy = source_id.clone();
        let symbol_copy = symbol.clone();
        let version_copy = derived_version.clone();
        let producer = tokio::spawn(async move {
            input_store
                .canonical_quote_scan(source_copy, symbol_copy, version_copy, 1000, move |batch| {
                    sender
                        .blocking_send(batch)
                        .context("quote comparison receiver ended")
                })
                .await
        });
        while let Some(batch) = receiver.recv().await {
            select_quotes(
                &authority,
                &pg_version,
                batch,
                &input,
                &mut counts,
                &mut comparison,
                &mut overlay,
                &mut expected_latest,
            )
            .await?;
        }
        producer.await??;
        for retained in derived
            .canonical_latest_quote_rows(derived_version.clone())
            .await?
        {
            let key = (
                retained.realtime_source_id.clone(),
                retained.instrument_symbol.clone(),
            );
            let Some(previous) = expected_latest.get(&key).cloned() else {
                counts.unresolved += 1;
                comparison.push(json!({"kind":"quote","state":"unresolved","reason":"reconstructed_latest_has_no_selected_price_identity","key":quote_key_value(&retained)}))?;
                continue;
            };
            match select_latest_supplement(&previous, &retained, &tail) {
                Ok(Some((mut imported, mut selected))) => {
                    let proof = witness(
                        &input,
                        Some(json!(previous)),
                        json!(retained),
                        "later_capture_bound_statistics_only_overlay",
                    )?;
                    imported.evidence["retained_raw_reconciliation"] = proof.clone();
                    selected.evidence = imported.evidence.clone();
                    overlay.push(json!({"kind":"quote","dimension":"latest_statistics","before":previous,"after":imported,"witness":proof}))?;
                    comparison.push(json!({"kind":"quote","state":"selected","dimension":"latest_statistics","key":quote_key_value(&selected),"expected_sha256":digest(&semantic(json!(selected)))?,"witness":proof}))?;
                    expected_latest.insert(key, selected);
                    counts.overlay += 1;
                    counts.latest_statistics += 1;
                }
                Ok(None) => {}
                Err(error) => {
                    counts.unresolved += 1;
                    comparison.push(json!({"kind":"quote","state":"unresolved","dimension":"latest_statistics","key":quote_key_value(&retained),"reason":error.to_string()}))?;
                }
            }
        }
        ranges.push(json!({"symbol":symbol,"source":source_id,"raw_frames_scanned":tail.sequence.to_string(),"effective_frames":effective.to_string(),"bar_keys":counts.bars.to_string(),"quote_events":counts.quotes.to_string(),"preserved_pg":counts.preserved.to_string(),"overlay_rows":counts.overlay.to_string(),"latest_statistics_comparisons":counts.latest_statistics.to_string(),"classified_decode_errors":classified.to_string(),"unresolved_differences":counts.unresolved.to_string(),"complete":counts.unresolved==0&&unknown==0}));
        totals.bars += counts.bars;
        totals.quotes += counts.quotes;
        totals.preserved += counts.preserved;
        totals.overlay += counts.overlay;
        totals.missing_bars += counts.missing_bars;
        totals.missing_quotes += counts.missing_quotes;
        totals.unresolved += counts.unresolved;
        totals.latest_statistics += counts.latest_statistics;
        derived.close().await?;
        drop(scratch);
    }
    let (comparison_path, comparison_rows) = comparison.finish()?;
    let (overlay_path, overlay_rows) = overlay.finish()?;
    let expected_path = comparison_path.clone();
    let expected_rows = comparison_rows;
    ensure!(
        authority.version().await? == pg_version && pg.version().await? == live_before,
        "comparison changed original fixed authority"
    );
    pg.close().await?;
    capture.close_and_drain().await?;
    // Selection is complete before any target writes. Ambiguity preserves target.
    if totals.unresolved != 0 {
        legacy_import::atomic_json(
            &root.join("terminal-unresolved.json"),
            &json!({"complete":false,"unresolved_differences":totals.unresolved.to_string(),"affected_ranges":ranges,"decode_rejections":rejection_path,"comparison":comparison_path,"overlay_not_applied":true,"activated":false}),
        )?;
        anyhow::bail!(
            "terminal unresolved differences; preserved input/evidence and unchanged inactive target"
        )
    }
    ensure!(
        totals.bars + totals.quotes + totals.latest_statistics == expected_rows
            && comparison_rows == expected_rows
            && overlay_rows == totals.overlay,
        "selected row conservation differs"
    );
    let store = Store::open(target)?;
    ensure!(
        store.version().await? == live_before,
        "inactive target changed before overlay"
    );
    let stage = store.staging(&generation).await?;
    ensure!(
        stage.version().await? == pg_version,
        "authority stage changed before overlay"
    );
    ensure!(
        apply_overlay(&stage, &overlay_path, &input, target).await? == overlay_rows,
        "applied overlay count differs"
    );
    let proof = stage.verify_staging().await?;
    let live_after = store.version().await?;
    ensure!(
        live_after == live_before && stage.version().await?.committed_capture.is_none(),
        "overlay changed live authority or ordinary cursor"
    );
    store.close().await?;
    let reopened = Store::open_read_only_bounded(target, 128 * MIB)?;
    ensure!(
        reopened.version().await? == live_before,
        "reopened live authority differs"
    );
    let selected = reopened.read_generation(&generation).await?;
    let mut verification = verify_selected(&selected, &expected_path).await?;
    let summary = selected.generation_summary().await?;
    let count = |v: &Value, key: &str| -> Result<u64> {
        Ok(v["counts"][key]
            .as_str()
            .context("exact generation count missing")?
            .parse()?)
    };
    ensure!(
        count(&summary, "bar_rows")? == count(&base_counts, "bar_rows")? + totals.missing_bars
            && count(&summary, "quote_events")?
                == count(&base_counts, "quote_events")? + totals.missing_quotes
            && summary["counts"]["quote_event_identities"] == summary["counts"]["quote_events"],
        "full composite row/event identity conservation differs"
    );
    let current = selected.verify_index().await?;
    for key in [
        "fact_codec_sha256",
        "index_codec_sha256",
        "complete",
        "index_verified",
    ] {
        ensure!(
            current[key] == proof[key],
            "reopened Store proof differs: {key}"
        );
    }
    ensure!(
        proof["verified_commit_id"] == json!(selected.version().await?.commit_id.to_string()),
        "Store proof no longer binds final stage commit"
    );
    let quote_proof = final_quote_proof(&selected, root, &expected_latest).await?;
    let binding = json!({"schema":"legacy-reconciliation-binding-v3","policy":POLICY,"fixed_input_manifest_sha256":fixed_sha,"clock_projection_manifest_sha256":plan["_descriptor_sha256"],"backend_build_fingerprint":build,"capture_tail":tail,"mapping_sha256":input.raw["native_mapping"]["sha256"],"verified_fact_sha256":proof["fact_codec_sha256"],"verified_index_sha256":proof["index_codec_sha256"],"quote_events_verified":quote_proof["quote_events_verified"],"quote_events_sha256":quote_proof["quote_events_sha256"],"latest_quotes_verified":quote_proof["latest_quotes_verified"],"latest_quotes_sha256":quote_proof["latest_quotes_sha256"]});
    for (key, value) in binding.as_object().unwrap() {
        verification[key] = value.clone();
    }
    verification["schema"] = json!("legacy-reconciliation-reopen-v3");
    verification["binding"] = binding.clone();
    verification["unresolved_differences"] = json!("0");
    verification["empty_raw_seed"] = json!(true);
    verification["live_before"] = json!(live_before);
    verification["live_after"] = json!(live_after);
    verification["proof"] = proof.clone();
    verification["generation_summary"] = summary;
    verification["original_pg_verification"] = json!({"complete":true,"source_manifest_id":input.id,"postgres_snapshot":input.postgres["snapshot"],"clock_projection_manifest_sha256":plan["_descriptor_sha256"],"bars":source_bars,"quotes":source_quotes,"version":pg_version});
    verification["selected_input"] = artifact(&expected_path, expected_rows, &binding)?;
    verification["quote_event_ledger"] = artifact(
        Path::new(
            quote_proof["quote_event_ledger"]["path"]
                .as_str()
                .context("quote ledger path missing")?,
        ),
        text(&quote_proof["quote_event_ledger"], "rows")?.parse()?,
        &binding,
    )?;
    verification["latest_quote_ledger"] = artifact(
        Path::new(
            quote_proof["latest_quote_ledger"]["path"]
                .as_str()
                .context("latest ledger path missing")?,
        ),
        text(&quote_proof["latest_quote_ledger"], "rows")?.parse()?,
        &binding,
    )?;
    verification["latest_independent_exact_equivalence"] = json!(true);
    let verification_path = root.join("terminal-independent-verification.json");
    legacy_import::atomic_json(&verification_path, &verification)?;
    reopened.close().await?;
    let comparison_summary = summary_manifest(
        root,
        "terminal-comparison.json",
        "legacy-reconciliation-comparison-v3",
        &comparison_path,
        comparison_rows,
        &binding,
    )?;
    let overlay_summary = summary_manifest(
        root,
        "terminal-overlay.json",
        "legacy-reconciliation-overlay-v3",
        &overlay_path,
        overlay_rows,
        &binding,
    )?;
    let clock_manifest = artifact(&root.join("terminal-clock-manifest.json"), 1, &binding)?;
    let clock_audit = artifact(&root.join("terminal-clock-audit.json"), 1, &binding)?;
    let rejection_artifact = artifact(&rejection_path, rejection_rows, &binding)?;
    let policy_path = root.join("terminal-clock-policy-source.rs");
    checked_reference(
        root,
        "terminal-clock-policy-source.rs",
        Path::new(&text(&plan, "policy_source_file")?),
    )?;
    let policy_source_artifact = artifact(&policy_path, 1, &binding)?;
    let auditor_input = sealed_files
        .iter()
        .find(|v| v["sha256"] == auditor_sha)
        .context("sealed auditor path absent")?;
    checked_reference(
        root,
        "terminal-clock-auditor.py",
        Path::new(&text(auditor_input, "path")?),
    )?;
    let auditor_artifact = artifact(&root.join("terminal-clock-auditor.py"), 1, &binding)?;
    ensure!(
        read(&root.join("terminal-clock-audit.json"))?["oracle_source_sha256"] == auditor_sha,
        "independent audit does not bind the sealed auditor source"
    );
    let mut witness_artifacts = vec![];
    for entry in plan["policy_evidence"]
        .as_array()
        .context("clock witnesses missing")?
    {
        let input = PathBuf::from(text(entry, "file")?);
        let basename = input
            .file_name()
            .context("witness basename missing")?
            .to_string_lossy();
        checked_reference(root, &basename, &input)?;
        witness_artifacts.push(artifact(&root.join(basename.as_ref()), 1, &binding)?);
    }
    let completed = ns()?;
    let result = json!({"schema":"legacy-reconciliation-v3","production_terminal":true,"conflict_policy_version":POLICY,"policy":POLICY,"source_manifest_id":input.id,"stream":early.stream,"epoch":early.epoch,"last_sequence":early.last_sequence.to_string(),"capture_tail":tail,"mapping_sha256":input.raw["native_mapping"]["sha256"],"fixed_input_manifest_file":"fixed-input-manifest.json","fixed_input_manifest_sha256":fixed_sha,"postgres_snapshot":input.postgres["snapshot"],"postgres_source_fingerprint":input.postgres["fingerprint"],"started_at_ns":started_at.to_string(),"completed_at_ns":completed.to_string(),"early_tail_observation":early,"raw_frames_scanned":tail.sequence.to_string(),"unresolved_differences":"0","complete":true,"affected_ranges":ranges,"comparison":artifact(&comparison_summary,comparison_rows,&binding)?,"overlay":artifact(&overlay_summary,overlay_rows,&binding)?,"independent_verification":artifact(&verification_path,expected_rows,&binding)?,"verified_fact_sha256":proof["fact_codec_sha256"],"verified_index_sha256":proof["index_codec_sha256"],"backend_build_fingerprint":build,"backend_build_sha256":build,"clock_projection":{"manifest":clock_manifest,"independent_audit":clock_audit,"policy_source":policy_source_artifact,"auditor_source":auditor_artifact,"policy_evidence":witness_artifacts},"clock_binding":{"policy_id":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"source_manifest_id":input.id,"postgres_snapshot":input.postgres["snapshot"],"postgres_source_fingerprint":input.postgres["fingerprint"],"original_source_manifest_sha256":stable_hash(&original_path)?,"original_canonical_sha256":plan["original_canonical_file_sha256"],"corrected_canonical_sha256":plan["sha256"],"mapping_manifest":clock_manifest,"independent_audit":clock_audit,"policy_source_sha256":policy_source,"policy_witnesses":plan["policy_evidence"],"auditor_sha256":auditor_sha,"backend_build_sha256":build},"sealed_tools":{"file":"terminal-tools-manifest.json","sha256":stable_hash(&root.join("terminal-tools-manifest.json"))?,"backend_build_sha256":build,"executor_sha256":executable_sha},"capture_prefix":{"first":reader.manifest.first,"last":reader.manifest.last,"frames":reader.manifest.frames.to_string(),"canonical_decoded_sha256":reader.manifest.canonical_decoded_sha256,"original_prefix_complete":reader.manifest.original_prefix_complete,"origin_coverage":reader.manifest.origin_coverage,"spool_file_sha256":stable_hash(Path::new(&args[5]))?,"spool_audit":artifact(&root.join("terminal-spool-audit.json"),1,&binding)?,"raw_lineage":artifact(&root.join("terminal-raw-lineage.json"),1,&binding)?,"verified_full_retained_envelopes":raw_dependency_proof["frames"]},"frame_accounting":{"projection_frames":output_frames.to_string(),"no_output_frames":empty_frames.to_string(),"classified_rejection_frames":classified.to_string(),"unresolved_frames":unknown.to_string()},"classified_rejections":rejection_artifact,"classification_policy":CLASSIFICATION,"decode_rejections":{"file":rejection_artifact["file"],"sha256":rejection_artifact["sha256"],"complete":true,"row_count":rejection_rows.to_string(),"classified":classified.to_string(),"unresolved":unknown.to_string()},"quote_events_verified":quote_proof["quote_events_verified"],"quote_events_sha256":quote_proof["quote_events_sha256"],"latest_quotes_verified":quote_proof["latest_quotes_verified"],"latest_quotes_sha256":quote_proof["latest_quotes_sha256"],"quote_events_reconciled":true,"initial_seed":"empty","all_scopes_complete":true,"selected_bar_rows":totals.bars.to_string(),"selected_quote_rows":totals.quotes.to_string(),"binding":binding,"elapsed_ms":started.elapsed().as_secs_f64()*1000.,"activated":false,"providers_started":false});
    legacy_import::atomic_json(report, &result)?;
    manifest.phases.push(json!({"phase":"retained_raw_reconciliation","state":"verified_composite_inactive","proof":proof,"reconciliation_report_sha256":stable_hash(report)?,"reconciliation_report_file":report.file_name().map(|v|v.to_string_lossy()),"activated":false}));
    manifest.save(root)?;
    println!(
        "{}",
        json!({"report":report,"complete":true,"activated":false})
    );
    Ok(())
}
