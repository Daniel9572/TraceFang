use super::{CONFIG, Query, Research, ResearchError, Result, normalize};
use chrono::{Duration as ChronoDuration, NaiveDate, Utc};
use chrono_tz::Asia::Shanghai;
use regex::Regex;
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    process::Stdio,
    time::{Duration, Instant},
};
use tokio::{io::{AsyncReadExt, AsyncWriteExt}, process::{Child, Command}};
use tracefang_core::domain::Decimal;

const MAX_WORKER_STDOUT: usize = 32 * 1024 * 1024;
fn worker_timeout() -> ResearchError { ResearchError::new(504, "AKShare 读取超时, 已停止本次采集。") }

async fn worker_output(child: &mut Child, request: &[u8], deadline: tokio::time::Instant, limit: usize) -> Result<Vec<u8>> {
    let result = tokio::time::timeout_at(deadline, async {
        child.stdin.take().ok_or_else(ResearchError::upstream)?.write_all(request).await.map_err(|_| ResearchError::upstream())?;
        let stdout = child.stdout.take().ok_or_else(ResearchError::upstream)?;
        let mut bytes = Vec::new();
        stdout.take((limit + 1) as u64).read_to_end(&mut bytes).await.map_err(|_| ResearchError::upstream())?;
        if bytes.len() > limit { return Err(ResearchError::upstream()); }
        if !child.wait().await.map_err(|_| ResearchError::upstream())?.success() { return Err(ResearchError::upstream()); }
        Ok(bytes)
    }).await.unwrap_or_else(|_| Err(worker_timeout()));
    if result.is_err() {
        // Reap before returning the limit/timeout error, including a blocked writer.
        let _ = child.start_kill();
        child.wait().await.map_err(|_| ResearchError::upstream())?;
    }
    result
}

pub(super) fn validate_exact_option_result(operation: &str, result: &Value) -> Result<()> {
    if operation == "daily_source" {
        let date = result["source_date"].as_str().ok_or_else(ResearchError::upstream)?;
        if NaiveDate::parse_from_str(date, "%Y-%m-%d").map_err(|_|ResearchError::upstream())?.to_string() != date
            || !["czce-option-daily","gfex-option-daily"].contains(&result["source_family"].as_str().unwrap_or(""))
            || result["precision_policy"] != "source-decimal-lexeme-v1"
            || !result["source_evidence"]["body_sha256"].is_string() { return Err(ResearchError::upstream()); }
        return Ok(());
    }
    if !["metadata", "chain"].contains(&operation) { return Ok(()); }
    if result["precision_policy"] != "source-decimal-lexeme-v1" { return Err(ResearchError::upstream()); }
    let contracts = result["contracts"].as_array().filter(|rows| rows.len() <= 30000)
        .ok_or_else(ResearchError::upstream)?;
    for row in contracts {
        for key in ["strike", "multiplier", "bid", "ask", "last", "volume", "open_interest", "source_change", "source_change_percent", "previous_close", "daily_close", "settlement", "previous_settlement", "daily_open", "daily_high", "daily_low", "turnover", "source_settlement_change"] {
            if let Some(value) = row.get(key).filter(|value| !value.is_null()) {
                let text = value.as_str().ok_or_else(ResearchError::upstream)?;
                Decimal::from_str_exact(text).map_err(|_| ResearchError::upstream())?;
            } else if ["strike", "multiplier"].contains(&key) { return Err(ResearchError::upstream()); }
        }
    }
    if result.get("reference_spot").is_some_and(|value| !value.is_null()) {
        let value = result["reference_spot"].as_str().ok_or_else(ResearchError::upstream)?;
        Decimal::from_str_exact(value).map_err(|_| ResearchError::upstream())?;
    }
    if operation == "chain" {
        if result["price_semantics"] == "official_daily_close" {
            let date = result["source_date"].as_str().ok_or_else(ResearchError::upstream)?;
            if NaiveDate::parse_from_str(date,"%Y-%m-%d").map_err(|_|ResearchError::upstream())?.to_string()!=date { return Err(ResearchError::upstream()); }
            for row in contracts {
                if !row["bid"].is_null() || !row["ask"].is_null() || !row["observed_at"].is_null() { return Err(ResearchError::upstream()); }
                if row.get("daily_close").is_some() && (row["source_date"] != date || row["observed_precision"] != "day") { return Err(ResearchError::upstream()); }
                if !row["last"].is_null() && row["last"] != row["daily_close"] { return Err(ResearchError::upstream()); }
            }
        }
        match result["reference_precision"].as_str() {
            Some("day") => {
                let day = result["reference_date"].as_str().ok_or_else(ResearchError::upstream)?;
                let parsed = NaiveDate::parse_from_str(day, "%Y-%m-%d").map_err(|_| ResearchError::upstream())?;
                if parsed.to_string() != day || result["reference_spot"].is_null() || !result["reference_observed_at"].is_null() { return Err(ResearchError::upstream()); }
            }
            Some("second") => {
                chrono::DateTime::parse_from_rfc3339(result["reference_observed_at"].as_str().ok_or_else(ResearchError::upstream)?).map_err(|_| ResearchError::upstream())?;
                if result["reference_spot"].is_null() || !result["reference_date"].is_null() { return Err(ResearchError::upstream()); }
            }
            Some("unknown") if result["reference_date"].is_null() && result["reference_observed_at"].is_null() => {},
            _ => return Err(ResearchError::upstream()),
        }
    }
    Ok(())
}

