use std::{collections::BTreeMap, str::FromStr};
use anyhow::{Context,Result,bail};
use chrono::{DateTime,Utc};
use serde_json::{Value,json};
use tracefang_core::domain::{Decimal,QuoteSnapshot,SourceMetadata};
use crate::catalog::Definition;

#[derive(Default,Clone)]
pub struct QuoteCache { values:BTreeMap<(String,String),QuoteSnapshot> }
impl QuoteCache {
    pub fn snapshot(&self)->Result<Value> {Ok(serde_json::to_value(self.values.values().collect::<Vec<_>>())?)}
    pub fn restore(snapshot:Value)->Result<Self> {
        let mut result=Self::default();
        for quote in serde_json::from_value::<Vec<QuoteSnapshot>>(snapshot)? {
            quote.validate()?;
            let key=(quote.source.provider.clone(),quote.instrument.symbol.clone());
            anyhow::ensure!(!result.values.contains_key(&key), "duplicate checkpoint quote");
            result.values.insert(key,quote);
        }
        Ok(result)
    }
    pub fn accept(&mut self,quote:QuoteSnapshot)->bool {
        let key=(quote.source.provider.clone(),quote.instrument.symbol.clone());
        if let Some(current)=self.values.get(&key) {
            if &quote==current || quote.source.observed_at<current.source.observed_at {return false;}
            if quote.source.observed_at==current.source.observed_at {
                if let Some(order)=quote.source.application_cmp(&current.source) {if order.is_lt(){return false;}}
                else if quote.source.received_at<current.source.received_at {return false;}
            }
        }
        self.values.insert(key,quote);true
    }
    /// A transaction receipt is authoritative, including the original price clock
    /// retained when a later statistics-only frame was applied.
    pub fn replace_canonical(&mut self,quote:QuoteSnapshot)->Result<()> {
        quote.validate()?;
        self.values.insert((quote.source.provider.clone(),quote.instrument.symbol.clone()),quote);
        Ok(())
    }
    pub fn get(&self,channel:&str,symbol:&str)->Option<&QuoteSnapshot> {
        self.values.get(&(channel.into(),symbol.into()))
    }
    pub fn view(&self,definition:&Definition,source:&str,now:DateTime<Utc>,allow_stale:bool)->Result<Value> {
        if definition.quote_kind=="derived" {return self.derived(definition,now,allow_stale)}
        let client=source=="jin10_client";
        let channel=if client {"jin10_web"} else {source};
        let price=self.get(channel,&definition.instrument.symbol).context("该品种暂时没有所选来源报价")?;
        let age=if client {12} else {30};
        let price_stale=!price.source.is_fresh(now,age);
        if price_stale&&!allow_stale {bail!("所选来源报价已过期")}
        let mut quote=price.clone();
        let mut unavailable:Vec<&str>=vec![];
        let mut stale:Vec<&str>=vec![];
        if client {
            let supplement=self.get("jin10_local",&definition.instrument.symbol);
            let supplement_stale=supplement.is_some_and(|s|!s.source.is_fresh(now,12));
            for field in ["open","high","low","volume"] {
                let value=if supplement_stale&&!allow_stale {None} else {supplement.and_then(|q|number(q,field))};
                set_number(&mut quote,field,value.clone());
                if supplement.is_none()||(!supplement_stale||allow_stale)&&value.is_none(){unavailable.push(field)}
                if supplement_stale {stale.push(field)}
            }
            for field in ["last","change","change_percent"] {
                if number(price,field).is_none(){unavailable.push(field)} else if price_stale {stale.push(field)}
            }
            quote.source.provider=source.into();
            quote.source.provider_symbol=definition.instrument.symbol.clone();
            quote.source.raw_payload=None;
        } else {
            for field in ["last","open","high","low","volume","change","change_percent"] {
                if number(price,field).is_none(){unavailable.push(field)}else if price_stale{stale.push(field)}
            }
        }
        Ok(json!({"source_id":source,"quote":quote,"quality":if unavailable.is_empty()&&stale.is_empty(){"complete"}else{"degraded"},
          "unavailable_fields":unavailable,"stale_fields":stale,"composed_at":now}))
    }
    fn derived(&self,d:&Definition,now:DateTime<Utc>,allow_stale:bool)->Result<Value> {
        let gold=self.get("jin10_web","XAU/USD").context("人民币金价缺少黄金报价")?;
        let fx=self.get("jin10_web","USD/CNH").context("人民币金价缺少汇率报价")?;
        let stale=!gold.source.is_fresh(now,12)||!fx.source.is_fresh(now,12);
        if stale&&!allow_stale {bail!("人民币金价的一条换算行情已过期")}
        let grams=Decimal::from_str("31.1034768")?;
        let last=(gold.last.clone()*fx.last.clone()).div_significant(&grams,28).context("黄金换算除法不可表示")?;
        let previous=gold.change.clone().zip(fx.change.clone()).and_then(|(g,f)|{
            let g=gold.last.clone()-g;let f=fx.last.clone()-f;
            (g>Decimal::ZERO&&f>Decimal::ZERO).then(||(g*f).div_significant(&grams,28)).flatten()
        });
        let change=previous.clone().map(|v|precision28(last.clone()-v));
        let percent=change.clone().zip(previous).and_then(|(v,p)|(v*Decimal::from(100)).div_significant(&p,28));
        let change_missing=change.is_none();
        let quote=QuoteSnapshot{instrument:d.instrument.clone(),last,open:None,high:None,low:None,volume:None,
          change,change_percent:percent,source:SourceMetadata{
            provider:"jin10_client".into(),provider_symbol:"XAUUSD.GOODS*USDCNH.FXCM/31.1034768".into(),
            observed_at:gold.source.observed_at.max(fx.source.observed_at),received_at:gold.source.received_at.max(fx.source.received_at),
            raw_payload:Some(json!({"derivation":"XAUUSD * USDCNH / grams_per_troy_ounce","grams_per_troy_ounce":"31.1034768","calculation_version":"exact-wide-gold-fx-v2","rounding_policy":"final result 28 significant digits half-even",
              "gold_observed_at":gold.source.observed_at,"fx_observed_at":fx.source.observed_at}))}};
        let mut missing=vec!["open","high","low","volume"];
        if change_missing {missing.extend(["change","change_percent"])}
        Ok(json!({"source_id":"jin10_client","quote":quote,"quality":"degraded","unavailable_fields":missing,
          "stale_fields":if stale{vec!["last","change","change_percent"]}else{vec![]},"composed_at":now}))
    }
}

fn number(q:&QuoteSnapshot,field:&str)->Option<Decimal> {
    match field{"last"=>Some(q.last.clone()),"open"=>q.open.clone(),"high"=>q.high.clone(),"low"=>q.low.clone(),"volume"=>q.volume.clone(),"change"=>q.change.clone(),"change_percent"=>q.change_percent.clone(),_=>None}
}
fn set_number(q:&mut QuoteSnapshot,field:&str,value:Option<Decimal>) {
    match field{"open"=>q.open=value,"high"=>q.high=value,"low"=>q.low=value,"volume"=>q.volume=value,_=>{}}
}
pub fn precision28(v:Decimal)->Decimal {v.round_significant(28)}
