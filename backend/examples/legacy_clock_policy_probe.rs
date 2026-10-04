//! Streaming policy worker for the bounded archive clock-projection planner.
#[path="../src/legacy_clock.rs"]mod legacy_clock;
use anyhow::Result;use std::io::{BufRead,Write};use serde_json::{Value,json};
fn main()->Result<()>{
 if std::env::args().nth(1).as_deref()==Some("identity"){println!("{}",json!({"schema":"tracefang-migration-tool-identity-v1","tool":"legacy_clock_policy_probe","backend_build_sha256":tracefang_core::quant_core::results::backend_build_fingerprint(),"version":env!("CARGO_PKG_VERSION")}));return Ok(())}
 if std::env::args().nth(1).as_deref()==Some("policy"){println!("{}",json!({"policy":legacy_clock::describe(),"build_sha256":tracefang_core::quant_core::results::backend_build_fingerprint()}));return Ok(())}
 let stdin=std::io::stdin();let mut output=std::io::BufWriter::new(std::io::stdout().lock());for line in stdin.lock().lines(){let value:Value=serde_json::from_str(&line?)?;let result=legacy_clock::project(&value["row"],value.get("history").filter(|v|!v.is_null()),value.get("original").filter(|v|!v.is_null()),&value["source_ref"],value["source_row_sha256"].as_str().unwrap_or(""));let mut result=match result{Ok(decision)=>serde_json::to_value(decision)?,Err(error)=>json!({"decision":"unresolved","reason":error.to_string()})};
  if result["evidence"]["restored_prior_final_state"]==true||result["evidence"]["original_final_not_restored_reason"].is_string() {
   if !value["original_source_ref"].is_object()||value["original_source_row_sha256"].as_str().is_none_or(|v|v.len()!=64){result=json!({"decision":"unresolved","reason":"restored final state has no immutable original selected-row reference"});}
   else{result["evidence"]["original_final_source_record"]=value["original_source_ref"].clone();result["evidence"]["original_final_source_row_sha256"]=value["original_source_row_sha256"].clone();result["row"]["_legacy_clock_projection"]=result["evidence"].clone();}
  }
  serde_json::to_writer(&mut output,&result)?;output.write_all(b"\n")?;output.flush()?;}Ok(())
}
