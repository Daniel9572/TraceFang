use chrono::{DateTime, Utc};
use serde::Deserialize;
use serde_json::Value;
use tracefang_core::{
    domain::{Candle, Decimal, QuoteSnapshot, Timestamp},
    events::{MarketEvent, RealtimeBar, quote_event_id},
    periods::{
        Bucket, LivePeriodProjector, MarketSchedule, Period, bucket_for, materialization_version,
        previous_bucket, project_bars, schedule_version,
    },
    reducer::{BarContract, BarReducer},
};

fn golden() -> Value {
    serde_json::from_str(include_str!("fixtures/core/golden.json")).unwrap()
}
fn decode<T: for<'de> Deserialize<'de>>(value: &Value) -> T {
    serde_json::from_value(value.clone()).unwrap()
}
// The checked-in Python evidence remains immutable. New wire/provenance fields
// are asserted separately. Original values, source clocks and revisions remain
// independent expectations; corrected derived availability has its own oracle.
fn assert_legacy_projection(mut actual:Vec<RealtimeBar>,mut expected:Vec<RealtimeBar>,label:&str) {
    for bar in &mut actual {if let Some(raw)=bar.source.raw_payload.as_mut().and_then(Value::as_object_mut) {
        if let Some(verified)=raw.remove("calendar_bucket_verified"){assert_eq!(verified,true,"legacy declared calendars have complete date authority: {label}");}
        for key in ["known_volume_count","known_volume_sum","capture_epoch","capture_sequence","capture_digest","capture_accepted_at_ns","source_connection_id","source_sequence","component_finalized_at_ns","derived_availability_lower_bound_ns","finalization_clock_policy","finalization_time_unknown","source_publication_time_unknown","accepted_clock_known_component_count","accepted_clock_all_components_known","source_volume_components","source_volume_component_groups","source_volume_unit","source_volume_fallback_evidence","source_volume_grouping_reason","calendar_evidence_known_at_ns","calendar_evidence_clock_policy"] {raw.remove(key);}
    }}
    for bar in &mut expected {if let Some(raw)=bar.source.raw_payload.as_mut().and_then(Value::as_object_mut) {
        if let Some(value)=raw.get_mut("component_count") {if let Some(count)=value.as_u64(){*value=Value::String(count.to_string());}}
    }}
    assert_eq!(actual,expected,"{label}");
}
fn assert_derived_clock_policy(actual:&[RealtimeBar],expected:&mut[RealtimeBar],rows:&[RealtimeBar],period:Period,schedule:Option<&MarketSchedule>,now:Timestamp) {
    use tracefang_core::events::BarState;
    for (bar,want) in actual.iter().zip(expected) {
        let bucket=bucket_for(bar.open_time,period,schedule).unwrap();
        let members=rows.iter().filter(|row|bucket_for(row.open_time,period,schedule).unwrap().start==bar.open_time).collect::<Vec<_>>();
        let completion=members.iter().filter_map(|row|row.finalized_at).max();
        let receipt=members.iter().map(|row|row.source.received_at).max().unwrap();
        let lower_bound=completion.map(|clock|clock.max(bucket.input_end()).max(receipt));
        let raw=bar.source.raw_payload.as_ref().unwrap();
        assert_eq!(raw["component_finalized_at_ns"],serde_json::json!(completion.and_then(|clock|clock.timestamp_nanos_opt()).map(|n|n.to_string())));
        assert_eq!(raw["derived_availability_lower_bound_ns"],serde_json::json!(lower_bound.and_then(|clock|clock.timestamp_nanos_opt()).map(|n|n.to_string())));
        assert_eq!(raw["finalization_clock_policy"],"derived-availability-max-required-calendar-and-component-clocks-v2");
        assert_eq!(raw["source_publication_time_unknown"],true);assert_eq!(raw["finalization_time_unknown"],members.iter().any(|row|row.finalized_at.is_none()));
        assert_eq!(raw["accepted_clock_known_component_count"],"0");assert_eq!(raw["accepted_clock_all_components_known"],false);
        // The old Python fixture propagated a component's confirmation into a
        // larger interval. Keep that fixture unchanged and separately enforce
        // the declared derived lower bound and knowledge cutoff.
        if want.state==BarState::Final && (now<bucket.input_end() || now<receipt || lower_bound.is_some_and(|at|at>now)) {want.state=BarState::ProvisionalAuthoritative;}
        want.finalized_at=if want.state==BarState::Final{lower_bound}else{None};
        assert_eq!(bar.state,want.state);assert_eq!(bar.finalized_at,want.finalized_at);
    }
}
fn assert_volume_coverage(actual:&[RealtimeBar],expected:&mut[RealtimeBar],rows:&[RealtimeBar],period:Period,schedule:Option<&MarketSchedule>) {
    assert_eq!(actual.len(),expected.len());
    for (bar,want) in actual.iter().zip(expected) {
        let members=rows.iter().filter(|row|bucket_for(row.open_time,period,schedule).unwrap().start==bar.open_time).collect::<Vec<_>>();
        assert!(!members.is_empty());let known=members.iter().filter(|bar|bar.volume.is_some()).count();
        let sum=members.iter().filter_map(|bar|bar.volume.clone()).fold(Decimal::ZERO,|a,b|a+b);
        let raw=bar.source.raw_payload.as_ref().unwrap();
        assert_eq!(raw["known_volume_count"],known.to_string());assert_eq!(raw["component_count"],members.len().to_string());assert_eq!(raw["known_volume_sum"],sum.to_string());
        assert_eq!(raw["source_volume_components"]["policy"],tracefang_core::source_volume::CANONICAL_FALLBACK);
        assert_eq!(raw["source_volume_components"]["known_volume_sum"],sum.to_string());
        assert_eq!(raw["source_volume_components"]["known_count"],known.to_string());
        assert_eq!(raw["source_volume_components"]["total_count"],members.len().to_string());
        assert!(raw["source_volume_fallback_evidence"].as_str().unwrap().contains("original upstream fragment coverage is unknown"));
        let volume=(known==members.len()).then_some(sum);assert_eq!(bar.volume,volume);want.volume=volume;
    }
}
fn identity_oracle(input:&Value,canonical:bool)->String {
    use sha2::{Sha256,Digest};
    let source=&input["source"];let raw=&source["raw_payload"];
    let mut parts=vec![source["provider"].as_str().unwrap().to_owned(),source["provider_symbol"].as_str().unwrap().to_owned()];
    let transport=raw["connection_id"].as_str().filter(|v|!v.is_empty()).zip(raw["sequence"].as_u64().or_else(||raw["sequence"].as_str()?.parse().ok()));
    if let Some((connection,sequence))=transport {parts.extend(["transport".into(),connection.into(),sequence.to_string()]);}
    else {
        parts.push("capture".into());
        for key in ["observed_at","received_at"] {let at=decode::<Timestamp>(&source[key]);parts.push(if at.timestamp_subsec_nanos()%1000==0{tracefang_core::domain::isoformat_microseconds(at)}else{tracefang_core::domain::isoformat(at)});}
        for key in ["last","open","high","low","volume","change","change_percent"] {
            let text=if input[key].is_null(){"None".into()}else if canonical{decode::<Decimal>(&input[key]).to_string()}else{input[key].as_str().unwrap().to_owned()};parts.push(text);
        }
    }
    format!("quote:{}{}",if canonical&&transport.is_none(){"v2:"}else{""},hex::encode(Sha256::digest(parts.join("\u{1f}").as_bytes())))
}

