//! Gold-options DTO and analyses; missing exchange quantities stay unknown.
use crate::providers::shfe::{OptionChain, OptionQuote};
use chrono::NaiveDate;
use rust_decimal::RoundingStrategy;
use num_traits::ToPrimitive;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
};
use tracefang_core::domain::{CoreError, CoreResult, Decimal, Timestamp};

const LIMITATIONS: &[&str] = &[
    "上期所公开源为交易所延时行情, 不得标记为实时行情。",
    "逐合约 Delta 与月份 IV 来自上一交易日日报, 不代表盘中 Greeks。",
    "公开源不含逐合约 Gamma/Vega, 也没有做市商净头寸, 不能生成可信方向性 GEX。",
    "沪金以人民币/克计价, 与 XAUUSD 现货存在汇率、期限和基差差异。",
];
const SHFE_REQUIRED: &[&str] = &[
    "官方延时期权买卖价、最新价、成交量与持仓量",
    "官方合约到期日、行权价、合约乘数与对应标的",
    "上一交易日逐合约 Delta 与分月份 IV 参考值",
];
const CME_REQUIRED: &[&str] = &[
    "CME Real-Time Futures & Options WebSocket API 订阅",
    "CME API ID、认证凭据与 COMEX 贵金属数据授权",
    "若需官方 Greeks/IV, 另需 Options Analytics 数据权限",
];
const QUOTE_FIELDS: &[&str] = &[
    "contract_id",
    "underlying_contract_id",
    "expiry",
    "strike",
    "option_type",
    "bid",
    "ask",
    "last",
    "volume",
    "open_interest",
    "observed_at",
    "source_id",
];
fn error(message: &str) -> CoreError {
    CoreError(message.into())
}
fn numeric(value: Option<Decimal>) -> Value {
    value.map(|v|Value::String(v.normalize().to_string())).unwrap_or(Value::Null)
}
fn ratio(numerator: Option<u64>, denominator: Option<u64>) -> Option<Decimal> {
    let (n, d) = (numerator?, denominator?);
    if d == 0 {
        return None;
    }
    Decimal::from(n)
        .checked_div(Decimal::from(d))
        .map(|v| v.round_dp_with_strategy(4, RoundingStrategy::MidpointNearestEven))
}
fn total(quotes:&[&OptionQuote],field:impl Fn(&OptionQuote)->Option<u64>)->CoreResult<Option<u64>>{
    if quotes.is_empty(){return Ok(None);}let mut sum=0u64;let mut missing=false;
    for quote in quotes{if let Some(value)=field(quote){sum=sum.checked_add(value).ok_or_else(||error("option quantity total exceeds unsigned 64-bit range; not missing source data"))?;}else{missing=true;}}
    Ok(if missing{None}else{Some(sum)})
}
fn wall(quotes: &[&OptionQuote]) -> Option<Decimal> {
    if quotes.iter().any(|q| q.open_interest.is_none()) {
        return None;
    }
    quotes
        .iter()
        .filter(|q| q.open_interest.is_some_and(|v| v > 0))
        .max_by(|a, b| {
            (a.open_interest, std::cmp::Reverse(&a.contract.strike))
                .cmp(&(b.open_interest, std::cmp::Reverse(&b.contract.strike)))
        })
        .map(|q| q.contract.strike.clone())
}
fn max_pain(quotes: &[&OptionQuote]) -> CoreResult<Option<Decimal>> {
    if quotes.iter().any(|q| q.open_interest.is_none()) {
        return Ok(None);
    }
    let strikes: BTreeSet<_> = quotes.iter().map(|q| q.contract.strike.clone()).collect();
    let mut best: Option<(Decimal, Decimal)> = None;
    for settlement in strikes {
        let mut payout = Decimal::ZERO;
        for quote in quotes {
            let difference = if quote.contract.option_type == "call" {
                &settlement - &quote.contract.strike
            } else {
                &quote.contract.strike - &settlement
            };
            let value = difference
                .max(Decimal::ZERO)
                .checked_mul(Decimal::from(quote.open_interest.unwrap_or(0)))
                .and_then(|v| v.checked_mul(quote.contract.contract_multiplier.clone()))
                .ok_or_else(|| error("option payout exceeds decimal range"))?;
            payout = payout
                .checked_add(value)
                .ok_or_else(|| error("option payout total overflows"))?;
        }
        if best.as_ref().is_none_or(|(_, v)| &payout < v) {
            best = Some((settlement, payout));
        }
    }
    Ok(best.map(|(strike, _)| strike))
}

