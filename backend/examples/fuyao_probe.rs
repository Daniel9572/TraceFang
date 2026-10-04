//! Fixed public-source acceptance. Expected values are emitted by an independent
//! Python Decimal oracle; this tool only emits production parser results.
#[path="../src/catalog.rs"]mod catalog;
#[path="../src/providers/fuyao.rs"]mod fuyao;
use anyhow::{Result,Context,ensure};
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
fn main()->Result<()> {
    let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==2,"usage: fuyao_probe FIXED_SOURCE_DIR OUTPUT");
    let root=std::path::Path::new(&args[0]);let catalog=catalog::Catalog::embedded()?;let manifest:Value=serde_json::from_slice(&std::fs::read(root.join("manifest.json"))?)?;
    let mut results=Vec::new();let mut hashes=serde_json::Map::new();
    for definition in catalog.items.iter().filter(|d|d.public_feed.is_some()) {
        let feed=definition.public_feed.as_ref().unwrap();let snapshot_name=format!("snapshot-{}.json",feed.market);let snapshot_bytes=std::fs::read(root.join(&snapshot_name))?;
        let quote_record=manifest.as_array().context("source manifest array")?.iter().find(|r|r["file"]==snapshot_name).context("source snapshot provenance missing")?;
        ensure!(hex::encode(Sha256::digest(&snapshot_bytes))==quote_record["sha256"].as_str().context("source snapshot hash")?,"snapshot fixture changed");
        hashes.insert(snapshot_name.clone(),quote_record["sha256"].clone());let snapshot:Value=serde_json::from_slice(&snapshot_bytes)?;
        let received=quote_record["received_at"].as_str().context("actual receive timestamp")?.parse()?;
        let quote=fuyao::parse_quote(&snapshot,&definition.instrument,feed,&definition.name,received);
        let quote_error=quote.as_ref().err().map(|error|error.to_string());
        let name=format!("kline-{}-{}.json",feed.market,feed.code);let bytes=std::fs::read(root.join(&name))?;
        let row=manifest.as_array().unwrap().iter().find(|r|r["file"]==name).context("source minute provenance missing")?;
        ensure!(hex::encode(Sha256::digest(&bytes))==row["sha256"].as_str().context("minute hash")?,"minute fixture changed");hashes.insert(name,row["sha256"].clone());
        let payload:Value=serde_json::from_slice(&bytes)?;let received=row["received_at"].as_str().context("minute receive timestamp")?.parse()?;
        let bars=fuyao::parse_minutes(&payload,&definition.instrument,feed,received,quote.as_ref().ok().map(|q|q.source.observed_at)).with_context(||format!("{} minute parser failed",definition.code))?;
        results.push(json!({"code":definition.code,"market":feed.market,"provider_code":feed.code,"quote":quote.ok(),"quote_error":quote_error,"bars":bars,
            "history_state":if bars.is_empty(){"source_success_empty"}else{"source_minute_records_normalized"},"recurring_calendar":"unverified","date_calendar_authority":"exact captured market/code/trading_date absolute UTC sessions; no weekly inference"}));
    }
    let report=json!({"schema":"fuyao-native-fixed-v2-absolute-date-calendar","inputs_sha256":hashes,"items":results,"runtime":"native Rust"});
    let output=std::path::Path::new(&args[1]);if let Some(parent)=output.parent(){std::fs::create_dir_all(parent)?;}std::fs::write(output,serde_json::to_vec_pretty(&report)?)?;println!("{}",json!({"contracts":report["items"].as_array().unwrap().len()}));Ok(())
}