pub(super) struct FetchedBars { pub rows: Vec<Value>, pub provenance: Value }
pub(super) fn exact_bars_result(result: &Value) -> Result<FetchedBars> {
    if result["precision_policy"] != "source-decimal-lexeme-v1" { return Err(ResearchError::upstream()); }
    let eligible = result["temporal_authority_eligible"].as_bool().ok_or_else(ResearchError::upstream)?;
    let rows = result["bars"].as_array().ok_or_else(ResearchError::upstream)?;
    for row in rows {
        if !row["time"].is_string() || !row["source_payload"].is_object() { return Err(ResearchError::upstream()); }
        if eligible && ["span_start_unknown","span_end_unknown"].iter().any(|key| row["source_payload"][key].as_bool() == Some(true)) { return Err(ResearchError::upstream()); }
        for key in ["open", "high", "low", "close", "volume", "open_interest"] {
            if let Some(value) = row.get(key).filter(|value| !value.is_null()) {
                Decimal::from_str_exact(value.as_str().ok_or_else(ResearchError::upstream)?).map_err(|_| ResearchError::upstream())?;
            }
        }
    }
    let evidence = result["source_evidence"].as_array().filter(|proofs| !proofs.is_empty() && proofs.len() <= 4).ok_or_else(ResearchError::upstream)?;
    for proof in evidence {
        if !proof["url"].is_string() || proof["body_sha256"].as_str().is_none_or(|sha|sha.len()!=64 || !sha.bytes().all(|b|b.is_ascii_hexdigit())) || !proof["received_at"].is_string() { return Err(ResearchError::upstream()); }
    }
    Ok(FetchedBars { rows: rows.clone(), provenance: json!({"source_evidence":evidence,"precision_policy":result["precision_policy"],"mapping_version":result["mapping_version"],"source_response_state":result["source_response_state"],"temporal_authority_eligible":result["temporal_authority_eligible"],"time_policy":result["time_policy"]}) })
}

