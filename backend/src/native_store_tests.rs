use super::*;
use crate::range_index::MINUTE_NS;

fn row(minute:i64,price:&str,volume:Option<&str>,revision:u64)->ImportBarRow {
    let ns=minute*MINUTE_NS;
    ImportBarRow {instrument_symbol:"SIGNED".into(),realtime_source_id:"source".into(),evidence_channel_id:"native".into(),interval_seconds:60,open_time_ns:ns,close_time_ns:ns+MINUTE_NS,
        open:price.into(),high:price.into(),low:price.into(),close:price.into(),volume:volume.map(str::to_owned),revision,received_sequence:Some(u64::MAX),state:"final".into(),finalized_at_ns:Some(ns+MINUTE_NS),
        source_observed_at_ns:123_456_789,received_at_ns:123_456_790,source_metadata:json!({"provider":"source","provider_symbol":"SIGNED","raw_payload":null}),evidence:Value::Null}
}
fn position(seq:u64)->CapturePosition {CapturePosition{epoch:"capture-epoch".into(),sequence:seq,digest:hex::encode(Sha256::digest(seq.to_be_bytes()))}}
fn batch(offset:u64,rows:Vec<ImportBarRow>)->ImportBatch<ImportBarRow> {ImportBatch {context:ImportContext {origin_id:"old-pg".into(),source_fingerprint:"original".into(),schema_version:SCHEMA_VERSION.into(),range_label:"bars".into(),legacy_cursor:Some(json!({"old_sequence":"2723916"})),expected_sha256:None},row_offset:offset,rows}}
fn snapshot(selection:BarSelection)->CanonicalSnapshotRequest {CanonicalSnapshotRequest {symbol:"SIGNED".into(),source_id:"source".into(),period:"1m".into(),selection,final_only:false,expected_version:None}}

#[test]fn fixture_upgrade_refuses_changed_cursor_or_nonempty_provider_state(){
    let p=position(4);let cp=json!({"position":p,"projector_version":"tracefang-projector-v2-exact","decoder":{"version":2,"quotes":{},"daily":{},"sessions":{}}});
    assert_eq!(fixture_decoder_upgrade(cp.clone(),&p).unwrap()["decoder"]["version"],3);
    assert!(fixture_decoder_upgrade(cp.clone(),&position(5)).is_err());
    for key in ["quotes","daily","sessions"] {let mut dirty=cp.clone();dirty["decoder"][key]=json!({"unknown":{}});assert!(fixture_decoder_upgrade(dirty,&p).is_err());}
    let mut dirty=cp;dirty["decoder"]["calendar_authorities"]=json!([{}]);assert!(fixture_decoder_upgrade(dirty,&p).is_err());
}

#[tokio::test]
async fn integer_trailing_zero_summaries_verify_across_node_roundtrip() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let stage=store.staging("trailing-zero").await?;
    // Each leaf has revision sum 10. Recompute decodes leaf nodes before
    // constructing their parent; verification derives them directly from facts.
    stage.import_bars(batch(0,(0..10).chain(64..74).map(|i|row(i,"10",Some("10"),1)).collect())).await?;
    let proof=stage.verify_staging().await?;
    assert_eq!(proof["fact_rows"],"20");
    let (_,values)=stage.range_aggregates("SIGNED","source",vec![(0,128*MINUTE_NS)],false,None).await?;
    assert_eq!(values[0].as_ref().unwrap().known_volume_sum.to_string(),"200");
    assert_eq!(values[0].as_ref().unwrap().revision_sum.to_string(),"20");
    store.close().await?;Ok(())
}

#[tokio::test]
async fn exact_signed_facts_cursor_and_reopen() -> Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    let wide="79228162514264337593543950335.0000000000000000000000000001";
    store.commit_rows(position(1),vec![row(-1,"-1690",Some(wide),u64::MAX),row(0,"0",None,1),row(1,"1.0000000000000000000000000001",Some("0.0000000000000000000000000001"),1)],vec![],vec![]).await?;
    let view=store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?;
    assert_eq!(view.bars.iter().map(|v|v["open_time_ns"].as_str().unwrap()).collect::<Vec<_>>(),["-60000000000","0","60000000000"]);
    assert_eq!(view.bars[0]["open"],"-1690");assert_eq!(view.bars[0]["volume"],wide);assert_eq!(view.bars[0]["revision"],u64::MAX.to_string());assert_eq!(view.bars[0]["received_sequence"],u64::MAX.to_string());
    assert_eq!(view.bars[0]["source_observed_at_ns"],"123456789");
    let (version,aggregate)=store.range_aggregates("SIGNED","source",vec![(-MINUTE_NS,2*MINUTE_NS)],false,None).await?;
    let aggregate=aggregate[0].as_ref().unwrap();assert_eq!(aggregate.volume(),None);assert_eq!(aggregate.known_volume_count,2);assert_eq!(aggregate.total_count,3);
    assert_eq!(aggregate.known_volume_sum.to_string(),"79228162514264337593543950335.0000000000000000000000000002");
    assert_eq!(version.committed_capture,Some(position(1)));
    let duplicate=store.commit_rows(position(1),vec![],vec![],vec![]).await?;assert_eq!(duplicate.version.commit_id,version.commit_id);
    assert!(store.commit_rows(position(3),vec![],vec![],vec![]).await.is_err());assert_eq!(store.version().await?.commit_id,version.commit_id);
    store.close().await?;assert!(store.version().await.is_err());drop(store);
    let reopened=Store::open(path)?;assert_eq!(reopened.version().await?.committed_capture,Some(position(1)));assert_eq!(reopened.canonical_snapshot(snapshot(BarSelection::Latest {count:3})).await?.bars[0]["volume"],wide);reopened.close().await?;Ok(())
}

