//! Jin10 binary protocols. Raw frames can be decoded without live session state.
use std::{collections::HashMap, io::Read, path::Path, time::Duration};

use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use chrono::{DateTime, Utc};
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::{
    net::TcpStream,
    sync::{mpsc, oneshot, watch},
    task::JoinHandle,
};
use tokio_tungstenite::{
    MaybeTlsStream, WebSocketStream,
    tungstenite::{Message, client::IntoClientRequest},
};
use tracefang_core::domain::{Candle, Decimal, Instrument, QuoteSnapshot, SourceMetadata};

use crate::capture::ProviderFrame;

const LOCAL: &str = "jin10_local";
const WEB: &str = "jin10_web";
const MAX_HISTORY_BYTES: u64 = 64 * 1024 * 1024;
type Socket = WebSocketStream<MaybeTlsStream<TcpStream>>;

struct Reader<'a> {
    data: &'a [u8],
    offset: usize,
}
impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }
    fn bytes<const N: usize>(&mut self) -> Result<[u8; N]> {
        let end = self
            .offset
            .checked_add(N)
            .context("Jin10 frame length overflow")?;
        let value = self
            .data
            .get(self.offset..end)
            .context("Jin10 frame is truncated")?;
        self.offset = end;
        Ok(value.try_into().expect("fixed length"))
    }
    fn i8(&mut self) -> Result<i8> {
        Ok(i8::from_le_bytes(self.bytes()?))
    }
    fn u16(&mut self) -> Result<u16> {
        Ok(u16::from_le_bytes(self.bytes()?))
    }
    fn i32(&mut self) -> Result<i32> {
        Ok(i32::from_le_bytes(self.bytes()?))
    }
    fn u32(&mut self) -> Result<u32> {
        Ok(u32::from_le_bytes(self.bytes()?))
    }
    fn i64(&mut self) -> Result<i64> {
        Ok(i64::from_le_bytes(self.bytes()?))
    }
    fn string(&mut self) -> Result<String> {
        let length = self.u16()? as usize;
        ensure!(length > 0, "Jin10 symbol or filename is empty");
        let end = self.offset + length;
        let bytes = self
            .data
            .get(self.offset..end)
            .context("Jin10 string is truncated")?;
        self.offset = end;
        Ok(std::str::from_utf8(bytes)
            .context("Jin10 string is not UTF-8")?
            .to_owned())
    }
    fn finish(&self) -> Result<()> {
        ensure!(
            self.offset == self.data.len(),
            "Jin10 frame has trailing bytes"
        );
        Ok(())
    }
}