#[test]
fn python_reducer_golden_every_revision_and_event_identity() {
    for case in golden()["sequences"].as_array().unwrap() {
        let mut reducer = BarReducer::new(vec![BarContract::new(
            "source-a",
            "history-a",
            vec!["live-a".into()],
        )])
        .unwrap();
        for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let event = if step["kind"] == "quote" {
                let quote: QuoteSnapshot = decode(&step["input"]);
                assert_eq!(identity_oracle(&step["input"],false),step["event_id"].as_str().unwrap(),"unchanged v1 evidence");
                assert_eq!(quote_event_id(&quote),identity_oracle(&step["input"],true),"explicit v2 identity");
                MarketEvent::Quote(reducer.normalize_quote(quote).unwrap().unwrap())
            } else {
                MarketEvent::Bar(
                    reducer
                        .normalize_bar(decode::<Candle>(&step["input"]))
                        .unwrap()
                        .unwrap(),
                )
            };
            let expected: Vec<RealtimeBar> = decode(&step["expected"]);
            assert_legacy_projection(reducer.apply(event).unwrap(),expected,&format!("{} step {}",case["name"],index));
        }
    }
}
#[test]
fn python_period_golden_sessions_finality_dst_decimal_volume() {
    for case in golden()["periods"].as_array().unwrap() {
        let schedule: Option<MarketSchedule> = decode(&case["schedule"]);
        let rows=decode::<Vec<RealtimeBar>>(&case["rows"]);let period=Period::parse(case["period"].as_str().unwrap()).unwrap();
        let actual = project_bars(
            &rows,
            period,
            schedule.as_ref(),
            decode(&case["now"]),
        )
        .unwrap();
        let mut expected=decode::<Vec<RealtimeBar>>(&case["expected"]);
        assert_volume_coverage(&actual,&mut expected,&rows,period,schedule.as_ref());
        assert_derived_clock_policy(&actual,&mut expected,&rows,period,schedule.as_ref(),decode(&case["now"]));
        assert_legacy_projection(actual,expected,case["name"].as_str().unwrap());
    }
}
#[test]
fn python_live_period_corrections_can_lower_extremes_and_do_not_rewind() {
    let golden = golden();
    let case = &golden["live"];
    let schedule: MarketSchedule = decode(&case["schedule"]);
    let period = Period::parse(case["period"].as_str().unwrap()).unwrap();
    let mut projector = LivePeriodProjector::new();
    let mut rows=std::collections::BTreeMap::new();
    for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
        let input:RealtimeBar=decode(&step["input"]);let now=input.source.received_at;rows.insert(input.open_time,input.clone());
        let actual = projector
            .accept(input, Some(&schedule), &[period])
            .unwrap()
            .into_iter()
            .map(|(_, bar)| bar)
            .collect::<Vec<_>>();
        let mut expected=decode::<Vec<RealtimeBar>>(&step["expected"]);
        assert_volume_coverage(&actual,&mut expected,&rows.values().cloned().collect::<Vec<_>>(),period,Some(&schedule));
        assert_derived_clock_policy(&actual,&mut expected,&rows.values().cloned().collect::<Vec<_>>(),period,Some(&schedule),now);
        assert_legacy_projection(actual,expected,&format!("step {index}"));
    }
}
#[test]
fn python_calendar_bucket_cursor_and_hash_compatibility() {
    for case in golden()["buckets"].as_array().unwrap() {
        let schedule: MarketSchedule = decode(&case["schedule"]);
        let period = Period::parse(case["period"].as_str().unwrap()).unwrap();
        let actual = bucket_for(decode(&case["at"]), period, Some(&schedule)).unwrap();
        assert_eq!(
            actual,
            decode::<Bucket>(&case["bucket"]),
            "{}",
            case["name"]
        );
        assert_eq!(
            previous_bucket(&actual, period, Some(&schedule)).unwrap(),
            decode::<Bucket>(&case["previous"]),
            "{} previous",
            case["name"]
        );
    }
    for case in golden()["versions"].as_array().unwrap() {
        let schedule: Option<MarketSchedule> = decode(&case["schedule"]);
        assert_eq!(
            schedule_version(schedule.as_ref()).unwrap(),
            case["cursor"].as_str().unwrap()
        );
        assert_eq!(
            materialization_version(schedule.as_ref()).unwrap(),
            case["materialization"].as_str().unwrap()
        );
    }
}
#[test]
fn decimal_json_roundtrip_is_exact_and_emits_a_string() {
    let mut value = golden()["sequences"][0]["steps"][0]["input"].clone();
    value["last"] = serde_json::from_str("12345678901234567890.12345678").unwrap();
    let quote: QuoteSnapshot = decode(&value);
    assert_eq!(quote.last.to_string(), "12345678901234567890.12345678");
    let rendered = serde_json::to_value(&quote).unwrap();
    assert!(rendered["last"].is_string());
    assert_eq!(
        rendered["last"].as_str().unwrap(),
        "12345678901234567890.12345678"
    );
    assert_eq!(decode::<QuoteSnapshot>(&rendered), quote);
    let mut invalid = value.clone();
    invalid["last"] = Value::String("NaN".into());
    assert!(serde_json::from_value::<QuoteSnapshot>(invalid).is_err());
    value["volume"] = Value::String("-1".into());
    assert!(decode::<QuoteSnapshot>(&value).validate().is_err());
}
#[test]
fn timezone_less_input_is_rejected() {
    let mut value = golden()["sequences"][0]["steps"][0]["input"].clone();
    value["source"]["observed_at"] = Value::String("2026-08-10T12:00:00".into());
    assert!(serde_json::from_value::<QuoteSnapshot>(value).is_err());
}
#[test]
fn restored_final_bars_reject_quotes_after_restart() {
    let case = &golden()["sequences"][1];
    let last = &case["steps"].as_array().unwrap().last().unwrap()["expected"];
    let bars: Vec<RealtimeBar> = decode(last);
    let mut reducer = BarReducer::new(vec![BarContract::new(
        "source-a",
        "history-a",
        vec!["live-a".into()],
    )])
    .unwrap();
    reducer.hydrate(bars.clone(), None).unwrap();
    let mut quote: QuoteSnapshot = decode(&case["steps"][0]["input"]);
    quote.source.received_at = "2026-08-10T12:00:50Z".parse::<DateTime<Utc>>().unwrap();
    quote.source.observed_at = quote.source.received_at;
    quote.last = Decimal::from(9000);
    let event = reducer.normalize_quote(quote).unwrap().unwrap();
    let emitted = reducer.apply(MarketEvent::Quote(event)).unwrap();
    assert!(emitted.iter().all(|bar| bar.interval_seconds != 60));
}
#[test]
fn live_prefix_restores_long_period_open_without_losing_hot_corrections() {
    let data = golden();
    let case = &data["periods"][0];
    let schedule: MarketSchedule = decode(&case["schedule"]);
    let rows: Vec<RealtimeBar> = decode(&case["rows"]);
    let mut projector = LivePeriodProjector::new();
    let hot = rows.last().unwrap().clone();
    projector.seed(&[hot.clone()]).unwrap();
    let bucket = bucket_for(hot.open_time, Period::Mo1, Some(&schedule)).unwrap();
    projector.prepare_calendar(
        "source-a",
        &hot.instrument,
        Period::Mo1,
        bucket,
        rows.clone(),
        Some(&schedule),
    ).unwrap();
    let now: Timestamp = decode(&case["now"]);
    let actual = projector
        .current("source-a", &hot.instrument, Period::Mo1, now)
        .unwrap()
        .unwrap();
    let expected = project_bars(&rows, Period::Mo1, Some(&schedule), now).unwrap();
    assert_eq!(actual, expected[0]);
}

