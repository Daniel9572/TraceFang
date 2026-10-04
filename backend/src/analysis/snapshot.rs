//! The same evaluator evidence used by UI, AI, live snapshots and simulations.
use super::{evaluator::Evaluator,quant::*};
use anyhow::Result;
use serde::{Serialize,Deserialize};
use serde_json::{Value,json};
#[derive(Clone,Debug,Serialize)]
pub struct QuantSnapshot{pub evidence:SnapshotEvidence,pub confirmed:Option<IndicatorPoint>,pub preview:Value,pub external:Vec<Value>,pub strategy_catalog:Vec<StrategyQualification>,pub series:Vec<ChartIndicatorPoint>,pub bars:Vec<QuantBar>,pub quote:Option<QuantQuote>,pub name:String,pub unit:String,pub executable_contract:bool}
pub fn external_context(point:&mut IndicatorPoint,input:&QuantInput){external_context_at(point,input,point.decision_at)}
pub fn external_context_at(point:&mut IndicatorPoint,input:&QuantInput,cutoff:chrono::DateTime<chrono::Utc>){
    point.indicators.insert("external_context_cutoff".into(),json!(cutoff));
    for (id,kind) in [("multi-timeframe","multi_timeframe"),("vix-gvz","volatility"),("volume-open-interest","positioning")]{
        let fact=input.external_facts.iter().filter(|f|f.kind==kind&&f.known_at().is_some_and(|at|at<=cutoff)).max_by_key(|f|f.known_at());
        if let Some(signal)=point.signals.iter_mut().find(|s|s.strategy_id==id){
            if let Some(fact)=fact{signal.state=if fact.unavailable_reason.is_some(){"context_unavailable"}else{"context"}.into();signal.evidence=vec![format!("来源 {}；observed={:?} published={:?} received={:?} known_at={:?}；record={:?} revision={:?} provenance={}；不进入合成/回测",fact.source,fact.observed_at,fact.published_at,fact.received_at,fact.known_at(),fact.record_id,fact.revision,fact.provenance)];if let Some(reason)=&fact.unavailable_reason{signal.evidence.push(reason.clone());}point.indicators.insert(kind.into(),if kind=="multi_timeframe"{multi_timeframe_context(input,fact,cutoff)}else{fact.value.clone()});}
            else{signal.state="context_unavailable".into();signal.evidence=vec!["截止点以前没有带可核验接收/发布时间的同版本外部证据；保持未知".into()];point.indicators.insert(kind.into(),Value::Null);}
        }
    }
}
fn external_evidence(input: &QuantInput) -> Vec<Value> {
    input.external_facts.iter().map(|fact| {
        let structural = fact.is_structural_coverage();
        let known_at = fact.known_at();
        let reason = fact.unavailable_reason.as_deref().or_else(|| {
            if structural { None }
            else if known_at.is_none() { Some("unknown availability time") }
            else if known_at.is_some_and(|at| at > input.decision_as_of) { Some("known after decision cutoff") }
            else { None }
        });
        let mut evidence = json!({"kind":fact.kind,"source":fact.source,"record_id":fact.record_id,"revision":fact.revision.map(|v|v.to_string()),"provenance":fact.provenance,"observed_at":fact.observed_at,"published_at":fact.published_at,"received_at":fact.received_at,"known_at":known_at,"included":fact.included_in_evidence(input.decision_as_of),"unavailable_reason":reason});
        if structural {
            evidence["evidence_role"] = json!("same_view_structural_coverage; not a timestamped market observation");
            evidence["value"] = fact.value.clone();
        }
        evidence
    }).collect()
}
pub fn evaluate(input:&QuantInput,parameters:&Parameters)->Result<QuantSnapshot>{let evidence=evidence(input,parameters)?;let mut evaluator=Evaluator::new(parameters.clone())?;let mut point=None;for bar in input.confirmed_bars(){point=Some(evaluator.push(bar,&input.semantics)?);}if let Some(p)=&mut point{external_context_at(p,input,input.decision_as_of);}
    let preview:Vec<_>=input.bars.iter().filter(|b|b.state!="final"||(input.semantics==HistorySemantics::OriginalEventReplay&&b.finalized_at.is_none())).collect();let external=external_evidence(input);
    Ok(QuantSnapshot{evidence,confirmed:point,preview:json!({"state":"unconfirmed_preview","bars":preview,"contributes_to_composite":false}),external,strategy_catalog:strategy_catalog(),series:vec![],bars:evaluator.bars.clone(),quote:input.quote.clone(),name:input.name.clone(),unit:input.unit.clone(),executable_contract:input.executable_contract})}
