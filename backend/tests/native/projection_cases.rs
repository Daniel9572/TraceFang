#[path="../../src/store.rs"] mod store;
use serde_json::json;
use tracefang_core::persistence_contract::{CapturePosition,ProjectionCommit,CanonicalScanRequest};
fn position(seq:u64)->CapturePosition{CapturePosition{epoch:"isolated-projection-test".into(),sequence:seq,digest:format!("{seq:064x}")}}
#[tokio::test]
async fn projections_and_cursor_commit_together_without_losing_precision()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let instrument=json!({"symbol":"XAU/USD","asset_class":"spot","base":"XAU","quote":"USD","venue":"OTC"});
    db.initialize_instruments(&[instrument.clone()],&["XAU/USD".into()]).await?;
    assert_eq!(db.watchlist().await?,vec!["XAU/USD"]);db.set_route("XAU/USD","jin10_client").await?;
    let exact="2731.123456789012345678901234567890123456789";
    let mut quote=json!({"instrument":instrument,"event_id":"frame-1","last":exact,"open":null,"high":null,"low":null,"volume":null,"change":null,"change_percent":null,
        "source":{"provider":"jin10_web","provider_symbol":"XAUUSD.GOODS","observed_at":"2026-09-30T01:00:00.000000123Z","received_at":"2026-09-30T01:00:01.000000456Z",
            "raw_payload":{"capture_epoch":"isolated-projection-test","capture_sequence":"1","capture_digest":position(1).digest,"capture_accepted_at_ns":"1790729999000000000"}}});
    let bar=json!({"instrument":instrument,"interval":60,"open_time":"2026-09-30T01:00:00Z","open":exact,"high":"2732","low":"2730","close":exact,"volume":null,"state":"final","revision":"2","finalized_at":"2026-09-30T01:01:00Z","evidence_channel_id":"jin10_local",
        "source":{"provider":"jin10_client","provider_symbol":"XAUUSD.GOODS","observed_at":"2026-09-30T01:00:59Z","received_at":"2026-09-30T01:01:00Z","raw_payload":{"test":true}}});
    let first=db.commit_projection(ProjectionCommit{position:position(1),quotes:vec![quote.clone()],bars:vec![bar],errors:vec![],decoder_state:None}).await?;
    assert_eq!(first.version.committed_capture,Some(position(1)));assert_eq!(db.bars_before("XAU/USD","jin10_client",60,None,2).await?.len(),1);
    let retry=db.commit_projection(ProjectionCommit{position:position(1),quotes:vec![quote.clone()],bars:vec![],errors:vec![],decoder_state:None}).await?;
    assert_eq!(retry.version,first.version);assert_eq!(db.generation_summary().await?["counts"]["quote_events"],"1");
    let before=db.latest_quotes().await?[0].clone();assert_eq!(before["last"],exact);assert_eq!(before["observed_at_ns"],"2026-09-30T01:00:00.000000123Z".parse::<chrono::DateTime<chrono::Utc>>()?.timestamp_nanos_opt().unwrap().to_string());
    let mut supplement=quote.clone();supplement["volume"]=json!("100.0000000000000000000000000001");
    supplement["source"]["raw_payload"]["observation_kind"]=json!("supplement");supplement["source"]["raw_payload"]["capture_sequence"]=json!("2");
    supplement["source"]["raw_payload"]["capture_digest"]=json!(position(2).digest);supplement["source"]["raw_payload"]["capture_accepted_at_ns"]=json!("1790729999000000999");
    db.commit_projection(ProjectionCommit{position:position(2),quotes:vec![supplement],bars:vec![],errors:vec![],decoder_state:None}).await?;
    let latest=db.latest_quotes().await?[0].clone();assert_eq!(latest["volume"],"100.0000000000000000000000000001");assert_eq!(latest["last"],exact);
    assert_eq!(latest["applied_capture"],before["applied_capture"]);assert_eq!(latest["raw_payload"]["capture_accepted_at_ns"],before["raw_payload"]["capture_accepted_at_ns"]);
    assert_eq!(latest["raw_payload"]["statistics_evidence"]["applied_capture"]["sequence"],"2");
    assert_eq!(db.generation_summary().await?["counts"]["quote_events"],"1");
    let version=db.version().await?;quote["event_id"]=json!("invalid");quote["last"]=json!("not-a-number");
    assert!(db.commit_projection(ProjectionCommit{position:position(3),quotes:vec![quote],bars:vec![],errors:vec![],decoder_state:None}).await.is_err());
    assert_eq!(db.version().await?,version);assert!(db.commit_rows(position(4),vec![],vec![],vec![]).await.is_err());
    let context=db.canonical_scan_context(CanonicalScanRequest{symbol:"XAU/USD".into(),source_id:"jin10_client".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:i64::MAX,final_only:false,expected_version:None},None).await?;
    assert_eq!(context.context.unwrap().quote.unwrap()["last"],exact);
    db.close().await?;drop(db);let reopened=store::Store::connect(path.to_str().unwrap()).await?;
    assert_eq!(reopened.latest_quotes().await?[0],latest);assert_eq!(reopened.version().await?,version);reopened.close().await?;Ok(())
}
#[tokio::test]
async fn routes_and_dependencies_are_one_atomic_metadata_version()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
    let before=db.version().await?;db.set_routes_group(vec!["XAU/USD".into(),"USD/CNH".into(),"XAU/CNH/g".into()],"jin10_client".into()).await?;
    assert_eq!(db.version().await?.commit_id,before.commit_id+1);let rows=db.routes().await?;assert_eq!(rows.len(),3);assert!(rows.iter().all(|v|v["source_id"]=="jin10_client"));
    let version=db.version().await?;assert!(db.set_routes_group(vec![],"bad".into()).await.is_err());assert_eq!(db.version().await?,version);assert_eq!(db.routes().await?,rows);db.close().await?;Ok(())
}

#[tokio::test]
async fn watchlist_minimum_is_checked_inside_writer_transaction()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
    db.initialize_instruments(&[],&["first".into(),"second".into()]).await?;
    let (a,b)=tokio::join!(db.set_watchlist("first",false),db.set_watchlist("second",false));
    assert_eq!(usize::from(a.is_ok())+usize::from(b.is_ok()),1);assert_eq!(db.watchlist().await?.len(),1);
    let version=db.version().await?;let survivor=db.watchlist().await?[0].clone();
    assert!(db.set_watchlist(&survivor,false).await.is_err());assert_eq!(db.version().await?,version);assert_eq!(db.watchlist().await?,vec![survivor]);db.close().await?;Ok(())
}
