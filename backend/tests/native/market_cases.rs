#[path="../../src/catalog.rs"] mod catalog;
#[path="../../src/market.rs"] mod market;
#[path="../../src/pages.rs"] mod pages;
#[path="../../src/quotes.rs"] mod quotes;
#[path="../../src/store.rs"] mod store;
#[path="../../src/stream.rs"] mod stream;
#[path="../../src/providers/fuyao.rs"] mod fuyao;
#[path="../../src/providers/tonghuashun.rs"] mod tonghuashun;
#[path="../../src/capture.rs"] mod capture;
#[path="../../src/quant_bar_adapter.rs"] mod quant_bar_adapter;
use serde_json::json;
use chrono::{DateTime,Utc,Duration};
use tracefang_core::{domain::{Candle,Decimal,QuoteSnapshot,SourceMetadata,Instrument},events::BarState,
    periods::Period,persistence_contract::{CapturePosition,ImportBarRow,ImportContext,ImportBatch,SCHEMA_VERSION},
    native_store::{bar_from_value,CanonicalBarKey}};
fn at(s:&str)->DateTime<Utc>{s.parse().unwrap()}
fn position(seq:u64)->CapturePosition{CapturePosition{epoch:"isolated-page-test".into(),sequence:seq,digest:format!("{seq:064x}")}}
fn candle(instrument:&Instrument,time:DateTime<Utc>,price:i64,receive:DateTime<Utc>,volume:Option<Decimal>)->Candle {
    Candle{instrument:instrument.clone(),interval_seconds:60,open_time:time,open:price.into(),high:price.into(),low:price.into(),close:price.into(),volume,
        source:SourceMetadata{provider:"tonghuashun_futures".into(),provider_symbol:"qh_au8888".into(),observed_at:time,received_at:receive,
            raw_payload:Some(json!({"bar_state":"final","connection_id":"connection-reset","sequence":"1","history_file":"fixed-test"}))}}
}
fn canonical(instrument:&Instrument,time:DateTime<Utc>,price:i64,revision:u64,state:BarState)->anyhow::Result<ImportBarRow>{
    let value=tracefang_core::events::RealtimeBar::from_candle(candle(instrument,time,price,time+Duration::seconds(61),Some(Decimal::ONE)),
        "tonghuashun_futures".into(),state,revision,(state==BarState::Final).then_some(time+Duration::seconds(60)));
    Ok(bar_from_value(&serde_json::to_value(value)?)?)
}
#[tokio::test]
async fn v6_clock_and_unclassified_points_remain_visible_in_same_mvcc_after_reopen()->anyhow::Result<()> {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,BarSelection,CanonicalScanRequest};
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let catalog=catalog::Catalog::embedded()?;let instrument=catalog.get("AU8888")?.instrument.clone();let market=market::Market::new(catalog,db.clone()).await?;
    let receipt=at("2026-09-30T06:06:07Z");let payload=json!({"data":"202609300900,900,900,900,900,20,0;202609301406,900,901,899,900,0,0;202609301407,900,901,899,901,,0;202609301408,901,902,900,901,1,0"});
    let stamp_witness=|bars:&mut Vec<Candle>,position:CapturePosition|->anyhow::Result<()> {
        use sha2::{Digest,Sha256};
        let digest=hex::encode(Sha256::digest(serde_json::to_vec(&payload)?));
        for bar in bars {bar.source.raw_payload.as_mut().unwrap()["authoritative_input"]=json!({"protocol":"tonghuashun_public_line_v6","provider_code":"qh_au8888","period":"61","response_kind":"minute_year","body_sha256":digest,"capture_position":position});}Ok(())
    };
    let mut bars=tonghuashun::parse_minutes(&payload,&instrument,"qh_au8888","沪金主连",chrono_tz::Asia::Shanghai,receipt)?;
    stamp_witness(&mut bars,position(1))?;
    market.apply(vec![],bars,position(1),Some(receipt.timestamp_nanos_opt().unwrap()),false,None).await?;
    let request=CanonicalSnapshotRequest{symbol:"AU8888".into(),source_id:"tonghuashun_futures".into(),period:"1m".into(),selection:BarSelection::Latest{count:10},expected_version:None,final_only:false};
    let snapshot=db.canonical_snapshot(request.clone()).await?;assert_eq!(snapshot.bars.len(),3);
    assert_eq!(snapshot.coverage["source_event_coverage_complete"],false);assert_eq!(snapshot.coverage["source_point_coverage"]["known_unclassified_point_count_lower_bound"],"1");
    let hot=market.hot_bars("AU8888","tonghuashun_futures",60)?;assert_eq!(hot[0].open_time,at("2026-09-30T06:05:00Z"));assert_eq!(hot[0].state,BarState::Final);
    assert_eq!(hot[1].state,BarState::ProvisionalAuthoritative);assert_eq!(hot[1].source.observed_at,at("2026-09-30T06:07:00Z"));assert_eq!(hot[1].source.received_at,receipt);
    assert_eq!(hot[1].source.raw("source_label"),Some(&json!("202609301407")));
    let contexts=std::sync::Arc::new(std::sync::Mutex::new(vec![]));let target=contexts.clone();
    db.canonical_scan(CanonicalScanRequest{symbol:"AU8888".into(),source_id:"tonghuashun_futures".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:i64::MAX,expected_version:None,final_only:false},10,move|batch|{if let Some(context)=batch.context{target.lock().unwrap().push(context);}Ok(())}).await?;
    assert!(contexts.lock().unwrap()[0].external_facts.iter().any(|v|v.kind=="source_point_coverage" && v.value["known_unclassified_point_count_lower_bound"]=="1"));
    // Receiving the same annual witness again does not count a new source point.
    let mut bars=tonghuashun::parse_minutes(&payload,&instrument,"qh_au8888","沪金主连",chrono_tz::Asia::Shanghai,receipt)?;
    stamp_witness(&mut bars,position(2))?;
    market.apply(vec![],bars,position(2),Some(receipt.timestamp_nanos_opt().unwrap()),false,None).await?;
    assert_eq!(db.canonical_snapshot(request.clone()).await?.coverage["source_point_coverage"]["known_unclassified_point_count_lower_bound"],"1");
    drop(market);db.close().await?;drop(db);let reopened=store::Store::connect(path.to_str().unwrap()).await?;
    let again=reopened.canonical_snapshot(request).await?;assert_eq!(again.coverage["source_event_coverage_complete"],false);assert_eq!(again.bars[0]["open_time"],"2026-09-30T06:05:00+00:00");reopened.close().await?;Ok(())
}
#[tokio::test]
async fn fuyao_partial_source_components_survive_capture_commit_reopen_and_both_aggregators()->anyhow::Result<()> {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,BarSelection};
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let raw=capture::Capture::connect(dir.path().join("capture.redb").to_str().unwrap()).await?;
    let db=store::Store::connect(path.to_str().unwrap()).await?;let catalog=catalog::Catalog::embedded()?;let d=catalog.get("IC2612")?.clone();let feed=d.public_feed.as_ref().unwrap();
    let opening=1790731800000i64;let received=at("2026-09-30T02:00:00Z");let start=DateTime::from_timestamp_nanos(opening*1_000_000);let end=start+Duration::minutes(3);
    let market=market::Market::new(catalog,db.clone()).await?;
    let payload=|opening_volume:&str,high:&str|json!({"status_code":0,"data":{"fail_params":null,"quote_data":[{"market":feed.market,"code":feed.code,"data_fields":["1","7","8","9","11","13","19"],"value":[[opening,"100","100","100","100",opening_volume,0],[opening+60000,"101",high,"99","102",null,0],[opening+120000,"102","102","102","102","0",0]]}]}});
    let mut last=None;
    for (sequence,quantity,high) in [(1,"2","103"),(2,"0.0000000000000000000000000001","103"),(3,"0.0000000000000000000000000001","102")] {
        let body=payload(quantity,high);let frame=capture::ProviderFrame{version:1,channel:"tonghuashun_fuyao".into(),connection_id:"fixed-quantity".into(),sequence,received_at:received,encoding:"utf8".into(),body:serde_json::to_vec(&body)?};
        let receipt=raw.append(&frame).await?;assert_eq!(raw.get_at(&receipt.position).await?.frame.body,frame.body);
        let bars=fuyao::parse_minutes(&body,&d.instrument,feed,received,Some(received))?;assert_eq!(bars.len(),2);assert!(bars[0].volume.is_none());
        market.apply(vec![],bars,receipt.position.clone(),Some(receipt.accepted_at_ns),false,None).await?;
        let (_,ranges)=db.range_aggregates(&d.instrument.symbol,"tonghuashun_futures",vec![(start.timestamp_nanos_opt().unwrap(),end.timestamp_nanos_opt().unwrap())],false,None).await?;let range=ranges[0].as_ref().unwrap();
        assert_eq!(range.total_count,2);assert_eq!(range.known_volume_count,1);assert_eq!(range.known_volume_sum,Decimal::ZERO);assert!(range.volume().is_none());
        assert_eq!(range.high.to_string(),high);assert_eq!(range.source_volume_components.len(),1);let coverage=&range.source_volume_components[0];
        assert_eq!(coverage.policy,tracefang_core::source_volume::FUYAO_INTERVAL);assert_eq!(coverage.total_count,3);assert_eq!(coverage.known_count,2);assert_eq!(coverage.known_volume_sum.to_string(),quantity);
        let hot=tracefang_core::periods::project_bars(&market.hot_bars(&d.instrument.symbol,"tonghuashun_futures",60)?,Period::M3,None,Utc::now())?;
        let page=db.canonical_period_page_at(CanonicalSnapshotRequest{symbol:d.instrument.symbol.clone(),source_id:"tonghuashun_futures".into(),period:"3m".into(),selection:BarSelection::Latest{count:1},final_only:false,expected_version:None},Period::M3,None,Utc::now().timestamp_nanos_opt().unwrap()).await?;
        let derived=bar_from_value(&page.bars[0])?;let quant=quant_bar_adapter::bar_ref(&derived)?;
        assert_eq!(quant.component_count,2);assert_eq!(quant.known_volume_count,1);assert_eq!(quant.known_volume_sum.to_string(),"0");assert!(quant.volume.is_none());
        let source=quant.source_volume_components.as_ref().unwrap();assert_eq!(source.known_volume_sum,tracefang_core::quant_core::exact::parse(quantity)?);assert_eq!(source.known_count,2);assert_eq!(source.total_count,3);
        assert_eq!(hot[0].source.raw("source_volume_components"),Some(&derived.source_metadata["raw_payload"]["source_volume_components"]));assert_eq!(hot[0].volume,None);assert_eq!(hot[0].high.to_string(),high);
        let (_,rows)=db.lookup_bars(vec![CanonicalBarKey{source_id:"tonghuashun_futures".into(),symbol:d.instrument.symbol.clone(),interval_seconds:60,open_time_ns:start.timestamp_nanos_opt().unwrap()}]).await?;
        let base_quant=quant_bar_adapter::bar_ref(rows[0].as_ref().unwrap())?;assert_eq!(base_quant.revision,sequence,"quantity evidence alone is a real source revision");assert_eq!(base_quant.component_count,1);assert_eq!(base_quant.known_volume_count,0);assert_eq!(base_quant.source_volume_components.as_ref().unwrap().total_count,2);
        last=Some(receipt.position);
    }
    // A failed supplement remains exact raw evidence and advances an explicit
    // decode failure, without mutating the already committed minute facts.
    let bad=json!({"status_code":0,"data":{"fail_params":null,"quote_data":[{"market":feed.market,"code":feed.code,"data_fields":["1","7","8","9","11","13","19"],"value":[[opening,"100","100","100","100","2",0]]}]}});
    let frame=capture::ProviderFrame{version:1,channel:"tonghuashun_fuyao".into(),connection_id:"fixed-quantity".into(),sequence:4,received_at:received,encoding:"utf8".into(),body:serde_json::to_vec(&bad)?};let receipt=raw.append(&frame).await?;
    let failure=fuyao::parse_minutes(&bad,&d.instrument,feed,received,None).unwrap_err();assert!(failure.to_string().contains("quarantined"));market.record_decode_failure(receipt.position.clone(),"tonghuashun_fuyao",&failure.to_string(),None).await?;
    assert_eq!(raw.get_at(&receipt.position).await?.frame.body,frame.body);assert_eq!(db.version().await?.committed_capture,Some(receipt.position));assert_ne!(last,db.version().await?.committed_capture);
    assert!(market.persistence.borrow()["last_write_at"].is_null());assert!(market.persistence.borrow()["observed_at"].is_string());
    drop(market);raw.close_and_drain().await?;db.close().await?;drop(db);let reopened=store::Store::connect(path.to_str().unwrap()).await?;
    let (_,ranges)=reopened.range_aggregates(&d.instrument.symbol,"tonghuashun_futures",vec![(start.timestamp_nanos_opt().unwrap(),end.timestamp_nanos_opt().unwrap())],false,None).await?;let range=ranges[0].as_ref().unwrap();
    assert_eq!(range.source_volume_components[0].known_volume_sum.to_string(),"0.0000000000000000000000000001");assert_eq!(range.total_count,2);assert!(range.volume().is_none());reopened.close().await?;Ok(())
}
#[tokio::test]
async fn committed_pages_preserve_corrections_exclusive_cursors_and_readonly_queries()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
    let cat=catalog::Catalog::embedded()?;let instrument=cat.get("AU8888")?.instrument.clone();
    let times=[at("2026-08-10T13:00:00Z"),at("2026-08-11T01:00:00Z"),at("2026-08-11T01:01:00Z")];
    db.commit_rows(position(1),vec![canonical(&instrument,times[0],90,1,BarState::Final)?,canonical(&instrument,times[1],100,1,BarState::Final)?,
        canonical(&instrument,times[2],105,1,BarState::ProvisionalAuthoritative)?],vec![],vec![]).await?;
    let market=market::Market::new(cat,db.clone()).await?;
    let mut correction=candle(&instrument,times[2],102,times[2]+Duration::seconds(62),Some(Decimal::ONE));
    correction.source.raw_payload.as_mut().unwrap()["bar_state"]=json!("provisional_authoritative");
    let newest=candle(&instrument,times[2]+Duration::minutes(1),103,times[2]+Duration::seconds(123),Some(Decimal::ONE));
    market.apply(vec![],vec![correction,newest],position(2),Some((times[2]+Duration::seconds(124)).timestamp_nanos_opt().unwrap()),false,None).await?;
    let version=db.version().await?;let counts=db.generation_summary().await?["counts"].clone();
    let page=pages::chart_page(&market,"AU8888",Period::M5,None,1).await?;
    assert_eq!(page.items.len(),1);assert!(page.has_more);assert_eq!(page.items[0].open,100.into());assert_eq!(page.items[0].high,103.into());
    assert_eq!(page.items[0].close,103.into());assert_eq!(page.items[0].volume,Some(3.into()));
    assert_eq!(page.next_before,Some(page.items[0].open_time));assert_eq!(page.snapshot_version,version);
    let schedule=pages::schedule(&market,"AU8888")?;
    let cursor=pages::encode_cursor(&instrument,"tonghuashun_futures",Period::M5,Some(&schedule),page.next_before.unwrap())?;
    let before=pages::resolve_boundary(Some(&cursor),None,&instrument,"tonghuashun_futures",Period::M5,Some(&schedule))?;
    let older=pages::chart_page(&market,"AU8888",Period::M5,before,1).await?;
    assert_eq!(older.items.len(),1);assert!(older.items[0].open_time<page.items[0].open_time);assert!(!older.has_more);
    assert!(pages::resolve_boundary(Some(&cursor),None,&instrument,"jin10_client",Period::M5,Some(&schedule)).is_err());
    assert!(pages::resolve_boundary(Some(&cursor),Some(1),&instrument,"tonghuashun_futures",Period::M5,Some(&schedule)).is_err());
    let daily=pages::chart_page(&market,"AU8888",Period::D1,None,1).await?;
    assert_eq!(daily.items[0].open,90.into());assert_eq!(daily.items[0].close,103.into());
    assert_eq!(pages::prepare_live_period(&market,"AU8888",Period::D1).await?,daily.items);
    let minute=pages::chart_page(&market,"AU8888",Period::M1,None,2).await?;assert!(minute.has_more);
    assert_eq!(minute.items.iter().map(|b|b.close.clone()).collect::<Vec<_>>(),vec![102.into(),103.into()]);
    let payload=pages::page_payload(minute,&instrument,"tonghuashun_futures",Period::M1,Some(&schedule))?;
    assert_eq!(payload["local_status"],"ready");assert!(payload["next_cursor"].is_string());
    assert!(pages::chart_page(&market,"AU8888",Period::M1,None,10000).await.is_ok(),"maximum supported page includes an internal peek");
    assert_eq!(db.version().await?,version);assert_eq!(db.generation_summary().await?["counts"],counts);
    drop(market);db.close().await?;Ok(())
}
#[tokio::test]
async fn two_cold_key_corrections_in_one_frame_keep_overlay_revisions_and_reopen()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let cat=catalog::Catalog::embedded()?;let instrument=cat.get("AU8888")?.instrument.clone();let base=at("2026-08-11T01:00:00Z");
    let rows=(0..301).map(|i|canonical(&instrument,base+Duration::minutes(i),if i==0{1000}else{1},40,BarState::Final)).collect::<anyhow::Result<Vec<_>>>()?;
    db.commit_rows(position(1),rows,vec![],vec![]).await?;let market=market::Market::new(cat,db.clone()).await?;
    assert!(market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?.iter().all(|b|b.open_time>base));
    let mut stream=market.streams.subscribe("tonghuashun_futures",&instrument.symbol,"1m");
    let corrections=vec![candle(&instrument,base,2,base+Duration::seconds(10),Some(1.into())),candle(&instrument,base,-3,base+Duration::seconds(9),None)];
    market.apply(vec![],corrections,position(2),Some((base+Duration::seconds(11)).timestamp_nanos_opt().unwrap()),true,None).await?;
    let key=CanonicalBarKey{source_id:"tonghuashun_futures".into(),symbol:instrument.symbol.clone(),interval_seconds:60,open_time_ns:base.timestamp_nanos_opt().unwrap()};
    let (_,rows)=db.lookup_bars(vec![key.clone()]).await?;let corrected=rows[0].as_ref().unwrap();
    assert_eq!(corrected.revision,42);assert_eq!(corrected.high,"-3");assert!(corrected.volume.is_none());
    let mut changed=false;let mut invalidated=false;while let Ok(event)=stream.try_recv(){if event["kind"]=="bar"{assert_eq!(event["bar"]["revision"],"42");assert_eq!(event["bar"]["high"],"-3");changed=true;}
        if event["kind"]=="range_invalidated"{assert_eq!(event["change"]["start_ns"],base.timestamp_nanos_opt().unwrap().to_string());assert!(event["change"]["historical_correction"]==true);invalidated=true;}}
    assert!(changed&&invalidated);let (_,range)=db.range_aggregates(&instrument.symbol,"tonghuashun_futures",vec![(base.timestamp_nanos_opt().unwrap(),(base+Duration::minutes(301)).timestamp_nanos_opt().unwrap())],false,None).await?;
    assert_eq!(range[0].as_ref().unwrap().high.to_string(),"1");assert!(range[0].as_ref().unwrap().volume().is_none());
    let hot=market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?;drop(market);db.close().await?;drop(db);
    let reopened=store::Store::connect(path.to_str().unwrap()).await?;let market=market::Market::new(catalog::Catalog::embedded()?,reopened.clone()).await?;
    assert_eq!(market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?,hot);assert_eq!(reopened.lookup_bars(vec![key]).await?.1[0].as_ref().unwrap().revision,42);
    drop(market);reopened.close().await?;Ok(())
}
#[tokio::test]
async fn canonical_legacy_final_with_unknown_clock_survives_old_quotes_and_restart()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let cat=catalog::Catalog::embedded()?;let instrument=cat.get("AU8888")?.instrument.clone();let base=at("2026-08-11T01:00:00Z");let stage=db.staging("fixed-legacy").await?;
    let mut rows=(0..301).map(|i|canonical(&instrument,base+Duration::minutes(i),1,40,BarState::Final)).collect::<anyhow::Result<Vec<_>>>()?;
    for row in &mut rows {row.finalized_at_ns=None;row.evidence=json!({"table":"candles","semantics":"final_revision_history","finalization_time_unknown":true,
        "fixed_snapshot":{"source_fingerprint":"fixed-original","snapshot":"fixed-snapshot","table_sha256":"a".repeat(64)}});}
    stage.import_bars(ImportBatch{context:ImportContext{origin_id:"fixed-pg".into(),source_fingerprint:"fixed-original".into(),schema_version:SCHEMA_VERSION.into(),range_label:"bars".into(),legacy_cursor:None,expected_sha256:None},row_offset:0,rows}).await?;
    let proof=stage.verify_staging().await?;db.activate_staging("fixed-legacy",proof).await?;drop(stage);
    let mut market=market::Market::new(cat,db.clone()).await?;
    for cycle in 0..2 {
        let before=market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?;assert!(before.iter().all(|b|b.state==BarState::Final&&b.finalized_at.is_none()));
        let quote=QuoteSnapshot{instrument:instrument.clone(),last:9999.into(),open:None,high:None,low:None,volume:None,change:None,change_percent:None,
            source:SourceMetadata{provider:"tonghuashun_futures".into(),provider_symbol:"qh_au8888".into(),observed_at:base,received_at:base+Duration::seconds(30),raw_payload:Some(json!({"connection_id":"new","sequence":"1"}))}};
        market.apply(vec![quote],vec![],position(cycle+1),Some((base+Duration::seconds(31)).timestamp_nanos_opt().unwrap()),false,None).await?;
        let minute=pages::chart_page(&market,"AU8888",Period::M1,Some(base+Duration::minutes(1)),1).await?;assert_eq!(minute.items[0].state,BarState::Final);assert!(minute.items[0].finalized_at.is_none());assert_eq!(minute.items[0].close,1.into());assert_eq!(minute.items[0].revision,40);
        assert_eq!(market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?,before);
        market=market::Market::new(catalog::Catalog::embedded()?,db.clone()).await?;
    }
    drop(market);db.close().await?;drop(db);let reopened=store::Store::connect(path.to_str().unwrap()).await?;
    let market=market::Market::new(catalog::Catalog::embedded()?,reopened.clone()).await?;assert!(market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?.iter().all(|b|b.state==BarState::Final&&b.finalized_at.is_none()));
    drop(market);reopened.close().await?;Ok(())
}

