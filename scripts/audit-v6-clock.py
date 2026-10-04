"""Test label hypotheses from fixed exact source text, independently of Rust.

This reports evidence; it does not rewrite either source or select a migration.
"""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
helper = runpy.run_path(str(ROOT / "scripts/audit-au-channel-comparison.py"))
read, minute_label, number = (helper[k] for k in ("read", "minute_label", "number"))
SOURCE, LEGACY = helper["SOURCE"], helper["LEGACY"]
FIELDS = ("open", "high", "low", "close", "volume")


def verify(path, entries):
    entry = next(e for e in entries if e["file"] == path.name)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest == entry["sha256"], path
    return {"path": str(path.relative_to(ROOT)), "sha256": digest, "receipt": entry}


def aggregate(rows):
    quantities = [r[1][4] for r in rows]
    return [rows[0][1][0], max(r[1][1] for r in rows), min(r[1][2] for r in rows), rows[-1][1][3],
            sum(quantities, Decimal(0)) if all(v is not None for v in quantities) else None]


def main():
    source_manifest = json.loads((SOURCE / "manifest.json").read_text())
    legacy_manifest = json.loads((LEGACY / "manifest.json").read_text())
    inputs = []
    v6 = {}
    for filename in ("source-AU2610-2026.js", "source-AU2610-last.js"):
        path = LEGACY / filename
        inputs.append(verify(path, legacy_manifest))
        payload = read(path)
        for line in payload["data"].split(";"):
            if line:
                fields = line.split(",")
                v6[minute_label(fields[0])] = [number(v) for v in fields[1:6]]
    for filename in ("fuyao-min-1.json", "fuyao-min-5.json", "v6-time.js"):
        inputs.append(verify(SOURCE / filename, source_manifest))
    fuyao1 = {int(row["1"]): [number(row[k]) for k in ("7", "8", "9", "11", "13")] for row in helper["rows"]("fuyao-min-1.json")}
    fuyao5 = {int(row["1"]): [number(row[k]) for k in ("7", "8", "9", "11", "13")] for row in helper["rows"]("fuyao-min-5.json")}
    hypotheses = []
    for name, start_labels in (("v6 label is interval END", False), ("v6 label is interval START", True)):
        matched = dict.fromkeys(FIELDS, 0)
        tested = 0
        examples = []
        for end, expected in sorted(fuyao5.items()):
            start = end - 300_000
            members = sorted((label, values) for label, values in v6.items() if
                             (start <= label < end if start_labels else start < label <= end))
            if len(members) != 5:
                continue
            tested += 1
            actual = aggregate(members)
            equal = [a == b for a, b in zip(actual, expected)]
            for key, good in zip(FIELDS, equal):
                matched[key] += int(good)
            if not all(equal) and len(examples) < 12:
                examples.append({"source_5m_end_label_ms": str(end), "component_labels_ms": [str(t) for t, _ in members],
                                 "v6_aggregate": [None if v is None else str(v) for v in actual],
                                 "official_fuyao_5m": [None if v is None else str(v) for v in expected]})
        hypotheses.append({"hypothesis": name, "five_regular_components_tested": tested,
                           "matches_by_field": matched, "counterexamples": examples,
                           "opening_fold_or_special_component_policy_not_guessed": True})
    pairs = [(label, v6[label], values) for label, values in fuyao1.items() if label in v6]
    same_label_matches = {key: sum(a[i] == b[i] for _, a, b in pairs) for i, key in enumerate(FIELDS)}
    result = {"schema": "independent-v6-end-label-hypothesis-v1", "contract": "AU2610", "inputs": inputs,
              "same_raw_label_overlap": len(pairs), "same_label_matches_by_field": same_label_matches,
              "current_decoder_mapping": {"v6_regular_open": "raw label", "fuyao_regular_open": "raw label minus 60 seconds"},
              "hypotheses": hypotheses,
              "canonical_closing_boundary_witness": {"last_v6_label_ms": str(max(v6)), "same_fuyao_label": str(max(fuyao1)),
                  "source_day_continuous_close": "15:00 Asia/Shanghai (current hours; not inferred for all history)",
                  "start_label_hypothesis_would_end_last_minute_after_close": "15:01 Asia/Shanghai"},
              "limitations": ["fixed exact AU2610 sample; two channels are independently identified",
                               "matching label/value evidence does not equate channels or alter their source OHLCV",
                               "5m aggregation counterexamples remain source policy differences, not license to remove zero-volume minutes",
                               "no original tick stream proves which source assigned opening trades correctly",
                               "this report does not change original PG facts, captured bodies, or decoder code"], "migration_applied": False}
    output = ROOT / ".mypowers/work/rust-migration/validation/market/v6-clock-hypotheses.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "same_label_overlap": len(pairs), "same_label_matches": same_label_matches,
                      "hypotheses": [{k: v for k, v in r.items() if k != "counterexamples"} for r in hypotheses]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
