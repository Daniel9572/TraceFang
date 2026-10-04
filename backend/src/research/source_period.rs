//! Bounded manual source-period reference prices, separate from executable authority.
use super::{Research,ResearchError,Result};
use crate::{catalog::{Definition,PublicFeed},providers::fuyao};
use crate::analysis::{quant::content_hash,results::{atomic_json,create_directory}};
use anyhow::{Context,ensure};
use base64::{Engine,engine::general_purpose::STANDARD};
use chrono::Utc;
use serde_json::{Value,json};
use sha2::{Digest,Sha256};
use std::{fs,io::Read,path::Path,sync::{LazyLock,Mutex,Arc}};
use tokio::sync::Semaphore;

const MAX_BODY:usize=1024*1024;
const MAX_REFERENCE:usize=8*1024*1024;
const ARCHIVE_BUDGET:u64=256*1024*1024;
static REQUESTS:LazyLock<Arc<Semaphore>>=LazyLock::new(||Arc::new(Semaphore::new(2)));
static ARCHIVE_WRITES:Mutex<()>=Mutex::new(());

fn with_reference(mut packet:Value,id:&str)->Value {
    packet["reference_id"]=json!(id);
    packet["reference_url"]=json!(format!("/api/source-period-prices/{}/{id}",packet["code"].as_str().unwrap_or("")));
    packet
}
fn read_reference(root:&Path,id:&str)->anyhow::Result<Value> {
    ensure!(id.len()==64&&id.bytes().all(|b|b.is_ascii_hexdigit()&&!b.is_ascii_uppercase()),"invalid source reference id");
    let mut bytes=Vec::new();fs::File::open(root.join(format!("{id}.json")))?.take((MAX_REFERENCE+1) as u64).read_to_end(&mut bytes)?;
    ensure!(bytes.len()<=MAX_REFERENCE,"source reference exceeds byte bound");
    let packet:Value=serde_json::from_slice(&bytes)?;
    ensure!(content_hash(&packet)?==id,"source reference content hash differs");
    let body=STANDARD.decode(packet["source_evidence"]["body_base64"].as_str().context("source body missing")?)?;
    ensure!(body.len()<=MAX_BODY&&hex::encode(Sha256::digest(&body))==packet["source_evidence"]["body_sha256"].as_str().context("source body hash missing")?,"source reference body hash differs");
    Ok(packet)
}
fn publish_reference(root:&Path,packet:&Value)->anyhow::Result<String> {
    let _write=ARCHIVE_WRITES.lock().map_err(|_|anyhow::anyhow!("source reference archive lock poisoned"))?;
    create_directory(root)?;
    let id=content_hash(packet)?;
    let target=root.join(format!("{id}.json"));
    if target.exists(){read_reference(root,&id)?;return Ok(id)}
    let bytes=serde_json::to_vec(packet)?;
    ensure!(bytes.len()<=MAX_REFERENCE,"source reference exceeds byte bound");
    // ponytail: manual archive writes scan the fixed byte budget; add an index if this becomes a frequent workload.
    let retained=fs::read_dir(root)?.try_fold(0u64,|total,entry|->anyhow::Result<u64>{let entry=entry?;Ok(total.checked_add(entry.metadata()?.len()).context("source archive byte count overflow")?)})?;
    ensure!(retained.checked_add(bytes.len() as u64).is_some_and(|n|n<=ARCHIVE_BUDGET),"来源参考归档已达到本机容量，本次未保存新记录；旧原文仍保留");
    atomic_json(&target,packet)?;
    Ok(id)
}

