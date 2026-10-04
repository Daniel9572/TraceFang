//! Versioned binary facts: signed nanoseconds, unsigned identities and exact decimals.
use crate::{domain::Decimal, persistence_contract::{CapturePosition, ImportBarRow, ImportQuoteRow}};
use anyhow::{Context, Result, ensure};
use num_bigint::BigInt;
use num_traits::ToPrimitive;
use serde_json::Value;

pub(crate) const MAX_METADATA_BYTES: usize = 1 << 20;

pub(crate) struct Encoder(pub Vec<u8>);
impl Encoder {
    pub fn new(version: u8) -> Self { Self(vec![version]) }
    pub fn u8(&mut self, v: u8) { self.0.push(v); }
    pub fn u32(&mut self, v: u32) { self.0.extend(v.to_le_bytes()); }
    pub fn u64(&mut self, v: u64) { self.0.extend(v.to_le_bytes()); }
    pub fn i64(&mut self, v: i64) { self.0.extend(v.to_le_bytes()); }
    pub fn bytes(&mut self, v: &[u8]) -> Result<()> {
        self.u32(v.len().try_into()?); self.0.extend(v); Ok(())
    }
    pub fn text(&mut self, v: &str) -> Result<()> { self.bytes(v.as_bytes()) }
    pub fn json(&mut self, v: &Value) -> Result<()> {
        let bytes=serde_json::to_vec(v)?;
        ensure!(bytes.len() <= MAX_METADATA_BYTES, "fact metadata exceeds 1 MiB; preserve payload in capture instead");
        self.bytes(&bytes)
    }
    pub fn decimal(&mut self, value: &Decimal) -> Result<()> {
        let (coefficient, scale)=value.coefficient_and_scale();
        ensure!(scale.unsigned_abs()<=crate::exact::INTERNAL_DECIMAL_LIMIT as u64 && value.coefficient_digits()<=crate::exact::INTERNAL_DECIMAL_LIMIT as u32,"decimal result exceeds canonical storage bound; no rounding applied");self.i64(scale);
        if let Some(v)=coefficient.to_i64() { self.u8(0); self.i64(v); }
        else if let Some(v)=coefficient.to_i128() { self.u8(1); self.0.extend(v.to_le_bytes()); }
        else { self.u8(2); self.bytes(&coefficient.to_signed_bytes_le())?; }
        Ok(())
    }
    pub fn decimal_text(&mut self, v: &str) -> Result<()> { self.decimal(&Decimal::from_str_exact(v)?) }
    pub fn optional_decimal(&mut self, v: &Option<String>) -> Result<()> {
        self.u8(u8::from(v.is_some())); if let Some(v)=v { self.decimal_text(v)?; } Ok(())
    }
    pub fn optional_u64(&mut self, v: Option<u64>) { self.u8(u8::from(v.is_some())); if let Some(v)=v { self.u64(v); } }
    pub fn optional_i64(&mut self, v: Option<i64>) { self.u8(u8::from(v.is_some())); if let Some(v)=v { self.i64(v); } }
    pub fn position(&mut self, v: &Option<CapturePosition>) -> Result<()> {
        self.u8(u8::from(v.is_some()));
        if let Some(v)=v { self.text(&v.epoch)?; self.u64(v.sequence); self.text(&v.digest)?; } Ok(())
    }
}
pub(crate) struct Decoder<'a>(pub &'a [u8]);
impl<'a> Decoder<'a> {
    pub fn new(data: &'a [u8], version: u8) -> Result<Self> {
        let mut out=Self(data); ensure!(out.u8()? == version, "unknown native codec version"); Ok(out)
    }
    fn take<const N: usize>(&mut self) -> Result<[u8; N]> {
        ensure!(self.0.len() >= N, "truncated native row"); let (v, rest)=self.0.split_at(N); self.0=rest; Ok(v.try_into()?)
    }
    pub fn u8(&mut self) -> Result<u8> { Ok(self.take::<1>()?[0]) }
    pub fn u32(&mut self) -> Result<u32> { Ok(u32::from_le_bytes(self.take()?)) }
    pub fn u64(&mut self) -> Result<u64> { Ok(u64::from_le_bytes(self.take()?)) }
    pub fn i64(&mut self) -> Result<i64> { Ok(i64::from_le_bytes(self.take()?)) }
    pub fn bytes(&mut self, max: usize) -> Result<&'a [u8]> {
        let len=self.u32()? as usize; ensure!(len <= max && len <= self.0.len(), "invalid native field length");
        let (v, rest)=self.0.split_at(len); self.0=rest; Ok(v)
    }
    pub fn text(&mut self) -> Result<String> { Ok(std::str::from_utf8(self.bytes(MAX_METADATA_BYTES)?)?.to_owned()) }
    pub fn json(&mut self) -> Result<Value> { Ok(serde_json::from_slice(self.bytes(MAX_METADATA_BYTES)?)?) }
    pub fn decimal(&mut self) -> Result<Decimal> {
        let scale=self.i64()?; ensure!(scale.unsigned_abs() <= 16384, "native decimal exponent exceeds arithmetic bound");
        let coefficient=match self.u8()? {
            0 => BigInt::from(self.i64()?), 1 => BigInt::from(i128::from_le_bytes(self.take()?)),
            2 => BigInt::from_signed_bytes_le(self.bytes(16384)?), _ => anyhow::bail!("unknown decimal coefficient encoding"),
        };
        ensure!(coefficient.to_string().trim_start_matches('-').len()<=crate::exact::INTERNAL_DECIMAL_LIMIT,"decoded decimal coefficient exceeds canonical bound");
        Ok(Decimal::from_coefficient(coefficient, scale))
    }
    pub fn optional_decimal(&mut self) -> Result<Option<String>> { match self.u8()? { 0=>Ok(None),1=>Ok(Some(self.decimal()?.to_string())),_=>anyhow::bail!("invalid optional decimal tag") } }
    pub fn optional_u64(&mut self) -> Result<Option<u64>> { match self.u8()? { 0=>Ok(None),1=>Ok(Some(self.u64()?)),_=>anyhow::bail!("invalid optional integer tag") } }
    pub fn optional_i64(&mut self) -> Result<Option<i64>> { match self.u8()? { 0=>Ok(None),1=>Ok(Some(self.i64()?)),_=>anyhow::bail!("invalid optional integer tag") } }
    pub fn position(&mut self) -> Result<Option<CapturePosition>> { match self.u8()? {
        0=>Ok(None),1=>Ok(Some(CapturePosition{epoch:self.text()?,sequence:self.u64()?,digest:self.text()?})),_=>anyhow::bail!("invalid capture tag")
    } }
    pub fn finish(self) -> Result<()> { ensure!(self.0.is_empty(), "trailing native row data"); Ok(()) }
}

