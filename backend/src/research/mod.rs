//! Native research queries, durable same-query cache, and cancellable analysis jobs.
mod normalize;
mod providers;
mod source_period;
use crate::{
    analysis::ai::{AiService, AnalyzeOptions},
    api::AppState,
};
use axum::{
    Json, Router,
    extract::{Path, Query as UrlQuery, State},
    http::StatusCode,
    response::{IntoResponse, Response},
    routing::{get, post},
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, HashMap},
    path::PathBuf,
    sync::{Arc, LazyLock},
    time::{Duration, Instant},
};
use tokio::{
    process::Command,
    sync::{Mutex, OnceCell, watch,Semaphore},
};

static CONFIG: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("config.json")).expect("valid research catalog")
});
static MERGE_LIMIT:LazyLock<Arc<Semaphore>>=LazyLock::new(||Arc::new(Semaphore::new(2)));
#[derive(Debug, Clone)]
pub struct ResearchError {
    pub status: StatusCode,
    pub detail: String,
}
pub type Result<T> = std::result::Result<T, ResearchError>;
impl ResearchError {
    pub fn new(status: u16, detail: impl Into<String>) -> Self {
        Self {
            status: StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY),
            detail: detail.into(),
        }
    }
    pub fn invalid(detail: impl Into<String>) -> Self {
        Self::new(422, detail)
    }
    pub fn upstream() -> Self {
        Self::new(502, "来源连接或数据格式异常, 请稍后重试。")
    }
}
impl IntoResponse for ResearchError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({"detail":self.detail}))).into_response()
    }
}
impl std::fmt::Display for ResearchError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.detail)
    }
}
impl std::error::Error for ResearchError {}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct Query {
    pub source: String,
    pub symbol: String,
    pub asset: String,
    pub period: String,
    pub adjustment: String,
    pub before: Option<DateTime<Utc>>,
    pub limit: usize,
}
impl Default for Query {
    fn default() -> Self {
        Self {
            source: String::new(),
            symbol: String::new(),
            asset: "equity".into(),
            period: "1d".into(),
            adjustment: "raw".into(),
            before: None,
            limit: 300,
        }
    }
}
type Pending = watch::Receiver<Option<Result<Value>>>;
struct Jobs {
    rows: BTreeMap<String, Value>,
    active: Option<(String, watch::Sender<bool>)>,
}
struct Inner {
    http: reqwest::Client,
    cache_dir: PathBuf,
    worker_python: PathBuf,
    worker_root: PathBuf,
    environment: BTreeMap<String, String>,
    inflight: Mutex<HashMap<String, Pending>>,
    gates: HashMap<String, Mutex<Option<Instant>>>,
    diagnostics: Mutex<BTreeMap<String, Value>>,
    akshare_available: OnceCell<bool>,
    jobs: Mutex<Jobs>,
}
#[derive(Clone)]
pub struct Research(Arc<Inner>);
impl Research {
    pub fn new(
        http: reqwest::Client,
        cache_dir: PathBuf,
        worker_python: PathBuf,
        worker_root: PathBuf,
    ) -> Self {
        let gates = CONFIG["sources"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| (s["id"].as_str().unwrap().into(), Mutex::new(None)))
            .collect();
        Self(Arc::new(Inner {
            http,
            cache_dir,
            worker_python,
            worker_root,
            environment: std::env::vars().collect(),
            inflight: Mutex::new(HashMap::new()),
            gates,
            diagnostics: Mutex::new(BTreeMap::new()),
            akshare_available: OnceCell::new(),
            jobs: Mutex::new(Jobs {
                rows: BTreeMap::new(),
                active: None,
            }),
        }))
    }
    pub async fn sources(&self) -> Value {
        let available=*self.0.akshare_available.get_or_init(||async {
            let mut cmd=Command::new(&self.0.worker_python);cmd.args(["-c","import importlib.util; raise SystemExit(0 if importlib.util.find_spec('akshare') else 1)"]).kill_on_drop(true);
            tokio::time::timeout(Duration::from_secs(5),cmd.output()).await.ok().and_then(|r|r.ok()).is_some_and(|r|r.status.success())
        }).await;
        let diagnostics = self.0.diagnostics.lock().await;
        Value::Array(
            CONFIG["sources"]
                .as_array()
                .unwrap()
                .iter()
                .cloned()
                .map(|mut spec| {
                    let id = spec["id"].as_str().unwrap().to_owned();
                    spec["configured"] = json!(
                        spec["credentials"].as_array().unwrap().iter().all(|v| self
                            .0
                            .environment
                            .get(v.as_str().unwrap())
                            .is_some_and(|v| !v.trim().is_empty()))
                            && (id != "akshare" || available)
                    );
                    spec["diagnostic"] = diagnostics.get(&id).cloned().unwrap_or(Value::Null);
                    spec
                })
                .collect(),
        )
    }
    async fn validate(&self, query: &Query) -> Result<()> {
        if !(1..=1000).contains(&query.limit) || query.symbol.len() > 40 || query.symbol.is_empty()
        {
            return Err(ResearchError::invalid("证券代码或读取条数无效。"));
        }
        let sources = self.sources().await;
        let spec = sources
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == query.source)
            .ok_or_else(|| ResearchError::invalid("来源无效。"))?;
        if spec["configured"] != true {
            return Err(ResearchError::new(
                409,
                format!(
                    "{} 尚未配置或运行依赖未安装。",
                    spec["name"].as_str().unwrap_or("来源")
                ),
            ));
        }
        let periods = spec["asset_periods"]
            .get(&query.asset)
            .unwrap_or(&spec["periods"]);
        if !spec["assets"]
            .as_array()
            .unwrap()
            .contains(&json!(query.asset))
            || !periods.as_array().unwrap().contains(&json!(query.period))
        {
            return Err(ResearchError::invalid(
                "该来源不支持此资产或周期, 请切换来源或周期。",
            ));
        }
        if query.source == "tencent" && query.limit > 639 {
            return Err(ResearchError::invalid(
                "腾讯行情每页最多 639 根, 请使用分页读取。",
            ));
        }
        if !["raw", "forward", "backward"].contains(&query.adjustment.as_str()) {
            return Err(ResearchError::invalid("复权方式无效。"));
        }
        if query.adjustment != "raw"
            && (!["eastmoney", "tencent", "akshare"].contains(&query.source.as_str())
                || !["equity", "etf"].contains(&query.asset.as_str()))
        {
            return Err(ResearchError::invalid("此来源/资产仅支持不复权研究。"));
        }
        let pattern = match query.source.as_str() {
            "eastmoney" => r"\d{6}\.(SH|SZ|BJ)",
            "tencent" => r"\d{6}\.(SH|SZ)",
            "sina" => r"[A-Z]{1,3}\d{1,4}",
            "tushare" => r"[A-Z0-9-]{1,28}\.(SH|SZ|BJ|SHF|DCE|CZC|CFX|INE|GFE)",
            "alpaca" => r"[A-Z][A-Z0-9.]{0,29}",
            "akshare" if ["equity", "etf"].contains(&query.asset.as_str()) => r"\d{6}\.(SH|SZ|BJ)",
            "akshare" if query.asset == "future" => r"[A-Z]{1,3}\d{1,4}",
            _ => r"(?:[19]\d{7}(?:\.(?:SH|SZ))?|[A-Z]{1,3}\d{3,4}-?[CP]-?\d+(?:\.\d+)?)",
        };
        if !regex::Regex::new(&format!("^{pattern}$"))
            .unwrap()
            .is_match(&query.symbol)
        {
            return Err(ResearchError::invalid(
                "证券代码格式不正确, 请参考来源旁的示例。",
            ));
        }
        Ok(())
    }
    async fn cached(&self, key: &str) -> Option<Value> {
        let bytes = tokio::fs::read(self.0.cache_dir.join(format!("{key}.json")))
            .await
            .ok()?;
        serde_json::from_slice(&bytes).ok()
    }
    async fn save(&self, key: &str, value: &Value) -> std::io::Result<()> {
        tokio::fs::create_dir_all(&self.0.cache_dir).await?;
        let temporary = self
            .0
            .cache_dir
            .join(format!("{key}-{}.tmp", uuid::Uuid::new_v4().simple()));
        tokio::fs::write(&temporary, serde_json::to_vec(value)?).await?;
        tokio::fs::rename(&temporary, self.0.cache_dir.join(format!("{key}.json"))).await?;
        let mut directory = tokio::fs::read_dir(&self.0.cache_dir).await?;
        let mut entries = Vec::new();
        while let Some(entry) = directory.next_entry().await? {
            if entry.path().extension().is_some_and(|s| s == "json") {
                if let Ok(meta) = entry.metadata().await {
                    entries.push((meta.modified()?, entry.path()));
                }
            }
        }
        entries.sort_by_key(|(time, _)| *time);
        let remove = entries.len().saturating_sub(256);
        for (_, path) in entries.into_iter().take(remove) {
            let _ = tokio::fs::remove_file(path).await;
        }
        Ok(())
    }
    fn authority_dir(&self)->PathBuf{self.0.cache_dir.join("authority")}
    async fn merge_authority(&self,request:MergeAuthority)->Result<Value>{let _permit=MERGE_LIMIT.clone().try_acquire_owned().map_err(|_|ResearchError::new(429,"研究历史合并正在进行，请稍后重试。"))?;let root=self.authority_dir();let authority=tokio::task::spawn_blocking(move||crate::analysis::research_input::merge(&root,&request.base_snapshot_id,&request.additional_snapshot_id)).await.map_err(|_|ResearchError::upstream())?.map_err(|error|ResearchError::invalid(error.to_string()))?;Ok(json!({"authority_snapshot_id":authority.id,"authority_manifest":authority.manifest}))}
    pub async fn scan_authority<F>(&self,request:&crate::analysis::quant::QuantInputRequest,batch_rows:usize,on_batch:F)->anyhow::Result<tracefang_core::persistence_contract::CanonicalScanSummary> where F:FnMut(crate::analysis::quant::QuantInput)->anyhow::Result<()>+Send+'static{let root=self.authority_dir();let request=request.clone();tokio::task::spawn_blocking(move||crate::analysis::research_input::scan(&root,&request,batch_rows,on_batch)).await?}
    pub async fn bars(&self, mut query: Query, refresh: bool) -> Result<Value> {
        query.symbol = query.symbol.to_uppercase();
        self.validate(&query).await?;
        let key = hex::encode(Sha256::digest(format!(
            "native-research-source-lexeme-v2:{}",
            serde_json::to_string(&query).unwrap()
        )));
        let cached = self.cached(&key).await;
        let ttl = if query.period.ends_with('m') || query.period.ends_with('h') {
            30.0
        } else {
            300.0
        };
        if !refresh {
            if let Some(mut cached) = cached.clone() {
                if now_seconds() - cached["cached_at"].as_f64().unwrap_or(0.0) < ttl {
                    cached["cache_state"] = json!("cached");
                    return Ok(cached);
                }
            }
        }
        let mut inflight = self.0.inflight.lock().await;
        let mut receiver = if let Some(receiver) = inflight.get(&key) {
            receiver.clone()
        } else {
            if inflight.len() >= 24 {
                return Err(ResearchError::new(429, "数据请求较多, 请稍后重试。"));
            }
            let (sender, receiver) = watch::channel(None);
            inflight.insert(key.clone(), receiver.clone());
            let service = self.clone();
            let task_key = key.clone();
            tokio::spawn(async move {
                let result = service.load(query, &task_key, cached).await;
                sender.send_replace(Some(result));
                service.0.inflight.lock().await.remove(&task_key);
            });
            receiver
        };
        drop(inflight);
        loop {
            if let Some(result) = receiver.borrow().clone() {
                return result;
            }
            receiver
                .changed()
                .await
                .map_err(|_| ResearchError::upstream())?;
        }
    }
    async fn load(&self, query: Query, key: &str, cached: Option<Value>) -> Result<Value> {
        let fetched = tokio::time::timeout(Duration::from_secs(35), self.fetch_bars(&query))
            .await
            .map_err(|_| ResearchError::new(504, "来源读取超时, 请稍后重试。"));
        let result = match fetched {
            Ok(value) => value,
            Err(error) => Err(error),
        };
        let result = match result {
            Ok(fetched) => {
                let rows = fetched.rows;
                let source_provenance = fetched.provenance;
                let now = Utc::now();
                let normalized = normalize::bars(&rows, &query, now);
                if !normalized.conflicts.is_empty() {
                    self.0.diagnostics.lock().await.insert(query.source.clone(),json!({"state":"input_conflict","checked_at":now,"conflicts":normalized.conflicts}));
                    return Err(ResearchError::new(422,format!("来源同一时间返回 {} 组冲突行情，缺少修订顺序证据，本次未发布权威版本。",normalized.conflicts.len())));
                }
                let mut bars=normalized.bars; let rejected=normalized.rejected;
                if bars.is_empty() && rejected > 0 {
                    Err(ResearchError::upstream())
                } else {
                    let has_more = bars.len() > query.limit;
                    let mut warnings = Vec::new();
                    if normalized.deduplicated>0 {warnings.push(format!("已去重 {} 条内容完全相同的重复行情。",normalized.deduplicated));}
                    if ["1w","1M"].contains(&query.period.as_str()){warnings.push("保留来源周/月日期标签；来源起点或期末标签口径尚未核验，结束边界按本地日历解释，不代表精确发布时刻。".into());}
                    if rejected > 0 {
                        warnings.push(format!("已排除 {rejected} 条无效行情。"));
                    }
                    if query.asset == "future" && query.symbol.ends_with('0') {
                        warnings.push(
                            "连续期货是观察序列; 换月可能产生跳空, 不代表可交易合约回报。".into(),
                        );
                    }
                    if query.adjustment == "raw"
                        && ["equity", "etf"].contains(&query.asset.as_str())
                    {
                        warnings.push("不复权价格可能包含除权除息跳空。".into());
                    }
                    if query.source == "alpaca" {
                        warnings.push(
                            if query.asset == "option" {
                                "期权历史至少延迟 15 分钟, 数据权限依账户而定。"
                            } else {
                                "IEX 仅覆盖单交易所。"
                            }
                            .into(),
                        );
                    }
                    if query.source == "akshare" {
                        warnings.push(
                            "AKShare 是采集适配器; 原始来源为东方财富或新浪, 非授权实时专线。"
                                .into(),
                        );
                        if query.asset == "future" && query.period != "1d" {
                            warnings.push(
                                "分钟线仅覆盖上游近期窗口; 分页耗尽不代表上市以来的历史已完整。"
                                    .into(),
                            );
                        }
                        if query.asset == "option" {
                            warnings.push(
                                "期权无成交日可能缺少 K 线; 读取成功不代表最新交易日。".into(),
                            );
                        }
                    }
                    let volume_unit = "来源原值（单位未核实）";
                    let feed = if query.source == "alpaca" && query.asset != "option" {
                        "IEX"
                    } else if query.source == "akshare"
                        && ["equity", "etf"].contains(&query.asset.as_str())
                    {
                        "AKShare / 东方财富"
                    } else if query.source == "akshare" {
                        "AKShare / 新浪"
                    } else {
                        &query.source
                    };
                    let temporal_eligible = !(query.source == "akshare" && query.asset == "future" && query.period != "1d")
                        && source_provenance["temporal_authority_eligible"].as_bool().unwrap_or(query.source != "akshare");
                    let mut time_semantics = normalize::time_policy(&query);
                    if let Some(policy) = source_provenance["time_policy"].as_object() { time_semantics.as_object_mut().unwrap().extend(policy.clone()); }
                    let authority = if temporal_eligible {
                    let quant_rows=normalize::quant_bars(&bars).map_err(|_|ResearchError::upstream())?;
                    let manifest=crate::analysis::research_input::ResearchManifest{authority:String::new(),scope:crate::analysis::research_input::ResearchScope{source:query.source.clone(),symbol:query.symbol.clone(),asset:query.asset.clone(),period:query.period.clone(),adjustment:query.adjustment.clone()},query:serde_json::to_value(&query).unwrap(),feed:feed.into(),fetched_at:now,first_open:None,last_open:None,row_count:0,canonical_sha256:String::new(),canonical_bytes:0,upstream_payload_sha256:crate::analysis::quant::content_hash(&rows).map_err(|_|ResearchError::upstream())?,rejected_rows:rejected as u64,parser_version:"research-normalize-exact-v3-conflict-rejection".into(),calendar_version:"source-label-local-boundary-v2".into(),precision_policy:if rows.iter().any(|row|["open","high","low","close","volume"].iter().any(|key|row[key].is_number())){"source_adapter_numeric_precision_only; normalization does not restore upstream lost digits"}else{"source_adapter_decimal_strings; upstream provenance governs original precision"}.into(),pagination_evidence:json!({"before":query.before,"source_returned_rows":rows.len(),"available_normalized_rows":bars.len(),"has_more_than_display":has_more,"display_limit":query.limit,"deduplicated_rows":normalized.deduplicated,"time_semantics":time_semantics,"source_response":source_provenance}),warmup_complete:false,coverage_reason:"available source response window; history before first row and full listing coverage not proven".into(),unit:format!("{} / instrument price unit",if query.source=="alpaca"{"USD"}else{"CNY"})};
                    let authority_root=self.authority_dir();
                    Some(tokio::task::spawn_blocking(move||crate::analysis::research_input::publish(&authority_root,manifest,&quant_rows)).await.map_err(|_|ResearchError::upstream())?.map_err(|_|ResearchError::new(507,"研究输入无法持久发布，本次不产生可分析版本。"))?)
                    } else { None };
                    if has_more{bars.drain(..bars.len()-query.limit);}
                    Ok(
                        json!({"authority_snapshot_id":authority.as_ref().map(|a|&a.id),"authority_manifest":authority.as_ref().map(|a|&a.manifest),"authority_unavailable_reason":if temporal_eligible{None}else{Some("该来源未提供可核实的分钟区间，目前仅展示和下载原始价格序列，暂不能用于精确指标、AI、回放或模拟。")},"source_response_evidence":if temporal_eligible{Value::Null}else{source_provenance},"query":query,"next_before":if has_more{bars.first().map(|b|b["open_time"].clone())}else{None},
                    "data_as_of":bars.last().map(|b|b["open_time"].clone()),"empty_reason":if bars.is_empty(){Some("该范围没有行情。请检查代码、上市/到期日、来源权限或向前查询。")}else{None},
                    "items":bars,"cache_state":"fresh","cached_at":now_seconds(),"fetched_at":now,"rejected_rows":rejected,"deduplicated_rows":normalized.deduplicated,"time_semantics":time_semantics,
                    "warnings":warnings,"volume_unit":volume_unit,"currency":if query.source=="alpaca"{"USD"}else{"CNY"},"feed":feed,
                    "frequency":if query.period.ends_with('m')||query.period.ends_with('h'){"分钟快照"}else if query.source=="alpaca"{"历史快照"}else{"日频研究"}}),
                    )
                }
            }
            Err(error) => Err(error),
        };
        match result {
            Ok(mut payload) => {
                if self.save(key, &payload).await.is_err() {
                    payload["warnings"]
                        .as_array_mut()
                        .unwrap()
                        .push(json!("本机缓存写入失败, 本次数据仅在内存显示。"));
                }
                self.0.diagnostics.lock().await.insert(query.source,json!({"state":"ok","checked_at":Utc::now(),"detail":format!("最近读取 {} 根 K 线",payload["items"].as_array().unwrap().len())}));
                Ok(payload)
            }
            Err(error) => {
                self.0.diagnostics.lock().await.insert(
                    query.source,
                    json!({"state":"error","checked_at":Utc::now(),"detail":error.detail}),
                );
                if let Some(mut payload) = cached {
                    payload["cache_state"] = json!("stale");
                    if let Some(warnings) = payload["warnings"].as_array_mut() {
                        warnings.push(json!(format!("{} 当前展示同源旧缓存。", error.detail)));
                    }
                    Ok(payload)
                } else {
                    Err(error)
                }
            }
        }
    }
    async fn resource(&self, operation: &str, params: Value, ttl: f64) -> Result<Value> {
        let key = hex::encode(Sha256::digest(format!("native-ak-source-decimal-v4:{operation}:{params}")));
        let cached = self.cached(&key).await;
        if let Some(mut cached) = cached.clone() {
            if now_seconds() - cached["cached_at"].as_f64().unwrap_or(0.0) < ttl {
                cached["cache_state"] = json!("cached");
                return Ok(cached);
            }
        }
        let mut inflight = self.0.inflight.lock().await;
        let mut receiver = if let Some(receiver) = inflight.get(&key) {
            receiver.clone()
        } else {
            if inflight.len() >= 24 {
                return Err(ResearchError::new(429, "数据请求较多, 请稍后重试。"));
            }
            let (sender, receiver) = watch::channel(None);
            inflight.insert(key.clone(), receiver.clone());
            let service = self.clone();
            let task_key = key.clone();
            let operation = operation.to_owned();
            tokio::spawn(async move {
                let result = service
                    .load_resource(&operation, params, &task_key, cached)
                    .await;
                sender.send_replace(Some(result));
                service.0.inflight.lock().await.remove(&task_key);
            });
            receiver
        };
        drop(inflight);
        loop {
            if let Some(result) = receiver.borrow().clone() {
                return result;
            }
            receiver
                .changed()
                .await
                .map_err(|_| ResearchError::upstream())?;
        }
    }
    async fn load_resource(
        &self,
        operation: &str,
        params: Value,
        key: &str,
        cached: Option<Value>,
    ) -> Result<Value> {
        match self.worker(operation, params).await {
            Ok(result) => {
                providers::validate_exact_option_result(operation, &result)?;
                let payload = json!({"result":result,"cached_at":now_seconds(),"fetched_at":Utc::now(),"cache_state":"fresh"});
                let _ = self.save(&key, &payload).await;
                Ok(payload)
            }
            Err(error) => {
                if let Some(mut cached) = cached {
                    cached["cache_state"] = json!("stale");
                    Ok(cached)
                } else {
                    Err(error)
                }
            }
        }
    }
}
fn now_seconds() -> f64 {
    Utc::now().timestamp_millis() as f64 / 1000.0
}

