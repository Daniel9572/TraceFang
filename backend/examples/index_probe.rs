//! Diagnose/rebuild only an explicitly named inactive fact generation.
use anyhow::{Context,Result};
use tracefang_core::native_store::Store;
#[tokio::main]
async fn main()->Result<()> {
    let args:Vec<_>=std::env::args().collect();
    let repair=args.get(3).is_some_and(|v|v=="rebuild-inactive");
    let path=args.get(1).context("existing facts path required")?;
    let store=if repair {Store::open(path)?}else{Store::open_read_only(path)?};
    let generation=store.read_generation(args.get(2).context("existing generation required")?).await?;
    let result:Result<serde_json::Value>=if args.get(3).is_some_and(|v|v=="budget-inactive") {
        Ok(serde_json::json!({"active_version":store.version().await?,"staged_version":generation.version().await?,"space":generation.index_space_usage().await?}))
    }else if repair {
        // An older derived codec cannot pass the new byte-for-byte verifier.
        // Rebuild hashes untouched facts before rewriting any derived node;
        // compare that fresh digest with the stored old proof and new verifier.
        let before=generation.metadata("migration","index_verification").await?.context("existing verified staging proof missing")?;
        let before_version=generation.version().await?;
        anyhow::ensure!(before["complete"]==true && before["verified_commit_id"]==before_version.commit_id.to_string(),"staged proof is stale");
        let active_before=store.version().await?;
        eprintln!("rebuild_started: fresh fact digest before replacing derived nodes");
        let rebuilt=generation.rebuild_staging_index().await?;
        eprintln!("rebuild_committed: verifying new derived codec against exact facts");
        let verified=generation.verify_staging().await?;
        anyhow::ensure!(rebuilt["fact_codec_sha256"]==verified["fact_codec_sha256"],"fact bytes changed during index-only repair");
        anyhow::ensure!(before["fact_codec_sha256"]==verified["fact_codec_sha256"],"fact bytes differ before and after repair");
        anyhow::ensure!(store.version().await?==active_before,"active generation changed during inactive repair");
        Ok(serde_json::json!({"before":before,"before_version":before_version,"fresh_fact_hash_before_node_rewrite":rebuilt["fact_codec_sha256"],"active_before":active_before,"rebuild":rebuilt,"verified":verified,"active_after":store.version().await?,"fact_bytes_unchanged":true,"activation":false}))
    }else{Ok(serde_json::json!({"active_version":store.version().await?,"verified":generation.verify_index().await?}))};
    store.close().await?;
    println!("{}",serde_json::to_string_pretty(&result?)?);Ok(())
}