pub fn expiry_analyses(chain: &OptionChain) -> CoreResult<Vec<Value>> {
    let mut grouped: BTreeMap<(NaiveDate, &str), Vec<&OptionQuote>> = BTreeMap::new();
    for quote in &chain.quotes {
        grouped
            .entry((
                quote.contract.expiry,
                &quote.contract.underlying_contract_id,
            ))
            .or_default()
            .push(quote);
    }
    let mut rows = Vec::new();
    for ((expiry, underlying_id), quotes) in grouped {
        let calls: Vec<_> = quotes
            .iter()
            .copied()
            .filter(|q| q.contract.option_type == "call")
            .collect();
        let puts: Vec<_> = quotes
            .iter()
            .copied()
            .filter(|q| q.contract.option_type == "put")
            .collect();
        let call_oi = total(&calls, |q| q.open_interest)?;
        let put_oi = total(&puts, |q| q.open_interest)?;
        let call_volume = total(&calls, |q| q.volume)?;
        let put_volume = total(&puts, |q| q.volume)?;
        let oi_ratio = ratio(put_oi, call_oi);
        let underlying_price = chain.underlyings.get(underlying_id).and_then(|q| q.last.clone());
        let strikes: BTreeSet<_> = quotes.iter().map(|q| q.contract.strike.clone()).collect();
        let atm = underlying_price.as_ref().and_then(|price| {
            strikes
                .into_iter()
                .min_by_key(|strike| (strike - price).abs())
        });
        let iv = chain.reference_iv_by_underlying.get(underlying_id).cloned();
        let days = (expiry - chain.trading_day).num_days().max(0);
        // The sole floating-point calculation is a model estimate, never a source price.
        let expected_move = if days > 0 {
            iv.as_ref().and_then(|v| v.as_bigdecimal().to_f64())
                .and_then(|v| Decimal::from_f64_retain(v * (days as f64 / 365.0).sqrt() * 100.0))
                .map(|v| v.round_dp_with_strategy(2, RoundingStrategy::MidpointNearestEven))
        } else {
            None
        };
        let delta_count = quotes.iter().filter(|q| q.delta.is_some()).count() as u64;
        let positioning = match oi_ratio.as_ref() {
            Some(v) if v >= &Decimal::new(12, 1) => "put_open_interest_dominant",
            Some(v) if v <= &Decimal::new(8, 1) => "call_open_interest_dominant",
            Some(_) => "balanced_open_interest",
            None => "insufficient",
        };
        rows.push(json!({"underlying_contract_id":underlying_id,"expiry":expiry,
            "underlying_price":numeric(underlying_price),"option_count":quotes.len(),
            "call_open_interest":call_oi.map(|v|v.to_string()),"put_open_interest":put_oi.map(|v|v.to_string()),
            "put_call_open_interest_ratio":numeric(oi_ratio),"call_volume":call_volume.map(|v|v.to_string()),"put_volume":put_volume.map(|v|v.to_string()),
            "put_call_volume_ratio":numeric(ratio(put_volume,call_volume)),"atm_strike":numeric(atm),
            "call_wall_strike":numeric(wall(&calls)),"put_wall_strike":numeric(wall(&puts)),
            "max_pain_strike":numeric(max_pain(&quotes)?),"reference_iv":numeric(iv.clone()),
            "expected_move_percent":numeric(expected_move.clone()),"expected_move_calculation_policy":"approximate IV model in binary64: IV * sqrt(calendar_days / 365) * 100; percent rounded to 2 decimal places half-even; never a source price or fact",
            "expected_move_unavailable_reason":if expected_move.is_some(){None}else{Some(if days<=0{"expiry has no remaining calendar days"}else if iv.is_none(){"reference IV unknown"}else{"IV outside approximate model representability"})},"quantity_total_policy":"all components required; u64 overflow is an explicit error",
            "delta_coverage_ratio":numeric(ratio(Some(delta_count),Some(quotes.len() as u64))),
            "positioning_state":positioning,"gamma_state":"unavailable_missing_contract_gamma_and_dealer_position","gex":null}));
    }
    Ok(rows)
}