#[tokio::test]
async fn batch_rollback_and_correctable_nodes_with_null_volume() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let rows=(0..4097).map(|minute|row(minute,if minute==64 {"1000"}else{"1"},Some("1"),1)).collect();
    store.commit_rows(position(1),rows,vec![],vec![]).await?;
    let mut invalid=row(10,"2",Some("1"),2);invalid.low="3".into();
    assert!(store.commit_rows(position(2),vec![row(64,"2",None,2),invalid],vec![],vec![]).await.is_err());
    let (_,a)=store.range_aggregates("SIGNED","source",vec![(0,4097*MINUTE_NS)],false,None).await?;assert_eq!(a[0].as_ref().unwrap().high.to_string(),"1000");assert_eq!(store.version().await?.committed_capture,Some(position(1)));
    store.commit_rows(position(2),vec![row(64,"-2",None,2),row(65,"3",Some("2"),2)],vec![],vec![]).await?;
    let (_,a)=store.range_aggregates("SIGNED","source",vec![(0,4097*MINUTE_NS)],false,None).await?;let a=a[0].as_ref().unwrap();assert_eq!(a.high.to_string(),"3");assert_eq!(a.low.to_string(),"-2");assert_eq!(a.known_volume_count,4096);assert_eq!(a.volume(),None);assert_eq!(a.known_volume_sum.to_string(),"4097");
    let full=store.canonical_snapshot(snapshot(BarSelection::Range {start_ns:0,end_ns:4097*MINUTE_NS,max_rows:10})).await;assert!(full.unwrap_err().to_string().contains("max_rows"));
    let mut reconnected=row(64,"-3",None,1);reconnected.received_sequence=Some(1);store.commit_rows(position(3),vec![reconnected],vec![],vec![]).await?;
    let corrected=store.canonical_snapshot(snapshot(BarSelection::Range {start_ns:64*MINUTE_NS,end_ns:65*MINUTE_NS,max_rows:1})).await?;
    assert_eq!(corrected.bars[0]["revision"],"3");assert_eq!(corrected.bars[0]["low"],"-3");
    assert_eq!(store.version().await?.committed_capture,Some(position(3)));store.close().await?;Ok(())
}

#[tokio::test]
async fn legacy_import_isolated_idempotent_verified_and_not_native_cursor() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;let staging=store.staging("legacy-test").await?;
    let imported=staging.import_bars(batch(0,vec![row(0,"1",None,1),row(1,"2",Some("1"),1)])).await?;assert_eq!(imported.accepted,2);assert_eq!(imported.version.committed_capture,None);
    assert!(store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?.bars.is_empty());
    let duplicate=staging.import_bars(batch(0,vec![row(0,"1",None,1),row(1,"2",Some("1"),1)])).await?;assert_eq!(duplicate.unchanged,2);assert_eq!(duplicate.version.commit_id,imported.version.commit_id);
    assert!(staging.import_bars(batch(0,vec![row(0,"9",None,2)])).await.is_err());
    assert!(store.activate_staging("legacy-test",json!({"complete":true,"index_verified":true})).await.is_err());
    staging.import_metadata(ImportBatch {context:ImportContext {origin_id:"old-pg".into(),source_fingerprint:"original".into(),schema_version:SCHEMA_VERSION.into(),range_label:"config".into(),legacy_cursor:None,expected_sha256:None},row_offset:0,rows:vec![ImportMetadataRow {namespace:"watchlist".into(),key:"default".into(),value:json!(["SIGNED"]),evidence:json!({"table":"watchlist_items"})},ImportMetadataRow {namespace:"capabilities".into(),key:"source:SIGNED".into(),value:json!(["minute_history"]),evidence:Value::Null}]}).await?;
    let manifest=staging.verify_staging().await?;assert_eq!(manifest["fact_rows"],"2");store.activate_staging("legacy-test",manifest).await?;
    let current=store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?;assert_eq!(current.bars.len(),2);assert_eq!(current.version.committed_capture,None);assert_eq!(current.version.active_generation,"legacy-test");
    assert_eq!(store.metadata("watchlist","default").await?,Some(json!(["SIGNED"])));assert_eq!(current.capabilities,json!(["minute_history"]));
    assert!(store.import_bars(batch(0,vec![])).await.is_err());store.close().await?;Ok(())
}

fn quote(event:&str,observed:i64)->ImportQuoteRow {ImportQuoteRow {instrument_symbol:"SIGNED".into(),realtime_source_id:"source".into(),evidence_channel_id:"native".into(),event_id:event.into(),price:"-1".into(),bid:None,ask:None,volume:None,observed_at_ns:observed,received_at_ns:observed,source_sequence:Some(1),source_metadata:json!({"provider":"source","provider_symbol":"SIGNED"}),statistics:Value::Null,is_supplement:false,evidence:Value::Null}}

