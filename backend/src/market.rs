use std::{sync::{Arc,Mutex},collections::{BTreeMap,BTreeSet}};
use anyhow::{Result,Context};
use chrono::Utc;
use serde_json::{Value,json};
use tokio::sync::{watch,Mutex as AsyncMutex};
use tracefang_core::{domain::{QuoteSnapshot,Candle,SourceMetadata},events::{MarketEvent,quote_event_id},
    persistence_contract::{CapturePosition,ProjectionCommit,SnapshotVersion},reducer::{BarReducer,BarContract,SeriesKey,SeriesState}};
use crate::{catalog::{Catalog,database_bar,database_quote},store::Store,quotes::QuoteCache,stream::StreamHub};

#[derive(Clone)]
pub struct MarketState {pub reducer:BarReducer,pub quotes:QuoteCache}
#[derive(Clone)]
pub struct Market {
    pub catalog:Arc<Catalog>,pub state:Arc<Mutex<MarketState>>,pub routes:Arc<Mutex<BTreeMap<String,String>>>,
    pub watchlist:Arc<Mutex<Vec<String>>>,pub streams:Arc<StreamHub>,pub store:Store,pub persistence:watch::Receiver<Value>,
    configuration:Arc<AsyncMutex<()>>,
    persistence_tx:watch::Sender<Value>,application:Arc<AsyncMutex<()>>,
}
struct Prepared {state:MarketState,quotes:Vec<Value>,bars:Vec<Value>,samples:Vec<(String,String,Value)>,affected:BTreeSet<(String,String)>}

