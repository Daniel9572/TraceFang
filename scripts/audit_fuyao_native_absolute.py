"""Independent absolute-date fixed-source oracle. The prior v1 oracle stays unchanged."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re


def load(path):
    return json.loads(path.read_text(), parse_float=Decimal)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ns(value):
    value = value.replace("Z", "+00:00")
    match = re.search(r"\.(\d+)(?=[+-]\d\d:\d\d$)", value)
    fraction = int((match[1] + "000000000")[:9]) if match else 0
    clean = value[:match.start()] + value[match.end():] if match else value
    delta = datetime.fromisoformat(clean).astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + fraction


def number(value):
    return None if value is None or value == "" else Decimal(str(value))


def section(payload, market, code):
    assert payload["status_code"] == 0
    rows = [row for row in payload["data"]["quote_data"] if row["market"] == market and row["code"] == code]
    assert len(rows) <= 1
    return rows[0] if rows else {"data_fields": [], "value": []}


def fields(node):
    keys = node["data_fields"]
    assert len(keys) == len(set(keys))
    for row in node["value"]:
        assert len(row) == len(keys)
        yield dict(zip(keys, row))


def ohlcv(rows):
    return [number(rows[0]["7"]), max(number(r["8"]) for r in rows), min(number(r["9"]) for r in rows), number(rows[-1]["11"]), None if any(number(r.get("13")) is None for r in rows) else sum(number(r["13"]) for r in rows)]


def absolute_session_policy(time_info):
    """One captured exact market/code trade_date, never a repeated civil time rule."""
    ranges = [(int(r["begin_time"]), int(r["end_time"]))
              for h in time_info["trade_hours"] if h["trade_phase"] == "continuous"
              for r in h["phase_range"]]
    assert ranges and all(lo < hi for lo, hi in ranges)
    ranges.sort()
    assert all(a[1] <= b[0] for a, b in zip(ranges, ranges[1:]))
    return {lo for lo, _ in ranges}, ranges


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", type=Path)
    parser.add_argument("intervals", type=Path)
    parser.add_argument("sessions", type=Path)
    parser.add_argument("native", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    native = load(args.native)
    assert native["schema"] == "fuyao-native-fixed-v2-absolute-date-calendar"
    manifests = {record["file"]: record for record in load(args.sources / "manifest.json")}
    hashes = {}
    other_manifests={str(root):{record["file"]:record for record in load(root/"manifest.json")} for root in [args.intervals,args.sessions]}
    for root in [args.sources,args.intervals,args.sessions]:
        hashes[str(root/"manifest.json")]=digest(root/"manifest.json")
    def checked(root,name):
        path=root/name;sha=digest(path);assert other_manifests[str(root)][name]["sha256"]==sha;hashes[str(path)]=sha;return load(path)
    for name, sha in native["inputs_sha256"].items():
        assert digest(args.sources / name) == sha == manifests[name]["sha256"]
        hashes[name] = sha
    snapshot_identities={(row["market"],row["code"]) for name in manifests if name.startswith("snapshot-") for row in load(args.sources/name)["data"]["quote_data"]}
    assert snapshot_identities=={(item["market"],item["provider_code"]) for item in native["items"]}
    cases = []
    for item in native["items"]:
        market, code = item["market"], item["provider_code"]
        source_snapshot = section(load(args.sources / f"snapshot-{market}.json"), market, code)
        quote_fields = list(fields(source_snapshot))
        assert len(quote_fields) == 1
        quote_fields = quote_fields[0]
        quote = item["quote"]
        if number(quote_fields.get("10")) is None:
            assert quote is None and "last is missing" in item["quote_error"]
            quote_state = "source_success_price_unknown"
        else:
            assert quote is not None
            for public, source in [("last", "10"), ("open", "7"), ("high", "8"), ("low", "9"), ("volume", "13"), ("change", "264648"), ("change_percent", "199112")]:
                assert number(quote[public]) == number(quote_fields.get(source)), (code, public)
            assert ns(quote["source"]["observed_at"]) == int(quote_fields["1"]) * 10**6
            assert ns(quote["source"]["received_at"]) == ns(manifests[f"snapshot-{market}.json"]["received_at"])
            assert quote["source"]["provider_symbol"] == f"fuyao:{market}:{code}"
            assert quote["source"]["raw_payload"]["source_fields"]["55"] == quote_fields["55"]
            quote_state = "pass_exact_fields"
        raw = list(fields(section(load(args.sources / f"kline-{market}-{code}.json"), market, code)))
        source_time = checked(args.sessions,f"{market}-{code}.json")["data"]["time_info"]
        time_info = next(row for row in source_time if row["market"] == market and row["code"] == code)
        starts, absolute_ranges = absolute_session_policy(time_info)
        intervals = list(fields(section(checked(args.intervals,f"{market}-{code}-5m.json"), market, code)))
        by_label = {int(row["1"]): row for row in raw}
        assert len(by_label) == len(raw)
        proved_points = set()
        comparisons = {"windows": 0, "time_end": 0, "volume": 0, "close": 0, "open": 0, "high": 0, "low": 0, "full_ohlcv": 0, "proved_opening_points": 0, "with_zero_quantity":0, "with_unknown_quantity":0, "positive_quantity_price_probe_matches":0, "positive_quantity_price_probe_windows":0}
        differences = []
        for interval in intervals:
            end = int(interval["1"])
            labels = [end - step * 60000 for step in range(4, -1, -1)]
            if not all(label in by_label for label in labels):
                continue
            selected = [by_label[label] for label in labels]
            expected = [number(interval[k]) for k in ["7", "8", "9", "11", "13"]]
            observed = ohlcv(selected)
            point = by_label.get(end - 5 * 60000)
            if point and int(point["1"]) // 1000 in starts and len({number(point[k]) for k in ["7", "8", "9", "11"]}) == 1:
                folded = ohlcv([point] + selected)
                if observed != expected and folded == expected:
                    observed = folded
                    proved_points.add(int(point["1"]))
            comparisons["windows"] += 1
            for index, name in enumerate(["open", "high", "low", "close", "volume"]):
                comparisons[name] += observed[index] == expected[index]
            comparisons["full_ohlcv"] += observed == expected
            comparisons["with_zero_quantity"] += any(number(row.get("13")) == 0 for row in selected)
            comparisons["with_unknown_quantity"] += any(number(row.get("13")) is None for row in selected)
            positive=[row for row in selected if number(row.get("13")) is not None and number(row["13"]) > 0]
            if positive:
                comparisons["positive_quantity_price_probe_windows"] += 1
                comparisons["positive_quantity_price_probe_matches"] += ohlcv(positive)[:4] == expected[:4]
            # Close and quantity agreement are independent evidence, not a blanket OHLC/time assertion.
            comparisons["time_end"] += observed[3:] == expected[3:]
            if observed != expected and len(differences) < 5:
                differences.append({"end_label_ms": str(end), "source_5m": list(map(str, expected)), "all_1m_components": list(map(str, observed)), "positive_quantity_price_probe":list(map(str,ohlcv(positive)[:4])) if positive else None,"component_quantities":[str(number(row.get("13"))) for row in selected], "policy": "diagnostic_only; no zero-component deletion or proved general source price-selection policy; canonical_1m_retained"})
        comparisons["proved_opening_points"] = len(proved_points)
        pending = {}
        expected_rows = []
        for row in raw:
            label = int(row["1"])
            opening = label // 1000 in starts and len({number(row[k]) for k in ["7", "8", "9", "11"]}) == 1
            if opening:
                if label in proved_points:
                    pending[label] = row
                continue
            members = ([pending.pop(label - 60000)] if label - 60000 in pending else []) + [row]
            expected_rows.append((label, ohlcv(members), len(members)))
        assert len(item["bars"]) == len(expected_rows), (code, len(item["bars"]), len(expected_rows))
        source_calendar_unverified = 0
        source_points_folded = 0
        for bar, (label, expected, component_count) in zip(item["bars"], expected_rows):
            assert ns(bar["open_time"]) == (label - 60000) * 10**6, code
            assert ns(bar["source"]["observed_at"]) == label * 10**6, code
            assert ns(bar["source"]["received_at"]) == ns(manifests[f"kline-{market}-{code}.json"]["received_at"]), code
            assert [number(bar[key]) for key in ["open", "high", "low", "close", "volume"]] == expected, (code, label)
            metadata = bar["source"]["raw_payload"]
            assert metadata["source_precision_ns"] == "1000000"
            assert metadata["source_calendar_trade_date"] == time_info["trade_date"]
            known = any(lo <= label // 1000 <= hi for lo, hi in absolute_ranges)
            assert metadata["source_calendar_verified_for_label"] is known
            source_calendar_unverified += not known
            assert metadata["source_label_semantics"] == "interval_end"
            assert ns(metadata["source_interval_end"]) == label * 10**6
            assert metadata["source_interval_end_ns"] == str(label * 10**6)
            assert metadata["source_timestamp_ms"] == str(label)
            assert metadata["publication_time_unknown"] is True and metadata["published_at"] is None
            # Verify quantities against the original referenced components. This
            # checks lineage and NULL coverage separately from min5 price policy.
            components = metadata.get("components", [])
            component_labels = ([int(v["source_label_ms"]) for v in components]
                                if components else [label])
            assert component_labels[-1] == label
            assert len(set(component_labels)) == len(component_labels)
            original_components = [by_label[v] for v in component_labels]
            if components:
                assert component_labels == [label - 60000, label]
                assert (label - 60000) // 1000 in starts
                assert len({number(original_components[0][k]) for k in ["7", "8", "9", "11"]}) == 1
                source_points_folded += 1
            quantities = [number(v.get("13")) for v in original_components]
            coverage = metadata["source_volume_components"]
            assert int(coverage["total_count"]) == len(quantities)
            assert int(coverage["known_count"]) == sum(v is not None for v in quantities)
            assert number(coverage["known_volume_sum"]) == sum((v for v in quantities if v is not None), Decimal(0))
            assert number(bar["volume"]) == (None if any(v is None for v in quantities) else sum(quantities))
            assert coverage["policy"] == "fuyao-source-field-13-interval-v1"
            source_as_of = int(quote_fields["1"]) * 10**6 if quote is not None else None
            received_ns = ns(manifests[f"kline-{market}-{code}.json"]["received_at"])
            confirmed = source_as_of is not None and label * 10**6 <= source_as_of <= received_ns
            assert metadata["bar_state"] == ("final" if confirmed else "provisional_authoritative")
            if confirmed:
                assert metadata["finality_evidence"]["kind"] == "source_clock_reached_interval_end"
                assert ns(metadata["finality_evidence"]["source_as_of"]) == source_as_of
            assert "unclassified_source_points" not in metadata
            if "source_point_quarantine_ref" in metadata:
                reference = metadata["source_point_quarantine_ref"]
                assert int(reference["count"]) > 0
                assert len(reference["points_sha256"]) == 64
                assert len(json.dumps(reference, ensure_ascii=False).encode()) <= 1024
            if component_count == 2:
                assert len(bar["source"]["raw_payload"]["components"]) == 2
        cases.append({"code": item["code"], "source_instrument": {"market": market, "code": code}, "quote_state": quote_state, "source_rows": len(raw), "canonical_rows": len(expected_rows), "history_state": "pass_exact_original_source_minutes" if raw else "source_success_empty", "source_min5_dimensions": comparisons, "min5_price_policy_state": "evidence_insufficient" if differences else "all_retained_windows_agree", "difference_samples": differences, "recurring_calendar": "not_inferred_from_one_trade_date", "verified_trade_date": time_info["trade_date"], "absolute_continuous_sessions": [[str(lo),str(hi)] for lo,hi in absolute_ranges], "source_calendar_unverified_rows_retained": source_calendar_unverified, "folded_source_points": source_points_folded})
    report = {"schema": "fuyao-independent-decimal-native-acceptance-v2-absolute-date", "native_sha256": digest(args.native), "inputs_sha256": hashes, "oracle_source_sha256": digest(Path(__file__)), "contracts": len(cases), "snapshot_prices_available": sum(c["quote_state"] == "pass_exact_fields" for c in cases), "minute_history_nonempty": sum(c["source_rows"] > 0 for c in cases), "canonical_rows": sum(c["canonical_rows"] for c in cases), "cases": cases, "policy": "Captured absolute trade_date sessions classify opening points only on that actual date; unknown-date raw minutes remain with explicit coverage. Decimal/NULL and source-component lineage are checked independently from native-min5 OHLC policy, which remains evidence-insufficient where any fixed counterexample exists. Snapshot exchange-tick coverage and recurring/holiday calendars are not inferred."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({key: report[key] for key in ["contracts", "snapshot_prices_available", "minute_history_nonempty", "canonical_rows"]}))


if __name__ == "__main__":
    with localcontext() as context:
        context.prec = 10000
        main()
