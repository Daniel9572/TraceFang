"""Compare source text with Decimal; never use normalized application bars as oracle."""
import datetime as dt
from decimal import Decimal
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".mypowers/evidence/market/au2610-channel-comparison-2026-10-04-01"
LEGACY = ROOT / ".mypowers/work/market-data/full-audit-2026-10-02/baseline"
OUT = ROOT / ".mypowers/work/rust-migration/validation/market/au2610-channel-comparison.json"

def read(path):
    text = path.read_text()
    if text.lstrip().startswith("{"):
        return json.loads(text, parse_float=Decimal)
    return json.loads(text[text.index("(") + 1:text.rindex(")")], parse_float=Decimal)

def minute_label(value):
    return int(dt.datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()) * 1000

def number(value):
    return None if value is None or str(value).strip() == "" else Decimal(str(value))

def node(filename):
    value = read(SOURCE / filename)
    assert value["status_code"] == 0
    candidates = [r for r in value["data"]["quote_data"] if r["market"] == "65" and r["code"] == "au2610"]
    assert len(candidates) == 1
    return candidates[0]

def rows(filename):
    value = node(filename)
    return [{key: field for key, field in zip(value["data_fields"], row)} for row in value["value"]]

if __name__ == "__main__":
    manifest = json.loads((SOURCE / "manifest.json").read_text())
    for item in manifest:
        assert item["status"] == 200
        assert hashlib.sha256((SOURCE / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    old = {}
    for filename in ["source-AU2610-2026.js", "source-AU2610-last.js"]:
        for line in read(LEGACY / filename)["data"].split(";"):
            if not line:
                continue
            fields = line.split(",")
            old[minute_label(fields[0])] = [number(x) for x in fields[1:6]]
    current = rows("fuyao-min-1.json")
    fields = ["open", "high", "low", "close", "volume"]
    comparisons = []
    for shift in [0, -60_000, 60_000]:
        matched = {field: 0 for field in fields}
        overlap = 0
        examples = []
        for row in current:
            label = int(row["1"])
            previous = old.get(label + shift)
            if previous is None:
                continue
            overlap += 1
            actual = [number(row[key]) for key in ["7", "8", "9", "11", "13"]]
            equal = [a == b for a, b in zip(actual, previous)]
            for field, valid in zip(fields, equal):
                matched[field] += int(valid)
            if not all(equal) and len(examples) < 8:
                examples.append({"fuyao_label_ms": str(label), "v6_label_ms": str(label + shift), "fuyao_ohlcv": list(map(str, actual)), "v6_ohlcv": list(map(str, previous))})
        comparisons.append({"v6_label_minus_fuyao_label_ms": str(shift), "overlap": overlap, "matches_by_field": matched, "counterexamples": examples})
    snapshot = rows("fuyao-snapshot.json")[0]
    time = read(SOURCE / "v6-time.js")["qh_au2610"]
    v6_last = time["data"].split(";")[-1].split(",")
    last = number(snapshot["10"])
    previous_settlement = number(time["pre"])
    source_change = number(snapshot["264648"])
    result = {"contract": "AU2610", "channels": ["tonghuashun_public_line_v6", "tonghuashun_fuyao"], "identity": {"v6": "qh_au2610", "fuyao_market": "65", "fuyao_code": "au2610", "v6_name": time["name"], "fuyao_name": snapshot["55"]}, "inputs": [{"path": str(p.relative_to(ROOT)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in [LEGACY / "source-AU2610-2026.js", LEGACY / "source-AU2610-last.js", SOURCE / "manifest.json"]], "source_rows": {"fuyao_minute": len(current), "v6_unique_minute_labels": len(old)}, "minute_label_comparisons": comparisons, "snapshot": {"last": str(last), "v6_last": v6_last[1], "last_equal": last == number(v6_last[1]), "fuyao_previous_close_field6": str(snapshot["6"]), "v6_previous_settlement_pre": str(previous_settlement), "reported_change": str(source_change), "last_minus_v6_previous_settlement": str(last - previous_settlement), "last_minus_fuyao_previous_close": str(last - number(snapshot["6"])), "reported_change_uses_previous_settlement_in_this_fixed_sample": source_change == last - previous_settlement, "fuyao_reported_change_basis_not_inferred_as_universal_protocol": True}, "clock_policy": {"fuyao": "fuyao-interval-end-v1 independently established by official 5m windows; source timestamp milliseconds", "legacy_current_decoder": "v6 source label currently used as normalized open; existing raw decoder unchanged", "raw_labels_not_normalized_for_this_comparison": True, "equal_label_ohlcv_does_not_change_legacy_projection_or_establish_universal_v6_clock_semantics": True}, "merge_applied": False}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(OUT), "minute_label_comparisons": [{k: v for k, v in row.items() if k != "counterexamples"} for row in comparisons], "snapshot": result["snapshot"]}, ensure_ascii=False))