#[derive(Clone, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AnalysisRequest {
    query: Query,
    question: String,
    model: Option<String>,
    reasoning_effort: Option<String>,
    research_snapshot_id:Option<String>,
    expected_input_hash:Option<String>,
    parameters:Option<crate::analysis::quant::Parameters>,
}
impl Default for AnalysisRequest {
    fn default() -> Self {
        Self {
            query: Query::default(),
            question: "分析趋势、关键价位和数据局限, 给出看多、看空和观望三种条件。".into(),
            model: None,
            reasoning_effort: None,
            research_snapshot_id:None,expected_input_hash:None,parameters:None,
        }
    }
}
impl Research {
    async fn start_analysis(&self, request: AnalysisRequest, ai: Arc<AiService>) -> Result<Value> {
        if request.question.chars().count() > 8000
            || request
                .model
                .as_ref()
                .is_some_and(|v| v.is_empty() || v.len() > 128)
            || request
                .reasoning_effort
                .as_ref()
                .is_some_and(|v| v.len() > 32)
        {
            return Err(ResearchError::invalid("AI 请求长度超出限制。"));
        }
        self.validate(&request.query).await?;
        let mut jobs = self.0.jobs.lock().await;
        if jobs.active.is_some() {
            return Err(ResearchError::new(
                409,
                "已有 AI 分析正在执行, 请先取消或等待完成。",
            ));
        }
        jobs.rows
            .retain(|_, row| now_seconds() - row["created_at"].as_f64().unwrap_or(0.0) < 1800.0);
        while jobs.rows.len() >= 20 {
            let oldest = jobs
                .rows
                .iter()
                .min_by(|a, b| {
                    a.1["created_at"]
                        .as_f64()
                        .partial_cmp(&b.1["created_at"].as_f64())
                        .unwrap()
                })
                .map(|(key, _)| key.clone())
                .unwrap();
            jobs.rows.remove(&oldest);
        }
        let id = uuid::Uuid::new_v4().simple().to_string();
        let (cancel, mut cancelled) = watch::channel(false);
        let job = json!({"id":id,"state":"loading","stage":"正在读取同源行情","created_at":now_seconds(),"query":request.query,"result":null,"evidence":null,"error":null});
        jobs.rows.insert(id.clone(), job.clone());
        jobs.active = Some((id.clone(), cancel));
        drop(jobs);
        let service = self.clone();
        tokio::spawn(async move {
            let result = tokio::select! {_=cancelled.changed()=>None,result=service.run_analysis(&id,request,ai)=>Some(result)};
            let mut jobs = service.0.jobs.lock().await;
            if let Some(job) = jobs.rows.get_mut(&id) {
                match result {
                    None => {
                        job["state"] = json!("cancelled");
                        job["stage"] = json!("已取消");
                    }
                    Some(Err(error)) => {
                        job["state"] = json!("failed");
                        job["stage"] = json!("分析未完成");
                        job["error"] = json!(error.detail);
                    }
                    Some(Ok(result)) => {
                        let success = result.get("analysis").is_some_and(|v| !v.is_null());
                        job["state"] = json!(if success { "completed" } else { "failed" });
                        job["stage"] = json!(if success {
                            "分析完成"
                        } else {
                            "分析未完成"
                        });
                        job["error"] = if success {
                            Value::Null
                        } else {
                            result["detail"].clone()
                        };
                        job["result"] = result;
                    }
                }
                job["finished_at"] = json!(Utc::now());
            }
            jobs.active = None;
        });
        Ok(job)
    }
    async fn run_analysis(
        &self,
        id: &str,
        request: AnalysisRequest,
        ai: Arc<AiService>,
    ) -> Result<Value> {
        let parameters=request.parameters.unwrap_or_default();
        let query=crate::analysis::quant::QuantInputRequest{code:request.query.symbol.clone(),source_id:Some(request.query.source.clone()),period:request.query.period.clone(),research_snapshot_id:request.research_snapshot_id.clone(),research_adjustment:Some(request.query.adjustment.clone()),research_asset:Some(request.query.asset.clone()),parameters:parameters.clone(),..Default::default()};
        let id_ref=request.research_snapshot_id.as_deref().ok_or_else(||ResearchError::invalid("请先读取研究快照。"))?;
        let authority=crate::analysis::research_input::read(&self.authority_dir(),id_ref).map_err(|_|ResearchError::invalid("研究输入版本不可用或校验失败。"))?;
        if authority.manifest.scope.source!=request.query.source||authority.manifest.scope.symbol!=request.query.symbol||authority.manifest.scope.period!=request.query.period||authority.manifest.scope.asset!=request.query.asset||authority.manifest.scope.adjustment!=request.query.adjustment{return Err(ResearchError::invalid("研究快照与当前查询不一致。"));}
        let expected=request.expected_input_hash.as_deref().ok_or_else(||ResearchError::invalid("请先等待同版本指标就绪。"))?;
        let snapshot=crate::analysis::service::authority_version(&query,expected).map_err(|e|ResearchError::new(409,e.to_string()))?;
        let evidence=serde_json::to_value(&snapshot.confirmed).map_err(|_|ResearchError::upstream())?;
        if let Some(job)=self.0.jobs.lock().await.rows.get_mut(id){job["state"]=json!("analyzing");job["stage"]=json!("正在分析同版本行情证据");job["evidence"]=evidence;job["snapshot_id"]=json!(snapshot.evidence.snapshot_hash);job["input_hash"]=json!(snapshot.evidence.input_hash);job["data_as_of"]=json!(snapshot.evidence.decision_as_of);}
        let snapshot=serde_json::to_value(snapshot).map_err(|_|ResearchError::upstream())?;
        ai.analyze(
            snapshot,
            AnalyzeOptions {
                enabled_strategies: parameters.enabled_strategies.iter().cloned().collect(),
                custom_prompt: request.question,
                model: request.model.unwrap_or_default(),
                reasoning_effort: request.reasoning_effort.unwrap_or_default(),
            },
        )
        .await
        .map_err(|_| ResearchError::new(502, "分析服务异常, 请检查本机 AI 连接后重试。"))
    }
    async fn job(&self, id: &str, cancel: bool) -> Result<Value> {
        let mut jobs = self.0.jobs.lock().await;
        let active = jobs.active.as_ref().is_some_and(|(key, _)| key == id);
        if !active
            && jobs
                .rows
                .get(id)
                .is_some_and(|j| now_seconds() - j["created_at"].as_f64().unwrap_or(0.0) > 1800.0)
        {
            jobs.rows.remove(id);
        }
        if !jobs.rows.contains_key(id) {
            return Err(ResearchError::new(404, "分析任务不存在或已过期。"));
        }
        if cancel && active {
            if let Some((_, sender)) = &jobs.active {
                sender.send_replace(true);
            }
            let row = jobs.rows.get_mut(id).unwrap();
            row["state"] = json!("cancelled");
            row["stage"] = json!("已取消");
        }
        Ok(jobs.rows[id].clone())
    }
}