#[tokio::test]
async fn concurrent_configuration_receipts_keep_ram_store_and_reopen_equal()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let mut catalog=catalog::Catalog::embedded()?;catalog.default_watchlist=vec!["AU8888".into(),"AU2610".into()];
    let market=market::Market::new(catalog.clone(),db.clone()).await?;
    let (first,second)=tokio::join!(market.change_watchlist("AU8888",false),market.change_watchlist("AU2610",false));
    assert_eq!(usize::from(first.is_ok())+usize::from(second.is_ok()),1);
    let error=if first.is_err(){first.unwrap_err()}else{second.unwrap_err()};assert!(error.to_string().contains("watchlist_minimum_one"));
    let ram=market.watchlist.lock().unwrap().clone();assert_eq!(ram.len(),1);
    assert_eq!(db.watchlist().await?,ram.iter().map(|code|catalog.get(code).unwrap().instrument.symbol.clone()).collect::<Vec<_>>());
    let (add,remove)=tokio::join!(market.change_watchlist("XAUUSD",true),market.change_watchlist("AU8888",false));assert!(add.is_ok());
    // Deleting an absent item is harmless; deleting the sole remaining item before add may be rejected.
    if let Err(error)=remove{assert!(error.to_string().contains("watchlist_minimum_one"));}
    let (a,b)=tokio::join!(market.change_source("XAUCNHG","jin10_client"),market.change_source("XAUUSD","jin10_client"));
    let av=a?;let bv=b?;assert_ne!(av.commit_id,bv.commit_id);
    let routes=db.routes().await?;for row in routes {assert_eq!(market.source(row["instrument_symbol"].as_str().unwrap())?,row["source_id"].as_str().unwrap());}
    let watched=market.watchlist.lock().unwrap().clone();let routed=market.routes.lock().unwrap().clone();drop(market);db.close().await?;drop(db);
    let db=store::Store::connect(path.to_str().unwrap()).await?;let reopened=market::Market::new(catalog,db.clone()).await?;
    assert_eq!(*reopened.watchlist.lock().unwrap(),watched);assert_eq!(*reopened.routes.lock().unwrap(),routed);
    db.close().await?;Ok(())
}

