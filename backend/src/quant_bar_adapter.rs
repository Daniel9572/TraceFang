//! Shared exact canonical bar adapter for replay and live authority scans.
use anyhow::Result;
use chrono::{DateTime,Utc};
use serde_json::Value;
use tracefang_core::{persistence_contract::ImportBarRow,quant_core::{quant::{QuantBar,SourceVolumeComponents},exact::{parse,d}}};
fn timestamp(value:i64)->DateTime<Utc>{DateTime::from_timestamp_nanos(value)}
fn unsigned(value:&Value)->Option<u64>{value.as_u64().or_else(||value.as_str()?.parse().ok())}
fn signed(value:&Value)->Option<i64>{value.as_i64().or_else(||value.as_str()?.parse().ok())}
fn source_precision(raw:&Value)->Option<u64>{unsigned(&raw["source_precision_ns"]).or_else(||unsigned(&raw["timestamp_precision_seconds"]).and_then(|v|v.checked_mul(1_000_000_000)))}
pub fn bar(row:ImportBarRow)->Result<QuantBar>{
    bar_ref(&row)
}
pub fn bar_ref(row:&ImportBarRow)->Result<QuantBar>{
    let raw=&row.source_metadata["raw_payload"];let volume=row.volume.as_deref().map(parse).transpose()?;
    let components=unsigned(&raw["component_count"]).unwrap_or(1);
    let known=unsigned(&raw["known_volume_count"]).unwrap_or(u64::from(volume.is_some()));
    let sum=raw["known_volume_sum"].as_str().map(parse).transpose()?.unwrap_or_else(||volume.clone().unwrap_or_else(||d("0")));
    let explicit=!raw["source_volume_components"].is_null() || !raw["source_volume_component_groups"].is_null() || !raw["source_component_count"].is_null();
    let groups=if explicit {tracefang_core::source_volume::read(raw,tracefang_core::domain::Decimal::ZERO,0,1)?.into_iter().map(|g|Ok(SourceVolumeComponents{known_volume_sum:parse(&g.known_volume_sum.to_string())?,known_count:g.known_count,total_count:g.total_count,policy:g.policy})).collect::<Result<Vec<_>>>()?}else{vec![]};
    let (source_volume_components,source_volume_component_groups)=if groups.len()==1{(groups.first().cloned(),vec![])}else{(None,groups)};
    Ok(QuantBar {open_time:timestamp(row.open_time_ns),bucket_end:timestamp(row.close_time_ns),open:parse(&row.open)?,high:parse(&row.high)?,low:parse(&row.low)?,close:parse(&row.close)?,volume,known_volume_sum:sum,known_volume_count:known,component_count:components,source_volume_components,source_volume_component_groups,state:match row.state.as_str(){"final"=>"final","forming"|"provisional_quote"=>"forming",_=>"provisional"}.into(),revision:row.revision,observed_at:timestamp(row.source_observed_at_ns),received_at:timestamp(row.received_at_ns),accepted_at:signed(&raw["capture_accepted_at_ns"]).map(timestamp),finalized_at:row.finalized_at_ns.map(timestamp),applied_frame_seq:unsigned(&raw["capture_sequence"]),source_precision_ns:source_precision(raw)})
}
