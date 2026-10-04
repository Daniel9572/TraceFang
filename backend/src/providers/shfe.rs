//! Official SHFE option metadata and delayed quotes, with nullable source fields.
use chrono::{NaiveDate, NaiveDateTime, TimeZone, Timelike, Utc};
use chrono_tz::Asia::Shanghai;
use rust_decimal::prelude::ToPrimitive;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};
use tracefang_core::domain::{
    CoreError, CoreResult, Decimal, Timestamp, decimal_json, optional_decimal_json,
};

fn error(message: impl Into<String>) -> CoreError {
    CoreError(message.into())
}
fn text<'a>(row: &'a Value, key: &str) -> CoreResult<&'a str> {
    row.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .ok_or_else(|| error(format!("SHFE field {key} is missing")))
}
fn rows<'a>(payload: &'a Value, key: &str) -> CoreResult<&'a Vec<Value>> {
    payload
        .get(key)
        .and_then(Value::as_array)
        .ok_or_else(|| error(format!("SHFE array {key} is missing")))
}
fn number(row: &Value, key: &str) -> CoreResult<Option<Decimal>> {
    let value = match row.get(key) {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => return Ok(None),
        Some(Value::String(s)) => s.trim().to_owned(),
        Some(Value::Number(n)) => n.to_string(),
        _ => return Err(error(format!("SHFE {key} must be numeric"))),
    };
    Decimal::from_source_str(&value)
        .or_else(|_| Decimal::from_scientific(&value))
        .map(Some)
        .map_err(|_| error(format!("SHFE {key} is not a finite decimal")))
}
fn required_number(row: &Value, key: &str) -> CoreResult<Decimal> {
    number(row, key)?.ok_or_else(|| error(format!("SHFE {key} is missing")))
}
fn lots(row: &Value, key: &str) -> CoreResult<Option<u64>> {
    number(row, key)?
        .map(|n| {
            if n.fract() != Decimal::ZERO || n < Decimal::ZERO {
                return Err(error(format!("SHFE {key} must be nonnegative lots")));
            }
            n.to_u64()
                .ok_or_else(|| error(format!("SHFE {key} exceeds lot range")))
        })
        .transpose()
}
fn signed_lots(row: &Value, key: &str) -> CoreResult<Option<i64>> {
    number(row, key)?
        .map(|n| {
            if n.fract() != Decimal::ZERO {
                return Err(error(format!("SHFE {key} must be integral lots")));
            }
            n.to_i64()
                .ok_or_else(|| error(format!("SHFE {key} exceeds lot range")))
        })
        .transpose()
}
fn date(value: &str) -> CoreResult<NaiveDate> {
    if value.len() != 8 || !value.bytes().all(|b| b.is_ascii_digit()) {
        return Err(error("SHFE date must be YYYYMMDD"));
    }
    NaiveDate::parse_from_str(value, "%Y%m%d").map_err(|_| error("SHFE date is invalid"))
}
fn observed_at(row: &Value) -> CoreResult<Timestamp> {
    let local = NaiveDateTime::parse_from_str(text(row, "updatetime")?, "%Y-%m-%d %H:%M:%S")
        .map_err(|_| error("SHFE quote time is invalid"))?;
    Shanghai
        .from_local_datetime(&local)
        .single()
        .map(|v| v.with_timezone(&Utc))
        .ok_or_else(|| error("SHFE quote time is ambiguous"))
}
fn nonnegative(value: Option<Decimal>, field: &str) -> CoreResult<Option<Decimal>> {
    if value.as_ref().is_some_and(|v| v < &Decimal::ZERO) {
        Err(error(format!("SHFE {field} cannot be negative")))
    } else {
        Ok(value)
    }
}
fn validate_product(product: &str) -> CoreResult<()> {
    if product.is_empty() || product.len() > 6 || !product.bytes().all(|b| b.is_ascii_lowercase()) {
        Err(error("SHFE product code is invalid"))
    } else {
        Ok(())
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionContract {
    pub contract_id: String,
    pub underlying_contract_id: String,
    pub product: String,
    pub product_name: String,
    pub expiry: NaiveDate,
    #[serde(with = "decimal_json")]
    pub strike: Decimal,
    pub option_type: String,
    #[serde(with = "decimal_json")]
    pub contract_multiplier: Decimal,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionQuote {
    #[serde(flatten)]
    pub contract: OptionContract,
    #[serde(with = "optional_decimal_json")]
    pub bid: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub ask: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub last: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub previous_settlement: Option<Decimal>,
    #[serde(default,skip_serializing_if="Option::is_none",with="optional_decimal_json")]
    pub source_change:Option<Decimal>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub source_change_basis:Option<String>,
    #[serde(default,skip_serializing_if="Option::is_none")]
    pub source_change_matches_previous_settlement:Option<bool>,
    #[serde(with="tracefang_core::persistence_contract::optional_u64_string")]
    pub volume: Option<u64>,
    #[serde(with="tracefang_core::persistence_contract::optional_u64_string")]
    pub open_interest: Option<u64>,
    #[serde(with="tracefang_core::persistence_contract::optional_i64_string")]
    pub open_interest_change: Option<i64>,
    #[serde(with = "optional_decimal_json")]
    pub turnover: Option<Decimal>,
    pub observed_at: Timestamp,
    #[serde(with = "optional_decimal_json")]
    pub delta: Option<Decimal>,
    pub delta_as_of: Option<NaiveDate>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct UnderlyingQuote {
    pub contract_id: String,
    #[serde(with = "optional_decimal_json")]
    pub bid: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub ask: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub last: Option<Decimal>,
    #[serde(with = "optional_decimal_json")]
    pub previous_settlement: Option<Decimal>,
    #[serde(default,skip_serializing_if="Option::is_none",with="optional_decimal_json")]
    pub source_change:Option<Decimal>,
    #[serde(with="tracefang_core::persistence_contract::optional_u64_string")]
    pub volume: Option<u64>,
    #[serde(with="tracefang_core::persistence_contract::optional_u64_string")]
    pub open_interest: Option<u64>,
    pub observed_at: Timestamp,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OptionChain {
    pub provider_id: String,
    pub market_id: String,
    pub market_label: String,
    pub product: String,
    pub delivery_mode: String,
    pub trading_day: NaiveDate,
    pub reference_data_as_of: NaiveDate,
    pub observed_at: Timestamp,
    pub retrieved_at: Timestamp,
    pub quote_currency: String,
    pub price_unit: Option<String>,
    pub quotes: Vec<OptionQuote>,
    pub underlyings: BTreeMap<String, UnderlyingQuote>,
    pub reference_iv_by_underlying: BTreeMap<String, Decimal>,
    pub source_urls: Vec<String>,
    pub missing_contracts: Vec<String>,
    #[serde(default,with="tracefang_core::persistence_contract::u64_string")]
    pub metadata_contract_count:u64,
    #[serde(default)]
    pub unquoted_contract_ids:Vec<String>,
}

/// No AU-only filter: every master contract gets its own exchange-specified expiry/multiplier.
pub fn parse_contracts(payload: &Value) -> CoreResult<Vec<OptionContract>> {
    let mut contracts = Vec::new();
    let mut seen = BTreeSet::new();
    for row in rows(payload, "OptionContractBaseInfo")? {
        let code = text(row, "INSTRUMENTID")?;
        let product = text(row, "COMMODITYID")?;
        validate_product(product)?;
        let (index, kind) = code
            .char_indices()
            .find(|(_, c)| *c == 'C' || *c == 'P')
            .ok_or_else(|| error("SHFE option identifier is invalid"))?;
        let underlying = &code[..index];
        let delivery = underlying
            .strip_prefix(product)
            .ok_or_else(|| error("SHFE product and identifier differ"))?;
        if !(delivery.len() == 3 || delivery.len() == 4)
            || !delivery.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(error("SHFE underlying contract is invalid"));
        }
        let strike = Decimal::from_str_exact(&code[index + 1..])
            .map_err(|_| error("SHFE strike is invalid"))?;
        let multiplier = required_number(row, "TRADEUNIT")?;
        if strike <= Decimal::ZERO || multiplier <= Decimal::ZERO {
            return Err(error("SHFE strike/multiplier must be positive"));
        }
        if !seen.insert(code.to_owned()) {
            return Err(error("SHFE master repeats a contract"));
        }
        contracts.push(OptionContract {
            contract_id: code.into(),
            underlying_contract_id: underlying.into(),
            product: product.into(),
            product_name: text(row, "COMMODITYNAME")?.into(),
            expiry: date(text(row, "EXPIREDATE")?)?,
            strike,
            option_type: if kind == 'C' { "call" } else { "put" }.into(),
            contract_multiplier: multiplier,
        });
    }
    if contracts.is_empty() {
        return Err(error("SHFE master contains no options"));
    }
    Ok(contracts)
}

pub fn parse_chain(
    trading_day: &Value,
    option_payload: &Value,
    future_payload: &Value,
    master: &Value,
    daily: &Value,
    product: &str,
    received_at: Timestamp,
) -> CoreResult<OptionChain> {
    validate_product(product)?;
    let current_day = date(text(trading_day, "currentTradingday")?)?;
    let reference_date = date(text(trading_day, "lastTradingday")?)?;
    let contracts: BTreeMap<_, _> = parse_contracts(master)?
        .into_iter()
        .filter(|c| c.product == product)
        .map(|c| (c.contract_id.clone(), c))
        .collect();
    let product_name = contracts
        .values()
        .next()
        .ok_or_else(|| error("SHFE product has no contract metadata"))?
        .product_name
        .clone();
    let underlying_ids: BTreeSet<_> = contracts
        .values()
        .map(|c| c.underlying_contract_id.as_str())
        .collect();
    let mut deltas = BTreeMap::new();
    if let Some(rows) = daily.get("o_curinstrument").and_then(Value::as_array) {
        for row in rows {
            let Some(id) = row.get("INSTRUMENTID").and_then(Value::as_str) else {
                continue;
            };
            if !contracts.contains_key(id) {
                continue;
            }
            if let Some(delta) = number(row, "DELTA")? {
                let tolerance = Decimal::new(100001, 5);
                if delta.abs() <= tolerance {
                    deltas.insert(id.to_owned(), delta.clamp(-Decimal::ONE, Decimal::ONE));
                }
            }
        }
    }
    let mut reference_iv_by_underlying = BTreeMap::new();
    if let Some(rows) = daily.get("o_cursigma").and_then(Value::as_array) {
        for row in rows {
            if row.get("PRODUCTID").and_then(Value::as_str) != Some(&format!("{product}_o")) {
                continue;
            }
            if let Some(iv) = nonnegative(number(row, "SIGMA")?, "SIGMA")? {
                reference_iv_by_underlying.insert(text(row, "INSTRUMENTID")?.to_owned(), iv);
            }
        }
    }
    let mut underlyings = BTreeMap::new();
    for row in rows(future_payload, "delaymarket")? {
        let id = text(row, "contractname")?;
        if !underlying_ids.contains(id) {
            continue;
        }
        underlyings.insert(
            id.into(),
            UnderlyingQuote {
                contract_id: id.into(),
                bid: nonnegative(number(row, "bidprice")?, "bid")?,
                ask: nonnegative(number(row, "askprice")?, "ask")?,
                last: nonnegative(number(row, "lastprice")?, "last")?,
                previous_settlement: nonnegative(
                    number(row, "presettlementprice")?,
                    "previous_settlement",
                )?,
                source_change:number(row,"upperdown")?,
                volume: lots(row, "volume")?,
                open_interest: lots(row, "openinterest")?,
                observed_at: observed_at(row)?,
            },
        );
    }
    let mut quotes = Vec::new();
    let mut missing_contracts = Vec::new();
    let mut seen = BTreeSet::new();
    for row in rows(option_payload, "delaymarket")? {
        let id = text(row, "contractname")?;
        let Some(contract) = contracts.get(id) else {
            if row.get("instrumentid").and_then(Value::as_str) == Some(product)
                || id
                    .strip_prefix(product)
                    .is_some_and(|s| s.starts_with(|c: char| c.is_ascii_digit()))
            {
                missing_contracts.push(id.into());
            }
            continue;
        };
        if !seen.insert(id) {
            return Err(error("SHFE quote set repeats a contract"));
        }
        let delta = deltas.get(id).cloned();
        let last=nonnegative(number(row,"lastprice")?,"last")?;
        let previous_settlement=nonnegative(number(row,"presettlementprice")?,"previous_settlement")?;
        let source_change=number(row,"upperdown")?;
        let reference_consistent=last.as_ref().zip(previous_settlement.as_ref()).zip(source_change.as_ref()).map(|((last,previous),change)|last-previous==*change);
        quotes.push(OptionQuote {
            contract: contract.clone(),
            bid: nonnegative(number(row, "bidprice")?, "bid")?,
            ask: nonnegative(number(row, "askprice")?, "ask")?,
            last,previous_settlement,
            source_change_basis:source_change.as_ref().map(|_|"previous_settlement_reference; source upperdown retained independently".into()),
            source_change_matches_previous_settlement:reference_consistent,source_change,
            volume: lots(row, "volume")?,
            open_interest: lots(row, "openinterest")?,
            open_interest_change: signed_lots(row, "openinterestchg")?,
            turnover: nonnegative(number(row, "turnover")?, "turnover")?,
            observed_at: observed_at(row)?,
            delta:delta.clone(),
            delta_as_of: delta.as_ref().map(|_| reference_date),
        });
    }
    if quotes.is_empty() {
        return Err(error(
            "SHFE delayed feed contains no matching option quotes",
        ));
    }
    if missing_contracts.len() > 5.max(quotes.len() / 100) {
        return Err(error("SHFE quote/master sets are inconsistent"));
    }
    quotes.sort_by(|a, b| {
        (
            a.contract.expiry,
            &a.contract.strike,
            &a.contract.option_type,
        )
            .cmp(&(
                b.contract.expiry,
                &b.contract.strike,
                &b.contract.option_type,
            ))
    });
    let observed_at = quotes
        .iter()
        .map(|q| q.observed_at)
        .max()
        .expect("nonempty quotes checked");
    let local = observed_at.with_timezone(&Shanghai);
    let quote_day = if local.date_naive() <= reference_date && (6..18).contains(&local.hour()) {
        local.date_naive()
    } else {
        current_day
    };
    Ok(OptionChain {
        provider_id: "shfe_official_delayed".into(),
        market_id: if product == "au" {
            "shfe_gold_options".into()
        } else {
            format!("shfe_{product}_options")
        },
        market_label: format!("上海期货交易所{product_name}期权"),
        product: product.into(),
        delivery_mode: "exchange_delayed".into(),
        trading_day: quote_day,
        reference_data_as_of: reference_date,
        observed_at,
        retrieved_at: received_at,
        quote_currency: "CNY".into(),
        price_unit: if product == "au" {
            Some("CNY_PER_GRAM".into())
        } else {
            None
        },
        quotes,
        underlyings,
        reference_iv_by_underlying,
        source_urls: vec![],
        missing_contracts,
        metadata_contract_count:contracts.len().try_into().map_err(|_|error("SHFE metadata contract count overflow"))?,
        unquoted_contract_ids:contracts.keys().filter(|id|!seen.contains(id.as_str())).cloned().collect(),
    })
}

pub async fn fetch_json(client: &reqwest::Client, url: &str) -> CoreResult<Value> {
    let mut response=client
        .get(url)
        .timeout(std::time::Duration::from_secs(15))
        .header(
            "Referer",
            "https://www.shfe.com.cn/eng/reports/MarketData/DelayedQuotes/",
        )
        .send()
        .await
        .map_err(|e| error(format!("SHFE transport: {e}")))?
        .error_for_status()
        .map_err(|e| error(format!("SHFE response: {e}")))?;
    const LIMIT:usize=32*1024*1024;
    if response.content_length().is_some_and(|v|v>LIMIT as u64){return Err(error("SHFE response exceeds decoded body limit"))}
    let mut body=Vec::new();
    while let Some(chunk)=response.chunk().await.map_err(|e|error(format!("SHFE body: {e}")))? {
        if body.len()+chunk.len()>LIMIT{return Err(error("SHFE response exceeds decoded body limit"))}
        body.extend_from_slice(&chunk);
    }
    serde_json::from_slice(&body).map_err(|e|error(format!("SHFE JSON: {e}")))
}

/// Products present in the fixed official 6,276-contract master. Each endpoint
/// remains product scoped; metadata coverage does not stand in for quote coverage.
pub const OPTION_PRODUCTS:&[&str]=&["ad","ag","al","ao","au","bc","br","bu","cu","fu","hc","lu","ni","nr","op","pb","rb","ru","sc","sn","sp","ss","zn"];

pub async fn fetch_chain(
    client: &reqwest::Client,
    base_url: &str,
    product: &str,
) -> CoreResult<OptionChain> {
    validate_product(product)?;
    let base = base_url.trim_end_matches('/');
    let day = fetch_json(client, &format!("{base}/data/config/currentTradingday.dat")).await?;
    let current = date(text(&day, "currentTradingday")?)?;
    let previous = date(text(&day, "lastTradingday")?)?;
    let urls = vec![
        format!("{base}/data/tradedata/option/delaymarket/delaymarket_{product}Q.dat"),
        format!("{base}/data/tradedata/future/delaymarket/delaymarket_{product}.dat"),
        format!(
            "{base}/data/busiparamdata/option/ContractBaseInfo{}.dat",
            current.format("%Y%m%d")
        ),
        format!(
            "{base}/data/tradedata/option/dailydata/kx{}.dat",
            previous.format("%Y%m%d")
        ),
    ];
    let (options, futures, master, daily) = tokio::try_join!(
        fetch_json(client, &urls[0]),
        fetch_json(client, &urls[1]),
        fetch_json(client, &urls[2]),
        fetch_json(client, &urls[3])
    )?;
    let mut result = parse_chain(
        &day,
        &options,
        &futures,
        &master,
        &daily,
        product,
        Utc::now(),
    )?;
    result.source_urls = urls;
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    fn master() -> Value {
        json!({"OptionContractBaseInfo":[
        {"INSTRUMENTID":"au2611C900","COMMODITYID":"au","COMMODITYNAME":"黄金","EXPIREDATE":"20261026","TRADEUNIT":"1000"},
        {"INSTRUMENTID":"cu2611P70000","COMMODITYID":"cu","COMMODITYNAME":"铜","EXPIREDATE":"20261026","TRADEUNIT":"5"}]})
    }
    #[test]
    fn all_products_have_exact_metadata_and_unknown_lots_remain_unknown() {
        assert_eq!(parse_contracts(&master()).unwrap().len(), 2);
        let options = json!({"delaymarket":[{"contractname":"cu2611P70000","instrumentid":"cu","updatetime":"2026-09-30 15:25:21",
            "lastprice":"216.54","presettlementprice":"216.54","volume":"","openinterest":"0.0","openinterestchg":"-1","turnover":""}]});
        let chain = parse_chain(
            &json!({"currentTradingday":"20261008","lastTradingday":"20260930"}),
            &options,
            &json!({"delaymarket":[]}),
            &master(),
            &json!({}),
            "cu",
            Utc::now(),
        )
        .unwrap();
        assert_eq!(chain.quotes[0].volume, None);
        assert_eq!(chain.quotes[0].open_interest, Some(0));
        assert_eq!(chain.quotes[0].open_interest_change, Some(-1));
        assert_eq!(chain.trading_day.to_string(), "2026-09-30");
        assert_eq!(chain.price_unit, None);
        assert_eq!(
            chain.quotes[0].contract.contract_multiplier,
            Decimal::from(5)
        );
        let mut bad = options.clone();
        bad["delaymarket"][0]["updatetime"] = Value::Null;
        assert!(
            parse_chain(
                &json!({"currentTradingday":"20261008","lastTradingday":"20260930"}),
                &bad,
                &json!({"delaymarket":[]}),
                &master(),
                &json!({}),
                "cu",
                Utc::now()
            )
            .is_err()
        );
    }
}