impl Research {
    fn endpoint(&self, key: &str, default: &str) -> String {
        self.0
            .environment
            .get(key)
            .cloned()
            .unwrap_or_else(|| default.into())
    }
    async fn http(
        &self,
        source: &str,
        request: reqwest::RequestBuilder,
    ) -> Result<reqwest::Response> {
        let mut gate = self
            .0
            .gates
            .get(source)
            .ok_or_else(|| ResearchError::invalid("来源无效。"))?
            .lock()
            .await;
        if let Some(last) = *gate {
            let delay = Duration::from_millis(350).saturating_sub(last.elapsed());
            tokio::time::sleep(delay).await;
        }
        *gate = Some(Instant::now());
        let request = request
            .timeout(Duration::from_secs(15))
            .build()
            .map_err(|_| ResearchError::upstream())?;
        for attempt in 0..2 {
            match self
                .0
                .http
                .execute(request.try_clone().ok_or_else(ResearchError::upstream)?)
                .await
            {
                Ok(response) => {
                    let status = response.status();
                    if status.is_server_error() && attempt == 0 {
                        tokio::time::sleep(Duration::from_millis(500)).await;
                        continue;
                    }
                    if status.as_u16() == 401 || status.as_u16() == 403 {
                        return Err(ResearchError::new(
                            403,
                            "来源拒绝访问: 请检查账户授权、数据权限或公开接口限制。",
                        ));
                    }
                    if status.as_u16() == 429 {
                        return Err(ResearchError::new(
                            429,
                            "来源请求额度已用尽或触发限流, 请稍后重试。",
                        ));
                    }
                    return if status.is_success() {
                        Ok(response)
                    } else {
                        Err(ResearchError::upstream())
                    };
                }
                Err(_) if attempt == 0 => tokio::time::sleep(Duration::from_millis(500)).await,
                Err(_) => return Err(ResearchError::upstream()),
            }
        }
        Err(ResearchError::upstream())
    }
    pub(super) async fn worker(&self, operation: &str, params: Value) -> Result<Value> {
        let operation = operation.to_owned();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(35);
        let mut gate = tokio::time::timeout_at(deadline, self.0.gates["akshare"].lock()).await.map_err(|_| worker_timeout())?;
        if let Some(last) = *gate {
            tokio::time::timeout_at(deadline, tokio::time::sleep(Duration::from_millis(350).saturating_sub(last.elapsed()))).await.map_err(|_| worker_timeout())?;
        }
        *gate = Some(Instant::now());
        let mut child = Command::new(&self.0.worker_python)
            .args(["-m", "tracefang.akshare_worker"])
            .current_dir(&self.0.worker_root)
            .env("PYTHONPATH", self.0.worker_root.join("src"))
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .kill_on_drop(true)
            .spawn()
            .map_err(|_| ResearchError::new(409, "AKShare 运行环境不可用。"))?;
        let request = serde_json::to_vec(&json!({"operation":operation,"params":params})).unwrap();
        let output = worker_output(&mut child, &request, deadline, MAX_WORKER_STDOUT).await?;
        let packet: Value =
            serde_json::from_slice(&output).map_err(|_| ResearchError::upstream())?;
        if packet.get("error").is_some() {
            return Err(ResearchError::upstream());
        }
        packet
            .get("result")
            .cloned()
            .ok_or_else(ResearchError::upstream)
    }
    pub(super) async fn fetch_bars(&self, query: &Query) -> Result<FetchedBars> {
        let end = normalize::history_end(query, Utc::now())
            .with_timezone(&Shanghai)
            .format("%Y%m%d")
            .to_string();
        let rows: Vec<Value> = match query.source.as_str() {
            "akshare" => {
                let mut params = serde_json::to_value(query).unwrap();
                params["end_date"] = json!(end);
                if Regex::new(r"^[A-Z]{1,3}\d{3}(?:-?[CP]-?\d+(?:\.\d+)?)?$")
                    .unwrap()
                    .is_match(&query.symbol)
                {
                    let metadata = self.resource("metadata", json!({}), 21600.0).await?;
                    let today = Utc::now().with_timezone(&Shanghai).date_naive().to_string();
                    let row = metadata["result"]["contracts"].as_array().and_then(|rows| {
                        rows.iter().find(|r| {
                            contract_key(
                                r[if query.asset == "option" {
                                    "symbol"
                                } else {
                                    "underlying"
                                }]
                                .as_str()
                                .unwrap_or(""),
                            ) == contract_key(&query.symbol)
                                && r["expiry"].as_str().unwrap_or("") >= today.as_str()
                        })
                    });
                    if metadata["cache_state"] == "stale" || row.is_none() {
                        return Err(ResearchError::invalid(
                            "三位月份代码无法核对年份; 请用两位年份的新浪代码, 如 SR2701。",
                        ));
                    }
                    let month = row.unwrap()["month"]
                        .as_str()
                        .ok_or_else(ResearchError::upstream)?;
                    params["contract_year"] = json!(
                        month
                            .get(..4)
                            .and_then(|v| v.parse::<i32>().ok())
                            .ok_or_else(ResearchError::upstream)?
                    );
                }
                return exact_bars_result(&self.worker("bars", params).await?);
            }
            "eastmoney" => {
                let (code, exchange) = query
                    .symbol
                    .split_once('.')
                    .ok_or_else(ResearchError::upstream)?;
                let klt = match query.period.as_str() {
                    "1d" => "101",
                    "1w" => "102",
                    _ => "103",
                };
                let fqt = match query.adjustment.as_str() {
                    "forward" => "1",
                    "backward" => "2",
                    _ => "0",
                };
                let params = [
                    (
                        "secid",
                        format!("{}.{}", if exchange == "SH" { 1 } else { 0 }, code),
                    ),
                    ("klt", klt.into()),
                    ("fqt", fqt.into()),
                    ("beg", "19900101".into()),
                    ("end", end),
                    ("lmt", (query.limit + 1).to_string()),
                    ("ut", "7eea3edcaed734bea9cbfc24409ed989".into()),
                    ("fields1", "f1,f2,f3,f4,f5,f6".into()),
                    ("fields2", "f51,f52,f53,f54,f55,f56".into()),
                ];
                let response = self
                    .http(
                        "eastmoney",
                        self.0
                            .http
                            .get(self.endpoint(
                                "TRACEFANG_RESEARCH_EASTMONEY_URL",
                                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                            ))
                            .query(&params)
                            .header("Referer", "https://quote.eastmoney.com/"),
                    )
                    .await?;
                let payload: Value = response
                    .json()
                    .await
                    .map_err(|_| ResearchError::upstream())?;
                if payload["rc"].as_i64().is_some_and(|v| v != 0) {
                    return Err(ResearchError::upstream());
                }
                Ok(payload["data"]["klines"]
                    .as_array()
                    .unwrap_or(&vec![])
                    .iter()
                    .map(|row| {
                        map_fields(
                            &["time", "open", "close", "high", "low", "volume"],
                            &row.as_str()
                                .unwrap_or("")
                                .split(',')
                                .map(|s| json!(s))
                                .collect::<Vec<_>>(),
                        )
                    })
                    .collect())
            }
            "tencent" => {
                let (code, exchange) = query
                    .symbol
                    .split_once('.')
                    .ok_or_else(ResearchError::upstream)?;
                let symbol = format!("{}{code}", exchange.to_lowercase());
                let period = match query.period.as_str() {
                    "1d" => "day",
                    "1w" => "week",
                    _ => "month",
                };
                let adjustment = match query.adjustment.as_str() {
                    "forward" => "qfq",
                    "backward" => "hfq",
                    _ => "",
                };
                let end = normalize::history_end(query, Utc::now())
                    .with_timezone(&Shanghai)
                    .format("%Y-%m-%d")
                    .to_string();
                let param = format!("{symbol},{period},,{end},{},{adjustment}", query.limit + 1);
                let payload:Value=self.http("tencent",self.0.http.get(self.endpoint("TRACEFANG_RESEARCH_TENCENT_URL","https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get")).query(&[("param",param)])).await?.json().await.map_err(|_|ResearchError::upstream())?;
                if payload["code"] != 0 {
                    return Err(ResearchError::upstream());
                }
                let data = &payload["data"][&symbol];
                let values = &data[format!("{adjustment}{period}")];
                if values.is_null() && !adjustment.is_empty() && data.get(period).is_some() {
                    return Err(ResearchError::invalid(
                        "来源未返回所选复权口径, 请显式切换为不复权。",
                    ));
                }
                Ok(values
                    .as_array()
                    .unwrap_or(&vec![])
                    .iter()
                    .map(|row| {
                        map_fields(
                            &["time", "open", "close", "high", "low", "volume"],
                            row.as_array().unwrap_or(&vec![]),
                        )
                    })
                    .collect())
            }
            "sina" => {
                let raw=self.http("sina",self.0.http.get(self.endpoint("TRACEFANG_RESEARCH_SINA_URL","https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_tracefang=/InnerFuturesNewService.getDailyKLine")).query(&[("symbol",&query.symbol)]).header("Referer","https://finance.sina.com.cn/")).await?.text().await.map_err(|_|ResearchError::upstream())?;
                let start = raw.find('[').ok_or_else(ResearchError::upstream)?;
                let end = raw
                    .rfind(']')
                    .filter(|n| *n >= start)
                    .ok_or_else(ResearchError::upstream)?;
                let rows: Vec<Value> = serde_json::from_str(&raw[start..=end])
                    .map_err(|_| ResearchError::upstream())?;
                Ok(rows.iter().map(|r|json!({"time":r["d"],"open":r["o"],"high":r["h"],"low":r["l"],"close":r["c"],"volume":r["v"]})).collect())
            }
            "tushare" => {
                let api = match query.asset.as_str() {
                    "equity" => "daily",
                    "etf" => "fund_daily",
                    "index" => "index_daily",
                    "future" => "fut_daily",
                    _ => "opt_daily",
                };
                let values = self
                    .tushare(
                        api,
                        json!({"ts_code":query.symbol,"end_date":end,"limit":query.limit+1}),
                        "ts_code,trade_date,open,high,low,close,vol",
                    )
                    .await?;
                values.iter().map(|r|{let day=NaiveDate::parse_from_str(r["trade_date"].as_str().ok_or_else(ResearchError::upstream)?,"%Y%m%d").map_err(|_|ResearchError::upstream())?;
                    Ok(json!({"time":day,"open":r["open"],"high":r["high"],"low":r["low"],"close":r["close"],"volume":r["vol"]}))}).collect()
            }
            "alpaca" => self.alpaca_bars(query).await,
            _ => Err(ResearchError::invalid("来源无效。")),
        }?;
        Ok(FetchedBars { rows, provenance: Value::Null })
    }
    async fn tushare(&self, api: &str, params: Value, fields: &str) -> Result<Vec<Value>> {
        let token = self
            .0
            .environment
            .get("TUSHARE_TOKEN")
            .filter(|v| !v.is_empty())
            .ok_or_else(|| ResearchError::new(409, "请配置 TUSHARE_TOKEN 后读取。"))?;
        let payload: Value = self
            .http(
                "tushare",
                self.0
                    .http
                    .post(
                        self.endpoint("TRACEFANG_RESEARCH_TUSHARE_URL", "https://api.tushare.pro"),
                    )
                    .json(&json!({"api_name":api,"token":token,"params":params,"fields":fields})),
            )
            .await?
            .json()
            .await
            .map_err(|_| ResearchError::upstream())?;
        if payload["code"] != 0 {
            return Err(ResearchError::new(
                403,
                "Tushare 请求被拒绝, 请检查 Token、接口积分权限和调用频率。",
            ));
        }
        let fields: Vec<_> = payload["data"]["fields"]
            .as_array()
            .ok_or_else(ResearchError::upstream)?
            .iter()
            .map(|v| v.as_str().ok_or_else(ResearchError::upstream))
            .collect::<Result<_>>()?;
        Ok(payload["data"]["items"]
            .as_array()
            .ok_or_else(ResearchError::upstream)?
            .iter()
            .map(|row| map_fields(&fields, row.as_array().unwrap_or(&vec![])))
            .collect())
    }
    fn alpaca_request(&self, url: String) -> Result<reqwest::RequestBuilder> {
        let key = self
            .0
            .environment
            .get("ALPACA_API_KEY")
            .filter(|v| !v.is_empty())
            .ok_or_else(|| ResearchError::new(409, "Alpaca 尚未配置。"))?;
        let secret = self
            .0
            .environment
            .get("ALPACA_SECRET_KEY")
            .filter(|v| !v.is_empty())
            .ok_or_else(|| ResearchError::new(409, "Alpaca 尚未配置。"))?;
        Ok(self
            .0
            .http
            .get(url)
            .header("APCA-API-KEY-ID", key)
            .header("APCA-API-SECRET-KEY", secret))
    }
    async fn alpaca_bars(&self, query: &Query) -> Result<Vec<Value>> {
        let option = query.asset == "option";
        let endpoint = if option {
            "v1beta1/options/bars"
        } else {
            "v2/stocks/bars"
        };
        let period = match query.period.as_str() {
            "1m" => "1Min",
            "5m" => "5Min",
            "15m" => "15Min",
            "30m" => "30Min",
            "1h" => "1Hour",
            "1d" => "1Day",
            "1w" => "1Week",
            _ => "1Month",
        };
        let end = query
            .before
            .unwrap_or_else(|| Utc::now() - ChronoDuration::minutes(if option { 16 } else { 0 }))
            - ChronoDuration::microseconds(1);
        let mut params = BTreeMap::from([
            ("symbols", query.symbol.clone()),
            ("timeframe", period.into()),
            (
                "start",
                if option {
                    "2024-02-01T00:00:00Z"
                } else {
                    "2016-01-01T00:00:00Z"
                }
                .into(),
            ),
            ("end", end.to_rfc3339()),
            ("sort", "desc".into()),
            ("limit", (query.limit + 1).to_string()),
        ]);
        if !option {
            params.insert("feed", "iex".into());
            params.insert("adjustment", "raw".into());
        }
        let mut result = Vec::new();
        let mut seen = BTreeSet::new();
        let mut finished = false;
        for _ in 0..8 {
            let base = self.endpoint(
                "TRACEFANG_RESEARCH_ALPACA_URL",
                "https://data.alpaca.markets",
            );
            let payload: Value = self
                .http(
                    "alpaca",
                    self.alpaca_request(format!("{base}/{endpoint}"))?
                        .query(&params),
                )
                .await?
                .json()
                .await
                .map_err(|_| ResearchError::upstream())?;
            if let Some(rows) = payload["bars"][&query.symbol].as_array() {
                result.extend(rows.iter().cloned());
            }
            let token = payload["next_page_token"]
                .as_str()
                .filter(|s| !s.is_empty());
            if token.is_none() || result.len() > query.limit {
                finished = true;
                break;
            }
            let token = token.unwrap();
            if !seen.insert(token.to_owned()) {
                return Err(ResearchError::upstream());
            }
            params.insert("page_token", token.into());
        }
        if !finished {
            return Err(ResearchError::new(
                502,
                "来源分页超过本次读取上限, 请缩小请求。",
            ));
        }
        Ok(result.iter().map(|r|json!({"time":r["t"],"open":r["o"],"high":r["h"],"low":r["l"],"close":r["c"],"volume":r["v"]})).collect())
    }
    pub(super) async fn contracts(
        &self,
        asset: &str,
        exchange: &str,
        source: &str,
    ) -> Result<Value> {
        if ![
            "SSE", "SZSE", "BSE", "SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX",
        ]
        .contains(&exchange)
        {
            return Err(ResearchError::invalid("交易所无效。"));
        }
        if source == "akshare" {
            if !["future", "option"].contains(&asset) || exchange == "BSE" {
                return Err(ResearchError::invalid(
                    "AKShare 目录支持指定交易所的期权及其期货标的。",
                ));
            }
            let page = self.resource("metadata", json!({}), 21600.0).await?;
            if page["cache_state"] == "stale" {
                return Err(ResearchError::new(502, "合约元数据读取失败, 请稍后重试。"));
            }
            let today = Utc::now().with_timezone(&Shanghai).date_naive().to_string();
            let mut directory = BTreeMap::new();
            for row in page["result"]["contracts"]
                .as_array()
                .ok_or_else(ResearchError::upstream)?
            {
                if row["exchange"] != exchange
                    || row["expiry"].as_str().unwrap_or("") < today.as_str()
                {
                    continue;
                }
                if asset == "future" && ["SSE", "SZSE", "CFFEX"].contains(&exchange) {
                    continue;
                }
                let symbol = row[if asset == "option" {
                    "symbol"
                } else {
                    "underlying"
                }]
                .as_str()
                .ok_or_else(ResearchError::upstream)?;
                directory.insert(symbol.to_owned(),json!({"source":"akshare","asset":asset,"symbol":symbol,"name":if asset=="option"{row["name"].clone()}else{json!(symbol)},"currency":"CNY","expiry":if asset=="option"{row["expiry"].clone()}else{Value::Null}}));
            }
            return Ok(Value::Array(directory.into_values().collect()));
        }
        if source != "tushare" || !["equity", "future", "option"].contains(&asset) {
            return Err(ResearchError::invalid("目录来源或类别无效。"));
        }
        let api = match asset {
            "equity" => "stock_basic",
            "future" => "fut_basic",
            _ => "opt_basic",
        };
        let rows = self
            .tushare(api, json!({"exchange":exchange,"limit":6000}), "")
            .await?;
        Ok(Value::Array(rows.into_iter().filter(|r|r["ts_code"].is_string()).map(|r|json!({"source":"tushare","asset":asset,"symbol":r["ts_code"],"name":r.get("name").unwrap_or(&r["ts_code"]),"currency":"CNY","expiry":r.get("maturity_date").or_else(||r.get("delist_date"))})).collect()))
    }
    pub(super) async fn months(&self, symbol: &str) -> Result<Value> {
        if !CONFIG["underlyings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|s| s["symbol"] == symbol)
        {
            return Err(ResearchError::invalid(
                "尚未支持该期权标的, 请从标的列表选择。",
            ));
        }
        let page = self.resource("metadata", json!({}), 21600.0).await?;
        if page["cache_state"] == "stale" {
            return Err(ResearchError::new(
                502,
                "合约元数据更新失败, 暂不据旧目录加载期权链。",
            ));
        }
        let today = Utc::now().with_timezone(&Shanghai).date_naive().to_string();
        let mut months = BTreeMap::new();
        let pattern = Regex::new(&format!("^{}\\d{{3,4}}$", regex::escape(symbol))).unwrap();
        let contracts: Vec<_> = page["result"]["contracts"]
            .as_array()
            .ok_or_else(ResearchError::upstream)?
            .iter()
            .filter(|row| {
                (row["underlying"] == symbol
                    || pattern.is_match(row["underlying"].as_str().unwrap_or("")))
                    && row["expiry"].as_str().unwrap_or("") >= today.as_str()
            })
            .cloned()
            .collect();
        for row in &contracts {
            let month = row["month"].as_str().ok_or_else(ResearchError::upstream)?;
            if month.len() != 6 {
                return Err(ResearchError::upstream());
            }
            months.insert(month.to_owned(),json!({"month":month,"expiry":row["expiry"],"label":format!("{}-{} · 到期 {}",&month[..4],&month[4..],row["expiry"].as_str().unwrap_or(""))}));
        }
        Ok(
            json!({"symbol":symbol,"months":months.into_values().collect::<Vec<_>>(),"fetched_at":page["fetched_at"],"contracts":contracts,"metadata_evidence":page["result"]["source_evidence"]}),
        )
    }
    pub(super) async fn option_chain(&self, symbol: &str, month: &str, report_date: Option<&str>) -> Result<Value> {
        if month.len() != 6 || NaiveDate::parse_from_str(&format!("{month}01"), "%Y%m%d").is_err() {
            return Err(ResearchError::invalid("合约月份无效。"));
        }
        let directory = self.months(symbol).await?;
        let contracts: Vec<_> = directory["contracts"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["month"] == month)
            .cloned()
            .collect();
        if contracts.is_empty() {
            return Err(ResearchError::new(
                404,
                "该标的月份没有有效合约, 请重新读取合约月份。",
            ));
        }
        let spec = CONFIG["underlyings"].as_array().unwrap().iter().find(|row|row["symbol"]==symbol).ok_or_else(ResearchError::upstream)?;
        let mut params = json!({"symbol":symbol,"month":month,"contracts":contracts,"metadata_evidence":directory["metadata_evidence"]});
        if ["czce-option-daily","gfex-option-daily"].contains(&spec["quote_source"].as_str().unwrap_or("")) {
            let date = report_date.unwrap_or("2026-09-30");
            let parsed = NaiveDate::parse_from_str(date,"%Y-%m-%d").map_err(|_|ResearchError::invalid("报告日期须为 YYYY-MM-DD；不会自动选择其他交易日。"))?;
            if parsed.to_string()!=date { return Err(ResearchError::invalid("报告日期无效。")); }
            // Historical report identity is source+requested date. Share its original body across products/months.
            let source = self.resource("daily_source",json!({"source_family":spec["quote_source"],"report_date":date}),300.0).await?;
            if source["cache_state"]=="stale" || source["result"]["source_date"]!=date { return Err(ResearchError::new(502,"所选日期的官方报告不可用；未替换为其他日期。")); }
            params["report_date"] = json!(date);
            params["daily_source_packet"] = source["result"].clone();
        } else if report_date.is_some() { return Err(ResearchError::invalid("该来源不是官方日行情，不能应用报告日期。")); }
        let page = self
            .resource(
                "chain",
                params,
                30.0,
            )
            .await?;
        let mut result = page["result"].clone();
        result["cache_state"] = page["cache_state"].clone();
        if result["price_semantics"] == "official_daily_close" { result["read_at"] = page["fetched_at"].clone(); }
        else { result["fetched_at"] = page["fetched_at"].clone(); }
        if page["cache_state"] == "stale" {
            if let Some(warnings) = result["warnings"].as_array_mut() {
                warnings.push(json!("上游读取失败, 当前为旧缓存; 暂不能导入报价。"));
            }
        }
        Ok(result)
    }
    pub(super) async fn alpaca_options(&self, symbol: &str, expiry: Option<&str>) -> Result<Value> {
        self.validate(&Query {
            source: "alpaca".into(),
            symbol: symbol.into(),
            ..Query::default()
        })
        .await?;
        if !Regex::new(r"^[A-Z][A-Z.]{0,8}$").unwrap().is_match(symbol) {
            return Err(ResearchError::invalid("请输入美股标的代码, 例如 SPY。"));
        }
        let mut params =
            BTreeMap::from([("feed", "indicative".to_owned()), ("limit", "1000".into())]);
        if let Some(expiry) = expiry {
            NaiveDate::parse_from_str(expiry, "%Y-%m-%d")
                .map_err(|_| ResearchError::invalid("到期日格式无效。"))?;
            params.insert("expiration_date", expiry.into());
        }
        let mut result = Vec::new();
        let mut seen = BTreeSet::new();
        let mut truncated = false;
        let pattern = Regex::new(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$").unwrap();
        for _ in 0..5 {
            let base = self.endpoint(
                "TRACEFANG_RESEARCH_ALPACA_URL",
                "https://data.alpaca.markets",
            );
            let payload: Value = self
                .http(
                    "alpaca",
                    self.alpaca_request(format!("{base}/v1beta1/options/snapshots/{symbol}"))?
                        .query(&params),
                )
                .await?
                .json()
                .await
                .map_err(|_| ResearchError::upstream())?;
            if let Some(rows) = payload["snapshots"].as_object() {
                for (code, row) in rows {
                    let Some(fields) = pattern.captures(code) else {
                        continue;
                    };
                    let expiry = NaiveDate::parse_from_str(&fields[2], "%y%m%d")
                        .map_err(|_| ResearchError::upstream())?;
                    let strike = Decimal::from_str_exact(&fields[4])
                        .map_err(|_| ResearchError::upstream())?
                        / Decimal::from(1000);
                    let quote = &row["latestQuote"];
                    let trade = &row["latestTrade"];
                    result.push(json!({"symbol":code,"underlying":symbol,"expiry":expiry,"kind":if &fields[3]=="C"{"call"}else{"put"},
                    "strike":normalize::numeric(Some(strike)),"bid":normalize::numeric(normalize::decimal(&quote["bp"])),"ask":normalize::numeric(normalize::decimal(&quote["ap"])),"last":normalize::numeric(normalize::decimal(&trade["p"])),
                    "observed_at":quote.get("t").or_else(||trade.get("t")),"iv":normalize::numeric(normalize::decimal(&row["impliedVolatility"])),
                    "greeks":row["greeks"].as_object().map(|values|values.iter().map(|(k,v)|(k.clone(),normalize::numeric(normalize::decimal(v)))).collect::<BTreeMap<_,_>>()).unwrap_or_default()}));
                }
            }
            let token = payload["next_page_token"]
                .as_str()
                .filter(|s| !s.is_empty());
            truncated = token.is_some();
            let Some(token) = token else { break };
            if !seen.insert(token.to_owned()) {
                return Err(ResearchError::upstream());
            }
            params.insert("page_token", token.into());
        }
        result.sort_by(|a, b| {
            (
                a["expiry"].as_str(),
                normalize::decimal(&a["strike"]),
                a["kind"].as_str(),
            )
                .cmp(&(
                    b["expiry"].as_str(),
                    normalize::decimal(&b["strike"]),
                    b["kind"].as_str(),
                ))
        });
        Ok(
            json!({"source":"alpaca","feed":"indicative","underlying":symbol,"fetched_at":Utc::now(),"truncated":truncated,
            "contracts":result,"note":"指示性延迟行情; 仅返回已读取的合约, 非可成交承诺。标准美股乘数通常为100, 请核对调整合约。"}),
        )
    }
}
fn map_fields(fields: &[&str], values: &[Value]) -> Value {
    if values.len() < fields.len() {
        return json!({});
    }
    Value::Object(
        fields
            .iter()
            .zip(values)
            .map(|(key, value)| ((*key).into(), value.clone()))
            .collect(),
    )
}
fn contract_key(value: &str) -> String {
    value
        .to_uppercase()
        .trim_end_matches(".SH")
        .trim_end_matches(".SZ")
        .chars()
        .filter(|c| !c.is_whitespace() && *c != '-')
        .collect()
}

#[cfg(test)]
mod exact_option_result_tests {
    use super::*;
    #[test]
    fn worker_source_prices_and_metadata_must_be_decimal_strings() {
        let mut packet=json!({"precision_policy":"source-decimal-lexeme-v1","contracts":[{"strike":"9007199254740993.0000000000000000000000000001","multiplier":"10000","bid":null,"ask":"0.0000000000000000000000000001","last":"0","volume":"0","open_interest":"18446744073709551615","source_change":"-0.0001"}],"reference_spot":null,"reference_precision":"unknown","reference_date":null,"reference_observed_at":null});
        validate_exact_option_result("chain",&packet).unwrap();
        packet["contracts"][0]["ask"]=json!(0.1);
        assert!(validate_exact_option_result("chain",&packet).is_err());
        packet["contracts"][0]["ask"]=Value::Null;
        packet["contracts"][0]["strike"]=json!(900);
        assert!(validate_exact_option_result("metadata",&packet).is_err());
        packet["contracts"][0]["strike"]=json!("900");
        packet["reference_spot"]=json!(950.1);
        assert!(validate_exact_option_result("chain",&packet).is_err());
        packet["reference_spot"]=json!("950.1000");
        validate_exact_option_result("chain",&packet).unwrap();
        packet["reference_precision"]=json!("day");packet["reference_date"]=json!("None");
        assert!(validate_exact_option_result("chain",&packet).is_err());
        packet["reference_date"]=json!("2030-09-30");
        validate_exact_option_result("chain",&packet).unwrap();
        packet["precision_policy"]=Value::Null;
        assert!(validate_exact_option_result("metadata",&packet).is_err(),"old rounded worker/cache cannot claim exact source input");
    }
    #[test]
    fn exact_bar_wrapper_retains_response_evidence_for_empty_or_rejected_first_row() {
        let proof=json!({"url":"https://source.test/bars","body_sha256":"0".repeat(64),"received_at":"2030-09-30T08:00:00Z","body_base64":"W10="});
        let mut packet=json!({"precision_policy":"source-decimal-lexeme-v1","bars":[],"source_evidence":[proof],"source_response_state":"empty_response; retention_floor_not_proven","mapping_version":"fixed","temporal_authority_eligible":true,"time_policy":{"clock_policy_verified":false}});
        let empty=exact_bars_result(&packet).unwrap();assert!(empty.rows.is_empty());assert_eq!(empty.provenance["source_evidence"],packet["source_evidence"]);
        packet["bars"]=json!([{"time":"2030-09-30","open":null,"high":"2","low":"0","close":"1","volume":"0","source_payload":{"source_label":"2030-09-30"}},{"time":"2030-09-29","open":"9007199254740993.0000000000000000000000000001","high":"9007199254740993.0000000000000000000000000001","low":"9007199254740993.0000000000000000000000000001","close":"9007199254740993.0000000000000000000000000001","volume":null,"source_payload":{"clock_policy_verified":false}}]);
        let fetched=exact_bars_result(&packet).unwrap();assert_eq!(fetched.provenance,empty.provenance);
        let normalized=normalize::bars(&fetched.rows,&Query{source:"akshare".into(),symbol:"600519.SH".into(),..Default::default()},chrono::DateTime::parse_from_rfc3339("2030-10-01T00:00:00Z").unwrap().with_timezone(&Utc));assert_eq!(normalized.rejected,1);assert_eq!(normalized.bars.len(),1);assert_eq!(normalized.bars[0]["close"],packet["bars"][1]["close"]);assert_eq!(normalized.bars[0]["source"]["raw_payload"]["clock_policy_verified"],false);
        packet["bars"][1]["close"]=json!(0.1);assert!(exact_bars_result(&packet).is_err());packet["bars"]=json!([]);packet["source_evidence"]=json!([]);assert!(exact_bars_result(&packet).is_err());
    }
    #[test]
    fn unknown_minute_span_keeps_label_but_cannot_become_executable_input() {
        let mut packet=json!({"precision_policy":"source-decimal-lexeme-v1","temporal_authority_eligible":false,"bars":[{"time":"2030-09-30 15:00:00","open":"1.0000000000000000000000000001","high":"2","low":"1","close":"1.5","volume":"0","source_payload":{"source_label":"2030-09-30 15:00:00","span_start_unknown":true,"span_end_unknown":true,"clock_policy_verified":false}}],"source_evidence":[{"url":"https://source.test/minute","body_sha256":"0".repeat(64),"received_at":"2030-09-30T08:00:00Z"}]});
        let fetched=exact_bars_result(&packet).unwrap();
        let normalized=normalize::bars(&fetched.rows,&Query{source:"akshare".into(),symbol:"AU0".into(),asset:"future".into(),period:"1h".into(),..Default::default()},"2030-10-01T00:00:00Z".parse().unwrap());
        assert_eq!(normalized.bars.len(),1);assert_eq!(normalized.bars[0]["open_time"],"2030-09-30T07:00:00+00:00");
        assert_eq!(normalized.bars[0]["state"],"provisional_authoritative");assert!(normalized.bars[0]["bucket_end"].is_null());
        assert_eq!(normalized.bars[0]["source"]["raw_payload"]["source_label"],"2030-09-30 15:00:00");assert!(normalize::quant_bars(&normalized.bars).is_err());
        packet["temporal_authority_eligible"]=json!(true);assert!(exact_bars_result(&packet).is_err());
        packet["temporal_authority_eligible"]=Value::Null;assert!(exact_bars_result(&packet).is_err());
    }
    fn python_worker(script: &str) -> Child {
        Command::new(std::env::var("TRACEFANG_TEST_WORKER_PYTHON").unwrap_or_else(|_| "python3".into())).args(["-c",script]).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).kill_on_drop(true).spawn().unwrap()
    }
    #[tokio::test]
    async fn stdout_limit_kills_and_reaps_a_blocked_child() {
        let mut child=python_worker("import sys,time;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*4096);sys.stdout.buffer.flush();time.sleep(60)");
        let result=worker_output(&mut child,b"{}",tokio::time::Instant::now()+Duration::from_secs(5),128).await;
        assert!(result.is_err());assert!(child.id().is_none(),"oversized worker must be reaped before error returns");
    }
    #[tokio::test]
    async fn timeout_kills_and_reaps_without_waiting_for_child_exit() {
        let mut child=python_worker("import sys,time;sys.stdin.buffer.read();time.sleep(60)");
        let start=Instant::now();let result=worker_output(&mut child,b"{}",tokio::time::Instant::now()+Duration::from_millis(150),128).await;
        assert_eq!(result.unwrap_err().status.as_u16(),504);assert!(child.id().is_none());assert!(start.elapsed()<Duration::from_secs(3));
    }
    #[tokio::test]
    async fn normal_worker_keeps_utf8_and_source_decimal_bytes_while_stderr_is_discarded() {
        let body=r#"{"source":"原始价格","price":"9007199254740993.0000000000000000000000000001","volume":"0","missing":null}"#;
        let script=format!("import sys;sys.stdin.buffer.read();sys.stderr.write('e'*2097152);sys.stdout.buffer.write({}.encode('utf-8'))",serde_json::to_string(body).unwrap());
        let mut child=python_worker(&script);let bytes=worker_output(&mut child,b"{}",tokio::time::Instant::now()+Duration::from_secs(5),body.len()).await.unwrap();
        assert_eq!(bytes,body.as_bytes());assert!(child.id().is_none());
    }
}
