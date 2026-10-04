//! Source-bound canonical Bar reducer. No I/O: live capture and replay call the same code.
use crate::domain::{
    Candle, CoreError, CoreResult, Instrument, QuoteSnapshot, SourceMetadata, Timestamp, require,
};
use crate::events::{BarEvent, BarFinalityPolicy, BarState, MarketEvent, QuoteEvent, RealtimeBar};
use chrono::{Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BarContract {
    pub source_id: String,
    pub authoritative_bar_channel_id: String,
    pub quote_channel_ids: Vec<String>,
    pub interval_seconds: i64,
    pub quote_projection_intervals: Vec<i64>,
    pub finality_policy: BarFinalityPolicy,
    pub quote_max_age_seconds: i64,
}
impl BarContract {
    pub fn new(
        source: impl Into<String>,
        authority: impl Into<String>,
        quotes: Vec<String>,
    ) -> Self {
        Self {
            source_id: source.into(),
            authoritative_bar_channel_id: authority.into(),
            quote_channel_ids: quotes,
            interval_seconds: 60,
            quote_projection_intervals: vec![1, 60],
            finality_policy: BarFinalityPolicy::NextAuthoritativeBar,
            quote_max_age_seconds: 60,
        }
    }
    pub fn validate(&self) -> CoreResult<()> {
        require(
            !self.source_id.trim().is_empty()
                && !self.authoritative_bar_channel_id.trim().is_empty(),
            "source and authoritative channel cannot be empty",
        )?;
        require(
            self.quote_max_age_seconds > 0 && self.interval_seconds > 0,
            "interval and quote maximum age must be positive",
        )?;
        require(
            !self.quote_channel_ids.is_empty()
                && self.quote_channel_ids.iter().all(|v| !v.trim().is_empty()),
            "quote channels cannot be empty",
        )?;
        require(
            self.quote_channel_ids.iter().collect::<BTreeSet<_>>().len()
                == self.quote_channel_ids.len(),
            "quote channels must be unique",
        )?;
        require(
            self.quote_projection_intervals.iter().all(|v| *v > 0)
                && self
                    .quote_projection_intervals
                    .contains(&self.interval_seconds),
            "authoritative interval must be quote-projected",
        )?;
        require(
            self.quote_projection_intervals
                .iter()
                .collect::<BTreeSet<_>>()
                .len()
                == self.quote_projection_intervals.len(),
            "projection intervals must be unique",
        )
    }
}
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
pub struct SeriesKey {
    pub source_id: String,
    pub instrument: Instrument,
    pub interval_seconds: i64,
}
impl SeriesKey {
    pub fn from_bar(bar: &RealtimeBar) -> Self {
        Self {
            source_id: bar.source.provider.clone(),
            instrument: bar.instrument.clone(),
            interval_seconds: bar.interval_seconds,
        }
    }
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SeriesState {
    pub realtime_source_id: String,
    pub instrument_symbol: String,
    pub upstream_channel_id: String,
    pub provider_symbol: String,
    #[serde(rename = "interval", alias = "interval_seconds")]
    pub interval_seconds: i64,
    pub latest_authoritative_open_time: Option<Timestamp>,
    pub authoritative_through: Timestamp,
    pub history_floor: Option<Timestamp>,
    pub tail_checked_through: Option<Timestamp>,
    pub tail_checked_at: Option<Timestamp>,
    pub evidence_version: String,
    pub updated_at: Timestamp,
}
#[derive(Debug, Default, Clone, Serialize, Deserialize)]
struct Series {
    bars: BTreeMap<Timestamp, RealtimeBar>,
    watermark: Option<Timestamp>,
    authoritative_watermark: Option<Timestamp>,
    state: Option<SeriesState>,
}
#[derive(Debug,Clone)]
pub struct BarReducer {
    contracts: BTreeMap<String, BarContract>,
    quote_channels: BTreeMap<String, String>,
    bar_channels: BTreeMap<String, String>,
    series: BTreeMap<SeriesKey, Series>,
}
impl BarReducer {
    /// Complete immutable replay state; complex keys are encoded as ordered entries.
    pub fn snapshot(&self) -> CoreResult<serde_json::Value> {
        serde_json::to_value((self.contracts.values().collect::<Vec<_>>(), self.series.iter().collect::<Vec<_>>()))
            .map_err(|e| CoreError(e.to_string()))
    }
    pub fn restore(snapshot: serde_json::Value) -> CoreResult<Self> {
        let (contracts, series): (Vec<BarContract>, Vec<(SeriesKey, Series)>) =
            serde_json::from_value(snapshot).map_err(|e| CoreError(e.to_string()))?;
        let mut result = Self::new(contracts)?;
        for (key, value) in series {
            require(result.contracts.contains_key(&key.source_id), "checkpoint series source is unsupported")?;
            for (at, bar) in &value.bars {
                bar.validate()?;
                require(*at == bar.open_time && SeriesKey::from_bar(bar) == key, "checkpoint series key differs from its fact")?;
            }
            require(!result.series.contains_key(&key), "duplicate checkpoint series")?;
            result.series.insert(key, value);
        }
        Ok(result)
    }
    pub fn new(contracts: Vec<BarContract>) -> CoreResult<Self> {
        require(
            !contracts.is_empty(),
            "at least one Bar contract is required",
        )?;
        let mut result = Self {
            contracts: BTreeMap::new(),
            quote_channels: BTreeMap::new(),
            bar_channels: BTreeMap::new(),
            series: BTreeMap::new(),
        };
        for contract in contracts {
            contract.validate()?;
            require(
                !result.contracts.contains_key(&contract.source_id),
                "Bar source ids must be unique",
            )?;
            for channel in &contract.quote_channel_ids {
                require(
                    !result.quote_channels.contains_key(channel),
                    "quote channel belongs to multiple realtime sources",
                )?;
                result
                    .quote_channels
                    .insert(channel.clone(), contract.source_id.clone());
            }
            require(
                !result
                    .bar_channels
                    .contains_key(&contract.authoritative_bar_channel_id),
                "Bar channel belongs to multiple realtime sources",
            )?;
            result.bar_channels.insert(
                contract.authoritative_bar_channel_id.clone(),
                contract.source_id.clone(),
            );
            result
                .contracts
                .insert(contract.source_id.clone(), contract);
        }
        Ok(result)
    }
    pub fn normalize_quote(&self, quote: QuoteSnapshot) -> CoreResult<Option<QuoteEvent>> {
        quote.validate()?;
        if crate::events::is_quote_supplement(&quote) {return Ok(None)}
        Ok(self
            .quote_channels
            .get(&quote.source.provider)
            .map(|source_id| QuoteEvent {
                source_id: source_id.clone(),
                channel_id: quote.source.provider.clone(),
                sequence: quote.source.raw("sequence").and_then(|v| v.as_u64().or_else(||v.as_str().and_then(|s|s.parse().ok()))),
                quote,
            }))
    }
    pub fn normalize_bar(&self, candle: Candle) -> CoreResult<Option<BarEvent>> {
        candle.validate()?;
        let Some(source_id) = self.bar_channels.get(&candle.source.provider) else {
            return Ok(None);
        };
        let state = candle
            .source
            .raw("bar_state")
            .and_then(|v| serde_json::from_value::<BarState>(v.clone()).ok())
            .unwrap_or_else(|| {
                if candle
                    .source
                    .raw("history_file")
                    .is_some_and(|v| v != &json!(false) && !v.is_null() && v != &json!(""))
                    && candle.open_time.checked_add_signed(Duration::seconds(candle.interval_seconds)).is_some_and(|end|end<=candle.source.received_at)
                {
                    BarState::Final
                } else {
                    BarState::ProvisionalAuthoritative
                }
            });
        require(
            state != BarState::ProvisionalQuote,
            "an authoritative Bar event cannot have quote-only state",
        )?;
        Ok(Some(BarEvent {
            source_id: source_id.clone(),
            channel_id: candle.source.provider.clone(),
            finalized_at: (state == BarState::Final).then_some(candle.source.received_at),
            sequence: candle.source.raw("sequence").and_then(|v| v.as_u64().or_else(||v.as_str().and_then(|s|s.parse().ok()))),
            candle,
            state,
        }))
    }
    pub fn apply(&mut self, event: MarketEvent) -> CoreResult<Vec<RealtimeBar>> {
        let mut transitions = match event {
            MarketEvent::Quote(event) => self.apply_quote(event)?,
            MarketEvent::Bar(event) => self.apply_bar(event)?,
        };
        // Coalesce a same-time replacement without changing its original delivery position.
        let mut result: Vec<RealtimeBar> = Vec::new();
        for bar in transitions.drain(..) {
            if let Some(at) = result.iter().position(|v| {
                v.source.provider == bar.source.provider
                    && v.instrument == bar.instrument
                    && v.interval_seconds == bar.interval_seconds
                    && v.open_time == bar.open_time
            }) {
                result[at] = bar;
            } else {
                result.push(bar);
            }
        }
        Ok(result)
    }
    /// The canonical current is installed at the moment its event is applied.
    /// An old correction cannot be lost to hydrate's 240-row trim beforehand.
    pub fn apply_with_current(&mut self,event:MarketEvent,current:Vec<RealtimeBar>)->CoreResult<Vec<RealtimeBar>> {
        for bar in current {bar.validate_hydrated()?;let series=self.series.entry(SeriesKey::from_bar(&bar)).or_default();series.bars.entry(bar.open_time).or_insert(bar);}
        let result=self.apply(event);
        for series in self.series.values_mut(){trim(series);}
        result
    }
    fn apply_quote(&mut self, event: QuoteEvent) -> CoreResult<Vec<RealtimeBar>> {
        event.quote.validate()?;
        if event.quote.source.raw("bar_projection")==Some(&json!("suppressed_outside_verified_source_session")) {return Ok(vec![])}
        require(
            event.quote.source.provider == event.channel_id,
            "quote channel must match raw evidence provider",
        )?;
        let contract = self
            .contracts
            .get(&event.source_id)
            .ok_or_else(|| {
                CoreError(format!(
                    "{} has no source-bound Bar capability",
                    event.source_id
                ))
            })?
            .clone();
        if !event.quote.source.is_fresh(
            event.quote.source.received_at,
            contract.quote_max_age_seconds,
        ) {
            return Ok(vec![]);
        }
        require(
            contract.quote_channel_ids.contains(&event.channel_id)
                || event.channel_id == event.source_id,
            "quote channel is not part of the Bar contract",
        )?;
        let mut transitions = Vec::new();
        for interval in contract.quote_projection_intervals {
            let quote = &event.quote;
            let open_time = floor_time(quote.source.observed_at, interval)?;
            let key = SeriesKey {
                source_id: event.source_id.clone(),
                instrument: quote.instrument.clone(),
                interval_seconds: interval,
            };
            let series = self.series.entry(key).or_default();
            if series.watermark.is_some_and(|at| open_time < at) {
                continue;
            }
            if series.watermark.is_none_or(|at| open_time > at) {
                if interval == 1 {
                    transitions.extend(finalize_before(
                        series,
                        open_time,
                        quote.source.received_at,
                        BarState::ProvisionalQuote,
                    )?);
                }
                series.watermark = Some(open_time);
            }
            let current = series.bars.get(&open_time);
            if current.is_some_and(|bar| {
                bar.state == BarState::Final || quote.source.application_cmp(&bar.source).map_or(quote.source.received_at < bar.source.received_at,|order|order.is_lt())
            }) {
                continue;
            }
            // Retained raw delivery may repeat the latest snapshot after restoring a
            // persisted Bar. Its market/receive timestamps and projection are unchanged.
            if current.is_some_and(|bar| {
                quote.source.received_at == bar.source.received_at
                    && quote.source.observed_at == bar.source.observed_at
                    && quote.last == bar.close
                    && bar
                        .source
                        .raw("last_event_channel_id")
                        .and_then(|v| v.as_str())
                        == Some(event.channel_id.as_str())
            }) {
                continue;
            }
            let value = if let Some(current) = current {
                let authoritative = current.state == BarState::ProvisionalAuthoritative;
                let evidence = if authoritative {
                    current.evidence_channel_id.as_str()
                } else {
                    &event.channel_id
                };
                let mut next = current.clone();
                next.high = next.high.max(quote.last.clone());
                next.low = next.low.min(quote.last.clone());
                next.close = quote.last.clone();
                next.source = public_metadata(
                    &event.source_id,
                    evidence,
                    &quote.source,
                    if authoritative {
                        "authoritative_bar_with_quote_overlay"
                    } else {
                        "quote_event"
                    },
                    Some(&event.channel_id),
                );
                next.evidence_channel_id = evidence.into();
                next.revision = next.revision.checked_add(1).ok_or_else(||CoreError("canonical bar revision exhausted".into()))?;
                next
            } else {
                RealtimeBar {
                    instrument: quote.instrument.clone(),
                    interval_seconds: interval,
                    open_time,
                    open: quote.last.clone(),
                    high: quote.last.clone(),
                    low: quote.last.clone(),
                    close: quote.last.clone(),
                    volume: None,
                    source: public_metadata(
                        &event.source_id,
                        &event.channel_id,
                        &quote.source,
                        "quote_event",
                        None,
                    ),
                    evidence_channel_id: event.channel_id.clone(),
                    state: BarState::ProvisionalQuote,
                    revision: 1,
                    finalized_at: None,
                }
            };
            series.bars.insert(open_time, value.clone());
            trim(series);
            transitions.push(value);
        }
        Ok(transitions)
    }
    fn apply_bar(&mut self, event: BarEvent) -> CoreResult<Vec<RealtimeBar>> {
        event.candle.validate()?;
        require(
            event.candle.source.provider == event.channel_id,
            "Bar channel must match raw evidence provider",
        )?;
        require(
            event.state != BarState::ProvisionalQuote,
            "an authoritative Bar cannot have quote-only state",
        )?;
        require(
            event.state == BarState::Final || event.finalized_at.is_none(),
            "only final Bar events can have finalized_at",
        )?;
        let contract = self.contracts.get(&event.source_id).ok_or_else(|| {
            CoreError(format!(
                "{} has no source-bound Bar capability",
                event.source_id
            ))
        })?;
        require(
            event.channel_id == contract.authoritative_bar_channel_id,
            "Bar channel is not authoritative",
        )?;
        require(
            event.candle.interval_seconds == contract.interval_seconds,
            "Bar interval does not match contract",
        )?;
        let candle = &event.candle;
        let key = SeriesKey {
            source_id: event.source_id.clone(),
            instrument: candle.instrument.clone(),
            interval_seconds: candle.interval_seconds,
        };
        let series = self.series.entry(key).or_default();
        let authority = candle.open_time
            + if event.state == BarState::Final {
                Duration::seconds(candle.interval_seconds)
            } else {
                Duration::zero()
            };
        series.authoritative_watermark = Some(
            series
                .authoritative_watermark
                .map_or(authority, |at| at.max(authority)),
        );
        let mut transitions = Vec::new();
        let mut state = event.state;
        if contract.finality_policy == BarFinalityPolicy::NextAuthoritativeBar {
            if series.watermark.is_some_and(|at| candle.open_time < at)
                && candle.open_time.checked_add_signed(Duration::seconds(candle.interval_seconds)).is_some_and(|end|end<=candle.source.received_at) {
                state = BarState::Final;
            } else if series.watermark.is_none_or(|at| candle.open_time > at) {
                series.watermark = Some(candle.open_time);
                transitions.extend(finalize_before(
                    series,
                    candle.open_time,
                    candle.source.received_at,
                    BarState::ProvisionalAuthoritative,
                )?);
            }
        }
        let current = series.bars.get(&candle.open_time);
        if current.is_some_and(|bar| candle.source.application_cmp(&bar.source).map_or(candle.source.received_at < bar.source.received_at,|order|order.is_lt())) {
            return Ok(transitions);
        }
        if current.is_some_and(|bar| bar.state == BarState::Final) {
            state = BarState::Final;
        }
        let mut candidate = RealtimeBar::from_candle(
            event.candle.clone(),
            event.channel_id.clone(),
            state,
            current.map_or(1,|bar|bar.revision),
            (state == BarState::Final)
                .then_some(event.finalized_at.unwrap_or(candle.source.received_at)),
        );
        candidate.source = public_metadata(
            &event.source_id,
            &event.channel_id,
            &candle.source,
            if state == BarState::Final {
                "authoritative_history"
            } else {
                "authoritative_bar"
            },
            None,
        );
        if current.is_some_and(|bar| same_projection(bar, &candidate)) {
            return Ok(transitions);
        }
        candidate.revision=current.map(|bar|bar.revision.checked_add(1).ok_or_else(||CoreError("canonical bar revision exhausted".into()))).transpose()?.unwrap_or(1);
        series.bars.insert(candidate.open_time, candidate.clone());
        advance_authority(series, &candidate);
        trim(series);
        transitions.push(candidate);
        Ok(transitions)
    }
    pub fn hydrate(
        &mut self,
        rows: Vec<RealtimeBar>,
        state: Option<SeriesState>,
    ) -> CoreResult<()> {
        for bar in rows {
            bar.validate_hydrated()?;
            let series = self.series.entry(SeriesKey::from_bar(&bar)).or_default();
            series.watermark = Some(
                series
                    .watermark
                    .map_or(bar.open_time, |at| at.max(bar.open_time)),
            );
            let merged = merge_for_read(series.bars.remove(&bar.open_time), bar);
            series.bars.insert(merged.open_time, merged);
            trim(series);
        }
        if let Some(state) = state {
            for (key, series) in &mut self.series {
                if key.source_id == state.realtime_source_id
                    && key.instrument.symbol == state.instrument_symbol
                    && key.interval_seconds == state.interval_seconds
                {
                    series.authoritative_watermark = Some(state.authoritative_through);
                    series.state = Some(state.clone());
                }
            }
        }
        Ok(())
    }
    /// Replace the bounded hot projection with canonical rows from one committed view.
    pub fn replace_series(&mut self,key:SeriesKey,rows:Vec<RealtimeBar>)->CoreResult<()> {
        for row in &rows {row.validate_hydrated()?;require(SeriesKey::from_bar(row)==key,"canonical hot series identity mismatch")?;}
        let previous=self.series.remove(&key);
        let mut series=Series::default();
        if let Some(previous)=previous {series.state=previous.state;series.authoritative_watermark=previous.authoritative_watermark;}
        for row in rows {series.watermark=Some(series.watermark.map_or(row.open_time,|v|v.max(row.open_time)));advance_authority(&mut series,&row);series.bars.insert(row.open_time,row);}
        trim(&mut series);self.series.insert(key,series);Ok(())
    }
    pub fn latest(&self, key: &SeriesKey, count: usize) -> Vec<RealtimeBar> {
        self.before(key, None, count)
    }
    pub fn before(
        &self,
        key: &SeriesKey,
        before: Option<Timestamp>,
        count: usize,
    ) -> Vec<RealtimeBar> {
        let Some(series) = self.series.get(key) else {
            return vec![];
        };
        let mut rows: Vec<_> = series
            .bars
            .values()
            .rev()
            .filter(|bar| before.is_none_or(|at| bar.open_time < at))
            .take(count)
            .cloned()
            .collect();
        rows.reverse();
        rows
    }
    pub fn series_state(&self, key: &SeriesKey) -> Option<&SeriesState> {
        self.series.get(key)?.state.as_ref()
    }
    pub fn live_count(&self) -> usize {
        self.series.values().map(|series| series.bars.len()).sum()
    }
}
pub fn floor_time(at: Timestamp, interval_seconds: i64) -> CoreResult<Timestamp> {
    require(interval_seconds > 0, "interval must be positive")?;
    Utc.timestamp_opt(
        at.timestamp().div_euclid(interval_seconds) * interval_seconds,
        0,
    )
    .single()
    .ok_or_else(|| CoreError("Bar timestamp is outside supported range".into()))
}
use chrono::TimeZone;
fn trim(series: &mut Series) {
    while series.bars.len() > 240 {
        series.bars.pop_first();
    }
}
fn finalize_before(
    series: &mut Series,
    before: Timestamp,
    at: Timestamp,
    state: BarState,
) -> CoreResult<Vec<RealtimeBar>> {
    let mut values = Vec::new();
    require(!series.bars.values().any(|bar|bar.open_time<before && bar.open_time.checked_add_signed(Duration::seconds(bar.interval_seconds)).is_some_and(|end|end<=at) && bar.state==state && bar.revision==u64::MAX),"canonical bar revision exhausted")?;
    for bar in series
        .bars
        .values_mut()
        .filter(|bar| bar.open_time < before && bar.open_time.checked_add_signed(Duration::seconds(bar.interval_seconds)).is_some_and(|end|end<=at) && bar.state == state)
    {
        bar.state = BarState::Final;
        bar.revision = bar.revision.checked_add(1).ok_or_else(||CoreError("canonical bar revision exhausted".into()))?;
        bar.finalized_at = Some(at);
        values.push(bar.clone());
    }
    if state == BarState::ProvisionalAuthoritative {
        for bar in &values {
            advance_authority(series, bar);
        }
    }
    Ok(values)
}
fn advance_authority(series: &mut Series, bar: &RealtimeBar) {
    if bar.state != BarState::Final
        || !bar
            .source
            .raw("derivation")
            .and_then(|v| v.as_str())
            .is_some_and(|v| v.starts_with("authoritative_"))
    {
        return;
    }
    let boundary = bar.open_time + Duration::seconds(bar.interval_seconds);
    if let Some(state) = &mut series.state {
        if boundary >= state.authoritative_through {
            state.upstream_channel_id = bar.evidence_channel_id.clone();
            state.provider_symbol = bar.source.provider_symbol.clone();
        }
        state.authoritative_through = state.authoritative_through.max(boundary);
        state.latest_authoritative_open_time = Some(
            state
                .latest_authoritative_open_time
                .map_or(bar.open_time, |at| at.max(bar.open_time)),
        );
        if state
            .tail_checked_through
            .is_some_and(|at| state.authoritative_through >= at)
        {
            state.tail_checked_through = None;
            state.tail_checked_at = None;
        }
        if state.evidence_version.starts_with("live:") {
            state.evidence_version = format!("live:{}", bar.evidence_channel_id);
        }
        state.updated_at = state.updated_at.max(bar.source.received_at);
    } else {
        series.state = Some(SeriesState {
            realtime_source_id: bar.source.provider.clone(),
            instrument_symbol: bar.instrument.symbol.clone(),
            upstream_channel_id: bar.evidence_channel_id.clone(),
            provider_symbol: bar.source.provider_symbol.clone(),
            interval_seconds: bar.interval_seconds,
            latest_authoritative_open_time: Some(bar.open_time),
            authoritative_through: boundary,
            history_floor: None,
            tail_checked_through: None,
            tail_checked_at: None,
            evidence_version: format!("live:{}", bar.evidence_channel_id),
            updated_at: bar.source.received_at,
        });
    }
    series.authoritative_watermark = Some(
        series
            .authoritative_watermark
            .map_or(boundary, |at| at.max(boundary)),
    );
}
pub fn public_metadata(
    source_id: &str,
    evidence: &str,
    metadata: &SourceMetadata,
    derivation: &str,
    last_channel: Option<&str>,
) -> SourceMetadata {
    let mut raw=json!({ "cache_scope": "realtime_source", "derivation": derivation, "evidence_channel_id": evidence, "last_event_channel_id": last_channel.unwrap_or(evidence), "quote_time_basis": "source", "timestamp_precision_seconds": metadata.raw("timestamp_precision_seconds").cloned().unwrap_or(json!(0)),
        "capture_epoch":metadata.raw("capture_epoch"),"capture_sequence":metadata.raw("capture_sequence"),"capture_digest":metadata.raw("capture_digest"),"capture_accepted_at_ns":metadata.raw("capture_accepted_at_ns"),"source_connection_id":metadata.raw("connection_id"),"source_sequence":metadata.raw("sequence") });
    // Carry bounded semantic summaries, never a full captured response per bar.
    for key in ["source_volume_components","source_volume_component_groups","source_component_count","source_component_known_volume_count","source_component_known_volume_sum"] {
        if let Some(value)=metadata.raw(key){raw[key]=value.clone();}
    }
    if matches!(metadata.raw("protocol").and_then(|v|v.as_str()),Some("tonghuashun_fuyao_v1"|"tonghuashun_public_line_v6")) {
        for key in ["channel","protocol","source_instrument","source_precision_ns","source_timestamp_ms","wire_time_unit","published_at","publication_time_unknown","field_policy_version","reference_sha256","source_label","source_label_ns","source_label_semantics","source_period","canonical_interval_semantics","clock_policy_verified","source_interval_end","source_interval_end_ns","minute_clock_policy","source_row","source_fields","components","auction_policy","source_point_quarantine_ref","finality_evidence","authoritative_input","source_calendar_trade_date","source_calendar_time_info_sha256","source_calendar_verified_for_label","source_point_classification"] {
            if let Some(value)=metadata.raw(key){raw[key]=value.clone();}
        }
    }
    SourceMetadata {
        provider: source_id.into(),
        provider_symbol: metadata.provider_symbol.clone(),
        observed_at: metadata.observed_at,
        received_at: metadata.received_at,
        raw_payload: Some(raw),
    }
}
fn same_projection(left: &RealtimeBar, right: &RealtimeBar) -> bool {
    left.open == right.open
        && left.high == right.high
        && left.low == right.low
        && left.close == right.close
        && left.volume == right.volume
        && left.state == right.state
        && left.evidence_channel_id == right.evidence_channel_id
        && ["source_volume_components","source_volume_component_groups","source_component_count","source_component_known_volume_count","source_component_known_volume_sum"].iter().all(|key|left.source.raw(key)==right.source.raw(key))
}
pub fn merge_for_read(current: Option<RealtimeBar>, mut incoming: RealtimeBar) -> RealtimeBar {
    let Some(mut current) = current else {
        return incoming;
    };
    if current.state == BarState::Final {
        if incoming.state == BarState::Final
            && incoming.source.received_at > current.source.received_at
        {
            incoming.revision = incoming.revision.max(current.revision);
            return incoming;
        }
        return current;
    }
    if incoming.state == BarState::Final {
        incoming.revision = incoming.revision.max(current.revision);
        return incoming;
    }
    if current.state == BarState::ProvisionalAuthoritative {
        if incoming.state == BarState::ProvisionalQuote {
            if incoming.source.received_at < current.source.received_at {
                return current;
            }
            current.high = current.high.max(incoming.high);
            current.low = current.low.min(incoming.low);
            current.close = incoming.close;
            current.revision = current.revision.max(incoming.revision);
            return current;
        }
        return if incoming.source.received_at >= current.source.received_at {
            incoming
        } else {
            current
        };
    }
    if incoming.state == BarState::ProvisionalAuthoritative
        || incoming.source.received_at >= current.source.received_at
    {
        incoming
    } else {
        current
    }
}