#[test]
fn corrected_authority_state_hydrates_exclusive_end_and_next_capture_advances_from_corrected_key() {
    use tracefang_core::{events::{BarEvent,BarState},reducer::{SeriesKey,SeriesState}};
    let mut bar:RealtimeBar=decode(&golden()["periods"][0]["rows"][0]);
    bar.open_time="2026-09-30T06:05:00Z".parse().unwrap();bar.interval_seconds=60;bar.state=BarState::Final;bar.finalized_at=Some("2026-09-30T06:06:07Z".parse().unwrap());bar.source.received_at=bar.finalized_at.unwrap();bar.source.observed_at="2026-09-30T06:06:00Z".parse().unwrap();bar.source.provider="source-a".into();bar.source.raw_payload=Some(serde_json::json!({"derivation":"authoritative_history","minute_clock_policy":"ths-v6-period61-shfe-interval-end-v2"}));
    let boundary=bar.open_time+chrono::Duration::minutes(1);let state=SeriesState{realtime_source_id:"source-a".into(),instrument_symbol:bar.instrument.symbol.clone(),upstream_channel_id:"history-a".into(),provider_symbol:bar.source.provider_symbol.clone(),interval_seconds:60,latest_authoritative_open_time:Some(bar.open_time),authoritative_through:boundary,history_floor:Some(bar.open_time),tail_checked_through:None,tail_checked_at:None,evidence_version:"clock-corrected-fixed-authority".into(),updated_at:bar.source.received_at};
    let mut reducer=BarReducer::new(vec![BarContract::new("source-a","history-a",vec!["live-a".into()])]).unwrap();reducer.hydrate(vec![bar.clone()],Some(state.clone())).unwrap();let key=SeriesKey::from_bar(&bar);assert_eq!(reducer.series_state(&key),Some(&state));
    let mut quote:QuoteSnapshot=decode(&golden()["sequences"][0]["steps"][0]["input"]);quote.instrument=bar.instrument.clone();quote.source.provider="live-a".into();quote.source.observed_at="2026-09-30T06:05:59Z".parse().unwrap();quote.source.received_at="2026-09-30T06:06:18Z".parse().unwrap();let old=reducer.normalize_quote(quote.clone()).unwrap().unwrap();assert!(reducer.apply(MarketEvent::Quote(old)).unwrap().iter().all(|v|v.interval_seconds!=60));
    quote.source.observed_at="2026-09-30T06:06:18Z".parse().unwrap();let event=reducer.normalize_quote(quote).unwrap().unwrap();assert!(reducer.apply(MarketEvent::Quote(event)).unwrap().iter().any(|v|v.interval_seconds==60 && v.open_time==boundary && v.state==BarState::ProvisionalQuote));
    let mut candle=bar.candle();candle.open_time=boundary;candle.source.provider="history-a".into();candle.source.received_at="2026-09-30T06:07:18Z".parse().unwrap();candle.source.observed_at="2026-09-30T06:07:00Z".parse().unwrap();let at=candle.source.received_at;
    reducer.apply(MarketEvent::Bar(BarEvent{source_id:"source-a".into(),channel_id:"history-a".into(),candle,state:BarState::Final,sequence:Some(1),finalized_at:Some(at)})).unwrap();let updated=reducer.series_state(&key).unwrap();assert_eq!(updated.latest_authoritative_open_time,Some(boundary));assert_eq!(updated.authoritative_through,boundary+chrono::Duration::minutes(1));assert_eq!(updated.updated_at,at);
    let restored=BarReducer::restore(reducer.snapshot().unwrap()).unwrap();assert_eq!(restored.series_state(&key),Some(updated));
}

