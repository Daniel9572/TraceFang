use super::ai::AnalyzeOptions;
use crate::{
    api::{ApiError, ApiResult, AppState},
    pages,
};
use anyhow::{Context, Result, anyhow, ensure};
use axum::{
    Json, Router,
    extract::{Path, Query, State},
    http::StatusCode,
    routing::{get, post},
};
use chrono::{DateTime, NaiveDate, NaiveDateTime, TimeZone, Utc};
use num_traits::ToPrimitive;
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, HashSet},
    sync::LazyLock,
    time::{Duration, Instant},
};
use tokio::sync::Mutex;
use tracefang_core::{
    domain::Decimal,
    events::{BarState, RealtimeBar},
    periods::Period,
};

static CACHE: LazyLock<Mutex<BTreeMap<String, (Instant, Value)>>> =
    LazyLock::new(|| Mutex::new(BTreeMap::new()));

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/api/expert/ai/status", get(ai_status))
        .route("/api/expert/ai/models", get(ai_models))
        .route("/api/expert/ai/analyze", post(ai_analyze))
        .route("/api/expert/events/gold", get(gold_events))
        .route("/api/expert/context/volatility", get(volatility))
        .route(
            "/api/expert/context/shfe-positioning/{product}",
            get(positioning),
        )
        .route(
            "/api/expert/context/multi-timeframe/{code}",
            get(multi_timeframe),
        )
}
async fn ai_status(State(s): State<AppState>) -> ApiResult {
    Ok(Json(s.ai.status().await))
}
async fn ai_models(State(s): State<AppState>) -> ApiResult {
    Ok(Json(
        json!({"models":s.ai.models().await.map_err(|e|ApiError(StatusCode::SERVICE_UNAVAILABLE,e.to_string()))?}),
    ))
}
#[derive(Deserialize)]
struct AiRequest {
    #[serde(default = "default_code")]
    code: String,
    #[serde(default = "default_period")]
    period: String,
    source_id: Option<String>,
    decision_as_of: Option<DateTime<Utc>>,
    #[serde(default,with="super::quant::optional_u64_text")] application_cursor:Option<u64>,
    parameters:Option<super::quant::Parameters>,
    expected_input_hash:Option<String>,
    #[serde(flatten)] options:AnalyzeOptions,
}
fn default_code() -> String {
    "XAUUSD".into()
}
fn default_period() -> String {
    "15m".into()
}
async fn ai_analyze(State(s): State<AppState>, Json(request): Json<AiRequest>) -> ApiResult {
    let mut parameters=request.parameters.unwrap_or_default();parameters.enabled_strategies=request.options.enabled_strategies.iter().cloned().collect();parameters.validate().map_err(|e|ApiError(StatusCode::UNPROCESSABLE_ENTITY,e.to_string()))?;
    let query=super::quant::QuantInputRequest{code:request.code,source_id:request.source_id,period:request.period,decision_as_of:request.decision_as_of,application_cursor:request.application_cursor,parameters,..Default::default()};
    let expected=request.expected_input_hash.as_deref().ok_or_else(||ApiError(StatusCode::UNPROCESSABLE_ENTITY,"请先取得服务端指标输入版本再请求分析".into()))?;
    let snapshot=super::service::authority_version(&query,expected).map_err(|e|ApiError(StatusCode::CONFLICT,e.to_string()))?;
    Ok(Json(s.ai.analyze(serde_json::to_value(snapshot).map_err(anyhow::Error::from)?,request.options).await.map_err(|e|ApiError(StatusCode::UNPROCESSABLE_ENTITY,e.to_string()))?))

}

