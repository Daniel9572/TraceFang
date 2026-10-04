//! Independent retained-prefix selection. PG is an authority comparison only;
//! the replay reducer starts empty and never receives PG facts as seed state.
use anyhow::{Result, ensure};
use serde_json::{Value, json};
use tracefang_core::{
    domain::Decimal,
    persistence_contract::{ImportBarRow, ImportQuoteRow},
};
#[derive(Debug)]
pub enum BarChoice {
    Preserve(&'static str),
    Replace {
        row: ImportBarRow,
        reason: &'static str,
    },
    Unresolved(&'static str),
}
fn sha(value: &Value) -> bool {
    value
        .as_str()
        .is_some_and(|v| v.len() == 64 && v.bytes().all(|b| b.is_ascii_hexdigit()))
}
fn source_reference(value: &Value) -> bool {
    value["table"] == "candles"
        && sha(&value["sha256"])
        && value["file"].as_str().is_some_and(|v| !v.is_empty())
        && value["row_offset"]
            .as_str()
            .is_some_and(|v| v.parse::<u64>().is_ok())
}
/// Shape checks here only recognize lineages that the importer/global decoder
/// already resolved against the complete checksummed input. An arbitrary raw
/// `origin`/`history_file` string cannot promote a quote into source authority.
pub fn authority(row: &ImportBarRow) -> u8 {
    let raw = &row.source_metadata["raw_payload"];
    if raw["derivation"] == "quote_event" {
        return 1;
    }
    let fixed = &row.evidence["fixed_snapshot"];
    let selection = &row.evidence["canonical_selection"];
    let fixed_history = sha(&fixed["source_fingerprint"])
        && sha(&fixed["table_sha256"])
        && fixed["snapshot"].as_str().is_some_and(|v| !v.is_empty())
        && selection["source_records"]
            .as_array()
            .is_some_and(|v| v.iter().any(source_reference));
    let clock = &row.evidence["source_clock_projection"];
    let projected_history = fixed_history
        && clock["policy_id"] == tracefang_core::source_clock::THS_V6_SHFE_END_V2
        && source_reference(&clock["original_source_record"])
        && sha(&clock["source_row_sha256"]);
    if projected_history
        || fixed_history
            && (raw["derivation"] == "authoritative_history"
                || selection["chosen_table"] == "candles"
                || row.evidence_channel_id == "jin10_local")
    {
        return 2;
    }
    let captured = raw["capture_epoch"].as_str().is_some_and(|v| !v.is_empty())
        && raw["capture_sequence"]
            .as_str()
            .is_some_and(|v| v.parse::<u64>().is_ok_and(|v| v > 0))
        && sha(&raw["capture_digest"]);
    if captured && raw["derivation"] == "authoritative_history" {
        let provider = row.source_metadata["provider_symbol"]
            .as_str()
            .unwrap_or("");
        let reviewed = tracefang_core::source_clock::VERIFIED_V6_SCOPES
            .contains(&(provider, row.instrument_symbol.as_str()));
        let input = &raw["authoritative_input"];
        let pos = &input["capture_position"];
        let source_position = pos["epoch"] == raw["capture_epoch"]
            && sha(&pos["digest"])
            && pos["sequence"]
                .as_str()
                .and_then(|v| v.parse::<u64>().ok())
                .is_some_and(|v| {
                    v > 0
                        && v <= raw["capture_sequence"]
                            .as_str()
                            .and_then(|v| v.parse().ok())
                            .unwrap_or(0)
                });
        let v6_input = source_position
            && input["protocol"] == "tonghuashun_public_line_v6"
            && input["provider_code"] == provider
            && input["period"] == "61"
            && matches!(
                input["response_kind"].as_str(),
                Some("minute_year" | "minute_last")
            )
            && input["file"].as_str().is_some_and(|v| !v.is_empty())
            && sha(&input["body_sha256"]);
        if row.realtime_source_id == "tonghuashun_futures"
            && row.evidence_channel_id == "tonghuashun_futures"
            && reviewed
            && v6_input
            && row.interval_seconds == 60
            && raw["channel"] == "tonghuashun_public_line_v6"
            && raw["protocol"] == "tonghuashun_public_line_v6"
            && raw["source_period"] == "61"
            && raw["minute_clock_policy"] == tracefang_core::source_clock::THS_V6_SHFE_END_V2
            && raw["source_interval_end_ns"]
                .as_str()
                .and_then(|v| v.parse::<i64>().ok())
                == Some(row.close_time_ns)
        {
            return 2;
        }
        if row.realtime_source_id == "jin10_client" && row.evidence_channel_id == "jin10_local" {
            return 2;
        }
        if row.realtime_source_id == "tonghuashun_futures"
            && raw["protocol"] == "tonghuashun_fuyao_v1"
            && raw["minute_clock_policy"] == "fuyao-interval-end-v1"
            && sha(&raw["reference_sha256"])
        {
            return 2;
        }
    }
    0
}
fn semantic(row: &ImportBarRow) -> Result<Value> {
    let decimal = |value: &str| Decimal::from_str_exact(value).map(|v| v.to_string());
    let volume = row
        .volume
        .as_deref()
        .map(Decimal::from_str_exact)
        .transpose()?;
    let source_components = tracefang_core::source_volume::read(
        &row.source_metadata["raw_payload"],
        volume.clone().unwrap_or(Decimal::ZERO),
        u64::from(volume.is_some()),
        1,
    )?;
    Ok(
        json!({"key":[&row.realtime_source_id,&row.instrument_symbol,row.interval_seconds,row.open_time_ns.to_string()],"end":row.close_time_ns.to_string(),"ohlc":[decimal(&row.open)?,decimal(&row.high)?,decimal(&row.low)?,decimal(&row.close)?],"volume":row.volume.as_deref().map(decimal).transpose()?,"source_volume_components":source_components,"state":if matches!(row.state.as_str(),"forming"|"provisional_quote"){"provisional_quote"}else{&row.state}}),
    )
}
/// Only a genuinely later fact with sufficient source authority may replace a
/// PG fact. Ambiguous same-clock differences remain unresolved and block closure.
pub fn choose_bar(pg: Option<&ImportBarRow>, raw: &ImportBarRow) -> Result<BarChoice> {
    if raw.state == "final" && !raw.finalized_at_ns.is_some_and(|v| v >= raw.close_time_ns) {
        return Ok(BarChoice::Unresolved(
            "retained_finality_has_no_valid_confirmation",
        ));
    }
    let Some(pg) = pg else {
        return Ok(if authority(raw) == 0 {
            BarChoice::Unresolved("missing_retained_fact_has_unproved_source_authority")
        } else {
            BarChoice::Replace {
                row: raw.clone(),
                reason: "missing_retained_prefix_fact",
            }
        });
    };
    ensure!(
        pg.instrument_symbol == raw.instrument_symbol
            && pg.realtime_source_id == raw.realtime_source_id
            && pg.interval_seconds == raw.interval_seconds
            && pg.open_time_ns == raw.open_time_ns,
        "comparison crosses a logical source key"
    );
    if semantic(pg)? == semantic(raw)? {
        return Ok(BarChoice::Preserve(
            "same_fact_preserve_original_pg_lineage",
        ));
    }
    if pg.state == "final" && raw.state != "final" {
        return Ok(BarChoice::Preserve(
            "retained_preview_cannot_downgrade_pg_final",
        ));
    }
    if authority(pg) == 0 || authority(raw) == 0 {
        return Ok(BarChoice::Unresolved(
            "changed_fact_has_unproved_source_authority",
        ));
    }
    if authority(pg) > authority(raw) {
        return Ok(BarChoice::Preserve("higher_authority_pg_fact_preserved"));
    }
    if raw.received_at_ns < pg.received_at_ns {
        return Ok(BarChoice::Preserve(
            "older_retained_arrival_preserve_later_pg_fact",
        ));
    }
    if raw.received_at_ns == pg.received_at_ns {
        return Ok(BarChoice::Unresolved(
            "different_values_at_unproven_same_arrival_clock",
        ));
    }
    if raw.source_observed_at_ns < pg.source_observed_at_ns {
        return Ok(BarChoice::Unresolved(
            "received_clock_alone_cannot_override_observed_clock_regression",
        ));
    }
    let mut row = raw.clone();
    row.revision = row.revision.max(
        pg.revision
            .checked_add(1)
            .ok_or_else(|| anyhow::anyhow!("canonical revision exhausted"))?,
    );
    Ok(BarChoice::Replace {
        row,
        reason: "later_retained_fact_with_equal_or_higher_source_authority",
    })
}
pub fn semantic_fields(row: &ImportBarRow) -> Result<Value> {
    semantic(row)
}
#[derive(Debug)]
pub enum QuoteChoice {
    Preserve(&'static str),
    Insert {
        row: ImportQuoteRow,
        reason: &'static str,
    },
    Unresolved(&'static str),
}
/// Compare the upstream event contents. Local import/capture evidence is kept
/// with the selected row and checked separately after close/reopen; it cannot
/// create a second event or authorize a price/clock rewrite under one identity.
pub fn quote_semantic_fields(row: &ImportQuoteRow) -> Result<Value> {
    let decimal = |value: &str| Decimal::from_str_exact(value).map(|v| v.to_string());
    let mut statistics = row.statistics.clone();
    if let Some(values) = statistics.as_object_mut() {
        for value in values.values_mut() {
            if !value.is_null() {
                let exact = match value {
                    Value::String(v) => v.clone(),
                    Value::Number(v) => v.to_string(),
                    _ => anyhow::bail!("quote statistic is not exact scalar"),
                };
                *value = json!(decimal(&exact)?);
            }
        }
    }
    Ok(
        json!({"key":[row.realtime_source_id,row.instrument_symbol,row.event_id],"channel":row.evidence_channel_id,"price":decimal(&row.price)?,"bid":row.bid.as_deref().map(decimal).transpose()?,"ask":row.ask.as_deref().map(decimal).transpose()?,"volume":row.volume.as_deref().map(decimal).transpose()?,"observed_at_ns":row.observed_at_ns.to_string(),"received_at_ns":row.received_at_ns.to_string(),"source_sequence":row.source_sequence.map(|v|v.to_string()),"statistics":statistics,"is_supplement":row.is_supplement,"provider_symbol":row.source_metadata["provider_symbol"]}),
    )
}
pub fn choose_quote(pg: Option<&ImportQuoteRow>, raw: &ImportQuoteRow) -> Result<QuoteChoice> {
    let Some(pg) = pg else {
        let provenance = &raw.source_metadata["raw_payload"];
        let checked = provenance["capture_epoch"]
            .as_str()
            .is_some_and(|v| !v.is_empty())
            && provenance["capture_sequence"]
                .as_str()
                .and_then(|v| v.parse::<u64>().ok())
                .is_some_and(|v| v > 0)
            && sha(&provenance["capture_digest"]);
        return Ok(if raw.is_supplement {
            QuoteChoice::Unresolved("supplement_cannot_create_missing_price_event")
        } else if checked {
            QuoteChoice::Insert {
                row: raw.clone(),
                reason: "missing_retained_quote_identity",
            }
        } else {
            QuoteChoice::Unresolved("missing_quote_has_unproved_capture_lineage")
        });
    };
    ensure!(
        pg.instrument_symbol == raw.instrument_symbol
            && pg.realtime_source_id == raw.realtime_source_id
            && pg.event_id == raw.event_id,
        "quote comparison crosses an event identity"
    );
    if quote_semantic_fields(pg)? == quote_semantic_fields(raw)? {
        return Ok(QuoteChoice::Preserve(
            "same_quote_event_preserve_original_pg_lineage",
        ));
    }
    Ok(QuoteChoice::Unresolved(
        "same_quote_identity_has_different_source_contents_or_clocks",
    ))
}
/// Classification uses the original retained envelope, not an error substring
/// supplied by a caller. These rejected optional/HTTP responses produce no price.
pub fn classify_rejection(
    channel: &str,
    envelope: &Value,
    diagnostic: &str,
) -> Option<&'static str> {
    if !channel.starts_with("tonghuashun_") || envelope["provider_code"].as_str().is_none() {
        return None;
    }
    if envelope["status_code"] == 502 && diagnostic == "captured provider HTTP response failed" {
        return Some("provider_http_502_no_price");
    }
    if envelope["status_code"] == 200
        && envelope["kind"] == "daily_last"
        && matches!(
            envelope["protocol"].as_str(),
            Some("tonghuashun_public_line_v6")
        )
        && !diagnostic.is_empty()
    {
        return Some("optional_daily_statistics_rejected_no_price");
    }
    None
}
#[cfg(test)]
mod tests {
    use super::*;
    fn row(state: &str, price: &str, received: i64) -> ImportBarRow {
        ImportBarRow {
            instrument_symbol: "XAU/USD".into(),
            realtime_source_id: "jin10_client".into(),
            evidence_channel_id: "jin10_web".into(),
            interval_seconds: 60,
            open_time_ns: 0,
            close_time_ns: 60_000_000_000,
            open: price.into(),
            high: price.into(),
            low: price.into(),
            close: price.into(),
            volume: None,
            revision: 7,
            received_sequence: None,
            state: state.into(),
            finalized_at_ns: (state == "final").then_some(60_000_000_000),
            source_observed_at_ns: 60_000_000_000,
            received_at_ns: received,
            source_metadata: json!({"raw_payload":{"derivation":"quote_event"}}),
            evidence: json!({}),
        }
    }
    fn verified_history(row: &mut ImportBarRow) {
        row.source_metadata["raw_payload"] = json!({"derivation":"authoritative_history"});
        row.evidence = json!({"fixed_snapshot":{"source_fingerprint":"a".repeat(64),"table_sha256":"b".repeat(64),"snapshot":"fixed-transaction"},"canonical_selection":{"chosen_table":"realtime_bars","source_records":[{"table":"candles","file":"original-candles.ndjson","sha256":"c".repeat(64),"row_offset":"7"}]}});
    }
    #[test]
    fn actual_realtime_authoritative_history_without_raw_origin_cannot_be_overwritten_by_later_quote_final()
     {
        let mut pg = row("final", "2", 100);
        verified_history(&mut pg);
        assert_eq!(authority(&pg), 2);
        let raw = row("final", "3", 200);
        assert!(matches!(
            choose_bar(Some(&pg), &raw).unwrap(),
            BarChoice::Preserve("higher_authority_pg_fact_preserved")
        ));
    }
    #[test]
    fn arbitrary_raw_history_strings_do_not_promote_an_unproved_fact() {
        let mut pg = row("final", "2", 100);
        pg.source_metadata["raw_payload"] =
            json!({"origin":"authoritative_history","history_file":"invented"});
        assert_eq!(authority(&pg), 0);
        let raw = row("final", "3", 200);
        assert!(matches!(
            choose_bar(Some(&pg), &raw).unwrap(),
            BarChoice::Unresolved("changed_fact_has_unproved_source_authority")
        ));
    }
    #[test]
    fn partial_source_quantity_changes_are_not_hidden_by_null_volume() {
        let pg = row("provisional_quote", "2", 100);
        let mut raw = row("provisional_quote", "2", 200);
        raw.source_metadata["raw_payload"]["source_volume_components"] = json!({"known_volume_sum":"2","known_count":"1","total_count":"2","policy":tracefang_core::source_volume::FUYAO_INTERVAL});
        assert_ne!(
            semantic_fields(&pg).unwrap(),
            semantic_fields(&raw).unwrap()
        );
        assert!(matches!(
            choose_bar(Some(&pg), &raw).unwrap(),
            BarChoice::Replace { .. }
        ));
        raw.source_metadata["raw_payload"]["source_volume_components"]["known_volume_sum"] =
            json!("0");
        assert_ne!(
            semantic_fields(&pg).unwrap(),
            semantic_fields(&raw).unwrap()
        );
    }
    #[test]
    fn old_preview_cannot_replace_final_authority() {
        let pg = row("final", "2", 100);
        let raw = row("provisional_quote", "3", 200);
        assert!(matches!(
            choose_bar(Some(&pg), &raw).unwrap(),
            BarChoice::Preserve("retained_preview_cannot_downgrade_pg_final")
        ));
    }
    #[test]
    fn missing_and_later_corrected_fact_are_repaired_with_higher_revision() {
        let pg = row("provisional_quote", "1", 100);
        let raw = row("final", "2", 200);
        assert!(matches!(
            choose_bar(None, &raw).unwrap(),
            BarChoice::Replace { .. }
        ));
        let BarChoice::Replace { row, .. } = choose_bar(Some(&pg), &raw).unwrap() else {
            panic!("new final ignored")
        };
        assert_eq!(row.revision, 8);
        assert_eq!(row.close, "2");
    }
    #[test]
    fn same_clock_ambiguity_blocks_and_history_authority_beats_quote_final() {
        let mut pg = row("final", "2", 100);
        let raw = row("final", "3", 100);
        assert!(matches!(
            choose_bar(Some(&pg), &raw).unwrap(),
            BarChoice::Unresolved(_)
        ));
        pg.evidence_channel_id = "jin10_local".into();
        verified_history(&mut pg);
        let later = row("final", "3", 200);
        assert!(matches!(
            choose_bar(Some(&pg), &later).unwrap(),
            BarChoice::Preserve("higher_authority_pg_fact_preserved")
        ));
    }
    fn quote() -> ImportQuoteRow {
        ImportQuoteRow {
            instrument_symbol: "XAU/USD".into(),
            realtime_source_id: "jin10_client".into(),
            evidence_channel_id: "jin10_web".into(),
            event_id: "wide-clock-event".into(),
            price: "12345678901234567890.123456789".into(),
            bid: None,
            ask: Some("12345678901234567890.123456790".into()),
            volume: None,
            observed_at_ns: 1_800_000_000_123_456_789,
            received_at_ns: 1_800_000_000_223_456_789,
            source_sequence: Some(u64::MAX),
            source_metadata: json!({"provider_symbol":"XAUUSD.GOODS","raw_payload":{"capture_epoch":"raw","capture_sequence":"1","capture_digest":"a".repeat(64)}}),
            statistics: json!({"open":null,"change":"-0.000000001"}),
            is_supplement: false,
            evidence: json!({}),
        }
    }
    #[test]
    fn missing_quote_needs_real_capture_and_supplement_cannot_invent_event() {
        let mut raw = quote();
        assert!(matches!(
            choose_quote(None, &raw).unwrap(),
            QuoteChoice::Insert { .. }
        ));
        raw.is_supplement = true;
        assert!(matches!(
            choose_quote(None, &raw).unwrap(),
            QuoteChoice::Unresolved(_)
        ));
        raw.is_supplement = false;
        raw.source_metadata["raw_payload"]["capture_digest"] = Value::Null;
        assert!(matches!(
            choose_quote(None, &raw).unwrap(),
            QuoteChoice::Unresolved(_)
        ));
    }
    #[test]
    fn quote_null_wide_decimal_ns_sequence_and_statistics_are_exact() {
        let pg = quote();
        let mut raw = pg.clone();
        raw.evidence = json!({"different_local_lineage":true});
        assert!(matches!(
            choose_quote(Some(&pg), &raw).unwrap(),
            QuoteChoice::Preserve(_)
        ));
        raw.received_at_ns += 1;
        assert!(matches!(
            choose_quote(Some(&pg), &raw).unwrap(),
            QuoteChoice::Unresolved(_)
        ));
        raw = pg.clone();
        raw.volume = Some("0".into());
        assert!(matches!(
            choose_quote(Some(&pg), &raw).unwrap(),
            QuoteChoice::Unresolved(_)
        ));
        raw = pg.clone();
        raw.statistics["change"] = json!("-0.000000002");
        assert!(matches!(
            choose_quote(Some(&pg), &raw).unwrap(),
            QuoteChoice::Unresolved(_)
        ));
    }
    #[test]
    fn only_original_known_rejected_protocol_responses_are_classified() {
        let failed =
            json!({"provider_code":"au","status_code":502,"protocol":"tonghuashun_public_line_v6"});
        assert_eq!(
            classify_rejection(
                "tonghuashun_futures",
                &failed,
                "captured provider HTTP response failed"
            ),
            Some("provider_http_502_no_price")
        );
        assert_eq!(
            classify_rejection(
                "jin10_web",
                &failed,
                "captured provider HTTP response failed"
            ),
            None
        );
        assert_eq!(
            classify_rejection("tonghuashun_futures", &failed, "another decode error"),
            None
        );
        let optional = json!({"provider_code":"au","status_code":200,"kind":"daily_last","protocol":"tonghuashun_public_line_v6"});
        assert_eq!(
            classify_rejection(
                "tonghuashun_futures",
                &optional,
                "invalid optional statistics"
            ),
            Some("optional_daily_statistics_rejected_no_price")
        );
        let mut price = optional.clone();
        price["kind"] = json!("time");
        assert_eq!(
            classify_rejection("tonghuashun_futures", &price, "bad price"),
            None
        );
    }
}