pub fn router() -> Router<AppState> {
    Router::new()
        .route("/sources", get(sources))
        .route("/catalog", get(catalog))
        .route("/bars", post(bars))
        .route("/authority/merge",post(merge_authority))
        .route("/contracts", get(contracts))
        .route("/option-underlyings", get(underlyings))
        .route("/option-months/{symbol}", get(months))
        .route("/options/{symbol}", get(options))
        .route("/analysis", post(analyze))
        .route("/analysis/{id}", get(job).delete(cancel))
}
#[derive(Deserialize)]#[serde(deny_unknown_fields)]struct MergeAuthority{base_snapshot_id:String,additional_snapshot_id:String}
async fn merge_authority(State(state):State<AppState>,Json(request):Json<MergeAuthority>)->Result<Json<Value>>{Ok(Json(state.research.merge_authority(request).await?))}
async fn sources(State(state): State<AppState>) -> Json<Value> {
    Json(state.research.sources().await)
}
async fn catalog(UrlQuery(params): UrlQuery<HashMap<String, String>>) -> Result<Json<Value>> {
    let q = params.get("q").cloned().unwrap_or_default().to_lowercase();
    if q.chars().count() > 100 {
        return Err(ResearchError::invalid("搜索内容过长。"));
    }
    Ok(Json(Value::Array(
        CONFIG["catalog"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| {
                format!("{} {}", r["name"], r["symbol"])
                    .to_lowercase()
                    .contains(&q)
            })
            .cloned()
            .collect(),
    )))
}
async fn bars(
    State(state): State<AppState>,
    UrlQuery(params): UrlQuery<HashMap<String, String>>,
    Json(query): Json<Query>,
) -> Result<Json<Value>> {
    Ok(Json(
        state
            .research
            .bars(
                query,
                params
                    .get("refresh")
                    .is_some_and(|v| v == "true" || v == "1"),
            )
            .await?,
    ))
}
async fn contracts(
    State(state): State<AppState>,
    UrlQuery(params): UrlQuery<HashMap<String, String>>,
) -> Result<Json<Value>> {
    Ok(Json(
        state
            .research
            .contracts(
                params.get("asset").map(String::as_str).unwrap_or("future"),
                params.get("exchange").map(String::as_str).unwrap_or("SHFE"),
                params
                    .get("source")
                    .map(String::as_str)
                    .unwrap_or("tushare"),
            )
            .await?,
    ))
}
async fn underlyings() -> Json<Value> {
    Json(Value::Array(
        CONFIG["underlyings"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| json!({"symbol":r["symbol"],"name":r["name"],"category":r["category"],"quote_source":r["quote_source"],"daily_date":r["daily_date"],"availability":r["availability"]}))
            .collect(),
    ))
}
async fn months(State(state): State<AppState>, Path(symbol): Path<String>) -> Result<Json<Value>> {
    let mut result = state.research.months(&symbol.to_uppercase()).await?;
    result.as_object_mut().unwrap().remove("contracts");
    Ok(Json(result))
}
async fn options(
    State(state): State<AppState>,
    Path(symbol): Path<String>,
    UrlQuery(params): UrlQuery<HashMap<String, String>>,
) -> Result<Json<Value>> {
    let symbol = symbol.to_uppercase();
    let result = match params.get("source").map(String::as_str).unwrap_or("alpaca") {
        "akshare" => {
            if params.contains_key("expiry") {
                return Err(ResearchError::invalid(
                    "AKShare 使用合约月份, 实际到期日由合约目录提供。",
                ));
            }
            state
                .research
                .option_chain(
                    &symbol,
                    params
                        .get("month")
                        .ok_or_else(|| ResearchError::invalid("请先选择合约月份。"))?,
                    params.get("report_date").map(String::as_str),
                )
                .await?
        }
        "alpaca" => {
            state
                .research
                .alpaca_options(&symbol, params.get("expiry").map(String::as_str))
                .await?
        }
        _ => return Err(ResearchError::invalid("期权来源无效。")),
    };
    Ok(Json(result))
}
async fn analyze(
    State(state): State<AppState>,
    Json(request): Json<AnalysisRequest>,
) -> Result<(StatusCode, Json<Value>)> {
    Ok((
        StatusCode::ACCEPTED,
        Json(state.research.start_analysis(request, state.ai).await?),
    ))
}
async fn job(State(state): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    Ok(Json(state.research.job(&id, false).await?))
}
async fn cancel(State(state): State<AppState>, Path(id): Path<String>) -> Result<Json<Value>> {
    Ok(Json(state.research.job(&id, true).await?))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};

    #[tokio::test]
    async fn concurrent_reads_share_fetch_and_persist_only_same_query_fallback() {
        let calls = Arc::new(AtomicUsize::new(0));
        let failed = Arc::new(AtomicBool::new(false));
        let state = (calls.clone(), failed.clone());
        let app = Router::new().route("/quotes", get(|State((calls, failed)): State<(Arc<AtomicUsize>, Arc<AtomicBool>)>| async move {
            calls.fetch_add(1, Ordering::SeqCst);
            tokio::time::sleep(Duration::from_millis(20)).await;
            if failed.load(Ordering::SeqCst) {
                (StatusCode::BAD_GATEWAY, Json(json!({"secret_provider_body": "never expose"})))
            } else {
                (StatusCode::OK, Json(json!({"code":0,"data":{"sh600519":{"day":[
                    ["2026-09-29","10","11","12","9",null],
                    ["2026-09-30","11","12","13","10","0"]]}}})))
            }
        })).with_state(state);
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/quotes", listener.local_addr().unwrap());
        let server = tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
        let folder = tempfile::tempdir().unwrap();
        let mut service = Research::new(
            reqwest::Client::new(),
            folder.path().into(),
            PathBuf::from("/missing-python"),
            folder.path().into(),
        );
        Arc::get_mut(&mut service.0)
            .unwrap()
            .environment
            .insert("TRACEFANG_RESEARCH_TENCENT_URL".into(), url);
        let query = Query {
            source: "tencent".into(),
            symbol: "600519.SH".into(),
            limit: 1,
            ..Query::default()
        };
        let (one, two) = tokio::join!(
            service.bars(query.clone(), false),
            service.bars(query.clone(), false)
        );
        assert_eq!(one.as_ref().unwrap()["items"], two.unwrap()["items"]);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(one.unwrap()["next_before"].is_string());
        assert_eq!(
            service.bars(query.clone(), false).await.unwrap()["cache_state"],
            "cached"
        );
        failed.store(true, Ordering::SeqCst);
        assert_eq!(
            service.bars(query.clone(), true).await.unwrap()["cache_state"],
            "stale"
        );
        let other = service
            .bars(
                Query {
                    symbol: "000001.SZ".into(),
                    ..query
                },
                false,
            )
            .await
            .unwrap_err();
        assert!(!other.detail.contains("secret_provider_body"));
        server.abort();
    }
}
