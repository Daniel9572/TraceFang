//! Read-only measurement before enabling declared-session filtering on real data.
#[path="../src/catalog.rs"]mod catalog;
use anyhow::{Result,Context,ensure};
use tracefang_core::{native_store::Store,persistence_contract::CanonicalScanRequest,periods::MarketSchedule};
use serde_json::{Value,json};
use sha2::{Sha256,Digest};
fn file_hash(path:&std::path::Path)->Result<String>{use std::io::Read;let mut file=std::fs::File::open(path)?;let mut hash=Sha256::new();let mut buffer=[0u8;65536];loop{let count=file.read(&mut buffer)?;if count==0{break;}hash.update(&buffer[..count]);}Ok(hex::encode(hash.finalize()))}
#[tokio::main]async fn main()->Result<()> {
    let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==3,"usage: calendar_membership_audit FACTS GENERATION OUTPUT");
    let path=std::path::Path::new(&args[0]);let before=file_hash(path)?;
    let database=Store::open_read_only(path)?;let store=database.read_generation(&args[1]).await?;let inventory=store.series_inventory().await?;let catalog=catalog::Catalog::embedded()?;
    let mut items=Vec::<Value>::new();let mut total=0u64;let mut measured=0u64;let mut excluded=0u64;
    for item in inventory["series"].as_array().context("inventory series")? {
        let count=item["row_count"].as_str().context("exact inventory count")?.parse::<u64>()?;total+=count;
        let mut result=item.clone();let symbol=item["symbol"].as_str().context("symbol")?;let source=item["source_id"].as_str().context("source")?;
        let definition=catalog.items.iter().find(|d|d.instrument.symbol==symbol && d.source_ids.iter().any(|id|id==source));
        if item["interval_seconds"]!=60 {result["state"]=json!("not_applicable_base_second");items.push(result);continue;}
        if definition.is_none(){result["state"]=json!("minute_schedule_unmapped");items.push(result);continue;}
        let definition=definition.unwrap();let schedule:MarketSchedule=serde_json::from_value(catalog.schedules.get(&definition.market_schedule_id).context("declared source schedule")?.clone())?;
        let batch=store.canonical_scan_context(CanonicalScanRequest{symbol:symbol.into(),source_id:source.into(),interval_seconds:60,start_ns:i64::MIN,end_ns:i64::MAX,final_only:false,expected_version:Some(serde_json::from_value(inventory["version"].clone())?)},Some(schedule)).await?;
        let context=batch.context.context("calendar context")?;let stats=context.coverage["calendar_projection"].clone();let missing=stats["excluded_outside_schedule"].as_str().context("calendar excluded count")?.parse::<u64>()?;
        measured+=count;excluded+=missing;result["calendar_projection"]=stats.clone();result["state"]=json!(if missing==0{"all_declared_session_members"}else{"requires_source_and_calendar_review"});
        result["samples"]=json!([]);
        for field in ["earliest_ns","latest_ns"] {if let Some(at)=stats[field].as_str(){let at:i64=at.parse()?;let (_,rows)=store.lookup_bars(vec![tracefang_core::native_store::CanonicalBarKey{symbol:symbol.into(),source_id:source.into(),interval_seconds:60,open_time_ns:at}]).await?;result["samples"].as_array_mut().unwrap().push(json!({"boundary":field,"row":rows[0]}));}}
        items.push(result);
    }
    database.close().await?;drop(store);drop(database);let after=file_hash(path)?;ensure!(before==after,"read-only audit changed database bytes");
    let report=json!({"schema":"declared-calendar-membership-audit-v1","generated_at":chrono::Utc::now(),"generation":args[1],"inventory":inventory,"total_rows":total.to_string(),"measured_minute_rows":measured.to_string(),"excluded_rows":excluded.to_string(),"database_sha256_before":before,"database_sha256_after":after,"database_unchanged":true,"production_cutover":false,"policy":"quantity is measured against configured calendars; substantial exclusion is evidence to verify source clocks/schedules, never proof that the source is wrong","series":items});
    let output=std::path::Path::new(&args[2]);if let Some(parent)=output.parent(){std::fs::create_dir_all(parent)?;}std::fs::write(output,serde_json::to_vec_pretty(&report)?)?;println!("{}",json!({"total_rows":total.to_string(),"measured_rows":measured.to_string(),"excluded_rows":excluded.to_string(),"series":report["series"].as_array().unwrap().len(),"database_unchanged":true}));Ok(())
}
