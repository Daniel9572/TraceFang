//! Quant reads only the exact isolated replay facts prefix, never the live Store/view page.
use super::{checkpoint::ReplayScope,SeekToken};
use crate::{api::{AppState,ApiError},catalog::Definition};
use anyhow::{Context,Result,ensure};
use axum::{Json,extract::State};
use chrono::{DateTime,Utc};
use serde::Deserialize;
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{collections::{BTreeMap,BTreeSet},sync::{Arc,LazyLock,Mutex}};
use tracefang_core::{native_store::Store,persistence_contract::{CapturePosition,SnapshotVersion,CanonicalScanRequest,CanonicalScanBatch},periods::{Period,MarketSchedule},quant_core::{quant::{Parameters,QuantInput,QuantQuote,SnapshotToken,HistorySemantics},snapshot::{SnapshotAccumulator,QuantSnapshot},exact::parse}};

pub struct ReplayQuantView {
 pub store:Store,pub version:SnapshotVersion,pub scope:ReplayScope,pub definition:Definition,pub period:Period,pub schedule:MarketSchedule,
 pub position:CapturePosition,pub knowledge_at:DateTime<Utc>,pub origin_prefix_complete:bool,pub cancel:SeekToken,
}
static REGISTRY:LazyLock<Mutex<BTreeMap<String,Arc<ReplayQuantView>>>>=LazyLock::new(||Mutex::new(BTreeMap::new()));
static QUANT_WORKERS:LazyLock<Arc<tokio::sync::Semaphore>>=LazyLock::new(||Arc::new(tokio::sync::Semaphore::new(2)));
pub fn publish(session:&str,view:ReplayQuantView)->Result<()> {
 ensure!(view.version.committed_capture.as_ref()==Some(&view.position),"replay quant facts do not cover the requested prefix");
 let mut registry=REGISTRY.lock().map_err(|_|anyhow::anyhow!("replay registry unavailable"))?;
 ensure!(registry.contains_key(session)||registry.len()<4,"replay session budget exhausted");if let Some(old)=registry.insert(session.into(),Arc::new(view)){old.cancel.cancel();}Ok(())
}
pub fn clear(session:&str){if let Ok(mut registry)=REGISTRY.lock(){if let Some(view)=registry.remove(session){view.cancel.cancel();}}}
pub struct SessionGuard(pub String);
impl Drop for SessionGuard{fn drop(&mut self){clear(&self.0)}}
#[derive(Deserialize)]#[serde(deny_unknown_fields)]
pub struct Request {pub session_id:String,#[serde(with="tracefang_core::persistence_contract::u64_string")]pub cursor:u64,#[serde(default)]pub parameters:Parameters}
pub async fn endpoint(State(_state):State<AppState>,Json(request):Json<Request>)->Result<Json<QuantSnapshot>,ApiError>{
 let view=REGISTRY.lock().map_err(|_|ApiError(axum::http::StatusCode::SERVICE_UNAVAILABLE,"回放会话不可用".into()))?.get(&request.session_id).cloned().ok_or_else(||ApiError(axum::http::StatusCode::NOT_FOUND,"回放会话已结束或尚未应用输入".into()))?;
 if view.position.sequence!=request.cursor{return Err(ApiError(axum::http::StatusCode::CONFLICT,"回放游标已改变，请使用当前已应用游标".into()))}
 let permit=QUANT_WORKERS.clone().try_acquire_owned().map_err(|_|ApiError(axum::http::StatusCode::TOO_MANY_REQUESTS,"指标计算正在进行，请稍后重试".into()))?;
 let result=calculate(view.clone(),request.parameters,None).await.map_err(|error|ApiError(axum::http::StatusCode::CONFLICT,error.to_string()))?;view.cancel.check()?;
 let current=REGISTRY.lock().map_err(|_|ApiError(axum::http::StatusCode::SERVICE_UNAVAILABLE,"回放会话不可用".into()))?.get(&request.session_id).cloned();
 if !current.is_some_and(|value|Arc::ptr_eq(&value,&view)){return Err(ApiError(axum::http::StatusCode::CONFLICT,"回放游标已改变，旧计算已取消".into()))}
 drop(permit);Ok(Json(result.current()?))
}
fn unsigned(value:&Value)->Option<u64>{value.as_u64().or_else(||value.as_str()?.parse().ok())}
fn signed(value:&Value)->Option<i64>{value.as_i64().or_else(||value.as_str()?.parse().ok())}
fn structural_facts(view:&ReplayQuantView,scan:&tracefang_core::persistence_contract::CanonicalScanContext)->Result<Vec<tracefang_core::quant_core::quant::ExternalFact>>{
 let scope=view.scope.key()?;
 scan.external_facts.iter().filter(|row|matches!(row.kind.as_str(),"calendar_coverage"|"source_point_coverage"|"source_clock_coverage")).map(|row|{
  let mut provenance=row.provenance.clone();if let Some(map)=provenance.as_object_mut(){map.remove("snapshot_version");}
  if !provenance.is_object(){provenance=json!({"original_structural_provenance":provenance});}
  provenance["replay_authority"]=json!({"capture_position":view.position,"scope_build_identity":scope,"knowledge_at":view.knowledge_at});
  // Physical MVCC identity remains in SnapshotToken. Structural facts use the
  // authoritative original prefix identity so a regenerated cache is reproducible.
  let record_id=hex::encode(Sha256::digest(serde_json::to_vec(&json!({"kind":row.kind,"source":row.source,"value":row.value,"observed_at_ns":row.observed_at_ns,"published_at_ns":row.published_at_ns,"received_at_ns":row.received_at_ns,"unavailable_reason":row.unavailable_reason,"provenance":provenance}))?));
  Ok(tracefang_core::quant_core::quant::ExternalFact{kind:row.kind.clone(),source:row.source.clone(),record_id:Some(record_id),revision:Some(view.position.sequence),provenance,observed_at:row.observed_at_ns.map(DateTime::from_timestamp_nanos),published_at:row.published_at_ns.map(DateTime::from_timestamp_nanos),received_at:row.received_at_ns.map(DateTime::from_timestamp_nanos),unavailable_reason:row.unavailable_reason.clone(),value:row.value.clone()})
 }).collect()
}
fn template(view:&ReplayQuantView,batch:&CanonicalScanBatch)->Result<QuantInput>{
 let scan=batch.context.as_ref().context("replay quant scan has no initial context")?;
 ensure!(batch.version.committed_capture.as_ref()==Some(&view.position),"replay quant input belongs to a different capture prefix");
 let quote=scan.quote.as_ref().map(|row|->Result<_>{let raw=&row["source"]["raw_payload"];Ok(QuantQuote{price:parse(row["price"].as_str().context("exact replay quote price missing")?)?,observed_at:DateTime::from_timestamp_nanos(signed(&row["observed_at_ns"]).context("quote source clock missing")?),received_at:DateTime::from_timestamp_nanos(signed(&row["received_at_ns"]).context("quote receive clock missing")?),accepted_at:signed(&raw["capture_accepted_at_ns"]).map(DateTime::from_timestamp_nanos),applied_frame_seq:unsigned(&raw["capture_sequence"]),source_precision_ns:unsigned(&raw["source_precision_ns"]).or_else(||unsigned(&raw["timestamp_precision_seconds"]).and_then(|v|v.checked_mul(1_000_000_000)))})}).transpose()?.filter(|quote|quote.applied_frame_seq.is_some_and(|v|v<=view.position.sequence)&&quote.observed_at<=view.knowledge_at&&quote.received_at<=view.knowledge_at&&quote.accepted_at.is_none_or(|v|v<=view.knowledge_at));
 let mut capabilities=BTreeSet::from(["original_event_replay".into(),"independent_complete_captured_prefix_facts".into(),"contract_specification_unverified".into()]);if !view.origin_prefix_complete{capabilities.insert("missing_original_prefix_warmup_incomplete".into());}
 let calendar_complete=scan.coverage["calendar_projection"]["complete"]!=false;
 let source_complete=scan.coverage["source_event_coverage_complete"]!=false&&scan.coverage["source_clock"]["verified"]!=false;
 if !source_complete{capabilities.insert("source_event_coverage_incomplete".into());}
 if !view.period.is_base()&&!calendar_complete{capabilities.insert("derived_calendar_coverage_incomplete".into());}
 let version=&batch.version;
    let series=&scan.coverage["series_version"];
    let series_version=if series.is_null(){None}else{Some(tracefang_core::quant_core::quant::SeriesVersion{series_generation:series["series_generation"].as_str().context("replay series generation")?.into(),correction_epoch:unsigned(&series["correction_epoch"]).context("replay series correction epoch")?,append_watermark_ns:series["append_watermark_ns"].as_str().map(str::to_owned),last_mutation_commit_id:unsigned(&series["last_mutation_commit_id"]).context("replay mutation version")?})};
    Ok(QuantInput{code:view.definition.code.clone(),instrument:view.definition.instrument.symbol.clone(),name:view.definition.name.clone(),unit:view.definition.price_unit.clone(),executable_contract:false,source_id:view.scope.source.clone(),period:view.period.as_str().into(),decision_as_of:view.knowledge_at,application_cursor:Some(view.position.sequence),token:SnapshotToken{store_epoch:version.store_epoch.clone(),commit_id:version.commit_id,capture_epoch:Some(view.position.epoch.clone()),capture_digest:Some(view.position.digest.clone()),aggregation_version:version.aggregation_version.clone(),schema_version:version.schema_version.clone(),projector_version:view.scope.projector_version.clone(),committed_frame_seq:view.position.sequence,committed_part:None,catalog_version:view.scope.catalog_hash.clone(),schedule_version:scan.coverage["calendar_projection"]["schedule_version"].as_str().unwrap_or(&view.scope.schedule_hash).to_owned(),route_version:view.scope.key()?},semantics:HistorySemantics::OriginalEventReplay,derivation_id:Some(view.scope.key()?),capabilities,revision_start:None,revision_end:None,warmup_complete:view.origin_prefix_complete&&source_complete&&(view.period.is_base()||calendar_complete),series_version,bars:vec![],quote,external_facts:structural_facts(view,scan)?})
}
/// The same bounded accumulator is restored only under native prefix-correction proof.
/// A past final revision invalidates resume and falls back to a full ordered facts scan.
pub async fn calculate(view:Arc<ReplayQuantView>,parameters:Parameters,initial:Option<SnapshotAccumulator>)->Result<SnapshotAccumulator>{
 parameters.validate()?;view.cancel.check()?;
 let had_initial=initial.is_some();
 let resume=initial.as_ref().and_then(SnapshotAccumulator::resume_cursor).map(|cursor|->Result<_>{Ok(tracefang_core::native_store::ScanResume{after_ns:cursor.after.timestamp_nanos_opt().context("resume clock outside ns")?,series_generation:cursor.series_version.series_generation,correction_epoch:cursor.series_version.correction_epoch,append_watermark_ns:cursor.series_version.append_watermark_ns.as_deref().map(str::parse).transpose()?})}).transpose()?;
 let accumulator=Arc::new(Mutex::new(Some(initial.map(SnapshotAccumulator::resume).unwrap_or(SnapshotAccumulator::new(parameters.clone())?))));
 let worker=accumulator.clone();let context=view.clone();let mut metadata=None;
 let accept=move|batch:CanonicalScanBatch|->Result<()> {context.cancel.check()?;if batch.context.is_some(){metadata=Some(template(&context,&batch)?)}let mut input=metadata.as_ref().context("replay quant authority metadata missing")?.clone();input.bars=batch.rows.into_iter().map(super::bar_adapter::bar).collect::<Result<_>>()?;worker.lock().map_err(|_|anyhow::anyhow!("replay quant accumulator poisoned"))?.as_mut().context("replay quant accumulator ended")?.push_replay(input)?;context.cancel.check()?;Ok(())};
 let request=CanonicalScanRequest{symbol:view.definition.instrument.symbol.clone(),source_id:view.scope.source.clone(),interval_seconds:if view.period==Period::S1{1}else{60},start_ns:i64::MIN,end_ns:view.knowledge_at.timestamp_nanos_opt().context("replay knowledge clock outside ns")?,final_only:false,expected_version:Some(view.version.clone())};
 let result=if view.period.is_base(){view.store.canonical_scan_with_resume(request,256,resume,accept).await}else{view.store.canonical_calendar_scan(request,view.period,Some(view.schedule.clone()),256,resume,accept).await};
 match result {
  Ok(summary)=>{ensure!(summary.complete&&summary.version.committed_capture.as_ref()==Some(&view.position),"replay quant scan is incomplete");view.cancel.check()?;Ok(accumulator.lock().map_err(|_|anyhow::anyhow!("replay quant accumulator poisoned"))?.take().context("replay quant scan produced no metadata")?)},
  Err(error) if had_initial&&error.to_string().contains("quant_resume_invalid")=>Box::pin(calculate(view,parameters,None)).await,
  Err(error)=>Err(error),
 }
}

#[cfg(test)]mod tests {
 use super::*;
 use tracefang_core::persistence_contract::{CanonicalScanContext,ExternalFactRecord,ExternalFactScope};
 #[tokio::test]async fn structural_prefix_hash_is_independent_of_cache_epoch_but_binds_proof_and_cutoff()->Result<()>{
  let dir=tempfile::tempdir()?;let catalog=crate::catalog::Catalog::embedded()?;let definition=catalog.get("XAUUSD")?.clone();let schedule:MarketSchedule=serde_json::from_value(catalog.schedules[&definition.market_schedule_id].clone())?;
  let position=CapturePosition{epoch:"original-capture-epoch".into(),sequence:9,digest:"a".repeat(64)};
  let scope=ReplayScope{projector_version:"fixed-build".into(),catalog_hash:"fixed-catalog".into(),schedule_hash:"request-baseline".into(),instrument:definition.instrument.symbol.clone(),source:"jin10_client".into(),period:"1d".into(),epoch:position.epoch.clone()};
  let first=Store::open_replay(dir.path().join("one.redb"))?;let second=Store::open_replay(dir.path().join("two.redb"))?;
  let make=|store:Store,mut version:SnapshotVersion|{version.committed_capture=Some(position.clone());ReplayQuantView{store,version,scope:scope.clone(),definition:definition.clone(),period:Period::D1,schedule:schedule.clone(),position:position.clone(),knowledge_at:DateTime::from_timestamp_nanos(1_800_000_000_123_456_789),origin_prefix_complete:true,cancel:SeekToken::default()}};
  let a=make(first.clone(),first.version().await?);let mut b=make(second.clone(),second.version().await?);
  ensure!(a.version.store_epoch!=b.version.store_epoch,"fixture stores lack independent physical identities");
  let batch=|view:&ReplayQuantView|CanonicalScanBatch{version:view.version.clone(),context:Some(CanonicalScanContext{quote:None,capabilities:json!([]),coverage:json!({"calendar_projection":{"schedule_version":"captured-calendar-hash","complete":true},"source_clock":{"verified":true}}),semantics:"original_event_replay".into(),external_facts:vec![ExternalFactRecord{scope:ExternalFactScope{instrument_symbol:definition.instrument.symbol.clone(),market_source_id:Some(scope.source.clone())},kind:"calendar_coverage".into(),source:scope.source.clone(),record_id:format!("temporary-{}",view.version.store_epoch),revision:view.version.commit_id.max(1),observed_at_ns:Some(1_799_999_999_987_654_321),published_at_ns:None,received_at_ns:Some(1_800_000_000_000_000_001),value:json!({"source_body_sha256":"b".repeat(64),"capture_position":position,"source_clock_policy":"verified-fixed-policy"}),unavailable_reason:None,provenance:json!({"snapshot_version":view.version,"source_row_sha256":"c".repeat(64)})}]}),row_offset:0,rows:vec![]};
  let ia=template(&a,&batch(&a))?;let ib=template(&b,&batch(&b))?;
  ensure!(ia.token.store_epoch!=ib.token.store_epoch&&ia.token.schedule_version=="captured-calendar-hash","real MVCC identity or same-view calendar token lost");
  ensure!(serde_json::to_value(&ia.external_facts)?==serde_json::to_value(&ib.external_facts)?,"temporary cache identity entered business source evidence");
  let calculate=|input:QuantInput|->Result<SnapshotAccumulator>{let mut accumulator=SnapshotAccumulator::new(Default::default())?;accumulator.push_replay(input)?;Ok(accumulator)};
  let ca=calculate(ia.clone())?;let cb=calculate(ib.clone())?;let sa=ca.current()?;let sb=cb.current()?;
  ensure!(sa.evidence.input_hash==sb.evidence.input_hash&&sa.evidence.snapshot_hash==sb.evidence.snapshot_hash,"same original prefix changed business hash across cache rebuild");
  let restored=SnapshotAccumulator::restore(ca.snapshot()?,&Default::default())?.current()?;
  ensure!(restored.evidence.input_hash==sb.evidence.input_hash&&restored.evidence.snapshot_hash==sb.evidence.snapshot_hash,"complete accumulator checkpoint changed structural evidence hash");
  let mut changed=batch(&b);changed.context.as_mut().unwrap().external_facts[0].value["source_body_sha256"]=json!("d".repeat(64));
  ensure!(calculate(template(&b,&changed)?)?.current()?.evidence.input_hash!=sa.evidence.input_hash,"changed source body proof reused old business hash");
  changed=batch(&b);changed.context.as_mut().unwrap().external_facts[0].value["source_clock_policy"]=json!("another-verified-policy");
  ensure!(calculate(template(&b,&changed)?)?.current()?.evidence.snapshot_hash!=sa.evidence.snapshot_hash,"changed clock policy reused old business hash");
  b.knowledge_at+=chrono::Duration::nanoseconds(1);
  ensure!(calculate(template(&b,&batch(&b))?)?.current()?.evidence.input_hash!=sa.evidence.input_hash,"changed exact cutoff reused prior input hash");
  let mut future=batch(&a);future.version.committed_capture.as_mut().unwrap().sequence+=1;
  ensure!(template(&a,&future).is_err(),"future facts cursor admitted into historical prefix");
  ensure!(ia.external_facts[0].received_at==Some(DateTime::from_timestamp_nanos(1_800_000_000_000_000_001)),"real source evidence knowledge clock altered");
  first.close().await?;second.close().await?;Ok(())
 }
}
