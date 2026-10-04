#[path = "../src/capture.rs"]
mod capture;
#[path="../src/providers/ingress.rs"] pub mod provider_ingress;
mod providers {pub use crate::provider_ingress as ingress;}
#[path = "../src/providers/jin10.rs"]
mod jin10;

use base64::{Engine, engine::general_purpose::STANDARD};
use capture::ProviderFrame;
use chrono::{TimeZone, Utc};
use flate2::{Compression, write::GzEncoder};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use std::io::Write;
use tracefang_core::domain::{AssetClass, Decimal, Instrument};

fn gold() -> Instrument {
    Instrument {
        symbol: "XAU/USD".into(),
        asset_class: AssetClass::Spot,
        base: Some("XAU".into()),
        quote: Some("USD".into()),
        venue: None,
    }
}
fn string(value: &str) -> Vec<u8> {
    let mut out = (value.len() as u16).to_le_bytes().to_vec();
    out.extend(value.as_bytes());
    out
}
fn frame(channel: &str, body: Vec<u8>) -> ProviderFrame {
    ProviderFrame {
        version: 1,
        channel: channel.into(),
        connection_id: "recordedconnection".into(),
        sequence: 7,
        received_at: Utc.timestamp_opt(1786016200, 123000000).unwrap(),
        encoding: if channel == "jin10_web" {
            "wire"
        } else {
            "session-decrypted"
        }
        .into(),
        body,
    }
}
fn wire_candle() -> Vec<u8> {
    [
        1786027380_i64,
        4252000000,
        4250000000,
        4249000000,
        4251000000,
        10,
    ]
    .into_iter()
    .flat_map(i64::to_le_bytes)
    .collect()
}

#[test]
fn known_handshake_and_login_match_python_wire_contract() {
    let key = jin10::derive_session_key(&hex::decode("a2060f004bbd2b0005cd65359b260900").unwrap())
        .unwrap();
    assert_eq!(key, "895864069.2866507");
    let clear = b"structured market data";
    assert_eq!(
        jin10::xor_cipher(&jin10::xor_cipher(clear, &key).unwrap(), &key).unwrap(),
        clear
    );
    let packet = jin10::encode_login(&"x".repeat(36), 3).unwrap();
    assert_eq!(
        hex::encode(packet),
        "222700000000240078787878787878787878787878787878787878787878787878787878787878787878787800000300000003007765620300"
    );
    assert!(jin10::derive_session_key(&[1, 2, 3]).is_err());
}

#[test]
fn web_quote_retains_micro_price_and_original_event_identity() {
    let mut body = 10005_u16.to_le_bytes().to_vec();
    body.extend(string("XAUUSD.GOODS"));
    body.extend(1786016195_u32.to_le_bytes());
    body.extend(4266530000_i64.to_le_bytes());
    body.extend(4246730000_i64.to_le_bytes());
    let original = frame("jin10_web", body);
    let (quotes, bars) = jin10::decode_frame(&original, &[gold()]).unwrap();
    assert!(bars.is_empty());
    let quote = &quotes[0];
    assert_eq!(quote.last, Decimal::new(426653, 2));
    assert_eq!(quote.change, Some(Decimal::new(1980, 2)));
    assert_eq!(quote.source.observed_at.timestamp(), 1786016195);
    assert_eq!(quote.source.received_at, original.received_at);
    assert!(quote.open.is_none() && quote.volume.is_none());
    assert_eq!(quote.source.raw_payload.as_ref().unwrap()["sequence"], "7");
}

#[test]
fn local_quote_preserves_zero_volume_and_drops_inconsistent_high_low() {
    let mut body = 20010_u16.to_le_bytes().to_vec();
    body.extend(string("XAUUSD.GOODS"));
    for value in [
        4256230000_i64,
        4256230000,
        4256280000,
        0,
        4250000000,
        4247780000,
        4245750000,
        4246730000,
        0,
    ] {
        body.extend(value.to_le_bytes());
    }
    body.extend(1785997733_i32.to_le_bytes());
    let (quotes, _) = jin10::decode_frame(&frame("jin10_local", body), &[gold()]).unwrap();
    assert_eq!(quotes[0].volume, Some(Decimal::ZERO));
    assert_eq!(quotes[0].open, Some(Decimal::new(424778, 2)));
    assert!(quotes[0].high.is_none() && quotes[0].low.is_none());
}

#[test]
fn minute_snapshot_is_authoritative_and_rejects_truncation_and_invalid_ohlc() {
    let mut body = 10004_u16.to_le_bytes().to_vec();
    body.extend(string("XAUUSD.GOODS"));
    body.push(1);
    body.extend(1_i32.to_le_bytes());
    body.extend(wire_candle());
    let (_, bars) = jin10::decode_frame(&frame("jin10_local", body.clone()), &[gold()]).unwrap();
    assert_eq!(bars[0].close, Decimal::from(4251));
    assert_eq!(
        bars[0].source.raw_payload.as_ref().unwrap()["bar_state"],
        "provisional_authoritative"
    );
    body.pop();
    assert!(jin10::decode_frame(&frame("jin10_local", body), &[gold()]).is_err());
    let mut invalid = 10007_u16.to_le_bytes().to_vec();
    invalid.extend(string("XAUUSD.GOODS"));
    invalid.extend(1_i32.to_le_bytes());
    for value in [1786027380_i64, 1, 4250000000, 4249000000, 4251000000, 10] {
        invalid.extend(value.to_le_bytes());
    }
    assert!(jin10::decode_frame(&frame("jin10_local", invalid), &[gold()]).is_err());
}

