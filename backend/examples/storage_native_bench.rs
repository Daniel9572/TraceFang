//! Isolated redb candidate. Does not import or change the production Store.
//! Usage: storage_native_bench --validate-only STORAGE_DIR
//!        storage_native_bench STORAGE_DIR CACHE_DIR REPORT.json
use anyhow::{Context, Result, bail, ensure};
use redb::{
    Database, Durability, ReadableDatabase, ReadableTable, ReadableTableMetadata, TableDefinition,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{BufRead, BufReader, Write},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Instant,
};

const FACTS: TableDefinition<&[u8], &[u8]> = TableDefinition::new("bars");
const STATES: TableDefinition<&[u8], &[u8]> = TableDefinition::new("latest_states");
const TICKS: TableDefinition<u64, &[u8]> = TableDefinition::new("events");
const EVENT_INDEX: TableDefinition<&[u8], u64> = TableDefinition::new("source_event_index");
const META: TableDefinition<&str, u64> = TableDefinition::new("committed_input");
const BASE: u64 = 1759449600000000;
type Row = Vec<Option<String>>;

/// Exact fixture decimals, including values wider than rust_decimal's 96 bits.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Exact {
    coefficient: i128,
    scale: u32,
}
impl Exact {
    fn parse(value: &str) -> Result<Self> {
        let (mantissa, exponent) = value.split_once(['e', 'E']).unwrap_or((value, "0"));
        let exponent: i32 = exponent.parse()?;
        let negative = mantissa.starts_with('-');
        let mantissa = mantissa.trim_start_matches(['-', '+']);
        let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
        ensure!(
            !whole.is_empty()
                && whole
                    .bytes()
                    .chain(fraction.bytes())
                    .all(|b| b.is_ascii_digit()),
            "invalid decimal {value}"
        );
        let mut coefficient: i128 = format!("{whole}{fraction}").parse()?;
        if negative {
            coefficient = -coefficient
        }
        let scale = fraction.len() as i32 - exponent;
        if scale < 0 {
            coefficient = coefficient
                .checked_mul(
                    10i128
                        .checked_pow((-scale) as u32)
                        .context("decimal exponent overflow")?,
                )
                .context("decimal overflow")?;
        }
        let mut out = Self {
            coefficient,
            scale: scale.max(0) as u32,
        };
        ensure!(out.scale <= 38, "fixture scale exceeds i128 contract");
        while out.scale > 0 && out.coefficient % 10 == 0 {
            out.coefficient /= 10;
            out.scale -= 1
        }
        Ok(out)
    }
    fn rescaled(self, scale: u32) -> Result<i128> {
        if self.coefficient == 0 {
            return Ok(0);
        }
        self.coefficient
            .checked_mul(
                10i128
                    .checked_pow(scale - self.scale)
                    .context("exact scale overflow")?,
            )
            .context("exact mantissa overflow")
    }
    fn add(self, other: Self) -> Result<Self> {
        let scale = self.scale.max(other.scale);
        let coefficient = self
            .rescaled(scale)?
            .checked_add(other.rescaled(scale)?)
            .context("exact aggregate overflow")?;
        Ok(Self { coefficient, scale })
    }
    fn compare(self, other: Self) -> Result<std::cmp::Ordering> {
        let scale = self.scale.max(other.scale);
        Ok(self.rescaled(scale)?.cmp(&other.rescaled(scale)?))
    }
    fn text(mut self) -> String {
        while self.scale > 0 && self.coefficient % 10 == 0 {
            self.coefficient /= 10;
            self.scale -= 1;
        }
        if self.scale == 0 {
            return self.coefficient.to_string();
        }
        let mut digits = self.coefficient.unsigned_abs().to_string();
        let scale = self.scale as usize;
        if digits.len() <= scale {
            digits = format!("{}{}", "0".repeat(scale + 1 - digits.len()), digits)
        }
        digits.insert(digits.len() - scale, '.');
        if self.coefficient < 0 {
            digits.insert(0, '-')
        }
        digits
    }
}
fn normalized(record: csv::StringRecord) -> Result<Row> {
    ensure!(
        record.len() == 12 || record.len() == 6,
        "wrong fixture column count"
    );
    record
        .iter()
        .enumerate()
        .map(|(i, value)| {
            if value.is_empty() {
                Ok(None)
            } else if i < 2 || (record.len() == 12 && i == 10) {
                Ok(Some(value.to_owned()))
            } else {
                Ok(Some(Exact::parse(value)?.text()))
            }
        })
        .collect()
}
fn cell(row: &Row, i: usize) -> &str {
    row[i].as_deref().expect("required fixture field")
}
fn number(row: &Row, i: usize) -> Result<u64> {
    Ok(cell(row, i).parse()?)
}
fn event_key(source: &str, symbol: &str, ordinal: u64) -> Vec<u8> {
    let mut out = format!("{source}\0{symbol}\0").into_bytes();
    out.extend_from_slice(&ordinal.to_be_bytes());
    out
}
fn key<T: TryInto<i64>>(source: &str, symbol: &str, time: T) -> Vec<u8> {
    let time = time
        .try_into()
        .ok()
        .expect("timestamp exceeds signed i64 domain");
    event_key(source, symbol, (time as u64) ^ (1u64 << 63))
}
fn scope(row: &Row) -> Vec<u8> {
    format!("{}\0{}\0", cell(row, 1), cell(row, 0)).into_bytes()
}
/// Compact versioned rows: explicit NULL bitmap, integer times/cursors,
/// length-prefixed UTF-8 identities/state, and signed coefficient + scale.
/// Common coefficients use i64; wider values use i128 without conversion.
fn encode(row: &Row) -> Result<Vec<u8>> {
    ensure!(row.len() == 6 || row.len() == 12, "unsupported row width");
    let mut out = vec![row.len() as u8];
    let mask = row.iter().enumerate().fold(0u16, |mask, (i, v)| {
        mask | if v.is_none() { 1 << i } else { 0 }
    });
    out.extend_from_slice(&mask.to_le_bytes());
    for (i, value) in row.iter().enumerate() {
        let Some(value) = value else { continue };
        if i < 2 || (row.len() == 12 && i == 10) {
            let size: u16 = value.len().try_into()?;
            out.extend_from_slice(&size.to_le_bytes());
            out.extend_from_slice(value.as_bytes());
        } else if i == 2 || (row.len() == 12 && i == 11) {
            out.extend_from_slice(&value.parse::<i64>()?.to_le_bytes());
        } else if (row.len() == 12 && [8, 9].contains(&i)) || (row.len() == 6 && i == 3) {
            out.extend_from_slice(&value.parse::<u64>()?.to_le_bytes());
        } else {
            let value = Exact::parse(value)?;
            if let Ok(narrow) = i64::try_from(value.coefficient) {
                out.push(value.scale as u8);
                out.extend_from_slice(&narrow.to_le_bytes());
            } else {
                out.push(value.scale as u8 | 128);
                out.extend_from_slice(&value.coefficient.to_le_bytes());
            }
        }
    }
    Ok(out)
}
fn take<const N: usize>(data: &mut &[u8]) -> Result<[u8; N]> {
    ensure!(data.len() >= N, "truncated compact row");
    let (value, rest) = data.split_at(N);
    *data = rest;
    Ok(value.try_into()?)
}
fn decode(mut data: &[u8]) -> Result<Row> {
    let width = take::<1>(&mut data)?[0] as usize;
    ensure!(width == 6 || width == 12, "unknown compact row version");
    let mask = u16::from_le_bytes(take(&mut data)?);
    let mut row = Vec::with_capacity(width);
    for i in 0..width {
        let value = if mask & (1 << i) != 0 {
            None
        } else if i < 2 || (width == 12 && i == 10) {
            let size = u16::from_le_bytes(take(&mut data)?) as usize;
            ensure!(data.len() >= size, "truncated compact string");
            let (value, rest) = data.split_at(size);
            data = rest;
            Some(std::str::from_utf8(value)?.to_owned())
        } else if i == 2 || (width == 12 && i == 11) {
            Some(i64::from_le_bytes(take(&mut data)?).to_string())
        } else if (width == 12 && [8, 9].contains(&i)) || (width == 6 && i == 3) {
            Some(u64::from_le_bytes(take(&mut data)?).to_string())
        } else {
            let tag = take::<1>(&mut data)?[0];
            let coefficient = if tag & 128 == 0 {
                i64::from_le_bytes(take(&mut data)?) as i128
            } else {
                i128::from_le_bytes(take(&mut data)?)
            };
            ensure!(tag & 127 <= 38, "compact decimal scale exceeds contract");
            Some(
                Exact {
                    coefficient,
                    scale: (tag & 127) as u32,
                }
                .text(),
            )
        };
        row.push(value);
    }
    ensure!(data.is_empty(), "trailing compact row bytes");
    Ok(row)
}
fn hash_rows(rows: impl IntoIterator<Item = Row>) -> Result<String> {
    let mut hash = Sha256::new();
    for row in rows {
        hash.update(serde_json::to_vec(&row)?);
        hash.update(b"\n");
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn csv_rows(path: &Path) -> Result<impl Iterator<Item = Result<Row>>> {
    let reader = csv::ReaderBuilder::new()
        .has_headers(false)
        .from_path(path)?;
    Ok(reader.into_records().map(|r| normalized(r?)))
}
fn fixture_hash(path: &Path) -> Result<(u64, String)> {
    let mut hash = Sha256::new();
    let mut count = 0;
    for row in csv_rows(path)? {
        hash.update(serde_json::to_vec(&row?)?);
        hash.update(b"\n");
        count += 1
    }
    Ok((count, format!("{:x}", hash.finalize())))
}
fn create(path: &Path) -> Result<Database> {
    let db = Database::create(path)?;
    let mut tx = db.begin_write()?;
    tx.set_durability(Durability::Immediate)?;
    {
        tx.open_table(FACTS)?;
        tx.open_table(STATES)?;
        tx.open_table(TICKS)?;
        tx.open_table(EVENT_INDEX)?;
        tx.open_table(META)?.insert("cursor", 0)?;
    }
    tx.commit()?;
    Ok(db)
}
fn commit(db: &Database, rows: &[Row], ticks: bool, cursor: u64) -> Result<u64> {
    let mut tx = db.begin_write()?;
    tx.set_durability(Durability::Immediate)?;
    let mut accepted = 0;
    {
        let mut meta = tx.open_table(META)?;
        if ticks {
            let mut facts = tx.open_table(TICKS)?;
            let mut index = tx.open_table(EVENT_INDEX)?;
            for row in rows {
                let event = number(row, 3)?;
                if facts.get(event)?.is_some() {
                    continue;
                }
                let data = encode(row)?;
                facts.insert(event, data.as_slice())?;
                index.insert(
                    event_key(cell(row, 1), cell(row, 0), event).as_slice(),
                    event,
                )?;
                accepted += 1;
            }
        } else {
            let mut facts = tx.open_table(FACTS)?;
            let mut states = tx.open_table(STATES)?;
            for row in rows {
                let k = key(cell(row, 1), cell(row, 0), cell(row, 2).parse::<i64>()?);
                if let Some(old) = facts.get(k.as_slice())? {
                    let old = decode(old.value())?;
                    if (number(row, 8)?, number(row, 9)?) <= (number(&old, 8)?, number(&old, 9)?) {
                        continue;
                    }
                }
                let data = encode(row)?;
                facts.insert(k.as_slice(), data.as_slice())?;
                accepted += 1;
                let state_key = scope(row);
                let replace = states
                    .get(state_key.as_slice())?
                    .map(|old| decode(old.value()))
                    .transpose()?
                    .is_none_or(|old| {
                        cell(row, 2).parse::<i64>().unwrap()
                            >= cell(&old, 2).parse::<i64>().unwrap()
                    });
                if replace {
                    states.insert(state_key.as_slice(), data.as_slice())?;
                }
            }
        }
        let previous = meta.get("cursor")?.map(|v| v.value()).unwrap_or(0);
        meta.insert("cursor", cursor.max(previous))?;
    }
    tx.commit()?;
    Ok(accepted)
}
fn load(db: &Database, path: &Path, ticks: bool, offset: u64) -> Result<Value> {
    let started = Instant::now();
    let mut batch = vec![];
    let mut count = 0;
    let mut commits = vec![];
    for row in csv_rows(path)? {
        batch.push(row?);
        count += 1;
        if batch.len() == 1000 {
            let start = Instant::now();
            commit(db, &batch, ticks, offset + count)?;
            commits.push(start.elapsed().as_secs_f64() * 1000.);
            batch.clear();
        }
    }
    if !batch.is_empty() {
        let start = Instant::now();
        commit(db, &batch, ticks, offset + count)?;
        commits.push(start.elapsed().as_secs_f64() * 1000.);
    }
    Ok(
        json!({"rows":count,"batch_rows":1000,"wall_ms":started.elapsed().as_secs_f64()*1000.,"immediate_commit":stats(commits)}),
    )
}
#[derive(Default)]
struct Aggregate {
    time: u64,
    open: Option<Exact>,
    high: Option<Exact>,
    low: Option<Exact>,
    close: Option<Exact>,
    volume: Option<Exact>,
    known: u64,
    total: u64,
    revisions: u64,
    all_final: bool,
}
impl Aggregate {
    fn accept(&mut self, row: &Row) -> Result<()> {
        let open = Exact::parse(cell(row, 3))?;
        let high = Exact::parse(cell(row, 4))?;
        let low = Exact::parse(cell(row, 5))?;
        let close = Exact::parse(cell(row, 6))?;
        if self.total == 0 {
            self.time = number(row, 2)?;
            self.open = Some(open);
            self.high = Some(high);
            self.low = Some(low);
            self.all_final = true
        }
        if high.compare(self.high.unwrap())?.is_gt() {
            self.high = Some(high)
        }
        if low.compare(self.low.unwrap())?.is_lt() {
            self.low = Some(low)
        }
        self.close = Some(close);
        self.total += 1;
        self.revisions = self
            .revisions
            .checked_add(number(row, 8)?)
            .context("revision overflow")?;
        self.all_final &= cell(row, 10) == "final";
        if let Some(volume) = row[7].as_deref() {
            let volume = Exact::parse(volume)?;
            self.volume = Some(self.volume.map_or(Ok(volume), |old| old.add(volume))?);
            self.known += 1
        }
        Ok(())
    }
    fn row(&self, bucket: Option<u64>) -> Row {
        let mut out = vec![
            Some(bucket.unwrap_or(self.time).to_string()),
            self.open.map(Exact::text),
            self.high.map(Exact::text),
            self.low.map(Exact::text),
            self.close.map(Exact::text),
            self.volume.map(Exact::text),
        ];
        if bucket.is_some() {
            out.extend([
                Some(self.known.to_string()),
                Some(self.total.to_string()),
                Some(self.revisions.to_string()),
                Some(u8::from(self.all_final).to_string()),
            ])
        } else {
            out.extend([
                Some(self.revisions.to_string()),
                Some(self.total.to_string()),
                Some(self.known.to_string()),
                Some(u8::from(self.all_final).to_string()),
            ])
        }
        out
    }
}
fn daily_bounds(minutes: u64) -> (u64, u64) {
    let completed = minutes / 1440;
    (
        BASE + completed.saturating_sub(300) * 86400000000,
        BASE + completed * 86400000000,
    )
}
fn daily_aggregate(rows: impl IntoIterator<Item = Result<Row>>) -> Result<Vec<Row>> {
    let mut buckets = std::collections::BTreeMap::<u64, Aggregate>::new();
    for row in rows {
        let row = row?;
        let bucket = number(&row, 2)? / 86400000000 * 86400000000;
        buckets.entry(bucket).or_default().accept(&row)?
    }
    Ok(buckets
        .into_iter()
        .map(|(bucket, agg)| agg.row(Some(bucket)))
        .collect())
}
fn query(db: &Database, name: &str, index: usize, minutes: u64) -> Result<Vec<Row>> {
    let source = if index % 2 == 0 {
        "source_a"
    } else {
        "source_b"
    };
    let symbol = format!("SYM{index:03}");
    let tx = db.begin_read()?;
    if name == "latest_state" {
        let table = tx.open_table(STATES)?;
        let k = format!("{source}\0{symbol}\0");
        return table
            .get(k.as_bytes())?
            .map(|v| decode(v.value()))
            .transpose()?
            .map_or(Ok(vec![]), |row| Ok(vec![row]));
    }
    if name == "ordered_event_replay_1000" {
        let index = tx.open_table(EVENT_INDEX)?;
        let events = tx.open_table(TICKS)?;
        let mut rows = vec![];
        for value in index
            .range(
                event_key(source, &symbol, 1).as_slice()
                    ..=event_key(source, &symbol, u64::MAX).as_slice(),
            )?
            .take(1000)
        {
            let (_, id) = value?;
            rows.push(decode(
                events
                    .get(id.value())?
                    .context("index refers to missing event")?
                    .value(),
            )?);
        }
        return Ok(rows);
    }
    let table = tx.open_table(FACTS)?;
    if name == "complete_utc_days_300" {
        let (lo, hi) = daily_bounds(minutes);
        let rows = table
            .range(key(source, &symbol, lo).as_slice()..key(source, &symbol, hi).as_slice())?;
        return daily_aggregate(rows.map(|entry| decode(entry?.1.value())));
    }
    let (lo, hi, limit, reverse) = match name {
        "latest_300" => (0, i64::MAX as u64, 300, true),
        "exclusive_page_300" => (0, BASE + (minutes - 301) * 60000000, 300, true),
        "range_600" => (
            BASE + (minutes / 3) * 60000000,
            BASE + (minutes / 3 + 600) * 60000000,
            1000,
            false,
        ),
        _ => bail!("unknown workload"),
    };
    let low_key = key(
        source,
        &symbol,
        if name == "latest_300" || name == "exclusive_page_300" {
            i64::MIN
        } else {
            lo.try_into()?
        },
    );
    let mut high_key = key(source, &symbol, hi);
    if name == "latest_300" {
        high_key.push(0)
    }
    let range = table.range(low_key.as_slice()..high_key.as_slice())?;
    let mut rows = vec![];
    if reverse {
        for row in range.rev().take(limit) {
            rows.push(decode(row?.1.value())?);
        }
        rows.reverse()
    } else {
        for row in range.take(limit) {
            rows.push(decode(row?.1.value())?);
        }
    }
    Ok(rows)
}
fn full_hash(db: &Database, ticks: bool, symbols: usize) -> Result<String> {
    let tx = db.begin_read()?;
    let mut hash = Sha256::new();
    if ticks {
        let table = tx.open_table(TICKS)?;
        for entry in table.iter()? {
            let (_, value) = entry?;
            hash.update(serde_json::to_vec(&decode(value.value())?)?);
            hash.update(b"\n");
        }
    } else {
        let table = tx.open_table(FACTS)?;
        for index in 0..symbols {
            for source in ["source_a", "source_b"] {
                for entry in table.range(
                    key(source, &format!("SYM{index:03}"), i64::MIN).as_slice()
                        ..=key(source, &format!("SYM{index:03}"), i64::MAX).as_slice(),
                )? {
                    hash.update(serde_json::to_vec(&decode(entry?.1.value())?)?);
                    hash.update(b"\n");
                }
            }
        }
    }
    Ok(format!("{:x}", hash.finalize()))
}
fn stats(mut samples: Vec<f64>) -> Value {
    samples.sort_by(f64::total_cmp);
    let n = samples.len();
    if n == 0 {
        return json!({"n":0});
    }
    let p50 = if n % 2 == 0 {
        (samples[n / 2 - 1] + samples[n / 2]) / 2.
    } else {
        samples[n / 2]
    };
    json!({"n":n,"p50_ms":p50,"p95_ms":samples[((n as f64*0.95).ceil() as usize-1).min(n-1)],"min_ms":samples[0],"max_ms":samples[n-1],"samples_ms":samples})
}
fn crash_child(path: &Path, committed: bool) -> Result<()> {
    let db = Database::open(path)?;
    let mut tx = db.begin_write()?;
    tx.set_durability(Durability::Immediate)?;
    {
        let mut facts = tx.open_table(FACTS)?;
        let row: Row = serde_json::from_value(json!([
            "CRASH", "source_a", "1", "1", "1", "1", "1", null, "1", "1", "final", "2"
        ]))?;
        facts.insert(
            key("source_a", "CRASH", 1).as_slice(),
            encode(&row)?.as_slice(),
        )?;
        tx.open_table(META)?.insert("cursor", 99)?;
    }
    if committed {
        tx.commit()?;
    } else {
        std::mem::forget(tx);
    }
    println!("ready");
    std::io::stdout().flush()?;
    loop {
        std::thread::park();
    }
}
fn transaction_check(root: &Path) -> Result<Value> {
    let db = create(&root.join("transactions.redb"))?;
    let mut row: Row = serde_json::from_value(json!([
        "TX", "source_a", "1", "1", "1", "1", "1", null, "1", "1", "final", null
    ]))?;
    ensure!(
        commit(&db, &[row.clone()], false, 1)? == 1,
        "initial transaction failed"
    );
    let snapshot = db.begin_read()?;
    row[8] = Some("2".into());
    row[9] = Some("2".into());
    row[6] = Some("2".into());
    row[4] = Some("2".into());
    ensure!(
        commit(&db, &[row.clone()], false, 2)? == 1,
        "new revision was not accepted"
    );
    let old = snapshot.open_table(FACTS)?;
    let original = decode(
        old.get(key("source_a", "TX", 1).as_slice())?
            .context("snapshot fact missing")?
            .value(),
    )?;
    ensure!(
        cell(&original, 8) == "1"
            && snapshot.open_table(META)?.get("cursor")?.unwrap().value() == 1,
        "MVCC fact/cursor snapshot inconsistent"
    );
    drop(old);
    drop(snapshot);
    ensure!(
        commit(&db, &[row.clone()], false, 2)? == 0,
        "duplicate revision accepted"
    );
    let mut stale = row.clone();
    stale[8] = Some("1".into());
    stale[9] = Some(u64::MAX.to_string());
    ensure!(
        commit(&db, &[stale], false, 3)? == 0,
        "stale revision replaced fact"
    );
    let mut newer = row.clone();
    newer[8] = Some("3".into());
    newer[9] = Some("3".into());
    let mut invalid = newer.clone();
    invalid[2] = Some("2".into());
    invalid[3] = Some("invalid-number".into());
    ensure!(
        commit(&db, &[newer, invalid], false, 4).is_err(),
        "invalid projection should fail transaction"
    );
    let tx = db.begin_read()?;
    let facts = tx.open_table(FACTS)?;
    ensure!(
        decode(
            facts
                .get(key("source_a", "TX", 1).as_slice())?
                .unwrap()
                .value()
        )? == row
            && facts.len()? == 1
            && tx.open_table(META)?.get("cursor")?.unwrap().value() == 3,
        "failed batch changed fact or cursor"
    );
    drop(facts);
    drop(tx);
    let tick: Row = serde_json::from_value(json!([
        "TX",
        "source_a",
        "-1",
        "18446744073709551615",
        "0.1234567890123456789012345678",
        null
    ]))?;
    ensure!(
        commit(&db, &[tick.clone()], true, 4)? == 1 && commit(&db, &[tick.clone()], true, 4)? == 0,
        "event duplicate not idempotent"
    );
    let tx = db.begin_read()?;
    ensure!(
        decode(tx.open_table(TICKS)?.get(u64::MAX)?.unwrap().value())? == tick
            && tx.open_table(META)?.get("cursor")?.unwrap().value() == 4,
        "event/cursor roundtrip failed"
    );
    Ok(
        json!({"newer_revision":true,"stale_revision_rejected":true,"duplicate_fact_and_event_idempotent":true,"failed_batch_fact_and_cursor_rollback":true,"mvcc_snapshot_fact_and_cursor_consistent":true,"event_id_u64_max_roundtrip":true}),
    )
}
fn crash_check(root: &Path) -> Result<Value> {
    let signed_path = root.join("signed-epoch.redb");
    let db = create(&signed_path)?;
    let row: Row = serde_json::from_value(json!([
        "EPOCH", "source_a", "0", "1", "1", "1", "1", null, "1", "1", "final", null
    ]))?;
    let mut rows = vec![];
    for epoch in [-1i64, 0, 1] {
        let mut probe = row.clone();
        probe[2] = Some(epoch.to_string());
        rows.push(probe)
    }
    ensure!(
        commit(&db, &rows, false, 3)? == 3,
        "signed epoch commit failed"
    );
    drop(db);
    let db = Database::open(&signed_path)?;
    let tx = db.begin_read()?;
    let table = tx.open_table(FACTS)?;
    let stored = table
        .iter()?
        .map(|entry| decode(entry?.1.value()))
        .collect::<Result<Vec<_>>>()?;
    ensure!(stored == rows, "signed epoch durable order differs");
    drop(table);
    drop(tx);
    drop(db);
    for committed in [false, true] {
        let path = root.join(format!("crash-{committed}.redb"));
        drop(create(&path)?);
        let mut child = Command::new(std::env::current_exe()?)
            .arg("--crash-child")
            .arg(&path)
            .arg(if committed {
                "committed"
            } else {
                "uncommitted"
            })
            .stdout(Stdio::piped())
            .spawn()?;
        let mut line = String::new();
        BufReader::new(child.stdout.take().context("child stdout")?).read_line(&mut line)?;
        ensure!(line.trim() == "ready", "child was not ready");
        child.kill()?;
        child.wait()?;
        let db = Database::open(&path)?;
        let tx = db.begin_read()?;
        let present = tx
            .open_table(FACTS)?
            .get(key("source_a", "CRASH", 1).as_slice())?
            .is_some();
        let cursor = tx
            .open_table(META)?
            .get("cursor")?
            .context("missing cursor")?
            .value();
        ensure!(
            present == committed && cursor == if committed { 99 } else { 0 },
            "fact/cursor atomic crash recovery failed"
        );
    }
    Ok(
        json!({"signed_epoch_restart_order_passed":true,"sigkill_before_commit":"rollback fact and cursor","sigkill_after_immediate_commit":"fact and cursor retained","scope":"process kill only; not power-cut or disk-loss verification"}),
    )
}
fn validate(storage: &Path, manifest: &Value) -> Result<Value> {
    let mut report = json!({});
    for profile in ["scaled", "wide"] {
        for kind in ["bars", "ticks"] {
            let (count, hash) =
                fixture_hash(&storage.join(format!("fixtures/{profile}-{kind}.csv")))?;
            let hash_field = if kind == "bars" {
                "final_bars_sha256"
            } else {
                "ticks_sha256"
            };
            ensure!(
                manifest["profiles"][profile][hash_field] == hash,
                "fixture digest differs: {profile}/{kind}"
            );
            ensure!(
                manifest["profiles"][profile][kind].as_u64() == Some(count),
                "fixture row count differs"
            );
            report[profile][kind] = json!({"rows":count,"sha256":hash});
        }
    }
    for value in [
        "0.1234567890123456789012345678",
        "1234567890123456789.123456789012345678",
        "1E-18",
    ] {
        let exact = Exact::parse(value)?;
        ensure!(
            Exact::parse(&exact.text())? == exact,
            "decimal roundtrip changed value"
        );
    }
    let row: Row = serde_json::from_value(json!([
        "PRECISION",
        "source_a",
        "1759449600000000",
        "0.1234567890123456789012345678",
        "1234567890123456789.123456789012345678",
        "-0.000000000000000001",
        "0",
        null,
        "3",
        "18446744073709551615",
        "final",
        null
    ]))?;
    ensure!(
        decode(&encode(&row)?)? == row,
        "compact precision/NULL/u64 roundtrip failed"
    );
    let mut time_keys = vec![];
    for epoch in [-1i64, 0, 1] {
        let mut probe = row.clone();
        probe[2] = Some(epoch.to_string());
        probe[11] = Some(epoch.to_string());
        ensure!(
            decode(&encode(&probe)?)? == probe,
            "signed epoch roundtrip failed"
        );
        time_keys.push(key("source_a", "PRECISION", epoch));
    }
    ensure!(
        time_keys[0] < time_keys[1] && time_keys[1] < time_keys[2],
        "signed epoch key order failed"
    );
    report["encoding_probes"] = json!({"negative_zero_positive_epoch_roundtrip_and_order":true,"signed_i64_time":true,"unsigned_u64_sequence":true,"decimal_scale_28_and_coefficient_38_digits":true,"null":true});
    Ok(report)
}
fn main() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.first().map(String::as_str) == Some("--crash-child") {
        return crash_child(Path::new(&args[1]), args[2] == "committed");
    }
    let validate_only = args.first().map(String::as_str) == Some("--validate-only");
    let storage = PathBuf::from(
        args.get(if validate_only { 1 } else { 0 })
            .context("provide STORAGE_DIR [CACHE_DIR REPORT.json]")?,
    );
    let manifest: Value = serde_json::from_slice(&fs::read(storage.join("manifest.json"))?)?;
    let fixture = validate(&storage, &manifest)?;
    let epsilon: rust_decimal::Decimal = "0.0000000000000000000000000001".parse()?;
    let derived = rust_decimal::Decimal::MAX
        .checked_add(epsilon)
        .map(|v| v.to_string());
    let arithmetic = json!({"kernel":"rust_decimal 96-bit coefficient","max":rust_decimal::Decimal::MAX.to_string(),"epsilon":epsilon.to_string(),"checked_add_result":derived,"exact_mathematical_sum":format!("{}.0000000000000000000000000001",rust_decimal::Decimal::MAX),"checked_add_exact":derived.as_deref()==Some(format!("{}.0000000000000000000000000001",rust_decimal::Decimal::MAX).as_str()),"scope":"Independent arithmetic probe; compact storage roundtrip does not imply derived arithmetic is exact"});
    if validate_only {
        println!("{}", serde_json::to_string_pretty(&fixture)?);
        return Ok(());
    }
    let root = PathBuf::from(args.get(1).context("provide CACHE_DIR")?)
        .join(format!("redb-native-{}", uuid::Uuid::new_v4().simple()));
    fs::create_dir_all(&root)?;
    let mut report = json!({"candidate":"redb","version":"4.3.0","durability":"Immediate","batch_rows":1000,"default_cache_bytes":1073741824,"write_timing_scope":"Whole bounded transaction: begin, revision/duplicate gate, compact value encode, B+tree facts/state/index updates, cursor and Immediate commit; CSV parsing outside transaction samples; not fsync-only timing","key":"source, symbol, signed integer microsecond (sign-bit flipped ordered key) / unsigned event_id","numeric_encoding":"compact binary: explicit NULL bitmap, signed i64 timestamps, u64 cursor/event_id, length-prefixed strings, signed i64/i128 coefficient + u8 scale; no f64 fact conversion","timing_scope":"Rust same-process MVCC reads including binary decoding and all row fields materialized as exact canonical strings/NULL; full-row digests outside timings; warm samples; native macOS; cache-directory database outside iCloud","fixture":fixture,"arithmetic_probe":arithmetic,"profiles":{},"transactions":transaction_check(&root)?,"crash":crash_check(&root)?,"caveats":["Separate client language/protocol from Python SQL candidates; report condition, not global speed victory","No network, replicas, power-loss or hardware-failure acceptance","Raw capture payload and cross-storage transaction excluded","Full research scan and concurrent acquisition comparison are not implemented; complete 300 UTC-day aggregation is included"]});
    let names = [
        "latest_state",
        "latest_300",
        "exclusive_page_300",
        "range_600",
        "ordered_event_replay_1000",
    ];
    for profile in ["scaled", "wide"] {
        let dbpath = root.join(format!("{profile}.redb"));
        let db = create(&dbpath)?;
        let bars = load(
            &db,
            &storage.join(format!("fixtures/{profile}-bars.csv")),
            false,
            0,
        )?;
        let ticks = load(
            &db,
            &storage.join(format!("fixtures/{profile}-ticks.csv")),
            true,
            bars["rows"].as_u64().unwrap(),
        )?;
        ensure!(
            full_hash(&db, false, manifest["symbols"].as_u64().unwrap() as usize)?
                == manifest["profiles"][profile]["final_bars_sha256"]
                    .as_str()
                    .unwrap(),
            "stored bar digest differs"
        );
        ensure!(
            full_hash(&db, true, 0)?
                == manifest["profiles"][profile]["ticks_sha256"]
                    .as_str()
                    .unwrap(),
            "stored event digest differs"
        );
        {
            let tx = db.begin_read()?;
            let table = tx.open_table(FACTS)?;
            let mut count = 0;
            for row in table.range(
                key("source_b", "SYM000", i64::MIN).as_slice()
                    ..=key("source_b", "SYM000", i64::MAX).as_slice(),
            )? {
                let row = decode(row?.1.value())?;
                ensure!(
                    cell(&row, 0) == "SYM000" && cell(&row, 1) == "source_b" && row[7].is_none(),
                    "source or NULL isolation failed"
                );
                count += 1;
            }
            ensure!(count == 1440, "source-isolation probe missing rows");
        }
        let probe = csv_rows(&storage.join(format!("fixtures/{profile}-bars.csv")))?
            .next()
            .context("missing bar fixture")??;
        let cursor = bars["rows"].as_u64().unwrap() + ticks["rows"].as_u64().unwrap();
        ensure!(
            commit(&db, &[probe.clone()], false, cursor)? == 0,
            "duplicate replay mutated bar"
        );
        let mut stale = probe.clone();
        stale[8] = Some("2".into());
        stale[9] = Some("999999999".into());
        ensure!(
            commit(&db, &[stale], false, cursor)? == 0,
            "stale correction overwritten newer revision"
        );
        let mut expected: std::collections::BTreeMap<(usize, String), Vec<Row>> =
            Default::default();
        let mut source_rows = std::collections::BTreeMap::<usize, Vec<Row>>::new();
        let mut source_events = std::collections::BTreeMap::<usize, Vec<Row>>::new();
        let indices = [
            0usize,
            1,
            manifest["symbols"].as_u64().unwrap() as usize - 1,
        ];
        for row in csv_rows(&storage.join(format!("fixtures/{profile}-bars.csv")))? {
            let row = row?;
            for index in indices {
                if cell(&row, 0) == format!("SYM{index:03}")
                    && cell(&row, 1)
                        == if index % 2 == 0 {
                            "source_a"
                        } else {
                            "source_b"
                        }
                {
                    source_rows.entry(index).or_default().push(row.clone());
                }
            }
        }
        for row in csv_rows(&storage.join(format!("fixtures/{profile}-ticks.csv")))? {
            let row = row?;
            for index in indices {
                if cell(&row, 0) == format!("SYM{index:03}")
                    && cell(&row, 1)
                        == if index % 2 == 0 {
                            "source_a"
                        } else {
                            "source_b"
                        }
                {
                    let rows = source_events.entry(index).or_default();
                    if rows.len() < 1000 {
                        rows.push(row.clone());
                    }
                }
            }
        }
        let mut correctness = json!({});
        for index in indices {
            let rows = &source_rows[&index];
            let minutes = if index == 0 {
                manifest["hot_symbol_minutes"].as_u64().unwrap()
            } else {
                manifest["minutes_per_symbol"].as_u64().unwrap()
            };
            let cursor = BASE + (minutes - 301) * 60000000;
            let lo = BASE + (minutes / 3) * 60000000;
            let hi = lo + 600 * 60000000;
            for name in names {
                let wanted = match name {
                    "latest_state" => rows[rows.len() - 1..].to_vec(),
                    "latest_300" => rows[rows.len() - 300..].to_vec(),
                    "exclusive_page_300" => rows
                        .iter()
                        .filter(|r| number(r, 2).unwrap() < cursor)
                        .rev()
                        .take(300)
                        .cloned()
                        .collect::<Vec<_>>()
                        .into_iter()
                        .rev()
                        .collect(),
                    "range_600" => rows
                        .iter()
                        .filter(|r| {
                            let time = number(r, 2).unwrap();
                            lo <= time && time < hi
                        })
                        .cloned()
                        .collect(),
                    _ => source_events[&index].clone(),
                };
                let actual = query(&db, name, index, minutes)?;
                ensure!(
                    actual == wanted,
                    "workload differs: {profile}/{name}/{index}"
                );
                correctness[format!("{name}:{index}")] =
                    json!({"rows":actual.len(),"sha256":hash_rows(actual)?});
                expected.insert((index, name.to_owned()), wanted);
            }
        }
        let minutes = manifest["hot_symbol_minutes"].as_u64().unwrap();
        let (lo, hi) = daily_bounds(minutes);
        let wanted = daily_aggregate(
            source_rows[&0]
                .iter()
                .filter(|row| {
                    let time = number(row, 2).unwrap();
                    lo <= time && time < hi
                })
                .cloned()
                .map(Ok),
        )?;
        let actual = query(&db, "complete_utc_days_300", 0, minutes)?;
        ensure!(
            actual == wanted && actual.len() == 300,
            "complete UTC-day aggregation differs"
        );
        let oracle: Value =
            serde_json::from_slice(&fs::read(storage.join("long-period-oracle.json"))?)?;
        let oracle_rows: Vec<Row> =
            serde_json::from_value(oracle["oracle"][profile]["rows"].clone())?;
        ensure!(
            actual == oracle_rows,
            "independent shared 300-day oracle rows differ"
        );
        ensure!(
            hash_rows(actual.clone())?
                == oracle["oracle"][profile]["sha256"]
                    .as_str()
                    .context("missing day oracle digest")?,
            "independent shared day digest differs"
        );
        correctness["complete_utc_days_300:0"] = json!({"rows":actual.len(),"sha256":hash_rows(actual)?,"input_rows":432000,"lo_us":lo,"hi_us":hi,"fields":["bucket_us","first_open","max_high","min_low","last_close","known_volume_sum","known_volume_count","total_count","sum_revision","all_final"]});
        drop(source_rows);
        drop(source_events);
        drop(expected);
        let mut hot = json!({});
        let mut rotated = json!({});
        let samples = manifest["read_samples"].as_u64().unwrap() as usize;
        for name in names {
            for index in 0..3 {
                let minutes = if index == 0 {
                    manifest["hot_symbol_minutes"].as_u64().unwrap()
                } else {
                    manifest["minutes_per_symbol"].as_u64().unwrap()
                };
                std::hint::black_box(query(&db, name, index, minutes)?);
            }
            let mut times = vec![];
            let mut rotation = vec![];
            for _ in 0..samples {
                let at = Instant::now();
                std::hint::black_box(query(
                    &db,
                    name,
                    0,
                    manifest["hot_symbol_minutes"].as_u64().unwrap(),
                )?);
                times.push(at.elapsed().as_secs_f64() * 1000.);
            }
            for index in (0..samples).map(|i| i % manifest["symbols"].as_u64().unwrap() as usize) {
                let minutes = if index == 0 {
                    manifest["hot_symbol_minutes"].as_u64().unwrap()
                } else {
                    manifest["minutes_per_symbol"].as_u64().unwrap()
                };
                let at = Instant::now();
                std::hint::black_box(query(&db, name, index, minutes)?);
                rotation.push(at.elapsed().as_secs_f64() * 1000.);
            }
            hot[name] = stats(times);
            rotated[name] = stats(rotation);
        }
        std::hint::black_box(query(&db, "complete_utc_days_300", 0, minutes)?);
        let mut days = vec![];
        for _ in 0..30 {
            let at = Instant::now();
            std::hint::black_box(query(&db, "complete_utc_days_300", 0, minutes)?);
            days.push(at.elapsed().as_secs_f64() * 1000.);
        }
        hot["complete_utc_days_300"] = stats(days);
        report["profiles"][profile] = json!({"bars":bars,"ticks":ticks,"all_row_digests_passed":true,"source_isolation_and_null_passed":true,"duplicate_and_stale_revision_passed":true,"workloads":correctness,"hot_symbol_queries":hot,"rotated_queries":rotated,"database_bytes":fs::metadata(&dbpath)?.len()});
        eprintln!("{profile}: exact rows and 16 source-bound query checks passed");
    }
    let output = PathBuf::from(args.get(2).context("provide REPORT.json")?);
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?
    }
    fs::write(&output, serde_json::to_vec_pretty(&report)?)?;
    println!("{}", output.display());
    Ok(())
}