#[tokio::test]
async fn canonical_quote_scan_is_complete_scoped_and_pinned_across_writer_commit()->Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("quote-scan.redb"))?;
    let mut a=quote("a",-123_456_789);a.price="79228162514264337593543950335.0000000000000000000000000001".into();a.source_sequence=Some(u64::MAX);
    let b=quote("b",200);let mut c=quote("c",300);c.realtime_source_id="other".into();
    store.commit_rows(position(1),vec![],vec![a,b,c],vec![]).await?;let pinned=store.version().await?;
    let latest=store.canonical_latest_quote_rows(pinned.clone()).await?;assert_eq!(latest.len(),2);
    let rows=std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));let collected=rows.clone();let writer=store.clone();let handle=tokio::runtime::Handle::current();let mut wrote=false;
    let summary=store.canonical_quote_scan("".into(),"".into(),pinned.clone(),1,move|batch|{
        assert_eq!(batch.len(),1);collected.lock().unwrap().extend(batch);
        if !wrote{wrote=true;handle.block_on(writer.commit_rows(position(2),vec![],vec![quote("during",400)],vec![]))?;}Ok(())
    }).await?;
    assert_eq!(summary["row_count"],"3");assert_eq!(summary["version"],json!(pinned));assert_eq!(summary["complete"],true);
    let rows=rows.lock().unwrap();assert_eq!(rows.len(),3);assert!(rows.iter().all(|row|row.event_id!="during"));
    let wide=rows.iter().find(|row|row.event_id=="a").unwrap();assert_eq!(wide.source_sequence,Some(u64::MAX));assert_eq!(wide.observed_at_ns,-123_456_789);assert_eq!(wide.price,"79228162514264337593543950335.0000000000000000000000000001");assert_eq!(wide.volume,None);assert_eq!(wide.source_metadata["raw_payload"]["capture_epoch"],position(1).epoch);assert_eq!(wide.source_metadata["raw_payload"]["capture_sequence"],"1");assert_eq!(wide.source_metadata["raw_payload"]["capture_digest"],position(1).digest);drop(rows);
    assert!(store.canonical_quote_scan("".into(),"".into(),pinned.clone(),1,|_|anyhow::bail!("stale view callback must not run")).await.is_err());assert!(store.canonical_latest_quote_rows(pinned).await.is_err());
    let current=store.version().await?;let scoped=store.canonical_quote_scan("source".into(),"SIGNED".into(),current.clone(),2,|rows|{assert!(rows.iter().all(|row|row.realtime_source_id=="source"&&row.instrument_symbol=="SIGNED"));Ok(())}).await?;assert_eq!(scoped["row_count"],"3");
    let empty=store.canonical_quote_scan("missing".into(),"SIGNED".into(),current.clone(),1,|_|anyhow::bail!("empty scope callback must not run")).await?;assert_eq!(empty["row_count"],"0");assert_eq!(empty["complete"],true);
    assert!(store.canonical_quote_scan("source".into(),"".into(),current.clone(),1,|_|Ok(())).await.is_err());assert!(store.canonical_quote_scan("".into(),"".into(),current,1001,|_|Ok(())).await.is_err());store.close().await?;Ok(())
}

#[tokio::test]
async fn canonical_quote_scan_obeys_byte_batches_and_callback_failure_without_complete_summary()->Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("quote-bounds.redb"))?;
    let large=(0..3).map(|i|{let mut row=quote(&format!("large-{i}"),i);let text="x".repeat(900*1024);row.source_metadata["evidence"]=json!(text);row.statistics=json!({"original":text});row.evidence=json!({"original":text});row}).collect();
    store.commit_rows(position(1),vec![],large,vec![]).await?;let wanted=store.version().await?;
    let complete=store.canonical_quote_scan("".into(),"".into(),wanted.clone(),1000,|rows|{assert_eq!(rows.len(),1);assert!(serde_json::to_vec(&rows)?.len()<=4*1024*1024);Ok(())}).await?;assert_eq!(complete["row_count"],"3");assert_eq!(complete["batches"],"3");
    assert!(store.canonical_quote_scan("".into(),"".into(),wanted,1,|_|anyhow::bail!("bounded callback failed")).await.unwrap_err().to_string().contains("bounded callback failed"));store.close().await?;Ok(())
}