#[test]
fn manifest_and_gzip_history_match_source_count_and_replay() {
    let mut payload = string("XAUUSD.GOODS");
    payload.extend(0_i16.to_le_bytes());
    payload.extend([0, 1]);
    payload.extend((-1_i64).to_le_bytes());
    payload.push(1);
    payload.extend(string(
        "25b57cce844256b11025c73a947753b4.1.1786027380.1786027380",
    ));
    let manifest = jin10::parse_manifest(&payload).unwrap();
    assert_eq!(manifest.boundary_timestamp, -1);
    let mut gzip = GzEncoder::new(Vec::new(), Compression::default());
    gzip.write_all(&wire_candle()).unwrap();
    let envelope = json!({"provider_code":"XAUUSD.GOODS","file":manifest.files[0],"body_base64":STANDARD.encode(gzip.finish().unwrap())});
    let mut original = frame("jin10_history", serde_json::to_vec(&envelope).unwrap());
    original.encoding = "gzip-json".into();
    let (_, bars) = jin10::decode_frame(&original, &[gold()]).unwrap();
    assert_eq!(bars.len(), 1);
    assert_eq!(
        bars[0].source.raw_payload.as_ref().unwrap()["bar_state"],
        "final"
    );
    let mut wrong = envelope;
    wrong["file"]["record_count"] = json!(2);
    original.body = serde_json::to_vec(&wrong).unwrap();
    assert!(jin10::decode_frame(&original, &[gold()]).is_err());
}

#[test]
fn subscription_and_history_request_are_python_compatible() {
    let codes = vec![
        "XAUUSD.GOODS".into(),
        "XAUUSD.GOODS".into(),
        "XAGUSD.GOODS".into(),
    ];
    let packet = jin10::encode_subscription(&codes, 0, false).unwrap();
    assert_eq!(
        hex::encode(packet),
        "13270000000002000c005841555553442e474f4f44530c005841475553442e474f4f4453"
    );
    assert_eq!(
        hex::encode(jin10::encode_history_request("XAUUSD.GOODS", 1786000000).unwrap()),
        "16270c005841555553442e474f4f4453018032746a000000000100ff"
    );
}

#[tokio::test]
async fn websocket_transport_exchanges_frames_on_loopback() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let received = ws.next().await.unwrap().unwrap();
        ws.send(received).await.unwrap();
    });
    let mut socket = jin10::connect_websocket(&format!("ws://{address}"), None)
        .await
        .unwrap();
    socket
        .send(tokio_tungstenite::tungstenite::Message::Binary(
            vec![1, 2, 3].into(),
        ))
        .await
        .unwrap();
    assert_eq!(
        socket.next().await.unwrap().unwrap().into_data().as_ref(),
        &[1, 2, 3]
    );
    server.await.unwrap();
}

#[test]
fn recorded_nats_frames_preserve_quotes_and_recover_actual_desktop_candle_layouts() {
    let fixtures: Vec<serde_json::Value> =
        serde_json::from_str(include_str!("fixtures/jin10-recorded.json")).unwrap();
    // The newly captured Python fixture uses the authoritative OTC catalog metadata.
    let instruments = [Instrument { venue:Some("OTC".into()), ..gold() }, Instrument { symbol: "XAG/USD".into(), base: Some("XAG".into()), venue:Some("OTC".into()), ..gold() }];
    let mut candle_count = 0;
    for fixture in fixtures {
        let frame: ProviderFrame = serde_json::from_value(fixture["frame"].clone()).unwrap();
        let (quotes, candles) = jin10::decode_frame(&frame, &instruments).unwrap();
        let mut expected_quotes: Vec<tracefang_core::domain::QuoteSnapshot> =
            serde_json::from_value(fixture["quotes"].clone()).unwrap();
        for quote in &mut expected_quotes {
            if let Some(raw)=quote.source.raw_payload.as_mut() {
                if let Some(value)=raw["sequence"].as_u64(){raw["sequence"]=json!(value.to_string());}
                for key in ["ask","buy","previous_close"] {
                    if let Some(value)=raw[key].as_str(){raw[key]=json!(value.parse::<Decimal>().unwrap().to_string());}
                }
            }
        }
        assert_eq!(quotes, expected_quotes);
        if let Some(rows) = fixture["observed_wire_candles"].as_array() {
            assert_eq!(candles.len(), rows.len());
            candle_count += candles.len();
            for (candle, expected) in candles.iter().zip(rows) {
                assert_eq!(candle.open_time.timestamp(), expected["timestamp"].as_i64().unwrap());
                for (name,value) in [("open",&candle.open),("high",&candle.high),("low",&candle.low),("close",&candle.close),("volume",candle.volume.as_ref().unwrap())] {
                    assert_eq!(value, &expected[name].as_str().unwrap().parse::<Decimal>().unwrap());
                }
                assert_eq!(candle.source.received_at, frame.received_at);
            }
        }
    }
    assert_eq!(candle_count, 61);
}

#[test]
fn tiny_return_never_exceeds_decimal_scale_or_panics_during_json_serialization() {
    let frame: ProviderFrame = serde_json::from_str(include_str!("fixtures/jin10-decimal-regression.json")).unwrap();
    let instruments = [gold(),Instrument {symbol:"XAG/USD".into(),base:Some("XAG".into()),..gold()},Instrument {symbol:"USD/CNH".into(),base:Some("USD".into()),quote:Some("CNH".into()),..gold()}];
    let (quotes,_) = jin10::decode_frame(&frame,&instruments).unwrap();
    for quote in quotes {
        assert!(quote.change_percent.as_ref().unwrap().scale()<=28);
        let encoded=serde_json::to_value(&quote).unwrap();
        assert!(encoded["change_percent"].is_string());
    }
}
