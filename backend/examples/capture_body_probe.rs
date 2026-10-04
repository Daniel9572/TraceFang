//! Matched read-only export subset: inline bodies versus atomic content addressing.
#[path="../src/capture.rs"]mod capture;
use capture::{Capture,CaptureOptions,ProviderFrame,LegacyOrigin};
use anyhow::{Result,Context,ensure};
use serde::{Serialize,Deserialize};use serde_json::{Value,json};use sha2::{Digest,Sha256};
use std::{fs::{self,File},io::{Read,Write,BufReader,BufWriter,Seek,SeekFrom},path::Path,collections::{BTreeMap,HashSet},time::Instant};
#[derive(Serialize,Deserialize)]struct Header{schema:u32,stream:String,epoch:String,sequence:String,subject:String,broker_stored_at_ns:String,headers:async_nats::HeaderMap,body_bytes:usize,body_sha256:String}
fn read_header(input:&mut impl Read)->Result<Option<Header>>{let mut length=[0;4];if input.read(&mut length[..1])?==0{return Ok(None)}input.read_exact(&mut length[1..])?;let n=u32::from_be_bytes(length) as usize;ensure!(n<=65536,"archive header too large");let mut bytes=vec![0;n];input.read_exact(&mut bytes)?;Ok(Some(serde_json::from_slice(&bytes)?))}
fn sha_file(path:&Path)->Result<String>{let mut input=File::open(path)?;let mut hash=Sha256::new();let mut bytes=vec![0;1024*1024];loop{let n=input.read(&mut bytes)?;if n==0{break}hash.update(&bytes[..n]);}Ok(hex::encode(hash.finalize()))}
fn stats(mut values:Vec<f64>)->Value{values.sort_by(f64::total_cmp);let n=values.len();if n==0{return json!({"n":0})}json!({"n":n,"p50_ms":values[n/2],"p95_ms":values[((n*95).div_ceil(100)-1).min(n-1)]})}
fn boundary(seq:u64,repeated:bool)->ProviderFrame{ProviderFrame{version:1,channel:"probe".into(),connection_id:"large-boundary".into(),sequence:seq,received_at:chrono::DateTime::from_timestamp(1_800_000_000+seq as i64,123456789).unwrap(),encoding:"binary".into(),body:vec![if repeated{1}else{seq as u8};32*1024*1024]}}
#[tokio::main]async fn main()->Result<()> {
 let args:Vec<String>=std::env::args().collect();
 if args.get(1).is_some_and(|v|v=="kill-child"){
  let cap=Capture::open(&args[2],Default::default())?;let a=cap.append(&boundary(1,true)).await?;let b=cap.append(&boundary(2,true)).await?;
  fs::write(&args[3],serde_json::to_vec(&vec![a,b])?)?;File::open(&args[3])?.sync_all()?;
  #[cfg(unix)]unsafe{libc::kill(libc::getpid(),libc::SIGKILL);}return Ok(())
 }
 let source=Path::new(args.get(1).context("usage: capture_body_probe EXPORTED_DIRECTORY OWNED_CACHE_DIRECTORY REPORT.json")?);let dir=Path::new(args.get(2).context("owned cache directory missing")?);let report=args.get(3).context("report path missing")?;
 fs::create_dir_all(dir)?;ensure!(fs::read_dir(dir)?.next().is_none(),"probe directory must be fresh and owned");fs::write(dir.join("tracefang-owned-probe.json"),b"{\"purpose\":\"capture-body-directed\"}")?;
 let manifest:Value=serde_json::from_slice(&fs::read(source.join("manifest.json"))?)?;let raw=source.join(manifest["raw"]["file"].as_str().context("raw file missing")?);
 ensure!(sha_file(&raw)?==manifest["raw"]["sha256"].as_str().unwrap_or(""),"raw archive checksum differs");
 let sample=dir.join("representative.frames");let mut input=BufReader::new(File::open(raw)?);let mut output=BufWriter::new(File::create(&sample)?);let mut selected=0u64;let mut channels=BTreeMap::<String,u64>::new();let mut duplicates=HashSet::new();let mut logical=0u64;let mut unique=0u64;
 while let Some(header)=read_header(&mut input)?{ensure!(header.body_bytes<=32*1024*1024,"actual raw exceeds accepted boundary");let channel=header.headers.get("Market-Frame-Channel").context("channel missing")?.to_string();let sequence=header.sequence.parse::<u64>()?;
  if channel=="tonghuashun_futures_history"||sequence%97==0{let mut body=vec![0;header.body_bytes];input.read_exact(&mut body)?;ensure!(hex::encode(Sha256::digest(&body))==header.body_sha256,"body checksum differs");let encoded=serde_json::to_vec(&header)?;output.write_all(&(encoded.len() as u32).to_be_bytes())?;output.write_all(&encoded)?;output.write_all(&body)?;selected+=1;*channels.entry(channel).or_default()+=1;logical+=body.len() as u64;if duplicates.insert(header.body_sha256.clone()){unique+=body.len() as u64;}}
  else{input.seek(SeekFrom::Current(header.body_bytes.try_into()?))?;}
 }output.flush()?;output.get_ref().sync_all()?;drop(output);
 let sample_hash=sha_file(&sample)?;let mut runs=vec![];
 for addressed in [false,true] {
  let label=if addressed{"content_addressed"}else{"inline"};let path=dir.join(format!("{label}.redb"));let cap=Capture::open(&path,CaptureOptions{content_addressed_bodies:addressed,..Default::default()})?;
  let mut input=BufReader::new(File::open(&sample)?);let mut times=BTreeMap::<String,Vec<f64>>::new();let mut seen=HashSet::new();let mut number=0u64;
  while let Some(header)=read_header(&mut input)? {let mut body=vec![0;header.body_bytes];input.read_exact(&mut body)?;let frame=ProviderFrame::from_parts(&header.headers,&body)?;number+=1;
   // The directed subset is a distinct synthetic origin, with contiguous subset order.
   // Original provider envelope stays exact; original broker sequence remains in the sample.
   let origin=LegacyOrigin{stream:"directed_export_subset".into(),epoch:format!("{}:subset-{sample_hash}",header.epoch),sequence:number.to_string(),broker_stored_at_ns:header.broker_stored_at_ns};
   let category=if seen.insert(header.body_sha256){"new_body"}else{"repeated_body"};let start=Instant::now();let receipt=cap.append_legacy(&frame,origin).await?;let elapsed=start.elapsed().as_secs_f64()*1000.0;
   times.entry(category.into()).or_default().push(elapsed);times.entry(format!("channel:{}",frame.channel)).or_default().push(elapsed);
   ensure!(cap.get_at(&receipt.position).await?.frame==frame,"reference changed complete provider frame");
  }
  let mut boundary_times=vec![];for (seq,repeated) in [(1,true),(2,true),(3,false)]{let frame=boundary(seq,repeated);let start=Instant::now();let receipt=cap.append(&frame).await?;boundary_times.push(start.elapsed().as_secs_f64()*1000.0);ensure!(cap.get_at(&receipt.position).await?.frame==frame,"boundary raw body changed");}
  let bounds=cap.bounds().await?;cap.close_and_drain().await?;drop(cap);let size=fs::metadata(&path)?.len();let reopened=Capture::open(&path,Default::default())?;ensure!(reopened.bounds().await?["message_count"].as_str()==Some(&(selected+3).to_string()),"reopen lost frames");reopened.close().await?;drop(reopened);
  runs.push(json!({"mode":label,"durability":"same Immediate commit+sync receipt","timings":times.into_iter().map(|(k,v)|(k,stats(v))).collect::<BTreeMap<_,_>>(),"boundary_32_mib":{"n":3,"samples_ms":boundary_times,"two_same_bodies_one_unique":true},"redb_file_bytes":size.to_string(),"bounds":bounds,"full_frame_roundtrip_and_reopen":true}));
 }
 let killed=dir.join("kill.redb");let receipts=dir.join("kill-receipts.json");let status=std::process::Command::new(std::env::current_exe()?).arg("kill-child").arg(&killed).arg(&receipts).status()?;ensure!(!status.success(),"kill child did not terminate");
 let receipts:Vec<tracefang_core::persistence_contract::DurableReceipt>=serde_json::from_slice(&fs::read(receipts)?)?;let cap=Capture::open(&killed,Default::default())?;for (i,receipt) in receipts.iter().enumerate(){ensure!(cap.get_at(&receipt.position).await?.frame==boundary(i as u64+1,true),"atomic body/frame lost after SIGKILL");}ensure!(cap.bounds().await?["unique_bodies"]=="1","kill duplicated body");cap.close().await?;
 let result=json!({"source_manifest_id":manifest["id"],"source_raw_sha256":manifest["raw"]["sha256"],"subset_policy":"all 277 history frames plus original global sequence divisible by 97; original provider envelope retained; contiguous synthetic subset origin is not full recovery coverage","subset_sha256":sample_hash,"selected_frames":selected.to_string(),"channels":channels,"logical_body_bytes":logical.to_string(),"unique_body_bytes":unique.to_string(),"runs":runs,"sigkill_after_receipts_passed":true,"limits":["exploratory native macOS; simultaneous builds/OS may affect p95","matched sequential receipt workloads; no universal SLO","redb physical allocated bytes include reusable pages; table payload byte accounting reported separately","SIGKILL is not physical power loss","32MiB is synthetic boundary; actual exported max is about1.93MB"]});
 fs::write(report,serde_json::to_vec_pretty(&result)?)?;println!("matched body comparison passed; report saved");Ok(())
}