#[tokio::test]
async fn materialized_resume_is_bounded_cancelable_and_detached_from_read_view()->Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    store.commit_rows(position(1),(0..10).map(|i|row(i,"1",None,1)).collect(),vec![],vec![]).await?;
    let view=store.canonical_snapshot(snapshot(BarSelection::Latest{count:1})).await?;
    let series:SeriesVersion=serde_json::from_value(view.coverage["series_version"].clone())?;
    let proof=ScanResume {after_ns:0,series_generation:series.series_generation,correction_epoch:series.correction_epoch,append_watermark_ns:Some(series.append_watermark_ns)};
    let request=CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:10*MINUTE_NS,final_only:false,expected_version:Some(view.version.clone())};
    let before=hex::encode(Sha256::digest(std::fs::read(&path)?));
    let too_many=store.materialize_tail(request.clone(),crate::periods::Period::M1,None,proof.clone(),8,16*1024*1024,Arc::new(||false)).await.unwrap_err();
    assert!(too_many.downcast_ref::<MaterializedTailTooLarge>().is_some());
    assert!(store.materialize_tail(request.clone(),crate::periods::Period::M1,None,proof.clone(),10,10,Arc::new(||false)).await.unwrap_err().downcast_ref::<MaterializedTailTooLarge>().is_some());
    assert!(store.materialize_tail(request.clone(),crate::periods::Period::M1,None,proof.clone(),10,16*1024*1024,Arc::new(||true)).await.unwrap_err().to_string().contains("quant_cancelled"));
    let input=store.materialize_tail(request.clone(),crate::periods::Period::M1,None,proof.clone(),10,16*1024*1024,Arc::new(||false)).await?;
    assert_eq!(input.summary.row_count,9);assert_eq!(input.summary.version,view.version);assert!(input.batches[0].context.is_some());
    let mut empty_proof=proof.clone();empty_proof.after_ns=9*MINUTE_NS;
    let empty=store.materialize_tail(request.clone(),crate::periods::Period::M1,None,empty_proof,10,16*1024*1024,Arc::new(||false)).await?;
    assert_eq!(empty.summary.row_count,0);assert_eq!(empty.batches.len(),1);assert!(empty.batches[0].context.is_some());
    assert_eq!(before,hex::encode(Sha256::digest(std::fs::read(&path)?))); // No publish/fsync.
    store.commit_rows(position(2),vec![row(0,"2",None,2)],vec![],vec![]).await?;
    let mut changed=request;changed.expected_version=Some(store.version().await?);
    assert!(store.materialize_tail(changed,crate::periods::Period::M1,None,proof,10,16*1024*1024,Arc::new(||false)).await.unwrap_err().to_string().contains("quant_resume_invalid"));
    store.close().await?; // Owned rows/context remain usable after all readers close.
    assert_eq!(input.batches.iter().flat_map(|b|&b.rows).count(),9);Ok(())
}

#[tokio::test]
async fn replay_savepoint_restores_exact_prefix_and_invalidates_later_checkpoints()->Result<()> {
    let dir=tempfile::tempdir()?;let live=Store::open(dir.path().join("live.redb"))?;
    assert!(live.create_replay_savepoint().await.is_err());live.close().await?;
    let path=dir.path().join("replay.redb");let store=Store::open_replay(&path)?;
    let frame=|seq,price:&str|->Result<_>{Ok(ProjectionCommit {position:position(seq),bars:vec![serde_json::to_value(row(0,price,None,1))?],quotes:vec![],errors:vec![],decoder_state:None})};
    assert!(store.create_replay_savepoint().await.is_err());
    store.commit_replay_frames(vec![frame(1,"1")?]).await?;
    let first=store.create_replay_savepoint().await?;assert_eq!(first.version.committed_capture,Some(position(1)));
    store.commit_replay_frames(vec![frame(2,"2")?]).await?;let second=store.create_replay_savepoint().await?;
    store.commit_replay_frames(vec![frame(3,"3")?]).await?;let original=store.version().await?;
    assert!(store.restore_replay_savepoint(first.savepoint_id,original.clone()).await.is_err());assert_eq!(store.version().await?,original);
    let restored=store.restore_replay_savepoint(first.savepoint_id,first.version.clone()).await?;
    assert_eq!(restored.invalidated_later_ids,vec![second.savepoint_id]);assert_eq!(restored.version,first.version);
    assert_eq!(store.canonical_snapshot(snapshot(BarSelection::Latest{count:1})).await?.bars[0]["close"],"1");
    assert!(store.restore_replay_savepoint(second.savepoint_id,second.version).await.is_err());
    store.close().await?;drop(store);let store=Store::open_replay(path)?;
    assert_eq!(store.version().await?,first.version);assert_eq!(store.delete_replay_savepoints(vec![first.savepoint_id]).await?,vec![first.savepoint_id]);
    for _ in 0..4 {store.create_replay_savepoint().await?;}assert!(store.create_replay_savepoint().await.is_err());
    store.close().await?;Ok(())
}

#[tokio::test]
async fn quote_timeline_is_actual_application_order_under_clock_rollback() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    store.commit_rows(position(1),vec![],vec![quote("a",200)],vec![]).await?;
    store.commit_rows(position(2),vec![],vec![quote("b",100)],vec![]).await?;
    let rows=store.timeline("SIGNED",&["source".into()],None,10).await?;assert_eq!(rows.iter().map(|r|r["event_id"].as_str().unwrap()).collect::<Vec<_>>(),["b","a"]);
    assert_eq!(rows[0]["applied_capture"]["sequence"],"2");assert_eq!(rows[0]["application_order"],"2");assert_eq!(rows[0]["observed_at_ns"],"100");
    store.commit_rows(position(3),vec![],vec![quote("a",200)],vec![]).await?;assert_eq!(store.timeline("SIGNED",&["source".into()],None,10).await?.len(),2);
    assert_eq!(store.timeline("SIGNED",&["source".into()],Some(2),10).await?[0]["event_id"],"a");store.close().await?;Ok(())
}