#[derive(Deserialize, Default)]
struct EventQuery {
    start: Option<DateTime<Utc>>,
    end: Option<DateTime<Utc>>,
    as_of: Option<DateTime<Utc>>,
}
pub fn event_catalog(
    start: Option<DateTime<Utc>>,
    end: Option<DateTime<Utc>>,
    as_of: Option<DateTime<Utc>>,
) -> Result<Value> {
    ensure!(
        start.zip(end).is_none_or(|(a, b)| b > a),
        "end must be later than start"
    );
    let mut catalog: Value = serde_json::from_str(include_str!("../../assets/gold-events.json"))?;
    let facts = catalog["facts"]
        .as_array_mut()
        .context("invalid gold event catalog")?;
    facts.retain(|fact| {
        let marker = fact["marker_at"]
            .as_str()
            .and_then(|v| v.parse::<DateTime<Utc>>().ok());
        let published = fact["source_published_at"]
            .as_str()
            .and_then(|v| v.parse::<DateTime<Utc>>().ok());
        marker.is_some_and(|v| start.is_none_or(|at| v >= at) && end.is_none_or(|at| v < at))
            && published.is_some_and(|v| as_of.is_none_or(|at| v <= at))
    });
    catalog["generated_at"] = json!(Utc::now());
    Ok(catalog)
}
async fn gold_events(Query(q): Query<EventQuery>) -> ApiResult {
    Ok(Json(event_catalog(q.start, q.end, q.as_of).map_err(
        |e| ApiError(StatusCode::UNPROCESSABLE_ENTITY, e.to_string()),
    )?))
}
fn numeric(value: Decimal) -> Value {
    Value::String(value.to_string())
}
async fn persist_external(s:&AppState,kind:&str,provider:&str,value:&Value,observed:Option<DateTime<Utc>>,received:DateTime<Utc>,product:Option<&str>)->Result<()> {
    use tracefang_core::persistence_contract::{ExternalFactRecord,ExternalFactScope};
    let record_id=super::quant::content_hash(&(kind,provider,observed,received,value))?;
    let symbols=s.market.catalog.items.iter().filter(|d|{
        let base=d.instrument.base.as_deref();
        if product==Some("ag"){base==Some("XAG")||d.code.starts_with("AG")}else{base==Some("XAU")||d.code.starts_with("AU")}
    }).map(|d|d.instrument.symbol.clone()).collect::<std::collections::BTreeSet<_>>();
    let records=symbols.into_iter().map(|instrument_symbol|ExternalFactRecord{scope:ExternalFactScope{instrument_symbol,market_source_id:None},kind:kind.into(),source:provider.into(),record_id:record_id.clone(),revision:1,observed_at_ns:observed.and_then(|at|at.timestamp_nanos_opt()),published_at_ns:None,received_at_ns:received.timestamp_nanos_opt(),value:value.clone(),unavailable_reason:None,provenance:json!({"fetch_received_at":received,"source_published_at":"not supplied by provider","source_scope":"independent external context; not the selected price feed","calculation":"source decimal strings; disclosed model aggregates only","record_sha256":record_id,"observation_precision":if provider=="cboe_volatility"{"trading_date_only; timezone not supplied; observed_at_ns unknown"}else{"source stated clock"}})}).collect::<Vec<_>>();
    s.market.store.commit_external_facts(records).await?;Ok(())
}
pub fn volatility_csv(
    text: &str,
    code: &str,
    source_url: &str,
    received: DateTime<Utc>,
) -> Result<Value> {
    ensure!(
        ["VIX", "GVZ"].contains(&code),
        "unsupported volatility index"
    );
    let mut reader = csv::Reader::from_reader(text.trim_start_matches('\u{feff}').as_bytes());
    let headers = reader.headers()?.clone();
    let date_column = headers
        .iter()
        .position(|v| v.trim().eq_ignore_ascii_case("DATE"))
        .context("missing volatility date column")?;
    let name = if code == "VIX" { "CLOSE" } else { code };
    let value_column = headers
        .iter()
        .position(|v| v.trim().eq_ignore_ascii_case(name))
        .context("missing volatility value column")?;
    let mut rows = BTreeMap::new();
    for row in reader.records() {
        let row = row?;
        let date = row.get(date_column).unwrap_or("").trim();
        let value = row.get(value_column).unwrap_or("").trim();
        if date.is_empty() || value.is_empty() {
            continue;
        }
        let date = NaiveDate::parse_from_str(date, "%m/%d/%Y")?;
        let value = value.parse::<Decimal>()?;
        ensure!(value > Decimal::ZERO, "volatility value must be positive");
        rows.insert(date, value);
    }
    let values: Vec<_> = rows.into_iter().collect();
    let (date, last) = values.last().cloned().context("volatility history is empty")?;
    let sample = &values[values.len().saturating_sub(252)..];
    let rank = sample.iter().filter(|(_, v)| v <= &last).count();
    let percentile =
        (Decimal::from(rank) * Decimal::from(100) / Decimal::from(sample.len())).round_dp(2);
    Ok(
        json!({"index_code":code,"underlying":if code=="VIX"{"SPX"}else{"GLD"},"value":numeric(last),"as_of":date,
        "trailing_percentile_252":numeric(percentile),"history_sample_size":sample.len(),"history_start":sample[0].0,
        "history_end":date,"expected_horizon_days":30,"directional":false,
        "source":{"provider_id":"cboe_volatility","dataset_id":format!("{code}_History.csv"),"source_url":source_url,"frequency":"daily","received_at":received}}),
    )
}
async fn volatility(State(s): State<AppState>) -> ApiResult {
    if let Some((at, value)) = CACHE.lock().await.get("volatility") {
        if at.elapsed() < Duration::from_secs(21600) {
            return Ok(Json(value.clone()));
        }
    }
    let fetch = |code: &'static str| {
        let client = s.http.clone();
        async move {
            let url = format!(
                "https://cdn.cboe.com/api/global/us_indices/daily_prices/{code}_History.csv"
            );
            let text = client
                .get(&url)
                .timeout(Duration::from_secs(10))
                .send()
                .await?
                .error_for_status()?
                .text()
                .await?;
            volatility_csv(&text, code, &url, Utc::now())
        }
    };
    let (vix, gvz) = tokio::try_join!(fetch("VIX"), fetch("GVZ"))?;
    let value = json!({"contract_version":"volatility-eod-context-v1","state":"ready","mode":"eod","refresh_after_seconds":21600,
        "directional":false,"indices":[vix,gvz],"limitations":[
        "仅使用 Cboe 官方日频历史 CSV 的最近已发布值, 不是实时或盘中报价。",
        "滚动分位使用含最近值在内的最多 252 个已发布日值, 不提供价格方向预测。",
        "CSV 不提供精确发布时间; as_of 是交易日, received_at 是本服务获取时间。"]});
    let received=value["indices"].as_array().into_iter().flatten().filter_map(|v|v["source"]["received_at"].as_str()?.parse::<DateTime<Utc>>().ok()).max().context("volatility received time missing")?;
    persist_external(&s,"volatility","cboe_volatility",&value,None,received,None).await?;
    CACHE
        .lock()
        .await
        .insert("volatility".into(), (Instant::now(), value.clone()));
    Ok(Json(value))
}
fn decimal_field(row: &Value, key: &str) -> Result<Option<Decimal>> {
    match row.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => Ok(Some(s.trim().parse()?)),
        Some(Value::Number(n)) => Ok(Some(n.to_string().parse()?)),
        _ => Err(anyhow!("invalid numeric field {key}")),
    }
}
fn lots(row: &Value, key: &str, optional: bool) -> Result<Option<i64>> {
    let Some(value) = decimal_field(row, key)? else {
        ensure!(optional, "missing contract lots");
        return Ok(None);
    };
    ensure!(
        value.scale() == 0 && (optional || value >= Decimal::ZERO),
        "invalid contract lots"
    );
    Ok(Some(value.as_bigdecimal().to_i64().context("contract lots overflow")?))
}
pub fn positioning_payload(
    payload: &Value,
    product: &str,
    url: &str,
    received: DateTime<Utc>,
) -> Result<Value> {
    ensure!(
        ["au", "ag"].contains(&product),
        "unsupported SHFE positioning product"
    );
    let rows = payload["delaymarket"]
        .as_array()
        .context("SHFE positioning has no contract rows")?;
    let mut seen = HashSet::new();
    let mut contracts = Vec::new();
    let mut volume = 0_i64;
    let mut interest = 0_i64;
    let mut change = 0_i64;
    let mut change_count = 0;
    let mut latest = None;
    for row in rows {
        let code = row["contractname"]
            .as_str()
            .unwrap_or("")
            .trim()
            .to_lowercase();
        if code == "imci" {
            continue;
        }
        ensure!(
            code.len() == 6
                && code.starts_with(product)
                && code[2..].bytes().all(|v| v.is_ascii_digit()),
            "SHFE positioning returned a non-contract row"
        );
        ensure!(
            row["instrumentid"]
                .as_str()
                .is_some_and(|v| v.trim().eq_ignore_ascii_case(product)),
            "SHFE positioning returned another product"
        );
        ensure!(seen.insert(code.clone()), "duplicate SHFE contract");
        let row_volume = lots(row, "volume", false)?.unwrap();
        let row_interest = lots(row, "openinterest", false)?.unwrap();
        let row_change = lots(row, "openinterestchg", true)?;
        let price = decimal_field(row, "lastprice")?;
        ensure!(
            price.as_ref().is_none_or(|v| v > &Decimal::ZERO),
            "SHFE price must be positive"
        );
        let at = NaiveDateTime::parse_from_str(
            row["updatetime"]
                .as_str()
                .context("SHFE update time missing")?,
            "%Y-%m-%d %H:%M:%S",
        )?;
        let at = chrono_tz::Asia::Shanghai
            .from_local_datetime(&at)
            .single()
            .context("SHFE update time is invalid")?
            .with_timezone(&Utc);
        latest = Some(latest.map_or(at, |v: DateTime<Utc>| v.max(at)));
        volume = volume.checked_add(row_volume).context("volume overflow")?;
        interest = interest
            .checked_add(row_interest)
            .context("open interest overflow")?;
        if let Some(row_change) = row_change {
            change = change
                .checked_add(row_change)
                .context("open interest change overflow")?;
            change_count += 1;
        }
        contracts.push(json!({"product_code":product.to_uppercase(),"contract_code":code.to_uppercase(),"volume":row_volume.to_string(),
            "open_interest":row_interest.to_string(),"open_interest_change":row_change.map(|v|v.to_string()),"last_price":price.clone().map(numeric),"observed_at":at}));
    }
    ensure!(
        !contracts.is_empty(),
        "SHFE positioning response contains no real contracts"
    );
    contracts.sort_by(|a, b| {
        a["contract_code"]
            .as_str()
            .cmp(&b["contract_code"].as_str())
    });
    Ok(
        json!({"contract_version":"shfe-positioning-context-v1","state":"ready","mode":"delayed_snapshot","refresh_after_seconds":60,
        "as_of":latest,"delayed":true,"declared_delay_seconds":1800,"product_code":product.to_uppercase(),"contract_count":contracts.len(),
        "volume":volume.to_string(),"open_interest":interest.to_string(),"open_interest_change":if change_count==contracts.len(){Some(change.to_string())}else{None},
        "open_interest_change_contracts":change_count,"unit":"lots","counting_method":"single_side","directional_inference":"unavailable",
        "derived_aggregate":true,"contracts":contracts,"source":{"provider_id":"shfe_positioning","dataset_id":format!("delaymarket_{product}.dat"),
        "source_url":url,"observed_at":latest,"received_at":received,"published_at":null,"delayed":true,"declared_delay_seconds":1800},
        "limitations":["官方页面声明延迟 30 分钟; as_of 来自真实合约行中最新的更新时间。","成交量与持仓量按单边手数统计; 当前值是合约聚合, 不是交易所发布的加权指数。",
        "总持仓量不能辨别多空方向; 缺任一合约 ΔOI 时聚合变化保持 null。","中国交易日包含前一晚夜盘, 不能按自然日拆解量仓。"]}),
    )
}
async fn positioning(State(s): State<AppState>, Path(product): Path<String>) -> ApiResult {
    if !["au", "ag"].contains(&product.as_str()) {
        return Err(ApiError(
            StatusCode::UNPROCESSABLE_ENTITY,
            "不支持该持仓品种".into(),
        ));
    }
    let cache_key = format!("positioning:{product}");
    if let Some((at, value)) = CACHE.lock().await.get(&cache_key) {
        if at.elapsed() < Duration::from_secs(60) {
            return Ok(Json(value.clone()));
        }
    }
    let url = format!(
        "https://www.shfe.com.cn/data/tradedata/future/delaymarket/delaymarket_{product}.dat"
    );
    let payload = s
        .http
        .get(&url)
        .timeout(Duration::from_secs(10))
        .send()
        .await
        .map_err(anyhow::Error::from)?
        .error_for_status()
        .map_err(anyhow::Error::from)?
        .json::<Value>()
        .await
        .map_err(anyhow::Error::from)?;
    let received=Utc::now();let value = positioning_payload(&payload, &product, &url, received)?;
    let observed=value["as_of"].as_str().map(str::parse::<DateTime<Utc>>).transpose().map_err(anyhow::Error::from)?;
    persist_external(&s,"positioning","shfe_positioning",&value,observed,received,Some(&product)).await?;
    CACHE
        .lock()
        .await
        .insert(cache_key, (Instant::now(), value.clone()));
    Ok(Json(value))
}

