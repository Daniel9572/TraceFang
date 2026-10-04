"""Read every advertised research series/period and available option month.

No credentials, trading actions, source changes or watchlist writes. Saved
responses are evidence of availability/structure, not price acceptance.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx


def reusable_record(file, path, body):
    if not file.exists():
        return None
    try:
        record = json.loads(file.read_text())
        if (
            record.get("status") == 200
            and record.get("path") == path
            and record.get("query") == body
            and "payload" in record
            and not (
                isinstance(record["payload"], dict)
                and ("detail" in record["payload"] or "error" in record["payload"])
            )
        ):
            return record
    except (ValueError, AttributeError):
        pass
    return None


def inspect_bars(items):
    if not items:
        return "empty"
    times = []
    try:
        for row in items:
            times.append(datetime.fromisoformat(row["open_time"]))
            o, h, low, c = (float(row[x]) for x in ("open", "high", "low", "close"))
            volume = row.get("volume")
            if (
                not all(math.isfinite(x) for x in (o, h, low, c))
                or not 0 < low <= min(o, c) <= max(o, c) <= h
                or (volume is not None and (not math.isfinite(float(volume)) or float(volume) < 0))
            ):
                return "invalid"
        return "structural_pass" if times == sorted(set(times)) else "invalid"
    except (ValueError, TypeError, KeyError):
        return "invalid"


def option_evidence(payload, symbol, month):
    fields = (
        "underlying",
        "expiry",
        "strike",
        "kind",
        "last",
        "previous_settlement",
        "volume",
        "open_interest",
        "observed_at",
    )
    rows = []
    for contract in payload.get("contracts", []):
        missing = [field for field in fields if contract.get(field) is None]
        rows.append(
            {
                "underlying": symbol,
                "month": month,
                "contract": contract.get("symbol"),
                "result": "insufficient_evidence" if missing else "not_verified",
                "missing_fields": missing,
                "price_acceptance": "not_verified",
            }
        )
    return rows


async def audit(output: Path, base_url: str, *, reference_file: Path, reuse=False) -> None:
    # Fail before any requests rather than silently omit the reference universe.
    reference_bytes = reference_file.read_bytes()
    reference = json.loads(reference_bytes)["data"]["result"]
    output.mkdir(parents=True, exist_ok=True)
    checks = []
    gate = asyncio.Semaphore(2)
    async with httpx.AsyncClient(base_url=base_url, timeout=45, trust_env=False) as client:

        async def request(name: str, path: str, body=None):
            file = output / f"{name}.json"
            record = reusable_record(file, path, body) if reuse else None
            reused = record is not None
            if record is None:
                async with gate:
                    start = time.monotonic()
                    try:
                        response = await client.request(
                            "POST" if body else "GET",
                            path,
                            json=body,
                        )
                        record = {"status": response.status_code, "payload": response.json()}
                    except Exception as error:
                        record = {"status": 0, "error": type(error).__name__}
                    record.update(
                        path=path,
                        query=body,
                        at=datetime.now(UTC).isoformat(),
                        milliseconds=round((time.monotonic() - start) * 1000),
                    )
                    file.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            checks.append(
                {
                    "file": file.name,
                    "status": record["status"],
                    "path": path,
                    "reused": reused,
                    "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                }
            )
            print(name, record["status"], flush=True)
            return record.get("payload") if record["status"] == 200 else None

        sources = await request("sources", "/api/research/sources")
        catalog = await request("catalog", "/api/research/catalog")
        underlyings = await request("underlyings", "/api/research/option-underlyings")
        if not sources or not catalog or not underlyings:
            raise RuntimeError("research inventory unavailable")
        configured = {row["id"]: row for row in sources if row["configured"]}
        queries = {}
        unavailable_credentials = [row for row in sources if not row["configured"]]

        def add_series(source, symbol, asset, periods):
            for period in periods:
                name = f"bars-{source}-{symbol}-{period}"
                queries[name] = {
                    "source": source,
                    "symbol": symbol,
                    "asset": asset,
                    "period": period,
                    "adjustment": "raw",
                    "limit": 300,
                }

        for row in catalog:
            spec = configured.get(row["source"])
            if spec:
                periods = spec.get("asset_periods", {}).get(row["asset"], spec["periods"])
                add_series(row["source"], row["symbol"], row["asset"], periods)
        # Reference all THS domestic main varieties, including those absent
        # from the research UI's small suggested catalog.
        reference_symbols = []
        for row in reference:
            symbol = row["contractCode"].upper()
            match = re.fullmatch(r"([A-Z]+)(\d{3})", symbol)
            if match:
                year = datetime.now(UTC).year
                contract_year = year // 10 * 10 + int(match[2][0])
                if contract_year < year:
                    contract_year += 10
                symbol = f"{match[1]}{contract_year % 100:02}{match[2][1:]}"
            reference_symbols.append({**row, "query_symbol": symbol})
            if "akshare" in configured:
                add_series("akshare", symbol, "future", ["1d", "1m"])

        directory = await request(
            "contracts-option-SHFE",
            "/api/research/contracts?source=akshare&asset=option&exchange=SHFE",
        )
        dependency_blocked = []
        option_checks, option_contract_checks = [], []
        if directory is not None:
            for exchange in ("SSE", "SZSE", "SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX"):
                for asset in ("option", "future"):
                    await request(
                        f"contracts-{asset}-{exchange}",
                        f"/api/research/contracts?source=akshare&asset={asset}&exchange={exchange}",
                    )
            for underlying in underlyings:
                symbol = underlying["symbol"]
                months = await request(f"months-{symbol}", f"/api/research/option-months/{symbol}")
                if months is not None:
                    for month in months["months"]:
                        chain = await request(
                            f"chain-{symbol}-{month['month']}",
                            f"/api/research/options/{symbol}?source=akshare&month={month['month']}",
                        )
                        contracts = chain.get("contracts", []) if chain else []
                        option_checks.append(
                            {
                                "symbol": symbol,
                                "month": month["month"],
                                "result": "unavailable"
                                if chain is None
                                else "available"
                                if contracts
                                else "empty",
                                "count": len(contracts),
                                "truncated": chain.get("truncated") if chain else None,
                                "price_acceptance": "not_verified",
                            }
                        )
                        if chain:
                            option_contract_checks.extend(
                                option_evidence(chain, symbol, month["month"])
                            )
                else:
                    option_checks.append({"symbol": symbol, "result": "months_unavailable"})
        else:
            dependency_blocked = [
                {
                    **row,
                    "result": "blocked_shared_contract_metadata",
                    "evidence": "contracts-option-SHFE.json",
                }
                for row in underlyings
            ]

        async def bars(name, query):
            payload = await request(name, "/api/research/bars", query)
            if payload is None:
                return {**query, "result": "unavailable", "price_acceptance": "not_verified"}
            items = payload.get("items", [])
            return {
                **query,
                "result": inspect_bars(items),
                "count": len(items),
                "price_acceptance": "not_verified",
                "cache_state": payload.get("cache_state"),
                "data_as_of": payload.get("data_as_of"),
            }

        # Keep the queue bounded so the audit cannot exhaust the serving process.
        bar_checks = []
        entries = list(queries.items())
        for index in range(0, len(entries), 2):
            bar_checks.extend(
                await asyncio.gather(*(bars(*row) for row in entries[index : index + 2]))
            )
        by_series = {
            (row["source"], row["symbol"], row["period"]): row["result"] for row in bar_checks
        }
        futures_coverage = [
            {
                "reference_contract": row["contractCode"],
                "query_symbol": row["query_symbol"],
                "exchange": row["marketCode"],
                "period": period,
                "result": by_series.get(("akshare", row["query_symbol"], period), "unconfigured"),
                "price_acceptance": "not_verified",
            }
            for row in reference_symbols
            for period in ("1d", "1m")
        ]
        result = {
            "finished_at": datetime.now(UTC).isoformat(),
            "checks": checks,
            "bar_checks": bar_checks,
            "counts": dict(Counter(row["result"] for row in bar_checks)),
            "unconfigured_sources": unavailable_credentials,
            "reference": {
                "file": str(reference_file.resolve()),
                "count": len(reference),
                "sha256": hashlib.sha256(reference_bytes).hexdigest(),
            },
            "futures_coverage": futures_coverage,
            "option_checks": option_checks,
            "option_contract_checks": option_contract_checks,
            "option_contract_counts": dict(
                Counter(row["result"] for row in option_contract_checks)
            ),
            "option_dependency_failures": dependency_blocked,
            "limits": "Availability and OHLC structure only; "
            "same-source/native-client price comparisons are separate.",
        }
        (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--reference", type=Path, required=True, help="Explicit THS main-contract reference JSON"
    )
    parser.add_argument(
        "--reuse", action="store_true", help="Reuse only successful matching responses"
    )
    args = parser.parse_args()
    asyncio.run(audit(args.output, args.base_url, reference_file=args.reference, reuse=args.reuse))
