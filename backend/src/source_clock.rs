//! Versioned source-label policy. This never changes evidence or knowledge clocks.
use chrono::{Duration, Timelike};
use chrono_tz::Asia::Shanghai;
use serde::{Deserialize, Serialize};
use crate::domain::{CoreError, CoreResult, Instrument, Timestamp};

pub const THS_V6_SHFE_END_V2: &str = "ths-v6-period61-shfe-interval-end-v2";
pub const LEGACY_V6_OPEN_V1: &str = "legacy-v6-label-as-open-v1";
pub const VERIFIED_V6_SCOPES: [(&str, &str); 4] = [
    ("qh_au2610", "AU2610"), ("qh_au8888", "AU8888"),
    ("qh_ag2706", "AG2706"), ("qh_ag8888", "AG8888"),
];

pub fn verified_v6_scope(provider_code: &str, instrument: &Instrument) -> bool {
    instrument.venue.as_deref() == Some("SHFE")
        && VERIFIED_V6_SCOPES.contains(&(provider_code, instrument.symbol.as_str()))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum V6Label {
    Regular { canonical_open: Timestamp, interval_end: Timestamp },
    SourcePoint { reason: &'static str },
}

/// Only the four reviewed period-61 scopes use this policy. Session-start labels
/// are independent points, never an invented minute ending outside the session.
pub fn classify_v6_label(provider_code: &str, instrument: &Instrument, label: Timestamp) -> CoreResult<V6Label> {
    if !verified_v6_scope(provider_code, instrument) {
        return Err(CoreError("v6 interval-end clock policy is not verified for this exact source scope".into()));
    }
    let local = label.with_timezone(&Shanghai);
    if local.second() != 0 || local.nanosecond() != 0 {
        return Err(CoreError("v6 minute source label is not minute aligned".into()));
    }
    let minute = local.hour() * 60 + local.minute();
    if [9*60, 10*60+30, 13*60+30, 21*60].contains(&minute) {
        return Ok(V6Label::SourcePoint { reason: "session_start_source_point_aggregation_unverified" });
    }
    // This describes label shape only; holiday/date membership is independently
    // checked by the versioned calendar. It does not reject raw holiday facts.
    if !(minute <= 2*60+30 || minute > 21*60
        || (minute > 9*60 && minute <= 10*60+15)
        || (minute > 10*60+30 && minute <= 11*60+30)
        || (minute > 13*60+30 && minute <= 15*60)) {
        return Ok(V6Label::SourcePoint { reason: "source_label_outside_reviewed_regular_interval_window" });
    }
    Ok(V6Label::Regular {
        canonical_open: label.checked_sub_signed(Duration::seconds(60))
            .ok_or_else(|| CoreError("v6 normalized interval start overflow".into()))?,
        interval_end: label,
    })
}

/// The complete original row stays in capture/archive. This bounded witness can
/// be retained separately by migration or referenced by a frame summary.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UnclassifiedSourcePoint {
    pub provider_code: String,
    pub source_label: String,
    #[serde(with = "crate::persistence_contract::i64_string")]
    pub source_label_ns: i64,
    #[serde(with = "crate::persistence_contract::u64_string")]
    pub source_row_index: u64,
    pub source_row_sha256: String,
    pub policy: String,
    pub reason: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::AssetClass;
    #[test]
    fn reviewed_scopes_keep_end_label_and_quarantine_opening_points() {
        for (code, symbol) in VERIFIED_V6_SCOPES {
            let instrument=Instrument{symbol:symbol.into(),asset_class:AssetClass::Future,venue:Some("SHFE".into()),base:None,quote:None};
            let label="2026-09-30T06:07:00Z".parse().unwrap();
            assert_eq!(classify_v6_label(code,&instrument,label).unwrap(),V6Label::Regular{canonical_open:"2026-09-30T06:06:00Z".parse().unwrap(),interval_end:label});
            for opening in ["2026-09-30T01:00:00Z","2026-09-30T02:30:00Z","2026-09-30T05:30:00Z","2026-09-29T13:00:00Z"] {
                assert!(matches!(classify_v6_label(code,&instrument,opening.parse().unwrap()).unwrap(),V6Label::SourcePoint{..}));
            }
            assert!(classify_v6_label("qh_other",&instrument,label).is_err());
        }
    }
}
