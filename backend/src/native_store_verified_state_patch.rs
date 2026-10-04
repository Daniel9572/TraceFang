//! Narrow inactive clock-state repair. Fact, event and index tables are read only.
use super::*;
use crate::reducer::SeriesState;

/// These receipts can only be issued by Store's verifying transactions. A
/// generic metadata writer must not be able to manufacture their provenance.
pub(super) fn require_generic_metadata_key(namespace: &str, key: &str) -> Result<()> {
    ensure!(
        namespace != "migration"
            || !matches!(key, "index_verification" | "series_state_clock_repair"),
        "metadata key is reserved for Store verification transactions"
    );
    Ok(())
}

impl Store {
    /// Offline audits can use a small fixed page cache without materializing rows.
    pub fn open_read_only_bounded(path: impl AsRef<Path>, cache_bytes: usize) -> Result<Self> {
        ensure!(
            (16 * 1024 * 1024..=1024 * 1024 * 1024).contains(&cache_bytes),
            "offline read cache outside16MiB..1GiB"
        );
        let path = path.as_ref();
        let db = redb::Builder::new()
            .set_cache_size(cache_bytes)
            .open_read_only(path)?;
        let tx = db.begin_read()?;
        read_version(&tx, None)?;
        let replay = tx
            .open_table(GLOBAL)?
            .get("store_kind")?
            .is_some_and(|v| v.value() == "replay_derived_v1");
        drop(tx);
        Ok(Self {
            inner: Arc::new(Inner {
                db: Mutex::new(Some(DatabaseHandle::ReadOnly(Arc::new(db)))),
                writer: Arc::new(Semaphore::new(1)),
                readers: Arc::new(Semaphore::new(READERS as usize)),
                closed: AtomicBool::new(false),
                read_only: true,
                replay,
                path: path.to_owned(),
            }),
            generation: None,
        })
    }