/// Chart histories contain scalar indicator values, never incomplete trading signals.
#[derive(Clone,Debug,Serialize,Deserialize)]
pub struct ChartIndicatorPoint{pub as_of:chrono::DateTime<chrono::Utc>,pub decision_at:chrono::DateTime<chrono::Utc>,pub indicators:std::collections::BTreeMap<String,Value>}
impl From<&IndicatorPoint> for ChartIndicatorPoint{fn from(point:&IndicatorPoint)->Self{Self{as_of:point.as_of,decision_at:point.decision_at,indicators:point.indicators.iter().filter(|(key,_)|["macd","kdj","rsi","moving_average","bollinger"].contains(&key.as_str())).map(|(key,value)|(key.clone(),value.clone())).collect()}}}
#[derive(Clone,Default)]
struct RenderCache{count:usize,last:Option<IndicatorPoint>,series:std::collections::VecDeque<ChartIndicatorPoint>}
/// Full-coverage authority snapshots retain only recurrence/window state.
#[derive(Clone,Serialize,Deserialize)]
pub struct SnapshotAccumulator{parameters:Parameters,input:Option<QuantInput>,prefix:Option<PrefixHash>,evaluator:Evaluator,last:Option<IndicatorPoint>,input_count:usize,confirmed_count:usize,preview_count:usize,prefix_preview_count:usize,series:std::collections::VecDeque<ChartIndicatorPoint>,#[serde(skip)]render_cache:std::sync::Arc<std::sync::Mutex<RenderCache>>}
impl SnapshotAccumulator{
    pub fn new(parameters:Parameters)->Result<Self>{Ok(Self{evaluator:Evaluator::new(parameters.clone())?,parameters,input:None,prefix:None,last:None,input_count:0,confirmed_count:0,preview_count:0,prefix_preview_count:0,series:std::collections::VecDeque::new(),render_cache:Default::default()})}
    pub fn snapshot(&self)->Result<Value>{Ok(json!({"checkpoint_schema":"quant-accumulator-chain-v1","calculation_version":CALCULATION_VERSION,"parameters_hash":content_hash(&self.parameters)?,"state":self}))}
    pub fn restore(value:Value,parameters:&Parameters)->Result<Self>{anyhow::ensure!(value["checkpoint_schema"]=="quant-accumulator-chain-v1"&&value["calculation_version"]==CALCULATION_VERSION,"quant accumulator checkpoint version mismatch");anyhow::ensure!(value["parameters_hash"]==content_hash(parameters)?,"quant accumulator checkpoint parameters differ");let state:Self=serde_json::from_value(value["state"].clone())?;anyhow::ensure!(state.parameters==*parameters&&state.series.len()<=1024,"invalid quant checkpoint");Evaluator::restore(state.evaluator.snapshot()?,parameters)?;Ok(state)}
    pub fn current(&self)->Result<QuantSnapshot>{self.clone().finish()}
    /// Native replay advances metadata clocks/cursor; the caller restores BEFORE
    /// an affected final bar on revision, and never seeds from live state.
    pub fn push_replay(&mut self,input:QuantInput)->Result<()>{anyhow::ensure!(input.semantics==HistorySemantics::OriginalEventReplay,"replay input must declare original event semantics");if let Some(previous)=&self.input{anyhow::ensure!(previous.code==input.code&&previous.source_id==input.source_id&&previous.period==input.period&&previous.token.capture_epoch==input.token.capture_epoch,"replay input identity changed");anyhow::ensure!(input.application_cursor.is_some()&&input.application_cursor>=previous.application_cursor,"replay cursor regressed");}self.input=None;self.push(input)}
    pub fn processed_bars(&self)->usize{self.confirmed_count}
    /// Only recurrence state belongs in a checkpoint. A fresh context-only read
    /// may change quote/cutoff metadata without changing this persisted prefix.
    pub fn checkpoint_identity(&self)->Result<String>{let replay_clock=self.input.as_ref().filter(|input|input.semantics==HistorySemantics::OriginalEventReplay).map(|input|(&input.token,input.application_cursor,input.decision_as_of));content_hash(&(&self.prefix,self.input_count,self.confirmed_count,self.preview_count,self.prefix_preview_count,self.input.as_ref().map(|input|&input.series_version),replay_clock))}
    pub fn resume_cursor(&self)->Option<ResumeCursor>{Some(ResumeCursor{after:self.evaluator.bars.last()?.open_time,series_version:self.input.as_ref()?.series_version.clone()?})}
    pub fn resume(mut self)->Self{self.input=None;self.input_count=self.confirmed_count+self.prefix_preview_count;self.preview_count=self.prefix_preview_count;self}
    pub fn push(&mut self,mut input:QuantInput)->Result<()>{input.validate()?;let bars=std::mem::take(&mut input.bars);if let Some(first)=&self.input{anyhow::ensure!(content_hash(&first.token)?==content_hash(&input.token)?,"snapshot batch changed MVCC transaction");}else{if let Some(prefix)=&self.prefix{anyhow::ensure!(prefix.compatible_seed(&input)?,"quant_resume_invalid: input seed changed");}else{self.prefix=Some(PrefixHash::new(&input,&self.parameters)?);}self.input=Some(input.clone());}
        for bar in bars{self.input_count+=1;if bar.state=="final"&&(input.semantics==HistorySemantics::FinalRevisionHistory||bar.finalized_at.is_some())&&bar.bucket_end<=input.decision_as_of&&(input.semantics==HistorySemantics::FinalRevisionHistory||bar.known_at(&input.semantics)<=input.decision_as_of)&&input.application_cursor.is_none_or(|seq|bar.applied_frame_seq.is_some_and(|at|at<=seq)){self.prefix.as_mut().unwrap().push(&bar)?;self.evaluator.advance_snapshot(bar,&input.semantics)?;self.confirmed_count+=1;self.prefix_preview_count=self.preview_count;}else{self.preview_count+=1;}}
        Ok(())}
    pub fn finish(mut self)->Result<QuantSnapshot>{let input=self.input.ok_or_else(||anyhow::anyhow!("missing authority snapshot metadata"))?;{let mut cache=self.render_cache.lock().map_err(|_|anyhow::anyhow!("render cache lock poisoned"))?;
        if cache.count>self.confirmed_count{*cache=RenderCache::default();}
        if cache.count!=self.confirmed_count||cache.last.is_none(){let after=cache.series.back().map(|p|p.as_of);cache.series.extend(self.evaluator.chart_points_after(&input.semantics,after)?);while cache.series.len()>1024{cache.series.pop_front();}cache.last=self.evaluator.current_point(&input.semantics)?;cache.count=self.confirmed_count;}
        self.last=cache.last.clone();self.series=cache.series.clone();}let prefix=self.prefix.unwrap();let effective_input_hash=prefix.effective_input_hash(&input,self.preview_count)?;let(input_hash,snapshot_hash)=prefix.finish(&input,input.decision_as_of,input.application_cursor)?;if let Some(point)=&mut self.last{external_context_at(point,&input,input.decision_as_of);}let external=external_evidence(&input);let evidence=SnapshotEvidence{schema_version:SCHEMA_VERSION,calculation_version:CALCULATION_VERSION,rounding_policy:super::exact::POLICY,input_hash,snapshot_hash,effective_input_hash,confirmed_prefix_hash:prefix.confirmed_prefix_hash(),code:input.code,source_id:input.source_id,period:input.period,decision_as_of:input.decision_as_of,token:input.token,semantics:input.semantics,input_count:self.input_count,confirmed_count:self.confirmed_count,preview_count:self.preview_count,warmup_complete:input.warmup_complete,capabilities:input.capabilities,parameters:self.parameters};Ok(QuantSnapshot{evidence,confirmed:self.last,preview:json!({"state":"unconfirmed_preview","count":self.preview_count,"contributes_to_composite":false,"note":"authority scan includes committed facts only; faster live cache is separate chart preview"}),external,strategy_catalog:strategy_catalog(),series:self.series.into_iter().collect(),bars:self.evaluator.bars.clone(),quote:input.quote,name:input.name,unit:input.unit,executable_contract:input.executable_contract})}
}

/// The historical 5/20 SMA summary consumes exact same-view closed buckets.
pub fn multi_timeframe_context(input:&QuantInput,fact:&ExternalFact,cutoff:chrono::DateTime<chrono::Utc>)->Value{
 use super::exact::{D,parse,div,n,text};use num_traits::{Zero,One};
 let mut summaries=vec![];
 for(horizon,period)in[("short","1h"),("medium","1d"),("long","1w")]{let entry=fact.value["horizons"].as_array().and_then(|rows|rows.iter().find(|v|v["period_id"]==period));let bars=entry.and_then(|e|e["bars"].as_array()).cloned().unwrap_or_default();let prices:Option<Vec<D>>=bars.iter().map(|r|r["close"].as_str().and_then(|v|parse(v).ok())).collect();let prices=prices.unwrap_or_default();let used=prices.len().min(20);let prices=&prices[prices.len()-used..];let ready=used==20&&prices.iter().all(|v|v>&D::zero());let fast=if prices.len()>=5&&prices[prices.len()-5..].iter().all(|v|v>&D::zero()){Some(div(&prices[prices.len()-5..].iter().cloned().sum::<D>(),&n(5)))}else{None};let slow=ready.then(||div(&prices.iter().cloned().sum::<D>(),&n(20)));let returns=ready.then(||(div(prices.last().unwrap(),&prices[0])-D::one())*100);let direction=match(fast.as_ref(),slow.as_ref(),returns.as_ref(),prices.last()){(Some(f),Some(s),Some(r),Some(last))if last>f&&f>s&&r>&D::zero()=>"up",(Some(f),Some(s),Some(r),Some(last))if last<f&&f<s&&r<&D::zero()=>"down",(Some(_),Some(_),Some(_),Some(_))=>"mixed",_=>"unavailable"};
 let time=|row:Option<&Value>,key:&str|->Option<chrono::DateTime<chrono::Utc>>{row?.get(key)?.as_str()?.parse::<i64>().ok().map(chrono::DateTime::from_timestamp_nanos)};
 summaries.push(json!({"horizon":horizon,"period_id":period,"state":if ready{"ready"}else if used==20{"unavailable"}else{"insufficient_data"},"direction":direction,"required_final_bars":20,"loaded_bar_count":bars.len(),"eligible_final_bar_count":bars.len(),"used_bar_count":used,"excluded_non_final_bars":0,"excluded_after_as_of_bars":0,"excluded_invalid_time_bars":0,"first_open_time":time(bars.first(),"open_time_ns"),"last_open_time":time(bars.last(),"open_time_ns"),"last_bucket_end":time(bars.last(),"close_time_ns"),"last_available_at":fact.known_at(),"last_close":prices.last().map(text),"sma_fast":fast.as_ref().map(text),"sma_slow":slow.as_ref().map(text),"window_return_percent":returns.as_ref().map(text),"limitation":if ready{None}else if used==20{Some("non_positive_close_not_comparable".into())}else{Some(format!("requires_20_final_bars_has_{used}"))},"history_search_bounded":entry.map(|v|v["history_search_bounded"].clone())}));}
 let ready=summaries.iter().filter(|v|v["state"]=="ready").count();let directions=summaries.iter().filter_map(|v|v["direction"].as_str()).collect::<std::collections::BTreeSet<_>>();let comparable=ready==3;let comparison=if !comparable{"not_comparable"}else if directions.len()==1&&!directions.contains("mixed"){"aligned"}else if directions.contains("up")&&directions.contains("down"){"divergent"}else{"mixed"};let reasons:Vec<_>=summaries.iter().filter(|v|v["state"]!="ready").map(|v|format!("{}:{}",v["horizon"].as_str().unwrap(),v["limitation"].as_str().unwrap_or("unavailable"))).collect();
 let differences:Vec<_>=(0..3).flat_map(|a|(a+1..3).filter_map({let summaries=&summaries;move|b|if comparable&&summaries[a]["direction"]!=summaries[b]["direction"]{Some(json!({"left":summaries[a]["horizon"],"right":summaries[b]["horizon"],"left_direction":summaries[a]["direction"],"right_direction":summaries[b]["direction"]}))}else{None}})).collect();
 json!({"contract_version":"multi-timeframe-trend-v1","profile_id":"swing-1h-1d-1w-v1","code":input.code,"instrument_symbol":input.instrument,"source_id":input.source_id,"decision_as_of":cutoff,"state":if ready==3{"ready"}else if ready>0{"partial"}else{"insufficient_data"},"as_of_policy":"closed canonical buckets and known source times within same MVCC cutoff; final revision history","direction_rule":"close_gt_sma5_gt_sma20_and_return20_gt_0; inverse for down","timeframes":summaries,"comparison":{"state":comparison,"comparable":comparable,"aligned_direction":if comparison=="aligned"{directions.iter().next().copied()}else{None},"differences":differences,"incomparable_reasons":reasons},"snapshot_token":input.token,"record_id":fact.record_id,"revision":fact.revision.map(|v|v.to_string()),"provenance":fact.provenance,"canonical_input":fact.value,"limitations":["Each horizon requires 20 closed bars from this same read view; insufficient coverage stays unknown.","Historical final revisions are not an as-observed trade reconstruction; received/finalization evidence is preserved.","Deterministic 5/20 SMA and return context does not enter composite or simulated fills."]})
}