#[tokio::test]
async fn derived_completion_clock_never_precedes_bucket_or_visible_components()->anyhow::Result<()> {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,BarSelection,CanonicalScanRequest};
    let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
    let catalog=catalog::Catalog::embedded()?;let instrument=catalog.get("AU8888")?.instrument.clone();let start=at("2026-09-30T01:00:00Z");
    let rows=(0..20).map(|n|canonical(&instrument,start+Duration::minutes(n),100+n,1,BarState::Final)).collect::<anyhow::Result<Vec<_>>>()?;
    db.commit_rows(position(1),rows,vec![],vec![]).await?;let market=market::Market::new(catalog,db.clone()).await?;let schedule=pages::schedule(&market,"AU8888")?;
    let request=CanonicalSnapshotRequest{symbol:instrument.symbol.clone(),source_id:"tonghuashun_futures".into(),period:"1h".into(),selection:BarSelection::Latest{count:10},final_only:false,expected_version:None};
    let early=start+Duration::minutes(20);let end=start+Duration::hours(1);
    let early_page=db.canonical_period_page_at(request.clone(),Period::H1,Some(schedule.clone()),early.timestamp_nanos_opt().unwrap()).await?;
    assert_eq!(early_page.bars.len(),1);assert_ne!(early_page.bars[0]["state"],"final");assert!(early_page.bars[0]["finalized_at"].is_null());
    let ended=db.canonical_period_page_at(request,Period::H1,Some(schedule.clone()),end.timestamp_nanos_opt().unwrap()).await?;
    assert_eq!(ended.bars[0]["state"],"final");assert_eq!(ended.bars[0]["finalized_at"].as_str().unwrap().parse::<DateTime<Utc>>()?,end);
    assert_eq!(ended.bars[0]["raw_payload"]["component_finalized_at_ns"],(start+Duration::minutes(20)).timestamp_nanos_opt().unwrap().to_string());
    let minutes=market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?;
    let hot_early=tracefang_core::periods::project_bars(&minutes,Period::H1,Some(&schedule),early)?;assert_ne!(hot_early[0].state,BarState::Final);assert_eq!(hot_early[0].finalized_at,None);
    let hot=tracefang_core::periods::project_bars(&minutes,Period::H1,Some(&schedule),end)?;assert_eq!(hot[0].finalized_at,Some(end));
    for period in [Period::D1,Period::W1] {let page=pages::chart_page(&market,"AU8888",period,None,10).await?;for bar in page.items {if let Some(clock)=bar.finalized_at{let bucket=tracefang_core::periods::bucket_for(minutes[0].open_time,period,Some(&schedule))?;assert!(clock>=bucket.input_end());}}}
    let batches=std::sync::Arc::new(std::sync::Mutex::new(vec![]));let output=batches.clone();db.canonical_calendar_scan(CanonicalScanRequest{symbol:instrument.symbol.clone(),source_id:"tonghuashun_futures".into(),interval_seconds:60,start_ns:i64::MIN,end_ns:early.timestamp_nanos_opt().unwrap(),final_only:false,expected_version:None},Period::H1,Some(schedule),10,None,move|batch|{output.lock().unwrap().extend(batch.rows);Ok(())}).await?;
    {let batches=batches.lock().unwrap();assert_eq!(batches.len(),1);assert_ne!(batches[0].state,"final");assert_eq!(batches[0].finalized_at_ns,None);}
    db.close().await?;Ok(())
}