#[test]
fn replay_latest_frame_after_hydration_does_not_change_bars_or_revisions() {
    use std::collections::BTreeMap;
    use tracefang_core::reducer::SeriesKey;
    for case in golden()["sequences"].as_array().unwrap() {
        let make = || {
            BarReducer::new(vec![BarContract::new(
                "source-a",
                "history-a",
                vec!["live-a".into()],
            )])
            .unwrap()
        };
        let mut reducer = make();
        let mut stored = BTreeMap::new();
        for (index, step) in case["steps"].as_array().unwrap().iter().enumerate() {
            let event = if step["kind"] == "quote" {
                MarketEvent::Quote(
                    reducer
                        .normalize_quote(decode(&step["input"]))
                        .unwrap()
                        .unwrap(),
                )
            } else {
                MarketEvent::Bar(
                    reducer
                        .normalize_bar(decode(&step["input"]))
                        .unwrap()
                        .unwrap(),
                )
            };
            for bar in reducer.apply(event.clone()).unwrap() {
                stored.insert((SeriesKey::from_bar(&bar), bar.open_time), bar);
            }
            let mut restored = make();
            restored
                .hydrate(stored.values().cloned().collect(), None)
                .unwrap();
            let repeated = restored.apply(event).unwrap();
            assert!(
                repeated.is_empty(),
                "{} step {} repeated {} transitions",
                case["name"],
                index,
                repeated.len()
            );
            for ((key, _), bar) in &stored {
                assert!(
                    restored.latest(key, 240).contains(bar),
                    "{} step {} changed persisted bar",
                    case["name"],
                    index
                );
            }
        }
    }
}