pub fn snapshot(
    chain: Option<&OptionChain>,
    failure: Option<&str>,
    checked_at: Timestamp,
) -> CoreResult<Value> {
    product_snapshot("au",chain,failure,checked_at)
}
pub fn product_snapshot(product:&str,chain:Option<&OptionChain>,failure:Option<&str>,checked_at:Timestamp)->CoreResult<Value>{
    if product.is_empty()||product.len()>6||!product.bytes().all(|b|b.is_ascii_lowercase()){return Err(error("invalid SHFE option product"));}
    if chain.is_some_and(|chain|chain.product!=product){return Err(error("SHFE option chain differs from requested product"));}
    let gold=product=="au";
    let contract_version=if gold{"gold-options-v2"}else{"shfe-product-options-v1"};
    let limitations=if gold{LIMITATIONS.to_vec()}else{LIMITATIONS[..3].to_vec()};
    let cme = json!({"market_id":"cme_comex_gold_options","label":"CME/COMEX 黄金期权",
        "state":"provider_and_entitlement_required","detail":"本机没有 CME API 订阅与贵金属行情授权。",
        "delivery_mode":null,"quote_count":0,"observed_at":null,"required_data":CME_REQUIRED});
    let Some(chain) = chain else {
        let state = if failure.is_some() {
            "unavailable"
        } else {
            "unconfigured"
        };
        let shfe=json!({"market_id":if gold{"shfe_gold_options".into()}else{format!("shfe_{product}_options")},"label":if gold{"上海期货交易所黄金期权".into()}else{format!("上海期货交易所 {product} 品种期权")},"state":if failure.is_some(){"unavailable"}else{"provider_required"},"detail":failure.unwrap_or("尚未取得可用的上期所该品种期权行情。"),"delivery_mode":if failure.is_some(){Some("exchange_delayed")}else{None},"quote_count":0,"observed_at":null,"required_data":SHFE_REQUIRED});
        let markets=if gold{vec![shfe,cme]}else{vec![shfe]};
        return Ok(
            json!({"contract_version":contract_version,"product":product,"state":state,"available":false,
            "provider_id":null,"market_id":null,"delivery_mode":null,"checked_at":checked_at,
            "observed_at":null,"trading_day":null,"reference_data_as_of":null,"quote_currency":null,
            "price_unit":null,"quote_count":0,"quoted_contract_count":"0","metadata_contract_count":null,"unquoted_contract_ids":[],"coverage":{"state":"unavailable","complete":false,"reason":"metadata_and_quotes_unavailable"},"markets":markets,
            "expiries":[],"contracts":[],"underlyings":[],"source_urls":[],
            "required_quote_fields":QUOTE_FIELDS,"analysis_state":"blocked_without_market_data",
            "detail":if failure.is_some(){"期权供应商请求失败。"}else{"当前没有可用的该品种期权行情。"},
            "limitations":limitations,"usage_notice":"不得把未授权或非实时数据重新标记为实时行情。",
            "refresh_after_seconds":15}),
        );
    };
    let state = if chain.delivery_mode == "live" {
        "live"
    } else {
        "delayed"
    };
    let expiries = expiry_analyses(chain)?;
    let quoted_count=u64::try_from(chain.quotes.len()).map_err(|_|error("SHFE quoted contract count overflow"))?;
    let unquoted_count=u64::try_from(chain.unquoted_contract_ids.len()).map_err(|_|error("SHFE unquoted contract count overflow"))?;
    let metadata_known=chain.metadata_contract_count>0&&quoted_count.checked_add(unquoted_count)==Some(chain.metadata_contract_count);
    let coverage=json!({"state":if !metadata_known{"unknown"}else if unquoted_count>0||!chain.missing_contracts.is_empty(){"partial"}else{"complete"},"complete":metadata_known&&unquoted_count==0&&chain.missing_contracts.is_empty(),"metadata_contract_count":metadata_known.then(||chain.metadata_contract_count.to_string()),"quoted_contract_count":quoted_count.to_string(),"unquoted_contract_count":metadata_known.then(||unquoted_count.to_string()),"reason":if !metadata_known{Some("master_coverage_not_verified")}else if unquoted_count>0{Some("metadata_contracts_without_quote_rows")}else if !chain.missing_contracts.is_empty(){Some("quote_rows_without_contract_metadata")}else{None},"semantics":"Quote-row coverage of this product's source master; nullable prices or quantities remain unknown independently."});
    let shfe=json!({"market_id":chain.market_id,"label":chain.market_label,"state":state,"detail":format!("已取得 {} 个真实合约报价; 行情截至 {}。",chain.quotes.len(),chain.observed_at.to_rfc3339()),"delivery_mode":chain.delivery_mode,"quote_count":chain.quotes.len(),"coverage":coverage,"observed_at":chain.observed_at,"required_data":SHFE_REQUIRED});
    let markets=if gold{vec![shfe,cme]}else{vec![shfe]};
    Ok(
        json!({"contract_version":contract_version,"product":product,"state":state,"available":true,
        "provider_id":chain.provider_id,"market_id":chain.market_id,"delivery_mode":chain.delivery_mode,
        "checked_at":checked_at,"observed_at":chain.observed_at,"trading_day":chain.trading_day,
        "reference_data_as_of":chain.reference_data_as_of,"quote_currency":chain.quote_currency,
        "price_unit":chain.price_unit,"quote_count":chain.quotes.len(),"metadata_contract_count":metadata_known.then(||chain.metadata_contract_count.to_string()),"quoted_contract_count":quoted_count.to_string(),"unquoted_contract_ids":chain.unquoted_contract_ids,"quote_contracts_without_metadata":chain.missing_contracts,"coverage":coverage,"markets":markets,"expiries":expiries,"contracts":chain.quotes,
        "underlyings":chain.underlyings.values().collect::<Vec<_>>(),"source_urls":chain.source_urls,
        "required_quote_fields":QUOTE_FIELDS,"analysis_state":"available_with_limitations",
        "detail":"已接入上期所官方公开延时期权链; 持仓墙、Put/Call 比和最大痛点来自真实报价与持仓, GEX 因缺少逐合约 Gamma 和做市商净头寸而不计算。",
        "limitations":limitations,"usage_notice":"仅用于本地研究展示。期权交易信息归上期所管理; 未经许可不得对外发布或用于未获授权的商业再分发。",
        "refresh_after_seconds":10}),
    )
}