#[tokio::test]
async fn known_clock_maximum_survives_partial_unknown_and_ordering()->anyhow::Result<()> {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,BarSelection};
    let start=at("2026-09-30T01:00:00Z");let end=start+Duration::hours(1);let late=end+Duration::minutes(5);
    for accepted in [[None,Some(late)],[Some(late),None],[None,None],[Some(late),Some(end)]] {
        let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
        let catalog=catalog::Catalog::embedded()?;let instrument=catalog.get("AU8888")?.instrument.clone();
        let mut rows=vec![canonical(&instrument,start,100,1,BarState::Final)?,canonical(&instrument,start+Duration::minutes(1),101,1,BarState::Final)?];
        for (n,row) in rows.iter_mut().enumerate() {
            // Source and receipt clocks deliberately run backwards; known acceptance remains independent.
            row.source_observed_at_ns=(start+Duration::seconds(if n==0{50}else{40})).timestamp_nanos_opt().unwrap();
            row.received_at_ns=(start+Duration::minutes(if n==0{4}else{3})).timestamp_nanos_opt().unwrap();
            row.source_metadata["observed_at"]=json!(tracefang_core::domain::isoformat(DateTime::from_timestamp_nanos(row.source_observed_at_ns)));
            row.source_metadata["received_at"]=json!(tracefang_core::domain::isoformat(DateTime::from_timestamp_nanos(row.received_at_ns)));
            row.source_metadata["raw_payload"]["capture_accepted_at_ns"]=json!(accepted[n].and_then(|t|t.timestamp_nanos_opt()).map(|v|v.to_string()));
        }
        db.commit_rows(position(1),rows,vec![],vec![]).await?;let market=market::Market::new(catalog,db.clone()).await?;
        let schedule=pages::schedule(&market,"AU8888")?;let request=CanonicalSnapshotRequest{symbol:instrument.symbol.clone(),source_id:"tonghuashun_futures".into(),period:"1h".into(),selection:BarSelection::Latest{count:1},final_only:false,expected_version:None};
        let expected_count=accepted.iter().filter(|v|v.is_some()).count();let maximum=accepted.iter().copied().flatten().max();
        let (_,range)=db.range_aggregates(&instrument.symbol,"tonghuashun_futures",vec![(start.timestamp_nanos_opt().unwrap(),end.timestamp_nanos_opt().unwrap())],false,None).await?;
        assert_eq!(range[0].as_ref().unwrap().accepted_at_ns,maximum.and_then(|v|v.timestamp_nanos_opt()));assert_eq!(range[0].as_ref().unwrap().accepted_known_count,expected_count as u64);
        let before=db.canonical_period_page_at(request.clone(),Period::H1,Some(schedule.clone()),end.timestamp_nanos_opt().unwrap()).await?;
        let hot=tracefang_core::periods::project_bars(&market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?,Period::H1,Some(&schedule),end)?;
        assert_eq!(before.bars[0]["state"]=="final",maximum.is_none());assert_eq!(hot[0].state==BarState::Final,maximum.is_none());
        let visible=db.canonical_period_page_at(request,Period::H1,Some(schedule),late.timestamp_nanos_opt().unwrap()).await?;
        assert_eq!(visible.bars[0]["state"],"final");assert_eq!(visible.bars[0]["raw_payload"]["accepted_clock_known_component_count"],expected_count.to_string());
        assert_eq!(visible.bars[0]["raw_payload"]["accepted_clock_all_components_known"],expected_count==2);
        assert_eq!(visible.bars[0]["finalized_at"].as_str().unwrap().parse::<DateTime<Utc>>()?,maximum.unwrap_or(end));
        db.close().await?;
    }
    Ok(())
}