#[tokio::test]
async fn accepted_exponent_boundaries_remain_closed_through_codec_and_reopen() -> Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    let values=["-1e-4096","1e-4096","-1e4096","1e4096"];
    for value in values {assert!(crate::domain::Decimal::from_source_str(value).is_ok());}
    store.commit_rows(position(1),values.iter().enumerate().map(|(i,value)|row(i as i64,value,None,1)).collect(),vec![],vec![]).await?;
    let bars=store.canonical_snapshot(snapshot(BarSelection::Latest {count:4})).await?.bars;
    for (bar,source) in bars.iter().zip(values) {let normalized=crate::domain::Decimal::from_source_str(source)?.to_string();assert_eq!(bar["close"],normalized);assert_eq!(crate::domain::Decimal::from_str_exact(&normalized)?,crate::domain::Decimal::from_source_str(source)?);}
    store.close().await?;drop(store);let reopened=Store::open(&path)?;assert_eq!(reopened.canonical_snapshot(snapshot(BarSelection::Latest {count:4})).await?.bars.iter().map(|v|v["close"].clone()).collect::<Vec<_>>(),bars.iter().map(|v|v["close"].clone()).collect::<Vec<_>>());reopened.close().await?;Ok(())
}

#[tokio::test]
async fn equal_source_time_receive_clock_rollback_updates_current_quote() -> Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    let mut first=quote("a",200);first.received_at_ns=300;store.commit_rows(position(1),vec![],vec![first],vec![]).await?;
    let mut next=quote("b",200);next.received_at_ns=100;next.price="-2".into();store.commit_rows(position(2),vec![],vec![next],vec![]).await?;
    assert_eq!(store.latest_quotes().await?[0]["last"],"-2");store.close().await?;drop(store);let reopened=Store::open(path)?;assert_eq!(reopened.latest_quotes().await?[0]["last"],"-2");reopened.close().await?;Ok(())
}

#[tokio::test(flavor="multi_thread",worker_threads=2)]
async fn streaming_scan_keeps_one_mvcc_while_writer_commits() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    store.commit_rows(position(1),(0..600).map(|i|row(i,"1",Some("1"),1)).collect(),vec![],vec![]).await?;
    let first_version=store.version().await?;let scanner=store.clone();let (started_tx,started_rx)=tokio::sync::oneshot::channel();let (resume_tx,resume_rx)=std::sync::mpsc::channel();
    let output=Arc::new(Mutex::new(Vec::new()));let captured=output.clone();
    let task=tokio::spawn(async move {let mut started_tx=Some(started_tx);scanner.canonical_scan(CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:0,end_ns:600*MINUTE_NS,final_only:true,expected_version:None},100,move|batch| {
        if let Some(sender)=started_tx.take() {let _=sender.send(());resume_rx.recv()?;}
        captured.lock().unwrap().push((batch.version.commit_id,batch.rows));Ok(())
    }).await});
    started_rx.await?;store.commit_rows(position(2),vec![row(599,"9",Some("2"),2)],vec![],vec![]).await?;resume_tx.send(())?;
    let summary=task.await??;assert!(summary.complete);assert_eq!(summary.row_count,600);assert_eq!(summary.version.commit_id,first_version.commit_id);
    let rows=output.lock().unwrap();assert_eq!(rows.len(),6);assert!(rows.iter().all(|r|r.0==first_version.commit_id));assert_eq!(rows[5].1[99].close,"1");drop(rows);
    assert_eq!(store.canonical_snapshot(snapshot(BarSelection::Latest {count:1})).await?.bars[0]["close"],"9");store.close().await?;Ok(())
}

#[tokio::test]
async fn source_prefix_generation_changes_only_for_actual_corrections() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    store.commit_rows(position(1),vec![row(0,"1",None,1)],vec![],vec![]).await?;
    let coverage=store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?.coverage;assert_eq!(coverage["series_version"]["correction_epoch"],"0");
    store.commit_rows(position(2),vec![row(1,"2",None,1)],vec![],vec![]).await?;
    let appended=store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?.coverage;assert_eq!(appended["series_version"]["correction_epoch"],"0");assert_eq!(appended["series_version"]["series_generation"],coverage["series_version"]["series_generation"]);
    store.commit_rows(position(3),vec![row(0,"3",None,2)],vec![],vec![]).await?;
    let changed=store.canonical_snapshot(snapshot(BarSelection::Latest {count:10})).await?.coverage;assert_eq!(changed["series_version"]["correction_epoch"],"1");store.close().await?;Ok(())
}

