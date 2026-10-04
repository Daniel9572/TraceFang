//! One immutable authority input and evaluator contract for live, replay and simulation.
use super::exact::{D, decimal, optional_decimal, d, text, POLICY};
use anyhow::{Result, ensure};
use chrono::{DateTime, Utc};
use num_traits::Zero;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const SCHEMA_VERSION: &str = "tracefang-quant-snapshot-v1";
pub const CALCULATION_VERSION: &str = "tracefang-quant-decimal-v1-next-open";

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HistorySemantics { FinalRevisionHistory, OriginalEventReplay }
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotToken {
    pub store_epoch: String,
    #[serde(with = "u64_text")] pub commit_id: u64,
    pub capture_epoch: Option<String>,
    pub capture_digest: Option<String>,
    pub aggregation_version: String,
    pub schema_version: String,
    pub projector_version: String,
    #[serde(with = "u64_text")] pub committed_frame_seq: u64,
    pub committed_part: Option<u32>,
    pub catalog_version: String,
    pub schedule_version: String,
    pub route_version: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceVolumeComponents {
    #[serde(with = "decimal")] pub known_volume_sum: D,
    #[serde(with = "u64_text")] pub known_count: u64,
    #[serde(with = "u64_text")] pub total_count: u64,
    pub policy: String,
}
impl SourceVolumeComponents {
    pub fn validate(&self) -> Result<()> {
        ensure!(self.known_volume_sum >= D::zero(), "negative source component volume");
        ensure!(self.total_count > 0, "empty source component evidence must be absent");
        ensure!(self.known_count <= self.total_count, "invalid source component count");
        ensure!(self.known_count != 0 || self.known_volume_sum.is_zero(), "unknown source components cannot have a known sum");
        ensure!(!self.policy.trim().is_empty() && self.policy.len() <= 128, "source component policy missing or too long");
        Ok(())
    }
}
mod source_volume_groups {
    use super::SourceVolumeComponents;
    use serde::{Deserialize, Serialize, Serializer, Deserializer};
    pub fn serialize<S: Serializer>(groups: &[SourceVolumeComponents], serializer: S) -> Result<S::Ok, S::Error> {
        let mut sorted: Vec<_> = groups.iter().collect();
        sorted.sort_by(|a, b| a.policy.cmp(&b.policy));
        sorted.serialize(serializer)
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Vec<SourceVolumeComponents>, D::Error> {
        let mut groups = Vec::<SourceVolumeComponents>::deserialize(deserializer)?;
        groups.sort_by(|a, b| a.policy.cmp(&b.policy));
        Ok(groups)
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuantBar {
    pub open_time: DateTime<Utc>,
    pub bucket_end: DateTime<Utc>,
    #[serde(with = "decimal")] pub open: D,
    #[serde(with = "decimal")] pub high: D,
    #[serde(with = "decimal")] pub low: D,
    #[serde(with = "decimal")] pub close: D,
    #[serde(with = "optional_decimal")] pub volume: Option<D>,
    #[serde(with = "decimal")] pub known_volume_sum: D,
    #[serde(with = "u64_text")] pub known_volume_count: u64,
    #[serde(with = "u64_text")] pub component_count: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_volume_components: Option<SourceVolumeComponents>,
    #[serde(default, skip_serializing_if = "Vec::is_empty", with = "source_volume_groups")]
    pub source_volume_component_groups: Vec<SourceVolumeComponents>,
    pub state: String,
    #[serde(with = "u64_text")] pub revision: u64,
    pub observed_at: DateTime<Utc>,
    pub received_at: DateTime<Utc>,
    pub accepted_at: Option<DateTime<Utc>>,
    pub finalized_at: Option<DateTime<Utc>>,
    #[serde(with = "optional_u64_text")] pub applied_frame_seq: Option<u64>,
    #[serde(with = "optional_u64_text")] pub source_precision_ns: Option<u64>,
}
impl QuantBar {
    pub fn validate(&self) -> Result<()> {
        if let Some(components) = &self.source_volume_components { components.validate()?; }
        ensure!(self.source_volume_components.is_none() || self.source_volume_component_groups.is_empty(), "single and grouped source volume evidence are mutually exclusive");
        ensure!(self.source_volume_component_groups.len() <= 8, "too many source volume policies");
        let mut policies = BTreeSet::new();
        for group in &self.source_volume_component_groups {
            group.validate()?;
            ensure!(policies.insert(group.policy.as_str()), "duplicate source volume policy");
        }
        ensure!(self.bucket_end > self.open_time, "bar interval must be positive");
        ensure!(self.low <= self.high && self.open >= self.low && self.open <= self.high && self.close >= self.low && self.close <= self.high, "invalid OHLC ordering");
        ensure!(self.volume.as_ref().is_none_or(|v| v >= &D::zero()) && self.known_volume_sum >= D::zero(), "negative volume");
        ensure!(self.component_count > 0 && self.known_volume_count <= self.component_count, "invalid volume coverage");
        ensure!(self.volume.is_some() == (self.known_volume_count == self.component_count), "complete volume must be null unless every component is known");
        ensure!(self.volume.as_ref().is_none_or(|v| v == &self.known_volume_sum), "complete volume differs from known sum");
        ensure!(["final", "forming", "provisional"].contains(&self.state.as_str()), "unknown bar state");
        Ok(())
    }
    pub fn known_at(&self, semantics: &HistorySemantics) -> DateTime<Utc> {
        if *semantics == HistorySemantics::FinalRevisionHistory { self.bucket_end }
        else { [Some(self.bucket_end), Some(self.received_at), self.accepted_at, self.finalized_at].into_iter().flatten().max().unwrap() }
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuantQuote {
    #[serde(with = "decimal")] pub price: D,
    pub observed_at: DateTime<Utc>,
    pub received_at: DateTime<Utc>,
    pub accepted_at: Option<DateTime<Utc>>,
    #[serde(with = "optional_u64_text")] pub applied_frame_seq: Option<u64>,
    #[serde(with = "optional_u64_text")] pub source_precision_ns: Option<u64>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExternalFact {
    pub kind: String,
    pub source: String,
    #[serde(default)]pub record_id:Option<String>,
    #[serde(default,with="optional_u64_text")]pub revision:Option<u64>,
    #[serde(default)]pub provenance:Value,
    pub observed_at: Option<DateTime<Utc>>,
    pub published_at: Option<DateTime<Utc>>,
    pub received_at: Option<DateTime<Utc>>,
    pub unavailable_reason: Option<String>,
    pub value: Value,
}
impl ExternalFact {
    /// Same-view coverage is evidence about this input, not a timestamped market observation.
    pub fn is_structural_coverage(&self) -> bool {
        matches!(self.kind.as_str(), "calendar_coverage" | "source_point_coverage" | "source_clock_coverage")
    }
    pub fn included_in_evidence(&self, cutoff: DateTime<Utc>) -> bool {
        self.is_structural_coverage() || self.known_at().is_some_and(|at| at <= cutoff)
    }
    pub fn known_at(&self)->Option<DateTime<Utc>>{
        // A source observation alone does not prove availability. Unknown received time stays unavailable.
        let received=self.received_at?;
        Some([Some(received),self.observed_at,self.published_at].into_iter().flatten().max().unwrap())
    }
}
#[derive(Clone,Debug,Serialize,Deserialize,PartialEq,Eq)]
#[serde(deny_unknown_fields)]
pub struct SeriesVersion{pub series_generation:String,#[serde(with="u64_text")]pub correction_epoch:u64,pub append_watermark_ns:Option<String>,#[serde(with="u64_text")]pub last_mutation_commit_id:u64}
#[derive(Clone,Debug)]
pub struct ResumeCursor{pub after:DateTime<Utc>,pub series_version:SeriesVersion}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QuantInput {
    pub code: String,
    pub instrument: String,
    pub name: String,
    pub unit: String,
    pub executable_contract: bool,
    pub source_id: String,
    pub period: String,
    pub decision_as_of: DateTime<Utc>,
    #[serde(with = "optional_u64_text")] pub application_cursor: Option<u64>,
    pub token: SnapshotToken,
    pub semantics: HistorySemantics,
    #[serde(default)]pub derivation_id:Option<String>,
    pub capabilities: BTreeSet<String>,
    pub revision_start: Option<DateTime<Utc>>,
    pub revision_end: Option<DateTime<Utc>>,
    pub warmup_complete: bool,
    #[serde(default)]pub series_version:Option<SeriesVersion>,
    pub bars: Vec<QuantBar>,
    pub quote: Option<QuantQuote>,
    pub external_facts: Vec<ExternalFact>,
}
impl QuantInput {
    pub fn validate(&self) -> Result<()> {
        ensure!(!self.code.is_empty() && !self.source_id.is_empty() && !self.period.is_empty() && !self.token.store_epoch.is_empty(), "missing input identity");
        let mut previous = None;
        for bar in &self.bars {
            bar.validate()?;
            ensure!(previous.is_none_or(|time| bar.open_time > time), "bars must be unique in increasing interval order");
            previous = Some(bar.open_time);
            ensure!(bar.applied_frame_seq.is_none_or(|seq| seq <= self.token.committed_frame_seq), "bar exceeds committed watermark");
        }
        ensure!(self.quote.as_ref().is_none_or(|quote| quote.applied_frame_seq.is_none_or(|seq| seq <= self.token.committed_frame_seq)), "invalid quote watermark");
        ensure!(self.application_cursor.is_none_or(|seq| seq <= self.token.committed_frame_seq), "application cursor exceeds committed watermark");
        Ok(())
    }
    pub fn confirmed_bars(&self) -> Vec<QuantBar> {
        self.bars.iter().filter(|bar| bar.state == "final" && (self.semantics==HistorySemantics::FinalRevisionHistory||bar.finalized_at.is_some()) && bar.bucket_end <= self.decision_as_of
            && (self.semantics == HistorySemantics::FinalRevisionHistory || bar.known_at(&self.semantics) <= self.decision_as_of)
            && self.application_cursor.is_none_or(|seq| bar.applied_frame_seq.is_some_and(|applied| applied <= seq))).cloned().collect()
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct Parameters {
    pub enabled_strategies: BTreeSet<String>,
    pub macd_fast: usize, pub macd_slow: usize, pub macd_signal: usize,
    pub kdj_period: usize, pub kdj_dulling_bars: usize,
    #[serde(with = "decimal")] pub kdj_lower: D,
    #[serde(with = "decimal")] pub kdj_upper: D,
    pub rsi_period: usize,
    #[serde(with = "decimal")] pub rsi_lower: D,
    #[serde(with = "decimal")] pub rsi_upper: D,
    pub ma_periods: Vec<usize>, pub ma_slope_bars: usize,
    pub bollinger_period: usize, pub bollinger_rank_bars: usize,
    #[serde(with = "decimal")] pub bollinger_deviations: D,
    pub momentum_horizons: Vec<usize>,
    #[serde(with = "decimal_vec")] pub momentum_weights: Vec<D>,
    pub volatility_period: usize, pub atr_period: usize,
    pub slope_bars: usize,
    #[serde(with = "decimal")] pub slope_threshold: D,
    pub poc_bars: usize, pub poc_bins: usize, pub flow_bars: usize,
    #[serde(with = "decimal")] pub flow_threshold: D,
    pub volume_price_bars: usize, pub fvg_bars: usize,
    pub pattern: PatternParameters,
    #[serde(with = "decimal")] pub composite_threshold: D,
}
mod decimal_vec {
    use super::*;
    pub fn serialize<S: serde::Serializer>(values: &[D], serializer: S) -> std::result::Result<S::Ok, S::Error> { values.iter().map(text).collect::<Vec<_>>().serialize(serializer) }
    pub fn deserialize<'de, T: serde::Deserializer<'de>>(deserializer: T) -> std::result::Result<Vec<D>, T::Error> { Vec::<String>::deserialize(deserializer)?.into_iter().map(|value| super::super::exact::parse(&value).map_err(serde::de::Error::custom)).collect() }
}
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(default, deny_unknown_fields)]
pub struct PatternParameters {
    pub pivot_radius: usize, pub lookback_bars: usize,
    pub double_minimum_separation: usize, pub double_maximum_separation: usize, pub double_confirmation_bars: usize,
    #[serde(with = "decimal")] pub double_price_tolerance_percent: D,
    #[serde(with = "decimal")] pub double_price_tolerance_atr: D,
    #[serde(with = "decimal")] pub minimum_pattern_height_atr: D,
    #[serde(with = "decimal")] pub confirmation_buffer_atr: D,
    pub two_b_maximum_bars: usize, pub two_b_confirmation_bars: usize,
    #[serde(with = "decimal")] pub two_b_minimum_breach_atr: D,
    #[serde(with = "decimal")] pub two_b_maximum_breach_atr: D,
    #[serde(with = "decimal")] pub invalidation_buffer_atr: D,
}
impl Default for PatternParameters {
    fn default() -> Self { Self { pivot_radius: 2, lookback_bars: 160, double_minimum_separation: 5, double_maximum_separation: 80,
        double_confirmation_bars: 40, double_price_tolerance_percent: d("0.004"), double_price_tolerance_atr: d("0.5"), minimum_pattern_height_atr: d("1"),
        confirmation_buffer_atr: d("0.05"), two_b_maximum_bars: 30, two_b_confirmation_bars: 3, two_b_minimum_breach_atr: d("0.05"),
        two_b_maximum_breach_atr: d("1.25"), invalidation_buffer_atr: d("0.25") } }
}
impl Default for Parameters {
    fn default() -> Self { Self { enabled_strategies: ["structure", "ma-structure", "macd", "kdj", "rsi", "bollinger", "nine-count", "momentum-ensemble", "multi-timeframe", "auto-trend", "smart-money", "fair-value", "poc-proxy"].into_iter().map(str::to_owned).collect(),
        macd_fast: 12, macd_slow: 26, macd_signal: 9, kdj_period: 9, kdj_dulling_bars: 3, kdj_lower: d("20"), kdj_upper: d("80"),
        rsi_period: 14, rsi_lower: d("30"), rsi_upper: d("70"), ma_periods: vec![20,60,120,250], ma_slope_bars: 5,
        bollinger_period: 20, bollinger_rank_bars: 120, bollinger_deviations: d("2"), momentum_horizons: vec![20,60,120],
        momentum_weights: vec![d("0.5"),d("0.3"),d("0.2")], volatility_period: 20, atr_period: 14, slope_bars: 30,
        slope_threshold: d("0.004"), poc_bars: 240, poc_bins: 32, flow_bars: 32, flow_threshold: d("0.08"),
        volume_price_bars: 24, fvg_bars: 120, pattern: PatternParameters::default(), composite_threshold: d("0.2") } }
}
#[derive(Clone, Debug, Serialize)]
pub struct StrategyQualification { pub id: &'static str, pub composite_eligible: bool, pub backtest_eligible: bool, pub capability: &'static str }
pub fn strategy_catalog() -> Vec<StrategyQualification> {
    ["structure","ma-structure","macd","kdj","rsi","bollinger","nine-count","momentum-ensemble","multi-timeframe","auto-trend","smart-money","vix-gvz","volume-open-interest","fair-value","poc-proxy","order-flow-proxy","volume-price"].into_iter().map(|id| {
        let eligible = !["nine-count","multi-timeframe","auto-trend","smart-money","vix-gvz","volume-open-interest"].contains(&id);
        StrategyQualification { id, composite_eligible: eligible, backtest_eligible: eligible,
            capability: match id { "multi-timeframe" => "external_timeframes", "vix-gvz" => "external_volatility", "volume-open-interest" => "external_positioning", _ => "bars" } }
    }).collect()
}
impl Parameters {
    pub fn validate(&self) -> Result<()> {
        let catalog = strategy_catalog();
        ensure!(self.enabled_strategies.iter().all(|id| catalog.iter().any(|item| item.id == id)), "unknown strategy");
        for count in [self.macd_fast,self.macd_slow,self.macd_signal,self.kdj_period,self.kdj_dulling_bars,self.rsi_period,self.ma_slope_bars,self.bollinger_period,self.bollinger_rank_bars,self.volatility_period,self.atr_period,self.slope_bars,self.poc_bars,self.poc_bins,self.flow_bars,self.volume_price_bars,self.fvg_bars] { ensure!((2..=4096).contains(&count), "period/count must be 2–4096"); }
        ensure!(self.macd_fast < self.macd_slow, "MACD fast period must precede slow period");
        ensure!(self.rsi_lower > D::zero() && self.rsi_lower < self.rsi_upper && self.rsi_upper < d("100"), "RSI thresholds must satisfy 0 < lower < upper < 100");
        ensure!(self.kdj_lower > D::zero() && self.kdj_lower < self.kdj_upper && self.kdj_upper < d("100"), "invalid KDJ thresholds");
        ensure!(self.bollinger_deviations > D::zero() && self.bollinger_deviations <= d("10"), "invalid Bollinger deviation multiplier");
        ensure!((2..=8).contains(&self.ma_periods.len()) && self.ma_periods.windows(2).all(|pair| pair[0] < pair[1]) && self.ma_periods.iter().all(|n| (2..=4096).contains(n)), "MA periods must be 2–8 distinct ascending periods");
        ensure!(!self.momentum_horizons.is_empty() && self.momentum_horizons.len() <= 8 && self.momentum_horizons.len() == self.momentum_weights.len() && self.momentum_horizons.windows(2).all(|pair| pair[0] < pair[1]) && self.momentum_horizons.iter().all(|n| (2..=4096).contains(n)) && self.momentum_weights.iter().all(|weight| weight > &D::zero()), "invalid momentum horizons/weights");
        ensure!(self.composite_threshold > D::zero() && self.composite_threshold < d("1") && self.slope_threshold >= D::zero() && self.flow_threshold >= D::zero() && self.flow_threshold < d("1"), "invalid direction thresholds");
        let p = &self.pattern;
        for n in [p.pivot_radius,p.lookback_bars,p.double_minimum_separation,p.double_maximum_separation,p.double_confirmation_bars,p.two_b_maximum_bars,p.two_b_confirmation_bars] { ensure!((1..=4096).contains(&n), "invalid pattern count"); }
        ensure!(p.double_minimum_separation <= p.double_maximum_separation && p.pivot_radius*2 < p.lookback_bars && p.two_b_minimum_breach_atr <= p.two_b_maximum_breach_atr, "invalid pattern range");
        for value in [&p.double_price_tolerance_percent,&p.double_price_tolerance_atr,&p.minimum_pattern_height_atr,&p.confirmation_buffer_atr,&p.two_b_minimum_breach_atr,&p.two_b_maximum_breach_atr,&p.invalidation_buffer_atr] { ensure!(value >= &D::zero() && value <= &d("100"), "invalid pattern threshold"); }
        Ok(())
    }
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct QuantInputRequest {
    pub code: String, pub source_id: Option<String>, pub period: String,
    pub research_snapshot_id:Option<String>,pub research_adjustment:Option<String>,pub research_asset:Option<String>,
    pub start: Option<DateTime<Utc>>, pub end: Option<DateTime<Utc>>,
    pub decision_as_of: Option<DateTime<Utc>>,
    #[serde(with = "optional_u64_text")] pub application_cursor: Option<u64>,
    pub parameters: Parameters,
    #[serde(skip)]pub resume:Option<ResumeCursor>,
}

pub mod u64_text {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &u64, serializer: S) -> Result<S::Ok, S::Error> { serializer.serialize_str(&value.to_string()) }
    pub fn deserialize<'de, T: Deserializer<'de>>(deserializer: T) -> Result<u64, T::Error> { String::deserialize(deserializer)?.parse().map_err(serde::de::Error::custom) }
}
pub mod optional_u64_text {
    use serde::{Deserialize, Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &Option<u64>, serializer: S) -> Result<S::Ok, S::Error> { match value { Some(value) => serializer.serialize_some(&value.to_string()), None => serializer.serialize_none() } }
    pub fn deserialize<'de, T: Deserializer<'de>>(deserializer: T) -> Result<Option<u64>, T::Error> { Option::<String>::deserialize(deserializer)?.map(|value| value.parse().map_err(serde::de::Error::custom)).transpose() }
}

pub fn content_hash<T: Serialize>(value: &T) -> Result<String> {
    // BTreeMap/Set and struct declaration order form the canonical schema order.
    Ok(hex::encode(Sha256::digest(serde_json::to_vec(value)?)))
}
#[derive(Clone, Debug, Serialize)]
pub struct SnapshotEvidence {
    pub schema_version: &'static str, pub calculation_version: &'static str, pub rounding_policy: &'static str,
    pub input_hash: String, pub snapshot_hash: String, pub effective_input_hash:String, pub confirmed_prefix_hash:String, pub code: String, pub source_id: String, pub period: String,
    pub decision_as_of: DateTime<Utc>, pub token: SnapshotToken, pub semantics: HistorySemantics,
    pub input_count: usize, pub confirmed_count: usize, pub preview_count: usize,
    pub warmup_complete: bool, pub capabilities: BTreeSet<String>, pub parameters: Parameters,
}
/// Incremental canonical input digest; retained memory is independent of bar count.
#[derive(Clone,Serialize,Deserialize)]
pub struct PrefixHash{chain:[u8;32],seed_hash:String,count:usize,parameters:Parameters}
impl PrefixHash{
    pub fn new(input:&QuantInput,parameters:&Parameters)->Result<Self>{
        parameters.validate()?;
        let identity=serde_json::json!({"code":input.code,"instrument":input.instrument,"source_id":input.source_id,"period":input.period,"unit":input.unit,"executable_contract":input.executable_contract,"store_epoch":if input.semantics==HistorySemantics::FinalRevisionHistory{Some(&input.token.store_epoch)}else{None},"derivation_id":input.derivation_id,"capture_epoch":input.token.capture_epoch,"schema_version":input.token.schema_version,"aggregation_version":input.token.aggregation_version,"projector_version":input.token.projector_version,"catalog_version":input.token.catalog_version,"schedule_version":input.token.schedule_version,"route_version":input.token.route_version,"semantics":input.semantics,"warmup_complete":input.warmup_complete});
        let mut hasher=Sha256::new();hasher.update(serde_json::to_vec(&identity)?);hasher.update(b"\n");let chain:[u8;32]=hasher.finalize().into();let seed_hash=hex::encode(chain);Ok(Self{chain,seed_hash,count:0,parameters:parameters.clone()})
    }
    pub fn compatible_seed(&self,input:&QuantInput)->Result<bool>{Ok(self.seed_hash==Self::new(input,&self.parameters)?.seed_hash)}
    pub fn push(&mut self,bar:&QuantBar)->Result<()>{let mut hasher=Sha256::new();hasher.update(self.chain);hasher.update(serde_json::to_vec(bar)?);hasher.update(b"\n");self.chain=hasher.finalize().into();self.count+=1;Ok(())}
    pub fn finish(&self,input:&QuantInput,cutoff:DateTime<Utc>,cursor:Option<u64>)->Result<(String,String)>{
        let quote=input.quote.as_ref().filter(|q|q.observed_at<=cutoff&&q.received_at<=cutoff&&q.accepted_at.is_none_or(|v|v<=cutoff)&&cursor.is_none_or(|seq|q.applied_frame_seq.is_some_and(|s|s<=seq)));
        let external:Vec<_>=input.external_facts.iter().filter(|f|f.included_in_evidence(cutoff)).collect();
        let mut hasher=Sha256::new();hasher.update(self.chain);hasher.update(serde_json::to_vec(&(cutoff,cursor.map(|v|v.to_string()),quote,external,if input.semantics==HistorySemantics::OriginalEventReplay{input.token.capture_digest.as_ref()}else{None}))?);let input_hash=hex::encode(hasher.finalize());let snapshot_hash=content_hash(&(SCHEMA_VERSION,CALCULATION_VERSION,POLICY,&input_hash,&self.parameters))?;Ok((input_hash,snapshot_hash))
    }
    /// Automatic AI refresh identity excludes a read's wall clock and unrelated
    /// global commits. The auditable input/snapshot hashes above remain intact.
    /// A same-price new quote is a new source event and DOES change this key.
    pub fn effective_input_hash(&self,input:&QuantInput,preview_count:usize)->Result<String>{
        let cutoff=input.decision_as_of;let cursor=input.application_cursor;
        let quote=input.quote.as_ref().filter(|q|q.observed_at<=cutoff&&q.received_at<=cutoff&&q.accepted_at.is_none_or(|v|v<=cutoff)&&cursor.is_none_or(|seq|q.applied_frame_seq.is_some_and(|s|s<=seq)));
        let facts=input.external_facts.iter().filter(|f|f.included_in_evidence(cutoff)).map(|f|->Result<Value>{
            if f.kind!="multi_timeframe"&&!f.is_structural_coverage(){return Ok(serde_json::to_value(f)?);}
            let mut value=f.value.clone();if let Some(object)=value.as_object_mut(){object.remove("decision_cutoff_ns");object.remove("scope_start_ns");object.remove("scope_end_ns");}let mut provenance=f.provenance.clone();if let Some(object)=provenance.as_object_mut(){object.remove("snapshot_version");}
            Ok(serde_json::json!({"kind":f.kind,"source":f.source,"observed_at":f.observed_at,"published_at":f.published_at,"received_at":f.received_at,"value":value,"unavailable_reason":f.unavailable_reason,"derivation_provenance":provenance}))
        }).collect::<Result<Vec<_>>>()?;
        content_hash(&("effective-ai-input-v1",self.chain,CALCULATION_VERSION,POLICY,&self.parameters,quote,facts,preview_count,"unconfirmed preview excluded from confirmed indicators/AI price window"))
    }
    pub fn confirmed_prefix_hash(&self)->String{hex::encode(self.chain)}
    pub fn count(&self)->usize{self.count}
}
pub fn evidence(input: &QuantInput, parameters: &Parameters) -> Result<SnapshotEvidence> {
    input.validate()?; parameters.validate()?;let bars=input.confirmed_bars();let mut prefix=PrefixHash::new(input,parameters)?;for bar in &bars{prefix.push(bar)?;}
    let (input_hash,snapshot_hash)=prefix.finish(input,input.decision_as_of,input.application_cursor)?;
    let confirmed_count=bars.len();
    let effective_input_hash=prefix.effective_input_hash(input,input.bars.len()-confirmed_count)?;
    Ok(SnapshotEvidence { schema_version:SCHEMA_VERSION,calculation_version:CALCULATION_VERSION,rounding_policy:POLICY,
        input_hash,snapshot_hash,effective_input_hash,confirmed_prefix_hash:prefix.confirmed_prefix_hash(),code:input.code.clone(),source_id:input.source_id.clone(),period:input.period.clone(),decision_as_of:input.decision_as_of,
        token:input.token.clone(),semantics:input.semantics.clone(),input_count:input.bars.len(),confirmed_count,preview_count:input.bars.len()-confirmed_count,
        warmup_complete:input.warmup_complete,capabilities:input.capabilities.clone(),parameters:parameters.clone() })
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Signal {
    pub strategy_id: String, pub direction: String,
    #[serde(with = "decimal")] pub confidence: D,
    pub as_of: DateTime<Utc>, pub state: String, pub title: String,
    pub evidence: Vec<String>, pub composite_eligible: bool, pub backtest_eligible: bool,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct IndicatorPoint {
    pub as_of: DateTime<Utc>, pub decision_at: DateTime<Utc>,
    pub indicators: BTreeMap<String, Value>, pub signals: Vec<Signal>,
    #[serde(with = "decimal")] pub composite_score: D,
    pub direction: i8,
}