fn string(packet: &mut Vec<u8>, value: &str) -> Result<()> {
    let length: u16 = value.len().try_into().context("Jin10 string is too long")?;
    packet.extend(length.to_le_bytes());
    packet.extend(value.as_bytes());
    Ok(())
}
pub fn provider_code(instrument: &Instrument) -> Result<&'static str> {
    match instrument.symbol.as_str() {
        "XAU/USD" => Ok("XAUUSD.GOODS"),
        "XAG/USD" => Ok("XAGUSD.GOODS"),
        "USD/CNH" => Ok("USDCNH.FXCM"),
        _ => bail!("instrument is not supported by Jin10"),
    }
}
fn instrument_for<'a>(code: &str, instruments: &'a [Instrument]) -> Result<&'a Instrument> {
    instruments
        .iter()
        .find(|i| provider_code(i).is_ok_and(|v| v.eq_ignore_ascii_case(code)))
        .context("Jin10 frame has an unsupported instrument")
}
pub fn derive_session_key(handshake: &[u8]) -> Result<String> {
    let mut reader = Reader::new(handshake);
    reader.u32()?;
    let second = reader.u32()?;
    let third = reader.u32()?;
    Ok(format!("{third}.{second}"))
}
pub fn xor_cipher(data: &[u8], key: &str) -> Result<Vec<u8>> {
    ensure!(
        !key.is_empty() && key.is_ascii(),
        "invalid Jin10 session key"
    );
    let bytes = key.as_bytes();
    Ok(data
        .iter()
        .enumerate()
        .map(|(i, v)| v ^ bytes[(i + bytes[0] as usize) % bytes.len()])
        .collect())
}
pub fn encode_login(token: &str, vip_type: i32) -> Result<Vec<u8>> {
    ensure!(
        token.chars().count() == 36,
        "Jin10 session token must contain 36 characters"
    );
    ensure!([0, 1, 3].contains(&vip_type), "invalid Jin10 VIP type");
    let mut packet = 10018_i16.to_le_bytes().to_vec();
    packet.extend(0_i32.to_le_bytes());
    string(&mut packet, token)?;
    string(&mut packet, "")?;
    packet.extend(vip_type.to_le_bytes());
    string(&mut packet, "web")?;
    packet.extend(3_i16.to_le_bytes());
    Ok(packet)
}
pub fn encode_subscription(codes: &[String], frequency: u32, kline: bool) -> Result<Vec<u8>> {
    ensure!(frequency <= 60000, "invalid Jin10 subscription frequency");
    let mut unique = Vec::new();
    for code in codes {
        if !unique.contains(code) {
            unique.push(code.clone());
        }
    }
    let count: i16 = unique
        .len()
        .try_into()
        .context("too many Jin10 subscriptions")?;
    let mut packet = (if kline { 10002_i16 } else { 10003_i16 })
        .to_le_bytes()
        .to_vec();
    packet.extend(frequency.to_le_bytes());
    packet.extend(count.to_le_bytes());
    for code in unique {
        string(&mut packet, &code)?;
        if kline {
            packet.extend(1_i16.to_le_bytes());
        }
    }
    Ok(packet)
}
pub fn encode_history_request(code: &str, boundary: i64) -> Result<Vec<u8>> {
    let mut packet = 10006_i16.to_le_bytes().to_vec();
    string(&mut packet, code)?;
    packet.push(1);
    packet.extend(boundary.to_le_bytes());
    packet.extend(1_i16.to_le_bytes());
    packet.push(255);
    Ok(packet)
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HistoryFile {
    pub file_name: String,
    pub record_count: Option<usize>,
    pub start_timestamp: Option<i64>,
    pub end_timestamp: Option<i64>,
}
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct HistoryManifest {
    pub provider_code: String,
    pub time_type: i8,
    pub boundary_timestamp: i64,
    pub files: Vec<HistoryFile>,
}
pub fn parse_manifest(payload: &[u8]) -> Result<HistoryManifest> {
    let mut reader = Reader::new(payload);
    let provider_code = reader.string()?;
    reader.u16()?;
    reader.i8()?;
    let time_type = reader.i8()?;
    let boundary_timestamp = reader.i64()?;
    let count = reader.i8()?;
    ensure!(count >= 0, "invalid Jin10 history file count");
    let mut files = Vec::new();
    for _ in 0..count {
        let value = reader.string()?;
        let pieces: Vec<_> = value.split('.').collect();
        ensure!(!pieces[0].trim().is_empty(), "empty Jin10 history filename");
        files.push(HistoryFile {
            file_name: pieces[0].trim().to_owned(),
            record_count: pieces
                .get(1)
                .map(|v| v.parse())
                .transpose()
                .context("invalid history record count")?,
            start_timestamp: pieces
                .get(2)
                .map(|v| v.parse())
                .transpose()
                .context("invalid history start")?,
            end_timestamp: pieces
                .get(3)
                .map(|v| v.parse())
                .transpose()
                .context("invalid history end")?,
        });
    }
    reader.finish()?;
    Ok(HistoryManifest {
        provider_code,
        time_type,
        boundary_timestamp,
        files,
    })
}

fn timestamp(value: i64) -> Result<DateTime<Utc>> {
    ensure!(value > 0, "Jin10 timestamp must be positive");
    DateTime::from_timestamp(value, 0).context("invalid Jin10 timestamp")
}
fn price(value: i64) -> Decimal {
    Decimal::new(value, 6)
}
fn optional_price(value: i64) -> Option<Decimal> {
    (value != 0).then(|| price(value))
}
fn percentage(change: Decimal, previous: Decimal) -> Decimal {
    let value = change * Decimal::from(100) / previous.abs();
    value.round_significant(28)
}
fn wire_candle(
    reader: &mut Reader<'_>,
    instrument: &Instrument,
    frame: &ProviderFrame,
    code: &str,
    protocol: u16,
    file_name: Option<&str>,
) -> Result<Candle> {
    let open_time = timestamp(reader.i64()?)?;
    let high = price(reader.i64()?);
    let open = price(reader.i64()?);
    let low = price(reader.i64()?);
    let close = price(reader.i64()?);
    let volume = Decimal::from(reader.i64()?);
    ensure!(low > Decimal::ZERO, "Jin10 candle prices must be positive");
    let candle = Candle {
        instrument: instrument.clone(),
        interval_seconds: 60,
        open_time,
        open,
        high,
        low,
        close,
        volume: Some(volume),
        source: SourceMetadata {
            provider: LOCAL.into(),
            provider_symbol: code.into(),
            observed_at: open_time,
            received_at: frame.received_at,
            raw_payload: Some(
                json!({"protocol":protocol,"time_type":1,"price_scale":1000000,
              "history_file":file_name,"bar_state":if file_name.is_some(){"final"}else{"provisional_authoritative"},
              "connection_id":frame.connection_id,"sequence":frame.sequence.to_string()}),
            ),
        },
    };
    candle.validate()?;
    Ok(candle)
}

pub fn decode_frame(
    frame: &ProviderFrame,
    instruments: &[Instrument],
) -> Result<(Vec<QuoteSnapshot>, Vec<Candle>)> {
    if frame.channel == "jin10_history" {
        return Ok((vec![], decode_history_frame(frame, instruments)?));
    }
    let web = frame.channel == WEB;
    ensure!(
        (web && frame.encoding == "wire")
            || (frame.channel == LOCAL && frame.encoding == "session-decrypted"),
        "invalid Jin10 frame channel or encoding"
    );
    let mut reader = Reader::new(&frame.body);
    let protocol = reader.u16()?;
    if (web && protocol != 10005) || (!web && ![10005, 20010, 10004, 10007].contains(&protocol)) {
        return Ok((vec![], vec![]));
    }
    let code = reader.string()?;
    let instrument = instrument_for(&code, instruments)?;
    if !web && [10004, 10007].contains(&protocol) {
        // Recorded desktop frames use 10004 for one live bar and 10007 for
        // counted snapshots. Older fixtures use the opposite labels; identify
        // the unambiguous wire layout rather than silently losing both feeds.
        let single_record = reader.data.len() - reader.offset == 52
            && reader.data.get(reader.offset..reader.offset + 4) == Some(&[1, 0, 0, 0]);
        let (time_type, count) = if single_record {
            (reader.i32()?, 1)
        } else {
            (reader.i8()? as i32, reader.i32()?)
        };
        ensure!((0..=100000).contains(&count), "invalid Jin10 candle count");
        if time_type != 1 {
            return Ok((vec![], vec![]));
        }
        let mut candles = Vec::with_capacity(count as usize);
        for _ in 0..count {
            candles.push(wire_candle(
                &mut reader,
                instrument,
                frame,
                &code,
                protocol,
                None,
            )?);
        }
        reader.finish()?;
        return Ok((vec![], candles));
    }
    let (at, last, previous, open, mut high, mut low, volume, extra) = if web {
        let at = reader.u32()? as i64;
        let last = price(reader.i64()?);
        let previous = optional_price(reader.i64()?);
        (
            at,
            last,
            previous,
            None,
            None,
            None,
            None,
            json!({"channel":"jin10_public_websocket"}),
        )
    } else {
        let last = price(reader.i64()?);
        let buy = price(reader.i64()?);
        let ask = price(reader.i64()?);
        let volume = reader.i64()?;
        let high = optional_price(reader.i64()?);
        let open = optional_price(reader.i64()?);
        let low = optional_price(reader.i64()?);
        let previous = optional_price(reader.i64()?);
        let turnover = reader.i64()?;
        let at = reader.i32()? as i64;
        (
            at,
            last,
            previous,
            open,
            high,
            low,
            (volume >= 0).then(|| Decimal::from(volume)),
            json!({"buy":buy.to_string(),"ask":ask.to_string(),"turnover":turnover}),
        )
    };
    ensure!(last > Decimal::ZERO, "Jin10 quote price must be positive");
    if let (Some(h), Some(l)) = (&high, &low) {
        if !(l <= &last && &last <= h) {
            high = None;
            low = None;
        }
    }
    let change = previous.clone().map(|v| &last - &v);
    let mut raw = json!({"protocol":protocol,"observation_kind":"event","connection_id":frame.connection_id,
        "sequence":frame.sequence.to_string(),"previous_close":previous.as_ref().map(|v|v.to_string())});
    raw.as_object_mut()
        .unwrap()
        .extend(extra.as_object().unwrap().clone());
    let quote = QuoteSnapshot {
        instrument: instrument.clone(),
        last,
        open,
        high,
        low,
        volume,
        change:change.clone(),
        change_percent: previous
            .zip(change)
            .map(|(p, c)| percentage(c, p)),
        source: SourceMetadata {
            provider: frame.channel.clone(),
            provider_symbol: code,
            observed_at: timestamp(at)?,
            received_at: frame.received_at,
            raw_payload: Some(raw),
        },
    };
    quote.validate()?;
    Ok((vec![quote], vec![]))
}

#[derive(Serialize, Deserialize)]
struct HistoryEnvelope {
    provider_code: String,
    file: HistoryFile,
    body_base64: String,
}
fn decode_history_frame(frame: &ProviderFrame, instruments: &[Instrument]) -> Result<Vec<Candle>> {
    ensure!(
        frame.encoding == "gzip-json",
        "invalid Jin10 history encoding"
    );
    let envelope: HistoryEnvelope = serde_json::from_slice(&frame.body)?;
    let instrument = instrument_for(&envelope.provider_code, instruments)?;
    let zipped = STANDARD
        .decode(&envelope.body_base64)
        .context("invalid Jin10 history payload")?;
    let mut decoded = Vec::new();
    flate2::read::MultiGzDecoder::new(zipped.as_slice())
        .take(MAX_HISTORY_BYTES + 1)
        .read_to_end(&mut decoded)
        .context("invalid Jin10 history GZip")?;
    ensure!(
        !decoded.is_empty() && decoded.len() as u64 <= MAX_HISTORY_BYTES && decoded.len() % 48 == 0,
        "invalid Jin10 history record length"
    );
    let mut reader = Reader::new(&decoded);
    let mut rows = Vec::with_capacity(decoded.len() / 48);
    while reader.offset < decoded.len() {
        rows.push(wire_candle(
            &mut reader,
            instrument,
            frame,
            &envelope.provider_code,
            10006,
            Some(&envelope.file.file_name),
        )?);
    }
    if let Some(expected) = envelope.file.record_count {
        let actual = if let (Some(start), Some(end)) =
            (envelope.file.start_timestamp, envelope.file.end_timestamp)
        {
            rows.iter()
                .filter(|c| c.open_time.timestamp() >= start && c.open_time.timestamp() <= end)
                .count()
        } else {
            rows.len()
        };
        ensure!(
            actual == expected,
            "Jin10 history record count differs from its manifest"
        );
    }
    Ok(rows)
}

fn validate_session_token(token: String) -> Result<String> {
    let token = token.trim().to_owned();
    ensure!(
        token.chars().count() == 36,
        "Jin10 session token must contain 36 characters"
    );
    Ok(token)
}
pub fn session_token() -> Result<String> {
    if let Ok(token) = std::env::var("JIN10_LOCAL_SESSION_TOKEN") {
        if !token.trim().is_empty() {
            return validate_session_token(token);
        }
    }
    ensure!(
        cfg!(target_os = "macos"),
        "Jin10 desktop session requires JIN10_LOCAL_SESSION_TOKEN on this platform"
    );
    let home = std::env::var_os("HOME").context("home directory unavailable")?;
    read_desktop_session(&Path::new(&home).join("Library/Application Support/com.jin10.desktop"))
}
fn read_desktop_session(root: &Path) -> Result<String> {
    let path = root.join("local_storage.json");
    let directory =
        std::fs::symlink_metadata(root).context("Jin10 desktop session is unavailable")?;
    let info = std::fs::symlink_metadata(&path).context("Jin10 desktop session is unavailable")?;
    ensure!(
        !directory.file_type().is_symlink()
            && info.file_type().is_file()
            && info.len() <= 1024 * 1024,
        "Jin10 desktop session must be a regular client file"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        ensure!(
            info.uid() == unsafe { libc::geteuid() },
            "Jin10 desktop session must belong to this user"
        );
    }
    let bytes = std::fs::read(path).context("Jin10 desktop session could not be read")?;
    let data: serde_json::Value =
        serde_json::from_slice(&bytes).map_err(|_| anyhow!("Jin10 desktop session is invalid"))?;
    validate_session_token(
        data.get("ji10_token")
            .and_then(|v| v.as_str())
            .context("sign in to the Jin10 desktop client")?
            .to_owned(),
    )
}

#[derive(Debug, Clone, Serialize)]
pub struct ChannelStatus {
    pub state: String,
    pub error: Option<String>,
}
pub struct ProviderTask {
    pub task: JoinHandle<()>,
    pub status: watch::Receiver<ChannelStatus>,
}
struct HistoryCommand {
    code: String,
    boundary: i64,
    reply: oneshot::Sender<Result<HistoryManifest>>,
}
#[derive(Clone)]
pub struct LocalHandle {
    commands: mpsc::Sender<HistoryCommand>,
}
impl LocalHandle {
    pub fn disabled()->Self {let(commands,_)=mpsc::channel(1);Self {commands}}
    pub async fn manifest(&self, code: &str, boundary: i64) -> Result<HistoryManifest> {
        let (reply, receiver) = oneshot::channel();
        tokio::time::timeout(Duration::from_secs(20), async {
            self.commands
                .send(HistoryCommand {
                    code: code.into(),
                    boundary,
                    reply,
                })
                .await
                .context("Jin10 history connection is stopped")?;
            receiver
                .await
                .context("Jin10 history connection was interrupted")?
        })
        .await
        .context("Jin10 history manifest request timed out")?
    }
}

pub fn spawn_web(
    subscriptions: watch::Receiver<Vec<Instrument>>,
    shutdown: watch::Receiver<bool>,
    frames: crate::providers::ingress::FrameSink,
) -> ProviderTask {
    let (status, receiver) = watch::channel(ChannelStatus {
        state: "connecting".into(),
        error: None,
    });
    let task = tokio::spawn(run_channel(
        false,
        subscriptions,
        shutdown,
        frames,
        None,
        status,
    ));
    ProviderTask {
        task,
        status: receiver,
    }
}
pub fn spawn_local(
    subscriptions: watch::Receiver<Vec<Instrument>>,
    shutdown: watch::Receiver<bool>,
    frames: crate::providers::ingress::FrameSink,
) -> (LocalHandle, ProviderTask) {
    let (commands, receiver) = mpsc::channel(32);
    let (status, status_receiver) = watch::channel(ChannelStatus {
        state: "connecting".into(),
        error: None,
    });
    let task = tokio::spawn(run_channel(
        true,
        subscriptions,
        shutdown,
        frames,
        Some(receiver),
        status,
    ));
    (
        LocalHandle { commands },
        ProviderTask {
            task,
            status: status_receiver,
        },
    )
}

async fn run_channel(
    local: bool,
    mut subscriptions: watch::Receiver<Vec<Instrument>>,
    mut shutdown: watch::Receiver<bool>,
    frames: crate::providers::ingress::FrameSink,
    mut commands: Option<mpsc::Receiver<HistoryCommand>>,
    status: watch::Sender<ChannelStatus>,
) {
    let mut delay = 100;
    while !*shutdown.borrow() {
        if subscriptions.borrow().is_empty() {
            status.send_replace(ChannelStatus {
                state: "idle".into(),
                error: None,
            });
            tokio::select! { result=shutdown.changed()=>{if result.is_err(){break}}, result=subscriptions.changed()=>{if result.is_err(){break}} }
            continue;
        }
        status.send_replace(ChannelStatus {
            state: "connecting".into(),
            error: None,
        });
        // Transport errors intentionally use a fixed message: TLS URLs, proxies and
        // login payloads must never leak session credentials into status or logs.
        let result = connection(
            local,
            &mut subscriptions,
            &mut shutdown,
            &frames,
            &mut commands,
            &status,
        )
        .await;
        if *shutdown.borrow() {
            break;
        }
        status.send_replace(ChannelStatus {
            state: "reconnecting".into(),
            error: Some(
                if result.is_err() {
                    "Jin10 channel unavailable; reconnecting"
                } else {
                    "Jin10 channel closed; reconnecting"
                }
                .into(),
            ),
        });
        tokio::select! { result=shutdown.changed()=>{if result.is_err(){break}},_=tokio::time::sleep(Duration::from_millis(delay))=>{} }
        delay = (delay * 2).min(1000);
    }
    status.send_replace(ChannelStatus {
        state: "stopped".into(),
        error: None,
    });
}

fn env_u32(key: &str, default: u32) -> u32 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(default)
}
async fn connection(
    local: bool,
    subscriptions: &mut watch::Receiver<Vec<Instrument>>,
    shutdown: &mut watch::Receiver<bool>,
    frames: &crate::providers::ingress::FrameSink,
    commands: &mut Option<mpsc::Receiver<HistoryCommand>>,
    status: &watch::Sender<ChannelStatus>,
) -> Result<()> {
    let url = std::env::var(if local {
        "JIN10_LOCAL_URL"
    } else {
        "JIN10_WEB_URL"
    })
    .unwrap_or_else(|_| {
        if local {
            "wss://app-quote-ws.jin10.com/"
        } else {
            "wss://b-price.jin10.com/"
        }
        .into()
    });
    let origin = (!local).then(|| {
        std::env::var("JIN10_WEB_ORIGIN").unwrap_or_else(|_| "https://www.jin10.com".into())
    });
    let mut socket = tokio::time::timeout(
        Duration::from_secs(10),
        connect_websocket(&url, origin.as_deref()),
    )
    .await??;
    let key = if local {
        let _handshake_credit=frames.reserve().await?;
        let handshake = tokio::time::timeout(Duration::from_secs(10), socket.next())
            .await?
            .context("missing Jin10 handshake")??;
        let Message::Binary(handshake) = handshake else {
            bail!("Jin10 handshake must be binary")
        };
        let key = derive_session_key(&handshake)?;
        let token = session_token()?;
        socket
            .send(Message::Binary(
                xor_cipher(
                    &encode_login(&token, env_u32("JIN10_LOCAL_VIP_TYPE", 3) as i32)?,
                    &key,
                )?
                .into(),
            ))
            .await?;
        Some(key)
    } else {
        None
    };
    let mut relogged = false;
    let initial_subscriptions = subscriptions.borrow().clone();
    send_subscriptions(&mut socket, &initial_subscriptions, key.as_deref()).await?;
    status.send_replace(ChannelStatus {
        state: "connected".into(),
        error: None,
    });
    let connection_id = uuid::Uuid::new_v4().simple().to_string();
    let mut sequence = 0_u64;
    let mut heartbeat = tokio::time::interval(Duration::from_secs(if local { 10 } else { 20 }));
    heartbeat.tick().await;
    let mut pending: HashMap<(String, i64), Vec<oneshot::Sender<Result<HistoryManifest>>>> =
        HashMap::new();
    loop {
        let mut work=tokio::select!{result=frames.reserve()=>result?,_=shutdown.changed()=>return Ok(())};
        tokio::select! {
            result=shutdown.changed()=>{if result.is_err() || *shutdown.borrow(){let _=socket.close(None).await;return Ok(())}}
            result=subscriptions.changed()=>{
                result.context("subscriptions closed")?;
                let values=subscriptions.borrow().clone();
                if values.is_empty(){let _=socket.close(None).await;return Ok(())}
                send_subscriptions(&mut socket,&values,key.as_deref()).await?;
            }
            _=heartbeat.tick()=>{
                if local {socket.send(Message::Text("".into())).await?;}else{socket.send(Message::Ping(Vec::new().into())).await?;}
                pending.retain(|_,values|{values.retain(|v|!v.is_closed());!values.is_empty()});
            }
            command=async {match commands {Some(receiver)=>receiver.recv().await,None=>std::future::pending().await}}=>{
                if let Some(command)=command {
                    if command.reply.is_closed(){continue;}
                    let packet=encode_history_request(&command.code,command.boundary)?;
                    socket.send(Message::Binary(xor_cipher(&packet,key.as_deref().context("history needs local connection")?)?.into())).await?;
                    pending.entry((command.code,command.boundary)).or_default().push(command.reply);
                }
            }
            message=socket.next()=>{
                let message=message.context("Jin10 connection closed")??;
                match message {
                    Message::Binary(body)=>{
                        work.observe(body.len()*2+16384)?;
                        let body=if let Some(key)=&key{xor_cipher(&body,key)?}else{body.to_vec()};
                        sequence=sequence.checked_add(1).context("provider sequence exhausted")?;
                        let frame=ProviderFrame{version:1,channel:if local{LOCAL}else{WEB}.into(),connection_id:connection_id.clone(),sequence,received_at:Utc::now(),encoding:if local{"session-decrypted"}else{"wire"}.into(),body};
                        let protocol=frame.body.get(..2).map(|v|u16::from_le_bytes([v[0],v[1]]));
                        // Backpressure preserves every received frame until the capture actor accepts it.
                        let manifest=if local && protocol==Some(10006){Some(parse_manifest(&frame.body[2..])?)}else{None};
                        frames.send(frame,work).await.context("capture actor stopped")?;
                        if !local && protocol==Some(1200){socket.send(Message::Text("".into())).await?;}
                        if local && protocol==Some(21113){
                            ensure!(!relogged,"Jin10 session authentication failed after refresh"); relogged=true;
                            let token=session_token()?;
                            socket.send(Message::Binary(xor_cipher(&encode_login(&token,env_u32("JIN10_LOCAL_VIP_TYPE",3) as i32)?,key.as_ref().unwrap())?.into())).await?;
                        }
                        if let Some(manifest)=manifest {
                            if let Some(waiters)=pending.remove(&(manifest.provider_code.clone(),manifest.boundary_timestamp)){
                                for reply in waiters {let _=reply.send(Ok(manifest.clone()));}
                            }
                        }
                    }
                    Message::Ping(bytes)=>socket.send(Message::Pong(bytes)).await?,
                    Message::Close(_)=>return Ok(()),_=>{},
                }
            }
        }
    }
}

