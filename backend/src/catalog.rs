use std::collections::BTreeMap;
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tracefang_core::domain::Instrument;

#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct Definition {
    pub code:String,
    pub name:String,
    pub instrument:Instrument,
    pub price_unit:String,
    pub price_digits:u32,
    pub quote_kind:String,
    pub history_backfill_supported:bool,
    pub dependencies:Vec<Instrument>,
    pub source_ids:Vec<String>,
    pub market_schedule_id:String,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub public_feed:Option<PublicFeed>,
}

#[derive(Debug,Clone,Serialize,Deserialize)]
pub struct PublicFeed {
    pub protocol:String,pub market:String,pub code:String,
    pub snapshot_sha256:String,pub reference_sha256:String,
    pub minute_clock_policy:String,pub trade_time:Value,
    pub auction_fold_verified_open_seconds:Vec<i64>,
    #[serde(default)]pub auction_fold_verified_trade_date:Option<chrono::NaiveDate>,
}

#[derive(Clone)]
pub struct Catalog {
    pub items:Vec<Definition>,
    pub schedules:BTreeMap<String,Value>,
    pub schedule_versions:BTreeMap<String,String>,
    pub default_watchlist:Vec<String>,
}

impl Catalog {
    pub fn embedded()->Result<Self> {
        let mut items:Vec<Definition>=serde_json::from_str(include_str!("../assets/catalog.json"))?;
        items.extend(serde_json::from_str::<Vec<Definition>>(include_str!("../assets/futures_catalog.json"))?);
        Self::validate_items(&items)?;
        let mut schedules:BTreeMap<String,Value>=serde_json::from_str(include_str!("../assets/schedules.json"))?;
        let date_exceptions:Value=serde_json::from_str(include_str!("../assets/shfe-date-exceptions.json"))?;
        schedules.get_mut("shfe_metals").context("SHFE base schedule missing")?["authority"]=json!({"date_exceptions":date_exceptions});
        let absolute:BTreeMap<String,Value>=serde_json::from_str(include_str!("../assets/fuyao-calendar.json"))?;
        for definition in &mut items {if definition.public_feed.is_some(){
            let key=provider_code(definition);let authority=absolute.get(&key).context("exact-date source calendar missing")?;
            let schedule_id=format!("source-date:{key}");
            schedules.insert(schedule_id.clone(),json!({"time_zone":"Asia/Shanghai","trading_day_rule":"shfe","reference":"captured source-provided absolute intervals for exact market/code/trading_date; no recurring inference","sessions":[],"authority":authority}));
            definition.market_schedule_id=schedule_id;
        }}
        Ok(Self {
            items,
            schedules,
            schedule_versions:serde_json::from_str(include_str!("../assets/schedule_versions.json"))?,
            default_watchlist:serde_json::from_str(include_str!("../assets/watchlist.json"))?,
        })
    }
    fn validate_items(items:&[Definition])->Result<()> {
        let mut aliases=BTreeMap::new();let mut providers=BTreeMap::new();
        for definition in items {
            for alias in [&definition.code,&definition.instrument.symbol] {
                let alias=alias.to_lowercase();
                if let Some(old)=aliases.insert(alias,definition.code.clone()){anyhow::ensure!(old==definition.code,"catalog alias maps to multiple canonical codes");}
            }
            anyhow::ensure!(providers.insert(provider_code(definition).to_lowercase(),definition.code.clone()).is_none(),"duplicate catalog definition/provider channel; a controlled evidence-channel merge is required");
        }Ok(())
    }
    pub fn get(&self,code:&str)->Result<&Definition> {
        self.items.iter().find(|d|d.code.eq_ignore_ascii_case(code)||d.instrument.symbol.eq_ignore_ascii_case(code))
            .context("unsupported instrument")
    }
    pub fn by_provider(&self,code:&str)->Option<&Definition> {
        self.items.iter().find(|d|provider_code(d).eq_ignore_ascii_case(code))
    }
    pub fn public(&self,d:&Definition)->Value {
        json!({"provider":"canonical","provider_code":d.code,"name":d.name,"instrument":d.instrument,
          "price_unit":d.price_unit,"price_digits":d.price_digits,"quote_kind":d.quote_kind,
          "history_backfill_supported":d.history_backfill_supported,"source_ids":d.source_ids,
          "source_mapping":d.public_feed,"capabilities":if d.public_feed.is_some(){json!(["snapshot","recent_minute_history","source_clock_milliseconds"])}else{json!(["quote","candles"])},
          "source_period_reference":d.public_feed.as_ref().map(|feed|json!({"source_id":"tonghuashun_futures","period":"min_5","market":feed.market,"code":feed.code})),
          "derived_period_support":{"available":true,"scope":if d.public_feed.is_some(){"source_provided_exact_trade_dates_only"}else if d.market_schedule_id=="shfe_metals"{"configured_base_hours_with_official_shfe_2025_2026_date_exceptions"}else{"configured_schedule"},"recurring_calendar_verified":d.public_feed.is_none(),"reason":if d.public_feed.is_some(){Some("outside captured exact trading dates calendar is unverified; longer periods may be partial")}else{None}},
          "dependencies":d.dependencies.iter().filter_map(|v|self.get(&v.symbol).ok().map(|d|d.code.clone())).collect::<Vec<_>>(),
          "market_schedule":self.schedules.get(&d.market_schedule_id)})
    }
}

