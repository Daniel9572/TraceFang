//! Isolated UI evidence: real Jin10 protocols, synthetic exact prices, no production reads.
#[path="../src/capture.rs"]mod capture;
use anyhow::{Result,Context,ensure};use chrono::{DateTime,Utc};use serde_json::json;
use base64::{Engine,engine::general_purpose::STANDARD};use std::io::Write;
fn history(sequence:u64,first:i64,count:usize,price:i64,received:DateTime<Utc>)->Result<capture::ProviderFrame>{
 let mut gzip=flate2::write::GzEncoder::new(Vec::new(),flate2::Compression::fast());for index in 0..count{for value in [first+index as i64*60,price+100_000_000,price,price-100_000_000,price,67]{gzip.write_all(&value.to_le_bytes())?;}}
 Ok(capture::ProviderFrame{version:1,channel:"jin10_history".into(),connection_id:"ui-synthetic-protocol-fixed".into(),sequence,received_at:received,encoding:"gzip-json".into(),body:serde_json::to_vec(&json!({"provider_code":"XAUUSD.GOODS","file":{"file_name":"ui-fixed-authoritative","record_count":count,"start_timestamp":null,"end_timestamp":null},"body_base64":STANDARD.encode(gzip.finish()?)}))?})
}
fn quote(sequence:u64,observed:i64,price:i64,received:DateTime<Utc>)->capture::ProviderFrame{
 let symbol="XAUUSD.GOODS";let mut body=10005u16.to_le_bytes().to_vec();body.extend((symbol.len() as u16).to_le_bytes());body.extend(symbol.as_bytes());body.extend((observed as u32).to_le_bytes());body.extend(price.to_le_bytes());body.extend(3_999_000_000i64.to_le_bytes());
 capture::ProviderFrame{version:1,channel:"jin10_web".into(),connection_id:"ui-synthetic-protocol-fixed".into(),sequence,received_at:received,encoding:"wire".into(),body}
}
#[tokio::main]async fn main()->Result<()>{
 let dir=std::path::PathBuf::from(std::env::args().nth(1).context("isolated validation directory required")?);ensure!(dir.to_string_lossy().contains("TraceFang-validation"),"fixture command requires explicit isolated validation path");
 let cap=capture::Capture::open(dir.join("capture.redb"),Default::default())?;ensure!(cap.bounds().await?["first_sequence"].is_null(),"UI fixture requires empty capture; existing evidence preserved");
 let received=DateTime::parse_from_rfc3339("2026-10-03T02:00:00.123456789Z")?.with_timezone(&Utc);let first=DateTime::parse_from_rfc3339("2026-09-01T00:00:00Z")?.timestamp();
 let frames=vec![history(1,first,260,4_000_000_000,received)?,quote(2,received.timestamp(),4_050_000_000,received+chrono::Duration::nanoseconds(2)),quote(3,received.timestamp(),4_060_000_000,received+chrono::Duration::nanoseconds(1)),history(4,first,1,4_200_000_000,received+chrono::Duration::nanoseconds(2))?];
 let mut receipts=Vec::new();for frame in frames{receipts.push(cap.append_legacy(&frame,capture::LegacyOrigin{stream:"UI_FIXED_PROTOCOL_FIXTURE".into(),epoch:"UI_FIXED_PROTOCOL_FIXTURE:v1".into(),sequence:frame.sequence.to_string(),broker_stored_at_ns:frame.received_at.timestamp_nanos_opt().unwrap().to_string()}).await?);}
 let manifest=json!({"kind":"isolated_synthetic_real_protocol_replay_fixture","production_read":false,"code":"XAUUSD","source_id":"jin10_client","period":"1m","bars_in_first_history":"260","first_open":DateTime::from_timestamp(first,0),"frames":receipts,"bounds":cap.bounds().await?,"time_policy":"legacy fixture received ns preserved; accepted unknown/imported separate; receive tie and rollback at seq2..4; seq4 revises first minute outside240 hot state","expected":{"sequence1_facts":"260","sequence4_first_close":"4200","sequence4_first_revision":"2","first_last":"1..4","raw_prefix_complete_for_this_fixture":true}});
 let file=dir.join("replay-fixture.json");let mut output=std::fs::File::create(&file)?;serde_json::to_writer_pretty(&mut output,&manifest)?;output.sync_all()?;cap.close_and_drain().await?;println!("{}",file.display());Ok(())
}