#[tokio::test]
async fn inactive_index_version_upgrade_preserves_facts_and_readonly_file() -> Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let store=Store::open(&path)?;
    let stage=store.staging("index-upgrade").await?;
    stage.import_bars(batch(0,(0..10).chain(64..74).map(|i|row(i,"10",Some("10"),1)).collect())).await?;
    let original=stage.verify_index().await?;
    stage.write(|tx,version| {
        let definition:TableDefinition<&[u8],&[u8]>=TableDefinition::new("minute_range_nodes_v1");
        let mut prefix=Vec::new();component(&mut prefix,&version.active_generation);let end=prefix_end(&prefix)?;
        let mut table=tx.open_table(definition)?;let bytes=table.range(prefix.as_slice()..end.as_slice())?.map(|entry|->Result<_>{let(k,v)=entry?;let mut b=v.value().to_vec();b[0]=2;Ok((k.value().to_vec(),b))}).collect::<Result<Vec<_>>>()?;
        for(key,value)in bytes {table.insert(key.as_slice(),value.as_slice())?;}
        version.aggregation_version="tracefang-range-index-v2".into();Ok(())
    }).await?;
    assert!(stage.range_aggregates("SIGNED","source",vec![(0,128*MINUTE_NS)],false,None).await.is_err());
    let repair=stage.rebuild_staging_index().await?;
    let proof=stage.verify_staging().await?;
    assert_eq!(repair["fact_codec_sha256"],original["fact_codec_sha256"]);
    assert_eq!(proof["fact_codec_sha256"],original["fact_codec_sha256"]);
    assert_eq!(proof["aggregation_version"],AGGREGATION_VERSION);
    assert!(store.rebuild_staging_index().await.is_err());
    store.close().await?;drop(stage);drop(store);
    let before=hex::encode(Sha256::digest(std::fs::read(&path)?));
    let ro=Store::open_read_only(&path)?;let stage=ro.read_generation("index-upgrade").await?;
    assert_eq!(stage.verify_index().await?["fact_codec_sha256"],original["fact_codec_sha256"]);
    assert_eq!(stage.canonical_snapshot(snapshot(BarSelection::Latest{count:30})).await?.bars.len(),20);
    assert!(stage.set_metadata("test","key",json!(true)).await.is_err());
    assert!(stage.rebuild_staging_index().await.is_err());
    ro.close().await?;drop(stage);drop(ro);
    assert_eq!(before,hex::encode(Sha256::digest(std::fs::read(path)?)));Ok(())
}

#[tokio::test]
async fn event_identity_lookup_keeps_superseded_exact_events() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    store.commit_rows(position(1),vec![],vec![quote("a",200),quote("b",100)],vec![]).await?;
    let keys=["a","b","missing"].map(|event|CanonicalQuoteKey {source_id:"source".into(),symbol:"SIGNED".into(),event_id:event.into()});
    let(version,rows)=store.lookup_quotes(keys.to_vec()).await?;
    assert_eq!(version.committed_capture,Some(position(1)));assert_eq!(rows[0].as_ref().unwrap().observed_at_ns,200);
    assert_eq!(rows[1].as_ref().unwrap().observed_at_ns,100);assert!(rows[2].is_none());
    let summary=store.generation_summary().await?;assert_eq!(summary["counts"]["quote_events"],"2");assert_eq!(summary["counts"]["quote_event_identities"],"2");
    store.close().await?;Ok(())
}

#[tokio::test]
async fn external_date_only_facts_use_real_receive_availability() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let record=ExternalFactRecord {scope:ExternalFactScope {instrument_symbol:"SIGNED".into(),market_source_id:None},kind:"volatility".into(),source:"cboe".into(),record_id:"date-only".into(),revision:1,observed_at_ns:None,published_at_ns:None,received_at_ns:Some(100),value:json!({"trading_date":"2026-10-01","value":"10.0000000000000000000000000001"}),unavailable_reason:None,provenance:json!({"clock":"trading_date_only"})};
    let first=store.commit_external_facts(vec![record.clone()]).await?;
    assert_eq!(store.commit_external_facts(vec![record.clone()]).await?.commit_id,first.commit_id);
    for (cutoff,expected) in [(99,0),(100,1)] {
        let output=Arc::new(Mutex::new(Vec::new()));let captured=output.clone();
        store.canonical_scan(CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:0,end_ns:cutoff,final_only:false,expected_version:None},10,move|batch|{if let Some(context)=batch.context {captured.lock().unwrap().extend(context.external_facts);}Ok(())}).await?;
        let values=output.lock().unwrap();assert_eq!(values.len(),expected);if expected>0 {assert!(values[0].observed_at_ns.is_none());assert_eq!(values[0].received_at_ns,Some(100));}
    }
    let mut conflicting=record;conflicting.value=json!({"value":"11"});assert!(store.commit_external_facts(vec![conflicting]).await.is_err());assert_eq!(store.version().await?.commit_id,first.commit_id);
    store.close().await?;Ok(())
}

#[tokio::test]
async fn calendar_final_only_never_discards_unconfirmed_components() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let mut forming=row(1,"2",None,1);forming.state="forming".into();forming.finalized_at_ns=None;
    store.commit_rows(position(1),vec![row(0,"1",Some("1"),1),forming],vec![],vec![]).await?;
    let request=CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:0,end_ns:3*MINUTE_NS,final_only:true,expected_version:None};
    let summary=store.canonical_calendar_scan(request,crate::periods::Period::M3,None,10,None,|_|Ok(())).await?;
    assert_eq!(summary.row_count,0);
    let mut request=snapshot(BarSelection::Latest{count:10});request.period="3m".into();request.final_only=true;
    assert!(store.canonical_period_page(request,crate::periods::Period::M3,None).await?.bars.is_empty());
    store.close().await?;Ok(())
}

