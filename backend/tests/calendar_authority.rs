//! Date-bound calendar evidence is durable, selected in the fact read view, and
//! cannot confirm periods/dates it did not describe.
use anyhow::Result;
use chrono::{DateTime,Utc};
use serde_json::{Value,json};
use tracefang_core::{native_store::{Store,SeriesVersion,ScanResume},periods::{CalendarAuthority,CapturedSourceCalendar,MarketSchedule,Period},persistence_contract::*};
fn ns(s:&str)->i64{s.parse::<DateTime<Utc>>().unwrap().timestamp_nanos_opt().unwrap()}
fn pos(sequence:u64)->CapturePosition{CapturePosition{epoch:"calendar-raw".into(),sequence,digest:format!("{sequence:064x}")}}
fn fact(at:&str)->ImportBarRow{let open=ns(at);ImportBarRow{instrument_symbol:"AU2612".into(),realtime_source_id:"tonghuashun_futures".into(),evidence_channel_id:"tonghuashun_fuyao".into(),interval_seconds:60,open_time_ns:open,close_time_ns:open+60_000_000_000,open:"1".into(),high:"2".into(),low:"0".into(),close:"1".into(),volume:None,revision:1,received_sequence:Some(1),state:"final".into(),finalized_at_ns:Some(open+60_000_000_000),source_observed_at_ns:open+60_000_000_000,received_at_ns:open+60_000_000_000,source_metadata:json!({"provider":"tonghuashun_futures","provider_symbol":"fuyao:65:au2612"}),evidence:Value::Null}}
fn request(period:Period)->CanonicalSnapshotRequest{CanonicalSnapshotRequest{symbol:"AU2612".into(),source_id:"tonghuashun_futures".into(),period:period.as_str().into(),selection:BarSelection::Latest{count:300},final_only:false,expected_version:None}}
fn schedule()->MarketSchedule{MarketSchedule{time_zone:"Asia/Shanghai".into(),trading_day_rule:None,reference:None,sessions:vec![],authority:Some(CalendarAuthority::default())}}
fn update(sequence:u64)->CapturedSourceCalendar{
    let values:Value=serde_json::from_str(include_str!("../assets/fuyao-calendar.json")).unwrap();let authority:CalendarAuthority=serde_json::from_value(values["fuyao:65:au2612"].clone()).unwrap();
    let mut day=authority.absolute_days[0].clone();day.capture_position=Some(pos(sequence));day.received_at_ns=ns("2026-10-03T00:00:00Z");day.accepted_at_ns=Some(day.received_at_ns+1);
    CapturedSourceCalendar{source_id:"tonghuashun_futures".into(),symbol:"AU2612".into(),day}
}
#[tokio::test]
async fn calendar_only_capture_commit_reopen_duplicate_and_revision_preserve_mvcc_and_clock()->Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    // The prior trading date is unknown. Lunch on the known date is a separate,
    // actually known closed gap. Neither case changes/removes canonical facts.
    store.commit_rows(pos(1),vec![fact("2026-09-29T01:00:00Z"),fact("2026-09-30T01:00:00Z"),fact("2026-09-30T04:00:00Z")],vec![],vec![]).await?;
    let row=update(2);store.commit_rows_with_decoder(pos(2),vec![],vec![],vec![],Some(json!({"schema":"calendar-projection-only-v1","calendar_authorities":[row]}))).await?;
    assert!(store.metadata("runtime","decoder_checkpoint").await?.is_none(),"a projection-only delta is not a restorable decoder checkpoint");
    let early=store.canonical_period_page_at(request(Period::D1),Period::D1,Some(schedule()),ns("2026-09-30T08:00:00Z")).await?;
    assert!(early.bars.is_empty(),"future received calendar evidence cannot leak into an earlier cutoff");assert_eq!(early.coverage["calendar_projection"]["unverified_calendar_minutes"],"3");
    let cutoff=ns("2026-10-04T00:00:00Z");let page=store.canonical_period_page_at(request(Period::D1),Period::D1,Some(schedule()),cutoff).await?;
    assert_eq!(page.bars.len(),1);assert_eq!(page.bars[0]["state"],"final");assert_eq!(page.bars[0]["close_time_ns"],ns("2026-09-30T07:00:00Z").to_string());
    let known=ns("2026-10-03T00:00:00Z")+1;
    assert_eq!(page.bars[0]["raw_payload"]["calendar_evidence_known_at_ns"],known.to_string());
    assert_eq!(page.bars[0]["finalized_at_ns"],known.to_string(),"derived value cannot be known before the calendar input arrived");
    assert_eq!(page.coverage["calendar_projection"]["unverified_calendar_minutes"],"1");assert_eq!(page.coverage["calendar_projection"]["excluded_outside_schedule"],"1");assert_eq!(page.coverage["calendar_projection"]["complete"],false);
    assert_eq!(page.bars[0]["raw_payload"]["calendar_projection_version"],"date-authority-session-membership-v3");assert_eq!(page.bars[0]["raw_payload"]["calendar_bucket_verified"],true);assert_eq!(page.bars[0]["raw_payload"]["bucket_elapsed"],true);
    for period in [Period::W1,Period::Mo1,Period::Q1,Period::Y1]{let p=store.canonical_period_page_at(request(period),period,Some(schedule()),cutoff).await?;assert_eq!(p.bars.len(),1);assert_ne!(p.bars[0]["state"],"final","single-day authority must not confirm {period}");assert!(p.bars[0]["finalized_at_ns"].is_null());assert_eq!(p.bars[0]["raw_payload"]["calendar_bucket_verified"],false);}
    let baseline=store.metadata("source_calendar","tonghuashun_futures:AU2612").await?.unwrap();let proof:SeriesVersion=serde_json::from_value(page.coverage["series_version"].clone())?;
    let dup=update(3);store.commit_rows_with_decoder(pos(3),vec![],vec![],vec![],Some(json!({"schema":"calendar-projection-only-v1","calendar_authorities":[dup]}))).await?;
    assert_eq!(store.metadata("source_calendar","tonghuashun_futures:AU2612").await?.unwrap(),baseline,"duplicate body does not relabel its first knowledge clock");
    let mut correction=update(4);correction.day.raw_body_sha256="f".repeat(64);correction.day.continuous_sessions[3].start="2026-09-30T04:00:00Z".parse()?;
    let receipt=store.commit_rows_with_decoder(pos(4),vec![],vec![],vec![],Some(json!({"schema":"calendar-projection-only-v1","calendar_authorities":[correction]}))).await?;
    assert!(receipt.series_changes.iter().any(|c|c.historical_correction));
    let req=CanonicalScanRequest{symbol:"AU2612".into(),source_id:"tonghuashun_futures".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:cutoff,final_only:false,expected_version:None};
    let resume=ScanResume{after_ns:ns("2026-09-30T00:00:00Z"),series_generation:proof.series_generation,correction_epoch:proof.correction_epoch,append_watermark_ns:Some(proof.append_watermark_ns)};
    assert!(store.canonical_calendar_scan(req,Period::D1,Some(schedule()),16,Some(resume),|_|Ok(())).await.unwrap_err().to_string().contains("quant_resume_invalid"));
    store.close().await?;drop(store);let ro=Store::open_read_only(&path)?;
    let after=ro.canonical_period_page_at(request(Period::D1),Period::D1,Some(schedule()),cutoff).await?;assert_eq!(after.coverage["calendar_projection"]["excluded_outside_schedule"],"0");assert_eq!(after.bars[0]["raw_payload"]["component_count"],"2");
    assert_eq!(ro.canonical_snapshot(request(Period::M1)).await?.bars.len(),3);ro.close().await?;Ok(())
}