    /// Rebind an already-issued complete fact/index proof only after this exact
    /// metadata-only transaction independently reconstructs all four states.
    /// The exact descriptor bytes close the untrusted caller's basis boundary.
    pub async fn import_verified_series_state_metadata(
        &self,
        rows: Vec<ImportMetadataRow>,
        expected_proof: Value,
        clock_manifest_bytes: Vec<u8>,
    ) -> Result<Value> {
        ensure!(
            self.generation.is_some() && !self.inner.replay,
            "clock-state repair requires inactive legacy staging"
        );
        ensure!(
            rows.len() == 4 && clock_manifest_bytes.len() <= 1024 * 1024,
            "clock-state repair accepts exactly4scope states and bounded descriptor"
        );
        let mapping_sha = hex::encode(Sha256::digest(&clock_manifest_bytes));
        let plan: Value = serde_json::from_slice(&clock_manifest_bytes)?;
        self.write(move|tx,current| {
            ensure!(current.committed_capture.is_none()&&tx.open_table(GLOBAL)?.get("active")?.context("active generation missing")?.value()!=current.active_generation,"clock-state repair refuses active/capture generation");
            require_current_index(current)?;
            let proof_key=named_key(&current.active_generation,"migration","index_verification");
            let saved:Value=serde_json::from_slice(tx.open_table(METADATA)?.get(proof_key.as_slice())?.context("initial complete verification required")?.value())?;
            ensure!(saved==expected_proof&&saved["complete"]==true&&saved["index_verified"]==true&&saved["verified_commit_id"]==current.commit_id.to_string()&&saved["store_epoch"]==current.store_epoch&&saved["generation"]==current.active_generation&&saved["schema_version"]==current.schema_version&&saved["aggregation_version"]==current.aggregation_version,"clock-state repair basis is stale, altered or from another version");
            let clock_key=named_key(&current.active_generation,"migration","source_clock_policy");
            let clock:Value=serde_json::from_slice(tx.open_table(METADATA)?.get(clock_key.as_slice())?.context("verified fixed clock projection missing")?.value())?;
            ensure!(clock["verified"]==true&&clock["policy"]==crate::source_clock::THS_V6_SHFE_END_V2&&clock["mapping_manifest_sha256"]==mapping_sha,"clock-state repair descriptor is not the stored verified clock basis");
            ensure!(plan["complete"]==true&&plan["schema"]=="legacy-source-clock-projection-v2"&&plan["source_manifest_id"]==clock["source_manifest_id"]&&plan["snapshot"]==clock["snapshot"]&&plan["policy_source_sha256"]==clock["policy_source_sha256"]&&plan["original_canonical_file_sha256"]==clock["original_archive_sha256"]&&plan["policy"]["policy"]["policy_id"]==crate::source_clock::THS_V6_SHFE_END_V2,"clock-state repair descriptor policy/snapshot differs");
            let canonical_sha=plan["sha256"].as_str().context("corrected canonical digest missing")?;
            ensure!(hex::decode(canonical_sha).is_ok_and(|v|v.len()==32),"corrected canonical digest invalid");
            let mut seen=std::collections::BTreeSet::new();let mut repaired=Vec::new();let before=current.clone();
            for row in rows {
                let state:SeriesState=serde_json::from_value(row.value.clone())?;
                let provider=crate::source_clock::VERIFIED_V6_SCOPES.iter().find(|(_,symbol)|*symbol==state.instrument_symbol).map(|v|v.0).context("clock-state repair outside4exact scope")?;
                ensure!(row.namespace=="series_state"&&row.key==format!("tonghuashun_futures:{}",state.instrument_symbol)&&state.realtime_source_id=="tonghuashun_futures"&&state.interval_seconds==60&&seen.insert(state.instrument_symbol.clone()),"clock-state repair key/identity/scope duplicated or differs");
                let state_key=named_key(&current.active_generation,"series_state",&row.key);
                let old:Value=serde_json::from_slice(tx.open_table(METADATA)?.get(state_key.as_slice())?.context("original runtime series state missing")?.value())?;
                ensure!(row.evidence["original_state"]==old,"clock-state repair original state differs from current metadata");
                let prefix=scope(&current.active_generation,"tonghuashun_futures",&state.instrument_symbol,60);let end=prefix_end(&prefix)?;
                let facts=tx.open_table(FACTS)?;let mut authority_count=0u64;let mut known=0u64;let mut unknown=0u64;
                let mut latest:Option<ImportBarRow>=None;let mut updated:Option<i64>=None;
                for item in facts.range(prefix.as_slice()..end.as_slice())? {
                    let(_,bytes)=item?;let fact=StoredBar::decode(bytes.value())?;let bar=fact.row;let proof=&bar.evidence["source_clock_projection"];
                    if proof["policy_id"]!=crate::source_clock::THS_V6_SHFE_END_V2 {continue;}
                    ensure!(proof["provider_code"]==provider&&proof["original_source_record"]["table"]=="candles"&&proof["original_source_record"]["sha256"].as_str().is_some_and(|v|hex::decode(v).is_ok_and(|v|v.len()==32))&&bar.evidence["fixed_snapshot"]["canonical_file_sha256"]==canonical_sha&&bar.evidence["fixed_snapshot"]["snapshot"]==clock["snapshot"],"clock authority fact has a different source basis");
                    authority_count+=1;updated=Some(updated.map_or(bar.received_at_ns,|v|v.max(bar.received_at_ns)));
                    if bar.state!="final" {continue;}
                    let Some(finalized)=bar.finalized_at_ns else{unknown+=1;continue};
                    ensure!(finalized>=bar.close_time_ns&&bar.close_time_ns==bar.open_time_ns.checked_add(60_000_000_000).context("clock interval overflow")?,"clock authority Final has no valid completed interval");
                    known+=1;if latest.as_ref().is_none_or(|old|bar.close_time_ns>old.close_time_ns){latest=Some(bar);}
                }
                drop(facts);let last=latest.context("scope has no independently proven Final authority")?;
                let derived=SeriesState{realtime_source_id:"tonghuashun_futures".into(),instrument_symbol:state.instrument_symbol.clone(),upstream_channel_id:last.evidence_channel_id,provider_symbol:last.source_metadata["provider_symbol"].as_str().context("authority provider identity missing")?.into(),interval_seconds:60,
                    latest_authoritative_open_time:Some(chrono::DateTime::from_timestamp_nanos(last.open_time_ns)),authoritative_through:chrono::DateTime::from_timestamp_nanos(last.close_time_ns),history_floor:None,tail_checked_through:None,tail_checked_at:None,evidence_version:format!("{}:{mapping_sha}",crate::source_clock::THS_V6_SHFE_END_V2),updated_at:chrono::DateTime::from_timestamp_nanos(updated.context("authority receive clock missing")?)};
                ensure!(state==derived,"clock-state candidate differs from exact fixed authority facts");
                let counts=&row.evidence["clock_state_derivation"];
                ensure!(counts["authority_rows"]==authority_count.to_string()&&counts["confirmed_authority_rows"]==known.to_string()&&counts["final_clock_unknown_rows"]==unknown.to_string(),"clock-state reported basis counts differ from native facts");
                let bytes=serde_json::to_vec(&state)?;tx.open_table(METADATA)?.insert(state_key.as_slice(),bytes.as_slice())?;
                let evidence_key=named_key(&current.active_generation,"metadata_evidence",&serde_json::to_string(&("series_state",&row.key))?);
                let bytes=serde_json::to_vec(&json!({"evidence":row.evidence,"original_store_state":old,"basis_clock_manifest_sha256":mapping_sha,"independently_scanned_native_authority_rows":authority_count.to_string()}))?;tx.open_table(METADATA)?.insert(evidence_key.as_slice(),bytes.as_slice())?;
                repaired.push(json!({"key":row.key,"authority_rows":authority_count.to_string(),"confirmed_authority_rows":known.to_string(),"final_clock_unknown_rows":unknown.to_string(),"state":state}));
            }
            advance(tx,current)?;let mut proof=saved;proof["verified_commit_id"]=json!(current.commit_id.to_string());
            let bytes=serde_json::to_vec(&proof)?;tx.open_table(METADATA)?.insert(proof_key.as_slice(),bytes.as_slice())?;
            let receipt=json!({"schema":"verified-clock-series-state-repair-v1","complete":true,"before":before,"after":current,"proof":proof,"states":repaired,"clock_manifest_sha256":mapping_sha,"facts_quotes_index_written":false,"initial_full_verification_reused_only_for_metadata_transaction":true});
            let key=named_key(&current.active_generation,"migration","series_state_clock_repair");let bytes=serde_json::to_vec(&receipt)?;tx.open_table(METADATA)?.insert(key.as_slice(),bytes.as_slice())?;Ok(receipt)
        }).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn context(label: &str) -> ImportContext {
        ImportContext {
            origin_id: "state-fixture".into(),
            source_fingerprint: "a".repeat(64),
            schema_version: SCHEMA_VERSION.into(),
            range_label: label.into(),
            legacy_cursor: None,
            expected_sha256: None,
        }
    }
    async fn fixture() -> Result<(
        tempfile::TempDir,
        Store,
        Store,
        Value,
        Vec<ImportMetadataRow>,
        Vec<u8>,
        ImportBarRow,
    )> {
        let directory = tempfile::tempdir()?;
        let store = Store::open(directory.path().join("facts.redb"))?;
        let stage = store.staging("clock-inactive").await?;
        let plan = json!({"schema":"legacy-source-clock-projection-v2","complete":true,"source_manifest_id":"fixed-id","snapshot":"fixed-snapshot","policy_source_sha256":"d".repeat(64),"original_canonical_file_sha256":"e".repeat(64),"sha256":"f".repeat(64),"policy":{"policy":{"policy_id":crate::source_clock::THS_V6_SHFE_END_V2}}});
        let descriptor = serde_json::to_vec(&plan)?;
        let mapping = hex::encode(Sha256::digest(&descriptor));
        let clock = json!({"verified":true,"policy":crate::source_clock::THS_V6_SHFE_END_V2,"mapping_manifest_sha256":mapping,"source_manifest_id":"fixed-id","snapshot":"fixed-snapshot","policy_source_sha256":"d".repeat(64),"original_archive_sha256":"e".repeat(64)});
        let mut metadata = vec![ImportMetadataRow {
            namespace: "migration".into(),
            key: "source_clock_policy".into(),
            value: clock,
            evidence: json!({}),
        }];
        let mut rows = vec![];
        let mut patch = vec![];
        for (provider, symbol) in crate::source_clock::VERIFIED_V6_SCOPES {
            let old = SeriesState {
                realtime_source_id: "tonghuashun_futures".into(),
                instrument_symbol: symbol.into(),
                upstream_channel_id: "tonghuashun_futures".into(),
                provider_symbol: provider.into(),
                interval_seconds: 60,
                latest_authoritative_open_time: Some(chrono::DateTime::from_timestamp_nanos(
                    120_000_000_000,
                )),
                authoritative_through: chrono::DateTime::from_timestamp_nanos(180_000_000_000),
                history_floor: None,
                tail_checked_through: Some(chrono::DateTime::from_timestamp_nanos(180_000_000_000)),
                tail_checked_at: None,
                evidence_version: "old-open-clock".into(),
                updated_at: chrono::DateTime::from_timestamp_nanos(300_000_000_000),
            };
            let key = format!("tonghuashun_futures:{symbol}");
            metadata.push(ImportMetadataRow {
                namespace: "series_state".into(),
                key: key.clone(),
                value: json!(&old),
                evidence: json!({}),
            });
            for (at, state, finalized, received) in [
                (0, "final", Some(60_000_000_000), 61_000_000_000),
                (60_000_000_000, "final", None, 130_000_000_000),
                (
                    120_000_000_000,
                    "provisional_authoritative",
                    None,
                    190_000_000_000,
                ),
                (
                    180_000_000_000,
                    "final",
                    Some(240_000_000_000),
                    240_000_000_000,
                ),
            ] {
                let source_clock = if at == 180_000_000_000 {
                    Value::Null
                } else {
                    json!({"policy_id":crate::source_clock::THS_V6_SHFE_END_V2,"provider_code":provider,"original_source_record":{"table":"candles","sha256":"c".repeat(64)}})
                };
                rows.push(ImportBarRow{instrument_symbol:symbol.into(),realtime_source_id:"tonghuashun_futures".into(),evidence_channel_id:"tonghuashun_futures".into(),interval_seconds:60,open_time_ns:at,close_time_ns:at+60_000_000_000,open:"1".into(),high:"1".into(),low:"1".into(),close:"1".into(),volume:None,revision:1,received_sequence:None,state:state.into(),finalized_at_ns:finalized,source_observed_at_ns:at+60_000_000_000,received_at_ns:received,source_metadata:json!({"provider_symbol":provider}),evidence:json!({"semantics":"final_revision_history","finalization_time_unknown":finalized.is_none()&&state=="final","table":"candles","fixed_snapshot":{"source_fingerprint":"a".repeat(64),"table_sha256":"c".repeat(64),"snapshot":"fixed-snapshot","canonical_file_sha256":"f".repeat(64)},"source_clock_projection":source_clock})});
            }
            let mut candidate = old.clone();
            candidate.latest_authoritative_open_time =
                Some(chrono::DateTime::from_timestamp_nanos(0));
            candidate.authoritative_through =
                chrono::DateTime::from_timestamp_nanos(60_000_000_000);
            candidate.tail_checked_through = None;
            candidate.evidence_version =
                format!("{}:{mapping}", crate::source_clock::THS_V6_SHFE_END_V2);
            candidate.updated_at = chrono::DateTime::from_timestamp_nanos(190_000_000_000);
            patch.push(ImportMetadataRow{namespace:"series_state".into(),key,value:json!(candidate),evidence:json!({"original_state":old,"clock_state_derivation":{"authority_rows":"3","confirmed_authority_rows":"1","final_clock_unknown_rows":"1"}})});
        }
        let changed = rows[0].clone();
        stage
            .import_bars(ImportBatch {
                context: context("facts"),
                row_offset: 0,
                rows,
            })
            .await?;
        stage
            .import_metadata(ImportBatch {
                context: context("metadata"),
                row_offset: 0,
                rows: metadata,
            })
            .await?;
        let proof = stage.verify_staging().await?;
        Ok((directory, store, stage, proof, patch, descriptor, changed))
    }
    #[tokio::test]
    async fn verified_state_patch_is_atomic_exact_and_never_promotes_preview_unknown_or_quote(
    ) -> Result<()> {
        let (dir, store, stage, proof, patch, descriptor, _) = fixture().await?;
        let before = store.version().await?;
        let receipt = stage
            .import_verified_series_state_metadata(patch.clone(), proof.clone(), descriptor)
            .await?;
        ensure!(
            receipt["proof"]["fact_codec_sha256"] == proof["fact_codec_sha256"]
                && receipt["proof"]["index_codec_sha256"] == proof["index_codec_sha256"],
            "metadata repair changed fact/index bytes"
        );
        ensure!(
            store.version().await? == before,
            "inactive repair changed live authority"
        );
        for row in patch {
            ensure!(
                stage.metadata("series_state", &row.key).await? == Some(row.value),
                "repaired state differs"
            );
        }
        let after = stage.verify_index().await?;
        ensure!(
            after["fact_codec_sha256"] == proof["fact_codec_sha256"]
                && after["index_codec_sha256"] == proof["index_codec_sha256"],
            "independent index verification changed after metadata repair"
        );
        store.close().await?;
        let reopened =
            Store::open_read_only_bounded(dir.path().join("facts.redb"), 16 * 1024 * 1024)?
                .read_generation("clock-inactive")
                .await?;
        ensure!(
            reopened.metadata("migration", "index_verification").await?
                == Some(receipt["proof"].clone()),
            "rebound proof not durable after reopen"
        );
        reopened.close().await?;
        Ok(())
    }
    #[tokio::test]
    async fn verified_state_patch_rejects_altered_proof_descriptor_candidate_and_rolls_back(
    ) -> Result<()> {
        let (_dir, store, stage, proof, patch, descriptor, _) = fixture().await?;
        let before = stage.version().await?;
        for key in [
            "verified_commit_id",
            "store_epoch",
            "schema_version",
            "aggregation_version",
            "fact_codec_sha256",
            "generation",
        ] {
            let mut wrong = proof.clone();
            wrong[key] = json!("altered");
            ensure!(
                stage
                    .import_verified_series_state_metadata(patch.clone(), wrong, descriptor.clone())
                    .await
                    .is_err(),
                "altered proof accepted: {key}"
            );
        }
        let mut wrong = descriptor.clone();
        wrong.push(b' ');
        ensure!(
            stage
                .import_verified_series_state_metadata(patch.clone(), proof.clone(), wrong)
                .await
                .is_err(),
            "changed clock descriptor accepted"
        );
        // First valid state would have been written before the later invalid
        // scope: the failed transaction must preserve all original metadata.
        let mut wrong = patch.clone();
        wrong[3].value["authoritative_through"] = json!("1970-01-01T00:03:00Z");
        ensure!(
            stage
                .import_verified_series_state_metadata(wrong, proof.clone(), descriptor.clone())
                .await
                .is_err(),
            "invented later authority accepted"
        );
        let mut wrong = patch.clone();
        wrong[3].evidence["clock_state_derivation"]["authority_rows"] = json!("4");
        ensure!(
            stage
                .import_verified_series_state_metadata(wrong, proof, descriptor)
                .await
                .is_err(),
            "wrong basis count accepted"
        );
        ensure!(
            stage.version().await? == before,
            "failed metadata patch committed version"
        );
        for row in patch {
            ensure!(
                stage.metadata("series_state", &row.key).await?
                    == Some(row.evidence["original_state"].clone()),
                "partial metadata mutation escaped rollback"
            );
        }
        store.close().await?;
        Ok(())
    }
    #[tokio::test]
    async fn verified_state_patch_rejects_intervening_fact_commit() -> Result<()> {
        let (_dir, store, stage, proof, patch, descriptor, mut changed) = fixture().await?;
        changed.revision = 2;
        changed.close = "2".into();
        changed.high = "2".into();
        stage
            .import_bars(ImportBatch {
                context: context("later-fact"),
                row_offset: 0,
                rows: vec![changed],
            })
            .await?;
        ensure!(
            stage
                .import_verified_series_state_metadata(patch, proof, descriptor)
                .await
                .is_err(),
            "old proof reused after a facts commit"
        );
        store.close().await?;
        Ok(())
    }

    #[tokio::test]
    async fn generic_metadata_cannot_forge_or_replace_store_verification_receipts() -> Result<()> {
        let (dir, store, stage, proof, _, _, _) = fixture().await?;
        let version = stage.version().await?;
        let file = dir.path().join("facts.redb");
        let before = Sha256::digest(std::fs::read(&file)?);
        for key in ["index_verification", "series_state_clock_repair"] {
            let old = stage.metadata("migration", key).await?;
            let mut forged = proof.clone();
            forged["verified_commit_id"] =
                json!(version.commit_id.checked_add(1).unwrap().to_string());
            ensure!(
                stage
                    .set_metadata("migration", key, forged.clone())
                    .await
                    .is_err(),
                "set_metadata forged {key}"
            );
            let replacement = forged.clone();
            ensure!(
                stage
                    .update_metadata_receipt("migration", key, move |_| Ok(replacement))
                    .await
                    .is_err(),
                "update_metadata_receipt forged {key}"
            );
            ensure!(
                stage
                    .import_metadata(ImportBatch {
                        context: context(&format!("forged-{key}")),
                        row_offset: 0,
                        rows: vec![ImportMetadataRow {
                            namespace: "migration".into(),
                            key: key.into(),
                            value: forged,
                            evidence: json!({})
                        }],
                    })
                    .await
                    .is_err(),
                "import_metadata forged {key}"
            );
            ensure!(
                stage.version().await? == version && stage.metadata("migration", key).await? == old,
                "rejected writer changed receipt/version"
            );
        }
        ensure!(
            Sha256::digest(std::fs::read(&file)?) == before,
            "rejected metadata writes changed persistent bytes"
        );
        // The restriction is deliberately limited to the two Store receipts.
        stage
            .set_metadata(
                "migration",
                "source_clock_policy",
                json!({"ordinary_metadata":true}),
            )
            .await?;
        store.close().await?;
        Ok(())
    }
}
