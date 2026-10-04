//! Read-only legacy evidence archive and exact adapters. Old facts never seed raw replay.
use anyhow::{Context,Result,ensure,bail};
use chrono::{DateTime,Utc};
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use sqlx::{Connection,Row,PgConnection};
use std::{path::Path,fs::{self,File,OpenOptions},io::{BufReader,BufWriter,Read,Write,BufRead}};
use crate::capture::{Capture,ProviderFrame,LegacyOrigin};
use tracefang_core::persistence_contract::{ImportBarRow,ImportQuoteRow,ImportMetadataRow,ImportBatch,ImportContext};

#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct LegacyCursor {pub stream:String,pub epoch:String,pub sequence:String}
#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct TableArchive {pub table:String,pub rows:String,pub file:String,pub sha256:String,pub primary_key:Vec<String>,pub schema:Value}
#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct LegacyManifest {
 pub schema:u32,pub id:String,pub state:String,pub postgres:Value,pub tables:Vec<TableArchive>,
 pub raw:Value,pub configs:Vec<Value>,pub phases:Vec<Value>,pub limits:Vec<String>,
}
impl LegacyManifest {
 pub fn new()->Self {Self{schema:1,id:uuid::Uuid::new_v4().to_string(),state:"planned".into(),postgres:Value::Null,tables:vec![],raw:Value::Null,configs:vec![],phases:vec![],limits:vec![
  "PostgreSQL rows are final-revision historical evidence; no trustworthy native raw commit cursor is inferred".into(),
  "Legacy latest quotes, materializations and consumer acknowledgments are archived evidence, never past replay seed".into(),
  "Already rounded NUMERIC(38,18), microsecond timestamps and evicted raw input cannot be recovered by migration".into(),
  "PG snapshot and NATS fixed retained range are separate boundaries; cutover and incremental closure must be verified later".into()]}}
 pub fn save(&self,dir:&Path)->Result<()> {atomic_json(&dir.join("manifest.json"),self)}
}
pub fn atomic_json(path:&Path,value:&impl Serialize)->Result<()> {
 let parent=path.parent().context("manifest parent missing")?;fs::create_dir_all(parent)?;
 let temp=parent.join(format!(".manifest-{}.tmp",uuid::Uuid::new_v4()));let mut file=private_file(&temp)?;
 file.write_all(&serde_json::to_vec_pretty(value)?)?;file.sync_all()?;fs::rename(temp,path)?;
 #[cfg(unix)] File::open(parent)?.sync_all()?;Ok(())
}
fn private_file(path:&Path)->Result<File> {let mut options=OpenOptions::new();options.write(true).create_new(true);
 #[cfg(unix)] {use std::os::unix::fs::OpenOptionsExt;options.mode(0o600);}Ok(options.open(path)?) }
fn ident(value:&str)->String {format!("\"{}\"",value.replace('"',"\"\""))}
pub fn file_hash(path:&Path)->Result<String>{let mut file=File::open(path)?;let mut hash=Sha256::new();let mut bytes=vec![0;1024*1024];loop{let n=file.read(&mut bytes)?;if n==0{break}hash.update(&bytes[..n]);}Ok(hex::encode(hash.finalize()))}
fn require_space(dir:&Path)->Result<()> {
 ensure!(crate::capture::available_bytes(dir)?>4*1024*1024*1024,"archive stopped below 4GiB available disk; preserve retained evidence and resumable manifest");Ok(())
}
/// One Repeatable Read READ ONLY snapshot. Bounded server cursor, original decimal lexemes.
pub async fn export_postgres(url:&str,dir:&Path,manifest:&mut LegacyManifest,inventory_only:bool)->Result<()> {
 fs::create_dir_all(dir)?;require_space(dir)?;let mut conn=PgConnection::connect(url).await.map_err(|_|anyhow::anyhow!("legacy PostgreSQL read connection unavailable"))?;
 sqlx::query("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY").execute(&mut conn).await?;
 sqlx::query("SET LOCAL statement_timeout='30s'").execute(&mut conn).await?;sqlx::query("SET LOCAL timezone='UTC'").execute(&mut conn).await?;
 let info:String=sqlx::query_scalar("SELECT json_build_object('version',version(),'database',current_database(),'read_only',current_setting('transaction_read_only'),'isolation',current_setting('transaction_isolation'),'snapshot',pg_current_snapshot()::text,'captured_at',now(),'precision','declared PostgreSQL type limits; no restoration of previous rounding')::text").fetch_one(&mut conn).await?;
 manifest.postgres=serde_json::from_str(&info)?;ensure!(manifest.postgres["read_only"]=="on","legacy connection is not read-only");
 let tables:Vec<String>=sqlx::query_scalar("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename").fetch_all(&mut conn).await?;
 for table in tables {
  let columns:Vec<String>=sqlx::query_scalar("SELECT json_build_object('name',a.attname,'type',format_type(a.atttypid,a.atttypmod),'not_null',a.attnotnull,'default',pg_get_expr(d.adbin,d.adrelid))::text FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum WHERE n.nspname='public' AND c.relname=$1 AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum").bind(&table).fetch_all(&mut conn).await?;
  let primary_key:Vec<String>=sqlx::query_scalar("SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid=i.indrelid JOIN pg_namespace n ON n.oid=c.relnamespace JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum,ord) ON true JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum=k.attnum WHERE n.nspname='public' AND c.relname=$1 AND i.indisprimary ORDER BY k.ord").bind(&table).fetch_all(&mut conn).await?;
  let constraints:Vec<String>=sqlx::query_scalar("SELECT pg_get_constraintdef(x.oid) FROM pg_constraint x JOIN pg_class c ON c.oid=x.conrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname=$1 ORDER BY x.conname").bind(&table).fetch_all(&mut conn).await?;
  let indexes:Vec<String>=sqlx::query_scalar("SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=$1 ORDER BY indexname").bind(&table).fetch_all(&mut conn).await?;
  let schema=json!({"columns":columns.iter().map(|v|serde_json::from_str::<Value>(v)).collect::<std::result::Result<Vec<_>,_>>()?,"constraints":constraints,"indexes":indexes});
  let file=format!("pg-{}.ndjson",hex::encode(Sha256::digest(table.as_bytes()))[..20].to_owned());
  let mut archive=TableArchive{table:table.clone(),rows:if inventory_only{"unmeasured"}else{"0"}.into(),file:file.clone(),sha256:String::new(),primary_key:primary_key.clone(),schema};
  if !inventory_only {
   let order=if primary_key.is_empty(){"to_jsonb(t)::text".to_owned()}else{primary_key.iter().map(|c|format!("t.{}",ident(c))).collect::<Vec<_>>().join(",")};
   sqlx::query(&format!("DECLARE legacy_export NO SCROLL CURSOR FOR SELECT to_jsonb(t)::text AS row FROM public.{} t ORDER BY {order}",ident(&table))).execute(&mut conn).await?;
   let mut output=BufWriter::new(private_file(&dir.join(&file))?);let mut hash=Sha256::new();let mut count=0u64;
   loop {
    require_space(dir)?;let rows=sqlx::query("FETCH FORWARD 1000 FROM legacy_export").fetch_all(&mut conn).await?;
    if rows.is_empty(){break}
    for row in rows {let text:String=row.try_get("row")?;output.write_all(text.as_bytes())?;output.write_all(b"\n")?;hash.update(text.as_bytes());hash.update(b"\n");count+=1;}
   }
   output.flush()?;output.get_ref().sync_all()?;archive.rows=count.to_string();archive.sha256=hex::encode(hash.finalize());
   sqlx::query("CLOSE legacy_export").execute(&mut conn).await?;
  }
  manifest.tables.push(archive);manifest.state=if inventory_only{"inventory"}else{"exporting_postgres"}.into();manifest.save(dir)?;
 }
 sqlx::query("COMMIT").execute(&mut conn).await?;
 let source=serde_json::to_vec(&manifest.postgres)?;manifest.postgres["fingerprint"]=json!(hex::encode(Sha256::digest(source)));
 manifest.phases.push(json!({"phase":if inventory_only{"postgres_inventory"}else{"postgres_export"},"state":"complete","snapshot":manifest.postgres["snapshot"],"all_tables":manifest.tables.len()}));manifest.save(dir)?;Ok(())
}
#[derive(Debug,Serialize,Deserialize)]
struct RawHeader {schema:u32,stream:String,epoch:String,sequence:String,subject:String,broker_stored_at_ns:String,headers:async_nats::HeaderMap,body_bytes:usize,body_sha256:String}
/// Direct GET only; no stream mutation or consumer/ack is created.
pub async fn export_nats(url:&str,name:&str,dir:&Path,manifest:&mut LegacyManifest,after:Option<&LegacyCursor>,inventory_only:bool)->Result<()> {
 ensure!(!name.is_empty()&&name.bytes().all(|v|v.is_ascii_alphanumeric()||v==b'_'||v==b'-'),"invalid legacy stream name");
 let client=async_nats::ConnectOptions::new().connection_timeout(std::time::Duration::from_secs(3)).connect(url).await.map_err(|_|anyhow::anyhow!("legacy NATS read connection unavailable"))?;
 let context=async_nats::jetstream::new(client.clone());let mut stream=context.get_stream(name).await?;let info=stream.info().await?;
 let epoch=format!("{}:{}",name,info.created.unix_timestamp_nanos());let initial_first=info.state.first_sequence;let last=info.state.last_sequence;
 let first=if let Some(cursor)=after {ensure!(cursor.stream==name&&cursor.epoch==epoch,"legacy stream epoch changed; incremental tail cannot attach");let next=cursor.sequence.parse::<u64>()?.checked_add(1).context("legacy cursor exhausted")?;
  ensure!(info.state.messages==0||next>=initial_first,"legacy retained range no longer covers incremental cursor");ensure!(cursor.sequence.parse::<u64>()?<=last,"legacy stream tail rolled back");next}else{initial_first};
 let raw_info=client.request(format!("$JS.API.STREAM.INFO.{name}"),"".into()).await?;let source_config:Value=serde_json::from_slice(&raw_info.payload)?;
 manifest.raw=json!({"stream":name,"epoch":epoch,"first_sequence":if info.state.messages>0{Some(first.to_string())}else{None},"last_sequence":if info.state.messages>0{Some(last.to_string())}else{None},"retained_first_at_start":initial_first.to_string(),"message_count_at_start":info.state.messages.to_string(),"bytes_at_start":info.state.bytes.to_string(),"configuration":source_config,"gaps":[],"original_prefix_complete":initial_first<=1,"state":"inventory","legacy_cursor_is_native_cursor":false});manifest.save(dir)?;
 if inventory_only {client.drain().await?;return Ok(())}
 let file=format!("raw-{}.frames",uuid::Uuid::new_v4());let mut writer=BufWriter::new(private_file(&dir.join(&file))?);let mut hash=Sha256::new();let mut count=0u64;let mut payloads=std::collections::BTreeMap::<String,Vec<usize>>::new();let mut unique_bodies=std::collections::BTreeMap::<String,std::collections::HashMap<String,usize>>::new();
 if info.state.messages>0&&first<=last {for sequence in first..=last {
  require_space(dir)?;
  let message=match stream.get_raw_message(sequence).await {Ok(v)=>v,Err(error)=>{
   manifest.raw["state"]=json!("incomplete_gap");manifest.raw["gaps"]=json!([{"sequence":sequence.to_string(),"detail":"fixed retained input vanished or message unavailable; never skipped"}]);manifest.save(dir)?;return Err(error.into())}};
  ensure!(message.sequence==sequence,"legacy raw GET returned another sequence");
  let channel=message.headers.get("Market-Frame-Channel").map(|v|v.to_string()).unwrap_or_else(||"missing_channel_header".into());payloads.entry(channel.clone()).or_default().push(message.payload.len());
  let body_sha256=hex::encode(Sha256::digest(&message.payload));unique_bodies.entry(channel).or_default().entry(body_sha256.clone()).or_insert(message.payload.len());
  let header=RawHeader{schema:1,stream:name.into(),epoch:epoch.clone(),sequence:sequence.to_string(),subject:message.subject.to_string(),broker_stored_at_ns:message.time.unix_timestamp_nanos().to_string(),headers:message.headers,body_bytes:message.payload.len(),body_sha256};
  let encoded=serde_json::to_vec(&header)?;let len=u32::try_from(encoded.len())?.to_be_bytes();writer.write_all(&len)?;writer.write_all(&encoded)?;writer.write_all(&message.payload)?;hash.update(len);hash.update(encoded);hash.update(&message.payload);count+=1;
 }}
 writer.flush()?;writer.get_ref().sync_all()?;let final_info=stream.info().await?;ensure!(format!("{}:{}",name,final_info.created.unix_timestamp_nanos())==epoch,"legacy stream replaced during export");
 manifest.raw["payload_statistics"]=json!(payloads.into_iter().map(|(channel,mut values)|{values.sort_unstable();let n=values.len();let total=values.iter().map(|v|*v as u64).sum::<u64>();let unique=&unique_bodies[&channel];let unique_bytes=unique.values().map(|v|*v as u64).sum::<u64>();
 (channel,json!({"n":n.to_string(),"unique_body_count":unique.len().to_string(),"duplicate_body_frames":(n-unique.len()).to_string(),"unique_body_bytes":unique_bytes.to_string(),"logical_body_bytes":total.to_string(),"p50_bytes":values[n/2].to_string(),"p95_bytes":values[((n*95).div_ceil(100)-1).min(n-1)].to_string(),"max_bytes":values[n-1].to_string(),"total_bytes":total.to_string()}))}).collect::<std::collections::BTreeMap<_,_>>());
 manifest.raw["state"]=json!("fixed_range_exported");manifest.raw["file"]=json!(file);manifest.raw["sha256"]=json!(hex::encode(hash.finalize()));manifest.raw["exported_frames"]=json!(count.to_string());
 manifest.raw["tail_observed_after_export"]=json!(final_info.state.last_sequence.to_string());manifest.raw["incremental_after"]=json!(LegacyCursor{stream:name.into(),epoch,sequence:last.to_string()});
 manifest.phases.push(json!({"phase":"raw_export","state":"complete_fixed_range","frames":count.to_string(),"live_capture_not_stopped":true}));manifest.save(dir)?;client.drain().await?;Ok(())
}
pub fn archive_config(path:&Path,dir:&Path,manifest:&mut LegacyManifest)->Result<()> {
 if !path.is_file(){return Ok(())}let bytes=fs::read(path)?;let name=format!("config-{}.json",uuid::Uuid::new_v4());let mut file=private_file(&dir.join(&name))?;file.write_all(&bytes)?;file.sync_all()?;
 manifest.configs.push(json!({"source_path":path.to_string_lossy(),"file":name,"bytes":bytes.len().to_string(),"sha256":hex::encode(Sha256::digest(bytes)),"private_mode":"0600; values are not printed"}));manifest.save(dir)
}
async fn publish_raw_batch(capture:&Capture,rows:Vec<(RawHeader,ProviderFrame)>,mapping:&mut BufWriter<File>,hash:&mut Sha256)->Result<Option<(tracefang_core::persistence_contract::CapturePosition,tracefang_core::persistence_contract::CapturePosition)>> {
 let mut labels=vec![];let mut deliveries=vec![];
 for (header,frame) in rows {let origin=LegacyOrigin{stream:header.stream.clone(),epoch:header.epoch.clone(),sequence:header.sequence.clone(),broker_stored_at_ns:header.broker_stored_at_ns.clone()};labels.push(header);deliveries.push((frame,origin));}
 let receipts=capture.append_legacy_batch(deliveries).await?;ensure!(receipts.len()==labels.len(),"raw import receipt count differs");let mut first=None;let mut last=None;
 for (header,receipt) in labels.into_iter().zip(receipts) {
  let line=serde_json::to_vec(&json!({"legacy_stream":header.stream,"legacy_epoch":header.epoch,"legacy_sequence":header.sequence,"broker_stored_at_ns":header.broker_stored_at_ns,
   "native_position":receipt.position,"frame_received_at_ns":receipt.received_at_ns.to_string(),"original_accepted_at_ns":null,"imported_at_ns":receipt.accepted_at_ns.to_string(),"import_confirmed_at_ns":receipt.confirmed_at_ns.to_string(),"duplicate":receipt.duplicate,"body_sha256":header.body_sha256}))?;
  mapping.write_all(&line)?;mapping.write_all(b"\n")?;hash.update(line);hash.update(b"\n");if first.is_none(){first=Some(receipt.position.clone())}last=Some(receipt.position);
 }Ok(first.zip(last))
}
pub async fn import_raw_archive(dir:&Path,manifest:&mut LegacyManifest,capture:&Capture)->Result<()> {
 ensure!(manifest.raw["state"]=="fixed_range_exported"||manifest.raw["state"]=="imported","raw fixed range export is not complete");
 let file=manifest.raw["file"].as_str().context("raw archive missing")?;let path=dir.join(file);let checked=path.clone();let actual=tokio::task::spawn_blocking(move||file_hash(&checked)).await??;
 ensure!(actual==manifest.raw["sha256"].as_str().context("raw archive digest missing")?,"raw archive digest differs");
 let mapping_name=format!("raw-map-{}.ndjson",uuid::Uuid::new_v4());let mut mapping=BufWriter::new(private_file(&dir.join(&mapping_name))?);let mut input=BufReader::new(File::open(path)?);let mut count=0u64;let mut hash=Sha256::new();let mut first=None;let mut last=None;let mut batch=vec![];let mut batch_bytes=0usize;let mut groups=0u64;
 loop {
  let mut length=[0u8;4];let n=input.read(&mut length[..1])?;if n==0{break}input.read_exact(&mut length[1..]).context("truncated raw archive header")?;let n=u32::from_be_bytes(length) as usize;ensure!(n<=64*1024,"oversized raw archive header");
  let mut bytes=vec![0;n];input.read_exact(&mut bytes)?;let header:RawHeader=serde_json::from_slice(&bytes)?;ensure!(header.schema==1&&header.body_bytes<=48*1024*1024,"unsupported raw archive record");
  let mut body=vec![0;header.body_bytes];input.read_exact(&mut body)?;ensure!(hex::encode(Sha256::digest(&body))==header.body_sha256,"legacy raw payload checksum differs");
  let frame=ProviderFrame::from_parts(&header.headers,&body).with_context(||format!("legacy raw envelope invalid at {}; retained archive preserved, native import stops",header.sequence))?;drop(body);
  if !batch.is_empty()&&(batch.len()==64||batch_bytes+frame.body.len().max(1)>4*1024*1024){if let Some((a,b))=publish_raw_batch(capture,std::mem::take(&mut batch),&mut mapping,&mut hash).await?{if first.is_none(){first=Some(a)}last=Some(b)}groups+=1;batch_bytes=0;}
  batch_bytes+=frame.body.len().max(1);batch.push((header,frame));count+=1;
 }
 if !batch.is_empty(){if let Some((a,b))=publish_raw_batch(capture,batch,&mut mapping,&mut hash).await?{if first.is_none(){first=Some(a)}last=Some(b)}groups+=1;}
 mapping.flush()?;mapping.get_ref().sync_all()?;ensure!(count.to_string()==manifest.raw["exported_frames"].as_str().unwrap_or(""),"raw imported count differs");
 if count==0&&!manifest.raw["incremental_base"].is_null(){
  let base=&manifest.raw["incremental_base"];let source=std::path::PathBuf::from(base["source_manifest_path"].as_str().context("empty delta base source missing")?);
  ensure!(file_hash(&source)?==base["source_manifest_sha256"].as_str().context("base manifest hash missing")?,"empty delta base manifest changed");
  let previous:LegacyManifest=serde_json::from_slice(&fs::read(&source)?)?;let mut anchor=previous.raw["native_mapping"].clone();let filename=anchor["file"].as_str().context("base mapping file missing")?;
  ensure!(Path::new(filename).file_name().is_some_and(|v|v==filename),"base mapping escapes source directory");let source_map=source.parent().context("base directory missing")?.join(filename);
  ensure!(file_hash(&source_map)?==anchor["sha256"].as_str().context("base mapping digest missing")?,"base mapping hash differs");
  let position=serde_json::from_value(anchor["last_position"].clone())?;capture.get_at(&position).await?;
  let target=dir.join(filename);if target.exists(){ensure!(file_hash(&target)?==anchor["sha256"].as_str().unwrap_or(""),"existing base mapping differs");}else if fs::hard_link(&source_map,&target).is_err(){let mut output=private_file(&target)?;std::io::copy(&mut File::open(&source_map)?,&mut output)?;output.sync_all()?;}
  anchor["mapping_scope"]=json!("verified base range; terminal raw increment empty");manifest.raw["native_mapping"]=anchor;manifest.raw["state"]=json!("imported");
  manifest.phases.push(json!({"phase":"raw_native_import","state":"complete_empty_increment_fixed_base_anchor","new_frames":"0","cutover_closed":false}));return manifest.save(dir)
 }

 manifest.raw["native_mapping"]=json!({"file":mapping_name,"sha256":hex::encode(hash.finalize()),"frames":count.to_string(),"ordered_admission_groups":groups.to_string(),"limits":{"frames":64,"bytes":4194304,"single_large_frame":"kept whole up to48MiB"},"first_position":first,"last_position":last,"replay_seed":"empty retained-prefix state; never PostgreSQL latest"});manifest.raw["state"]=json!("imported");
 manifest.phases.push(json!({"phase":"raw_native_import","state":"complete_fixed_range","cutover_closed":false}));manifest.save(dir)
}
/// Independently read every durable native envelope against the immutable source mapping.
pub async fn verify_raw_import(dir:&Path,manifest:&mut LegacyManifest,capture:&Capture)->Result<()> {
 ensure!(manifest.raw["state"]=="imported","raw source range was not completely imported");
 let map=&manifest.raw["native_mapping"];let path=dir.join(map["file"].as_str().context("native mapping file missing")?);
 let checked=path.clone();ensure!(tokio::task::spawn_blocking(move||file_hash(&checked)).await??==map["sha256"].as_str().unwrap_or(""),"native mapping checksum differs");
 let mut lines=BufReader::new(File::open(path)?).lines();let bounds=capture.bounds().await?;
 let epoch=bounds["epoch"].as_str().context("native epoch missing")?;let count=map["frames"].as_str().context("mapping row count missing")?.parse::<u64>()?;
 let last:tracefang_core::persistence_contract::CapturePosition=serde_json::from_value(map["last_position"].clone())?;
 let first=if map["first_position"].is_null(){tracefang_core::persistence_contract::CapturePosition{epoch:last.epoch.clone(),sequence:last.sequence.checked_add(1).and_then(|v|v.checked_sub(count)).context("invalid legacy mapping span")?,digest:String::new()}}else{serde_json::from_value(map["first_position"].clone())?};
 ensure!(first.epoch==epoch&&last.epoch==epoch&&first.sequence>0&&last.sequence.checked_sub(first.sequence).and_then(|v|v.checked_add(1))==Some(count),"native mapping range is inconsistent");
 capture.get_at(&last).await?;if !first.digest.is_empty(){capture.get_at(&first).await?;}
 let mut next=first.sequence;let mut verified=0u64;
 while verified<count {
  let records=capture.scan(epoch,next,Some(last.sequence.checked_add(1).context("native verification sequence exhausted")?),64,4*1024*1024).await?;
  ensure!(!records.is_empty(),"native raw proof contains a gap");
  for record in records {
   let mapping:Value=serde_json::from_str(&lines.next().context("native mapping ended early")??)?;
   let position:tracefang_core::persistence_contract::CapturePosition=serde_json::from_value(mapping["native_position"].clone())?;
   ensure!(position==record.position,"mapping and persisted prefix differ");let origin=record.legacy.as_ref().context("imported record has no legacy provenance")?;
   ensure!(mapping["legacy_stream"]==origin.stream&&mapping["legacy_epoch"]==origin.epoch&&mapping["legacy_sequence"]==origin.sequence&&mapping["broker_stored_at_ns"]==origin.broker_stored_at_ns,"original broker envelope differs");
   ensure!(mapping["frame_received_at_ns"].as_str()==Some(&record.frame.received_at.timestamp_nanos_opt().context("frame receive ns missing")?.to_string()),"original received time differs");
   ensure!(mapping["body_sha256"].as_str()==Some(&hex::encode(Sha256::digest(&record.frame.body))),"persisted body differs from original source hash");
   verified+=1;next=record.position.sequence.checked_add(1).context("native proof sequence exhausted")?;
  }
 }
 ensure!(lines.next().is_none(),"native mapping has unverified extra rows");
 manifest.phases.push(json!({"phase":"raw_native_verify","state":"complete_reopened_every_envelope","frames":verified.to_string(),"epoch":epoch,"verified_first_sequence":first.sequence.to_string(),"verified_last_position":last,"bounds":bounds,"mapping_sha256":map["sha256"],"original_prefix_complete":manifest.raw["original_prefix_complete"],"cutover_closed":false}));manifest.save(dir)
}

fn text(row:&Value,key:&str)->Result<String> {match &row[key]{Value::String(v)=>Ok(v.clone()),Value::Number(v)=>Ok(v.to_string()),_=>bail!("legacy {key} must be exact text or number")}}
fn optional_text(row:&Value,key:&str)->Result<Option<String>> {if row[key].is_null(){Ok(None)}else{Ok(Some(text(row,key)?))}}
fn time(row:&Value,key:&str)->Result<i64>{text(row,key)?.parse::<DateTime<Utc>>()?.timestamp_nanos_opt().context("legacy timestamp exceeds signed ns contract")}
fn optional_time(row:&Value,key:&str)->Result<Option<i64>> {if row[key].is_null(){Ok(None)}else{Ok(Some(time(row,key)?))}}
fn source(row:&Value)->Value {json!({"provider":row["realtime_source_id"].as_str().or_else(||row["source_id"].as_str()),"provider_symbol":row["provider_symbol"],"observed_at":row["observed_at"],"received_at":row["received_at"],"raw_payload":if row["source_raw_payload"].is_null(){&row["raw_payload"]}else{&row["source_raw_payload"]},"legacy_precision":"NUMERIC declared typmod; pg timestamp microseconds"})}
pub fn convert_bar(table:&str,row:&Value)->Result<ImportBarRow> {
 ensure!(matches!(table,"candles"|"realtime_bars"),"only canonical historical bar tables may enter final-revision facts");
 let raw_source=row["realtime_source_id"].as_str().or_else(||row["source_id"].as_str()).context("legacy bar source missing")?;
 let source_id=if raw_source.starts_with("jin10_"){"jin10_client"}else{raw_source};
 let open_time_ns=time(row,"open_time")?;let interval_seconds=text(row,"interval_seconds")?.parse::<u32>()?;
 let close_time_ns=optional_time(row,"close_time")?.unwrap_or(open_time_ns.checked_add(i64::from(interval_seconds).checked_mul(1_000_000_000).context("legacy interval ns overflow")?).context("legacy interval end overflow")?);
 let revision=optional_text(row,"revision")?.map(|v|v.parse()).transpose()?.unwrap_or(1);
 Ok(ImportBarRow{instrument_symbol:text(row,"instrument_symbol")?,realtime_source_id:source_id.into(),evidence_channel_id:row["evidence_channel_id"].as_str().or_else(||row["upstream_channel_id"].as_str()).unwrap_or(raw_source).into(),
  interval_seconds,open_time_ns,close_time_ns,open:text(row,"open")?,high:text(row,"high")?,low:text(row,"low")?,close:text(row,"close")?,volume:optional_text(row,"volume")?,revision,
  received_sequence:optional_text(row,"received_sequence")?.map(|v|v.parse()).transpose()?,state:row["state"].as_str().unwrap_or("final").into(),finalized_at_ns:optional_time(row,"finalized_at")?,
  source_observed_at_ns:time(row,"observed_at")?,received_at_ns:time(row,"received_at")?,source_metadata:source(row),evidence:json!({"table":table,"computed_interval_end":row["close_time"].is_null(),"semantics":"final_revision_history","finalization_time_unknown":row["finalized_at"].is_null(),"raw_capture_cursor_unproved":true,"legacy_row":row})})
}
pub fn convert_quote(row:&Value)->Result<ImportQuoteRow> {
 let channel=text(row,"source_id")?;let source_id=if channel.starts_with("jin10_"){"jin10_client"}else{&channel};
 let seq=row["raw_payload"]["sequence"].as_str().map(str::parse).transpose()?.or_else(||row["raw_payload"]["sequence"].as_u64());
 Ok(ImportQuoteRow{instrument_symbol:text(row,"instrument_symbol")?,realtime_source_id:source_id.into(),evidence_channel_id:channel.clone(),event_id:row["event_id"].as_str().map(str::to_owned).unwrap_or(format!("legacy-pg:{}",text(row,"id")?)),
  price:text(row,"last")?,bid:optional_text(row,"bid")?,ask:optional_text(row,"ask")?,volume:optional_text(row,"volume")?,observed_at_ns:time(row,"observed_at")?,received_at_ns:time(row,"received_at")?,source_sequence:seq,
  source_metadata:source(row),statistics:json!({"open":row["open"],"high":row["high"],"low":row["low"],"change":row["change"],"change_percent":row["change_percent"]}),is_supplement:row["raw_payload"]["observation_kind"]=="supplement",evidence:json!({"semantics":"legacy_canonical_event","raw_capture_cursor_unproved":true,"legacy_row":row})})
}
pub fn metadata(table:&str,row:Value,index:u64)->ImportMetadataRow {ImportMetadataRow{namespace:format!("legacy_pg:{table}"),key:index.to_string(),value:row,evidence:json!({"table":table,"role":"archived_metadata_or_materialization_not_replay_seed"})}}
/// Reads and verifies an entire staged table before callbacks publish it.
pub fn table_rows(dir:&Path,table:&TableArchive,mut consume:impl FnMut(u64,Value)->Result<()>)->Result<u64> {
 let path=dir.join(&table.file);ensure!(file_hash(&path)?==table.sha256,"staged table checksum differs");let mut count=0u64;
 for line in BufReader::new(File::open(path)?).lines(){let value:Value=serde_json::from_str(&line?)?;consume(count,value)?;count+=1;}
 ensure!(count.to_string()==table.rows,"staged table row count differs");Ok(count)
}
/// Allows offline adapters to prove staged imports before wiring the production Store.
pub trait LegacySink:Send+Sync {
 fn bars(&self,batch:ImportBatch<ImportBarRow>)->std::pin::Pin<Box<dyn std::future::Future<Output=Result<Value>>+Send+'_>>;
 fn quotes(&self,batch:ImportBatch<ImportQuoteRow>)->std::pin::Pin<Box<dyn std::future::Future<Output=Result<Value>>+Send+'_>>;
 fn metadata(&self,batch:ImportBatch<ImportMetadataRow>)->std::pin::Pin<Box<dyn std::future::Future<Output=Result<Value>>+Send+'_>>;
}
pub fn runtime_metadata_rows(dir:&Path,manifest:&LegacyManifest)->Result<Vec<ImportMetadataRow>> {
 let mut output=vec![];let mut rows=|name:&str|->Result<Vec<Value>>{let mut result=vec![];if let Some(table)=manifest.tables.iter().find(|v|v.table==name){table_rows(dir,table,|_,row|{result.push(row);Ok(())})?;}Ok(result)};
 let evidence=json!({"origin_id":manifest.id,"semantics":"final_revision_history","native_raw_cursor":null,"original_replay_seed":false,"metadata_adapter":"legacy-runtime-v1"});
 let mut add=|namespace:&str,key:String,value:Value|{output.push(ImportMetadataRow{namespace:namespace.into(),key,value,evidence:evidence.clone()})};
 let mut watch=rows("watchlist_items")?;watch.retain(|v|v["profile_id"]=="default");watch.sort_by_key(|v|v["position"].as_i64().unwrap_or(i64::MAX));
 if !watch.is_empty(){add("watchlist","default".into(),json!(watch.iter().map(|v|text(v,"instrument_symbol")).collect::<Result<Vec<_>>>()?));}
 let routes=rows("instrument_source_routes")?.into_iter().filter(|v|v["capability"]=="realtime").map(|v|Ok(json!({"instrument_symbol":text(&v,"instrument_symbol")?,"source_id":text(&v,"source_id")?,"capability":"realtime"}))).collect::<Result<Vec<_>>>()?;
 if !routes.is_empty(){add("routes","realtime".into(),json!(routes));}
 for config in &manifest.configs {if config["source_path"].as_str().is_some_and(|v|v.ends_with("sources.json")) {
  let path=dir.join(config["file"].as_str().context("config archive file missing")?);ensure!(file_hash(&path)?==config["sha256"].as_str().unwrap_or(""),"config checksum differs");let value:Value=serde_json::from_slice(&fs::read(path)?)?;
  ensure!(value["sources"].is_object(),"source config has no explicit sources object; do not infer enabled rules from PG catalog");add("sources","config".into(),value);
 }}
 let mut coverage=std::collections::BTreeMap::<String,Vec<Value>>::new();
 for row in rows("realtime_candle_cache_ranges")? {let source=text(&row,"realtime_source_id")?;let symbol=text(&row,"instrument_symbol")?;coverage.entry(format!("{source}:{symbol}")).or_default().push(row);}
 let original_coverage=coverage.clone();
 for (key,ranges) in coverage {
  let normalized=ranges.iter().map(|row|->Result<Value>{let start=text(row,"range_start")?.parse::<DateTime<Utc>>()?;let end=text(row,"range_end")?.parse::<DateTime<Utc>>()?;ensure!(start<end,"legacy coverage is empty/inverted");Ok(json!([start,end]))}).collect::<Result<Vec<_>>>()?;
  add("capabilities",key.clone(),json!(["candles"]));add("coverage",key,json!({"ranges":normalized,"semantics":"final_revision_history","raw_prefix_complete":false,"native_raw_cursor":null,"precision":"legacy PG declared numeric and microsecond timestamp","cutover_closed":false}));
 }
 for row in rows("realtime_bar_series_state")? {let state:tracefang_core::reducer::SeriesState=serde_json::from_value(row)?;
  add("series_state",format!("{}:{}",state.realtime_source_id,state.instrument_symbol),serde_json::to_value(state)?);
 }
 drop(add);for row in &mut output{if row.namespace=="coverage"{row.evidence["original_range_rows"]=json!(original_coverage.get(&row.key));}}
 Ok(output)
}
pub fn checked_clock_projection(source:&Path,clock_dir:&Path,audit_path:&Path,manifest:&LegacyManifest)->Result<Value> {
 let path=clock_dir.join("canonical-bars-clock-v2.manifest.json");let mut plan:Value=serde_json::from_slice(&fs::read(&path)?)?;
 let descriptor_sha=file_hash(&path)?;let audit:Value=serde_json::from_slice(&fs::read(audit_path)?)?;
 ensure!(plan["schema"]=="legacy-source-clock-projection-v2"&&plan["policy_version"]=="legacy-bars-fixed-authority+clock-projection-v2"&&plan["complete"]==true&&plan["source_manifest_id"]==manifest.id&&plan["snapshot"]==manifest.postgres["snapshot"]&&plan["source_fingerprint"]==manifest.postgres["fingerprint"],"clock projection is incomplete or belongs to another source snapshot");
 ensure!(plan["policy"]["policy"]["policy_id"]==tracefang_core::source_clock::THS_V6_SHFE_END_V2,"clock policy differs");
 ensure!(audit["schema"]=="independent-clock-projection-audit-v1"&&audit["complete"]==true&&audit["projection_manifest_sha256"]==descriptor_sha&&audit["projected_file_sha256"]==plan["sha256"]&&audit["original_file_sha256"]==plan["original_canonical_file_sha256"]&&audit["policy"]==tracefang_core::source_clock::THS_V6_SHFE_END_V2,"independent clock audit does not bind exact complete projected input");
 let original:Value=serde_json::from_slice(&fs::read(source.join("canonical-bars-v1.manifest.json"))?)?;ensure!(original["sha256"]==plan["original_canonical_file_sha256"]&&original["source_manifest_id"]==manifest.id&&file_hash(&source.join("canonical-bars-v1.manifest.json"))?==text(&plan,"original_descriptor_sha256")?,"clock projection original descriptor differs");
 let scopes=plan["policy"]["policy"]["scopes"].as_array().context("clock source scopes missing")?;ensure!(scopes.len()==4,"clock source scopes differ");
 for(provider,symbol)in tracefang_core::source_clock::VERIFIED_V6_SCOPES{ensure!(scopes.iter().filter(|v|v["provider_code"]==provider&&v["instrument_symbol"]==symbol&&v["venue"]=="SHFE"&&v["period"]=="61").count()==1,"clock scope lacks exact reviewed identity");}
 for(name,descriptor)in plan["artifacts"].as_object().context("clock audit artifacts missing")?{let file=text(descriptor,"file")?;ensure!(Path::new(&file).file_name().is_some_and(|v|v==std::ffi::OsStr::new(&file))&&file==*name,"clock artifact escapes private directory");ensure!(file_hash(&clock_dir.join(file))?==text(descriptor,"sha256")?,"clock audit artifact hash changed");}
 for proof in plan["policy_evidence"].as_array().context("source clock evidence missing")?{ensure!(file_hash(Path::new(&text(proof,"file")?))?==text(proof,"sha256")?,"source clock witness changed");}
 ensure!(file_hash(Path::new(&text(&plan,"policy_source_file")?))?==text(&plan,"policy_source_sha256")?,"source clock implementation changed");
 let counts=&plan["counts"];let count=|key:&str|->Result<u64>{Ok(counts[key].as_str().with_context(||format!("clock count missing: {key}"))?.parse()?)};
 ensure!(count("input_rows")?==count("output_rows")?.checked_add(count("point_rows")?).and_then(|v|v.checked_add(counts["collided_input_rows"].as_str().unwrap_or("0").parse::<u64>().ok()?)).context("clock conservation overflow")?&&text(&plan,"rows")?.parse::<u64>()?==count("output_rows")?&&fs::metadata(clock_dir.join("clock-unresolved.ndjson"))?.len()==0,"clock row conservation or unresolved differences invalid");
 plan["_independent_audit"]=json!({"file":audit_path,"sha256":file_hash(audit_path)?,"audit":audit});plan["_descriptor_sha256"]=json!(descriptor_sha);Ok(plan)
}
fn clock_metadata_rows(source:&Path,clock_dir:&Path,plan:&Value,manifest:&LegacyManifest)->Result<Vec<ImportMetadataRow>> {
 let witness=plan["policy_evidence"].as_array().context("clock witness missing")?.iter().find(|v|v["file"].as_str().is_some_and(|s|s.ends_with("v6-four-shfe-clock-witness.json"))).context("four exact-scope witness missing")?;
 let proof=json!({"policy":tracefang_core::source_clock::THS_V6_SHFE_END_V2,"scope":plan["policy"]["policy"]["scopes"],"verified":true,"mapping_manifest_sha256":plan["_descriptor_sha256"],"mapping_manifest_path":clock_dir.join("canonical-bars-clock-v2.manifest.json"),"policy_source_sha256":plan["policy_source_sha256"],"witness_sha256":witness["sha256"],"original_archive_sha256":plan["original_canonical_file_sha256"],"original_manifest_sha256":file_hash(&source.join("manifest.json"))?,"source_manifest_id":manifest.id,"snapshot":manifest.postgres["snapshot"],"independent_audit":plan["_independent_audit"],"source_event_coverage_complete":false,"quote_source_clock_changed":false});
 let mut rows=vec![ImportMetadataRow{namespace:"migration".into(),key:"source_clock_policy".into(),value:proof.clone(),evidence:json!({"role":"verified fixed-snapshot clock projection; no raw cursor inferred"})}];
 for(_,symbol)in tracefang_core::source_clock::VERIFIED_V6_SCOPES{rows.push(ImportMetadataRow{namespace:"source_clock".into(),key:format!("tonghuashun_futures:{symbol}"),value:proof.clone(),evidence:json!({"scope":symbol,"role":"authoritative history clock only; quote-derived clocks remain independent"})});}
 for symbol in ["IXIC","BRN0Y","USDIND","000001.SH"]{rows.push(ImportMetadataRow{namespace:"source_clock".into(),key:format!("tonghuashun_futures:{symbol}"),value:json!({"policy":tracefang_core::source_clock::LEGACY_V6_OPEN_V1,"verified":false,"reason":"source_interval_label_semantics_unverified","applies_to":"authoritative v6 period61 history only; independent quote projection is not shifted"}),evidence:json!({"role":"explicit unresolved source label policy; no guessed shift"})});}
 let ledger=clock_dir.join("source-points.ndjson");let mut groups=std::collections::BTreeMap::<String,(u64,i64,i64)>::new();
 for line in BufReader::new(File::open(&ledger)?).lines(){let point:Value=serde_json::from_str(&line?)?;ensure!(point["unresolved"]==false,"unresolved point cannot enter verified metadata");let evidence=&point["evidence"];let symbol=text(evidence,"instrument_symbol")?;let at=text(evidence,"source_label_ns")?.parse::<i64>()?;let value=groups.entry(symbol).or_insert((0,at,at));value.0+=1;value.1=value.1.min(at);value.2=value.2.max(at);}
 for(symbol,(count,first,last))in groups{rows.push(ImportMetadataRow{namespace:"source_points".into(),key:format!("tonghuashun_futures:{symbol}"),value:json!({"known_unclassified_point_count_lower_bound":count.to_string(),"exact_unclassified_point_count":count.to_string(),"count_semantics":"unique selected authoritative point rows in this fixed PostgreSQL snapshot, not all upstream source events","first_source_label_ns":first.to_string(),"last_source_label_ns":last.to_string(),"ledger_file":ledger,"ledger_sha256":plan["artifacts"]["source-points.ndjson"]["sha256"],"source_event_coverage_complete":false,"canonical_minute_values_exact":true,"reason":"evidenced session-start source points retained independently; no invented minute or volume aggregation"}),evidence:proof.clone()});}
 Ok(rows)
}
/// The source file is fully checksummed before publication. Facts and raw replay stay separate.
pub async fn import_tables(dir:&Path,manifest:&mut LegacyManifest,sink:&impl LegacySink)->Result<()> {import_tables_at(dir,dir,None,manifest,sink).await}
/// A fresh progress directory keeps the original source manifest immutable.
pub async fn import_clock_tables(dir:&Path,clock_dir:&Path,progress_dir:&Path,audit_path:&Path,manifest:&mut LegacyManifest,sink:&impl LegacySink)->Result<()> {
 let plan=checked_clock_projection(dir,clock_dir,audit_path,manifest)?;
 import_tables_at(dir,progress_dir,Some((clock_dir,plan)),manifest,sink).await
}
async fn import_tables_at(dir:&Path,progress_dir:&Path,clock:Option<(&Path,Value)>,manifest:&mut LegacyManifest,sink:&impl LegacySink)->Result<()> {
 ensure!(manifest.phases.iter().any(|p|p["phase"]=="postgres_export"&&p["state"]=="complete"),"PostgreSQL export is not complete");
 let selection_path=clock.as_ref().map(|(dir,_)|dir.join("canonical-bars-clock-v2.manifest.json")).unwrap_or_else(||dir.join("canonical-bars-v1.manifest.json"));let selection=if let Some((_,plan))=&clock{Some(plan.clone())}else if selection_path.is_file(){Some(serde_json::from_slice::<Value>(&fs::read(&selection_path)?)?)}else{None};
 let mut tables=manifest.tables.clone();
 if let Some(plan)=&selection {
  ensure!((clock.is_some()||plan["schema"]==1&&plan["policy_version"]=="legacy-bars-fixed-authority-v1")&&plan["source_manifest_id"]==manifest.id&&plan["source_fingerprint"]==manifest.postgres["fingerprint"]&&plan["snapshot"]==manifest.postgres["snapshot"],"canonical selection belongs to another fixed snapshot");
  for name in ["candles","realtime_bars"] {let source=manifest.tables.iter().find(|v|v.table==name).context("canonical source table missing")?;ensure!(plan["source_tables"][name]["sha256"]==source.sha256&&plan["source_tables"][name]["rows"]==source.rows,"canonical source descriptor differs");}
  let file=plan["file"].as_str().context("canonical file missing")?;ensure!(Path::new(file).file_name().is_some_and(|v|v==std::ffi::OsStr::new(&file)),"canonical file must be inside fixed archive");
  tables.insert(0,TableArchive{table:"legacy_canonical_bars".into(),rows:text(plan,"rows")?,file:file.into(),sha256:text(plan,"sha256")?,primary_key:vec![],schema:plan.clone()});
  manifest.phases.push(json!({"phase":"canonical_bar_selection","state":"reviewed_fixed_snapshot","descriptor_sha256":file_hash(&selection_path)?,"descriptor":plan,"source_rows_retained":true,"activation":false}));manifest.save(progress_dir)?;
 }
 for table in tables {
  if (selection.is_some()&&matches!(table.table.as_str(),"candles"|"realtime_bars"))||!matches!(table.table.as_str(),"legacy_canonical_bars"|"candles"|"realtime_bars"|"quote_events"|"instruments"|"market_sources"|"instrument_source_routes"|"watchlists"|"watchlist_items") {
   manifest.phases.push(json!({"phase":"postgres_native_import","table":table.table,"state":"archived_evidence_only","reason":"derived/cache/latest/quarantine/reference rows are retained as archive, never replay seed"}));manifest.save(progress_dir)?;continue
  }
  let archive_dir=if table.table=="legacy_canonical_bars"{clock.as_ref().map(|(dir,_)|*dir).unwrap_or(dir)}else{dir}.to_path_buf();let staged=table.clone();let (send,mut receive)=tokio::sync::mpsc::channel::<(u64,Vec<Value>)>(2);
  let worker=tokio::task::spawn_blocking(move||->Result<u64> {
   let mut batch=vec![];let mut start=0u64;let mut bytes=0usize;
   let count=table_rows(&archive_dir,&staged,|offset,row|{
    let len=serde_json::to_vec(&row)?.len();
    if !batch.is_empty()&&(batch.len()>=500||bytes+len>4*1024*1024){send.blocking_send((start,std::mem::take(&mut batch))).context("legacy import receiver ended")?;start=offset;bytes=0;}
    batch.push(row);bytes+=len;Ok(())
   })?;
   if !batch.is_empty(){send.blocking_send((start,batch)).context("legacy import receiver ended")?;}Ok(count)
  });
  let context=ImportContext{origin_id:manifest.id.clone(),source_fingerprint:manifest.postgres["fingerprint"].as_str().context("PG source fingerprint missing")?.into(),schema_version:tracefang_core::persistence_contract::SCHEMA_VERSION.into(),range_label:table.table.clone(),legacy_cursor:None,expected_sha256:Some(table.sha256.clone())};
  let mut batches=0u64;let mut rows=0u64;let mut last_receipt=Value::Null;let mut accepted=0u64;let mut unchanged=0u64;let mut rejected=0u64;
  while let Some((offset,values))=receive.recv().await {
   let count=values.len();
   last_receipt=match table.table.as_str(){
    "legacy_canonical_bars"|"candles"|"realtime_bars"=>sink.bars(ImportBatch{context:context.clone(),row_offset:offset,rows:values.iter().enumerate().map(|(index,v)|{
     let chosen=if table.table=="legacy_canonical_bars"{v["_legacy_selection"]["chosen_table"].as_str().context("canonical row has no original selected table")?}else{&table.table};
     let mut row=convert_bar(chosen,v)?;let source_hash=if table.table=="legacy_canonical_bars"{manifest.tables.iter().find(|v|v.table==chosen).context("chosen source table absent")?.sha256.clone()}else{table.sha256.clone()};
     row.evidence["fixed_snapshot"]=json!({"source_fingerprint":context.source_fingerprint,"snapshot":manifest.postgres["snapshot"],"table_sha256":source_hash,"canonical_file_sha256":if table.table=="legacy_canonical_bars"{Some(&table.sha256)}else{None}});
     row.evidence.as_object_mut().unwrap().remove("legacy_row");row.evidence["legacy_row_ref"]=if table.table=="legacy_canonical_bars"{json!({"source_records":v["_legacy_selection"]["source_records"],"selected_table":chosen,"canonical_row_offset":(offset+index as u64).to_string()})}else{json!({"table":table.table,"file":table.file,"sha256":table.sha256,"row_offset":(offset+index as u64).to_string()})};
     if table.table=="legacy_canonical_bars"{row.evidence["canonical_selection"]=v["_legacy_selection"].clone();row.evidence["invalid_legacy_finality"]=v["_invalid_legacy_finality"].clone();if clock.is_some(){row.evidence["source_clock_projection"]=v["_legacy_clock_projection"].clone();}}Ok(row)
    }).collect::<Result<_>>()?}).await?,
    "quote_events"=>sink.quotes(ImportBatch{context:context.clone(),row_offset:offset,rows:values.iter().enumerate().map(|(index,v)|{let mut row=convert_quote(v)?;row.evidence.as_object_mut().unwrap().remove("legacy_row");row.evidence["legacy_row_ref"]=json!({"table":table.table,"file":table.file,"sha256":table.sha256,"row_offset":(offset+index as u64).to_string()});Ok(row)}).collect::<Result<_>>()?}).await?,
    _=>sink.metadata(ImportBatch{context:context.clone(),row_offset:offset,rows:values.into_iter().enumerate().map(|(i,v)|metadata(&table.table,v,offset+i as u64)).collect()}).await?,
   };
   accepted=accepted.checked_add(text(&last_receipt,"accepted")?.parse()?).context("accepted count overflow")?;unchanged=unchanged.checked_add(text(&last_receipt,"unchanged")?.parse()?).context("unchanged count overflow")?;rejected=rejected.checked_add(text(&last_receipt,"rejected")?.parse()?).context("rejected count overflow")?;
   batches+=1;rows+=count as u64;
   manifest.state="importing_final_revision_history".into();manifest.save(progress_dir)?;
  }
  let expected=worker.await??;ensure!(rows==expected,"native imported range differs from archived rows");
  ensure!(accepted+unchanged+rejected==rows,"import receipt counts do not close source rows");
  manifest.phases.push(json!({"phase":"postgres_native_import","table":table.table,"state":"complete_fixed_snapshot","rows":rows.to_string(),"accepted":accepted.to_string(),"unchanged":unchanged.to_string(),"rejected":rejected.to_string(),"bounded_batches":batches.to_string(),"last_receipt":last_receipt,"raw_projection_cursor_inferred":false,"activation":"caller must explicitly verify cutover; no service stopped"}));manifest.save(progress_dir)?;
 }
 let mut rows=runtime_metadata_rows(dir,manifest)?;if let Some((clock_dir,plan))=&clock{rows.extend(clock_metadata_rows(dir,clock_dir,plan,manifest)?);}let keys=rows.iter().map(|v|format!("{}/{}",v.namespace,v.key)).collect::<Vec<_>>();
 if !rows.is_empty(){let context=ImportContext{origin_id:manifest.id.clone(),source_fingerprint:manifest.postgres["fingerprint"].as_str().context("PG source fingerprint missing")?.into(),schema_version:tracefang_core::persistence_contract::SCHEMA_VERSION.into(),range_label:"runtime_metadata_v1".into(),legacy_cursor:None,expected_sha256:None};
  let receipt=sink.metadata(ImportBatch{context,row_offset:0,rows}).await?;manifest.phases.push(json!({"phase":"runtime_metadata_import","state":"complete_inactive","keys":keys,"receipt":receipt,"old_latest_is_seed":false,"enabled_rules":"only copied exact archived sources config; never inferred from market_sources"}));}
 manifest.state="fixed_inputs_imported_pending_incremental_closure".into();manifest.save(progress_dir)
}
#[cfg(test)]mod tests {
 use super::*;
 #[tokio::test] async fn raw_archive_preserves_each_legacy_envelope_and_missing_prefix()->Result<()> {
  let dir=tempfile::tempdir()?;let frame=ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"import-test".into(),sequence:u64::MAX,
      received_at:DateTime::from_timestamp(1_800_000_000,123456789).unwrap(),encoding:"wire".into(),body:vec![1,0,255,7]};
  let name="original.frames";let mut file=private_file(&dir.path().join(name))?;
  for sequence in [42u64,43] {
   let header=RawHeader{schema:1,stream:"OLD".into(),epoch:"OLD:epoch".into(),sequence:sequence.to_string(),subject:"market.raw.jin10_web".into(),broker_stored_at_ns:(1_800_000_001_000_000_000i64+sequence as i64).to_string(),headers:frame.headers()?,body_bytes:frame.body.len(),body_sha256:hex::encode(Sha256::digest(&frame.body))};
   let bytes=serde_json::to_vec(&header)?;file.write_all(&(bytes.len() as u32).to_be_bytes())?;file.write_all(&bytes)?;file.write_all(&frame.body)?;
  }file.sync_all()?;
  let mut manifest=LegacyManifest::new();manifest.raw=json!({"state":"fixed_range_exported","file":name,"sha256":file_hash(&dir.path().join(name))?,"exported_frames":"2","original_prefix_complete":false});
  let capture=Capture::open(dir.path().join("new.redb"),Default::default())?;import_raw_archive(dir.path(),&mut manifest,&capture).await?;
  let bounds=capture.bounds().await?;ensure!(bounds["message_count"]=="2","separate original envelopes were removed");
  ensure!(bounds["origin_prefix_complete"]==false&&bounds["origin_coverage"][0]["first_sequence"]=="42","new epoch hid missing legacy prefix");
  ensure!(bounds["origin_coverage"][0]["initial_state"]=="empty_retained_prefix_no_pg_latest","legacy latest seeded replay");
  let a=capture.get(1).await?;let b=capture.get(2).await?;ensure!(a.frame==frame&&b.frame==frame,"legacy payload or identity lost");
  ensure!(a.legacy.unwrap().broker_stored_at_ns!=b.legacy.unwrap().broker_stored_at_ns,"broker envelope times merged");
  import_raw_archive(dir.path(),&mut manifest,&capture).await?;ensure!(capture.bounds().await?["message_count"]=="2","retry imported duplicate original record");
  let name="incremental.frames";let mut file=private_file(&dir.path().join(name))?;
  for sequence in [44u64,45] {let header=RawHeader{schema:1,stream:"OLD".into(),epoch:"OLD:epoch".into(),sequence:sequence.to_string(),subject:"market.raw.jin10_web".into(),broker_stored_at_ns:sequence.to_string(),headers:frame.headers()?,body_bytes:frame.body.len(),body_sha256:hex::encode(Sha256::digest(&frame.body))};let bytes=serde_json::to_vec(&header)?;file.write_all(&(bytes.len() as u32).to_be_bytes())?;file.write_all(&bytes)?;file.write_all(&frame.body)?;}file.sync_all()?;
  let mut delta=LegacyManifest::new();delta.raw=json!({"state":"fixed_range_exported","file":name,"sha256":file_hash(&dir.path().join(name))?,"exported_frames":"2","original_prefix_complete":false});
  import_raw_archive(dir.path(),&mut delta,&capture).await?;verify_raw_import(dir.path(),&mut delta,&capture).await?;
  ensure!(delta.raw["native_mapping"]["first_position"]["sequence"]=="3"&&delta.raw["native_mapping"]["last_position"]["sequence"]=="4","incremental range masqueraded as native prefix one");
  verify_raw_import(dir.path(),&mut manifest,&capture).await?;ensure!(capture.bounds().await?["origin_coverage"][0]["first_sequence"]=="42","increment hid missing original prefix");
  delta.save(dir.path())?;let empty_dir=dir.path().join("empty-terminal");fs::create_dir_all(&empty_dir)?;private_file(&empty_dir.join("empty.frames"))?.sync_all()?;
  let mut empty=LegacyManifest::new();empty.raw=json!({"state":"fixed_range_exported","file":"empty.frames","sha256":file_hash(&empty_dir.join("empty.frames"))?,"exported_frames":"0","original_prefix_complete":false,"incremental_base":{"source_manifest_path":dir.path().join("manifest.json"),"source_manifest_sha256":file_hash(&dir.path().join("manifest.json"))?}});
  let before=file_hash(&dir.path().join(delta.raw["native_mapping"]["file"].as_str().unwrap()))?;
  import_raw_archive(&empty_dir,&mut empty,&capture).await?;verify_raw_import(&empty_dir,&mut empty,&capture).await?;
  import_raw_archive(&empty_dir,&mut empty,&capture).await?;verify_raw_import(&empty_dir,&mut empty,&capture).await?;
  ensure!(capture.bounds().await?["message_count"]=="4"&&empty.raw["native_mapping"]["last_position"]["sequence"]=="4","empty terminal delta changed capture or lost anchor");
  ensure!(file_hash(&dir.path().join(delta.raw["native_mapping"]["file"].as_str().unwrap()))?==before,"empty delta retry rewrote hardlinked immutable base mapping");
  capture.close().await?;Ok(())
 }
 #[test] fn legacy_precision_is_preserved_and_cursors_are_not_promoted(){
  let value:Value=serde_json::from_str(r#"{"instrument_symbol":"XAU/USD","realtime_source_id":"jin10_client","upstream_channel_id":"jin10_local","interval_seconds":60,"open_time":"2026-10-03T00:00:00.123456+00:00","close_time":"2026-10-03T00:01:00.123456+00:00","open":1234567890123456789.123456789012345678,"high":"1234567890123456789.123456789012345679","low":"1234567890123456789.123456789012345677","close":"1234567890123456789.123456789012345678","volume":null,"revision":"18446744073709551615","observed_at":"2026-10-03T00:00:00.123456+00:00","received_at":"2026-10-03T00:00:01.123456+00:00"}"#).unwrap();
  let converted=convert_bar("realtime_bars",&value).unwrap();assert_eq!(converted.open,"1234567890123456789.123456789012345678");assert!(converted.volume.is_none());assert_eq!(converted.revision,u64::MAX);assert_eq!(converted.open_time_ns%1_000_000_000,123456000);assert!(converted.received_sequence.is_none());assert!(convert_bar("derived_period_bars",&value).is_err());
 }
}
