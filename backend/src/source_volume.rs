//! Source fragments and canonical minutes have separate quantity coverage.
//! A fallback counts canonical minutes; it does not assert upstream fragments.
use crate::domain::{CoreError,CoreResult,Decimal};
use serde::{Deserialize,Serialize};
use serde_json::{Value,json};
use std::collections::BTreeMap;

pub const MAX_POLICIES:usize=8;
pub const CANONICAL_FALLBACK:&str="canonical-minute-fallback-v1";
pub const FUYAO_INTERVAL:&str="fuyao-source-field-13-interval-v1";

#[derive(Clone,Debug,Serialize,Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceVolumeComponents {
    pub known_volume_sum:Decimal,
    #[serde(with="crate::persistence_contract::u64_string")]pub known_count:u64,
    #[serde(with="crate::persistence_contract::u64_string")]pub total_count:u64,
    pub policy:String,
}
fn error(message:&str)->CoreError{CoreError(message.into())}
impl SourceVolumeComponents {
    pub fn validate(&self)->CoreResult<()> {
        if self.total_count==0 || self.known_count>self.total_count || self.known_volume_sum<Decimal::ZERO || self.known_count==0 && self.known_volume_sum!=Decimal::ZERO {return Err(error("invalid source volume component coverage"))}
        if self.policy.is_empty() || self.policy.len()>128 || !self.policy.bytes().all(|c|c.is_ascii_alphanumeric()||matches!(c,b'_'|b'-'|b'.'|b'/'|b':')) {return Err(error("invalid source volume component policy"))}
        Ok(())
    }
}
pub fn validate(groups:&[SourceVolumeComponents])->CoreResult<()> {
    if groups.is_empty() || groups.len()>MAX_POLICIES{return Err(error("source volume policy groups exceed bound"))}
    let mut previous:Option<&str>=None;
    for group in groups {group.validate()?;if previous.is_some_and(|p|p>=group.policy.as_str()){return Err(error("source volume policies must be unique and sorted"))}previous=Some(&group.policy);}
    Ok(())
}
fn unsigned(v:&Value)->CoreResult<u64>{v.as_u64().or_else(||v.as_str()?.parse().ok()).ok_or_else(||error("source volume count must be an exact integer"))}
pub fn read(raw:&Value,known_volume_sum:Decimal,known_count:u64,total_count:u64)->CoreResult<Vec<SourceVolumeComponents>> {
    let one=&raw["source_volume_components"];let many=&raw["source_volume_component_groups"];
    if !one.is_null() && !many.is_null(){return Err(error("single and grouped source volume evidence are mutually exclusive"))}
    let groups=if !one.is_null(){vec![serde_json::from_value(one.clone()).map_err(|_|error("invalid typed source volume evidence"))?]}
    else if !many.is_null(){serde_json::from_value(many.clone()).map_err(|_|error("invalid grouped source volume evidence"))?}
    else if !raw["source_component_count"].is_null(){
        vec![SourceVolumeComponents{known_volume_sum:Decimal::from_str_exact(raw["source_component_known_volume_sum"].as_str().ok_or_else(||error("source component sum missing"))?).map_err(|_|error("source component sum invalid"))?,known_count:unsigned(&raw["source_component_known_volume_count"])? ,total_count:unsigned(&raw["source_component_count"])? ,policy:FUYAO_INTERVAL.into()}]
    }else{vec![SourceVolumeComponents{known_volume_sum,known_count,total_count,policy:CANONICAL_FALLBACK.into()}]};
    validate(&groups)?;Ok(groups)
}
pub fn merge(target:&mut Vec<SourceVolumeComponents>,incoming:Vec<SourceVolumeComponents>)->CoreResult<()> {
    validate(&incoming)?;if !target.is_empty(){validate(target)?;}
    let mut groups=std::mem::take(target).into_iter().map(|v|(v.policy.clone(),v)).collect::<BTreeMap<_,_>>();
    for value in incoming {
        if let Some(current)=groups.get_mut(&value.policy){
            current.known_volume_sum=&current.known_volume_sum+&value.known_volume_sum;
            current.known_count=current.known_count.checked_add(value.known_count).ok_or_else(||error("source known count exhausted"))?;
            current.total_count=current.total_count.checked_add(value.total_count).ok_or_else(||error("source component count exhausted"))?;
        }else{groups.insert(value.policy.clone(),value);}
        if groups.len()>MAX_POLICIES{return Err(error("source volume policies exceed bounded aggregate"))}
    }
    *target=groups.into_values().collect();validate(target)
}
pub fn write(raw:&mut Value,groups:&[SourceVolumeComponents])->CoreResult<()> {
    validate(groups)?;
    let object=raw.as_object_mut().ok_or_else(||error("source volume evidence requires object metadata"))?;
    object.remove("source_volume_components");object.remove("source_volume_component_groups");
    object.remove("source_volume_grouping_reason");object.remove("source_volume_fallback_evidence");
    if groups.len()==1 {object.insert("source_volume_components".into(),json!(groups[0]));}
    else{object.insert("source_volume_component_groups".into(),json!(groups));object.insert("source_volume_grouping_reason".into(),json!("distinct source policies cannot be combined as one quantity"));}
    object.insert("source_volume_unit".into(),json!("source_unspecified"));
    if groups.iter().any(|g|g.policy==CANONICAL_FALLBACK){object.insert("source_volume_fallback_evidence".into(),json!("counts canonical minutes only; original upstream fragment coverage is unknown"));}
    Ok(())
}

#[cfg(test)]mod tests{
    use super::*;
    #[test]fn distinct_policies_never_form_a_fabricated_total(){
        let mut groups=read(&Value::Null,Decimal::ZERO,0,1).unwrap();
        merge(&mut groups,vec![SourceVolumeComponents{known_volume_sum:Decimal::from(2),known_count:1,total_count:2,policy:FUYAO_INTERVAL.into()}]).unwrap();
        assert_eq!(groups.len(),2);let mut raw=json!({});write(&mut raw,&groups).unwrap();
        assert!(raw["source_volume_components"].is_null());assert_eq!(raw["source_volume_component_groups"][1]["known_volume_sum"],"2");assert_eq!(groups[0].total_count,1);
        assert!(raw["source_volume_fallback_evidence"].as_str().unwrap().contains("unknown"));
    }
}
