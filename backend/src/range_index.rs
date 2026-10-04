//! Correctable 64-way UTC minute range index. Facts and all dirty nodes share one commit.
use crate::{domain::Decimal,native_codec::{Decoder,Encoder,StoredBar,component,signed_key,prefix_end},persistence_contract::ImportBarRow};
use anyhow::{Result,ensure};
use redb::{ReadTransaction,WriteTransaction,ReadableTable,ReadableTableMetadata,TableDefinition};
use serde::{Serialize,Deserialize};
use sha2::{Digest,Sha256};
use std::collections::BTreeSet;

pub(crate) const FACTS: TableDefinition<&[u8],&[u8]>=TableDefinition::new("canonical_bars_v1");
const NODES: TableDefinition<&[u8],&[u8]>=TableDefinition::new("minute_range_nodes_v1");
pub const MINUTE_NS: i64=60_000_000_000;
const LEVELS:u8=5; // 64^5 minutes exceeds the full signed i64 nanosecond domain.
pub(crate) type DirtyLeaves=BTreeSet<(String,String,i64)>;

pub(crate) fn scope(generation:&str,source:&str,symbol:&str,interval:u32)->Vec<u8> {
    let mut key=Vec::new();for value in [generation,source,symbol] { component(&mut key,value); }key.extend(interval.to_be_bytes());key
}
pub(crate) fn fact_key(generation:&str,row:&ImportBarRow)->Vec<u8> { let mut key=scope(generation,&row.realtime_source_id,&row.instrument_symbol,row.interval_seconds);key.extend(signed_key(row.open_time_ns));key }
fn node_key(generation:&str,source:&str,symbol:&str,level:u8,block:i64)->Vec<u8> {
    let mut key=scope(generation,source,symbol,60);key.push(level);key.extend(signed_key(block));key
}
pub(crate) fn dirty(row:&ImportBarRow,set:&mut DirtyLeaves) {
    if row.interval_seconds==60 { set.insert((row.realtime_source_id.clone(),row.instrument_symbol.clone(),row.open_time_ns.div_euclid(MINUTE_NS).div_euclid(64))); }
}
fn width(level:u8)->i64 { 64i64.pow(level as u32+1) }