#[tokio::test]
async fn replay_batch_matches_per_frame_business_revision_and_rolls_back_gaps() -> Result<()> {
    let dir=tempfile::tempdir()?;let batched=Store::open_replay(dir.path().join("batched.redb"))?;let sequential=Store::open_replay(dir.path().join("sequential.redb"))?;
    let frames=(1..=4).map(|seq|ProjectionCommit {position:position(seq),bars:vec![serde_json::to_value(row(if seq<=2{0}else{1},&seq.to_string(),Some("10"),1)).unwrap()],quotes:vec![],errors:vec![],decoder_state:None}).collect::<Vec<_>>();
    let last=batched.commit_replay_frames(frames.clone()).await?;for frame in &frames {sequential.commit_replay_frames(vec![frame.clone()]).await?;}
    assert_eq!(last.version.commit_id,4);assert_eq!(last.version.committed_capture,Some(position(4)));
    let a=batched.canonical_snapshot(snapshot(BarSelection::Latest{count:10})).await?;let b=sequential.canonical_snapshot(snapshot(BarSelection::Latest{count:10})).await?;assert_eq!(a.bars,b.bars);
    let(_,a)=batched.range_aggregates("SIGNED","source",vec![(0,128*MINUTE_NS)],false,None).await?;let(_,b)=sequential.range_aggregates("SIGNED","source",vec![(0,128*MINUTE_NS)],false,None).await?;assert_eq!(serde_json::to_value(a)?,serde_json::to_value(b)?);
    let gap=vec![ProjectionCommit {position:position(5),bars:vec![serde_json::to_value(row(2,"5",None,1))?],quotes:vec![],errors:vec![],decoder_state:None},ProjectionCommit {position:position(7),bars:vec![],quotes:vec![],errors:vec![],decoder_state:None}];assert!(batched.commit_replay_frames(gap).await.is_err());assert_eq!(batched.version().await?.committed_capture,Some(position(4)));assert_eq!(batched.generation_summary().await?["counts"]["bar_rows"],"2");
    assert!(batched.staging("import").await.is_err());assert!(batched.commit_rows(position(5),vec![],vec![],vec![]).await.is_err());
    batched.close().await?;sequential.close().await?;drop(batched);drop(sequential);
    assert!(Store::open(dir.path().join("batched.redb")).is_err());let reopened=Store::open_replay(dir.path().join("batched.redb"))?;assert_eq!(reopened.version().await?.committed_capture,Some(position(4)));reopened.close().await?;Ok(())
}

#[tokio::test]
async fn bounded_calendar_labels_and_replay_as_of_never_fabricate_full_buckets() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;store.commit_rows(position(1),(0..6).map(|i|row(i,"10",Some("1"),1)).collect(),vec![],vec![]).await?;
    let mut request=snapshot(BarSelection::Latest{count:10});request.period="3m".into();
    let page=store.canonical_period_page_at(request,crate::periods::Period::M3,None,4*MINUTE_NS).await?;
    assert_eq!(page.bars[0]["state"],"final");assert_eq!(page.bars[1]["state"],"provisional");assert!(page.bars[1]["finalized_at_ns"].is_null());
    let output=Arc::new(Mutex::new(Vec::new()));let rows=output.clone();let request=CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:MINUTE_NS,end_ns:6*MINUTE_NS,final_only:true,expected_version:None};
    let summary=store.canonical_calendar_scan(request,crate::periods::Period::M3,None,10,None,move|batch|{rows.lock().unwrap().extend(batch.rows);Ok(())}).await?;
    assert_eq!(summary.row_count,1);let output=output.lock().unwrap();assert_eq!(output[0].open_time_ns,3*MINUTE_NS);assert_eq!(output[0].volume.as_deref(),Some("3"));drop(output);store.close().await?;Ok(())
}

#[tokio::test]
async fn daily_and_weekly_night_pages_have_exclusive_display_cursors() -> Result<()> {
    use crate::periods::{MarketSchedule,TradingDayRule,TradingSession,Period};
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let schedule=MarketSchedule {authority:None,time_zone:"Asia/Shanghai".into(),trading_day_rule:Some(TradingDayRule::Shfe),reference:None,sessions:(1..=5).map(|weekday|TradingSession {weekday,open:"21:00".into(),close:"02:30".into(),close_day_offset:1}).collect()};
    let at=|s:&str|s.parse::<chrono::DateTime<chrono::Utc>>().unwrap().timestamp_nanos_opt().unwrap();
    let times=["2026-08-03T13:00:00Z","2026-08-10T13:00:00Z","2026-08-11T13:00:00Z"];
    store.commit_rows(position(1),times.iter().map(|s|row(at(s)/MINUTE_NS,"10",Some("1"),1)).collect(),vec![],vec![]).await?;
    for period in [Period::D1,Period::W1] {
        let mut request=snapshot(BarSelection::Latest{count:1});request.period=period.as_str().into();let first=store.canonical_period_page(request.clone(),period,Some(schedule.clone())).await?;
        let label=first.bars[0]["open_time_ns"].as_str().unwrap().parse()?;request.selection=BarSelection::Before{before_ns:label,count:10};
        let second=store.canonical_period_page(request,period,Some(schedule.clone())).await?;assert!(!second.bars.is_empty());assert!(second.bars.iter().all(|b|b["open_time_ns"].as_str().unwrap().parse::<i64>().unwrap()<label));
    }store.close().await?;Ok(())
}

