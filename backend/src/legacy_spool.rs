//! Regenerable decoded evidence: one global ordered decoder pass, then bounded
//! per-scope reconstruction. It is never an authority Store or original capture.
use anyhow::{Context,Result,ensure};
use redb::{Database,Durability,ReadableDatabase,ReadableTable,TableDefinition};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{path::{Path,PathBuf},sync::Arc,collections::BTreeMap};
use tracefang_core::{domain::{QuoteSnapshot,Candle},persistence_contract::CapturePosition};
use crate::{capture::{Capture,CapturedFrame,ProviderFrame},catalog::Catalog,providers::Decoder};
const FRAMES:TableDefinition<u64,&[u8]>=TableDefinition::new("decoded_frames_v1");
const TEMPLATES:TableDefinition<&str,&[u8]>=TableDefinition::new("decoded_row_templates_v1");
const META:TableDefinition<&str,&[u8]>=TableDefinition::new("decoded_spool_manifest_v1");
const SCHEMA:&str="retained-global-decoded-spool-v2";
const MAX_FILE:u64=4*1024*1024*1024;
const MIN_FREE:u64=4*1024*1024*1024;
const MAX_DECODED_FRAME:usize=256*1024*1024;
#[derive(Clone,Serialize,Deserialize)]pub struct Manifest {
 pub schema:String,pub source_manifest_sha256:String,pub build_sha256:String,
 pub first:CapturePosition,pub last:CapturePosition,
 #[serde(with="tracefang_core::persistence_contract::u64_string")]pub frames:u64,
 #[serde(with="tracefang_core::persistence_contract::u64_string")]pub quote_rows:u64,
 #[serde(with="tracefang_core::persistence_contract::u64_string")]pub bar_rows:u64,
 #[serde(with="tracefang_core::persistence_contract::u64_string")]pub unique_templates:u64,
 #[serde(with="tracefang_core::persistence_contract::u64_string")]pub template_bytes:u64,
 pub decode_rejections:BTreeMap<String,u64>,pub canonical_decoded_sha256:String,
 pub complete:bool,pub original_prefix_complete:bool,pub origin_coverage:Value,
}
#[derive(Clone,Serialize,Deserialize)]struct Reference {symbol:String,sha256:String}
#[derive(Clone,Serialize,Deserialize)]struct Frame {
 header:CapturedFrame,body_sha256:String,body_bytes:usize,
 quotes:Vec<Reference>,bars:Vec<Reference>,rejection:Option<String>,calendar_context:Option<Value>,
}
#[derive(Debug,Clone,Serialize,Deserialize)]enum Field {Received,Connection,SequenceString,SequenceNumber,CaptureEpoch,CaptureSequence,CaptureDigest,CapturePosition,Accepted}
#[derive(Debug,Clone,Serialize,Deserialize)]struct Slot {path:Vec<String>,field:Field}
#[derive(Clone,Serialize,Deserialize)]struct Template {value:Value,slots:Vec<Slot>}
fn digest(bytes:&[u8])->String{hex::encode(Sha256::digest(bytes))}
fn field(field:&Field,record:&CapturedFrame)->Value {let frame=&record.frame;match field{Field::Received=>json!(frame.received_at),Field::Connection=>json!(frame.connection_id),Field::SequenceString=>json!(frame.sequence.to_string()),Field::SequenceNumber=>json!(frame.sequence),Field::CaptureEpoch=>json!(record.position.epoch),Field::CaptureSequence=>json!(record.position.sequence.to_string()),Field::CaptureDigest=>json!(record.position.digest),Field::CapturePosition=>json!(record.position),Field::Accepted=>json!(record.legacy.is_none().then(||record.accepted_at_ns.to_string()))}}
fn at_mut<'a>(value:&'a mut Value,path:&[String])->Result<&'a mut Value>{let mut value=value;for part in path {value=value.get_mut(part).context("decoded template substitution path missing")?;}Ok(value)}
impl Template {
 fn new(mut value:Value,record:&CapturedFrame)->Result<Self>{
  let mut slots=vec![];
  let supplement=value["source"]["raw_payload"]["observation_kind"]=="supplement";
  let mut candidates=if supplement{vec![]}else{vec![(vec!["source".into(),"received_at".into()],Field::Received)]};
  for key in ["frame_connection_id","connection_id"] {candidates.push((vec!["source".into(),"raw_payload".into(),key.into()],Field::Connection));}
  for key in ["frame_sequence","sequence"] {for format in [Field::SequenceString,Field::SequenceNumber]{candidates.push((vec!["source".into(),"raw_payload".into(),key.into()],format));}}
  candidates.push((vec!["source".into(),"raw_payload".into(),"supplement_received_at".into()],Field::Received));
  if !supplement&&value["source"]["raw_payload"]["capture_epoch"]==record.position.epoch&&value["source"]["raw_payload"]["capture_sequence"]==record.position.sequence.to_string()&&value["source"]["raw_payload"]["capture_digest"]==record.position.digest{
   for(key,format)in [("capture_epoch",Field::CaptureEpoch),("capture_sequence",Field::CaptureSequence),("capture_digest",Field::CaptureDigest),("capture_accepted_at_ns",Field::Accepted),("accepted_at_ns",Field::Accepted)]{candidates.push((vec!["source".into(),"raw_payload".into(),key.into()],format));}
  }
  for(key,format)in [("supplement_capture_position",Field::CapturePosition),("supplement_capture_accepted_at_ns",Field::Accepted)]{candidates.push((vec!["source".into(),"raw_payload".into(),key.into()],format));}
  candidates.push((vec!["source".into(),"raw_payload".into(),"authoritative_input".into(),"capture_position".into()],Field::CapturePosition));
  for(path,format)in candidates {if let Ok(target)=at_mut(&mut value,&path){if *target==field(&format,record){*target=Value::Null;slots.push(Slot{path,field:format});}}}
  Ok(Self{value,slots})
 }
 fn restore(&self,record:&CapturedFrame)->Result<Value>{let mut value=self.value.clone();for slot in &self.slots{let target=at_mut(&mut value,&slot.path)?;ensure!(target.is_null(),"decoded template would overwrite actual source value");*target=field(&slot.field,record);}Ok(value)}
}
struct Prepared {frame:Frame,templates:Vec<(String,Vec<u8>)>,decoded:Vec<u8>}
impl Prepared {fn resident_bytes(&self)->usize{self.decoded.len()+self.templates.iter().map(|(_,v)|v.len()).sum::<usize>()+self.frame.quotes.len()*128+self.frame.bars.len()*128}}
fn prepare(mut header:CapturedFrame,decoded:Result<(Vec<QuoteSnapshot>,Vec<Candle>)>,calendar_context:Option<Value>)->Result<Prepared>{
 let body_sha256=digest(&header.frame.body);let body_bytes=header.frame.body.len();header.frame.body.clear();
 let (quotes,bars,rejection)=match decoded{Ok((quotes,bars))=>(quotes,bars,None),Err(error)=>(vec![],vec![],Some(error.to_string()))};
 let original=json!({"position":header.position,"quotes":quotes,"bars":bars,"rejection":rejection,"calendar_context":calendar_context});let decoded=serde_json::to_vec(&original)?;drop(original);
 ensure!(decoded.len()<=MAX_DECODED_FRAME,"decoded evidence frame exceeds 256MiB; input retained; no truncated spool");
 let mut templates=vec![];let mut refs=|values:Vec<Value>|->Result<Vec<Reference>>{values.into_iter().map(|value|{
  let symbol=value["instrument"]["symbol"].as_str().context("decoded row instrument missing")?.to_owned();let template=Template::new(value.clone(),&header)?;
  ensure!(template.restore(&header)?==value,"decoded template did not roundtrip complete source evidence");let bytes=serde_json::to_vec(&template)?;let sha256=digest(&bytes);templates.push((sha256.clone(),bytes));Ok(Reference{symbol,sha256})
 }).collect()};
 let quote_refs=refs(quotes.into_iter().map(serde_json::to_value).collect::<serde_json::Result<Vec<_>>>()?)?;let bar_refs=refs(bars.into_iter().map(serde_json::to_value).collect::<serde_json::Result<Vec<_>>>()?)?;
 Ok(Prepared{frame:Frame{header,body_sha256,body_bytes,quotes:quote_refs,bars:bar_refs,rejection,calendar_context},templates,decoded})
}
fn budget(path:&Path)->Result<()> {
 ensure!(crate::capture::available_bytes(path.parent().context("spool parent missing")?)?>MIN_FREE,"decoded spool stopped below 4GiB free; original source/capture untouched");
 if path.is_file(){ensure!(std::fs::metadata(path)?.len()<MAX_FILE,"decoded spool exceeds 4GiB cache bound; incomplete evidence preserved");}Ok(())
}
fn commit_entries(database:&Database,entries:Vec<Prepared>,manifest:&mut Manifest,hash:&mut Sha256,first_seq:u64,path:&Path)->Result<()> {
 budget(path)?;
 let mut txn=database.begin_write()?;txn.set_durability(Durability::Immediate)?;{
  let mut frames=txn.open_table(FRAMES)?;let mut templates=txn.open_table(TEMPLATES)?;
  for entry in entries {
   let sequence=entry.frame.header.position.sequence;ensure!(sequence==first_seq.checked_add(manifest.frames).context("spool sequence exhausted")?,"decoded spool reordered or skipped input frame");
   for(sha256,bytes)in entry.templates {if let Some(old)=templates.get(sha256.as_str())?{ensure!(old.value()==bytes,"decoded template SHA collision");}else{templates.insert(sha256.as_str(),bytes.as_slice())?;manifest.unique_templates+=1;manifest.template_bytes+=bytes.len() as u64;}}
   manifest.frames+=1;manifest.quote_rows+=entry.frame.quotes.len() as u64;manifest.bar_rows+=entry.frame.bars.len() as u64;if let Some(rejection)=&entry.frame.rejection{*manifest.decode_rejections.entry(rejection.clone()).or_default()+=1;}
   hash.update(&entry.decoded);hash.update(b"\n");let value=serde_json::to_vec(&entry.frame)?;frames.insert(sequence,value.as_slice())?;
  }
 }let value=serde_json::to_vec(&manifest)?;txn.open_table(META)?.insert("manifest",value.as_slice())?;txn.commit()?;budget(path)?;Ok(())
}
/// Only the caller's fresh private spool is written. Every original frame is
/// decoded in sequence before selecting scopes; dependencies remain ordered.
pub async fn build(capture:&Capture,path:&Path,through:u64,source_manifest_sha256:String,build_sha256:String)->Result<Manifest>{
 ensure!(!path.exists(),"decoded spool already exists; never overwrite retained proof");budget(path)?;
 let bounds=capture.bounds().await?;let first_seq=bounds["first_sequence"].as_str().context("empty raw capture")?.parse::<u64>()?;let epoch=bounds["epoch"].as_str().context("capture epoch missing")?.to_owned();
 ensure!(first_seq<=through,"spool target before retained prefix");let first=capture.get(first_seq).await?.position;let last=capture.get(through).await?.position;
 let path=path.to_path_buf();let database=Arc::new(Database::create(&path)?);#[cfg(unix)]{use std::os::unix::fs::PermissionsExt;std::fs::set_permissions(&path,std::fs::Permissions::from_mode(0o600))?;}
 let mut manifest=Manifest{schema:SCHEMA.into(),source_manifest_sha256,build_sha256,first,last,frames:0,quote_rows:0,bar_rows:0,unique_templates:0,template_bytes:0,decode_rejections:BTreeMap::new(),canonical_decoded_sha256:String::new(),complete:false,original_prefix_complete:bounds["origin_prefix_complete"].as_bool().unwrap_or(false),origin_coverage:bounds["origin_coverage"].clone()};
 let mut decoder=Decoder::new(Arc::new(Catalog::embedded()?));let mut next=first_seq;let mut hash=Sha256::new();
 loop{
  budget(&path)?;let rows=capture.scan(&epoch,next,through.checked_add(1),64,4*1024*1024).await?;ensure!(!rows.is_empty(),"raw gap while decoding complete prefix");let cursor=rows.last().unwrap().position.sequence;let db=database.clone();let work_path=path.clone();let output=tokio::task::spawn_blocking(move||->Result<_>{
   let mut entries=vec![];let mut bytes=0usize;
   // Flush each decoded group before constructing another group. A small raw
   // batch can expand into many annual frames; retaining all groups is unbounded.
   for record in rows{let decoded=decoder.decode_record(&record);let calendar_context=if decoded.is_ok(){decoder.calendar_projection_for(&record.position)?}else{None};let prepared=prepare(record,decoded,calendar_context)?;let size=prepared.resident_bytes();ensure!(size<=512*1024*1024,"decoded frame representations exceed 512MiB credit; original input retained");
    if !entries.is_empty()&&bytes+size>4*1024*1024{commit_entries(&db,std::mem::take(&mut entries),&mut manifest,&mut hash,first_seq,&work_path)?;bytes=0;}
    bytes+=size;entries.push(prepared);if bytes>=4*1024*1024{commit_entries(&db,std::mem::take(&mut entries),&mut manifest,&mut hash,first_seq,&work_path)?;bytes=0;}
   }
   if !entries.is_empty(){commit_entries(&db,entries,&mut manifest,&mut hash,first_seq,&work_path)?;}Ok((decoder,manifest,hash))
  }).await??;decoder=output.0;manifest=output.1;hash=output.2;
  if cursor==through{break}next=cursor.checked_add(1).context("spool cursor exhausted")?;
 }
 ensure!(manifest.frames==through.checked_sub(first_seq).and_then(|v|v.checked_add(1)).context("spool range count overflow")?,"spool frame count differs from fixed raw prefix");
 manifest.complete=true;manifest.canonical_decoded_sha256=hex::encode(hash.finalize());let mut txn=database.begin_write()?;txn.set_durability(Durability::Immediate)?;let value=serde_json::to_vec(&manifest)?;txn.open_table(META)?.insert("manifest",value.as_slice())?;txn.commit()?;drop(database);budget(&path)?;Ok(manifest)
}
pub struct Reader {database:redb::ReadOnlyDatabase,pub manifest:Manifest,pub path:PathBuf}
impl Reader {
 pub fn open(path:&Path,source_manifest_sha256:&str,build_sha256:&str,wanted:&CapturePosition)->Result<Self>{
  let database=redb::ReadOnlyDatabase::open(path)?;let manifest:Manifest=serde_json::from_slice(database.begin_read()?.open_table(META)?.get("manifest")?.context("spool manifest missing")?.value())?;
  ensure!(manifest.schema==SCHEMA&&manifest.complete&&manifest.source_manifest_sha256==source_manifest_sha256&&manifest.build_sha256==build_sha256&&manifest.last==*wanted,"spool is incomplete or belongs to another input/build/prefix");Ok(Self{database,manifest,path:path.into()})
 }
 /// Body is intentionally absent in this internal DTO. Its raw identity and
 /// body digest are retained in the spool; no caller may expose it as raw bytes.
 pub fn decoded(&self,sequence:u64,symbol:Option<&str>)->Result<(CapturedFrame,Result<(Vec<QuoteSnapshot>,Vec<Candle>)>,Option<Value>)>{
  let txn=self.database.begin_read()?;let frames=txn.open_table(FRAMES)?;let frame:Frame=serde_json::from_slice(frames.get(sequence)?.context("decoded input frame absent")?.value())?;ensure!(frame.header.position.sequence==sequence&&frame.header.position.epoch==self.manifest.first.epoch,"decoded spool frame identity mismatch");
  if let Some(error)=frame.rejection{return Ok((frame.header,Err(anyhow::anyhow!(error)),frame.calendar_context))}
  let templates=txn.open_table(TEMPLATES)?;let materialize=|reference:&Reference|->Result<Value>{let bytes=templates.get(reference.sha256.as_str())?.context("decoded row template missing")?;ensure!(digest(bytes.value())==reference.sha256,"decoded row template checksum changed");let template:Template=serde_json::from_slice(bytes.value())?;template.restore(&frame.header)};
  let quotes=frame.quotes.iter().map(|r|Ok(serde_json::from_value(materialize(r)?)?)).collect::<Result<Vec<QuoteSnapshot>>>()?;
  let bars=frame.bars.iter().filter(|r|symbol.is_none_or(|symbol|r.symbol==symbol)).map(|r|Ok(serde_json::from_value(materialize(r)?)?)).collect::<Result<Vec<Candle>>>()?;
  Ok((frame.header,Ok((quotes,bars)),frame.calendar_context))
 }
 pub fn audit_all(&self)->Result<Value>{
  let mut hash=Sha256::new();let(mut frames,mut quotes,mut bars,mut rejections)=(0u64,0u64,0u64,0u64);let mut next=self.manifest.first.sequence;
  loop{let(record,decoded,calendar_context)=self.decoded(next,None)?;let(q,b,rejection)=match decoded{Ok((q,b))=>(q,b,None),Err(error)=>(vec![],vec![],Some(error.to_string()))};frames+=1;quotes+=q.len() as u64;bars+=b.len() as u64;rejections+=u64::from(rejection.is_some());let value=json!({"position":record.position,"quotes":q,"bars":b,"rejection":rejection,"calendar_context":calendar_context});hash.update(serde_json::to_vec(&value)?);hash.update(b"\n");if next==self.manifest.last.sequence{break}next=next.checked_add(1).context("decoded audit cursor exhausted")?;}
  let sha256=hex::encode(hash.finalize());ensure!(sha256==self.manifest.canonical_decoded_sha256&&frames==self.manifest.frames&&quotes==self.manifest.quote_rows&&bars==self.manifest.bar_rows,"reopened decoded evidence differs from full original global pass");
  Ok(json!({"complete":true,"frames":frames.to_string(),"quote_rows":quotes.to_string(),"bar_rows":bars.to_string(),"rejections":rejections.to_string(),"expected_sha256":self.manifest.canonical_decoded_sha256,"actual_sha256":sha256,"scope":"all original decoded rows and rejection reasons; templates restore complete source clocks/metadata, no raw body replaced"}))
 }
}

