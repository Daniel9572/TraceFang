//! Current native parser outputs from immutable captured SHFE evidence; no IO to providers.
#[path="../src/providers/shfe.rs"] mod shfe;
#[path="../src/catalog.rs"] mod catalog;
use anyhow::{Result,Context,ensure};
use serde_json::{Value,json};
use sha2::{Sha256,Digest};
fn main()->Result<()> {
    let args=std::env::args().skip(1).collect::<Vec<_>>();ensure!(args.len()==2,"usage: market_fixed_audit BASELINE OUTPUT");
    let base=std::path::Path::new(&args[0]);let mut hashes=serde_json::Map::new();
    let mut read=|name:&str|->Result<Value>{let bytes=std::fs::read(base.join(name))?;hashes.insert(name.into(),json!(hex::encode(Sha256::digest(&bytes))));Ok(serde_json::from_slice(&bytes)?)};
    let api=read("options-api.json")?;let option=read("options-source-0.json")?;let future=read("options-source-1.json")?;let master=read("options-source-2.json")?;let daily=read("options-source-3.json")?;
    let day=json!({"currentTradingday":api["trading_day"].as_str().context("trading day")?.replace('-',""),
        "lastTradingday":api["reference_data_as_of"].as_str().context("reference day")?.replace('-',"")});
    let all=shfe::parse_contracts(&master)?;
    let received=api["checked_at"].as_str().context("captured API clock")?.parse()?;
    let chain=shfe::parse_chain(&day,&option,&future,&master,&daily,"au",received)?;
    let cat=catalog::Catalog::embedded()?;
    let result=json!({"schema":"native-fixed-market-audit-v1","generated_at":chrono::Utc::now(),"input_sha256":hashes,
        "catalog":cat.items.iter().map(|d|cat.public(d)).collect::<Vec<_>>(),"master_contracts":all,"contracts":chain.quotes,
        "connected_option_products":["au"],"coverage_policy":"only the explicit current gold endpoint is connected; master parsing is metadata coverage",
        "build_fingerprint_inputs":"production SHFE parser + exact domain + current embedded catalog"});
    let output=std::path::Path::new(&args[1]);if let Some(parent)=output.parent(){std::fs::create_dir_all(parent)?;}
    std::fs::write(output,serde_json::to_vec_pretty(&result)?)?;Ok(())
}
