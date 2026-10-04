//! Exact prices and source timestamps shared by every market-data boundary.
use chrono::{DateTime, SecondsFormat, Utc};
pub use crate::exact::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::Value;

pub type Timestamp = DateTime<Utc>;

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{0}")]
pub struct CoreError(pub String);
pub type CoreResult<T> = Result<T, CoreError>;
pub(crate) fn require(ok: bool, message: &str) -> CoreResult<()> {
    if ok {
        Ok(())
    } else {
        Err(CoreError(message.into()))
    }
}

/// JSON numbers are parsed as their decimal text, never through binary floating point.
pub mod decimal_json {
    use super::*;
    use serde::{Deserializer, Serializer};
    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        let value = Value::deserialize(deserializer)?;
        let text = match value {
            Value::String(text) => text,
            Value::Number(number) => number.to_string(),
            _ => {
                return Err(serde::de::Error::custom(
                    "price must be a decimal number or string",
                ));
            }
        };
        Decimal::from_str_exact(&text)
            .or_else(|_| Decimal::from_scientific(&text))
            .map_err(serde::de::Error::custom)
    }
}
pub mod optional_decimal_json {
    use super::*;
    use serde::{Deserializer, Serializer};
    pub fn serialize<S: Serializer>(
        value: &Option<Decimal>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        match value {
            Some(value) => decimal_json::serialize(value, serializer),
            None => serializer.serialize_none(),
        }
    }
    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Decimal>, D::Error> {
        let value = Option::<Value>::deserialize(deserializer)?;
        value
            .map(|value| decimal_json::deserialize(value).map_err(serde::de::Error::custom))
            .transpose()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AssetClass {
    Spot,
    Future,
    Forex,
    Equity,
    Index,
    Energy,
    Metal,
    Crypto,
    Unknown,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct Instrument {
    pub symbol: String,
    pub asset_class: AssetClass,
    pub base: Option<String>,
    pub quote: Option<String>,
    pub venue: Option<String>,
}
impl Instrument {
    pub fn validate(&self) -> CoreResult<()> {
        require(!self.symbol.trim().is_empty(), "symbol cannot be empty")
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SourceMetadata {
    pub provider: String,
    pub provider_symbol: String,
    pub observed_at: Timestamp,
    pub received_at: Timestamp,
    #[serde(default)]
    pub raw_payload: Option<Value>,
}
impl SourceMetadata {
    /// Capture order is comparable only inside the same immutable capture epoch.
    pub fn application_cmp(&self,other:&Self)->Option<std::cmp::Ordering> {
        let epoch=self.raw("capture_epoch")?.as_str()?;
        if other.raw("capture_epoch")?.as_str()?!=epoch {return None;}
        let sequence=|v:&Value|v.as_u64().or_else(||v.as_str()?.parse().ok());
        Some(sequence(self.raw("capture_sequence")?)?.cmp(&sequence(other.raw("capture_sequence")?)?))
    }
    pub fn is_fresh(&self, now: Timestamp, max_age_seconds: i64) -> bool {
        let precision = self
            .raw_payload
            .as_ref()
            .and_then(|raw| raw.get("timestamp_precision_seconds"))
            .and_then(Value::as_i64)
            .unwrap_or(0)
            .clamp(0, 60);
        let age = now.signed_duration_since(self.observed_at);
        age >= chrono::Duration::seconds(-max_age_seconds)
            && age <= chrono::Duration::seconds(max_age_seconds + precision)
            && now.signed_duration_since(self.received_at)
                <= chrono::Duration::seconds(max_age_seconds)
    }
    pub fn raw(&self, key: &str) -> Option<&Value> {
        self.raw_payload.as_ref()?.get(key)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteSnapshot {
    pub instrument: Instrument,
    #[serde(with = "decimal_json")]
    pub last: Decimal,
    #[serde(default, with = "optional_decimal_json")]
    pub open: Option<Decimal>,
    #[serde(default, with = "optional_decimal_json")]
    pub high: Option<Decimal>,
    #[serde(default, with = "optional_decimal_json")]
    pub low: Option<Decimal>,
    #[serde(default, with = "optional_decimal_json")]
    pub volume: Option<Decimal>,
    #[serde(default, with = "optional_decimal_json")]
    pub change: Option<Decimal>,
    #[serde(default, with = "optional_decimal_json")]
    pub change_percent: Option<Decimal>,
    pub source: SourceMetadata,
}
impl QuoteSnapshot {
    pub fn validate(&self) -> CoreResult<()> {
        self.instrument.validate()?;
        if let (Some(low), Some(high)) = (self.low.as_ref(), self.high.as_ref()) {
            require(low <= high, "low cannot be greater than high")?;
            require(
                low <= &self.last && &self.last <= high,
                "last must be within low and high",
            )?;
        }
        require(
            self.volume.as_ref().is_none_or(|v| v >= &Decimal::ZERO),
            "volume cannot be negative",
        )
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Candle {
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
}
impl Candle {
    pub fn validate(&self) -> CoreResult<()> {
        self.instrument.validate()?;
        require(self.interval_seconds > 0, "interval must be positive")?;
        require(self.low <= self.high, "low cannot be greater than high")?;
        require(
            self.low <= self.open && self.open <= self.high,
            "open must be within low and high",
        )?;
        require(
            self.low <= self.close && self.close <= self.high,
            "close must be within low and high",
        )?;
        require(
            self.volume.as_ref().is_none_or(|v| v >= &Decimal::ZERO),
            "volume cannot be negative",
        )
    }
}

/// Python's UTC isoformat spelling is part of existing event IDs and metadata.
pub fn isoformat(at: Timestamp) -> String {
    let format = if at.timestamp_subsec_nanos() % 1000 != 0 {
        SecondsFormat::Nanos
    } else if at.timestamp_subsec_micros() == 0 {
        SecondsFormat::Secs
    } else {
        SecondsFormat::Micros
    };
    at.to_rfc3339_opts(format, false)
}
pub fn isoformat_microseconds(at: Timestamp) -> String {
    at.to_rfc3339_opts(SecondsFormat::Micros, false)
}