#[test]
fn history_file_does_not_confirm_an_unfinished_tail_but_next_authority_does() {
    use tracefang_core::events::BarState;
    let data=golden();let input=data["sequences"].as_array().unwrap().iter().flat_map(|c|c["steps"].as_array().unwrap()).find(|s|s["kind"]=="bar").unwrap()["input"].clone();
    let mut candle:Candle=decode(&input);candle.source.raw_payload=Some(serde_json::json!({"history_file":"bounded-fixture"}));
    candle.source.received_at=candle.open_time+chrono::Duration::seconds(20);
    let mut reducer=BarReducer::new(vec![BarContract::new("source-a","history-a",vec!["live-a".into()])]).unwrap();
    let event=reducer.normalize_bar(candle.clone()).unwrap().unwrap();assert_eq!(event.state,BarState::ProvisionalAuthoritative);assert_eq!(event.finalized_at,None);
    let first=reducer.apply(MarketEvent::Bar(event)).unwrap();assert_eq!(first[0].state,BarState::ProvisionalAuthoritative);assert_eq!(first[0].finalized_at,None);
    let previous=candle.open_time;candle.open_time+=chrono::Duration::minutes(1);candle.source.observed_at=candle.open_time;candle.source.received_at=candle.open_time+chrono::Duration::seconds(5);
    let event=reducer.normalize_bar(candle).unwrap().unwrap();let transitions=reducer.apply(MarketEvent::Bar(event)).unwrap();
    let finalized=transitions.iter().find(|b|b.open_time==previous).unwrap();assert_eq!(finalized.state,BarState::Final);assert_eq!(finalized.finalized_at,Some(previous+chrono::Duration::seconds(65)));
    assert_eq!(transitions.last().unwrap().state,BarState::ProvisionalAuthoritative);
}

#[test]
fn explicit_source_finality_is_not_rejected_by_receive_clock_rollback() {
    use tracefang_core::events::BarState;
    let data=golden();let input=data["sequences"].as_array().unwrap().iter().flat_map(|c|c["steps"].as_array().unwrap()).find(|s|s["kind"]=="bar").unwrap()["input"].clone();
    let mut candle:Candle=decode(&input);candle.source.raw_payload=Some(serde_json::json!({"bar_state":"final","history_file":"explicit-source-marker-fixture"}));candle.source.received_at=candle.open_time-chrono::Duration::seconds(5);
    let reducer=BarReducer::new(vec![BarContract::new("source-a","history-a",vec!["live-a".into()])]).unwrap();let event=reducer.normalize_bar(candle).unwrap().unwrap();assert_eq!(event.state,BarState::Final);assert!(event.finalized_at.is_some());
}