fn bucket_end(bar: &RealtimeBar) -> Option<DateTime<Utc>> {
    if let Some(value) = bar.source.raw("bucket_end") {
        value
            .as_str()?
            .parse::<DateTime<Utc>>()
            .ok()
            .filter(|v| v > &bar.open_time)
    } else {
        bar.open_time
            .checked_add_signed(chrono::Duration::seconds(bar.interval_seconds))
    }
}
pub fn timeframe_summary(
    horizon: &str,
    period: &str,
    values: &[RealtimeBar],
    cutoff: DateTime<Utc>,
    scan_limit: bool,
) -> Value {
    let mut unique: BTreeMap<_, &RealtimeBar> = BTreeMap::new();
    for value in values {
        if unique.get(&value.open_time).is_none_or(|v| {
            (value.revision, value.source.received_at) > (v.revision, v.source.received_at)
        }) {
            unique.insert(value.open_time, value);
        }
    }
    let mut non_final = 0;
    let mut after = 0;
    let mut invalid = 0;
    let mut eligible = Vec::new();
    for value in unique.values() {
        if value.state != BarState::Final || value.finalized_at.is_none() {
            non_final += 1;
            continue;
        }
        let Some(end) = bucket_end(value) else {
            invalid += 1;
            continue;
        };
        let available = [
            end,
            value.finalized_at.unwrap(),
            value.source.observed_at,
            value.source.received_at,
        ]
        .into_iter()
        .max()
        .unwrap();
        if available > cutoff {
            after += 1;
            continue;
        }
        eligible.push((*value, available, end));
    }
    let window = &eligible[eligible.len().saturating_sub(20)..];
    let complete = window.len() >= 20;
    let prices_valid = window.iter().all(|(v, _, _)| v.close > Decimal::ZERO);
    let ready = complete && prices_valid;
    let fast = if window.len() >= 5
        && window[window.len() - 5..]
            .iter()
            .all(|(v, _, _)| v.close > Decimal::ZERO)
    {
        Some(
            window[window.len() - 5..]
                .iter()
                .map(|(v, _, _)| v.close.clone())
                .sum::<Decimal>()
                / Decimal::from(5),
        )
    } else {
        None
    };
    let slow =
        ready.then(|| window.iter().map(|(v, _, _)| v.close.clone()).sum::<Decimal>() / Decimal::from(20));
    let returns = ready.then(|| {
        (window.last().unwrap().0.close.clone() / window[0].0.close.clone() - Decimal::ONE) * Decimal::from(100)
    });
    let last = window.last();
    let direction = if let (Some(fast), Some(slow), Some(returns), Some((last, _, _))) =
        (fast.as_ref(), slow.as_ref(), returns.as_ref(), last)
    {
        if &last.close > fast && fast > slow && returns > &Decimal::ZERO {
            "up"
        } else if &last.close < fast && fast < slow && returns < &Decimal::ZERO {
            "down"
        } else {
            "mixed"
        }
    } else {
        "unavailable"
    };
    let limitation = if complete && !prices_valid {
        Some("non_positive_close_not_comparable".to_owned())
    } else if !ready {
        Some(if scan_limit {
            "history_scan_limit_before_required_sample".into()
        } else {
            format!("requires_20_final_bars_has_{}", window.len())
        })
    } else {
        None
    };
    json!({"horizon":horizon,"period_id":period,"state":if ready{"ready"}else if complete{"unavailable"}else{"insufficient_data"},"direction":direction,
        "required_final_bars":20,"loaded_bar_count":values.len(),"eligible_final_bar_count":eligible.len(),"used_bar_count":window.len(),
        "excluded_non_final_bars":non_final,"excluded_after_as_of_bars":after,"excluded_invalid_time_bars":invalid,
        "first_open_time":window.first().map(|(v,_,_)|v.open_time),"last_open_time":last.map(|(v,_,_)|v.open_time),
        "last_bucket_end":last.map(|(_,_,end)|end),"last_available_at":last.map(|(_,available,_)|available),
        "last_close":last.map(|(v,_,_)|numeric(v.close.clone())),"sma_fast":fast.map(numeric),"sma_slow":slow.map(numeric),"window_return_percent":returns.map(numeric),"limitation":limitation})
}
#[derive(Deserialize, Default)]
struct AsOfQuery {
    as_of: Option<DateTime<Utc>>,
}
async fn multi_timeframe(
    State(s): State<AppState>,
    Path(code): Path<String>,
    Query(query): Query<AsOfQuery>,
) -> ApiResult {
    let cutoff=query.as_of.unwrap_or_else(Utc::now);
    let input=crate::quant_input::context_only(&s,&super::quant::QuantInputRequest{code:code.clone(),period:"1m".into(),decision_as_of:Some(cutoff),..Default::default()}).await?;
    let fact=input.external_facts.iter().filter(|f|f.kind=="multi_timeframe"&&f.known_at().is_some_and(|at|at<=cutoff)).max_by_key(|f|f.known_at()).ok_or_else(||ApiError(StatusCode::SERVICE_UNAVAILABLE,"同版本没有可用的闭合多周期事实".into()))?;
    Ok(Json(super::snapshot::multi_timeframe_context(&input,fact,cutoff)))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn csv_percentile_includes_latest_and_sorts_dates() {
        let value = volatility_csv(
            "DATE,CLOSE\n01/02/2026,20\n01/01/2026,10\n01/03/2026,15\n",
            "VIX",
            "https://cdn.cboe.com/test",
            Utc::now(),
        )
        .unwrap();
        assert_eq!(value["value"], "15");
        assert_eq!(value["history_sample_size"], 3);
        assert_eq!(value["trailing_percentile_252"], "66.67");
        assert_eq!(value["directional"], false);
    }
    #[test]
    fn positioning_does_not_invent_missing_aggregate_change() {
        let row = |code: &str, change: Value| json!({"contractname":code,"instrumentid":"au","volume":10,"openinterest":20,"openinterestchg":change,"lastprice":"900","updatetime":"2026-10-02 15:00:00"});
        let value = positioning_payload(
            &json!({"delaymarket":[row("au2610",json!(2)),row("au2612",Value::Null)]}),
            "au",
            "https://www.shfe.com.cn/test",
            Utc::now(),
        )
        .unwrap();
        assert_eq!(value["volume"], "20");
        assert_eq!(value["open_interest"], "40");
        assert!(value["open_interest_change"].is_null());
        assert_eq!(value["directional_inference"], "unavailable");
    }
    #[test]
    fn gold_events_respect_publication_cutoff() {
        let at = "1900-01-01T00:00:00Z".parse().unwrap();
        assert!(
            event_catalog(None, None, Some(at)).unwrap()["facts"]
                .as_array()
                .unwrap()
                .is_empty()
        );
        assert!(event_catalog(Some(at), Some(at), None).is_err());
    }
    #[test]
    fn multi_timeframe_rejects_late_provisional_and_invalid_evidence() {
        use tracefang_core::domain::{AssetClass, Instrument, SourceMetadata};
        let start: DateTime<Utc> = "2026-10-01T00:00:00Z".parse().unwrap();
        let mut bars = (0..23)
            .map(|index| {
                let open = start + chrono::Duration::hours(index);
                let end = open + chrono::Duration::hours(1);
                RealtimeBar {
                    instrument: Instrument {
                        symbol: "XAU/USD".into(),
                        asset_class: AssetClass::Spot,
                        base: Some("XAU".into()),
                        quote: Some("USD".into()),
                        venue: None,
                    },
                    interval_seconds: 3600,
                    open_time: open,
                    open: Decimal::from(100 + index),
                    high: Decimal::from(101 + index),
                    low: Decimal::from(99 + index),
                    close: Decimal::from(100 + index),
                    volume: None,
                    source: SourceMetadata {
                        provider: "jin10".into(),
                        provider_symbol: "XAUUSD.GOODS".into(),
                        observed_at: end,
                        received_at: end,
                        raw_payload: None,
                    },
                    evidence_channel_id: "jin10_local".into(),
                    state: BarState::Final,
                    revision: 1,
                    finalized_at: Some(end),
                }
            })
            .collect::<Vec<_>>();
        let cutoff = start + chrono::Duration::days(1);
        bars[20].state = BarState::ProvisionalQuote;
        bars[21].source.received_at = cutoff + chrono::Duration::seconds(1);
        bars[22].source.raw_payload = Some(json!({"bucket_end":"not-a-timestamp"}));
        let summary = timeframe_summary("short", "1h", &bars, cutoff, false);
        assert_eq!(summary["state"], "ready");
        assert_eq!(summary["direction"], "up");
        assert_eq!(summary["eligible_final_bar_count"], 20);
        assert_eq!(summary["excluded_non_final_bars"], 1);
        assert_eq!(summary["excluded_after_as_of_bars"], 1);
        assert_eq!(summary["excluded_invalid_time_bars"], 1);
        let mut later = bars[0].clone();
        later.revision = 2;
        later.source.received_at = cutoff + chrono::Duration::seconds(1);
        bars.push(later);
        let revised = timeframe_summary("short", "1h", &bars, cutoff, false);
        assert_eq!(revised["state"], "insufficient_data");
        assert_eq!(revised["eligible_final_bar_count"], 19);
    }
}