#[cfg(test)]mod tests {
 use super::*;
 fn record(frame:ProviderFrame)->CapturedFrame{CapturedFrame{position:CapturePosition{epoch:"test-epoch".into(),sequence:frame.sequence,digest:"a".repeat(64)},accepted_at_ns:frame.received_at.timestamp_nanos_opt().unwrap(),logical_at_ns:frame.received_at.timestamp_nanos_opt().unwrap(),legacy:None,clock_policy_version:None,frame}}
 #[tokio::test]async fn reopened_all_rows_equal_global_decode_and_wrong_identity_or_corruption_rejects()->Result<()>{
  let dir=tempfile::tempdir()?;let capture=Capture::open(dir.path().join("raw.redb"),Default::default())?;
  let symbol="XAUUSD.GOODS";let mut body=10005_u16.to_le_bytes().to_vec();body.extend((symbol.len() as u16).to_le_bytes());body.extend(symbol.as_bytes());body.extend(1_800_000_000u32.to_le_bytes());body.extend(4_000_000_000i64.to_le_bytes());body.extend(3_999_000_000i64.to_le_bytes());
  let mut frame=ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"one".into(),sequence:u64::MAX,received_at:chrono::DateTime::from_timestamp(1_800_000_000,123456789).unwrap(),encoding:"wire".into(),body};capture.append(&frame).await?;frame.connection_id="two".into();frame.received_at+=chrono::Duration::nanoseconds(1);let tail=capture.append(&frame).await?.position;
  let path=dir.path().join("spool.redb");let manifest=build(&capture,&path,2,"fixed-input".into(),"fixed-build".into()).await?;ensure!(manifest.frames==2&&manifest.quote_rows==2&&manifest.unique_templates==1,"duplicate body source evidence was not correctly interned");
  let reader=Reader::open(&path,"fixed-input","fixed-build",&tail)?;ensure!(reader.audit_all()?["complete"]==true,"full decoded reopen differs");let(header,decoded,_)=reader.decoded(2,None)?;let(quote,_)=decoded?;ensure!(header.position==tail&&quote[0].source.received_at==frame.received_at&&quote[0].source.raw("sequence")==Some(&json!(u64::MAX.to_string())),"exact full u64/time/source identity changed");drop(reader);
  ensure!(Reader::open(&path,"other-input","fixed-build",&tail).is_err()&&Reader::open(&path,"fixed-input","other-build",&tail).is_err(),"foreign input/build reused cache");
  let db=Database::open(&path)?;let mut txn=db.begin_write()?;txn.set_durability(Durability::Immediate)?;{let mut table=txn.open_table(TEMPLATES)?;let key=table.iter()?.next().context("template absent")??.0.value().to_owned();table.insert(key.as_str(),b"corrupt template".as_slice())?;}txn.commit()?;drop(db);
  let reader=Reader::open(&path,"fixed-input","fixed-build",&tail)?;ensure!(reader.audit_all().is_err(),"changed decoded template passed audit");drop(reader);capture.close_and_drain().await?;Ok(())
 }
 #[test]fn complete_template_roundtrip_preserves_old_price_clock_and_source_evidence()->Result<()>{
  let frame=ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"same-body-next-delivery".into(),sequence:u64::MAX,received_at:chrono::DateTime::from_timestamp(1_700_000_000,123456789).unwrap(),encoding:"wire".into(),body:vec![]};
  let value=json!({"source":{"received_at":"2020-01-01T00:00:00Z","raw_payload":{"connection_id":frame.connection_id,"sequence":u64::MAX,"supplement_received_at":frame.received_at,"original_accepted_at_ns":null,"complete_exact_decimal":"0.0000000000000000000000000001"}}});
  let template=Template::new(value.clone(),&record(frame.clone()))?;ensure!(template.restore(&record(frame.clone()))?==value,"original price timestamp or wide evidence changed");ensure!(template.value["source"]["received_at"]=="2020-01-01T00:00:00Z","supplement rewrote original price clock");Ok(())
 }
 #[test]fn changing_only_envelope_identity_interns_one_complete_row_template()->Result<()>{
  let frame=ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"first".into(),sequence:1,received_at:chrono::DateTime::from_timestamp(1_700_000_000,123456789).unwrap(),encoding:"wire".into(),body:vec![]};
  let make=|frame:&ProviderFrame|json!({"source":{"received_at":frame.received_at,"raw_payload":{"connection_id":frame.connection_id,"sequence":frame.sequence.to_string()}},"instrument":{"symbol":"XAU/USD"},"volume":null,"close":"12345678901234567890123456789.0000000000000000000000000001"});
  let mut next=frame.clone();next.connection_id="second".into();next.sequence=2;next.received_at+=chrono::Duration::nanoseconds(1);
  let a=Template::new(make(&frame),&record(frame.clone()))?;let b=Template::new(make(&next),&record(next.clone()))?;ensure!(serde_json::to_vec(&a)?==serde_json::to_vec(&b)?&&a.restore(&record(next.clone()))?==make(&next),"decoded template merged actual source fields or missed envelope-only dedup");Ok(())
 }
}
