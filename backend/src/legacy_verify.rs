//! Independent source-field oracle, compared to bounded reopened native lookups.
use anyhow::{Context,Result,ensure};
use chrono::{DateTime,Utc};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{path::Path,io::{BufRead,BufReader},fs::File};
use tracefang_core::{native_store::{Store,CanonicalBarKey,CanonicalQuoteKey},domain::Decimal};
use crate::legacy_import::{LegacyManifest,file_hash};
fn text(v:&Value)->Result<String>{match v{Value::String(v)=>Ok(v.clone()),Value::Number(v)=>Ok(v.to_string()),_=>anyhow::bail!("source exact field missing")}}
fn ns(v:&Value)->Result<i64>{text(v)?.parse::<DateTime<Utc>>()?.timestamp_nanos_opt().context("source timestamp outside exact ns")}
fn decimal(v:&Value)->Result<Value>{if v.is_null(){Ok(Value::Null)}else{Ok(json!(Decimal::from_str_exact(&text(v)?)?.to_string()))}}
fn canonical_source(raw:&str)->String {if raw.starts_with("jin10_"){"jin10_client".into()}else{raw.into()}}
fn source_metadata(row:&Value)->Value {json!({"provider":row["realtime_source_id"].as_str().or_else(||row["source_id"].as_str()),"provider_symbol":row["provider_symbol"],"observed_at":row["observed_at"],"received_at":row["received_at"],"raw_payload":if row["source_raw_payload"].is_null(){&row["raw_payload"]}else{&row["source_raw_payload"]},"legacy_precision":"NUMERIC declared typmod; pg timestamp microseconds"})}
fn expected_bar(row:&Value)->Result<Value>{
 let raw=row["realtime_source_id"].as_str().or_else(||row["source_id"].as_str()).context("source bar identity missing")?;
 let interval=text(&row["interval_seconds"])?.parse::<u32>()?;let open=ns(&row["open_time"])?;
 let end=if row["close_time"].is_null(){open.checked_add(i64::from(interval).checked_mul(1_000_000_000).context("interval overflow")?).context("end overflow")?}else{ns(&row["close_time"])?};
 Ok(json!({"instrument_symbol":row["instrument_symbol"],"realtime_source_id":canonical_source(raw),"evidence_channel_id":row["evidence_channel_id"].as_str().or_else(||row["upstream_channel_id"].as_str()).unwrap_or(raw),"interval_seconds":interval,"open_time_ns":open.to_string(),"close_time_ns":end.to_string(),"open":decimal(&row["open"])?,"high":decimal(&row["high"])?,"low":decimal(&row["low"])?,"close":decimal(&row["close"])?,"volume":decimal(&row["volume"])?,"revision":if row["revision"].is_null(){"1".to_owned()}else{text(&row["revision"])?},"received_sequence":if row["received_sequence"].is_null(){None}else{Some(text(&row["received_sequence"])?)},"state":row["state"].as_str().unwrap_or("final"),"finalized_at_ns":if row["finalized_at"].is_null(){None}else{Some(ns(&row["finalized_at"])?.to_string())},"source_observed_at_ns":ns(&row["observed_at"])?.to_string(),"received_at_ns":ns(&row["received_at"])?.to_string(),"source_metadata":source_metadata(row)}))
}
fn semantic_row(mut row:Value)->Value{row.as_object_mut().unwrap().remove("evidence");if let Some(raw)=row["source_metadata"]["raw_payload"].as_object_mut(){raw.remove("applied_commit_id");}row}
async fn compare_bars(store:&Store,values:Vec<(u64,Value)>,manifest:&LegacyManifest,sha:&str,expected_hash:&mut Sha256,actual_hash:&mut Sha256)->Result<u64>{
 let expected=values.iter().map(|(_,v)|expected_bar(v)).collect::<Result<Vec<_>>>()?;
 let keys=expected.iter().map(|v|->Result<CanonicalBarKey>{Ok(CanonicalBarKey{source_id:text(&v["realtime_source_id"])?,symbol:text(&v["instrument_symbol"])?,interval_seconds:v["interval_seconds"].as_u64().context("interval")?.try_into()?,open_time_ns:text(&v["open_time_ns"])?.parse()?})}).collect::<Result<_>>()?;
 let (_,actual)=store.lookup_bars(keys).await?;let count=expected.len();ensure!(actual.len()==count,"lookup count differs");
 for (((offset,input),expected),actual) in values.into_iter().zip(expected).zip(actual){
  let actual=actual.context("selected fixed source bar missing from reopened native facts")?;
  ensure!(actual.evidence["fixed_snapshot"]["canonical_file_sha256"]==sha&&actual.evidence["fixed_snapshot"]["source_fingerprint"]==manifest.postgres["fingerprint"]&&actual.evidence["canonical_selection"]["source_records"]==input["_legacy_selection"]["source_records"]&&actual.evidence["legacy_row_ref"]["canonical_row_offset"]==offset.to_string(),"selected source lineage differs at row {offset}");
  if !input["_legacy_clock_projection"].is_null(){ensure!(actual.evidence["source_clock_projection"]==input["_legacy_clock_projection"],"source clock mapping evidence differs at row {offset}");}
  let actual=semantic_row(serde_json::to_value(actual)?);ensure!(actual==expected,"source-to-native semantic field mismatch at canonical row {offset}");
  expected_hash.update(serde_json::to_vec(&expected)?);expected_hash.update(b"\n");actual_hash.update(serde_json::to_vec(&actual)?);actual_hash.update(b"\n");
 }Ok(count as u64)
}
pub async fn verify_bars(dir:&Path,manifest:&LegacyManifest,store:&Store)->Result<Value>{
 verify_bars_from(dir,"canonical-bars-v1.manifest.json",manifest,store).await
}
pub async fn verify_clock_bars(dir:&Path,manifest:&LegacyManifest,store:&Store)->Result<Value>{
 verify_bars_from(dir,"canonical-bars-clock-v2.manifest.json",manifest,store).await
}
async fn verify_bars_from(dir:&Path,descriptor:&str,manifest:&LegacyManifest,store:&Store)->Result<Value>{
 let plan:Value=serde_json::from_slice(&std::fs::read(dir.join(descriptor))?)?;
 ensure!(plan["source_manifest_id"]==manifest.id&&plan["snapshot"]==manifest.postgres["snapshot"],"bar oracle belongs to another source snapshot");
 let file=dir.join(plan["file"].as_str().context("canonical source file missing")?);let sha=text(&plan["sha256"])?;let check=file.clone();ensure!(tokio::task::spawn_blocking(move||file_hash(&check)).await??==sha,"canonical source checksum differs");
 let (sender,mut receiver)=tokio::sync::mpsc::channel(2);
 let worker=tokio::task::spawn_blocking(move||->Result<u64>{let mut batch=vec![];let mut count=0;let mut bytes=0;for line in BufReader::new(File::open(file)?).lines(){let line=line?;if !batch.is_empty()&&(batch.len()==1000||bytes+line.len()>4*1024*1024){sender.blocking_send(std::mem::take(&mut batch)).context("bar verification receiver ended")?;bytes=0;}bytes+=line.len();batch.push((count,serde_json::from_str::<Value>(&line)?));count+=1;}if !batch.is_empty(){sender.blocking_send(batch).context("bar verification receiver ended")?;}Ok(count)});
 let before=store.version().await?;let mut expected=Sha256::new();let mut actual=Sha256::new();let mut count=0;
 while let Some(values)=receiver.recv().await{count+=compare_bars(store,values,manifest,&sha,&mut expected,&mut actual).await?;ensure!(serde_json::to_value(store.version().await?)?==serde_json::to_value(&before)?,"inactive facts changed during independent verification");}
 ensure!(worker.await??==count&&plan["rows"]==count.to_string(),"independently verified bar count differs");
 let summary=store.generation_summary().await?;ensure!(summary["complete"]==true&&summary["counts"]["bar_rows"]==count.to_string()&&summary["version"]==serde_json::to_value(&before)?,"native complete bar row count/version differs from source");
 let expected=hex::encode(expected.finalize());let actual=hex::encode(actual.finalize());ensure!(expected==actual,"bar semantic digest differs");
 Ok(json!({"state":"complete_independent_source_field_oracle","rows":count.to_string(),"expected_sha256":expected,"actual_sha256":actual,"source_sha256":sha,"version":before,"generation_summary":summary,"fields":"all canonical identity/OHLCV/null/revision/state/finality/source clocks/received sequence/source metadata + immutable original lineage","native_cursor_inferred":false,"future_live_read":false}))
}
fn expected_quote(row:&Value)->Result<Value>{
 let raw=text(&row["source_id"])?;let seq=&row["raw_payload"]["sequence"];
 Ok(json!({"instrument_symbol":row["instrument_symbol"],"realtime_source_id":canonical_source(&raw),"evidence_channel_id":raw,"event_id":if row["event_id"].is_null(){format!("legacy-pg:{}",text(&row["id"])?) }else{text(&row["event_id"])?},"price":decimal(&row["last"])?,"bid":decimal(&row["bid"])?,"ask":decimal(&row["ask"])?,"volume":decimal(&row["volume"])?,"observed_at_ns":ns(&row["observed_at"])?.to_string(),"received_at_ns":ns(&row["received_at"])?.to_string(),"source_sequence":if seq.is_null(){None}else{Some(text(seq)?.parse::<u64>()?.to_string())},"source_metadata":source_metadata(row),"statistics":{"open":row["open"],"high":row["high"],"low":row["low"],"change":row["change"],"change_percent":row["change_percent"]},"is_supplement":row["raw_payload"]["observation_kind"]=="supplement"}))
}
async fn compare_quotes(store:&Store,values:Vec<(u64,Value)>,sha:&str,expected_hash:&mut Sha256,actual_hash:&mut Sha256)->Result<u64>{
 let expected=values.iter().map(|(_,v)|expected_quote(v)).collect::<Result<Vec<_>>>()?;let count=expected.len();
 let keys=expected.iter().map(|v|->Result<_>{Ok(CanonicalQuoteKey{source_id:text(&v["realtime_source_id"])?,symbol:text(&v["instrument_symbol"])?,event_id:text(&v["event_id"])?})}).collect::<Result<_>>()?;
 let (_,actual)=store.lookup_quotes(keys).await?;ensure!(actual.len()==count,"quote lookup count differs");
 for (((offset,_input),expected),actual) in values.into_iter().zip(expected).zip(actual){let actual=actual.context("fixed source quote event missing from reopened native events")?;
  ensure!(actual.evidence["legacy_row_ref"]["sha256"]==sha&&actual.evidence["legacy_row_ref"]["row_offset"]==offset.to_string(),"quote original lineage differs at row {offset}");
  let actual=semantic_row(serde_json::to_value(actual)?);ensure!(actual==expected,"source-to-native quote field mismatch at row {offset}");expected_hash.update(serde_json::to_vec(&expected)?);expected_hash.update(b"\n");actual_hash.update(serde_json::to_vec(&actual)?);actual_hash.update(b"\n");
 }Ok(count as u64)
}
pub async fn verify_quotes(dir:&Path,manifest:&LegacyManifest,store:&Store)->Result<Value>{
 let table=manifest.tables.iter().find(|v|v.table=="quote_events").context("fixed quote archive absent")?;let file=dir.join(&table.file);let sha=table.sha256.clone();let check=file.clone();ensure!(tokio::task::spawn_blocking(move||file_hash(&check)).await??==sha,"quote source checksum differs");
 let (sender,mut receiver)=tokio::sync::mpsc::channel(2);let worker=tokio::task::spawn_blocking(move||->Result<u64>{let mut batch=vec![];let mut count=0;let mut bytes=0;for line in BufReader::new(File::open(file)?).lines(){let line=line?;if !batch.is_empty()&&(batch.len()==1000||bytes+line.len()>4*1024*1024){sender.blocking_send(std::mem::take(&mut batch)).context("quote verification receiver ended")?;bytes=0;}bytes+=line.len();batch.push((count,serde_json::from_str::<Value>(&line)?));count+=1;}if !batch.is_empty(){sender.blocking_send(batch).context("quote verification receiver ended")?;}Ok(count)});
 let before=store.version().await?;let mut expected=Sha256::new();let mut actual=Sha256::new();let mut count=0;
 while let Some(values)=receiver.recv().await{count+=compare_quotes(store,values,&sha,&mut expected,&mut actual).await?;ensure!(serde_json::to_value(store.version().await?)?==serde_json::to_value(&before)?,"quote generation changed during independent verification");}
 ensure!(worker.await??==count&&table.rows==count.to_string(),"independently verified quote count differs");
 let summary=store.generation_summary().await?;ensure!(summary["complete"]==true&&summary["counts"]["quote_events"]==count.to_string()&&summary["counts"]["quote_event_identities"]==count.to_string(),"native quote event or identity count differs from complete source");ensure!(summary["version"]==serde_json::to_value(&before)?,"quote generation changed before complete count");
 let expected=hex::encode(expected.finalize());let actual=hex::encode(actual.finalize());ensure!(expected==actual,"quote semantic digest differs");
 Ok(json!({"state":"complete_independent_source_event_oracle","rows":count.to_string(),"expected_sha256":expected,"actual_sha256":actual,"source_sha256":sha,"version":before,"generation_summary":summary,"fields":"event identity/source/channel/price/bid/ask/null volume/observed+received ns/complete u64 source sequence/statistics/supplement flag/source metadata + fixed original row reference","native_cursor_inferred":false,"latest_quote_used_as_event_oracle":false}))
}