#[tokio::test]
async fn legacy_unknown_final_clock_never_becomes_invented_derived_publication()->anyhow::Result<()> {
    use tracefang_core::persistence_contract::{CanonicalSnapshotRequest,BarSelection};
    let dir=tempfile::tempdir()?;let db=store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
    let catalog=catalog::Catalog::embedded()?;let instrument=catalog.get("AU8888")?.instrument.clone();let start=at("2026-09-30T01:00:00Z");let end=start+Duration::hours(1);let late=end+Duration::minutes(5);
    let stage=db.staging("unknown-clock").await?;let mut row=canonical(&instrument,start,100,1,BarState::Final)?;row.finalized_at_ns=None;
    row.source_metadata["raw_payload"]["capture_accepted_at_ns"]=json!(late.timestamp_nanos_opt().unwrap().to_string());
    row.evidence=json!({"table":"candles","semantics":"final_revision_history","finalization_time_unknown":true,"fixed_snapshot":{"source_fingerprint":"fixed","snapshot":"fixed","table_sha256":"a".repeat(64)}});
    stage.import_bars(ImportBatch{context:ImportContext{origin_id:"fixed-pg".into(),source_fingerprint:"fixed".into(),schema_version:SCHEMA_VERSION.into(),range_label:"bars".into(),legacy_cursor:None,expected_sha256:None},row_offset:0,rows:vec![row]}).await?;
    let proof=stage.verify_staging().await?;db.activate_staging("unknown-clock",proof).await?;let market=market::Market::new(catalog,db.clone()).await?;let schedule=pages::schedule(&market,"AU8888")?;
    let request=CanonicalSnapshotRequest{symbol:instrument.symbol.clone(),source_id:"tonghuashun_futures".into(),period:"1h".into(),selection:BarSelection::Latest{count:1},final_only:false,expected_version:None};
    for (cutoff,final_state) in [(end,false),(late,true)] {
        let page=db.canonical_period_page_at(request.clone(),Period::H1,Some(schedule.clone()),cutoff.timestamp_nanos_opt().unwrap()).await?;
        let hot=tracefang_core::periods::project_bars(&market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?,Period::H1,Some(&schedule),cutoff)?;
        assert_eq!(page.bars[0]["state"]=="final",final_state);assert_eq!(hot[0].state==BarState::Final,final_state);
        assert!(page.bars[0]["finalized_at"].is_null());assert!(hot[0].finalized_at.is_none());
        assert_eq!(page.bars[0]["raw_payload"]["source_publication_time_unknown"],true);
    }
    db.close().await?;Ok(())
}