#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct RangeAggregate {
    #[serde(with="crate::persistence_contract::i64_string")]
    pub first_open_time_ns:i64,
    #[serde(with="crate::persistence_contract::i64_string")]
    pub last_open_time_ns:i64,
    pub open:Decimal,pub high:Decimal,pub low:Decimal,pub close:Decimal,
    pub known_volume_sum:Decimal,
    pub source_volume_components:Vec<crate::source_volume::SourceVolumeComponents>,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub known_volume_count:u64,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub total_count:u64,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub final_count:u64,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub forming_count:u64,
    pub revision_sum:Decimal,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub last_commit_id:u64,
    #[serde(with="crate::persistence_contract::optional_u64_string")]
    pub received_sequence:Option<u64>,
    #[serde(with="crate::persistence_contract::i64_string")]
    pub source_observed_at_ns:i64,
    #[serde(with="crate::persistence_contract::i64_string")]
    pub received_at_ns:i64,
    #[serde(with="crate::persistence_contract::optional_i64_string")]
    pub finalized_at_ns:Option<i64>,
    #[serde(with="crate::persistence_contract::optional_i64_string")]
    pub accepted_at_ns:Option<i64>,
    #[serde(with="crate::persistence_contract::u64_string")]
    pub accepted_known_count:u64,
    #[serde(with="crate::persistence_contract::optional_u64_string")]
    pub applied_frame_sequence:Option<u64>,
    pub capture_epoch:Option<String>,
    pub contiguous:bool,
    pub digest:String,
}
impl RangeAggregate {
    pub fn volume(&self)->Option<Decimal> { (self.known_volume_count==self.total_count).then(||self.known_volume_sum.clone()) }
    pub fn state(&self)->&str { if self.final_count==self.total_count {"final"} else if self.forming_count>0 {"forming"} else {"provisional"} }
    fn fact(stored:&StoredBar,bytes:&[u8])->Result<Self> {
        let row=&stored.row;
        let sum=row.volume.as_deref().map(Decimal::from_str_exact).transpose()?.unwrap_or(Decimal::ZERO);
        let known=u64::from(row.volume.is_some());
        let source_components=crate::source_volume::read(&row.source_metadata["raw_payload"],sum.clone(),known,1)?;
        Ok(Self {first_open_time_ns:row.open_time_ns,last_open_time_ns:row.open_time_ns,
            open:Decimal::from_str_exact(&row.open)?,high:Decimal::from_str_exact(&row.high)?,low:Decimal::from_str_exact(&row.low)?,close:Decimal::from_str_exact(&row.close)?,
            known_volume_sum:sum,known_volume_count:known,source_volume_components:source_components,
            total_count:1,final_count:u64::from(row.state=="final"),forming_count:u64::from(matches!(row.state.as_str(),"forming"|"provisional_quote")),revision_sum:Decimal::from(row.revision),
            last_commit_id:stored.commit_id,received_sequence:row.received_sequence,source_observed_at_ns:row.source_observed_at_ns,received_at_ns:row.received_at_ns,contiguous:true,
            finalized_at_ns:row.finalized_at_ns,accepted_at_ns:row.source_metadata["raw_payload"]["capture_accepted_at_ns"].as_str().and_then(|v|v.parse().ok()),
            accepted_known_count:u64::from(row.source_metadata["raw_payload"]["capture_accepted_at_ns"].as_str().and_then(|v|v.parse::<i64>().ok()).is_some()),
            applied_frame_sequence:stored.capture.as_ref().map(|p|p.sequence),capture_epoch:stored.capture.as_ref().map(|p|p.epoch.clone()),digest:hex::encode(Sha256::digest(bytes))})
    }
    pub(crate) fn append(&mut self,other:Self)->Result<()> {
        ensure!(self.last_open_time_ns<other.first_open_time_ns,"overlapping or unordered aggregate nodes");
        self.contiguous &= other.contiguous && self.last_open_time_ns.checked_add(MINUTE_NS)==Some(other.first_open_time_ns);
        self.last_open_time_ns=other.last_open_time_ns;self.close=other.close;
        if other.high>self.high {self.high=other.high;} if other.low<self.low {self.low=other.low;}
        self.known_volume_sum=&self.known_volume_sum+&other.known_volume_sum;
        crate::source_volume::merge(&mut self.source_volume_components,other.source_volume_components)?;
        self.revision_sum=&self.revision_sum+&other.revision_sum;
        self.known_volume_count=self.known_volume_count.checked_add(other.known_volume_count).ok_or_else(||anyhow::anyhow!("volume count overflow"))?;
        self.total_count=self.total_count.checked_add(other.total_count).ok_or_else(||anyhow::anyhow!("component count overflow"))?;
        self.final_count=self.final_count.checked_add(other.final_count).ok_or_else(||anyhow::anyhow!("final count overflow"))?;
        self.forming_count=self.forming_count.checked_add(other.forming_count).ok_or_else(||anyhow::anyhow!("forming count overflow"))?;
        self.last_commit_id=self.last_commit_id.max(other.last_commit_id);self.received_sequence=self.received_sequence.max(other.received_sequence);
        self.source_observed_at_ns=self.source_observed_at_ns.max(other.source_observed_at_ns);self.received_at_ns=self.received_at_ns.max(other.received_at_ns);
        self.finalized_at_ns=self.finalized_at_ns.zip(other.finalized_at_ns).map(|(a,b)|a.max(b));
        self.accepted_at_ns=self.accepted_at_ns.into_iter().chain(other.accepted_at_ns).max();
        self.accepted_known_count=self.accepted_known_count.checked_add(other.accepted_known_count).ok_or_else(||anyhow::anyhow!("accepted clock count overflow"))?;
        if self.capture_epoch==other.capture_epoch {self.applied_frame_sequence=self.applied_frame_sequence.zip(other.applied_frame_sequence).map(|(a,b)|a.max(b));}
        else {self.capture_epoch=None;self.applied_frame_sequence=None;}
        let mut hash=Sha256::new();hash.update(hex::decode(&self.digest)?);hash.update(hex::decode(other.digest)?);self.digest=hex::encode(hash.finalize());Ok(())
    }
    fn encode(&self,e:&mut Encoder)->Result<()> {
        e.i64(self.first_open_time_ns);e.i64(self.last_open_time_ns);
        for value in [&self.open,&self.high,&self.low,&self.close,&self.known_volume_sum,&self.revision_sum] {e.decimal(value)?;}
        for value in [self.known_volume_count,self.total_count,self.final_count,self.forming_count,self.last_commit_id] {e.u64(value);}
        e.optional_u64(self.received_sequence);e.i64(self.source_observed_at_ns);e.i64(self.received_at_ns);e.optional_i64(self.finalized_at_ns);e.optional_i64(self.accepted_at_ns);e.optional_u64(self.applied_frame_sequence);
        e.u8(u8::from(self.capture_epoch.is_some()));if let Some(epoch)=&self.capture_epoch {e.text(epoch)?;}
        e.u8(u8::from(self.contiguous));e.bytes(&hex::decode(&self.digest)?)?;e.u64(self.accepted_known_count);
        crate::source_volume::validate(&self.source_volume_components)?;
        e.u8(self.source_volume_components.len().try_into()?);
        for group in &self.source_volume_components{e.text(&group.policy)?;e.decimal(&group.known_volume_sum)?;e.u64(group.known_count);e.u64(group.total_count);}
        Ok(())
    }
    fn decode(d:&mut Decoder<'_>,version:u8)->Result<Self> {
        let mut out=Self {first_open_time_ns:d.i64()?,last_open_time_ns:d.i64()?,open:d.decimal()?,high:d.decimal()?,low:d.decimal()?,close:d.decimal()?,known_volume_sum:d.decimal()?,revision_sum:d.decimal()?,
            known_volume_count:d.u64()?,total_count:d.u64()?,final_count:d.u64()?,forming_count:d.u64()?,last_commit_id:d.u64()?,received_sequence:d.optional_u64()?,source_observed_at_ns:d.i64()?,received_at_ns:d.i64()?,finalized_at_ns:d.optional_i64()?,accepted_at_ns:d.optional_i64()?,applied_frame_sequence:d.optional_u64()?,capture_epoch:match d.u8()? {0=>None,1=>Some(d.text()?),_=>anyhow::bail!("invalid aggregate epoch tag")},contiguous:d.u8()?==1,digest:hex::encode(d.bytes(32)?),accepted_known_count:0,source_volume_components:vec![]};
        out.accepted_known_count=if version>=4{d.u64()?}else{if out.accepted_at_ns.is_some(){out.total_count}else{0}};
        ensure!(out.accepted_known_count<=out.total_count,"invalid accepted clock coverage");
        if version>=5{let count=d.u8()? as usize;ensure!(count>0&&count<=crate::source_volume::MAX_POLICIES,"invalid source volume policy count");for _ in 0..count{out.source_volume_components.push(crate::source_volume::SourceVolumeComponents{policy:d.text()?,known_volume_sum:d.decimal()?,known_count:d.u64()?,total_count:d.u64()?});}}
        else{out.source_volume_components=crate::source_volume::read(&serde_json::Value::Null,out.known_volume_sum.clone(),out.known_volume_count,out.total_count)?;}
        crate::source_volume::validate(&out.source_volume_components)?;Ok(out)
    }
}
#[derive(Default,Clone)]
struct Node { all:Option<RangeAggregate>,final_only:Option<RangeAggregate> }
pub(crate) fn space_usage(tx:&ReadTransaction,generation:&str)->Result<serde_json::Value> {
    let nodes=tx.open_table(NODES)?;let stats=nodes.stats()?;
    let mut prefix=Vec::new();component(&mut prefix,generation);let end=prefix_end(&prefix)?;
    let mut count=0u64;let mut bytes=0u64;let mut reencoded=0u64;
    for row in nodes.range(prefix.as_slice()..end.as_slice())? {let(key,value)=row?;count+=1;bytes=bytes.checked_add((key.value().len()+value.value().len()) as u64).ok_or_else(||anyhow::anyhow!("index byte size overflow"))?;
        reencoded=reencoded.checked_add((key.value().len()+Node::decode(value.value())?.encode()?.len()) as u64).ok_or_else(||anyhow::anyhow!("index re-encoding byte size overflow"))?;}
    let budget=bytes.checked_add(reencoded).and_then(|n|n.checked_mul(4)).and_then(|n|n.checked_add(64*1024*1024)).ok_or_else(||anyhow::anyhow!("index rebuild budget overflow"))?;
    Ok(serde_json::json!({"generation":generation,"nodes":count.to_string(),"encoded_key_value_bytes":bytes.to_string(),"table_stored_bytes":stats.stored_bytes().to_string(),"table_metadata_bytes":stats.metadata_bytes().to_string(),"table_fragmented_bytes":stats.fragmented_bytes().to_string(),"current_codec_reencoded_key_value_bytes_estimate":reencoded.to_string(),"reencode_estimate_policy":"existing-node quantities; new fact policies may add separately bounded groups","rebuild_growth_budget_bytes":budget.to_string()}))
}
fn append(target:&mut Option<RangeAggregate>,value:Option<RangeAggregate>)->Result<()> {
    if let Some(value)=value { if let Some(current)=target {current.append(value)?;} else {*target=Some(value);} }Ok(())
}
impl Node {
    fn encode(&self)->Result<Vec<u8>> {let mut e=Encoder::new(5);for v in [&self.all,&self.final_only] {e.u8(u8::from(v.is_some()));if let Some(v)=v {v.encode(&mut e)?;}}Ok(e.0)}
    fn decode(bytes:&[u8])->Result<Self> {
        let version=*bytes.first().ok_or_else(||anyhow::anyhow!("empty range node"))?;
        ensure!(matches!(version,2|3|4|5),"unknown range node codec");
        let mut d=Decoder::new(bytes,version)?;
        fn field(d:&mut Decoder<'_>,version:u8)->Result<Option<RangeAggregate>> {match d.u8()? {0=>Ok(None),1=>Ok(Some(RangeAggregate::decode(d,version)?)),_=>anyhow::bail!("invalid aggregate tag")}}
        let out=Self {all:field(&mut d,version)?,final_only:field(&mut d,version)?};d.finish()?;Ok(out)
    }
}

/// Recompute each affected leaf and ancestor once, even for large history frames.
pub(crate) fn recompute(tx:&WriteTransaction,generation:&str,mut affected:DirtyLeaves)->Result<()> {
    let facts=tx.open_table(FACTS)?;let mut nodes=tx.open_table(NODES)?;
    for level in 0..LEVELS {
        let mut parents=DirtyLeaves::new();
        for (source,symbol,block) in affected {
            let mut node=Node::default();
            if level==0 {
                let prefix=scope(generation,&source,&symbol,60);
                let lo=(block as i128)*64*(MINUTE_NS as i128);let hi=lo+64*(MINUTE_NS as i128);
                let mut first=prefix.clone();first.extend(signed_key(lo.max(i64::MIN as i128).min(i64::MAX as i128) as i64));
                let last=if hi>i64::MAX as i128 {prefix_end(&prefix)?}else{let mut k=prefix;k.extend(signed_key(hi.max(i64::MIN as i128) as i64));k};
                for record in facts.range(first.as_slice()..last.as_slice())? {
                    let (_,bytes)=record?;let fact=StoredBar::decode(bytes.value())?;
                    let aggregate=RangeAggregate::fact(&fact,bytes.value())?;
                    if fact.row.state=="final" {append(&mut node.final_only,Some(aggregate.clone()))?;}
                    append(&mut node.all,Some(aggregate))?;
                }
            } else {
                for child in block*64..block*64+64 {
                    let key=node_key(generation,&source,&symbol,level-1,child);
                    if let Some(bytes)=nodes.get(key.as_slice())? {let child=Node::decode(bytes.value())?;append(&mut node.all,child.all)?;append(&mut node.final_only,child.final_only)?;}
                }
            }
            let key=node_key(generation,&source,&symbol,level,block);
            if node.all.is_some() {let bytes=node.encode()?;nodes.insert(key.as_slice(),bytes.as_slice())?;} else {nodes.remove(key.as_slice())?;}
            parents.insert((source,symbol,block.div_euclid(64)));
        }
        affected=parents;
    }Ok(())
}
fn ceil_minute(ns:i64)->i64 {let q=ns.div_euclid(MINUTE_NS);q+i64::from(ns.rem_euclid(MINUTE_NS)!=0)}

/// Select canonical fact opens in [start_ns,end_ns); covers missing nodes without scans.
pub(crate) fn query(tx:&ReadTransaction,generation:&str,source:&str,symbol:&str,start_ns:i64,end_ns:i64,final_only:bool)->Result<Option<RangeAggregate>> {
    ensure!(start_ns<=end_ns,"inverted aggregate range");let facts=tx.open_table(FACTS)?;let nodes=tx.open_table(NODES)?;
    let mut minute=ceil_minute(start_ns);let end=ceil_minute(end_ns);let mut result=None;
    while minute<end {
        let level=(0..LEVELS).rev().find(|&level|minute.rem_euclid(width(level))==0 && minute.checked_add(width(level)).is_some_and(|v|v<=end));
        let aggregate=if let Some(level)=level {
            let key=node_key(generation,source,symbol,level,minute.div_euclid(width(level)));
            let value=nodes.get(key.as_slice())?.map(|v|Node::decode(v.value())).transpose()?;
            minute+=width(level);value.and_then(|v|if final_only {v.final_only}else{v.all})
        } else {
            let time=(minute as i128)*(MINUTE_NS as i128);let mut key=scope(generation,source,symbol,60);key.extend(signed_key(time.try_into()?));minute+=1;
            if let Some(bytes)=facts.get(key.as_slice())? {let fact=StoredBar::decode(bytes.value())?;if !final_only||fact.row.state=="final" {Some(RangeAggregate::fact(&fact,bytes.value())?)}else{None}}else{None}
        };
        append(&mut result,aggregate)?;
    }Ok(result)
}

pub(crate) fn initialize(tx:&WriteTransaction)->Result<()> { tx.open_table(FACTS)?;tx.open_table(NODES)?;Ok(()) }

/// Rebuild a derived index without re-encoding or replacing any fact bytes.
pub(crate) fn rebuild(tx:&WriteTransaction,generation:&str)->Result<serde_json::Value> {
    let mut prefix=Vec::new();component(&mut prefix,generation);let end=prefix_end(&prefix)?;
    let mut dirty=DirtyLeaves::new();let mut hash=Sha256::new();let mut fact_rows=0u64;
    {
        let facts=tx.open_table(FACTS)?;
        for entry in facts.range(prefix.as_slice()..end.as_slice())? {
            let(key,bytes)=entry?;hash.update(key.value());hash.update(Sha256::digest(bytes.value()));fact_rows+=1;
            let fact=StoredBar::decode(bytes.value())?;self::dirty(&fact.row,&mut dirty);
        }
    }
    // Remove only this generation's derived nodes, including possible orphans.
    tx.open_table(NODES)?.retain(|key,_|!key.starts_with(&prefix))?;
    let leaves=dirty.len();recompute(tx,generation,dirty)?;
    Ok(serde_json::json!({"fact_rows":fact_rows.to_string(),"fact_codec_sha256":hex::encode(hash.finalize()),"leaf_blocks":leaves.to_string(),"facts_rewritten":false,"aggregation_version":crate::persistence_contract::AGGREGATION_VERSION}))
}

/// Offline verification materializes only 64-minute nodes, never full fact rows.
pub(crate) fn verify(tx:&ReadTransaction,generation:&str)->Result<serde_json::Value> {
    let mut generation_prefix=Vec::new();component(&mut generation_prefix,generation);let end=prefix_end(&generation_prefix)?;
    let facts=tx.open_table(FACTS)?;let nodes=tx.open_table(NODES)?;
    let mut leaves=std::collections::BTreeMap::<(String,String,i64),Node>::new();let mut fact_hash=Sha256::new();let mut fact_count=0u64;
    for record in facts.range(generation_prefix.as_slice()..end.as_slice())? {
        let (key,bytes)=record?;let fact=StoredBar::decode(bytes.value())?;fact_count+=1;fact_hash.update(key.value());fact_hash.update(Sha256::digest(bytes.value()));
        if fact.row.interval_seconds!=60 {continue;}
        let aggregate=RangeAggregate::fact(&fact,bytes.value())?;
        let node=leaves.entry((fact.row.realtime_source_id.clone(),fact.row.instrument_symbol.clone(),fact.row.open_time_ns.div_euclid(MINUTE_NS).div_euclid(64))).or_default();
        if fact.row.state=="final" {append(&mut node.final_only,Some(aggregate.clone()))?;}append(&mut node.all,Some(aggregate))?;
    }
    let mut node_count=0u64;
    for level in 0..LEVELS {
        let mut parents=std::collections::BTreeMap::<(String,String,i64),Node>::new();
        for ((source,symbol,block),node) in leaves {
            let key=node_key(generation,&source,&symbol,level,block);let stored=nodes.get(key.as_slice())?.ok_or_else(||anyhow::anyhow!("missing range index node at level {level}, block {block}"))?;
            let expected=node.encode()?;
            if stored.value()!=expected.as_slice() {
                let decoded=Node::decode(stored.value())?;
                let representations=|value:&Option<RangeAggregate>|value.as_ref().map(|v| {
                    [&v.open,&v.high,&v.low,&v.close,&v.known_volume_sum,&v.revision_sum].map(|d| {
                        let (coefficient,scale)=d.coefficient_and_scale();(coefficient.to_string(),scale)
                    })
                });
                anyhow::bail!("range index differs from exact facts at level {level}, block {block}, source {source}, symbol {symbol}; expected decimal coefficient/scale {:?}/{:?}, decoded stored {:?}/{:?}; expected/stored SHA256 {}/{}",
                    representations(&node.all),representations(&node.final_only),representations(&decoded.all),representations(&decoded.final_only),hex::encode(Sha256::digest(&expected)),hex::encode(Sha256::digest(stored.value())));
            }node_count+=1;
            let parent=parents.entry((source,symbol,block.div_euclid(64))).or_default();append(&mut parent.all,node.all)?;append(&mut parent.final_only,node.final_only)?;
        }leaves=parents;
    }
    let mut stored_count=0u64;let mut index_hash=Sha256::new();
    for entry in nodes.range(generation_prefix.as_slice()..end.as_slice())? {let(key,bytes)=entry?;stored_count+=1;index_hash.update(key.value());index_hash.update(Sha256::digest(bytes.value()));}
    ensure!(stored_count==node_count,"unexpected orphan range index nodes");
    Ok(serde_json::json!({"complete":true,"index_verified":true,"fact_rows":fact_count.to_string(),"index_nodes":node_count.to_string(),"fact_codec_sha256":hex::encode(fact_hash.finalize()),"index_codec_sha256":hex::encode(index_hash.finalize()),"schema_version":crate::persistence_contract::SCHEMA_VERSION,"aggregation_version":crate::persistence_contract::AGGREGATION_VERSION}))
}