pub fn ai_context(snapshot: &Value) -> Value {
    let rows=snapshot.get("expiries").and_then(Value::as_array).map(|rows|rows.iter().take(6).map(|row|
        json!({"underlying":row["underlying_contract_id"],"expiry":row["expiry"],"underlying_price":row["underlying_price"],
            "put_call_oi_ratio":row["put_call_open_interest_ratio"],"put_call_volume_ratio":row["put_call_volume_ratio"],
            "call_wall":row["call_wall_strike"],"put_wall":row["put_wall_strike"],"max_pain":row["max_pain_strike"],
            "reference_iv":row["reference_iv"],"positioning_state":row["positioning_state"],"gamma_state":row["gamma_state"]})
    ).collect::<Vec<_>>()).unwrap_or_default();
    json!({"state":snapshot["state"],"provider_id":snapshot["provider_id"],"market_id":snapshot["market_id"],
        "delivery_mode":snapshot["delivery_mode"],"observed_at":snapshot["observed_at"],"reference_data_as_of":snapshot["reference_data_as_of"],
        "quote_count":snapshot["quote_count"],"unit":snapshot["price_unit"],
        "scope_note":"SHFE gold futures options; not the same instrument as XAUUSD spot","expiries":rows,
        "limitations":snapshot["limitations"]})
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::providers::shfe::parse_chain;
    #[test]
    fn non_gold_product_does_not_inherit_gold_or_cme_evidence() {
        let result=product_snapshot("cu",None,None,chrono::Utc::now()).unwrap();
        assert_eq!(result["product"],"cu");assert_eq!(result["markets"].as_array().unwrap().len(),1);assert_eq!(result["markets"][0]["market_id"],"shfe_cu_options");assert!(result["price_unit"].is_null());assert!(!result.to_string().contains("黄金"));assert!(!result.to_string().contains("人民币/克"));assert!(!result.to_string().contains("CME"));
        assert_eq!(snapshot(None,None,chrono::Utc::now()).unwrap()["markets"].as_array().unwrap().len(),2);
    }
    #[test]
    fn quantities_and_context_remain_unavailable_without_source_evidence() {
        let master = json!({"OptionContractBaseInfo":[
            {"INSTRUMENTID":"au2611C900","COMMODITYID":"au","COMMODITYNAME":"黄金","EXPIREDATE":"20261026","TRADEUNIT":"1000"},
            {"INSTRUMENTID":"au2611P900","COMMODITYID":"au","COMMODITYNAME":"黄金","EXPIREDATE":"20261026","TRADEUNIT":"1000"}]});
        let quotes = json!({"delaymarket":[
            {"contractname":"au2611C900","updatetime":"2026-09-30 15:25:21","lastprice":"15","volume":"","openinterest":"100"},
            {"contractname":"au2611P900","updatetime":"2026-09-30 15:25:21","lastprice":"10","volume":"5","openinterest":"50"}]});
        let chain = parse_chain(
            &json!({"currentTradingday":"20261008","lastTradingday":"20260930"}),
            &quotes,
            &json!({"delaymarket":[]}),
            &master,
            &json!({}),
            "au",
            chrono::Utc::now(),
        )
        .unwrap();
        let result = snapshot(Some(&chain), None, chrono::Utc::now()).unwrap();
        assert_eq!(result["quote_count"], 2);
        assert_eq!(result["metadata_contract_count"],"2");assert_eq!(result["quoted_contract_count"],"2");assert_eq!(result["coverage"]["state"],"complete");
        let mut partial=chain.clone();partial.metadata_contract_count=3;partial.unquoted_contract_ids.push("au2611C950".into());let partial=snapshot(Some(&partial),None,chrono::Utc::now()).unwrap();assert_eq!(partial["coverage"]["state"],"partial");assert_eq!(partial["coverage"]["complete"],false);assert_eq!(partial["quoted_contract_count"],"2");assert_eq!(partial["metadata_contract_count"],"3");assert_eq!(partial["unquoted_contract_ids"],json!(["au2611C950"]));assert!(partial["quote_contracts_without_metadata"].as_array().unwrap().is_empty());
        let mut old=chain.clone();old.metadata_contract_count=0;assert_eq!(snapshot(Some(&old),None,chrono::Utc::now()).unwrap()["coverage"]["state"],"unknown");
        assert!(result["expiries"][0]["call_volume"].is_null());
        assert!(result["expiries"][0]["put_call_volume_ratio"].is_null());
        assert_eq!(
            result["expiries"][0]["put_call_open_interest_ratio"],
            json!("0.5")
        );
        assert_eq!(result["expiries"][0]["max_pain_strike"], json!("900"));
        assert!(result["expiries"][0]["gex"].is_null());
        assert_eq!(
            ai_context(&result)["expiries"][0]["put_call_oi_ratio"],
            json!("0.5")
        );
        let mut wide=chain.clone();wide.quotes[0].volume=Some(u64::MAX);wide.quotes[1].volume=Some(1);assert!(total(&wide.quotes.iter().collect::<Vec<_>>(),|q|q.volume).unwrap_err().to_string().contains("unsigned 64-bit"));wide.quotes[1].volume=None;assert_eq!(total(&wide.quotes.iter().collect::<Vec<_>>(),|q|q.volume).unwrap(),None);
        let unavailable = snapshot(None, Some("source offline"), chrono::Utc::now()).unwrap();
        assert_eq!(unavailable["state"], "unavailable");
        assert_eq!(unavailable["quote_count"], 0);
        assert!(unavailable["metadata_contract_count"].is_null());assert_eq!(unavailable["coverage"]["complete"],false);
    }

    #[test]
    #[ignore = "requires SHFE_BASELINE_DIR and SHFE_EXPECTED_FILE captured evidence"]
    fn captured_710_contracts_and_expiry_analyses_match_current_python() {
        let directory = std::path::PathBuf::from(std::env::var("SHFE_BASELINE_DIR").unwrap());
        let read = |name: &str| -> Value {
            serde_json::from_slice(&std::fs::read(directory.join(name)).unwrap()).unwrap()
        };
        let old = read("options-api.json");
        let day = json!({"currentTradingday":old["trading_day"].as_str().unwrap().replace('-',""),
                      "lastTradingday":old["reference_data_as_of"].as_str().unwrap().replace('-',"")});
        let master = read("options-source-2.json");
        let all = crate::providers::shfe::parse_contracts(&master).unwrap();
        assert_eq!(all.len(), 6276);
        assert_eq!(
            all.iter()
                .map(|c| &c.product)
                .collect::<BTreeSet<_>>()
                .len(),
            23
        );
        let chain = parse_chain(
            &day,
            &read("options-source-0.json"),
            &read("options-source-1.json"),
            &master,
            &read("options-source-3.json"),
            "au",
            chrono::Utc::now(),
        )
        .unwrap();
        let expected: Value = serde_json::from_slice(
            &std::fs::read(std::env::var("SHFE_EXPECTED_FILE").unwrap()).unwrap(),
        )
        .unwrap();
        let actual = snapshot(Some(&chain), None, chrono::Utc::now()).unwrap();
        fn same(actual: &Value, expected: &Value, path: &str) {
            match expected {
                Value::Object(rows) => {
                    for (key, value) in rows {
                        same(&actual[key], value, &format!("{path}.{key}"));
                    }
                }
                Value::Array(rows) => {
                    assert_eq!(actual.as_array().unwrap().len(), rows.len(), "{path}");
                    for (i, value) in rows.iter().enumerate() {
                        same(&actual[i], value, &format!("{path}[{i}]"));
                    }
                }
                _ => {
                    let a = actual
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| actual.to_string());
                    let b = expected
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| expected.to_string());
                    if let (Ok(a), Ok(b)) =
                        (Decimal::from_str_exact(&a), Decimal::from_str_exact(&b))
                    {
                        assert_eq!(a, b, "{path}");
                    } else if let (Ok(a), Ok(b)) = (
                        chrono::DateTime::parse_from_rfc3339(&a),
                        chrono::DateTime::parse_from_rfc3339(&b),
                    ) {
                        assert_eq!(a, b, "{path}");
                    } else {
                        assert_eq!(actual, expected, "{path}");
                    }
                }
            }
        }
        same(&actual["contracts"], &expected["contracts"], "contracts");
        same(&actual["expiries"], &expected["expiries"], "expiries");
        assert_eq!(chain.quotes.len(), 710);
        let missing = chain.quotes.iter().filter(|q| q.volume.is_none()).count();
        assert_eq!(missing, 404);
        if let Ok(output) = std::env::var("SHFE_VALIDATION_OUTPUT") {
            let report = json!({"checked_at":chrono::Utc::now(),"scope":"Rust versus current Python on identical captured official SHFE inputs; not live market acceptance",
                "master_count":all.len(),"products":23,"contracts":710,"contracts_pass":710,
                "expiry_analyses_pass":actual["expiries"].as_array().unwrap().len(),"missing_volume_preserved":missing,
                "trading_day":chain.trading_day,"result":"pass"});
            std::fs::write(output, serde_json::to_vec_pretty(&report).unwrap()).unwrap();
        }
    }
}