#[tokio::test]
async fn recovery_acceptance_clocks_keep_legacy_unknown_native_known_and_price_provenance_after_reopen()->anyhow::Result<()> {
    let dir=tempfile::tempdir()?;let path=dir.path().join("facts.redb");let db=store::Store::connect(path.to_str().unwrap()).await?;
    let catalog=catalog::Catalog::embedded()?;let instrument=catalog.get("AU8888")?.instrument.clone();let market=market::Market::new(catalog,db.clone()).await?;
    let start=at("2026-09-30T01:00:00Z");let native_clock=(start+Duration::seconds(122)).timestamp_nanos_opt().unwrap();
    let prior_real_clock=(start+Duration::seconds(181)).timestamp_nanos_opt().unwrap();
    for (sequence,acceptance,existing_clock) in [(1,None,None),(2,Some(native_clock),None),(3,None,Some(prior_real_clock))] {
        let open=start+Duration::minutes(sequence as i64-1);let mut bar=candle(&instrument,open,100+sequence as i64,open+Duration::seconds(61),None);
        if let Some(clock)=existing_clock {bar.source.raw_payload.as_mut().unwrap()["capture_accepted_at_ns"]=json!(clock.to_string());}
        market.apply(vec![],vec![bar],position(sequence),acceptance,false,None).await?;
    }
    let query=vec![0,1,2].into_iter().map(|n|CanonicalBarKey{source_id:"tonghuashun_futures".into(),symbol:instrument.symbol.clone(),interval_seconds:60,open_time_ns:(start+Duration::minutes(n)).timestamp_nanos_opt().unwrap()}).collect::<Vec<_>>();
    let (_,bars)=db.lookup_bars(query.clone()).await?;
    let clocks=bars.iter().map(|bar|bar.as_ref().unwrap().source_metadata["raw_payload"]["capture_accepted_at_ns"].clone()).collect::<Vec<_>>();
    assert_eq!(clocks,vec![serde_json::Value::Null,json!(native_clock.to_string()),json!(prior_real_clock.to_string())]);
    let quote_time=start+Duration::minutes(5);let price_clock=(quote_time+Duration::seconds(11)).timestamp_nanos_opt().unwrap();
    let quote=QuoteSnapshot{instrument:instrument.clone(),last:105.into(),open:None,high:None,low:None,volume:None,change:None,change_percent:None,
        source:SourceMetadata{provider:"tonghuashun_futures".into(),provider_symbol:"qh_au8888".into(),observed_at:quote_time,received_at:quote_time+Duration::seconds(10),raw_payload:Some(json!({"connection_id":"actual-price","sequence":"1"}))}};
    market.apply(vec![quote.clone()],vec![],position(4),Some(price_clock),false,None).await?;
    let mut supplement=quote; supplement.open=Some(104.into());
    let raw=supplement.source.raw_payload.as_mut().unwrap();raw["observation_kind"]=json!("supplement");raw["capture_epoch"]=json!(position(4).epoch);raw["capture_sequence"]=json!("4");raw["capture_digest"]=json!(position(4).digest);raw["capture_accepted_at_ns"]=json!(price_clock.to_string());
    market.apply(vec![supplement],vec![],position(5),None,false,None).await?;
    let latest=db.latest_quotes().await?.into_iter().find(|row|row["instrument_symbol"]==instrument.symbol).unwrap();
    assert_eq!(latest["raw_payload"]["capture_sequence"],"4");assert_eq!(latest["raw_payload"]["capture_accepted_at_ns"],price_clock.to_string());
    let supplement_source=&latest["raw_payload"]["statistics_evidence"]["source_metadata"]["raw_payload"];
    assert_eq!(supplement_source["capture_sequence"],"4");assert_eq!(supplement_source["supplement_capture_position"]["sequence"],"5");assert!(supplement_source["supplement_capture_accepted_at_ns"].is_null());
    let hot=market.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?;drop(market);db.close().await?;drop(db);
    let db=store::Store::connect(path.to_str().unwrap()).await?;let reopened=market::Market::new(catalog::Catalog::embedded()?,db.clone()).await?;
    assert_eq!(reopened.hot_bars(&instrument.symbol,"tonghuashun_futures",60)?,hot);
    let (_,again)=db.lookup_bars(query).await?;assert_eq!(again.iter().map(|bar|bar.as_ref().unwrap().source_metadata["raw_payload"]["capture_accepted_at_ns"].clone()).collect::<Vec<_>>(),clocks);
    let latest=db.latest_quotes().await?.into_iter().find(|row|row["instrument_symbol"]==instrument.symbol).unwrap();assert_eq!(latest["raw_payload"]["capture_sequence"],"4");assert_eq!(latest["raw_payload"]["capture_accepted_at_ns"],price_clock.to_string());
    db.close().await?;Ok(())
}