async fn send_subscriptions(
    socket: &mut Socket,
    instruments: &[Instrument],
    key: Option<&str>,
) -> Result<()> {
    let codes = instruments
        .iter()
        .map(|i| provider_code(i).map(str::to_owned))
        .collect::<Result<Vec<_>>>()?;
    let packet = encode_subscription(
        &codes,
        env_u32(
            if key.is_some() {
                "JIN10_LOCAL_QUOTE_FREQUENCY_MS"
            } else {
                "JIN10_WEB_QUOTE_FREQUENCY_MS"
            },
            if key.is_some() { 1000 } else { 0 },
        ),
        false,
    )?;
    socket
        .send(Message::Binary(
            if let Some(key) = key {
                xor_cipher(&packet, key)?
            } else {
                packet
            }
            .into(),
        ))
        .await?;
    if let Some(key) = key {
        let packet = encode_subscription(
            &codes,
            env_u32("JIN10_LOCAL_KLINE_FREQUENCY_MS", 3000),
            true,
        )?;
        socket
            .send(Message::Binary(xor_cipher(&packet, key)?.into()))
            .await?;
    }
    Ok(())
}

pub async fn download_history_file(
    instrument: &Instrument,
    item: &HistoryFile,
) -> Result<ProviderFrame> {
    request_history_file(instrument, item, false).await
}
pub async fn refresh_history_file(
    instrument: &Instrument,
    item: &HistoryFile,
) -> Result<ProviderFrame> {
    request_history_file(instrument, item, true).await
}
async fn request_history_file(
    instrument: &Instrument,
    item: &HistoryFile,
    refresh: bool,
) -> Result<ProviderFrame> {
    request_history_file_accounted(instrument,item,refresh,None).await
}
pub async fn request_history_file_reserved(instrument:&Instrument,item:&HistoryFile,refresh:bool,work:&mut crate::providers::ingress::WorkReservation)->Result<ProviderFrame>{request_history_file_accounted(instrument,item,refresh,Some(work)).await}
async fn request_history_file_accounted(instrument:&Instrument,item:&HistoryFile,refresh:bool,mut work:Option<&mut crate::providers::ingress::WorkReservation>)->Result<ProviderFrame>{
    let code = provider_code(instrument)?;
    let endpoint = std::env::var("JIN10_LOCAL_KLINE_FILE_URL")
        .unwrap_or_else(|_| "https://jiaoyixia-market.jin10.com".into());
    let mut url = reqwest::Url::parse(&endpoint).context("invalid Jin10 history endpoint")?;
    url.path_segments_mut()
        .map_err(|_| anyhow!("invalid Jin10 history endpoint"))?
        .pop_if_empty()
        .push(code)
        .push("1")
        .push(&item.file_name);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(20))
        .gzip(false)
        .build()?;
    let version = format!(
        "{}-{}",
        item.record_count
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".into()),
        item.end_timestamp
            .map(|v| v.to_string())
            .unwrap_or_else(|| "unknown".into())
    );
    let mut request = client
        .get(url.clone())
        .query(&[("manifest_version", version.clone())]);
    if refresh {
        request = request.query(&[("refresh", Utc::now().timestamp_nanos_opt().unwrap_or(0))]);
    }
    let mut response = request
        .send()
        .await
        .map_err(|_| anyhow!("Jin10 history download unavailable"))?
        .error_for_status()
        .map_err(|_| anyhow!("Jin10 history download failed"))?;
    ensure!(
        response
            .content_length()
            .is_none_or(|v| v <= crate::providers::ingress::MAX_DECODED_BODY as u64),
        "Jin10 history file is too large"
    );
    let mut compressed = Vec::new();
    while let Some(chunk) = response
        .chunk()
        .await
        .map_err(|_| anyhow!("Jin10 history download interrupted"))?
    {
        compressed.extend_from_slice(&chunk);
        if let Some(work)=work.as_mut(){work.observe(compressed.capacity()+chunk.len()+16384)?;}
        ensure!(
            compressed.len() <= crate::providers::ingress::MAX_DECODED_BODY,
            "Jin10 history file is too large"
        );
    }
    let encoded=STANDARD.encode(&compressed);if let Some(work)=work.as_mut(){work.observe(compressed.capacity()+encoded.capacity()+16384)?;}drop(compressed);
    let encoded_capacity=encoded.capacity();let envelope=HistoryEnvelope {provider_code:code.into(),file:item.clone(),body_base64:encoded};
    let body=serde_json::to_vec(&envelope)?;if let Some(work)=work.as_mut(){work.observe(body.capacity()+encoded_capacity+16384)?;}drop(envelope);
    ensure!(body.len()<=crate::providers::ingress::MAX_ENCODED_FRAME,"encoded history envelope exceeds frame limit");
    let frame=ProviderFrame {version:1,channel:"jin10_history".into(),connection_id:uuid::Uuid::new_v4().simple().to_string(),sequence:1,received_at:Utc::now(),encoding:"gzip-json".into(),body};
    Ok(frame)
}

