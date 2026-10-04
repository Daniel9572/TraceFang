//! Server-owned authority adapter. Every batch is from one native MVCC read view.
use anyhow::{Context,Result,ensure};
use chrono::{DateTime,Utc};
use serde_json::Value;
use std::{collections::BTreeSet,sync::{Arc,Mutex}};
use tracefang_core::{persistence_contract::{CanonicalScanRequest,CanonicalScanSummary,CanonicalScanBatch},native_store::ScanResume,periods::Period};
use crate::{api::AppState,analysis::{quant::*,exact::parse},catalog::Definition};

fn timestamp(value:i64)->DateTime<Utc>{DateTime::from_timestamp_nanos(value)}
fn unsigned(value:&Value)->Option<u64>{value.as_u64().or_else(||value.as_str()?.parse().ok())}
fn signed(value:&Value)->Option<i64>{value.as_i64().or_else(||value.as_str()?.parse().ok())}
fn source_precision(raw:&Value)->Option<u64>{unsigned(&raw["source_precision_ns"]).or_else(||unsigned(&raw["timestamp_precision_seconds"]).and_then(|v|v.checked_mul(1_000_000_000)))}
#[path="quant_bar_adapter.rs"] mod bar_adapter;
pub use bar_adapter::bar;
fn context(batch:&CanonicalScanBatch,definition:&Definition,source:&str,period:Period,cutoff:DateTime<Utc>)->Result<QuantInput>{
    let scan=batch.context.as_ref().context("first quant scan batch requires MVCC context")?;
    let version=&batch.version;let capture=version.committed_capture.as_ref();
    let mut capabilities=scan.capabilities.as_array().into_iter().flatten().filter_map(Value::as_str).map(str::to_owned).collect::<BTreeSet<_>>();
    capabilities.extend(["native_committed_facts","final_revision_history","available_coverage_prefix","contract_specification_unverified"].map(str::to_owned));
    let series=scan.coverage["series_version"].clone();
    let series_version=if series.is_null(){None}else{Some(SeriesVersion {series_generation:series["series_generation"].as_str().context("series generation")?.into(),correction_epoch:unsigned(&series["correction_epoch"]).context("series correction epoch")?,append_watermark_ns:series["append_watermark_ns"].as_str().map(str::to_owned),last_mutation_commit_id:unsigned(&series["last_mutation_commit_id"]).context("series mutation version")?})};
    let quote=scan.quote.as_ref().map(|row|->Result<_>{let raw=&row["source"]["raw_payload"];Ok(QuantQuote {price:parse(row["price"].as_str().context("exact quote price")?)?,observed_at:timestamp(signed(&row["observed_at_ns"]).context("quote source time")?),received_at:timestamp(signed(&row["received_at_ns"]).context("quote receive time")?),accepted_at:signed(&raw["capture_accepted_at_ns"]).map(timestamp),applied_frame_seq:unsigned(&raw["capture_sequence"]),source_precision_ns:source_precision(raw)})}).transpose()?.filter(|q|q.observed_at<=cutoff && q.received_at<=cutoff && q.accepted_at.is_none_or(|at|at<=cutoff));
    if quote.is_none(){capabilities.insert("quote_unavailable_at_decision".into());}
    let calendar_complete=scan.coverage["calendar_projection"]["complete"]!=false;
    let source_events_complete=scan.coverage["source_event_coverage_complete"]!=false && scan.coverage["source_clock"]["verified"]!=false;
    if !source_events_complete {capabilities.insert("source_event_coverage_incomplete".into());}
    if !period.is_base() && !calendar_complete {capabilities.insert("derived_calendar_coverage_incomplete".into());}
    Ok(QuantInput {code:definition.code.clone(),instrument:definition.instrument.symbol.clone(),name:definition.name.clone(),unit:definition.price_unit.clone(),executable_contract:false,source_id:source.into(),period:period.as_str().into(),decision_as_of:cutoff,application_cursor:None,token:SnapshotToken {store_epoch:version.store_epoch.clone(),commit_id:version.commit_id,capture_epoch:capture.map(|v|v.epoch.clone()),capture_digest:capture.map(|v|v.digest.clone()),aggregation_version:version.aggregation_version.clone(),schema_version:version.schema_version.clone(),projector_version:version.projector_version.clone(),committed_frame_seq:capture.map_or(0,|v|v.sequence),committed_part:None,catalog_version:version.catalog_version.clone(),schedule_version:scan.coverage["calendar_projection"]["schedule_version"].as_str().unwrap_or(&version.schedule_version).to_owned(),route_version:version.route_version.clone()},semantics:HistorySemantics::FinalRevisionHistory,derivation_id:None,capabilities,revision_start:signed(&series["revision_start_ns"]).map(timestamp),revision_end:signed(&series["revision_end_ns"]).map(timestamp),warmup_complete:scan.coverage["history"]["warmup_complete"]==true && source_events_complete && (period.is_base() || calendar_complete),series_version,bars:vec![],quote,external_facts:scan.external_facts.iter().map(|r|ExternalFact {record_id:Some(r.record_id.clone()),revision:Some(r.revision),provenance:r.provenance.clone(),kind:r.kind.clone(),source:r.source.clone(),observed_at:r.observed_at_ns.map(timestamp),published_at:r.published_at_ns.map(timestamp),received_at:r.received_at_ns.map(timestamp),unavailable_reason:r.unavailable_reason.clone(),value:r.value.clone()}).collect()})
}
pub async fn scan<F>(state:&AppState,request:&QuantInputRequest,batch_rows:usize,on_batch:F)->Result<CanonicalScanSummary>
where F:FnMut(QuantInput)->Result<()>+Send+'static {
    scan_controlled(state,request,batch_rows,Arc::new(||false),on_batch).await
}
pub async fn scan_controlled<F>(state:&AppState,request:&QuantInputRequest,batch_rows:usize,cancel:Arc<dyn Fn()->bool+Send+Sync>,mut on_batch:F)->Result<CanonicalScanSummary>
where F:FnMut(QuantInput)->Result<()>+Send+'static {
    ensure!(!cancel(),"quant_cancelled");
    if request.research_snapshot_id.is_some(){return state.research.scan_authority(request,batch_rows,move|input|{ensure!(!cancel(),"quant_cancelled");on_batch(input)}).await;}
    ensure!(request.research_asset.is_none() && request.research_adjustment.is_none(),"research scope requires an immutable research snapshot reference");
    ensure!(request.application_cursor.is_none(),"original_event_replay requires the independent replay session facts view");
    let definition=state.market.catalog.get(&request.code)?.clone();
    let source=request.source_id.clone().unwrap_or(state.market.source(&definition.instrument.symbol)?);
    ensure!(definition.source_ids.contains(&source),"source is not configured for this instrument");
    let period=Period::parse(if request.period.is_empty(){"1m"}else{&request.period})?;
    ensure!(period!=Period::Timeline,"quant bars require an interval period");
    let cutoff=request.decision_as_of.unwrap_or_else(Utc::now);let end=request.end.unwrap_or(cutoff).min(cutoff);
    ensure!(request.start.is_none_or(|start|start<=end),"quant range is inverted");
    let end_ns=end.timestamp_nanos_opt().context("quant end outside signed nanoseconds")?;
    let resume=request.resume.as_ref().map(|r|->Result<_>{Ok(ScanResume {after_ns:r.after.timestamp_nanos_opt().context("quant_resume_invalid: time outside ns")?,series_generation:r.series_version.series_generation.clone(),correction_epoch:r.series_version.correction_epoch,append_watermark_ns:r.series_version.append_watermark_ns.as_deref().map(str::parse).transpose()?})}).transpose()?;
    // Recursive indicators require every stored bar before the simulation's trade
    // start. start restricts evaluation/output downstream, never the warmup scan.
    let scan=CanonicalScanRequest {symbol:definition.instrument.symbol.clone(),source_id:source.clone(),interval_seconds:if period==Period::S1{1}else{60},start_ns:i64::MIN,end_ns,final_only:false,expected_version:Some(state.market.store.version().await?)};
    let schedule=Some(crate::pages::schedule(&state.market,&request.code)?);
    let materialized=if let Some(proof)=resume.clone() {
        match state.market.store.materialize_tail(scan.clone(),period,schedule.clone(),proof,4096,16*1024*1024,cancel.clone()).await {
            Ok(input)=>Some(input),
            Err(error) if error.downcast_ref::<tracefang_core::native_store::MaterializedTailTooLarge>().is_some()=>None,
            Err(error)=>return Err(error),
        }
    }else{None};
    let mut template=None;
    let consumer_cancel=cancel.clone();
    let mut accept=move|version:tracefang_core::persistence_contract::SnapshotVersion,scan_context:Option<tracefang_core::persistence_contract::CanonicalScanContext>,row_offset:u64,bars:Vec<QuantBar>|->Result<()> {
        ensure!(!consumer_cancel(),"quant_cancelled");
        if scan_context.is_some(){template=Some(context(&CanonicalScanBatch{version,context:scan_context,row_offset,rows:vec![]},&definition,&source,period,cutoff)?);}
        let mut input=template.as_ref().context("missing first quant MVCC batch")?.clone();
        input.bars=bars;input.validate()?;on_batch(input)
    };
    if let Some(input)=materialized {
        return tokio::task::spawn_blocking(move|| {for batch in input.batches {let bars=batch.rows.into_iter().map(bar).collect::<Result<Vec<_>>>()?;accept(batch.version,batch.context,batch.row_offset,bars)?;}Ok(input.summary)}).await?;
    }
    let root=std::env::var_os("TRACEFANG_BATCH_SNAPSHOTS_DIR").map(std::path::PathBuf::from)
        .unwrap_or_else(||state.market.store.file_path().parent().unwrap_or_else(||std::path::Path::new(".")).join("batch-snapshots"));
    let published=crate::batch_snapshot::publish(&root,&state.market.store,crate::batch_snapshot::Plan {
        scan,period,schedule,resume,build:crate::replay::projector_build_hash()
    },cancel.clone(),Arc::new(|phase|tracing::debug!(progress=%phase,"native immutable input preparation"))).await?;
    tokio::task::spawn_blocking(move||crate::batch_snapshot::scan_quant(&published,batch_rows,&cancel,move|batch|accept(batch.version,batch.context,batch.row_offset,batch.bars))).await?
}
/// Small callers get a complete prefix or an explicit range-size error.
pub async fn context_only(state:&AppState,request:&QuantInputRequest)->Result<QuantInput> {
    ensure!(request.research_snapshot_id.is_none() && request.application_cursor.is_none(),"context-only live authority requires a native Store scope");
    let definition=state.market.catalog.get(&request.code)?;let source=request.source_id.clone().unwrap_or(state.market.source(&definition.instrument.symbol)?);
    ensure!(definition.source_ids.contains(&source),"source is not configured for this instrument");
    let period=Period::parse(if request.period.is_empty(){"1m"}else{&request.period})?;
    let cutoff=request.decision_as_of.unwrap_or_else(Utc::now);let end=request.end.unwrap_or(cutoff).min(cutoff);
    let schedule=Some(crate::pages::schedule(&state.market,&request.code)?);
    let batch=state.market.store.canonical_scan_context(CanonicalScanRequest {symbol:definition.instrument.symbol.clone(),source_id:source.clone(),interval_seconds:if period==Period::S1{1}else{60},start_ns:i64::MIN,end_ns:end.timestamp_nanos_opt().context("context cutoff outside signed ns")?,final_only:false,expected_version:None},schedule).await?;
    let input=context(&batch,definition,&source,period,cutoff)?;input.validate()?;Ok(input)
}
pub async fn read(state:&AppState,request:&QuantInputRequest)->Result<QuantInput>{
    let input=Arc::new(Mutex::new(None::<QuantInput>));let output=input.clone();
    scan(state,request,1000,move|batch|{let mut slot=output.lock().expect("quant read accumulator");
        if let Some(input)=slot.as_mut(){ensure!(input.bars.len()+batch.bars.len()<=10_000,"quant_read_range_too_large: use the bounded scan adapter");input.bars.extend(batch.bars);}else{*slot=Some(batch);}Ok(())
    }).await?;
    let result=input.lock().expect("quant read accumulator").take().context("quant scan did not deliver context")?;Ok(result)
}
