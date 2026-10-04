//! Shared persistence boundaries. A legacy cursor never becomes a native capture position.
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub const SCHEMA_VERSION: &str = "tracefang-native-v1";
pub const PROJECTOR_VERSION: &str = "tracefang-projector-v3-source-clock";
pub const AGGREGATION_VERSION: &str = "tracefang-range-index-v5-source-volume-coverage";

pub mod u64_string {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<u64, D::Error> {
        let value = serde_json::Value::deserialize(deserializer)?;
        match value {
            serde_json::Value::String(v) => v.parse().map_err(serde::de::Error::custom),
            serde_json::Value::Number(v) => v
                .as_u64()
                .ok_or_else(|| serde::de::Error::custom("invalid u64")),
            _ => Err(serde::de::Error::custom("expected integer string")),
        }
    }
}
pub mod optional_u64_string {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &Option<u64>, serializer: S) -> Result<S::Ok, S::Error> {
        match value {
            Some(v) => serializer.serialize_some(&v.to_string()),
            None => serializer.serialize_none(),
        }
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<u64>, D::Error> {
        Option::<serde_json::Value>::deserialize(deserializer)?
            .map(|v| super::u64_string::deserialize(v).map_err(serde::de::Error::custom))
            .transpose()
    }
}
pub mod i64_string {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &i64, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<i64, D::Error> {
        let v = serde_json::Value::deserialize(deserializer)?;
        match v {
            serde_json::Value::String(v) => v.parse().map_err(serde::de::Error::custom),
            serde_json::Value::Number(v) => v
                .as_i64()
                .ok_or_else(|| serde::de::Error::custom("invalid i64")),
            _ => Err(serde::de::Error::custom("expected signed integer string")),
        }
    }
}
pub mod optional_i64_string {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &Option<i64>, serializer: S) -> Result<S::Ok, S::Error> {
        match value {
            Some(v) => serializer.serialize_some(&v.to_string()),
            None => serializer.serialize_none(),
        }
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<i64>, D::Error> {
        Option::<serde_json::Value>::deserialize(deserializer)?
            .map(|v| super::i64_string::deserialize(v).map_err(serde::de::Error::custom))
            .transpose()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct CapturePosition {
    pub epoch: String,
    #[serde(with = "u64_string")]
    pub sequence: u64,
    pub digest: String,
}
/// A verified legacy authority baseline is not a native projection checkpoint.
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ProjectionStartBoundary {
    pub kind:String,
    pub schema_version:String,
    pub production_terminal:bool,
    pub authority_manifest_id:String,
    pub authority_manifest_sha256:String,
    pub postgres_source_fingerprint:String,
    pub postgres_snapshot:String,
    pub raw_tail:CapturePosition,
    pub legacy_stream:String,
    pub legacy_epoch:String,
    #[serde(with="u64_string")]pub legacy_tail_sequence:u64,
    pub legacy_mapping_sha256:String,
    pub conflict_policy_version:String,
    pub staging_generation:String,
    pub verified_fact_sha256:String,
    pub verified_index_sha256:String,
    pub closure:LegacyClosureEvidence,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct LegacyClosureEvidence {
    pub stopped_component_ids:Vec<String>,
    pub stop_report_sha256:String,
    pub projection_drain_report_sha256:String,
    #[serde(with="u64_string")]pub unresolved_frames:u64,
    #[serde(with="optional_u64_string")]pub raw_applied_through_legacy:Option<u64>,
    pub reconciliation_report_sha256:Option<String>,
    pub stable_tail_observations:Vec<LegacyTailObservation>,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct LegacyTailObservation {
    pub stream:String,pub epoch:String,
    #[serde(with="u64_string")]pub last_sequence:u64,
    #[serde(with="i64_string")]pub observed_at_ns:i64,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DurableReceipt {
    pub position: CapturePosition,
    #[serde(with = "i64_string")]
    pub received_at_ns: i64,
    #[serde(with = "i64_string")]
    pub accepted_at_ns: i64,
    #[serde(with = "i64_string")]
    pub confirmed_at_ns: i64,
    pub duplicate: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct CaptureGap {
    #[serde(with = "u64_string")]
    pub first_sequence: u64,
    #[serde(with = "u64_string")]
    pub last_sequence: u64,
    pub reason: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CaptureBounds {
    pub epoch: String,
    #[serde(with = "optional_u64_string")]
    pub first_sequence: Option<u64>,
    #[serde(with = "optional_u64_string")]
    pub last_sequence: Option<u64>,
    #[serde(with = "optional_i64_string")]
    pub first_received_at_ns: Option<i64>,
    #[serde(with = "optional_i64_string")]
    pub last_received_at_ns: Option<i64>,
    #[serde(with = "optional_i64_string")]
    pub first_accepted_at_ns: Option<i64>,
    #[serde(with = "optional_i64_string")]
    pub first_durable_at_ns: Option<i64>,
    #[serde(with = "optional_i64_string")]
    pub last_accepted_at_ns: Option<i64>,
    #[serde(with = "optional_i64_string")]
    pub last_durable_at_ns: Option<i64>,
    pub gaps: Vec<CaptureGap>,
    pub retention_policy: String,
}
#[derive(Clone, Debug, Serialize, Deserialize,PartialEq,Eq)]
pub struct SnapshotVersion {
    pub store_epoch: String,
    #[serde(with = "u64_string")]
    pub commit_id: u64,
    pub committed_capture: Option<CapturePosition>,
    pub schema_version: String,
    pub aggregation_version: String,
    pub projector_version: String,
    pub catalog_version: String,
    pub schedule_version: String,
    pub route_version: String,
    pub active_generation: String,
}
/// Savepoints belong exclusively to a regenerable replay database. The version
/// binds its fact prefix to the immutable captured-frame identity.
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ReplaySavepoint {
    #[serde(with="u64_string")]pub savepoint_id:u64,
    pub version:SnapshotVersion,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ReplayRestoreReceipt {
    pub version:SnapshotVersion,
    #[serde(with="u64_vec_string")]pub invalidated_later_ids:Vec<u64>,
}
mod u64_vec_string {
    use serde::{Deserialize,Deserializer,Serialize,Serializer};
    pub fn serialize<S:Serializer>(value:&[u64],serializer:S)->Result<S::Ok,S::Error>{value.iter().map(u64::to_string).collect::<Vec<_>>().serialize(serializer)}
    pub fn deserialize<'de,D:Deserializer<'de>>(deserializer:D)->Result<Vec<u64>,D::Error>{Vec::<String>::deserialize(deserializer)?.into_iter().map(|v|v.parse().map_err(serde::de::Error::custom)).collect()}
}

/// Every price/quantity lexeme is normalized exact decimal text. No f64 adapter.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportBarRow {
    pub instrument_symbol: String,
    pub realtime_source_id: String,
    pub evidence_channel_id: String,
    pub interval_seconds: u32,
    #[serde(with = "i64_string")]
    pub open_time_ns: i64,
    #[serde(with = "i64_string")]
    pub close_time_ns: i64,
    pub open: String,
    pub high: String,
    pub low: String,
    pub close: String,
    pub volume: Option<String>,
    #[serde(with = "u64_string")]
    pub revision: u64,
    #[serde(with = "optional_u64_string")]
    pub received_sequence: Option<u64>,
    pub state: String,
    #[serde(with = "optional_i64_string")]
    pub finalized_at_ns: Option<i64>,
    #[serde(with = "i64_string")]
    pub source_observed_at_ns: i64,
    #[serde(with = "i64_string")]
    pub received_at_ns: i64,
    pub source_metadata: Value,
    pub evidence: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportQuoteRow {
    pub instrument_symbol: String,
    pub realtime_source_id: String,
    pub evidence_channel_id: String,
    pub event_id: String,
    pub price: String,
    pub bid: Option<String>,
    pub ask: Option<String>,
    pub volume: Option<String>,
    #[serde(with = "i64_string")]
    pub observed_at_ns: i64,
    #[serde(with = "i64_string")]
    pub received_at_ns: i64,
    #[serde(with = "optional_u64_string")]
    pub source_sequence: Option<u64>,
    pub source_metadata: Value,
    pub statistics: Value,
    pub is_supplement: bool,
    pub evidence: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportContext {
    pub origin_id: String,
    pub source_fingerprint: String,
    pub schema_version: String,
    pub range_label: String,
    /// Opaque old namespaced watermark. Never used as native capture sequence.
    pub legacy_cursor: Option<Value>,
    pub expected_sha256: Option<String>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportBatch<T> {
    pub context: ImportContext,
    #[serde(with = "u64_string")]
    pub row_offset: u64,
    pub rows: Vec<T>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportReceipt {
    pub version: SnapshotVersion,
    #[serde(with = "u64_string")]
    pub accepted: u64,
    #[serde(with = "u64_string")]
    pub unchanged: u64,
    #[serde(with = "u64_string")]
    pub rejected: u64,
    pub origin_id: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ImportMetadataRow {
    pub namespace: String,
    pub key: String,
    pub value: Value,
    pub evidence: Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum BarSelection {
    Latest {
        count: usize,
    },
    Before {
        #[serde(with = "i64_string")]
        before_ns: i64,
        count: usize,
    },
    Range {
        #[serde(with = "i64_string")]
        start_ns: i64,
        #[serde(with = "i64_string")]
        end_ns: i64,
        max_rows: usize,
    },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalSnapshotRequest {
    pub symbol: String,
    pub source_id: String,
    pub period: String,
    pub selection: BarSelection,
    pub final_only: bool,
    pub expected_version: Option<SnapshotVersion>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalSnapshot {
    pub version: SnapshotVersion,
    /// Canonical domain JSON with decimal/u64/ns fields as exact strings.
    pub bars: Vec<Value>,
    pub quote: Option<Value>,
    pub capabilities: Value,
    pub coverage: Value,
    pub semantics: String,
    /// Request diagnostics are transient and never enter canonical input hashes.
    #[serde(skip)]pub page_timings:Option<CanonicalPageTimings>,
}
#[derive(Clone,Debug,Default)]
pub struct CanonicalPageTimings {
    pub calendar_query_ms:f64,
    pub calendar_coverage_ms:f64,
    pub context_ms:f64,
    pub canonical_dto_ms:f64,
    pub read_view_ms:f64,
    pub store_ms:f64,
    pub page_dto_ms:f64,
    pub buckets:usize,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProjectionCommit {
    pub position: CapturePosition,
    pub quotes: Vec<Value>,
    pub bars: Vec<Value>,
    pub errors: Vec<Value>,
    #[serde(default)]pub decoder_state:Option<Value>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProjectionReceipt {
    pub version: SnapshotVersion,
    /// Canonical results from the committing transaction, never a later read view.
    pub hot_series: Vec<CanonicalHotSeries>,
    pub changed_bars: Vec<Value>,
    pub changed_bars_complete: bool,
    pub latest_quotes: Vec<Value>,
    pub series_changes:Vec<CanonicalSeriesChange>,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct CanonicalSeriesChange {
    pub source_id:String,pub symbol:String,pub interval_seconds:u32,
    #[serde(with="i64_string")]pub start_ns:i64,
    #[serde(with="i64_string")]pub end_ns:i64,
    pub historical_correction:bool,pub series_version:Value,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalHotSeries {
    pub symbol: String,
    pub source_id: String,
    pub interval_seconds: u32,
    pub bars: Vec<Value>,
}

/// Full range scan keeps one MVCC read view, and bounded typed batches.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalScanRequest {
    pub symbol: String,
    pub source_id: String,
    pub interval_seconds: u32,
    #[serde(with = "i64_string")]
    pub start_ns: i64,
    #[serde(with = "i64_string")]
    pub end_ns: i64,
    pub final_only: bool,
    pub expected_version: Option<SnapshotVersion>,
}
#[derive(Clone, Debug)]
pub struct CanonicalScanBatch {
    pub version: SnapshotVersion,
    /// Present only on first batch, including an empty range. Same read view.
    pub context: Option<CanonicalScanContext>,
    pub row_offset: u64,
    pub rows: Vec<ImportBarRow>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalScanSummary {
    pub version: SnapshotVersion,
    #[serde(with = "u64_string")]
    pub row_count: u64,
    pub sha256: String,
    pub complete: bool,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct CanonicalScanContext {
    pub quote: Option<Value>,
    pub capabilities: Value,
    pub coverage: Value,
    pub semantics: String,
    pub external_facts:Vec<ExternalFactRecord>,
}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ExternalFactScope {pub instrument_symbol:String,pub market_source_id:Option<String>}
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ExternalFactRecord {
    pub scope:ExternalFactScope,pub kind:String,pub source:String,pub record_id:String,
    #[serde(with="u64_string")]pub revision:u64,
    #[serde(with="optional_i64_string")]pub observed_at_ns:Option<i64>,
    #[serde(with="optional_i64_string")]pub published_at_ns:Option<i64>,
    #[serde(with="optional_i64_string")]pub received_at_ns:Option<i64>,
    pub value:Value,pub unavailable_reason:Option<String>,pub provenance:Value,
}