fn fixed_body(path:&Path,manifest_sha:&str,feed:&PublicFeed,request:&Value)->anyhow::Result<(Vec<u8>,Value)> {
    ensure!(manifest_sha.len()==64&&manifest_sha.bytes().all(|b|b.is_ascii_hexdigit()),"fixed source manifest SHA required");
    let mut bytes=Vec::new();fs::File::open(path)?.take((2*MAX_BODY+1) as u64).read_to_end(&mut bytes)?;
    ensure!(bytes.len()<=2*MAX_BODY&&hex::encode(Sha256::digest(&bytes))==manifest_sha,"fixed source manifest hash or bound differs");
    let manifest:Value=serde_json::from_slice(&bytes)?;
    ensure!(manifest["schema"]=="tracefang-fuyao-source-period-min5-tests-v1","unsupported fixed source manifest");
    let bodies=manifest["bodies"].as_array().filter(|v|v.len()<=90).context("fixed source allowlist exceeds bounds")?;
    let total=bodies.iter().try_fold(0u64,|count,row|->anyhow::Result<u64>{Ok(count+row["row_count"].as_u64().filter(|n|*n<=100).context("fixed source declared rows exceed per-body bound")?)})?;
    ensure!(total<=9000,"fixed source declared total rows exceed bounds");
    let matches=bodies.iter().filter(|row|row["market"]==feed.market&&row["code"]==feed.code&&row["receipt"]["request"]==*request).collect::<Vec<_>>();
    ensure!(matches.len()==1,"固定原文未包含当前品种的这一请求，本次未请求上游");
    let row=matches[0];let receipt=&row["receipt"];
    let body=row["body_text"].as_str().context("fixed source body missing")?.as_bytes().to_vec();
    let sha=hex::encode(Sha256::digest(&body));
    ensure!(body.len()<=MAX_BODY&&row["body_sha256"]==sha&&receipt["sha256"]==sha&&receipt["bytes"].as_u64()==Some(body.len() as u64)&&receipt["status"]==200,"fixed source body or successful receipt differs");
    for key in ["requested_at","received_at"] {chrono::DateTime::parse_from_rfc3339(receipt[key].as_str().context("original source receipt clock missing")?)?;}
    ensure!(receipt["url"].as_str()==Some("https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1/single_kline"),"fixed source endpoint differs");
    Ok((body,json!({"url":receipt["url"],"request":receipt["request"],"http_status":receipt["status"],
        "requested_at":receipt["requested_at"],"received_at":receipt["received_at"],
        "receive_clock_role":"original acquisition completion; fixed read is not a new source receipt",
        "body_sha256":sha,"body_bytes":receipt["bytes"],"fixed_manifest_sha256":manifest_sha})))
}

impl Research {
    pub async fn source_period_prices(&self,definition:&Definition,limit:usize,read_only:bool)->Result<Value> {
        let feed=definition.public_feed.as_ref().ok_or_else(||ResearchError::new(409,"该品种尚未配置可核对的来源五分钟通道。"))?;
        if !(1..=100).contains(&limit){return Err(ResearchError::invalid("来源价格数量须为1–100"))}
        let _permit=REQUESTS.clone().try_acquire_owned().map_err(|_|ResearchError::new(429,"来源价格读取正在进行，请稍后重试。"))?;
        let request=fuyao::five_minute_request(feed,limit as u32,0);
        let (body,mut evidence,delivery_mode)=if let Some(path)=self.0.environment.get("TRACEFANG_SOURCE_PERIOD_FIXTURE_MANIFEST") {
            let path=std::path::PathBuf::from(path);let sha=self.0.environment.get("TRACEFANG_SOURCE_PERIOD_FIXTURE_SHA256").cloned().ok_or_else(||ResearchError::new(409,"固定原文未绑定清单SHA，本次未请求上游。"))?;
            let mapping=feed.clone();let actual_request=request.clone();
            let (body,evidence)=tokio::task::spawn_blocking(move||fixed_body(&path,&sha,&mapping,&actual_request)).await.map_err(|_|ResearchError::upstream())?.map_err(|error|ResearchError::new(409,format!("固定原文读取失败：{error}")))?;
            (body,evidence,"fixed_original_body")
        } else {
        if read_only || self.0.environment.get("TRACEFANG_ACQUISITION_ENABLED").is_some_and(|v|v=="0") || self.0.environment.contains_key("TRACEFANG_SOURCE_PERIOD_FIXTURE_SHA256") {return Err(ResearchError::new(409,"只读核验模式未配置来源五分钟固定输入，本次未请求上游。"))}
        let base=self.0.environment.get("TRACEFANG_FUYAO_BASE_URL").map(String::as_str).unwrap_or("https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1");
        let url=format!("{}/single_kline",base.trim_end_matches('/'));
        let requested_at=Utc::now();
        let mut response=self.0.http.post(&url).header("Referer","https://goodsfu.10jqka.com.cn/").json(&request).send().await.map_err(|_|ResearchError::new(502,"来源五分钟连接失败，请重试。"))?;
        let status=response.status().as_u16();
        if status!=200{return Err(ResearchError::new(502,format!("来源五分钟请求失败（HTTP {status}）。")))}
        if response.content_length().is_some_and(|n|n>MAX_BODY as u64){return Err(ResearchError::new(502,"来源五分钟响应超过读取上限。"))}
        let mut body=Vec::new();
        while let Some(chunk)=response.chunk().await.map_err(|_|ResearchError::new(502,"来源五分钟响应读取失败。"))? {
            if body.len().checked_add(chunk.len()).is_none_or(|n|n>MAX_BODY){return Err(ResearchError::new(502,"来源五分钟响应超过读取上限。"))}
            body.extend_from_slice(&chunk);
        }
        let received_at=Utc::now();
        let proof=json!({"url":url,"request":request,"http_status":status,"requested_at":requested_at,"received_at":received_at,
            "receive_clock_role":"actual acquisition completion; not source observation or finality",
            "body_sha256":hex::encode(Sha256::digest(&body)),"body_bytes":body.len()});
        (body,proof,"live")
        };
        evidence["body_base64"]=json!(STANDARD.encode(&body));evidence["capture_position"]=Value::Null;
        let payload:Value=serde_json::from_slice(&body).map_err(|_|ResearchError::new(502,"来源五分钟响应格式无效。"))?;
        let parsed=fuyao::parse_reported_five_minutes(&payload,feed,&request).map_err(|error|ResearchError::new(502,format!("来源五分钟记录校验失败：{error}")))?;
        let packet=json!({"schema":"source-period-reference-v1","code":definition.code,"name":definition.name,
            "source_id":"tonghuashun_futures","protocol":feed.protocol,"source_instrument":{"market":feed.market,"code":feed.code},
            "requested_source_period":"min_5","adjust_type":"actual","rows":parsed["rows"],
            "source_response_state":parsed["source_response_state"],"source_delay":parsed["source_delay"],
            "quantity_unit":null,"turnover_unit":null,"canonical_bar":false,"authority_snapshot_id":null,
            "source_evidence":evidence,"delivery_mode":delivery_mode,"reference_loaded_at":Utc::now(),
            "archive_semantics":"immutable source response and receipt; not canonical facts or complete revision history"});
        let root=self.0.cache_dir.join("source-period-reference");
        let saved=packet.clone();
        let id=tokio::task::spawn_blocking(move||publish_reference(&root,&saved)).await.map_err(|_|ResearchError::new(507,"来源原文归档失败，本次不返回已保存版本。"))?.map_err(|error|ResearchError::new(507,format!("来源原文归档失败：{error}")))?;
        Ok(with_reference(packet,&id))
    }
    pub async fn source_period_reference(&self,code:&str,id:&str)->Result<Value> {
        let root=self.0.cache_dir.join("source-period-reference");let id=id.to_owned();
        let (packet,id)=tokio::task::spawn_blocking(move||read_reference(&root,&id).map(|value|(value,id))).await.map_err(|_|ResearchError::upstream())?.map_err(|_|ResearchError::new(404,"来源参考不存在或完整性校验未通过。"))?;
        if packet["code"]!=code{return Err(ResearchError::invalid("来源参考与当前品种不一致。"))}
        Ok(with_reference(packet,&id))
    }
}