#[tokio::test]
async fn every_derived_period_excludes_closed_minutes_with_exact_same_mvcc_evidence()->Result<()> {
    use crate::periods::{MarketSchedule,TradingDayRule,TradingSession,Period};
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;
    let schedule=MarketSchedule {authority:None,time_zone:"America/New_York".into(),trading_day_rule:Some(TradingDayRule::SessionEnd),reference:Some("fixed declared OTC fixture".into()),sessions:(0..=4).map(|weekday|TradingSession{weekday,open:"18:00".into(),close:"17:00".into(),close_day_offset:1}).collect()};
    let at=|s:&str|s.parse::<chrono::DateTime<chrono::Utc>>().unwrap().timestamp_nanos_opt().unwrap();
    let valid=["2026-10-02T20:59:00Z","2026-10-04T22:00:00Z"];
    let invalid=["2026-10-02T21:00:00Z","2026-10-03T02:00:00Z"];
    let rows=valid.iter().map(|s|row(at(s)/MINUTE_NS,"2",Some("1"),1)).chain(invalid.iter().map(|s|row(at(s)/MINUTE_NS,"9000",Some("1000"),1))).collect();
    store.commit_rows(position(1),rows,vec![],vec![]).await?;let version=store.version().await?;
    assert_eq!(store.canonical_snapshot(snapshot(BarSelection::Latest{count:10})).await?.bars.len(),4);
    let cutoff=at("2026-10-06T00:00:00Z");
    for period in Period::ALL.into_iter().filter(|period|!period.is_base()) {
        let page=store.canonical_period_page_at(snapshot(BarSelection::Latest{count:100}),period,Some(schedule.clone()),cutoff).await?;
        assert_eq!(page.version,version);assert!(!page.bars.is_empty(),"{} omitted legitimate Friday/Sunday minutes",period.as_str());
        let components=page.bars.iter().map(|v|v["raw_payload"]["component_count"].as_str().unwrap().parse::<u64>().unwrap()).sum::<u64>();
        assert_eq!(components,2,"{} member count",period.as_str());assert!(page.bars.iter().all(|v|v["high"]=="2"));
        assert_eq!(page.coverage["calendar_projection"]["excluded_outside_schedule"],"2");assert_eq!(page.coverage["calendar_projection"]["complete"],false);
        assert_eq!(page.coverage["calendar_projection"]["earliest_ns"],at(invalid[0]).to_string());assert_eq!(page.coverage["calendar_projection"]["latest_ns"],at(invalid[1]).to_string());
        let out=Arc::new(Mutex::new(Vec::new()));let target=out.clone();
        let summary=store.canonical_calendar_scan(CanonicalScanRequest{symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:cutoff,final_only:false,expected_version:Some(version.clone())},period,Some(schedule.clone()),10,None,move|batch|{target.lock().unwrap().extend(batch.rows);Ok(())}).await?;
        assert_eq!(summary.row_count,page.bars.len() as u64);assert!(out.lock().unwrap().iter().all(|row|row.high=="2"));
    }
    store.close().await?;
    let empty=Store::open(dir.path().join("only-closed.redb"))?;empty.commit_rows(position(1),invalid.iter().map(|s|row(at(s)/MINUTE_NS,"9000",None,1)).collect(),vec![],vec![]).await?;
    for period in Period::ALL.into_iter().filter(|period|!period.is_base()) {let page=empty.canonical_period_page_at(snapshot(BarSelection::Latest{count:10}),period,Some(schedule.clone()),cutoff).await?;assert!(page.bars.is_empty());assert_eq!(page.coverage["calendar_projection"]["excluded_outside_schedule"],"2");}
    empty.close().await?;Ok(())
}

#[tokio::test]
async fn same_mvcc_context_provides_closed_exact_multi_timeframes() -> Result<()> {
    let dir=tempfile::tempdir()?;let store=Store::open(dir.path().join("facts.redb"))?;store.commit_rows(position(1),(0..24*60).map(|i|row(i,"-1690",None,1)).collect(),vec![],vec![]).await?;
    let request=CanonicalScanRequest {symbol:"SIGNED".into(),source_id:"source".into(),interval_seconds:60,start_ns:0,end_ns:24*60*MINUTE_NS,final_only:false,expected_version:None};
    let schedule=crate::periods::MarketSchedule {authority:None,time_zone:"UTC".into(),trading_day_rule:None,reference:None,sessions:vec![]};
    let batch=store.canonical_scan_context(request,Some(schedule)).await?;let fact=batch.context.unwrap().external_facts.into_iter().find(|f|f.kind=="multi_timeframe").unwrap();
    assert!(fact.unavailable_reason.is_none());assert_eq!(fact.source,"source");assert_eq!(fact.provenance["snapshot_version"],serde_json::to_value(batch.version)?);
    assert_eq!(fact.value["horizons"][0]["bars"].as_array().unwrap().len(),20);assert_eq!(fact.value["horizons"][0]["bars"][0]["close"],"-1690");assert!(fact.value["horizons"][0]["bars"][0]["volume"].is_null());assert_eq!(fact.value["horizons"][1]["bars"].as_array().unwrap().len(),1);
    store.close().await?;Ok(())
}
