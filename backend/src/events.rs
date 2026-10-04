use crate::domain::{
    Candle, CoreResult, Decimal, Instrument, QuoteSnapshot, SourceMetadata, Timestamp,
    decimal_json, isoformat, isoformat_microseconds, optional_decimal_json, require,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BarState {
    #[serde(alias="forming")]
    ProvisionalQuote,
    #[serde(alias="provisional")]
    ProvisionalAuthoritative,
    Final,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BarFinalityPolicy {
    Explicit,
    NextAuthoritativeBar,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QuoteObservationKind {
    Event,
    Snapshot,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteEvent {
    pub source_id: String,
    pub channel_id: String,
    pub quote: QuoteSnapshot,
    pub sequence: Option<u64>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BarEvent {
    pub source_id: String,
    pub channel_id: String,
    pub candle: Candle,
    pub state: BarState,
    pub sequence: Option<u64>,
    pub finalized_at: Option<Timestamp>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", content = "value", rename_all = "snake_case")]
pub enum MarketEvent {
    Quote(QuoteEvent),
    Bar(BarEvent),
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteSample {
    pub source_id: String,
    pub channel_id: String,
    pub event_id: String,
    pub instrument: Instrument,
    pub provider_symbol: String,
    pub observed_at: Timestamp,
    pub received_at: Timestamp,
    #[serde(with = "decimal_json")]
    pub value: Decimal,
    pub observation_kind: QuoteObservationKind,
    pub storage_id: Option<i64>,
}
impl QuoteEvent {
    pub fn sample(&self) -> QuoteSample {
        QuoteSample {
            source_id: self.source_id.clone(),
            channel_id: self.channel_id.clone(),
            event_id: quote_event_id(&self.quote),
            instrument: self.quote.instrument.clone(),
            provider_symbol: self.quote.source.provider_symbol.clone(),
            observed_at: self.quote.source.observed_at,
            received_at: self.quote.source.received_at,
            value: self.quote.last.clone(),
            observation_kind: quote_observation_kind(&self.quote),
            storage_id: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RealtimeBar {
    pub instrument: Instrument,
    #[serde(rename = "interval", alias = "interval_seconds")]
    pub interval_seconds: i64,
    pub open_time: Timestamp,
    #[serde(with = "decimal_json")]
    pub open: Decimal,
    #[serde(with = "decimal_json")]
    pub high: Decimal,
    #[serde(with = "decimal_json")]
    pub low: Decimal,
    #[serde(with = "decimal_json")]
    pub close: Decimal,
    #[serde(default, with = "optional_decimal_json")]
    pub volume: Option<Decimal>,
    pub source: SourceMetadata,
    pub evidence_channel_id: String,
    pub state: BarState,
    #[serde(with = "crate::persistence_contract::u64_string")]
    pub revision: u64,
    pub finalized_at: Option<Timestamp>,
}
impl RealtimeBar {
    pub fn from_candle(
        candle: Candle,
        channel: String,
        state: BarState,
        revision: u64,
        finalized_at: Option<Timestamp>,
    ) -> Self {
        Self {
            instrument: candle.instrument,
            interval_seconds: candle.interval_seconds,
            open_time: candle.open_time,
            open: candle.open,
            high: candle.high,
            low: candle.low,
            close: candle.close,
            volume: candle.volume,
            source: candle.source,
            evidence_channel_id: channel,
            state,
            revision,
            finalized_at,
        }
    }
    pub fn candle(&self) -> Candle {
        Candle {
            instrument: self.instrument.clone(),
            interval_seconds: self.interval_seconds,
            open_time: self.open_time,
            open: self.open.clone(),
            high: self.high.clone(),
            low: self.low.clone(),
            close: self.close.clone(),
            volume: self.volume.clone(),
            source: self.source.clone(),
        }
    }
    pub fn validate(&self) -> CoreResult<()> {
        self.candle().validate()?;
        require(
            !self.evidence_channel_id.trim().is_empty(),
            "evidence_channel_id cannot be empty",
        )?;
        require(self.revision > 0, "revision must be positive")?;
        require(
            (self.state == BarState::Final) == self.finalized_at.is_some(),
            "only a final Bar must have finalized_at",
        )
    }
    /// Only canonical persistence adapters may hydrate final-history facts whose
    /// legacy tables did not record an actual finalization clock.
    pub fn validate_hydrated(&self)->CoreResult<()> {
        if self.state==BarState::Final && self.finalized_at.is_none() {
            let evidence=self.source.raw("canonical_legacy_finality");
            require(evidence.is_some_and(|v|v["semantics"]=="final_revision_history" && v["finalization_time_unknown"]==true && matches!(v["table"].as_str(),Some("candles"|"realtime_bars")) && v["fixed_snapshot"].is_object()),"unknown finalization clock requires controlled canonical legacy evidence")?;
            self.candle().validate()?;require(self.revision>0 && !self.evidence_channel_id.is_empty(),"invalid canonical legacy bar")
        }else{self.validate()}
    }
}

pub fn is_quote_supplement(quote: &QuoteSnapshot) -> bool {
    quote.source.raw("observation_kind").and_then(|v|v.as_str()) == Some("supplement")
}

pub fn quote_event_id(quote: &QuoteSnapshot) -> String {
    let source = &quote.source;
    let mut parts = vec![source.provider.clone(), source.provider_symbol.clone()];
    let canonical_capture_identity=match (
        source.raw("connection_id").and_then(|v| v.as_str()),
        source.raw("sequence").and_then(|v| v.as_u64().or_else(||v.as_str().and_then(|s|s.parse().ok()))),
    ) {
        (Some(connection), Some(sequence)) if !connection.is_empty() => {
            parts.extend(["transport".into(), connection.into(), sequence.to_string()]);
            false
        }
        _ => {
            parts.extend([
                "capture".into(),
                if source.observed_at.timestamp_subsec_nanos()%1000==0{isoformat_microseconds(source.observed_at)}else{isoformat(source.observed_at)},
                if source.received_at.timestamp_subsec_nanos()%1000==0{isoformat_microseconds(source.received_at)}else{isoformat(source.received_at)},
                quote.last.to_string(),
            ]);
            parts.extend(
                [
                    quote.open.clone(),
                    quote.high.clone(),
                    quote.low.clone(),
                    quote.volume.clone(),
                    quote.change.clone(),
                    quote.change_percent.clone(),
                ]
                .map(|v| v.map(|v| v.to_string()).unwrap_or_else(|| "None".into())),
            );
            true
        }
    };
    format!(
        "quote:{}{:x}",
        if canonical_capture_identity{"v2:"}else{""},
        Sha256::digest(parts.join("\u{1f}").as_bytes())
    )
}
pub fn quote_observation_kind(quote: &QuoteSnapshot) -> QuoteObservationKind {
    if quote
        .source
        .raw("observation_kind")
        .and_then(|v| v.as_str())
        == Some("snapshot")
    {
        QuoteObservationKind::Snapshot
    } else {
        QuoteObservationKind::Event
    }
}
