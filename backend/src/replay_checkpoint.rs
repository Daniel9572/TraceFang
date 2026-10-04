//! Versioned immutable replay seeds. No live Store/cache is accepted by this boundary.
use anyhow::{Result,ensure};
use serde::{Serialize,Deserialize};
use serde_json::Value;
use sha2::{Digest,Sha256};
use tracefang_core::{persistence_contract::CapturePosition,periods::canonical_json_ascii};

pub const REPLAY_VERSION:&str="tracefang-raw-replay-v2";
#[derive(Debug,Clone,Serialize,Deserialize,PartialEq,Eq)]
pub struct ReplayScope {
    pub projector_version:String,pub catalog_hash:String,pub schedule_hash:String,
    pub instrument:String,pub source:String,pub period:String,pub epoch:String,
}
impl ReplayScope {pub fn key(&self)->Result<String>{Ok(hex::encode(Sha256::digest(serde_json::to_vec(self)?)))}}
#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct Checkpoint {
    pub schema:u32,pub scope:ReplayScope,pub through:CapturePosition,
    pub origin_prefix_complete:bool,#[serde(default)] pub origin_coverage:Value,pub state:Value,pub state_hash:String,
}
impl Checkpoint {
    pub fn new(scope:ReplayScope,through:CapturePosition,origin_prefix_complete:bool,state:Value)->Self {
        let state_hash=state_hash(&state);Self{schema:1,scope,through,origin_prefix_complete,origin_coverage:Value::Null,state,state_hash}
    }
    pub fn validate(&self,scope:&ReplayScope,target:u64)->Result<()> {
        ensure!(self.schema==1&&&self.scope==scope,"checkpoint schema/evaluator/source/catalog/schedule/epoch differs");
        ensure!(self.through.epoch==scope.epoch&&self.through.sequence<target,"checkpoint is at or after replay target");
        ensure!(self.state_hash==state_hash(&self.state),"checkpoint content hash differs");Ok(())
    }
}
pub fn state_hash(value:&Value)->String{hex::encode(Sha256::digest(canonical_json_ascii(value).as_bytes()))}

#[cfg(test)] mod tests {
    use super::*;
    #[test] fn immutable_seed_rejects_future_wrong_scope_and_mutation(){
        let scope=ReplayScope{projector_version:REPLAY_VERSION.into(),catalog_hash:"a".into(),schedule_hash:"b".into(),instrument:"XAU/USD".into(),source:"jin10_client".into(),period:"1m".into(),epoch:"one".into()};
        let seed=Checkpoint::new(scope.clone(),CapturePosition{epoch:"one".into(),sequence:42,digest:"prefix".into()},false,serde_json::json!({"price":"0.0000000000000000000000000001","volume":null}));
        assert!(seed.validate(&scope,43).is_ok());assert!(seed.validate(&scope,42).is_err());
        let mut wrong=scope.clone();wrong.projector_version="future".into();assert!(seed.validate(&wrong,43).is_err());
        wrong=scope.clone();wrong.source="other".into();assert!(seed.validate(&wrong,43).is_err());
        let mut corrupt=seed.clone();corrupt.state["volume"]=serde_json::json!(0);assert!(corrupt.validate(&scope,43).is_err());
        assert_eq!(state_hash(&seed.state),seed.state_hash);
    }
}