#[tokio::test]
async fn captured_clock_proof_is_atomic_and_does_not_certify_existing_unknown_prefix()->Result<()> {
    fn row(sequence:u64,proven:bool)->ImportBarRow {
        let mut row=fact("2026-09-30T01:01:00Z");row.instrument_symbol="AU2610".into();row.evidence_channel_id="tonghuashun_public_line_v6".into();
        if proven {row.source_metadata["raw_payload"]=json!({"clock_policy_verified":true,"minute_clock_policy":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"authoritative_input":{"protocol":"tonghuashun_public_line_v6","provider_code":"qh_au2610","period":"61","response_kind":"minute_year","body_sha256":"a".repeat(64),"capture_position":pos(sequence)}});}
        row
    }
    for existing in [false,true] {
        let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
        if existing {store.commit_rows(pos(1),vec![row(1,false)],vec![],vec![]).await?;}
        let sequence=if existing{2}else{1};let mut bad=row(sequence,true);bad.source_metadata["raw_payload"]["authoritative_input"]["capture_position"]=json!(pos(sequence+1));
        assert!(store.commit_rows(pos(sequence),vec![bad],vec![],vec![]).await.is_err());assert!(store.metadata("source_clock","tonghuashun_futures:AU2610").await?.is_none());
        store.commit_rows(pos(sequence),vec![row(sequence,true)],vec![],vec![]).await?;
        let proof=store.metadata("source_clock","tonghuashun_futures:AU2610").await?.unwrap();assert_eq!(proof["verified"],!existing);assert_eq!(proof["capture_position"],json!(pos(sequence)));
        store.close().await?;
    }Ok(())
}
