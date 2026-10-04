"""Retain evidence for all configured markets, not only the default watchlist.

Run with PYTHONPATH=src .venv/bin/python scripts/audit_market_data.py OUTPUT.
Public reference files are evidence, not a claim of complete client coverage.
No orders, source switches or watchlist mutations are performed here.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import httpx
from tracefang.application.period_bars import PERIOD_DEFINITIONS
from tracefang.environment import load_project_environment
from tracefang.infrastructure.postgres.settings import PostgresSettings
from tracefang.infrastructure.providers.tonghuashun_futures.symbols import (
    TonghuashunFuturesSymbolMapper,
)
from tracefang.instruments import INSTRUMENT_CATALOG


def number(value):
    return None if value is None or str(value).strip() == "" else Decimal(str(value))


def jsonp(text):
    return json.loads(text[text.index("(") + 1 : text.rindex(")")])


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def table(path, rows):
    if rows:
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def cached_response(output, name, url, previous_manifest):
    """Only reuse bytes proven to be a successful response to this exact URL."""
    record = next((x for x in reversed(previous_manifest) if x.get("file") == name), {})
    path = output / name
    if record.get("status") != 200 or record.get("url") != url or not path.exists():
        return None
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != record.get("sha256"):
        return None
    try:
        value = json.loads(content) if name.endswith(".json") else None
    except ValueError:
        return None
    if isinstance(value, dict) and ("error" in value or "detail" in value):
        return None
    return content


def inspect_bars(bars):
    if not bars:
        return {"count": 0, "invalid": 0, "result": "empty"}
    times, invalid = [], 0
    for bar in bars:
        try:
            times.append(datetime.fromisoformat(bar["open_time"]))
            o, h, low, c = (number(bar[x]) for x in ("open", "high", "low", "close"))
            volume = number(bar.get("volume"))
            valid = all(x is not None and x.is_finite() for x in (o, h, low, c))
            valid = valid and low <= min(o, c) <= max(o, c) <= h
            valid = valid and (volume is None or (volume.is_finite() and volume >= 0))
            invalid += not valid
        except (ValueError, TypeError, KeyError, ArithmeticError):
            invalid += 1
    if times != sorted(set(times)):
        invalid += 1
    return {
        "count": len(bars),
        "invalid": invalid,
        "result": "invalid" if invalid else "structural_pass",
    }


def compare_option_contracts(api_contracts, source_rows, master_rows):
    """Compare fields only when the source and API have the same observation time."""
    raw_by_id = {x["contractname"]: x for x in source_rows}
    master_by_id = {x["INSTRUMENTID"]: x for x in master_rows}
    api_by_id = {x["contract_id"]: x for x in api_contracts}
    fields = {
        "last": "lastprice",
        "bid": "bidprice",
        "ask": "askprice",
        "previous_settlement": "presettlementprice",
        "volume": "volume",
        "open_interest": "openinterest",
        "open_interest_change": "openinterestchg",
        "turnover": "turnover",
    }
    comparisons = []
    for contract_id in sorted(set(raw_by_id) | set(api_by_id)):
        api, source = api_by_id.get(contract_id), raw_by_id.get(contract_id)
        errors, result = [], "pass"
        if api is None:
            errors, result = ["missing_api"], "not_integrated"
        elif source is None or contract_id not in master_by_id:
            errors, result = ["missing_source_or_metadata"], "insufficient_evidence"
        else:
            try:
                source_time = datetime.strptime(source["updatetime"], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=ZoneInfo("Asia/Shanghai")
                )
                if datetime.fromisoformat(api["observed_at"]) != source_time:
                    errors, result = ["observation_changed"], "insufficient_evidence"
                else:
                    errors.extend(
                        dest
                        for dest, origin in fields.items()
                        if number(api[dest]) != number(source.get(origin))
                    )
                    spec = master_by_id[contract_id]
                    if api["expiry"].replace("-", "") != spec["EXPIREDATE"]:
                        errors.append("expiry")
                    if number(api["contract_multiplier"]) != number(spec["TRADEUNIT"]):
                        errors.append("multiplier")
                    match = re.fullmatch(r"([a-z]{1,6}\d{3,4})([CP])(\d+(?:\.\d+)?)", contract_id)
                    if match is None or (
                        api["underlying_contract_id"],
                        api["option_type"],
                        number(api["strike"]),
                    ) != (match[1], "call" if match[2] == "C" else "put", number(match[3])):
                        errors.append("contract_identity")
                    result = "fail" if errors else "pass"
            except (KeyError, ValueError, TypeError, ArithmeticError):
                errors, result = ["invalid_contract_fields"], "fail"
        comparisons.append(
            {
                "contract": contract_id,
                "result": result,
                "errors": ",".join(errors),
                "last": api.get("last") if api else None,
                "observed_at": api.get("observed_at") if api else None,
            }
        )
    return comparisons


def fixed_audit(output, baseline, native_file):
    """Pure fixed-input coverage. No fetch, source mutation or database connection."""
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((baseline / "manifest.json").read_text())
    records = {r["file"]: r for r in manifest if r.get("sha256")}
    proof = []
    for name, record in records.items():
        path = baseline / name
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        proof.append({"file": name, "expected_sha256": record["sha256"], "actual_sha256": actual,
                      "result": "pass" if actual == record["sha256"] else "evidence_insufficient"})
    def read(name):
        path = baseline / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if records.get(name, {}).get("sha256") != digest:
            raise ValueError(f"required captured evidence failed SHA256: {name}")
        return json.loads(path.read_text(), parse_float=Decimal)
    native = json.loads(native_file.read_text(), parse_float=Decimal)
    for name, digest in native["input_sha256"].items():
        read(name)
        if records[name]["sha256"] != digest:
            raise ValueError(f"native normalization used different evidence: {name}")
    catalog = native["catalog"]
    reference = read("ths-main-contracts.json")["data"]["result"]
    futures = []
    for row in reference:
        exact = next((item for item in catalog if item["provider_code"].upper() == row["contractCode"].upper()
                      and "tonghuashun_futures" in item["source_ids"]
                      and item["instrument"]["venue"] == row["marketCode"]), None)
        futures.append({"exchange": row["marketCode"], "variety": row["variety"], "name": row["varietyName"],
                        "main_contract": row["contractCode"], "result": "evidence_insufficient" if exact else "unconnected",
                        "reason": "exact source instrument requires a same-sample native quote" if exact else "no exact source instrument in current catalog",
                        "related_local_series": ",".join(item["provider_code"] for item in catalog
                            if item["instrument"]["venue"] == row["marketCode"] and (item["instrument"].get("base") or "").upper() == row["variety"].upper())})
    master = read("options-source-2.json")["OptionContractBaseInfo"]
    raw = read("options-source-0.json")["delaymarket"]
    old = read("options-api.json")
    comparisons = compare_option_contracts(native["contracts"], raw, master)
    old_comparisons = compare_option_contracts(old["contracts"], raw, master)
    result_by_id = {row["contract"]: row for row in comparisons}
    native_metadata = {row["contract_id"]: row for row in native["master_contracts"]}
    connected = set(native["connected_option_products"])
    options = []
    for source in master:
        identity = source["INSTRUMENTID"]
        metadata = native_metadata.get(identity)
        match = re.fullmatch(r"([a-z]{1,6}\d{3,4})([CP])(\d+(?:\.\d+)?)", identity)
        errors = []
        if metadata is None or match is None:
            errors.append("missing_native_metadata_or_identity")
        else:
            for key, expected in {"product": source["COMMODITYID"], "underlying_contract_id": match[1],
                                  "option_type": "call" if match[2] == "C" else "put",
                                  "expiry": datetime.strptime(source["EXPIREDATE"], "%Y%m%d").date().isoformat()}.items():
                if metadata.get(key) != expected:
                    errors.append(key)
            if number(metadata.get("strike")) != number(match[3]):
                errors.append("strike")
            if number(metadata.get("contract_multiplier")) != number(source["TRADEUNIT"]):
                errors.append("multiplier")
        quote = result_by_id.get(identity)
        state = ("fail" if errors else ("unconnected" if source["COMMODITYID"] not in connected else
                 {"not_integrated":"unconnected","insufficient_evidence":"evidence_insufficient"}.get(quote["result"],quote["result"])
                 if quote else "evidence_insufficient"))
        options.append({"contract": identity, "product": source["COMMODITYID"], "expiry": source["EXPIREDATE"],
                        "metadata_result": "fail" if errors else "pass", "metadata_errors": ",".join(errors),
                        "result": state, "quote_errors": quote["errors"] if quote else "no captured/native price for this product"})
    products = [{"product": product, "master_contracts": sum(row["product"] == product for row in options),
                 "results": dict(Counter(row["result"] for row in options if row["product"] == product))}
                for product in sorted({row["product"] for row in options})]
    structures = []
    for name in sorted(records):
        if name.startswith("bars-") and name.endswith(".json") and (baseline/name).is_file():
            payload = read(name)
            structures.append({"file": name, **inspect_bars(payload if isinstance(payload,list) else payload.get("items",[]))})
    table(output/"futures.csv", futures); table(output/"options.csv", options)
    save(output/"products.json",products); save(output/"source-integrity.json",proof)
    save(output/"legacy-options-discrepancies.json",[row for row in old_comparisons if row["result"] != "pass"])
    save(output/"fixed-bar-structures.json",structures)
    save(output/"summary.json",{"schema":"fixed-market-acceptance-v1","checked_at":datetime.now(UTC),
        "baseline":str(baseline.resolve()),"native_file_sha256":hashlib.sha256(native_file.read_bytes()).hexdigest(),
        "reference_futures":len(futures),"futures_results":dict(Counter(row["result"] for row in futures)),
        "option_master_contracts":len(options),"option_master_products":len(products),
        "option_results":dict(Counter(row["result"] for row in options)),
        "native_metadata_results":dict(Counter(row["metadata_result"] for row in options)),
        "native_connected_products":sorted(connected),"captured_price_rows":len(raw),
        "unknown_volume_preserved":sum(row["volume"] is None for row in native["contracts"]),
        "legacy_quote_results":dict(Counter(row["result"] for row in old_comparisons)),
        "source_integrity_results":dict(Counter(row["result"] for row in proof)),
        "limits":["fixed evidence only; no live acceptance", "master metadata pass does not accept an unconnected product",
                  "reference main contract and continuous/local alternatives remain separate identities",
                  "all prices are exact decimals; signed zero/negative prices are permitted; unknown quantity is not zero",
                  "bar structural checks do not establish source-price agreement"]})


async def audit(
    output, base_url, *, reuse=False, skip_periods=False, page_size=500, reference_file=None
):
    output.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    manifest, quotes, periods, minute_checks = [], [], [], []
    previous_manifest = []
    if reuse and (output / "manifest.json").exists():
        previous_manifest = json.loads((output / "manifest.json").read_text())
    gate = asyncio.Semaphore(1)
    async with httpx.AsyncClient(
        timeout=90,
        follow_redirects=True,
        mounts={"http://127.0.0.1": httpx.AsyncHTTPTransport()},
    ) as client:

        async def fetch(name, url):
            content = cached_response(output, name, url, previous_manifest) if reuse else None
            if content is not None:
                manifest.append(
                    {
                        "file": name,
                        "url": url,
                        "reused": True,
                        "status": 200,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
                return httpx.Response(200, content=content, request=httpx.Request("GET", url))
            async with gate:
                try:
                    response = await client.get(url)
                    (output / name).write_bytes(response.content)
                    manifest.append(
                        {
                            "file": name,
                            "url": url,
                            "status": response.status_code,
                            "at": datetime.now(UTC).isoformat(),
                            "sha256": hashlib.sha256(response.content).hexdigest(),
                        }
                    )
                    response.raise_for_status()
                    return response
                except Exception as error:
                    manifest.append({"file": name, "url": url, "error": str(error)})
                    return None

        catalog_response = await fetch("catalog.json", base_url + "/api/instruments")
        if catalog_response is None:
            raise RuntimeError("runtime catalog unavailable")
        catalog = catalog_response.json()
        codes = [item["provider_code"] for item in catalog]
        await fetch("ready.json", base_url + "/api/ready")
        await fetch("watchlist.json", base_url + "/api/watchlist")

        async def inspect_code(code):
            response = await fetch(f"quote-{code}.json", f"{base_url}/api/quotes/{code}/last")
            if response is None:
                quotes.append({"code": code, "result": "unavailable"})
            else:
                value = response.json()
                quotes.append(
                    {
                        "code": code,
                        **value,
                        "result": "captured",
                        "price_acceptance": "not_verified",
                    }
                )
            for period in PERIOD_DEFINITIONS:
                if skip_periods and not (output / f"bars-{code}-{period}.json").exists():
                    periods.append(
                        {
                            "code": code,
                            "period": period,
                            "count": 0,
                            "invalid": 0,
                            "result": "not_run",
                            "price_acceptance": "not_verified",
                        }
                    )
                    continue
                response = await fetch(
                    f"bars-{code}-{period}.json",
                    f"{base_url}/api/bars/{code}?period={period}&page_size={page_size}",
                )
                row = {
                    "code": code,
                    "period": period,
                    "count": 0,
                    "invalid": 0,
                    "result": "unavailable",
                    "price_acceptance": "not_verified",
                }
                if response is not None:
                    value = response.json()
                    row.update(inspect_bars(value.get("items", [])))
                periods.append(row)
            print("checked periods", code, flush=True)

        await asyncio.gather(*(inspect_code(code) for code in codes))
        save(output / "quotes.json", quotes)
        table(output / "periods.csv", sorted(periods, key=lambda x: (x["code"], x["period"])))

        main_url = (
            "https://ftapi.10jqka.com.cn/futgwapi/api/market/v1/contract/getMainContractDetailList"
        )
        if reference_file is not None:
            content = reference_file.read_bytes()
            response = httpx.Response(200, content=content)
            manifest.append(
                {
                    "file": str(reference_file.resolve()),
                    "reference": True,
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        else:
            response = await fetch("ths-main-contracts.json", main_url)
        reference_futures_status = "enumerated" if response is not None else "unavailable"
        futures = []
        if response is not None:
            for row in response.json()["data"]["result"]:
                exact = row["contractCode"].upper() in codes
                related = [
                    x["provider_code"]
                    for x in catalog
                    if x["instrument"]["venue"] == row["marketCode"]
                    and x["instrument"]["base"].upper() == row["variety"].upper()
                ]
                futures.append(
                    {
                        "exchange": row["marketCode"],
                        "variety": row["variety"],
                        "name": row["varietyName"],
                        "main_contract": row["contractCode"],
                        "exact_contract_supported": exact,
                        "other_local_series": ",".join(related),
                        "result": "requires_quote_audit" if exact else "not_integrated",
                    }
                )
        table(output / "futures-coverage.csv", futures)

        response = await fetch("options-api.json", base_url + "/api/expert/options/gold")
        options_summary = {"result": "unavailable"}
        if response is not None:
            options = response.json()
            source_responses = await asyncio.gather(
                *(
                    fetch(f"options-source-{i}.json", url)
                    for i, url in enumerate(options.get("source_urls", []))
                )
            )
            if len(source_responses) == 4 and all(source_responses):
                raw, _underlying, master, _daily = [x.json() for x in source_responses]
                raw_by_id = {x["contractname"]: x for x in raw["delaymarket"]}
                master_by_id = {x["INSTRUMENTID"]: x for x in master["OptionContractBaseInfo"]}
                api_by_id = {x["contract_id"]: x for x in options["contracts"]}
                comparisons = compare_option_contracts(
                    options["contracts"], raw["delaymarket"], master["OptionContractBaseInfo"]
                )
                table(output / "options-contract-checks.csv", comparisons)
                results_by_id = {x["contract"]: x["result"] for x in comparisons}
                coverage = [
                    {
                        "contract": key,
                        "commodity": row["COMMODITYID"],
                        "name": row["COMMODITYNAME"],
                        "expiry": row["EXPIREDATE"],
                        "result": results_by_id.get(key, "not_integrated"),
                    }
                    for key, row in master_by_id.items()
                ]
                table(output / "options-master-coverage.csv", coverage)
                options_summary = {
                    "api_count": len(api_by_id),
                    "raw_count": len(raw_by_id),
                    "results": dict(Counter(x["result"] for x in comparisons)),
                    "master_count": len(coverage),
                    "master_products": len(set(x["commodity"] for x in coverage)),
                    "coverage_results": dict(Counter(x["result"] for x in coverage)),
                    "trading_day": options["trading_day"],
                    "observed_at": options["observed_at"],
                    "delivery_mode": options["delivery_mode"],
                }

        settings = PostgresSettings.from_env()
        if settings:
            connection = await asyncpg.connect(settings.dsn)
            try:
                inventory = [
                    dict(x)
                    for x in await connection.fetch(
                        "SELECT instrument_symbol,realtime_source_id,interval_seconds,count(*) "
                        "AS count,min(open_time),max(open_time),count(*) FILTER(WHERE low>high "
                        "OR open<low OR open>high OR close<low OR close>high "
                        "OR low<=0 OR volume<0) AS invalid FROM realtime_bars GROUP BY 1,2,3"
                    )
                ]
                save(output / "database-inventory.json", inventory)
                mapper = TonghuashunFuturesSymbolMapper()
                for definition in INSTRUMENT_CATALOG:
                    if definition.source_ids != ("tonghuashun_futures",):
                        continue
                    stored = await connection.fetch(
                        "SELECT open_time,open,high,low,close,volume,state FROM realtime_bars "
                        "WHERE instrument_symbol=$1 AND realtime_source_id='tonghuashun_futures' "
                        "AND interval_seconds=60 ORDER BY open_time",
                        definition.instrument.symbol,
                    )
                    reference = {}
                    provider = mapper.to_provider_code(definition.instrument)
                    tz = mapper.line_time_zone(definition.instrument)
                    years = sorted({row["open_time"].astimezone(tz).year for row in stored})
                    for file in [*(f"{year}.js" for year in years), "last.js"]:
                        response = await fetch(
                            f"source-{definition.code}-{file}",
                            f"https://d.10jqka.com.cn/v6/line/{provider}/61/{file}",
                        )
                        if response is None or not response.text.strip():
                            continue
                        try:
                            for text in jsonp(response.text)["data"].split(";"):
                                parts = text.split(",")
                                time = datetime.strptime(parts[0], "%Y%m%d%H%M").replace(tzinfo=tz)
                                reference[time] = [number(x) for x in parts[1:6]]
                        except (ValueError, KeyError):
                            continue
                    mismatches, matched, missing = [], 0, 0
                    for row in stored:
                        source = reference.get(row["open_time"])
                        if source is None:
                            missing += 1
                            continue
                        local = [row[x] for x in ("open", "high", "low", "close", "volume")]
                        if source != local:
                            mismatches.append(
                                {
                                    "time": row["open_time"],
                                    "source": source,
                                    "local": local,
                                    "state": row["state"],
                                }
                            )
                        else:
                            matched += 1
                    save(output / f"minute-mismatches-{definition.code}.json", mismatches)
                    minute_checks.append(
                        {
                            "code": definition.code,
                            "stored": len(stored),
                            "source_rows": len(reference),
                            "matched": matched,
                            "mismatched": len(mismatches),
                            "no_reference": missing,
                        }
                    )
                    print("checked minute history", minute_checks[-1], flush=True)
            finally:
                await connection.close()
        table(output / "minute-source-checks.csv", minute_checks)
        save(output / "manifest.json", manifest)
        save(
            output / "summary.json",
            {
                "started_at": started,
                "finished_at": datetime.now(UTC),
                "catalog_count": len(codes),
                "quotes": dict(Counter(x["result"] for x in quotes)),
                "periods": dict(Counter(x["result"] for x in periods)),
                "reference_futures_count": len(futures),
                "reference_futures_status": reference_futures_status,
                "reference_file": str(reference_file.resolve()) if reference_file else None,
                "reference_futures_exact_supported": sum(
                    x["exact_contract_supported"] for x in futures
                ),
                "options": options_summary,
                "minute_source_checks": minute_checks,
                "limits": [
                    "A structural bar pass is not source price acceptance.",
                    "THS domestic main contracts and SHFE option master do not prove "
                    "full client/overseas coverage.",
                    "A reference item absent from the runtime is not an accepted instrument.",
                ],
            },
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--skip-periods", action="store_true")
    parser.add_argument("--page-size", type=int, default=500)
    parser.add_argument("--fixed-baseline", type=Path, help="Validate captured evidence offline without provider/database IO")
    parser.add_argument("--native-fixed", type=Path, help="Current native normalization artifact for the same captured bytes")
    parser.add_argument(
        "--reference",
        type=Path,
        help="Explicit THS main-contract reference JSON; otherwise fetch it live",
    )
    arguments = parser.parse_args()
    if arguments.fixed_baseline:
        if arguments.native_fixed is None:
            parser.error("--fixed-baseline requires --native-fixed")
        fixed_audit(arguments.output, arguments.fixed_baseline, arguments.native_fixed)
        raise SystemExit(0)
    load_project_environment(Path(__file__).resolve().parents[1])
    asyncio.run(
        audit(
            arguments.output,
            arguments.base_url,
            reuse=arguments.reuse,
            skip_periods=arguments.skip_periods,
            page_size=arguments.page_size,
            reference_file=arguments.reference,
        )
    )