#[derive(Clone, Debug)]
pub(crate) struct StoredBar { pub row: ImportBarRow, pub commit_id: u64, pub capture: Option<CapturePosition> }
impl StoredBar {
    pub fn validate(row: ImportBarRow) -> Result<ImportBarRow> {Self::validate_inner(row,false)}
    pub fn validate_import(row:ImportBarRow)->Result<ImportBarRow> {
        let unknown=row.state=="final" && row.finalized_at_ns.is_none();
        if unknown {ensure!(row.evidence["semantics"]=="final_revision_history" && row.evidence["finalization_time_unknown"]==true && matches!(row.evidence["table"].as_str(),Some("candles"|"realtime_bars")) && row.evidence["fixed_snapshot"].is_object(),"unknown legacy finalization time requires fixed-snapshot final-history evidence");}
        Self::validate_inner(row,unknown)
    }
    fn validate_inner(mut row: ImportBarRow,allow_legacy_unknown_finalization:bool) -> Result<ImportBarRow> {
        identity(&row.instrument_symbol)?; identity(&row.realtime_source_id)?; identity(&row.evidence_channel_id)?;
        ensure!(row.interval_seconds > 0, "bar interval must be positive");
        let span=i64::from(row.interval_seconds).checked_mul(1_000_000_000).context("bar interval overflow")?;
        ensure!(row.open_time_ns.checked_add(span)==Some(row.close_time_ns), "bar close time differs from interval");
        if row.interval_seconds==60 { ensure!(row.open_time_ns.rem_euclid(60_000_000_000)==0, "minute fact must use an aligned UTC open time"); }
        ensure!(matches!(row.state.as_str(),"final"|"forming"|"provisional"|"provisional_quote"|"provisional_authoritative"), "invalid bar state");
        ensure!(row.revision>0,"canonical bar revision must be positive");
        ensure!(allow_legacy_unknown_finalization || (row.state=="final")==row.finalized_at_ns.is_some(),"native finalized timestamp must be present exactly for final facts");
        let open=Decimal::from_str_exact(&row.open)?; let high=Decimal::from_str_exact(&row.high)?;
        let low=Decimal::from_str_exact(&row.low)?; let close=Decimal::from_str_exact(&row.close)?;
        ensure!(low <= high && low <= open && low <= close && high >= open && high >= close, "inconsistent OHLC ordering");
        row.open=open.to_string(); row.high=high.to_string(); row.low=low.to_string(); row.close=close.to_string();
        if let Some(volume)=&row.volume { let v=Decimal::from_str_exact(volume)?; ensure!(v>=Decimal::ZERO,"negative volume"); row.volume=Some(v.to_string()); }
        let raw=&row.source_metadata["raw_payload"];
        if !raw["source_volume_components"].is_null() || !raw["source_volume_component_groups"].is_null() || !raw["source_component_count"].is_null() {
            let sum=row.volume.as_deref().map(Decimal::from_str_exact).transpose()?.unwrap_or(Decimal::ZERO);
            let groups=crate::source_volume::read(raw,sum,u64::from(row.volume.is_some()),1)?;
            if groups.len()==1 && groups[0].policy!=crate::source_volume::CANONICAL_FALLBACK {
                let group=&groups[0];ensure!(row.volume.is_some()==(group.known_count==group.total_count),"source component completeness differs from canonical volume");
                if let Some(volume)=&row.volume{ensure!(Decimal::from_str_exact(volume)?==group.known_volume_sum,"source component sum differs from complete canonical volume");}
            }
        }
        // Metadata is bounded independently from durable raw frames.
        for v in [&row.source_metadata,&row.evidence] { ensure!(serde_json::to_vec(v)?.len() <= MAX_METADATA_BYTES,"fact metadata too large"); }
        Ok(row)
    }
    pub fn encode(&self) -> Result<Vec<u8>> {
        let r=&self.row; let mut e=Encoder::new(1);
        for v in [&r.instrument_symbol,&r.realtime_source_id,&r.evidence_channel_id] { e.text(v)?; }
        e.u32(r.interval_seconds); e.i64(r.open_time_ns); e.i64(r.close_time_ns);
        for v in [&r.open,&r.high,&r.low,&r.close] { e.decimal_text(v)?; } e.optional_decimal(&r.volume)?;
        e.u64(r.revision); e.optional_u64(r.received_sequence); e.text(&r.state)?; e.optional_i64(r.finalized_at_ns);
        e.i64(r.source_observed_at_ns); e.i64(r.received_at_ns); e.json(&r.source_metadata)?; e.json(&r.evidence)?;
        e.u64(self.commit_id); e.position(&self.capture)?; Ok(e.0)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        let mut d=Decoder::new(bytes,1)?;
        let row=ImportBarRow { instrument_symbol:d.text()?,realtime_source_id:d.text()?,evidence_channel_id:d.text()?,
            interval_seconds:d.u32()?,open_time_ns:d.i64()?,close_time_ns:d.i64()?,open:d.decimal()?.to_string(),high:d.decimal()?.to_string(),
            low:d.decimal()?.to_string(),close:d.decimal()?.to_string(),volume:d.optional_decimal()?,revision:d.u64()?,received_sequence:d.optional_u64()?,
            state:d.text()?,finalized_at_ns:d.optional_i64()?,source_observed_at_ns:d.i64()?,received_at_ns:d.i64()?,source_metadata:d.json()?,evidence:d.json()? };
        let out=Self{row,commit_id:d.u64()?,capture:d.position()?}; d.finish()?; Ok(out)
    }
}
#[derive(Clone, Debug)]
pub(crate) struct StoredQuote { pub row: ImportQuoteRow, pub commit_id: u64, pub capture: Option<CapturePosition> }
impl StoredQuote {
    pub fn validate(mut row: ImportQuoteRow) -> Result<ImportQuoteRow> {
        for v in [&row.instrument_symbol,&row.realtime_source_id,&row.evidence_channel_id,&row.event_id] { identity(v)?; }
        row.price=Decimal::from_str_exact(&row.price)?.to_string();
        for v in [&mut row.bid,&mut row.ask,&mut row.volume] { if let Some(text)=v { *text=Decimal::from_str_exact(text)?.to_string(); } }
        if let Some(v)=&row.volume { ensure!(Decimal::from_str_exact(v)? >= Decimal::ZERO,"negative quote volume"); }
        for v in [&row.source_metadata,&row.statistics,&row.evidence] { ensure!(serde_json::to_vec(v)?.len() <= MAX_METADATA_BYTES,"quote metadata too large"); }
        Ok(row)
    }
    pub fn encode(&self) -> Result<Vec<u8>> {
        let r=&self.row; let mut e=Encoder::new(1);
        for v in [&r.instrument_symbol,&r.realtime_source_id,&r.evidence_channel_id,&r.event_id] { e.text(v)?; }
        e.decimal_text(&r.price)?; for v in [&r.bid,&r.ask,&r.volume] { e.optional_decimal(v)?; }
        e.i64(r.observed_at_ns);e.i64(r.received_at_ns);e.optional_u64(r.source_sequence);e.json(&r.source_metadata)?;
        e.json(&r.statistics)?;e.u8(u8::from(r.is_supplement));e.json(&r.evidence)?;e.u64(self.commit_id);e.position(&self.capture)?;Ok(e.0)
    }
    pub fn decode(bytes: &[u8]) -> Result<Self> {
        let mut d=Decoder::new(bytes,1)?;
        let row=ImportQuoteRow { instrument_symbol:d.text()?,realtime_source_id:d.text()?,evidence_channel_id:d.text()?,event_id:d.text()?,
            price:d.decimal()?.to_string(),bid:d.optional_decimal()?,ask:d.optional_decimal()?,volume:d.optional_decimal()?,observed_at_ns:d.i64()?,
            received_at_ns:d.i64()?,source_sequence:d.optional_u64()?,source_metadata:d.json()?,statistics:d.json()?,is_supplement:d.u8()?==1,evidence:d.json()? };
        let out=Self{row,commit_id:d.u64()?,capture:d.position()?};d.finish()?;Ok(out)
    }
}
pub(crate) fn identity(value: &str) -> Result<()> { ensure!(!value.is_empty() && value.len()<=4096,"empty or oversized identity");Ok(()) }
pub(crate) fn component(out: &mut Vec<u8>, text: &str) { out.extend((text.len() as u32).to_be_bytes());out.extend(text.as_bytes()); }
pub(crate) fn signed_key(value: i64) -> [u8;8] { ((value as u64) ^ (1 << 63)).to_be_bytes() }
pub(crate) fn prefix_end(prefix: &[u8]) -> Result<Vec<u8>> {
    let mut out=prefix.to_vec(); while let Some(byte)=out.pop() { if byte != u8::MAX { out.push(byte+1);return Ok(out); } }
    anyhow::bail!("prefix has no exclusive bound")
}
