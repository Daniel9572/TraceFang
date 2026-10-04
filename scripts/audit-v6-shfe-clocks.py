"""Bounded inspection of original retained envelopes, never normalized facts."""
import base64
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = Path("/Users/daniel/Library/Application Support/TraceFang/migration-sources/legacy-export-b9f4e713-2d54-40fd-9cb2-f5ada4942baf")
SCOPES = ("qh_au2610", "qh_au8888", "qh_ag2706", "qh_ag8888")
LIMIT = 5000


def timestamp(label):
    return dt.datetime.strptime(label, "%Y%m%d%H%M").replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))


def decode_jsonp(body, encoding):
    text = body.decode(encoding)
    begin = text.index("(") + 1
    return json.loads(text[begin:text.rindex(")")], parse_float=Decimal)


def main():
    manifest = json.loads((ARCHIVE / "manifest.json").read_text())
    mappings = []
    with (ARCHIVE / manifest["raw"]["native_mapping"]["file"]).open() as source:
        for _, line in zip(range(LIMIT), source):
            mappings.append(json.loads(line))
    witnesses = {code: {"history": [], "quote": []} for code in SCOPES}
    examined = 0
    with (ARCHIVE / manifest["raw"]["file"]).open("rb") as source:
        for index in range(LIMIT):
            prefix = source.read(4)
            if not prefix:
                break
            length = struct.unpack(">I", prefix)[0]
            assert length <= 65536
            header_bytes = source.read(length)
            header = json.loads(header_bytes)
            assert header["body_bytes"] <= 48 * 1024 * 1024
            body = source.read(header["body_bytes"])
            assert len(body) == header["body_bytes"]
            examined += 1
            channel = header["headers"].get("Market-Frame-Channel", [None])[0]
            if channel not in ("tonghuashun_futures", "tonghuashun_futures_history"):
                continue
            assert hashlib.sha256(body).hexdigest() == header["body_sha256"]
            envelope = json.loads(body)
            code = envelope.get("provider_code")
            if code not in witnesses or envelope.get("status_code") != 200:
                continue
            kind = envelope.get("kind")
            if kind not in ("minute_year", "minute_last", "time"):
                continue
            content = base64.b64decode(envelope["content_base64"], validate=True)
            payload = decode_jsonp(content, envelope["text_encoding"])
            mapping = mappings[index]
            assert mapping["legacy_sequence"] == header["sequence"]
            received_text = header["headers"]["Market-Frame-Received-At"][0]
            received = dt.datetime.fromisoformat(received_text.replace("Z", "+00:00"))
            record = {"legacy_sequence": header["sequence"], "native_position": mapping["native_position"],
                      "provider_sequence": header["headers"]["Market-Frame-Sequence"][0], "kind": kind,
                      "body_sha256": header["body_sha256"], "content_sha256": hashlib.sha256(content).hexdigest(),
                      "header_sha256": hashlib.sha256(header_bytes).hexdigest(), "received_at": received_text,
                      "broker_stored_at_ns": header["broker_stored_at_ns"], "source_url": envelope["request_url"]}
            if kind == "time":
                value = payload[code]
                tail = value["data"].split(";")[-1].split(",")
                record.update({"date": value.get("date"), "dates": value.get("dates"), "last_source_row": tail})
                if len(witnesses[code]["quote"]) < 4:
                    witnesses[code]["quote"].append(record)
            else:
                lines = [line.split(",") for line in payload["data"].split(";") if line]
                tail = timestamp(lines[-1][0])
                label_ns = int(tail.timestamp()) * 1_000_000_000
                receipt_ns = int(mapping["frame_received_at_ns"])
                opening = []
                session_end_labels = {}
                for row in lines:
                    clock = row[0][-4:]
                    if clock in ("0230", "1500"):
                        session_end_labels[clock] = session_end_labels.get(clock, 0) + 1
                    if clock in ("2100", "0900", "1030", "1330") and all(Decimal(row[1]) == Decimal(v) for v in row[2:5]):
                        if len(opening) < 6:
                            opening.append(row)
                record.update({"source_name": payload.get("name"), "rows": len(lines), "first_source_row": lines[0],
                               "last_source_rows": lines[-3:], "last_label_ns": str(label_ns),
                               "last_label_minus_received_ns": str(label_ns - receipt_ns),
                               "end_hypothesis_tail_start_ns": str(label_ns - 60_000_000_000),
                               "end_hypothesis_tail_is_forming_at_receipt": label_ns > receipt_ns >= label_ns - 60_000_000_000,
                               "start_hypothesis_tail_is_future_at_receipt": label_ns > receipt_ns,
                               "source_state_or_finality_fields": {key: payload.get(key) for key in ("bar_state", "final", "finalized_at")},
                               "session_end_label_counts": session_end_labels, "unproved_opening_point_examples": opening})
                if len(witnesses[code]["history"]) < 3:
                    witnesses[code]["history"].append(record)
    result = {"schema": "retained-v6-four-shfe-clock-witness-v1", "scope": list(SCOPES), "records_examined": examined,
              "archive": {"path": str(ARCHIVE / manifest["raw"]["file"]), "full_file_sha256_from_previously_verified_import": manifest["raw"]["sha256"],
                          "mapping_sha256_from_previously_verified_import": manifest["raw"]["native_mapping"]["sha256"],
                          "selected_bodies_and_headers_freshly_verified": True, "full_file_rehash_in_this_bounded_audit": False},
              "witnesses": witnesses, "decoder_or_fact_changes": False,
              "limitations": ["received clock is actual captured local clock, not invented source publication time",
                               "opening points remain independent source evidence; no unproved fold or subtraction applied",
                               "END hypotheses do not change original source OHLCV or equate Fuyao and v6 channels"]}
    output = ROOT / ".mypowers/work/rust-migration/validation/market/v6-four-shfe-clock-witness.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(output), "records_examined": examined, "scopes": {key: {"history": len(value["history"]), "quote": len(value["quote"]), "forming_end_witness": any(row["end_hypothesis_tail_is_forming_at_receipt"] for row in value["history"])} for key, value in witnesses.items()}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