pub async fn connect_websocket(endpoint: &str, origin: Option<&str>) -> Result<Socket> {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    let url = reqwest::Url::parse(endpoint)?;
    ensure!(
        ["ws", "wss"].contains(&url.scheme()),
        "unsupported WebSocket URL scheme"
    );
    let host = url.host_str().context("WebSocket host missing")?;
    let port = url
        .port_or_known_default()
        .context("WebSocket port missing")?;
    let mut request = endpoint.into_client_request()?;
    if let Some(origin) = origin {
        request.headers_mut().insert("Origin", origin.parse()?);
    }
    let bypass = host == "localhost"
        || host
            .parse::<std::net::IpAddr>()
            .is_ok_and(|ip| ip.is_loopback());
    let proxy = if bypass {
        None
    } else {
        [
            "https_proxy",
            "HTTPS_PROXY",
            "all_proxy",
            "ALL_PROXY",
            "http_proxy",
            "HTTP_PROXY",
        ]
        .iter()
        .find_map(|key| std::env::var(key).ok().filter(|v| !v.is_empty()))
    };
    let stream = if let Some(proxy) = proxy {
        let proxy = reqwest::Url::parse(&proxy).context("invalid WebSocket proxy")?;
        let proxy_host = proxy.host_str().context("proxy host missing")?;
        let proxy_port = proxy.port_or_known_default().unwrap_or(1080);
        match proxy.scheme() {
            "socks5" | "socks5h" => {
                let socket = if proxy.username().is_empty() {
                    tokio_socks::tcp::Socks5Stream::connect((proxy_host, proxy_port), (host, port))
                        .await?
                } else {
                    tokio_socks::tcp::Socks5Stream::connect_with_password(
                        (proxy_host, proxy_port),
                        (host, port),
                        proxy.username(),
                        proxy.password().unwrap_or(""),
                    )
                    .await?
                };
                socket.into_inner()
            }
            "http" => {
                let mut stream = TcpStream::connect((proxy_host, proxy_port)).await?;
                let authority = format!("{host}:{port}");
                let auth = if proxy.username().is_empty() {
                    String::new()
                } else {
                    format!(
                        "Proxy-Authorization: Basic {}\r\n",
                        STANDARD.encode(format!(
                            "{}:{}",
                            proxy.username(),
                            proxy.password().unwrap_or("")
                        ))
                    )
                };
                stream
                    .write_all(
                        format!("CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n{auth}\r\n")
                            .as_bytes(),
                    )
                    .await?;
                let mut header = Vec::new();
                while !header.ends_with(b"\r\n\r\n") {
                    ensure!(header.len() < 16384, "proxy CONNECT response too large");
                    header.push(stream.read_u8().await?);
                }
                let status = std::str::from_utf8(&header)?.split_whitespace().nth(1);
                ensure!(status == Some("200"), "WebSocket proxy CONNECT failed");
                stream
            }
            _ => bail!("unsupported WebSocket proxy scheme"),
        }
    } else {
        TcpStream::connect((host, port)).await?
    };
    let config = tokio_tungstenite::tungstenite::protocol::WebSocketConfig::default()
        .max_message_size(Some(crate::providers::ingress::MAX_DECODED_BODY))
        .max_frame_size(Some(crate::providers::ingress::MAX_DECODED_BODY));
    let (socket, _) =
        tokio_tungstenite::client_async_tls_with_config(request, stream, Some(config), None)
            .await?;
    Ok(socket)
}
