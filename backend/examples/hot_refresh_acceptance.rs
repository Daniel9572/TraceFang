//! Controlled synthetic G06 fixture. One in-process Store writer; production
//! quant input, snapshot service, immutable export and pin/GC implementations.
//! Usage: hot_refresh_acceptance NEW_SCRATCH_DIRECTORY
#![recursion_limit = "512"]
#[path = "../src/batch_snapshot.rs"]
mod batch_snapshot;
#[path = "../src/capture.rs"]
mod capture;
#[path = "../src/catalog.rs"]
mod catalog;
#[path = "../src/quant_input.rs"]
mod quant_input;
#[path = "../src/analysis/service.rs"]
pub mod service;
// service.rs normally lives under analysis; these preserve its parent imports.
pub use tracefang_core::quant_core::{exact, quant, results, simulation, snapshot};
mod analysis {
    pub use crate::service;
    pub use tracefang_core::quant_core::{exact, quant, results, simulation, snapshot};
}
mod replay {
    pub fn projector_build_hash() -> String {
        tracefang_core::quant_core::results::backend_build_fingerprint()
    }
}
// Native-only fixture wiring follows columnar_api_probe. Research requests are
// rejected explicitly; this helper does not stand in for the installed server.
mod api {
    use anyhow::Result;
    use axum::{
        Json,
        http::StatusCode,
        response::{IntoResponse, Response},
    };
    use serde_json::Value;
    use std::sync::Arc;
    #[derive(Clone)]
    pub struct Market {
        pub catalog: Arc<crate::catalog::Catalog>,
        pub store: tracefang_core::native_store::Store,
    }
    impl Market {
        pub fn source(&self, symbol: &str) -> Result<String> {
            Ok(self.catalog.get(symbol)?.source_ids[0].clone())
        }
    }
    #[derive(Clone)]
    pub struct Research;
    impl Research {
        pub async fn scan_authority<F>(
            &self,
            _: &crate::analysis::quant::QuantInputRequest,
            _: usize,
            _: F,
        ) -> Result<tracefang_core::persistence_contract::CanonicalScanSummary>
        where
            F: FnMut(crate::analysis::quant::QuantInput) -> Result<()> + Send + 'static,
        {
            anyhow::bail!("synthetic G06 fixture supports native authority only")
        }
    }
    #[derive(Clone)]
    pub struct AppState {
        pub market: Market,
        pub research: Research,
    }
    pub struct ApiError(pub StatusCode, pub String);
    impl IntoResponse for ApiError {
        fn into_response(self) -> Response {
            (self.0, Json(serde_json::json!({"detail":self.1}))).into_response()
        }
    }
    impl From<anyhow::Error> for ApiError {
        fn from(value: anyhow::Error) -> Self {
            Self(StatusCode::BAD_REQUEST, value.to_string())
        }
    }
    pub type ApiResult = Result<Json<Value>, ApiError>;
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

use analysis::{
    quant::{QuantInputRequest, ResumeCursor},
    snapshot::SnapshotAccumulator,
};
use anyhow::{Context, Result, ensure};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tracefang_core::{
    native_store::{ScanResume, Store},
    periods::Period,
    persistence_contract::*,
};

const PREFIX: i64 = 8192;
const APPEND: i64 = 3;
const MINUTE: i64 = 60_000_000_000;
const BASE_NS: i64 = 1_790_035_200_000_000_000; // 2026-09-22T00:00:00Z
const SOURCE: &str = "jin10_client";
const SYMBOL: &str = "XAU/USD";
const GENERATION: &str = "controlled-synthetic-g06";
const SCENARIOS: [&str; 4] = ["nochange", "append", "unrelatedcommit", "earlycorrection"];
const SEMANTIC_CASES: [&str; 4] = [
    "quote_only",
    "forming_to_final",
    "calendar_only",
    "source_volume_component",
];
fn fixed_request() -> Result<QuantInputRequest> {
    Ok(QuantInputRequest {
        code: "XAUUSD".into(),
        source_id: Some(SOURCE.into()),
        period: "1m".into(),
        decision_as_of: Some("2026-10-01T00:00:00Z".parse()?),
        ..Default::default()
    })
}
fn never() -> batch_snapshot::Cancel {
    Arc::new(|| false)
}
fn silent() -> batch_snapshot::Progress {
    Arc::new(|_| {})
}
fn number(value: &Value) -> Result<u64> {
    value
        .as_u64()
        .or_else(|| value.as_str()?.parse().ok())
        .context("exact unsigned receipt field missing")
}
fn digest(value: &impl serde::Serialize) -> Result<String> {
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(value)?)))
}
fn identity(metadata: &fs::Metadata) -> Value {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        json!({"dev":metadata.dev().to_string(),"inode":metadata.ino().to_string(),"bytes":metadata.len().to_string(),
            "mtime_seconds":metadata.mtime().to_string(),"mtime_ns":metadata.mtime_nsec().to_string(),
            "ctime_seconds":metadata.ctime().to_string(),"ctime_ns":metadata.ctime_nsec().to_string()})
    }
    #[cfg(not(unix))]
    {
        json!({"bytes":metadata.len().to_string(),"modified":format!("{:?}",metadata.modified())})
    }
}
fn file_receipt(path: &Path) -> Result<Value> {
    let mut file = File::open(path)?;
    let before = identity(&file.metadata()?);
    let mut count = 0_u64;
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        count += n as u64;
        hash.update(&buffer[..n]);
    }
    ensure!(
        number(&before["bytes"])? == count
            && before == identity(&file.metadata()?)
            && before == identity(&fs::metadata(path)?),
        "short or changed file read: {}",
        path.display()
    );
    #[cfg(unix)]
    let mtime_ns = {
        use std::os::unix::fs::MetadataExt;
        let m = file.metadata()?;
        (i128::from(m.mtime()) * 1_000_000_000 + i128::from(m.mtime_nsec())).to_string()
    };
    #[cfg(not(unix))]
    let mtime_ns = format!("{:?}", file.metadata()?.modified()?);
    Ok(
        json!({"path":path,"sha256":hex::encode(hash.finalize()),"bytes":count,"mtime_ns":mtime_ns,"identity":before}),
    )
}
fn read_json(path: &Path) -> Result<Value> {
    let before = file_receipt(path)?;
    let bytes = fs::read(path)?;
    ensure!(
        bytes.len() as u64 == number(&before["identity"]["bytes"])?
            && hex::encode(Sha256::digest(&bytes)) == before["sha256"],
        "JSON read differs: {}",
        path.display()
    );
    Ok(serde_json::from_slice(&bytes)?)
}
fn write_json(path: &Path, value: &Value) -> Result<()> {
    let mut output = OpenOptions::new().write(true).create_new(true).open(path)?;
    serde_json::to_writer_pretty(&mut output, value)?;
    output.write_all(b"\n")?;
    output.sync_all()?;
    Ok(())
}
fn files(root: &Path) -> Result<BTreeMap<String, Value>> {
    fn walk(root: &Path, path: &Path, out: &mut BTreeMap<String, Value>) -> Result<()> {
        if !path.exists() {
            return Ok(());
        }
        for entry in fs::read_dir(path)? {
            let entry = entry?;
            if entry.file_type()?.is_dir() {
                walk(root, &entry.path(), out)?;
            } else {
                ensure!(
                    entry.file_type()?.is_file(),
                    "fixture cannot contain symlinks"
                );
                out.insert(
                    entry
                        .path()
                        .strip_prefix(root)?
                        .to_string_lossy()
                        .into_owned(),
                    file_receipt(&entry.path())?,
                );
            }
        }
        Ok(())
    }
    let mut out = BTreeMap::new();
    walk(root, root, &mut out)?;
    Ok(out)
}
fn copy_verified(source: &Path, destination: &Path) -> Result<()> {
    ensure!(!destination.exists(), "refuse to replace fixture copy");
    let before = file_receipt(source)?;
    fs::copy(source, destination)?;
    File::options()
        .write(true)
        .open(destination)?
        .set_times(fs::FileTimes::new().set_modified(fs::metadata(source)?.modified()?))?;
    let after = file_receipt(destination)?;
    ensure!(
        before == file_receipt(source)?
            && before["sha256"] == after["sha256"]
            && before["bytes"] == after["bytes"],
        "fixture copy differs"
    );
    File::open(destination)?.sync_all()?;
    Ok(())
}
fn copy_tree(source: &Path, destination: &Path) -> Result<()> {
    fs::create_dir(destination)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            copy_tree(&entry.path(), &destination.join(entry.file_name()))?;
        } else {
            ensure!(
                entry.file_type()?.is_file(),
                "fixture copy rejects symlinks"
            );
            copy_verified(&entry.path(), &destination.join(entry.file_name()))?;
        }
    }
    Ok(())
}
fn config(directory: &Path, version: &SnapshotVersion) -> Value {
    json!({"data":directory.join("data"),"store":directory.join("facts.redb"),"capture":directory.join("capture.redb"),
        "read_only":true,"rehearsal_generation":version.active_generation,"snapshot_version":version})
}
fn creation(directory: &Path, version: &SnapshotVersion, detail: Value) -> Result<Value> {
    let value = json!({"schema":"tracefang-controlled-fixture-creation-v1","complete":true,"data_kind":"controlled_synthetic","market_truth":false,
        "backend_build_fingerprint":replay::projector_build_hash(),"request":fixed_request()?,"config":config(directory,version),"snapshot_version":version,
        "store_file":file_receipt(&directory.join("facts.redb"))?,"capture_file":file_receipt(&directory.join("capture.redb"))?,"store_closed":true,"capture_closed":false,"capture_drained":true,"physical_seal_complete":false,"requires_post_process_exit_seal":true,
        "detail":detail,"actual_facts_or_raw_opened":false,"production_modified":false});
    write_json(&directory.join("creation-receipt.provisional.json"), &value)?;
    Ok(file_receipt(
        &directory.join("creation-receipt.provisional.json"),
    )?)
}
fn provisional_creation_path(directory: &Path) -> PathBuf {
    let provisional = directory.join("creation-receipt.provisional.json");
    if provisional.exists() {
        provisional
    } else {
        directory.join("creation-receipt.json")
    }
}
async fn seal_closed_creation(directory: &Path) -> Result<Value> {
    let no_handles = || -> Result<()> {
        let observed = std::process::Command::new("/usr/sbin/lsof")
            .args(["-nP", "--"])
            .arg(directory.join("facts.redb"))
            .arg(directory.join("capture.redb"))
            .output()?;
        ensure!(
            observed.status.code() == Some(1)
                && observed.stdout.is_empty()
                && observed.stderr.is_empty(),
            "fixture handles remain open or handle inspection failed"
        );
        Ok(())
    };
    no_handles()?;
    let original = provisional_creation_path(directory);
    let mut value = read_json(&original)?;
    ensure!(
        value["data_kind"] == "controlled_synthetic",
        "fixture identity differs"
    );
    let store_file = file_receipt(&directory.join("facts.redb"))?;
    ensure!(
        store_file["sha256"] == value["store_file"]["sha256"],
        "closed facts changed"
    );
    let capture = capture::Capture::open_read_only(directory.join("capture.redb"))?;
    let bounds = capture.bounds().await?;
    ensure!(
        bounds["message_count"] == "0",
        "controlled fixture capture is not empty"
    );
    if let Some(expected) = value.pointer("/detail/capture_bounds") {
        ensure!(
            *expected == bounds,
            "closed baseline capture metadata differs"
        );
    }
    capture.close().await?;
    drop(capture);
    no_handles()?;
    value["provisional_capture_file"] = value["capture_file"].clone();
    value["provisional_capture_closed_claim"] = value["capture_closed"].clone();
    value["store_file"] = store_file;
    value["capture_file"] = file_receipt(&directory.join("capture.redb"))?;
    value["capture_closed"] = json!(true);
    value["physical_seal_complete"] = json!(true);
    value["requires_post_process_exit_seal"] = json!(false);
    value["post_exit_capture_bounds"] = bounds;
    value["provisional_receipt"] = file_receipt(&original)?;
    value["file_seal_phase"] =
        json!("after producing child process exit and independent OSRO empty-capture reopen");
    let sealed = directory.join("creation-receipt.closed.json");
    write_json(&sealed, &value)?;
    file_receipt(&sealed)
}
async fn seal_closed_semantic(directory: &Path) -> Result<Value> {
    let facts = directory.join("facts.redb");
    let no_handles = || -> Result<()> {
        let observed = std::process::Command::new("/usr/sbin/lsof")
            .args(["-nP", "--"])
            .arg(&facts)
            .output()?;
        ensure!(
            observed.status.code() == Some(1)
                && observed.stdout.is_empty()
                && observed.stderr.is_empty(),
            "semantic facts handles remain open or inspection failed"
        );
        Ok(())
    };
    no_handles()?;
    ensure!(
        !directory.join("capture.redb").exists(),
        "semantic fixture unexpectedly has raw capture"
    );
    let case = read_json(&directory.join("receipt.json"))?;
    let before = file_receipt(&facts)?;
    let store = Store::open_read_only_bounded(&facts, 128 * 1024 * 1024)?;
    let view = store
        .read_generation(
            case["after_version"]["active_generation"]
                .as_str()
                .context("semantic generation")?,
        )
        .await?;
    ensure!(
        serde_json::to_value(view.version().await?)? == case["after_version"],
        "closed semantic version differs"
    );
    drop(view);
    store.close().await?;
    no_handles()?;
    ensure!(
        file_receipt(&facts)? == before,
        "semantic OSRO verification changed facts"
    );
    let path = directory.join("semantic-physical-receipt.json");
    write_json(
        &path,
        &json!({"schema":"tracefang-controlled-semantic-physical-v1","complete":true,"data_kind":"controlled_synthetic","market_truth":false,"store_file":before,"store_closed":true,"snapshot_version":case["after_version"],"case_receipt":file_receipt(&directory.join("receipt.json"))?,"raw_capture_present":false,"raw_capture_proof_claimed":false,"file_seal_phase":"after producing child process exit, no handles and unchanged independent OSRO version reopen"}),
    )?;
    file_receipt(&path)
}
fn child(mode: &str, directory: &Path, name: Option<&str>) -> Result<()> {
    let mut command = std::process::Command::new(std::env::current_exe()?);
    command.arg(mode).arg(directory);
    if let Some(name) = name {
        command.arg(name);
    }
    let stdout = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("stdout.log"))?;
    let stderr = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(directory.join("stderr.log"))?;
    let status = command.stdout(stdout).stderr(stderr).status()?;
    ensure!(
        status.success(),
        "{mode} failed; inspect {}",
        directory.join("failure.json").display()
    );
    Ok(())
}
async fn prepare_baseline(root: &Path) -> Result<Value> {
    let directory = root.join("baseline");
    fs::create_dir(&directory)?;
    fs::create_dir(directory.join("data"))?;
    let store = Store::open(directory.join("facts.redb"))?
        .staging(GENERATION)
        .await?;
    let imported = store
        .import_bars(batch(
            0,
            (0..PREFIX)
                .map(|minute| row(minute, format!("{}.{}", 100 + minute % 11, minute % 10), 1))
                .collect(),
        ))
        .await?;
    let proof = store.verify_staging().await?;
    let version = store.version().await?;
    store.close().await?;
    let capture = capture::Capture::open(directory.join("capture.redb"), Default::default())?;
    let bounds = capture.bounds().await?;
    capture.close().await?;
    let creation = creation(
        &directory,
        &version,
        json!({"import_receipt":imported,"Store_issued_index_proof":proof,"capture_bounds":bounds,"prefix_rows":PREFIX}),
    )?;
    Ok(json!({"config":config(&directory,&version),"creation_receipt":creation,"version":version}))
}
fn inventory(root: &Path) -> Result<BTreeMap<String, Value>> {
    let mut output = BTreeMap::new();
    if !root.exists() {
        return Ok(output);
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let id = entry.file_name().to_string_lossy().into_owned();
        if !entry.file_type()?.is_dir() || id.len() != 64 {
            continue;
        }
        let manifest_path = entry.path().join("manifest.json");
        let manifest = read_json(&manifest_path)?;
        let body = file_receipt(
            &entry
                .path()
                .join(manifest["file"].as_str().context("snapshot body file")?),
        )?;
        ensure!(
            body["sha256"] == manifest["file_sha256"]
                && number(&body["identity"]["bytes"])? == number(&manifest["file_bytes"])?
                && manifest["id"] == id,
            "manifest/body binding differs"
        );
        output.insert(id, json!({"manifest":manifest,"manifest_file":file_receipt(&manifest_path)?,"body_file":body}));
    }
    Ok(output)
}
fn export_delta(
    before: &BTreeMap<String, Value>,
    after: &BTreeMap<String, Value>,
) -> Result<Value> {
    for (id, previous) in before {
        ensure!(
            after.get(id) == Some(previous),
            "immutable snapshot changed/disappeared: {id}"
        );
    }
    let new: BTreeMap<_, _> = after
        .iter()
        .filter(|(id, _)| !before.contains_key(*id))
        .map(|(id, v)| (id.clone(), v.clone()))
        .collect();
    let mut rows = 0;
    let mut bytes = 0;
    for value in new.values() {
        rows += number(&value["manifest"]["summary"]["row_count"])?;
        bytes += number(&value["body_file"]["identity"]["bytes"])?;
    }
    Ok(
        json!({"exported_rows":rows.to_string(),"exported_parquet_bytes":bytes.to_string(),"new_snapshots":new,"existing_manifest_and_body_identity_unchanged":true}),
    )
}
fn row(minute: i64, price: String, revision: u64) -> ImportBarRow {
    let ns = BASE_NS + minute * MINUTE;
    ImportBarRow {
        instrument_symbol: SYMBOL.into(),
        realtime_source_id: SOURCE.into(),
        evidence_channel_id: "jin10_local".into(),
        interval_seconds: 60,
        open_time_ns: ns,
        close_time_ns: ns + MINUTE,
        open: price.clone(),
        high: price.clone(),
        low: price.clone(),
        close: price,
        volume: (minute % 3 != 0).then(|| {
            if minute % 3 == 1 {
                "0".into()
            } else {
                "2.0000000000000000000000000001".into()
            }
        }),
        revision,
        received_sequence: Some(minute as u64),
        state: "final".into(),
        finalized_at_ns: Some(ns + MINUTE),
        source_observed_at_ns: ns + MINUTE,
        received_at_ns: ns + MINUTE,
        source_metadata: json!({"provider":SOURCE,"provider_symbol":"XAUUSD.GOODS","raw_payload":{"synthetic":true}}),
        evidence: json!({"kind":"controlled_synthetic_fixture","market_truth":false}),
    }
}
fn batch(offset: u64, rows: Vec<ImportBarRow>) -> ImportBatch<ImportBarRow> {
    ImportBatch {
        context: ImportContext {
            origin_id: "hot-refresh-controlled-synthetic-v1".into(),
            source_fingerprint: "deterministic-synthetic-bars-v1".into(),
            schema_version: SCHEMA_VERSION.into(),
            range_label: "bars".into(),
            legacy_cursor: None,
            expected_sha256: None,
        },
        row_offset: offset,
        rows,
    }
}
async fn plan(state: &api::AppState, request: &QuantInputRequest) -> Result<batch_snapshot::Plan> {
    Ok(batch_snapshot::Plan {
        scan: CanonicalScanRequest {
            symbol: SYMBOL.into(),
            source_id: SOURCE.into(),
            interval_seconds: 60,
            start_ns: i64::MIN,
            end_ns: request
                .decision_as_of
                .context("fixed cutoff")?
                .timestamp_nanos_opt()
                .context("cutoff outside ns")?,
            final_only: false,
            expected_version: Some(state.market.store.version().await?),
        },
        period: Period::M1,
        schedule: Some(pages::schedule(&state.market, "XAUUSD")?),
        resume: None,
        build: replay::projector_build_hash(),
    })
}
async fn complete(
    client: &reqwest::Client,
    base: &str,
    request: &QuantInputRequest,
) -> Result<Value> {
    let mut value: Value = client
        .post(format!("{base}/api/expert/quant/snapshot"))
        .json(request)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let deadline = Instant::now() + Duration::from_secs(180);
    while value["state"] == "building" {
        ensure!(
            Instant::now() < deadline,
            "synthetic snapshot completion timed out"
        );
        let id = value["job_id"].as_str().context("snapshot job id")?;
        value = client
            .get(format!(
                "{base}/api/expert/quant/snapshot/jobs/{id}?wait_ms=2000"
            ))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
    }
    ensure!(
        value["state"] == "ready" && value["complete"] == true,
        "snapshot failed: {value}"
    );
    Ok(value)
}
/// Save the real production status handler responses for D1's independent
/// offline full/delta reconstruction. No delta implementation is duplicated.
async fn save_job_responses(
    client: &reqwest::Client,
    base: &str,
    ready: &Value,
    directory: &Path,
    label: &str,
    prior_basis: Option<&str>,
) -> Result<Value> {
    let id = ready["job_id"].as_str().context("actual job id")?;
    let basis = ready["chart_basis_hash"]
        .as_str()
        .context("actual chart basis")?;
    let path = format!("/api/expert/quant/snapshot/jobs/{id}");
    let full: Value = client
        .get(format!("{base}{path}"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    ensure!(
        full["snapshot"] == ready["snapshot"],
        "same actual job full response changed"
    );
    let known: Value = client
        .get(format!("{base}{path}"))
        .query(&[("known_chart_basis_hash", basis)])
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    let full_path = directory.join(format!("{label}-job-full.json"));
    write_json(&full_path, &full)?;
    let known_path = directory.join(format!("{label}-job-known-basis.json"));
    write_json(&known_path, &known)?;
    let mut out = json!({"job_id":id,"api_url_at_measurement":base,"process_id":std::process::id(),"known_basis":basis,
        "production_handler":"analysis/service.rs snapshot_status_payload via actual GET job route",
        "default_full":{"request_path":path,"response":file_receipt(&full_path)?},
        "matching_basis":{"request_path":path,"query":{"known_chart_basis_hash":basis},"response":file_receipt(&known_path)?},
        "offline_read_method":"stable whole-length SHA-pinned JSON response files; use full bars/series to reconstruct matching delta metadata. API job URL is only live until this fixture process joins/closes."});
    if let Some(prior) = prior_basis {
        let response: Value = client
            .get(format!("{base}{path}"))
            .query(&[("known_chart_basis_hash", prior)])
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        let response_path = directory.join(format!("{label}-job-prior-basis.json"));
        write_json(&response_path, &response)?;
        out["prior_basis"] = json!({"request_path":path,"query":{"known_chart_basis_hash":prior},"response":file_receipt(&response_path)?});
    }
    // Available while the child is still alive; the final receipt separately
    // records server_joined, so no surviving pointer claims a live post-exit job.
    let pointer = directory.join(format!("{label}-job-pointer.json"));
    write_json(&pointer, &out)?;
    out["pointer_file"] = file_receipt(&pointer)?;
    Ok(out)
}
async fn oracle(state: &api::AppState, request: &QuantInputRequest) -> Result<Value> {
    let accumulator = Arc::new(Mutex::new(SnapshotAccumulator::new(
        request.parameters.clone(),
    )?));
    let worker = accumulator.clone();
    let summary = quant_input::scan(state, request, 256, move |input| {
        worker.lock().unwrap().push(input)
    })
    .await?;
    ensure!(summary.complete, "cold oracle incomplete");
    let value = serde_json::to_value(accumulator.lock().unwrap().current()?)?;
    Ok(value)
}
fn business(snapshot: &Value) -> Value {
    let mut confirmed = snapshot["confirmed"].clone();
    // Derived external context carries the current MVCC version even when the
    // underlying rows are unchanged. Compare its values here; the independent
    // same-version cold oracle below still compares every provenance field.
    if let Some(context) = confirmed
        .pointer_mut("/indicators/multi_timeframe")
        .and_then(Value::as_object_mut)
    {
        for key in ["record_id", "revision", "snapshot_token"] {
            context.remove(key);
        }
        if let Some(provenance) = context.get_mut("provenance").and_then(Value::as_object_mut) {
            provenance.remove("snapshot_version");
        }
    }
    if let Some(signals) = confirmed.get_mut("signals").and_then(Value::as_array_mut) {
        for signal in signals {
            if signal["strategy_id"] == "multi-timeframe" {
                signal.as_object_mut().unwrap().remove("evidence");
            }
        }
    }
    json!({"bars":snapshot["bars"],"series":snapshot["series"],"confirmed":confirmed,
        "confirmed_prefix_hash":snapshot["evidence"]["confirmed_prefix_hash"],"confirmed_count":snapshot["evidence"]["confirmed_count"]})
}
async fn tail_receipt(
    store: &Store,
    p: &batch_snapshot::Plan,
    resume: &ResumeCursor,
) -> Result<Value> {
    let proof = ScanResume {
        after_ns: resume
            .after
            .timestamp_nanos_opt()
            .context("resume outside ns")?,
        series_generation: resume.series_version.series_generation.clone(),
        correction_epoch: resume.series_version.correction_epoch,
        append_watermark_ns: resume
            .series_version
            .append_watermark_ns
            .as_deref()
            .map(str::parse)
            .transpose()?,
    };
    match store
        .materialize_tail(
            p.scan.clone(),
            p.period,
            p.schedule.clone(),
            proof,
            4096,
            16 * 1024 * 1024,
            never(),
        )
        .await
    {
        Ok(materialized) => {
            let mut context_bytes = 0;
            let mut rows_bytes = 0;
            let mut rows = 0;
            for batch in &materialized.batches {
                if let Some(context) = &batch.context {
                    context_bytes += serde_json::to_vec(context)?.len();
                }
                rows_bytes += serde_json::to_vec(&batch.rows)?.len();
                rows += batch.rows.len();
            }
            ensure!(
                rows as u64 == materialized.summary.row_count
                    && rows <= 4096
                    && context_bytes + rows_bytes <= 16 * 1024 * 1024,
                "materialized tail bounds differ"
            );
            Ok(
                json!({"accepted":true,"materialized_rows":rows.to_string(),"row_arrays_json_bytes":rows_bytes.to_string(),"context_json_bytes":context_bytes.to_string(),
                "materialized_json_bytes":(context_bytes+rows_bytes).to_string(),"summary":materialized.summary,"measurement":"separate same-version Store call; exact serializers and bounds used by production materialize_tail"}),
            )
        }
        Err(error) => {
            ensure!(
                error.to_string().contains("quant_resume_invalid"),
                "unexpected tail error: {error}"
            );
            Ok(
                json!({"accepted":false,"rejection":error.to_string(),"materialized_rows":"0","materialized_json_bytes":"0"}),
            )
        }
    }
}

async fn scenario(root: &Path, name: &str) -> Result<Value> {
    ensure!(SCENARIOS.contains(&name), "unknown scenario");
    let directory = root.to_path_buf();
    let store = Store::open(directory.join("facts.redb"))?
        .read_generation(GENERATION)
        .await?;
    let state = api::AppState {
        market: api::Market {
            catalog: Arc::new(catalog::Catalog::embedded()?),
            store: store.clone(),
        },
        research: api::Research,
    };
    let request = fixed_request()?;
    let baseline_creation = read_json(
        &root
            .parent()
            .context("baseline parent")?
            .join("baseline/creation-receipt.closed.json"),
    )?;
    ensure!(
        serde_json::to_value(store.version().await?)? == baseline_creation["snapshot_version"],
        "copied baseline version differs"
    );
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let base = format!("http://{}", listener.local_addr()?);
    let instance = json!({"process_id":std::process::id(),"instance_id":uuid::Uuid::new_v4().to_string(),"api_url":base,"acquisition_enabled":false});
    let instance_route = instance.clone();
    let app = analysis::service::router().with_state(state.clone()).route(
        "/fixture-instance",
        axum::routing::get(move || {
            let value = instance_route.clone();
            async move { axum::Json(value) }
        }),
    );
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .await
    });
    let client = reqwest::Client::builder().no_proxy().build()?;
    let instance_before: Value = client
        .get(format!("{base}/fixture-instance"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    ensure!(instance_before == instance, "API instance identity differs");
    let snapshots = root.join("batch-snapshots");
    let before_cold = inventory(&snapshots)?;
    let baseline = complete(&client, &base, &request).await?;
    let before_job = save_job_responses(&client, &base, &baseline, root, "before", None).await?;
    ensure!(
        number(&baseline["profile"]["delivered_rows"])? == PREFIX as u64
            && baseline["profile"]["load_checkpoint_detail"]["state"] == "absent",
        "baseline was not a full cold scan"
    );
    let after_cold = inventory(&snapshots)?;
    let cold_export = export_delta(&before_cold, &after_cold)?;
    ensure!(
        number(&cold_export["exported_rows"])? == PREFIX as u64
            && number(&cold_export["exported_parquet_bytes"])? > 0,
        "cold export missing"
    );
    let old_plan = plan(&state, &request).await?;
    let phases = Arc::new(Mutex::new(Vec::<Value>::new()));
    let phase_writer = phases.clone();
    let old_snapshot = batch_snapshot::publish(
        &snapshots,
        &store,
        old_plan.clone(),
        never(),
        Arc::new(move |v| phase_writer.lock().unwrap().push(v)),
    )
    .await?;
    ensure!(
        phases
            .lock()
            .unwrap()
            .iter()
            .any(|v| v["phase"] == "reusing_immutable_input"),
        "exact plan did not reuse"
    );
    ensure!(
        inventory(&snapshots)? == after_cold,
        "exact reuse wrote snapshot bytes"
    );
    let first = Arc::new(Mutex::new(None));
    let first_writer = first.clone();
    quant_input::scan(&state, &request, 256, move |input| {
        let mut out = first_writer.lock().unwrap();
        if out.is_none() {
            *out = Some(input);
        }
        Ok(())
    })
    .await?;
    let input = first
        .lock()
        .unwrap()
        .take()
        .context("baseline context missing")?;
    let resume = ResumeCursor {
        after: DateTime::from_timestamp_nanos(BASE_NS + (PREFIX - 1) * MINUTE),
        series_version: input.series_version.context("series resume proof")?,
    };
    let before_version = store.version().await?;
    let checkpoints = root.join("quant-results/checkpoints");
    let checkpoint_before = files(&checkpoints)?;
    ensure!(
        !checkpoint_before.is_empty(),
        "cold baseline did not persist checkpoint"
    );
    let baseline_cache = root.join("baseline-cache");
    fs::create_dir(&baseline_cache)?;
    copy_tree(
        &root.join("quant-results"),
        &baseline_cache.join("quant-results"),
    )?;
    copy_tree(&snapshots, &baseline_cache.join("batch-snapshots"))?;
    let preserved_checkpoint = files(&baseline_cache.join("quant-results/checkpoints"))?;
    let mutation = match name {
        "nochange" => json!({"writer_operation":"none"}),
        "append" => {
            json!({"writer_operation":"Store::import_bars","receipt":store.import_bars(batch(PREFIX as u64,(PREFIX..PREFIX+APPEND).map(|minute|row(minute,"125.0000000000000000000000000001".into(),1)).collect())).await?})
        }
        "unrelatedcommit" => {
            json!({"writer_operation":"Store::set_metadata","receipt":store.set_metadata("synthetic_fixture","unrelated",json!({"kind":"unrelated_config_commit","synthetic":true})).await?})
        }
        "earlycorrection" => {
            json!({"writer_operation":"Store::import_bars","receipt":store.import_bars(batch(PREFIX as u64,vec![row(8,"777.0000000000000000000000000001".into(),2)])).await?})
        }
        _ => anyhow::bail!("unknown scenario"),
    };
    // A changed fixture needs a current Store-issued verification for the final
    // server's inactive RO gate. This is a real fixture-only metadata commit;
    // it is recorded and occurs before all first-refresh measurements.
    let post_mutation_verification = if name == "nochange" {
        Value::Null
    } else {
        store.verify_staging().await?
    };
    let after_version = store.version().await?;
    let stale = store
        .canonical_scan_context(old_plan.scan.clone(), old_plan.schedule.clone())
        .await;
    let stale_error = stale.as_ref().err().map(ToString::to_string);
    ensure!(
        stale.is_ok() == (name == "nochange"),
        "old global token acceptance differs"
    );
    let current_plan = plan(&state, &request).await?;
    let tail = tail_receipt(&store, &current_plan, &resume).await?;
    ensure!(
        tail["accepted"] == (name != "earlycorrection"),
        "old prefix resume acceptance differs"
    );
    if name != "earlycorrection" {
        ensure!(
            number(&tail["materialized_rows"])? == if name == "append" { APPEND as u64 } else { 0 },
            "tail row count differs"
        );
    }
    let before_hot = inventory(&snapshots)?;
    let parquet_before = files(&snapshots)?;
    let hot = complete(&client, &base, &request).await?;
    let after_job = save_job_responses(
        &client,
        &base,
        &hot,
        root,
        "after",
        Some(
            baseline["chart_basis_hash"]
                .as_str()
                .context("old chart basis")?,
        ),
    )
    .await?;
    let after_hot = inventory(&snapshots)?;
    let parquet_after = files(&snapshots)?;
    for (path, before) in &parquet_before {
        ensure!(
            parquet_after.get(path) == Some(before),
            "previous cache/Parquet file changed: {path}"
        );
    }
    let new_parquet_files: BTreeMap<_, _> = parquet_after
        .iter()
        .filter(|(path, _)| path.ends_with(".parquet") && !parquet_before.contains_key(*path))
        .map(|(path, value)| (path.clone(), value.clone()))
        .collect();
    let checkpoint_after = files(&checkpoints)?;
    let instance_after: Value = client
        .get(format!("{base}/fixture-instance"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    ensure!(
        instance_before == instance_after,
        "mutation/refresh changed API process/instance"
    );
    let exported = export_delta(&before_hot, &after_hot)?;
    let expected_rows = if name == "earlycorrection" {
        PREFIX as u64
    } else {
        0
    };
    ensure!(
        number(&exported["exported_rows"])? == expected_rows,
        "hot export row count differs"
    );
    ensure!(
        (number(&exported["exported_parquet_bytes"])? > 0) == (name == "earlycorrection"),
        "hot export byte behavior differs"
    );
    ensure!(
        number(&hot["profile"]["delivered_rows"])?
            == match name {
                "append" => APPEND as u64,
                "earlycorrection" => PREFIX as u64,
                _ => 0,
            },
        "service delivered wrong rows"
    );
    ensure!(
        hot["profile"]["checkpoint_rewritten"] == matches!(name, "append" | "earlycorrection"),
        "checkpoint rewrite differs"
    );
    if matches!(name, "nochange" | "unrelatedcommit") {
        ensure!(
            checkpoint_before == checkpoint_after,
            "unchanged prefix rewrote persisted checkpoint"
        );
    }
    ensure!(
        number(&hot["snapshot"]["evidence"]["confirmed_count"])?
            == (PREFIX + if name == "append" { APPEND } else { 0 }) as u64,
        "confirmed count differs"
    );
    if matches!(name, "nochange" | "unrelatedcommit") {
        ensure!(
            business(&hot["snapshot"]) == business(&baseline["snapshot"])
                && hot["chart_basis_hash"] == baseline["chart_basis_hash"]
                && hot["snapshot"]["evidence"]["effective_input_hash"]
                    == baseline["snapshot"]["evidence"]["effective_input_hash"],
            "unchanged business input differs"
        );
        if name == "nochange" {
            ensure!(
                hot["snapshot"]["evidence"]["input_hash"]
                    == baseline["snapshot"]["evidence"]["input_hash"],
                "no-change input hash differs"
            );
        }
    } else {
        ensure!(
            hot["snapshot"]["evidence"]["confirmed_prefix_hash"]
                != baseline["snapshot"]["evidence"]["confirmed_prefix_hash"],
            "changed prefix retained old hash"
        );
    }
    let warm = complete(&client, &base, &request).await?;
    let checkpoint_after_second = files(&checkpoints)?;
    ensure!(
        warm["snapshot"] == hot["snapshot"]
            && number(&warm["profile"]["delivered_rows"])? == 0
            && warm["profile"]["checkpoint_rewritten"] == false
            && checkpoint_after_second == checkpoint_after
            && inventory(&snapshots)? == after_hot,
        "repeated hot request changed output/files"
    );
    let old_scan = batch_snapshot::scan(&old_snapshot, 256, &never(), |_| Ok(()))?;
    ensure!(
        old_scan.version == before_version
            && old_scan.row_count == PREFIX as u64
            && old_scan.sha256 == old_snapshot.manifest.summary.sha256,
        "old immutable input changed"
    );
    let old_id = old_snapshot.manifest.id.clone();
    drop(old_snapshot);
    stop.send(())
        .map_err(|_| anyhow::anyhow!("fixture server already stopped"))?;
    tokio::time::timeout(Duration::from_secs(15), server).await???;
    store.close().await?;
    let oracle_root = root.join("oracle");
    fs::create_dir(&oracle_root)?;
    write_json(
        &oracle_root.join("input.json"),
        &json!({"config":config(root,&after_version),"request":request}),
    )?;
    child("--oracle", &oracle_root, None)?;
    let cold_oracle = read_json(&oracle_root.join("snapshot.json"))?;
    ensure!(
        cold_oracle == hot["snapshot"],
        "hot result differs from separate empty-process full cold oracle"
    );
    ensure!(
        checkpoint_after_second == files(&checkpoints)?
            && preserved_checkpoint == files(&baseline_cache.join("quant-results/checkpoints"))?
            && files(&snapshots)? == parquet_after,
        "oracle changed scenario/baseline cache"
    );
    let creation = creation(
        root,
        &after_version,
        json!({"baseline_creation_receipt":file_receipt(&root.parent().unwrap().join("baseline/creation-receipt.closed.json"))?,"mutation":mutation,"post_mutation_Store_issued_verification":post_mutation_verification,"same_server_instance":instance}),
    )?;
    write_json(&root.join("baseline-response.json"), &baseline)?;
    write_json(&root.join("first-refresh-response.json"), &hot)?;
    write_json(&root.join("second-refresh-response.json"), &warm)?;
    let expected_confirmed_count = PREFIX + if name == "append" { APPEND } else { 0 };
    let expected_delivered_rows = match name {
        "append" => APPEND,
        "earlycorrection" => PREFIX,
        _ => 0,
    };
    let expected_checkpoint_rewritten = matches!(name, "append" | "earlycorrection");
    let disk_consumer = json!({"config":config(root,&after_version),"input_receipts":{(creation["path"].as_str().unwrap()):creation["sha256"]},
        "expected_confirmed_count":expected_confirmed_count,"expected_delivered_rows":expected_delivered_rows,"expected_checkpoint_rewritten":expected_checkpoint_rewritten,
        "expected_new_parquet_bytes":number(&exported["exported_parquet_bytes"])?});
    let report = json!({"scenario":name,"data_kind":"controlled_synthetic","market_truth":false,"complete":true,"request":request,"mutation":mutation,"post_mutation_Store_issued_verification":post_mutation_verification,
        "process_id":std::process::id(),"same_server_instance":true,"instance_before":instance_before,"instance_after":instance_after,"actual_job_responses":{"before":before_job,"after":after_job},
        "profile":hot["profile"],"confirmed_count":number(&hot["snapshot"]["evidence"]["confirmed_count"])? ,"delivered_rows":number(&hot["profile"]["delivered_rows"])? ,"checkpoint_rewritten":hot["profile"]["checkpoint_rewritten"],"second_hot_nochange_profile":warm["profile"],
        "expected":{"confirmed_count":expected_confirmed_count,"delivered_rows":expected_delivered_rows,"checkpoint_rewritten":expected_checkpoint_rewritten,"exported_rows":expected_rows,"new_parquet_bytes":if name=="earlycorrection" {json!("positive actual file length; no invented exact byte constant")} else {json!(0)}},
        "parquet_before":parquet_before,"parquet_after":parquet_after,"previous_parquet_bytes_unchanged":true,"new_parquet_files":new_parquet_files,"new_parquet_bytes":number(&exported["exported_parquet_bytes"])? ,
        "checkpoint_before":checkpoint_before,"checkpoint_after":checkpoint_after,"checkpoint_after_second":checkpoint_after_second,"preserved_baseline_checkpoint":preserved_checkpoint,"preserved_baseline_cache":baseline_cache,
        "independent_full_equals_refresh":true,"refresh_canonical_result_sha256":digest(&hot["snapshot"])? ,"independent_full_canonical_result_sha256":digest(&cold_oracle)?,"exact_compared_projection":"entire returned QuantSnapshot including evidence/token, bars, series, confirmed, preview, external, quote and catalog; transport job_id/profile excluded",
        "independent_oracle_receipt":file_receipt(&oracle_root.join("receipt.json"))?,"disk_resume_consumer":disk_consumer,"creation_receipt":creation,
        "before_version":before_version,"after_version":after_version,"old_global_token":{"accepted":stale.is_ok(),"rejection":stale_error},"old_prefix_resume":tail,
        "cold_export":cold_export,"exact_plan_reuse_phases":phases.lock().unwrap().clone(),"hot_export":exported,
        "baseline_snapshot_id":old_id,"baseline_snapshot_sha256":digest(&baseline["snapshot"])? ,"business_before_sha256":digest(&business(&baseline["snapshot"]))?,"business_after_sha256":digest(&business(&hot["snapshot"]))?,
        "hot_snapshot_sha256":digest(&hot["snapshot"])? ,"cold_oracle_snapshot_sha256":digest(&cold_oracle)?,"same_version_full_cold_equals_hot":true,
        "baseline_response":file_receipt(&root.join("baseline-response.json"))?,"first_refresh_response":file_receipt(&root.join("first-refresh-response.json"))?,"second_refresh_response":file_receipt(&root.join("second-refresh-response.json"))?,"immutable_before_hot":before_hot,"immutable_after_hot":after_hot,"store_file":file_receipt(&directory.join("facts.redb"))?,"server_joined":true,"store_closed":true});
    write_json(&directory.join("receipt.json"), &report)?;
    Ok(report)
}

fn synthetic_position(sequence: u64) -> CapturePosition {
    CapturePosition {
        epoch: "controlled-semantic-calendar-position".into(),
        sequence,
        digest: format!("{sequence:064x}"),
    }
}
fn calendar_fact(at: &str, price: &str) -> Result<ImportBarRow> {
    let mut value = row(0, price.into(), 1);
    let open = at
        .parse::<DateTime<Utc>>()?
        .timestamp_nanos_opt()
        .context("calendar fixture ns")?;
    value.instrument_symbol = "AU2612".into();
    value.realtime_source_id = "tonghuashun_futures".into();
    value.evidence_channel_id = "tonghuashun_fuyao".into();
    value.open_time_ns = open;
    value.close_time_ns = open + MINUTE;
    value.finalized_at_ns = Some(open + MINUTE);
    value.source_observed_at_ns = open + MINUTE;
    value.received_at_ns = open + MINUTE;
    value.source_metadata = json!({"provider":"tonghuashun_futures","provider_symbol":"fuyao:65:au2612","raw_payload":{"synthetic":true}});
    Ok(value)
}
/// These extra semantic cases are separate from the four G06 disk-resume
/// fixtures. Calendar uses the existing public native projection API; none adds
/// a production write/debug endpoint or claims a validated real raw capture.
async fn semantic(root: &Path, name: &str) -> Result<Value> {
    ensure!(SEMANTIC_CASES.contains(&name), "unknown semantic fixture");
    let calendar = name == "calendar_only";
    let store = if calendar {
        Store::open(root.join("facts.redb"))?
    } else {
        Store::open(root.join("facts.redb"))?
            .read_generation(GENERATION)
            .await?
    };
    let mut request = fixed_request()?;
    if calendar {
        request.code = "AU2612".into();
        request.source_id = Some("tonghuashun_futures".into());
        request.period = "1d".into();
        request.decision_as_of = Some("2026-10-04T00:00:00Z".parse()?);
        store
            .commit_rows(
                synthetic_position(1),
                vec![
                    calendar_fact("2026-09-30T01:00:00Z", "100")?,
                    calendar_fact("2026-09-30T04:00:00Z", "120")?,
                ],
                vec![],
                vec![],
            )
            .await?;
    } else if name == "forming_to_final" {
        let mut forming = row(PREFIX, "125".into(), 1);
        forming.state = "forming".into();
        forming.finalized_at_ns = None;
        store
            .import_bars(batch(PREFIX as u64, vec![forming]))
            .await?;
    }
    let state = api::AppState {
        market: api::Market {
            catalog: Arc::new(catalog::Catalog::embedded()?),
            store: store.clone(),
        },
        research: api::Research,
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let base = format!("http://{}", listener.local_addr()?);
    let instance = json!({"process_id":std::process::id(),"instance_id":uuid::Uuid::new_v4().to_string(),"api_url":base,"acquisition_enabled":false});
    let instance_route = instance.clone();
    let app = analysis::service::router().with_state(state.clone()).route(
        "/fixture-instance",
        axum::routing::get(move || {
            let value = instance_route.clone();
            async move { axum::Json(value) }
        }),
    );
    let (stop, stopped) = tokio::sync::oneshot::channel::<()>();
    let server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async {
                let _ = stopped.await;
            })
            .await
    });
    let client = reqwest::Client::builder().no_proxy().build()?;
    let before_version = store.version().await?;
    let before = complete(&client, &base, &request).await?;
    let before_jobs = save_job_responses(&client, &base, &before, root, "before", None).await?;
    let snapshots = root.join("batch-snapshots");
    let parquet_before = files(&snapshots)?;
    let checkpoints = root.join("quant-results/checkpoints");
    let checkpoint_before = files(&checkpoints)?;
    let mutation = match name {
        "quote_only" => {
            let at = request
                .decision_as_of
                .unwrap()
                .timestamp_nanos_opt()
                .unwrap()
                - MINUTE;
            let quote = ImportQuoteRow {
                instrument_symbol: SYMBOL.into(),
                realtime_source_id: SOURCE.into(),
                evidence_channel_id: "jin10_local".into(),
                event_id: "controlled-quote-only-1".into(),
                price: "123.0000000000000000000000000001".into(),
                bid: None,
                ask: None,
                volume: None,
                observed_at_ns: at,
                received_at_ns: at,
                source_sequence: Some(1),
                source_metadata: json!({"provider":SOURCE,"provider_symbol":"XAUUSD.GOODS","raw_payload":{"synthetic":true}}),
                statistics: Value::Null,
                is_supplement: false,
                evidence: json!({"kind":"controlled_synthetic_quote"}),
            };
            let mut context = batch(0, vec![]).context;
            context.range_label = "semantic-quotes".into();
            json!({"public_api":"Store::import_quotes","receipt":store.import_quotes(ImportBatch{context,row_offset:0,rows:vec![quote]}).await?})
        }
        "forming_to_final" => {
            json!({"public_api":"Store::import_bars","receipt":store.import_bars(batch((PREFIX+1) as u64,vec![row(PREFIX,"125".into(),2)])).await?})
        }
        "source_volume_component" => {
            let minute = PREFIX - 2;
            let mut changed = row(minute, format!("{}.{}", 100 + minute % 11, minute % 10), 2);
            changed.source_metadata["raw_payload"]["source_volume_components"] = json!({"known_volume_sum":"2.0000000000000000000000000001","known_count":"1","total_count":"2","policy":"fuyao-minute-interval-samples-v1"});
            json!({"public_api":"Store::import_bars","quantity_policy_fixture":"same canonical minute count/NULL; independent exact source sample sum/count/policy from native/market_cases","receipt":store.import_bars(batch(PREFIX as u64,vec![changed])).await?})
        }
        "calendar_only" => {
            let values: Value =
                serde_json::from_str(include_str!("../assets/fuyao-calendar.json"))?;
            let authority: tracefang_core::periods::CalendarAuthority =
                serde_json::from_value(values["fuyao:65:au2612"].clone())?;
            let mut day = authority
                .absolute_days
                .first()
                .context("fixed calendar day")?
                .clone();
            ensure!(
                day.continuous_sessions.len() == 4,
                "existing calendar fixture changed"
            );
            day.continuous_sessions[3].start = "2026-09-30T04:00:00Z".parse()?;
            day.received_at_ns = "2026-10-03T23:00:00Z"
                .parse::<DateTime<Utc>>()?
                .timestamp_nanos_opt()
                .unwrap();
            day.accepted_at_ns = Some(day.received_at_ns + 1);
            day.capture_position = Some(synthetic_position(2));
            day.provenance = json!({"kind":"controlled_synthetic_exact_date_calendar_revision","basis_fixture":"backend/tests/calendar_authority.rs","market_truth":false});
            day.raw_body_sha256 = digest(&day.continuous_sessions)?;
            let calendar = tracefang_core::periods::CapturedSourceCalendar {
                source_id: "tonghuashun_futures".into(),
                symbol: "AU2612".into(),
                day,
            };
            let position = synthetic_position(2);
            json!({"public_api":"Store::commit_rows_with_decoder","synthetic_capture_position_not_real_raw_evidence":position,"calendar":calendar,"receipt":store.commit_rows_with_decoder(position,vec![],vec![],vec![],Some(json!({"schema":"calendar-projection-only-v1","calendar_authorities":[calendar]}))).await?})
        }
        _ => unreachable!(),
    };
    let after_version = store.version().await?;
    let after = complete(&client, &base, &request).await?;
    let after_jobs = save_job_responses(
        &client,
        &base,
        &after,
        root,
        "after",
        Some(
            before["chart_basis_hash"]
                .as_str()
                .context("semantic old basis")?,
        ),
    )
    .await?;
    let instance_after: Value = client
        .get(format!("{base}/fixture-instance"))
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    ensure!(
        instance_after == instance,
        "semantic mutation changed API instance"
    );
    if name == "quote_only" {
        ensure!(
            before["snapshot"]["bars"] == after["snapshot"]["bars"]
                && before["snapshot"]["series"] == after["snapshot"]["series"]
                && before["chart_basis_hash"] == after["chart_basis_hash"]
                && before["snapshot"]["quote"] != after["snapshot"]["quote"],
            "quote-only changed chart history or did not change quote"
        );
    } else {
        ensure!(
            before["chart_basis_hash"] != after["chart_basis_hash"],
            "semantic mutation retained old chart basis"
        );
        if name == "forming_to_final" {
            ensure!(
                number(&after["snapshot"]["evidence"]["confirmed_count"])?
                    == number(&before["snapshot"]["evidence"]["confirmed_count"])? + 1,
                "forming finality did not add one confirmed minute"
            );
        }
        if name == "calendar_only" {
            ensure!(
                before["snapshot"]["bars"] != after["snapshot"]["bars"],
                "calendar-only projection did not change its exact-date component membership"
            );
        }
        if name == "source_volume_component" {
            ensure!(
                after["snapshot"]["bars"]
                    .as_array()
                    .context("returned bars")?
                    .iter()
                    .any(|bar| bar["source_volume_components"]["policy"]
                        == "fuyao-minute-interval-samples-v1"),
                "source component evidence missing from actual returned bars"
            );
        }
    }
    let parquet_after = files(&snapshots)?;
    let checkpoint_after = files(&checkpoints)?;
    write_json(&root.join("before-response.json"), &before)?;
    write_json(&root.join("after-response.json"), &after)?;
    stop.send(())
        .map_err(|_| anyhow::anyhow!("semantic fixture stopped early"))?;
    tokio::time::timeout(Duration::from_secs(15), server).await???;
    store.close().await?;
    let oracle_root = root.join("oracle");
    fs::create_dir(&oracle_root)?;
    write_json(
        &oracle_root.join("input.json"),
        &json!({"config":config(root,&after_version),"request":request}),
    )?;
    child("--oracle", &oracle_root, None)?;
    let cold = read_json(&oracle_root.join("snapshot.json"))?;
    ensure!(
        cold == after["snapshot"],
        "semantic after result differs from independent empty oracle"
    );
    ensure!(
        parquet_after == files(&snapshots)? && checkpoint_after == files(&checkpoints)?,
        "semantic oracle changed API cache"
    );
    let report = json!({"complete":true,"data_kind":"controlled_synthetic","market_truth":false,"scenario":name,"request":request,"process_id":std::process::id(),"backend_build_fingerprint":replay::projector_build_hash(),"same_server_instance":true,"api_instance":instance,
        "before_version":before_version,"after_version":after_version,"mutation":mutation,"actual_job_responses":{"before":before_jobs,"after":after_jobs},"before_response":file_receipt(&root.join("before-response.json"))?,"after_response":file_receipt(&root.join("after-response.json"))?,
        "before_chart_basis":before["chart_basis_hash"],"after_chart_basis":after["chart_basis_hash"],"before_profile":before["profile"],"after_profile":after["profile"],"parquet_before":parquet_before,"parquet_after":parquet_after,"checkpoint_before":checkpoint_before,"checkpoint_after":checkpoint_after,
        "independent_full_equals_refresh":true,"refresh_canonical_result_sha256":digest(&after["snapshot"])? ,"independent_full_canonical_result_sha256":digest(&cold)?,"exact_compared_projection":"entire returned QuantSnapshot; transport job/profile excluded","independent_oracle_receipt":file_receipt(&oracle_root.join("receipt.json"))?,"store_closed":true,"server_joined":true,"actual_raw_capture_proof_claimed":false,"actual_facts_or_raw_opened":false,"production_modified":false});
    write_json(&root.join("receipt.json"), &report)?;
    Ok(report)
}

async fn lifetime(root: &Path) -> Result<Value> {
    let directory = root.join("lifetime");
    fs::create_dir(&directory)?;
    let store = Store::open(directory.join("facts.redb"))?
        .staging("synthetic-lifetime")
        .await?;
    store
        .import_bars(batch(0, vec![row(0, "1".into(), 1)]))
        .await?;
    let state = api::AppState {
        market: api::Market {
            catalog: Arc::new(catalog::Catalog::embedded()?),
            store: store.clone(),
        },
        research: api::Research,
    };
    let request = QuantInputRequest {
        code: "XAUUSD".into(),
        period: "1m".into(),
        decision_as_of: Some("2026-10-01T00:00:00Z".parse()?),
        ..Default::default()
    };
    let snapshots = directory.join("snapshots");
    let mut p = plan(&state, &request).await?;
    p.build.push_str(":audit");
    let audit = batch_snapshot::publish(&snapshots, &store, p.clone(), never(), silent()).await?;
    let audit_id = audit.manifest.id.clone();
    batch_snapshot::pin(&snapshots, &audit_id, "synthetic-audit-1")?;
    drop(audit);
    let pin_file = fs::read_dir(snapshots.join("pins"))?
        .next()
        .context("durable pin absent")??
        .path();
    let durable_pin = file_receipt(&pin_file)?;
    p.build.push_str(":released");
    let released =
        batch_snapshot::publish(&snapshots, &store, p.clone(), never(), silent()).await?;
    let released_id = released.manifest.id.clone();
    let conflict = batch_snapshot::pin(&snapshots, &released_id, "synthetic-audit-1")
        .err()
        .context("pin silently rebound")?
        .to_string();
    drop(released);
    p.build.push_str(":active");
    let active = batch_snapshot::publish(&snapshots, &store, p, never(), silent()).await?;
    let active_id = active.manifest.id.clone();
    let before = inventory(&snapshots)?;
    let protected_error = batch_snapshot::gc_to(&snapshots, 0, 0)
        .err()
        .context("GC ignored pinned/active bytes")?
        .to_string();
    let protected = inventory(&snapshots)?;
    ensure!(
        protected.get(&audit_id) == before.get(&audit_id)
            && protected.get(&active_id) == before.get(&active_id)
            && !protected.contains_key(&released_id)
            && file_receipt(&pin_file)? == durable_pin,
        "GC damaged protected files or retained released files"
    );
    let active_scan = batch_snapshot::scan(&active, 256, &never(), |_| Ok(()))?;
    ensure!(active_scan.row_count == 1, "active reader failed under GC");
    drop(active);
    batch_snapshot::unpin(&snapshots, "synthetic-audit-1")?;
    let reclaimed = batch_snapshot::gc_to(&snapshots, 0, 0)?;
    ensure!(
        inventory(&snapshots)?.is_empty() && !pin_file.exists(),
        "released snapshots/pin not reclaimed"
    );
    store.close().await?;
    let report = json!({"schema":"tracefang-synthetic-native-lifetime-v1","complete":true,"data_kind":"controlled_synthetic","market_truth":false,"process_id":std::process::id(),"backend_build_fingerprint":replay::projector_build_hash(),
        "snapshot_ids":{"durable_audit":audit_id,"active_reader":active_id,"unreferenced":released_id},"observed_after_protected_gc":{"audit_exists":protected.contains_key(&audit_id),"active_exists":protected.contains_key(&active_id),"unreferenced_exists":protected.contains_key(&released_id)},"observed_after_release_gc":{"audit_exists":snapshots.join(&audit_id).exists(),"active_exists":snapshots.join(&active_id).exists(),"unreferenced_exists":snapshots.join(&released_id).exists(),"pin_exists":pin_file.exists()},
        "durable_pin":durable_pin,"pin_conflict_rejection":conflict,"before":before,"protected_after_gc":protected,"protected_budget_rejection":protected_error,
        "active_reader_summary":active_scan,"released_reclaimed_while_protected":released_id,"final_gc":reclaimed,"all_unpinned_released_files_reclaimed":true,"store_closed":true});
    write_json(&root.join("native-lifetime-receipt.json"), &report)?;
    Ok(report)
}
async fn run_oracle(root: &Path) -> Result<()> {
    ensure!(
        !root.join("batch-snapshots").exists() && !root.join("quant-results").exists(),
        "oracle cache/checkpoint must start empty"
    );
    let input = read_json(&root.join("input.json"))?;
    let request: QuantInputRequest = serde_json::from_value(input["request"].clone())?;
    let facts = Path::new(
        input["config"]["store"]
            .as_str()
            .context("oracle store path")?,
    );
    let before = file_receipt(facts)?;
    let store = Store::open_read_only(facts)?
        .read_generation(
            input["config"]["rehearsal_generation"]
                .as_str()
                .context("oracle generation")?,
        )
        .await?;
    ensure!(
        serde_json::to_value(store.version().await?)? == input["config"]["snapshot_version"],
        "oracle version differs from refresh"
    );
    let state = api::AppState {
        market: api::Market {
            catalog: Arc::new(catalog::Catalog::embedded()?),
            store: store.clone(),
        },
        research: api::Research,
    };
    let snapshot = oracle(&state, &request).await?;
    store.close().await?;
    ensure!(
        before == file_receipt(facts)?,
        "RO oracle changed fixture facts"
    );
    write_json(&root.join("snapshot.json"), &snapshot)?;
    write_json(
        &root.join("receipt.json"),
        &json!({"complete":true,"data_kind":"controlled_synthetic","market_truth":false,"process_id":std::process::id(),"backend_build_fingerprint":replay::projector_build_hash(),"request":request,
        "config":input["config"],"empty_evaluator_checkpoint_and_immutable_cache":true,"snapshot_file":file_receipt(&root.join("snapshot.json"))?,"canonical_result_sha256":digest(&snapshot)?,"immutable_files":inventory(&root.join("batch-snapshots"))?,"store_identity_unchanged":true,"store_closed":true}),
    )?;
    Ok(())
}
async fn run(root: PathBuf) -> Result<()> {
    child("--baseline", &root, None)?;
    let mut baseline = read_json(&root.join("baseline-result.json"))?;
    let baseline_directory = root.join("baseline");
    baseline["creation_receipt"] = seal_closed_creation(&baseline_directory).await?;
    let mut scenarios = BTreeMap::new();
    for name in SCENARIOS {
        let directory = root.join(name);
        fs::create_dir(&directory)?;
        fs::create_dir(directory.join("data"))?;
        copy_verified(
            &baseline_directory.join("facts.redb"),
            &directory.join("facts.redb"),
        )?;
        copy_verified(
            &baseline_directory.join("capture.redb"),
            &directory.join("capture.redb"),
        )?;
        // Each child has empty service globals and the identical fixed request.
        // Its writer and API remain shared for cold, mutation and both refreshes.
        child("--scenario", &directory, Some(name))?;
        let mut case = read_json(&directory.join("receipt.json"))?;
        let creation = seal_closed_creation(&directory).await?;
        case["creation_receipt"] = creation.clone();
        case["disk_resume_consumer"]["input_receipts"] =
            json!({(creation["path"].as_str().context("creation path")?):creation["sha256"]});
        ensure!(
            case["complete"] == true
                && case["request"] == serde_json::to_value(fixed_request()?)?
                && case["before_version"] == baseline["version"],
            "scenario baseline/request differs"
        );
        scenarios.insert(name, case);
    }
    let semantic_root = root.join("semantic-variants");
    fs::create_dir(&semantic_root)?;
    let mut semantic_variants = BTreeMap::new();
    for name in SEMANTIC_CASES {
        let directory = semantic_root.join(name);
        fs::create_dir(&directory)?;
        fs::create_dir(directory.join("data"))?;
        if name != "calendar_only" {
            copy_verified(
                &baseline_directory.join("facts.redb"),
                &directory.join("facts.redb"),
            )?;
        }
        child("--semantic", &directory, Some(name))?;
        let mut case = read_json(&directory.join("receipt.json"))?;
        case["physical_receipt"] = seal_closed_semantic(&directory).await?;
        ensure!(case["complete"] == true, "semantic variant incomplete");
        semantic_variants.insert(name, case);
    }
    finish(root, baseline, scenarios, semantic_variants).await
}
async fn finalize(root: PathBuf) -> Result<()> {
    ensure!(
        !root.join("receipt.json").exists(),
        "completed driver receipt already exists"
    );
    let input = read_json(&root.join("continuation-input.json"))?;
    let baseline_creation = read_json(&provisional_creation_path(&root.join("baseline")))?;
    ensure!(
        baseline_creation["backend_build_fingerprint"] == replay::projector_build_hash(),
        "continuation baseline belongs to another core"
    );
    let baseline = json!({"config":baseline_creation["config"],"version":baseline_creation["snapshot_version"],"creation_receipt":seal_closed_creation(&root.join("baseline")).await?});
    let mut scenarios = BTreeMap::new();
    let mut semantic_variants = BTreeMap::new();
    for (kind, names) in [
        ("scenarios", SCENARIOS.as_slice()),
        ("semantic_variants", SEMANTIC_CASES.as_slice()),
    ] {
        for name in names {
            let binding = &input[kind][*name];
            let path = PathBuf::from(
                binding["receipt"]
                    .as_str()
                    .context("continued receipt path")?,
            )
            .canonicalize()?;
            ensure!(
                path.starts_with(&root)
                    && file_receipt(&path)?["sha256"] == binding["receipt_sha256"],
                "continued receipt changed or escaped scratch"
            );
            ensure!(
                binding["backend_build_fingerprint"] == replay::projector_build_hash(),
                "continued case core differs"
            );
            ensure!(
                binding["process_exited"] == true && binding["handles_closed"] == true,
                "continued case lacks process/handle closure proof"
            );
            let mut case = read_json(&path)?;
            ensure!(
                case["complete"] == true
                    && case["data_kind"] == "controlled_synthetic"
                    && case["market_truth"] == false,
                "continued case incomplete or identity differs"
            );
            case["executor_binding"] = binding.clone();
            if kind == "scenarios" {
                let creation =
                    seal_closed_creation(path.parent().context("case directory")?).await?;
                case["creation_receipt"] = creation.clone();
                ensure!(
                    case["scenario"] == *name
                        && case["before_version"] == baseline["version"]
                        && case["request"] == serde_json::to_value(fixed_request()?)?,
                    "continued scenario baseline/request differs"
                );
                case["disk_resume_consumer"]["input_receipts"] = json!({(creation["path"].as_str().context("creation path")?):creation["sha256"]});
                scenarios.insert(*name, case);
            } else {
                case["physical_receipt"] =
                    seal_closed_semantic(path.parent().context("case directory")?).await?;
                semantic_variants.insert(*name, case);
            }
        }
    }
    finish(root, baseline, scenarios, semantic_variants).await
}
async fn finish(
    root: PathBuf,
    baseline: Value,
    scenarios: BTreeMap<&str, Value>,
    semantic_variants: BTreeMap<&str, Value>,
) -> Result<()> {
    let lifetime = lifetime(&root).await?;
    let source_root = Path::new(env!("CARGO_MANIFEST_DIR"));
    let mut sources = BTreeMap::new();
    for path in [
        "examples/hot_refresh_acceptance.rs",
        "src/quant_input.rs",
        "src/analysis/service.rs",
        "src/batch_snapshot.rs",
    ] {
        sources.insert(path, file_receipt(&source_root.join(path))?);
    }
    let mut resume_scenarios = BTreeMap::new();
    for (name, case) in &scenarios {
        resume_scenarios.insert(*name, case["disk_resume_consumer"].clone());
    }
    let lifetime_reference = json!({"file":root.join("native-lifetime-receipt.json"),"sha256":file_receipt(&root.join("native-lifetime-receipt.json"))?["sha256"]});
    let report = json!({"schema":"tracefang-g06-hot-refresh-v1","complete":true,"data_kind":"controlled_synthetic","market_truth":false,
        "backend_build_fingerprint":replay::projector_build_hash(),"process_id":std::process::id(),"request":fixed_request()?,"executable":file_receipt(&std::env::current_exe()?)?,"sources_observed_at_execution":sources,
        "prefix_rows":PREFIX.to_string(),"append_rows":APPEND.to_string(),"writer_ownership":"one Store writer/API per isolated child; each mutation and its refreshes share that same running child API instance",
        "process_id_role":"driver; each scenarios.<name>.process_id identifies the actual writer/API child","same_request_all_scenarios":true,"identical_closed_baseline_copied":baseline,
        "verification_scope":"production quant_input + analysis/service router + batch_snapshot with minimal native-only fixture AppState; installed final server, actual market truth and real-data SLO remain separate gates",
        "scenarios":scenarios,"semantic_variants":semantic_variants,"native_lifetime":lifetime,"native_lifetime_receipt":lifetime_reference,"actual_facts_or_raw_opened":false,"production_modified":false});
    write_json(&root.join("receipt.json"), &report)?;
    let baseline_creation = baseline["creation_receipt"].clone();
    let mut consumer = baseline["config"].clone();
    consumer["input_receipts"] =
        json!({(baseline_creation["path"].as_str().unwrap()):baseline_creation["sha256"]});
    consumer["data_kind"] = json!("controlled_synthetic");
    consumer["market_truth"] = json!(false);
    consumer["note"] = json!(
        "Synthetic fixture fragment only; do not substitute for actual AU/long-prefix market performance configuration. Feed to packaged-server disk-resume fixture consumer."
    );
    consumer["page_code"] = json!("XAUUSD");
    consumer["quant_cases"] = json!([{"name":"controlled_synthetic_g06","expected_confirmed_count":PREFIX,"request":fixed_request()?,"resume_scenarios":resume_scenarios}]);
    consumer["native_lifetime_receipt"] = lifetime_reference;
    consumer["same_process_receipt"] = json!({"file":root.join("receipt.json"),"sha256":file_receipt(&root.join("receipt.json"))?["sha256"]});
    consumer["preserved_baseline_caches"] = json!(
        scenarios
            .values()
            .map(|case| case["preserved_baseline_cache"].clone())
            .collect::<Vec<_>>()
    );
    write_json(&root.join("disk-resume-config-fragment.json"), &consumer)?;
    println!(
        "{}",
        json!({"complete":true,"data_kind":"controlled_synthetic","report":root.join("receipt.json"),"report_sha256":file_receipt(&root.join("receipt.json"))?["sha256"],"native_lifetime_receipt":root.join("native-lifetime-receipt.json")})
    );
    Ok(())
}
fn main() -> Result<()> {
    let args = std::env::args_os().skip(1).collect::<Vec<_>>();
    let mode = args.first().and_then(|v| v.to_str()).unwrap_or("");
    let scenario_mode = mode == "--scenario";
    let semantic_mode = mode == "--semantic";
    let oracle_mode = mode == "--oracle";
    let baseline_mode = mode == "--baseline";
    let finalize_mode = mode == "--finalize";
    ensure!(
        args.len()
            == if scenario_mode || semantic_mode {
                3
            } else if oracle_mode || baseline_mode || finalize_mode {
                2
            } else {
                1
            },
        "expected NEW_NON_CLOUD_SCRATCH or internal --scenario/--oracle mode"
    );
    let root = PathBuf::from(
        &args[usize::from(
            scenario_mode || semantic_mode || oracle_mode || baseline_mode || finalize_mode,
        )],
    );
    let cache = PathBuf::from(std::env::var_os("HOME").context("user home")?)
        .join("Library/Caches/TraceFang/acceptance");
    fs::create_dir_all(&cache)?;
    let cache = cache.canonicalize()?;
    if !scenario_mode && !semantic_mode && !oracle_mode && !baseline_mode && !finalize_mode {
        ensure!(
            root.is_absolute()
                && !root.exists()
                && root.parent().context("scratch parent")?.canonicalize()? == cache
                && root
                    .file_name()
                    .and_then(|v| v.to_str())
                    .is_some_and(|v| v.starts_with("g06-")),
            "fixture scratch must be a new g06-* directory directly under {}",
            cache.display()
        );
        fs::create_dir(&root)?;
    }
    let root = root.canonicalize()?;
    ensure!(
        root.starts_with(&cache),
        "internal fixture mode outside non-cloud acceptance cache"
    );
    // Set isolated destinations before creating any runtime thread (Rust 2024).
    unsafe {
        std::env::set_var(
            "TRACEFANG_BATCH_SNAPSHOTS_DIR",
            root.join("batch-snapshots"),
        );
        std::env::set_var("TRACEFANG_QUANT_RESULTS_DIR", root.join("quant-results"));
        std::env::set_var("TRACEFANG_ACQUISITION_ENABLED", "0");
    }
    let runtime = tokio::runtime::Runtime::new()?;
    let result = if scenario_mode {
        runtime
            .block_on(scenario(&root, args[2].to_str().context("scenario name")?))
            .map(|_| ())
    } else if semantic_mode {
        runtime
            .block_on(semantic(&root, args[2].to_str().context("semantic name")?))
            .map(|_| ())
    } else if oracle_mode {
        runtime.block_on(run_oracle(&root))
    } else if baseline_mode {
        runtime.block_on(async {
            let baseline = prepare_baseline(&root).await?;
            write_json(&root.join("baseline-result.json"), &baseline)
        })
    } else if finalize_mode {
        runtime.block_on(finalize(root.clone()))
    } else {
        runtime.block_on(run(root.clone()))
    };
    if let Err(error) = result {
        let failure = json!({"complete":false,"data_kind":"controlled_synthetic","error":format!("{error:#}"),"production_modified":false});
        write_json(
            &root.join(if finalize_mode {
                "finalize-failure.json"
            } else {
                "failure.json"
            }),
            &failure,
        )?;
        return Err(error);
    }
    Ok(())
}