impl Market {
    pub async fn new(catalog:Catalog,store:Store)->Result<Self> {
        let catalog=Arc::new(catalog);
        let instruments=catalog.items.iter().map(|d|serde_json::to_value(&d.instrument)).collect::<std::result::Result<Vec<_>,_>>()?;
        let defaults=catalog.default_watchlist.iter().map(|c|catalog.get(c).map(|d|d.instrument.symbol.clone())).collect::<Result<Vec<_>>>()?;
        if !store.read_only(){store.initialize_instruments(&instruments,&defaults).await?;}
        use sha2::{Digest,Sha256};
        if !store.read_only(){store.configure_versions(hex::encode(Sha256::digest(serde_json::to_vec(&catalog.items)?)),hex::encode(Sha256::digest(serde_json::to_vec(&catalog.schedules)?))).await?;}
        let mut routes=BTreeMap::new();for d in &catalog.items {routes.insert(d.instrument.symbol.clone(),d.source_ids[0].clone());}
        for row in store.routes().await? {if row["capability"]=="realtime" {
            if let (Some(symbol),Some(source))=(row["instrument_symbol"].as_str(),row["source_id"].as_str()) {if let Ok(d)=catalog.get(symbol) {if d.source_ids.iter().any(|s|s==source){routes.insert(symbol.into(),source.into());}}}
        }}
        let watchlist=store.watchlist().await?.into_iter().filter_map(|s|catalog.get(&s).ok().map(|d|d.code.clone())).collect();
        let mut reducer=BarReducer::new(vec![BarContract::new("jin10_client","jin10_local",vec!["jin10_web".into()]),BarContract::new("tonghuashun_futures","tonghuashun_futures",vec!["tonghuashun_futures".into()])])?;
        let mut quotes=QuoteCache::default();
        for row in store.latest_quotes().await? {if let Some(d)=row["instrument_symbol"].as_str().and_then(|s|catalog.get(s).ok()) {quotes.accept(database_quote(row,&d.instrument)?);}}
        for d in &catalog.items {for source in &d.source_ids {for interval in [1,60] {
            let bars=store.bars_before(&d.instrument.symbol,source,interval,None,240).await?.into_iter().map(|r|database_bar(r,&d.instrument)).collect::<Result<Vec<_>>>()?;
            let state=if interval==60 {store.series_state(&d.instrument.symbol,source).await?.map(serde_json::from_value::<SeriesState>).transpose()?}else{None};reducer.hydrate(bars,state)?;
        }}}
        let version=store.version().await?;let (persistence_tx,persistence)=watch::channel(persisted_status(&version));
        Ok(Self {catalog,state:Arc::new(Mutex::new(MarketState {reducer,quotes})),routes:Arc::new(Mutex::new(routes)),watchlist:Arc::new(Mutex::new(watchlist)),streams:Arc::new(StreamHub::default()),store,persistence,persistence_tx,application:Arc::new(AsyncMutex::new(())),configuration:Arc::new(AsyncMutex::new(()))})
    }
    pub fn source(&self,symbol:&str)->Result<String> {self.routes.lock().expect("routes lock").get(symbol).cloned().context("source not configured")}
    pub async fn change_watchlist(&self,code:&str,add:bool)->Result<SnapshotVersion> {
        let _configuration=self.configuration.lock().await;
        let definition=self.catalog.get(code)?;
        let (version,values)=self.store.set_watchlist(&definition.instrument.symbol,add).await?;
        *self.watchlist.lock().expect("watchlist lock")=values.into_iter().filter_map(|symbol|self.catalog.get(&symbol).ok().map(|d|d.code.clone())).collect();
        Ok(version)
    }
    pub async fn change_source(&self,code:&str,source:&str)->Result<SnapshotVersion> {
        let _configuration=self.configuration.lock().await;
        let definition=self.catalog.get(code)?;anyhow::ensure!(definition.source_ids.iter().any(|id|id==source),"source is not supported by instrument");
        let symbols=std::iter::once(&definition.instrument).chain(&definition.dependencies).map(|v|v.symbol.clone()).collect();
        let (version,rows)=self.store.set_routes_group(symbols,source.into()).await?;
        let mut routes=self.routes.lock().expect("routes lock");
        for row in rows {if row["capability"]=="realtime" {if let (Some(symbol),Some(source))=(row["instrument_symbol"].as_str(),row["source_id"].as_str()){routes.insert(symbol.into(),source.into());}}}
        Ok(version)
    }
    pub fn quote_view(&self,code:&str,allow_stale:bool)->Result<Value> {let d=self.catalog.get(code)?;let source=self.source(&d.instrument.symbol)?;self.state.lock().expect("market lock").quotes.view(d,&source,Utc::now(),allow_stale)}
    /// Stage the entire frame, commit facts+index+cursor, then expose RAM and streams.
    pub async fn apply(&self,quotes:Vec<QuoteSnapshot>,candles:Vec<Candle>,position:CapturePosition,accepted_at_ns:Option<i64>,broadcast:bool,decoder_state:Option<Value>)->Result<SnapshotVersion> {
        let _application=self.application.lock().await;
        let initial=self.state.lock().expect("market lock").clone();let catalog=self.catalog.clone();let receipt=position.clone();
        let mut keys=BTreeMap::new();
        for quote in &quotes {
            let source=match quote.source.provider.as_str(){"jin10_web"=>"jin10_client","tonghuashun_futures"=>"tonghuashun_futures",_=>continue};
            for interval in [1u32,60] {let time=tracefang_core::reducer::floor_time(quote.source.observed_at,interval.into())?;
                let key=(source.to_owned(),quote.instrument.symbol.clone(),interval,crate::store::ns(time)?);keys.insert(key.clone(),tracefang_core::native_store::CanonicalBarKey {source_id:key.0,symbol:key.1,interval_seconds:interval,open_time_ns:key.3});}
        }
        for candle in &candles {
            let source=match candle.source.provider.as_str(){"jin10_local"=>"jin10_client","tonghuashun_futures"=>"tonghuashun_futures",_=>continue};
            let interval=u32::try_from(candle.interval_seconds)?;let key=(source.to_owned(),candle.instrument.symbol.clone(),interval,crate::store::ns(candle.open_time)?);
            keys.insert(key.clone(),tracefang_core::native_store::CanonicalBarKey {source_id:key.0,symbol:key.1,interval_seconds:interval,open_time_ns:key.3});
        }
        let (_,current)=self.store.lookup_bars(keys.into_values().collect()).await?;
        let mut current=current.into_iter().flatten().map(|row|{let definition=catalog.get(&row.instrument_symbol)?;let mut value=serde_json::to_value(&row)?;
            value["open_time"]=json!(chrono::DateTime::from_timestamp_nanos(row.open_time_ns));value["interval"]=json!(row.interval_seconds);value["finalized_at"]=json!(row.finalized_at_ns.map(chrono::DateTime::from_timestamp_nanos));value["observed_at"]=json!(chrono::DateTime::from_timestamp_nanos(row.source_observed_at_ns));value["received_at"]=json!(chrono::DateTime::from_timestamp_nanos(row.received_at_ns));value["provider_symbol"]=row.source_metadata["provider_symbol"].clone();value["raw_payload"]=row.source_metadata["raw_payload"].clone();
            if row.state=="final" && row.finalized_at_ns.is_none(){value["raw_payload"]["canonical_legacy_finality"]=row.evidence.clone();}
            database_bar(value,&definition.instrument)
        }).collect::<Result<Vec<_>>>()?;
        let mut prepared=tokio::task::spawn_blocking(move||prepare(initial,&catalog,quotes,candles,&receipt,accepted_at_ns,current)).await??;
        for bar in &mut prepared.bars {
            let raw=&mut bar["source"]["raw_payload"];
            if !raw.is_object(){*raw=json!({});}
            stamp_accepted_clock(raw,"capture_accepted_at_ns",accepted_at_ns);
        }
        let result=self.store.commit_projection(ProjectionCommit {position,quotes:prepared.quotes,bars:prepared.bars,errors:vec![],decoder_state}).await;
        let receipt=match result {Ok(receipt)=>receipt,Err(error)=> {self.persistence_tx.send_replace(json!({"state":"unavailable","detail":"原始帧已持久，事实事务失败，等待重试","error":error.to_string()}));return Err(error);}};
        for series in &receipt.hot_series {
            let definition=self.catalog.get(&series.symbol)?;
            let rows=series.bars.iter().map(|row|database_bar(row.clone(),&definition.instrument)).collect::<Result<Vec<_>>>()?;
            prepared.state.reducer.replace_series(SeriesKey {source_id:series.source_id.clone(),instrument:definition.instrument.clone(),interval_seconds:series.interval_seconds.into()},rows)?;
        }
        for row in &receipt.latest_quotes {
            let definition=self.catalog.get(row["instrument_symbol"].as_str().context("canonical quote identity")?)?;
            prepared.state.quotes.replace_canonical(database_quote(row.clone(),&definition.instrument)?)?;
        }
        *self.state.lock().expect("market lock")=prepared.state;
        self.persistence_tx.send_replace(persisted_status(&receipt.version));
        if broadcast {
            for (source,symbol,sample) in prepared.samples {self.streams.publish(&source,&symbol,Some("1s"),json!({"kind":"sample","sample":sample,"snapshot_version":receipt.version}));}
            for (source,symbol) in &prepared.affected {if let Ok(d)=self.catalog.get(symbol) {if let Ok(view)=self.state.lock().expect("market lock").quotes.view(d,source,Utc::now(),true) {
                let live=!view["stale_fields"].as_array().is_some_and(|a|a.iter().any(|v|v=="last"));self.streams.publish(source,symbol,None,json!({"kind":"quote","state":if live{"live"}else{"stale"},"quote":view,"snapshot_version":receipt.version}));
            }}}
            for row in &receipt.changed_bars {
                // Materialized before this transaction returned; publishing never mixes a later read view.
                if let Some(symbol)=row["instrument_symbol"].as_str() {
                    if let Ok(definition)=self.catalog.get(symbol) {
                        match database_bar(row.clone(),&definition.instrument) {
                            Ok(bar)=>self.streams.publish(&bar.source.provider,&bar.instrument.symbol,Some(if bar.interval_seconds==1{"1s"}else{"1m"}),json!({"kind":"bar","state":"committed","bar":bar,"snapshot_version":receipt.version})),
                            Err(error)=>tracing::error!(%error,%symbol,"canonical stream bar serialization failed"),
                        }
                    }
                }
            }
            for change in &receipt.series_changes {
                if change.interval_seconds!=60{continue;}
                self.streams.publish(&change.source_id,&change.symbol,None,json!({"kind":if change.historical_correction {"range_invalidated"}else{"period_tail_changed"},"change":change,"snapshot_version":receipt.version}));
            }
        }
        Ok(receipt.version)
    }
    pub async fn record_decode_failure(&self,position:CapturePosition,channel:&str,detail:&str,decoder_state:Option<Value>)->Result<SnapshotVersion> {
        let _application=self.application.lock().await;let receipt=self.store.commit_rows_with_decoder(position.clone(),vec![],vec![],vec![json!({"position":position,"channel":channel,"detail":detail})],decoder_state).await?;
        self.persistence_tx.send_replace(persisted_status(&receipt.version));Ok(receipt.version)
    }
    pub fn hot_bars(&self,symbol:&str,source:&str,interval:i64)->Result<Vec<tracefang_core::events::RealtimeBar>> {
        let d=self.catalog.get(symbol)?;let key=SeriesKey {source_id:source.into(),instrument:d.instrument.clone(),interval_seconds:interval};Ok(self.state.lock().expect("market lock").reducer.latest(&key,240))
    }
}
fn persisted_status(version:&SnapshotVersion)->Value {json!({"state":"healthy","queue_depth":0,"committed_version":version,"projected_sequence":version.committed_capture.as_ref().map(|p|p.sequence.to_string()),"last_write_at":null,"observed_at":Utc::now()})}
fn stamp_accepted_clock(raw:&mut Value,key:&str,accepted:Option<i64>) {
    // Imported legacy frames have no original local acceptance clock. A replay
    // import timestamp is not source availability, and an existing real clock
    // must survive a later transition with unknown acceptance.
    if let Some(accepted)=accepted {raw[key]=json!(accepted.to_string());}
    else if raw.get(key).is_none(){raw[key]=Value::Null;}
}
fn decorate_source(source:&mut SourceMetadata,position:&CapturePosition,accepted:Option<i64>) {
    if !source.raw_payload.as_ref().is_some_and(Value::is_object) {source.raw_payload=Some(json!({"original_payload":source.raw_payload}));}
    let raw=source.raw_payload.as_mut().expect("source raw object");
    if raw["observation_kind"]=="supplement" {
        raw["supplement_capture_position"]=json!(position);
        stamp_accepted_clock(raw,"supplement_capture_accepted_at_ns",accepted);
        return;
    }
    raw["capture_epoch"]=json!(position.epoch);raw["capture_sequence"]=json!(position.sequence.to_string());raw["capture_digest"]=json!(position.digest);
    stamp_accepted_clock(raw,"capture_accepted_at_ns",accepted);
}
fn prepare(mut state:MarketState,catalog:&Catalog,quotes:Vec<QuoteSnapshot>,candles:Vec<Candle>,position:&CapturePosition,accepted:Option<i64>,current:Vec<tracefang_core::events::RealtimeBar>)->Result<Prepared> {
    for quote in &quotes {quote.validate()?;catalog.get(&quote.instrument.symbol)?;}for candle in &candles {candle.validate()?;catalog.get(&candle.instrument.symbol)?;}
    let mut current=current.into_iter().map(|bar|((bar.source.provider.clone(),bar.instrument.symbol.clone(),bar.interval_seconds,bar.open_time),bar)).collect::<BTreeMap<_,_>>();
    let mut quote_rows=vec![];let mut bars=BTreeMap::new();let mut samples=vec![];let mut affected=BTreeSet::new();
    let append_bar=|bar:tracefang_core::events::RealtimeBar,rows:&mut BTreeMap<(String,String,i64,tracefang_core::domain::Timestamp),Value>,affected:&mut BTreeSet<(String,String)>,current:&mut BTreeMap<(String,String,i64,tracefang_core::domain::Timestamp),tracefang_core::events::RealtimeBar>|->Result<()> {
        affected.insert((bar.source.provider.clone(),bar.instrument.symbol.clone()));current.insert((bar.source.provider.clone(),bar.instrument.symbol.clone(),bar.interval_seconds,bar.open_time),bar.clone());rows.insert((bar.source.provider.clone(),bar.instrument.symbol.clone(),bar.interval_seconds,bar.open_time),serde_json::to_value(bar)?);Ok(())
    };
    for mut quote in quotes {
        decorate_source(&mut quote.source,position,accepted);let symbol=quote.instrument.symbol.clone();let event=state.reducer.normalize_quote(quote.clone())?;
        let source=event.as_ref().map(|e|e.source_id.clone()).or_else(||tracefang_core::events::is_quote_supplement(&quote).then(||quote.source.provider.clone()));
        let mut raw=serde_json::to_value(&quote)?;raw["event_id"]=json!(quote_event_id(&quote));quote_rows.push(raw);
        if let Some(event)=event {samples.push((event.source_id.clone(),symbol.clone(),serde_json::to_value(event.sample())?));for bar in state.reducer.apply_with_current(MarketEvent::Quote(event.clone()),current.iter().filter(|((source,symbol,_,_),_)|source==&event.source_id && symbol==&event.quote.instrument.symbol).map(|(_,v)|v.clone()).collect())? {append_bar(bar,&mut bars,&mut affected,&mut current)?;}}
        let derive=quote.source.provider=="jin10_web" && (symbol=="XAU/USD"||symbol=="USD/CNH");let at=quote.source.received_at;
        if state.quotes.accept(quote) {if let Some(source)=source {affected.insert((source,symbol));}
            if derive {if let Ok(d)=catalog.get("XAUCNHG") {if let Ok(view)=state.quotes.view(d,"jin10_client",at,false) {
                let mut quote:QuoteSnapshot=serde_json::from_value(view["quote"].clone())?;decorate_source(&mut quote.source,position,accepted);
                let mut raw=serde_json::to_value(&quote)?;raw["event_id"]=json!(quote_event_id(&quote));quote_rows.push(raw);
                let event=tracefang_core::events::QuoteEvent {source_id:"jin10_client".into(),channel_id:"jin10_web".into(),quote,sequence:None};
                for bar in state.reducer.apply_with_current(MarketEvent::Quote(event.clone()),current.iter().filter(|((source,symbol,_,_),_)|source==&event.source_id && symbol==&event.quote.instrument.symbol).map(|(_,v)|v.clone()).collect())? {append_bar(bar,&mut bars,&mut affected,&mut current)?;}
            }}}
        }
    }
    for mut candle in candles {decorate_source(&mut candle.source,position,accepted);if let Some(event)=state.reducer.normalize_bar(candle)? {for bar in state.reducer.apply_with_current(MarketEvent::Bar(event.clone()),current.get(&(event.source_id.clone(),event.candle.instrument.symbol.clone(),event.candle.interval_seconds,event.candle.open_time)).cloned().into_iter().collect())? {append_bar(bar,&mut bars,&mut affected,&mut current)?;}}}
    Ok(Prepared {state,quotes:quote_rows,bars:bars.into_values().collect(),samples,affected})
}
