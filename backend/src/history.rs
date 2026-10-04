//! Same-source historical demand. Downloaded evidence enters the durable ordered
//! consumer before coverage is committed; HTTP reads never call this coordinator.
use crate::{
    capture::{Capture, ProviderFrame},
    catalog::{Definition, provider_code},
    ingestion::Acquisition,
    market::Market,
    pages,
    providers::{self, jin10},
};
use anyhow::{Context, Result, bail, ensure};
use chrono::{Datelike, Duration, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::{Arc, Mutex},
    time::Duration as StdDuration,
};
use tokio::{
    sync::{Mutex as AsyncMutex, Semaphore, watch},
    task::JoinHandle,
};
use tracefang_core::{
    domain::{Candle, Timestamp},
    periods::Period,
    reducer::{SeriesState, floor_time},
};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum BackfillState {
    Cached,
    Joined,
    Fetched,
    Advanced,
    Exhausted,
    Deferred,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BackfillResult {
    pub source_id: String,
    pub state: BackfillState,
    pub start: Timestamp,
    pub end: Timestamp,
    pub row_count: usize,
    pub covered_start: Option<Timestamp>,
    pub covered_end: Option<Timestamp>,
    pub authoritative_through: Option<Timestamp>,
    pub history_floor: Option<Timestamp>,
    pub retry_after: Option<Timestamp>,
    pub evidence_version: Option<String>,
}
#[derive(Debug, Default, Clone, Serialize)]
pub struct HistoryMetrics {
    pub cache_hits: u64,
    pub upstream_calls: u64,
    pub joined_calls: u64,
    pub written_rows: u64,
    pub failures: u64,
    pub pending: usize,
}
#[derive(Clone)]
struct Failure {
    count: u32,
    retry_after: Timestamp,
}
type SeriesId = (String, String);
pub struct History {
    market: Market,
    capture: Capture,
    acquisition: Acquisition,
    http: reqwest::Client,
    locks: Mutex<BTreeMap<SeriesId, Arc<AsyncMutex<()>>>>,
    failures: Mutex<BTreeMap<SeriesId, Failure>>,
    metrics: Mutex<HistoryMetrics>,
    concurrency: Semaphore,
}
struct DownloadEvidence {
    rows: BTreeMap<Timestamp, Candle>,
    checked_start: Option<Timestamp>,
    published_through: Option<Timestamp>,
    version: String,
}
impl History {
    pub fn new(market: Market, capture: Capture, acquisition: Acquisition) -> Result<Self> {
        Ok(Self {
            market,
            capture,
            acquisition,
            http: providers::http_client()?,
            locks: Mutex::new(BTreeMap::new()),
            failures: Mutex::new(BTreeMap::new()),
            metrics: Mutex::new(HistoryMetrics::default()),
            concurrency: Semaphore::new(2),
        })
    }
    pub fn metrics(&self) -> HistoryMetrics {
        self.metrics.lock().expect("history metrics lock").clone()
    }
    pub async fn backfill(
        &self,
        code: &str,
        start: Timestamp,
        count: usize,
        revalidate: bool,
    ) -> Result<BackfillResult> {
        ensure!(
            (1..=10_000).contains(&count),
            "count must be between 1 and 10000"
        );
        let start = floor_time(start, 60)?;
        let end = start + Duration::minutes(count as i64);
        let definition = self.market.catalog.get(code)?.clone();
        ensure!(
            definition.history_backfill_supported,
            "instrument has no same-source history backfill"
        );
        let source = self.market.source(&definition.instrument.symbol)?;
        ensure!(
            definition.source_ids.contains(&source),
            "instrument source is not supported"
        );
        let key = (source.clone(), definition.instrument.symbol.clone());
        let lock = {
            self.locks
                .lock()
                .expect("history lock registry")
                .entry(key.clone())
                .or_insert_with(|| Arc::new(AsyncMutex::new(())))
                .clone()
        };
        let (guard, joined) = match lock.try_lock() {
            Ok(guard) => (guard, false),
            Err(_) => (lock.lock().await, true),
        };
        let _guard = guard;
        if joined {
            self.metrics.lock().unwrap().joined_calls += 1;
        }
        let state = self
            .market
            .store
            .series_state(&definition.instrument.symbol, &source)
            .await?
            .map(serde_json::from_value::<SeriesState>)
            .transpose()?;
        let mut result = BackfillResult {
            source_id: source.clone(),
            state: BackfillState::Deferred,
            start,
            end,
            row_count: 0,
            covered_start: None,
            covered_end: None,
            authoritative_through: state.as_ref().map(|s| s.authoritative_through),
            history_floor: state.as_ref().and_then(|s| s.history_floor),
            retry_after: None,
            evidence_version: state.as_ref().map(|s| s.evidence_version.clone()),
        };
        if let Some(failure) = self.failures.lock().unwrap().get(&key) {
            if failure.retry_after > Utc::now() {
                result.retry_after = Some(failure.retry_after);
                return Ok(result);
            }
        }
        if !revalidate
            && self
                .is_covered(&definition.instrument.symbol, &source, start, end)
                .await?
        {
            result.state = if joined {
                BackfillState::Joined
            } else {
                BackfillState::Cached
            };
            result.covered_start = Some(start);
            result.covered_end = Some(end);
            self.metrics.lock().unwrap().cache_hits += 1;
            return Ok(result);
        }
        if !revalidate {
            if let Some(state) = &state {
                if state.tail_checked_through.is_some_and(|at| at >= end)
                    && state.authoritative_through <= start
                {
                    if let Some(checked) = state.tail_checked_at {
                        if checked + Duration::seconds(5) > Utc::now() {
                            result.retry_after = Some(checked + Duration::seconds(5));
                            return Ok(result);
                        }
                    }
                }
            }
        }
        let _permit = self.concurrency.acquire().await?;
        {
            let mut metrics = self.metrics.lock().unwrap();
            metrics.upstream_calls += 1;
            metrics.pending += 1;
        }
        let fetched = match source.as_str() {
            "jin10_client" => {
                self.download_jin10(&definition, start, end, revalidate)
                    .await
            }
            "tonghuashun_futures" => self.download_ths(&definition, start, end).await,
            _ => Err(anyhow::anyhow!(
                "source has no configured historical channel"
            )),
        };
        self.metrics.lock().unwrap().pending -= 1;
        let evidence = match fetched {
            Ok(value) => {
                self.failures.lock().unwrap().remove(&key);
                value
            }
            Err(error) => {
                let mut failures = self.failures.lock().unwrap();
                let count = failures.get(&key).map_or(1, |f| (f.count + 1).min(6));
                failures.insert(
                    key,
                    Failure {
                        count,
                        retry_after: Utc::now() + Duration::seconds((1_i64 << count).min(60)),
                    },
                );
                self.metrics.lock().unwrap().failures += 1;
                return Err(error);
            }
        };
        ensure!(
            self.market.source(&definition.instrument.symbol)? == source,
            "instrument source changed during history demand"
        );
        let Some(authority) = evidence_authority(end, evidence.published_through, Utc::now())?
        else {
            // An empty manifest is not proof of a permanent beginning or published tail.
            result.retry_after = Some(Utc::now() + Duration::seconds(5));
            result.evidence_version = Some(evidence.version);
            return Ok(result);
        };
        let covered_start = evidence
            .checked_start
            .map(|at| at.max(start))
            .filter(|at| *at < authority);
        let accepted = evidence
            .rows
            .values()
            .filter(|bar| start <= bar.open_time && bar.open_time < end)
            .count();
        let latest = evidence
            .rows
            .values()
            .filter(|bar| bar.open_time < authority)
            .map(|bar| bar.open_time)
            .max();
        let channel = if source == "jin10_client" {
            "jin10_local"
        } else {
            "tonghuashun_futures"
        };
        let provider = if source == "jin10_client" {
            jin10::provider_code(&definition.instrument)?.to_owned()
        } else {
            provider_code(&definition)
        };
        let persisted = self
            .commit_coverage(
                &definition,
                &source,
                channel,
                &provider,
                covered_start,
                end,
                authority,
                latest,
                accepted,
                &evidence.version,
            )
            .await?;
        result.authoritative_through = Some(persisted.authoritative_through);
        result.history_floor = persisted.history_floor;
        result.evidence_version = Some(evidence.version);
        result.row_count = accepted;
        if let Some(start) = covered_start {
            result.covered_start = Some(start);
            result.covered_end = Some(authority);
        }
        result.state = if accepted > 0 {
            BackfillState::Fetched
        } else if covered_start == Some(start) && authority > start {
            BackfillState::Advanced
        } else {
            BackfillState::Deferred
        };
        if authority < end {
            result.retry_after = Some(Utc::now() + Duration::seconds(5));
        }
        self.metrics.lock().unwrap().written_rows += accepted as u64;
        // The persisted state is metadata, while all Bars themselves came through the ordered consumer.
        self.market
            .state
            .lock()
            .unwrap()
            .reducer
            .hydrate(vec![], Some(persisted))?;
        Ok(result)
    }
    async fn is_covered(
        &self,
        symbol: &str,
        source: &str,
        start: Timestamp,
        end: Timestamp,
    ) -> Result<bool> {
        let coverage=self.market.store.metadata("coverage",&format!("{source}:{symbol}")).await?.unwrap_or(Value::Null);
        let ranges=coverage["ranges"].as_array().into_iter().flatten().map(|row|Ok((row[0].as_str().context("coverage start")?.parse()?,row[1].as_str().context("coverage end")?.parse()?))).collect::<Result<Vec<(Timestamp,Timestamp)>>>()?;
        Ok(ranges_cover(&ranges, start, end))
    }
    async fn capture_history(
        &self,
        frame: ProviderFrame,
        decoder: &mut providers::Decoder,
        work:providers::ingress::WorkReservation,
    ) -> Result<Vec<Candle>> {
        let decoded=decoder.decode(&frame);
        let receipt=self.acquisition.frames.append(frame,work).await?;
        let sequence=receipt.position.sequence;
        // Decode for manifest validation and coverage evidence only. This path never applies Bars.
        self.acquisition.wait_projected(sequence).await?;
        self.wait_persisted(sequence).await?;
        Ok(decoded?.1)
    }
    async fn wait_persisted(&self,sequence:u64)->Result<()> {self.acquisition.wait_projected(sequence).await}
    async fn download_jin10(
        &self,
        definition: &Definition,
        start: Timestamp,
        end: Timestamp,
        revalidate: bool,
    ) -> Result<DownloadEvidence> {
        let provider = jin10::provider_code(&definition.instrument)?;
        let mut boundary = end.timestamp();
        let mut seen_files = BTreeSet::new();
        let mut seen_boundaries = BTreeSet::new();
        let mut decoder = providers::Decoder::new(self.market.catalog.clone());
        let mut rows = BTreeMap::new();
        let mut digest = Sha256::new();
        let mut published = None;
        let mut checked_start = None;
        for _ in 0..32 {
            ensure!(
                seen_boundaries.insert(boundary),
                "history manifest cursor did not advance"
            );
            let manifest = self.acquisition.local.manifest(provider, boundary).await?;
            ensure!(
                manifest.provider_code == provider && manifest.time_type == 1,
                "history manifest belongs to a different dataset"
            );
            digest.update(serde_json::to_vec(&manifest)?);
            let files = manifest
                .files
                .into_iter()
                .filter(|item| seen_files.insert(item.file_name.clone()))
                .collect::<Vec<_>>();
            if files.is_empty() {
                return Ok(DownloadEvidence {
                    rows,
                    checked_start,
                    published_through: published,
                    version: format!("{:x}", digest.finalize()),
                });
            }
            let mut oldest = None;
            for file in files {
                let mut work=self.acquisition.frames.reserve().await?;
                let frame=jin10::request_history_file_reserved(&definition.instrument,&file,revalidate,&mut work).await?;
                digest.update(&frame.body);
                let decoded = match self.capture_history(frame, &mut decoder,work).await {
                    Ok(rows) => rows,
                    Err(error) if error.to_string().contains("record count differs") => {
                        let mut work=self.acquisition.frames.reserve().await?;
                        let frame=jin10::request_history_file_reserved(&definition.instrument,&file,true,&mut work).await?;
                        digest.update(&frame.body);
                        self.capture_history(frame, &mut decoder,work).await?
                    }
                    Err(error) => return Err(error),
                };
                for candle in decoded {
                    ensure!(
                        candle.instrument == definition.instrument
                            && candle.source.provider == "jin10_local",
                        "history decoder returned another dataset"
                    );
                    checked_start = Some(
                        checked_start
                            .map_or(candle.open_time, |at: Timestamp| at.min(candle.open_time)),
                    );
                    let through = candle.open_time + Duration::minutes(1);
                    if through<=candle.source.received_at {published = Some(published.map_or(through, |at: Timestamp| at.max(through)));}
                    oldest = Some(oldest.map_or(candle.open_time.timestamp(), |at: i64| {
                        at.min(candle.open_time.timestamp())
                    }));
                    if start <= candle.open_time && candle.open_time < end {
                        rows.insert(candle.open_time, candle);
                    }
                }
                if let Some(at) = file.start_timestamp {
                    oldest = Some(oldest.map_or(at, |oldest| oldest.min(at)));
                }
            }
            let Some(oldest) = oldest else {
                return Ok(DownloadEvidence {
                    rows,
                    checked_start,
                    published_through: published,
                    version: format!("{:x}", digest.finalize()),
                });
            };
            if oldest <= start.timestamp() {
                checked_start = Some(start);
                return Ok(DownloadEvidence {
                    rows,
                    checked_start,
                    published_through: published,
                    version: format!("{:x}", digest.finalize()),
                });
            }
            ensure!(
                oldest < boundary,
                "history manifest did not move to older evidence"
            );
            boundary = oldest;
        }
        bail!("historical demand exceeded the bounded manifest page limit")
    }
    async fn download_ths(
        &self,
        definition: &Definition,
        start: Timestamp,
        end: Timestamp,
    ) -> Result<DownloadEvidence> {
        let zone = providers::line_zone(definition);
        let first = start.with_timezone(&zone).year();
        let last = (end - Duration::microseconds(1))
            .with_timezone(&zone)
            .year();
        ensure!(
            last - first <= 1,
            "historical transport window crosses too many years"
        );
        let connection = uuid::Uuid::new_v4().to_string();
        let mut decoder = providers::Decoder::new(self.market.catalog.clone());
        let mut rows = BTreeMap::new();
        let mut digest = Sha256::new();
        let mut published = None;
        for (index, year) in (first..=last).enumerate() {
            let mut work=self.acquisition.frames.reserve().await?;
            let frame = providers::http_frame_reserved(
                &self.http,
                definition,
                "minute_year",
                &format!("{year}.js"),
                &connection,
                index as u64 + 1,
                &mut work,
            )
            .await?;
            digest.update(&frame.body);
            for candle in self.capture_history(frame, &mut decoder,work).await? {
                ensure!(
                    candle.instrument == definition.instrument
                        && candle.source.provider == "tonghuashun_futures",
                    "history decoder returned another dataset"
                );
                let through = candle.open_time + Duration::minutes(1);
                if through<=candle.source.received_at {published = Some(published.map_or(through, |at: Timestamp| at.max(through)));}
                if start <= candle.open_time && candle.open_time < end {
                    rows.insert(candle.open_time, candle);
                }
            }
        }
        Ok(DownloadEvidence {
            rows,
            checked_start: Some(start),
            published_through: published,
            version: format!("{:x}", digest.finalize()),
        })
    }
    #[allow(clippy::too_many_arguments)]
    async fn commit_coverage(
        &self,
        definition: &Definition,
        source: &str,
        channel: &str,
        provider: &str,
        covered_start: Option<Timestamp>,
        end: Timestamp,
        authority: Timestamp,
        latest: Option<Timestamp>,
        count: usize,
        version: &str,
    ) -> Result<SeriesState> {
        let key=format!("{source}:{}",definition.instrument.symbol);
        let current=self.market.store.series_state(&definition.instrument.symbol,source).await?.map(serde_json::from_value::<SeriesState>).transpose()?;
        let state=SeriesState {realtime_source_id:source.into(),instrument_symbol:definition.instrument.symbol.clone(),upstream_channel_id:channel.into(),provider_symbol:provider.into(),interval_seconds:60,latest_authoritative_open_time:latest.or(current.as_ref().and_then(|v|v.latest_authoritative_open_time)),authoritative_through:current.as_ref().map_or(authority,|v|authority.max(v.authoritative_through)),history_floor:current.as_ref().and_then(|v|v.history_floor),tail_checked_through:(authority<end).then_some(end),tail_checked_at:(authority<end).then_some(Utc::now()),evidence_version:version.into(),updated_at:Utc::now()};
        self.market.store.commit_history_metadata(key,serde_json::to_value(&state)?,covered_start.map(|start|(start,authority)),json!({"source":source,"channel":channel,"provider_symbol":provider,"row_count":count,"evidence_version":version})).await?;Ok(state)
    }
    pub async fn ensure_older(
        &self,
        code: &str,
        period: Period,
        before: Timestamp,
        count_back: usize,
    ) -> Result<Value> {
        let count = history_demand_minutes(period, count_back)?;
        let definition = self.market.catalog.get(code)?;
        let source = self.market.source(&definition.instrument.symbol)?;
        let schedule = pages::schedule(&self.market, code)?;
        let mut page =
            pages::chart_page(&self.market, code, period, Some(before), count_back).await?;
        let mut backfill = None;
        let mut source_status = "available";
        if page.items.is_empty() {
            if matches!(period, Period::Timeline | Period::S1)
                || !definition.history_backfill_supported
            {
                source_status = "unsupported";
            } else {
                let end = floor_time(before, 60)?;
                let result = self
                    .backfill(code, end - Duration::minutes(count as i64), count, false)
                    .await?;
                source_status = match result.state {
                    BackfillState::Deferred => "deferred",
                    BackfillState::Exhausted => "exhausted",
                    _ => "available",
                };
                page =
                    pages::chart_page(&self.market, code, period, Some(before), count_back).await?;
                if page.next_before.is_none() {
                    if let (Some(start), Some(covered_end)) =
                        (result.covered_start, result.covered_end)
                    {
                        if start < end && end <= covered_end {
                            page.next_before = Some(start);
                        }
                    }
                }
                backfill = Some(result);
            }
        }
        ensure!(
            page.next_before.is_none_or(|at| at < before),
            "history page cursor did not advance"
        );
        let next_before = page.next_before;
        let local_status = if page.items.is_empty() {
            "empty"
        } else {
            "ready"
        };
        let payload = pages::page_payload(
            page,
            &definition.instrument,
            &source,
            period,
            Some(&schedule),
        )?;
        Ok(
            json!({"source_id":source,"period_id":period.as_str(),"local_status":local_status,"source_status":source_status,"next_before":next_before,"next_cursor":payload["next_cursor"],"page":payload,"backfill":backfill}),
        )
    }
    pub fn spawn_tail_recovery(
        self: Arc<Self>,
        mut shutdown: watch::Receiver<bool>,
    ) -> JoinHandle<()> {
        tokio::spawn(async move {
            // Let transport authentication and the retained-frame consumer establish their state.
            tokio::select! {_=shutdown.changed()=>return,_=tokio::time::sleep(StdDuration::from_secs(3))=>{}}
            loop {
                let codes = self.market.watchlist.lock().unwrap().clone();
                let mut targets = BTreeSet::new();
                for code in codes {
                    let Ok(definition) = self.market.catalog.get(&code) else {
                        continue;
                    };
                    if definition.dependencies.is_empty() {
                        if definition.history_backfill_supported {
                            targets.insert(code);
                        }
                    } else {
                        for dependency in &definition.dependencies {
                            if let Ok(d) = self.market.catalog.get(&dependency.symbol) {
                                if d.history_backfill_supported {
                                    targets.insert(d.code.clone());
                                }
                            }
                        }
                    }
                }
                for code in targets {
                    if *shutdown.borrow() {
                        return;
                    }
                    let Ok(definition) = self.market.catalog.get(&code) else {
                        continue;
                    };
                    let Ok(source) = self.market.source(&definition.instrument.symbol) else {
                        continue;
                    };
                    let end = match floor_time(Utc::now(), 60) {
                        Ok(end) => end,
                        Err(_) => continue,
                    };
                    let state = self
                        .market
                        .store
                        .series_state(&definition.instrument.symbol, &source)
                        .await
                        .ok()
                        .flatten()
                        .and_then(|v| serde_json::from_value::<SeriesState>(v).ok());
                    let start = state
                        .map_or(end - Duration::minutes(240), |s| s.authoritative_through)
                        .max(end - Duration::minutes(10_000))
                        .min(end);
                    let count = (end - start).num_minutes() as usize;
                    if count == 0 {
                        continue;
                    }
                    tokio::select! {
                        _=shutdown.changed()=>return,
                        result=self.backfill(&code,start,count,false)=>if let Err(error)=result{tracing::warn!(code,%error,"same-source tail recovery deferred");},
                    }
                }
                tokio::select! {_=shutdown.changed()=>return,_=tokio::time::sleep(StdDuration::from_secs(60))=>{}}
            }
        })
    }
}
pub fn history_demand_minutes(period: Period, count_back: usize) -> Result<usize> {
    ensure!(
        (1..=10_000).contains(&count_back),
        "count_back must be between 1 and 10000"
    );
    let minutes = if let Some(seconds) = period.seconds() {
        (seconds as usize * count_back).div_ceil(60)
    } else {
        let factor = match period {
            Period::D1 => 1440,
            Period::W1 => 10080,
            Period::Mo1 => 44640,
            Period::Q1 => 132480,
            Period::Y1 => 527040,
            _ => unreachable!(),
        };
        factor * count_back
    };
    Ok(minutes.clamp(1, 10_000))
}
fn evidence_authority(
    end: Timestamp,
    published: Option<Timestamp>,
    now: Timestamp,
) -> Result<Option<Timestamp>> {
    let completed = floor_time(now, 60)?;
    Ok(published.map(|at| at.min(end).min(completed)))
}
fn ranges_cover(ranges: &[(Timestamp, Timestamp)], start: Timestamp, end: Timestamp) -> bool {
    let mut covered = start;
    for &(left, right) in ranges {
        if left > covered {
            return false;
        }
        covered = covered.max(right);
        if covered >= end {
            return true;
        }
    }
    covered >= end
}
#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn durable_history_enters_ordered_consumer_before_coverage() -> Result<()> {
        use base64::{Engine, engine::general_purpose::STANDARD};
        use flate2::{Compression, write::GzEncoder};
        use std::io::Write;
        let dir=tempfile::tempdir()?;
        let db=crate::store::Store::connect(dir.path().join("facts.redb").to_str().unwrap()).await?;
        let capture=Capture::connect(dir.path().join("capture.redb").to_str().unwrap()).await?;
        let market=Market::new(crate::catalog::Catalog::embedded()?,db.clone()).await?;
        let(acquisition,mut tasks)=Acquisition::start_with_acquisition_enabled(market.clone(),capture.clone(),false).await?;
        let history=History::new(market.clone(),capture.clone(),acquisition.clone())?;
        let outcome:Result<()>=async {
                let at:Timestamp="2026-08-06T13:23:00Z".parse()?;
                let mut gzip=GzEncoder::new(Vec::new(),Compression::default());
                for value in [at.timestamp(),4252000000,4250000000,4249000000,4251000000,10] {gzip.write_all(&value.to_le_bytes())?;}
                let envelope=json!({"provider_code":"XAUUSD.GOODS","file":{"file_name":"historyintegration","record_count":1,"start_timestamp":at.timestamp(),"end_timestamp":at.timestamp()},"body_base64":STANDARD.encode(gzip.finish()?)});
                let mut frame=ProviderFrame{version:1,channel:"jin10_history".into(),connection_id:uuid::Uuid::new_v4().simple().to_string(),sequence:1,received_at:Utc::now(),encoding:"gzip-json".into(),body:serde_json::to_vec(&envelope)?};
                let mut decoder=providers::Decoder::new(market.catalog.clone());
                let rows=history.capture_history(frame.clone(),&mut decoder,acquisition.frames.reserve().await?).await?;
                ensure!(rows.len()==1,"history record was not decoded");
                let definition=market.catalog.get("XAUUSD")?;
                let stored=db.bars_before(&definition.instrument.symbol,"jin10_client",60,None,10).await?;
                ensure!(stored.len()==1,"ordered consumer persisted {} Bars, expected one",stored.len());
                ensure!(tracefang_core::domain::Decimal::from_str_exact(stored[0]["close"].as_str().context("price string")?)?==tracefang_core::domain::Decimal::from(4251),"historical price changed precision");
                let revision=stored[0]["revision"].clone();
                history.capture_history(frame.clone(),&mut decoder,acquisition.frames.reserve().await?).await?;
                let repeated=db.bars_before(&definition.instrument.symbol,"jin10_client",60,None,10).await?;
                ensure!(repeated[0]["revision"]==revision,"raw redelivery changed a historical Bar revision");
                let state=history.commit_coverage(definition,"jin10_client","jin10_local","XAUUSD.GOODS",Some(at),at+Duration::minutes(2),at+Duration::minutes(1),Some(at),1,"history-integration").await?;
                ensure!(state.authoritative_through==at+Duration::minutes(1)&&state.history_floor.is_none(),"coverage fabricated authority or a history floor");
                ensure!(state.tail_checked_through==Some(at+Duration::minutes(2)),"unpublished tail was not recorded");
                ensure!(history.is_covered(&definition.instrument.symbol,"jin10_client",at,at+Duration::minutes(1)).await?,"coverage did not persist");
                ensure!(!history.is_covered(&definition.instrument.symbol,"tonghuashun_futures",at,at+Duration::minutes(1)).await?,"coverage leaked across source bindings");
                let cached=history.backfill("XAUUSD",at,1,false).await?;
                ensure!(cached.state==BackfillState::Cached,"repeated demand performed an upstream fetch");
                ensure!(history.metrics().upstream_calls==0,"cached demand reached upstream");
                let mut invalid=envelope;invalid["file"]["record_count"]=json!(2);frame.sequence=2;frame.body=serde_json::to_vec(&invalid)?;
                ensure!(history.capture_history(frame,&mut decoder,acquisition.frames.reserve().await?).await.is_err(),"invalid manifest was accepted");
                let failures=db.metadata("runtime","decode_failures").await?.unwrap_or(Value::Null);
                ensure!(failures=="1","invalid durable evidence lost its diagnostic");
                ensure!(!history.is_covered(&definition.instrument.symbol,"jin10_client",at,at+Duration::minutes(2)).await?,"failed history extended coverage");
                Ok(())
        }.await;
        tasks.stop_and_drain().await?;capture.close_and_drain().await?;db.close().await?;
        outcome
    }
    #[test]
    fn history_transport_is_bounded_for_every_period() {
        for period in Period::ALL {
            for count in [1, 500, 10_000] {
                assert!((1..=10_000).contains(&history_demand_minutes(period, count).unwrap()));
            }
        }
        assert_eq!(history_demand_minutes(Period::M5, 100).unwrap(), 500);
        assert_eq!(history_demand_minutes(Period::Y1, 500).unwrap(), 10_000);
        assert!(history_demand_minutes(Period::M1, 0).is_err());
    }
    #[test]
    fn authority_requires_evidence_and_cannot_advance_past_publication() {
        let start: Timestamp = "2026-10-01T00:00:00Z".parse().unwrap();
        assert_eq!(
            evidence_authority(start + Duration::hours(1), None, start + Duration::hours(2))
                .unwrap(),
            None
        );
        assert_eq!(
            evidence_authority(
                start + Duration::hours(1),
                Some(start - Duration::minutes(1)),
                start + Duration::hours(2)
            )
            .unwrap(),
            Some(start - Duration::minutes(1))
        );
        assert_eq!(
            evidence_authority(
                start + Duration::hours(1),
                Some(start + Duration::hours(2)),
                start + Duration::seconds(90)
            )
            .unwrap(),
            Some(start + Duration::minutes(1))
        );
    }
    #[test]
    fn coverage_must_not_jump_an_unchecked_gap() {
        let start: Timestamp = "2026-10-01T00:00:00Z".parse().unwrap();
        let at = |n| start + Duration::minutes(n);
        assert!(ranges_cover(
            &[(at(0), at(2)), (at(2), at(4))],
            at(0),
            at(4)
        ));
        assert!(!ranges_cover(
            &[(at(0), at(2)), (at(3), at(4))],
            at(0),
            at(4)
        ));
        assert!(!ranges_cover(&[], at(0), at(4)));
    }
}