#[cfg(test)]mod tests{
    use super::*;
    #[test]fn canonical_codes_and_provider_channels_are_unique(){
        let catalog=Catalog::embedded().unwrap();assert_eq!(catalog.items.len(),102);
        let mut duplicated=catalog.items.clone();duplicated.push(duplicated[0].clone());assert!(Catalog::validate_items(&duplicated).is_err());
        let mut alias=catalog.items.clone();alias[1].instrument.symbol=alias[0].instrument.symbol.clone();assert!(Catalog::validate_items(&alias).is_err());
        assert!(catalog.get("AU2610").unwrap().public_feed.is_none());
        assert_eq!(catalog.get("AU2612").unwrap().public_feed.as_ref().unwrap().code,"au2612");
        assert_eq!(catalog.by_provider("qh_au2610").unwrap().code,"AU2610");
        assert_eq!(catalog.by_provider("fuyao:65:au2612").unwrap().code,"AU2612");
    }
}

pub fn provider_code(d:&Definition)->String {
    if let Some(feed)=&d.public_feed {return format!("fuyao:{}:{}",feed.market,feed.code);}
    match d.code.as_str() {
        "XAUUSD"=>"XAUUSD.GOODS".into(), "XAGUSD"=>"XAGUSD.GOODS".into(),
        "USDCNH"=>"USDCNH.FXCM".into(), "USDIND"=>"wh_USDIND".into(),
        "BRN0Y"=>"219_BRN0Y".into(), "SHCOMP"=>"zs_1A0001".into(),
        "IXIC"=>"88_IXIC".into(), _=>format!("qh_{}",d.code.to_lowercase()),
    }
}

pub fn database_quote(mut row:Value,instrument:&Instrument)->Result<tracefang_core::domain::QuoteSnapshot> {
    row["instrument"]=serde_json::to_value(instrument)?;
    row["source"]=json!({"provider":row["source_id"],"provider_symbol":row["provider_symbol"],
      "observed_at":row["observed_at"],"received_at":row["received_at"],"raw_payload":row["raw_payload"]});
    Ok(serde_json::from_value(row)?)
}

pub fn database_bar(mut row:Value,instrument:&Instrument)->Result<tracefang_core::events::RealtimeBar> {
    // Canonical storage exposes interval_seconds; its UI value also has interval.
    // Serde aliases are alternatives, not two independent accepted fields.
    if let Some(object)=row.as_object_mut(){if object.contains_key("interval"){object.remove("interval_seconds");}}
    row["instrument"]=serde_json::to_value(instrument)?;
    row["source"]=json!({"provider":row["realtime_source_id"],"provider_symbol":row["provider_symbol"],
      "observed_at":row["observed_at"],"received_at":row["received_at"],"raw_payload":row["raw_payload"]});
    Ok(serde_json::from_value(row)?)
}