#[cfg(test)]mod tests {
    use super::*;
    #[test]fn archive_preserves_receipt_body_and_rejects_tampering() {
        let root=tempfile::tempdir().unwrap();let body=b"{\"source\":\"0.0000000000000000000000000001\"}";
        let packet=json!({"code":"FIXED","source_evidence":{"received_at":"2026-10-04T08:00:00Z","body_base64":STANDARD.encode(body),"body_sha256":hex::encode(Sha256::digest(body))}});
        let id=publish_reference(root.path(),&packet).unwrap();assert_eq!(read_reference(root.path(),&id).unwrap(),packet);
        let mut later=packet.clone();later["source_evidence"]["received_at"]=json!("2026-10-04T08:01:00Z");let newer=publish_reference(root.path(),&later).unwrap();assert_ne!(newer,id);assert_eq!(read_reference(root.path(),&id).unwrap(),packet);
        fs::write(root.path().join(format!("{id}.json")),b"{}").unwrap();assert!(read_reference(root.path(),&id).is_err());assert!(read_reference(root.path(),"../unknown").is_err());
    }
    #[test]fn fixed_original_input_keeps_old_receipt_and_refuses_unlisted_request() {
        let path=std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/source-period-min5-v1.json");
        let sha=hex::encode(Sha256::digest(fs::read(&path).unwrap()));
        let catalog=crate::catalog::Catalog::embedded().unwrap();let feed=catalog.items.iter().filter_map(|d|d.public_feed.as_ref()).find(|f|f.market=="129"&&f.code=="IC2612").unwrap();
        let request=fuyao::five_minute_request(feed,100,0);let (body,receipt)=fixed_body(&path,&sha,feed,&request).unwrap();
        assert_eq!(receipt["received_at"],"2026-10-03T22:16:02.966108+00:00");assert_eq!(receipt["request"],request);assert_eq!(receipt["body_bytes"],body.len());
        assert!(fixed_body(&path,&"0".repeat(64),feed,&request).is_err());assert!(fixed_body(&path,&sha,feed,&fuyao::five_minute_request(feed,1,0)).is_err());
    }
}
