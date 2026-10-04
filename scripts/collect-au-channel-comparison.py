"""Capture exact AU2610 public channels for a fixed, independent comparison."""
import concurrent.futures
import datetime as dt
import hashlib
import json
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / ".mypowers/evidence/market/au2610-channel-comparison-2026-10-04-01"
BASE = "https://quota-h.10jqka.com.cn/fuyao/common_hq_aggr/quote/v1/"
NOW_MS = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
CODE = [{"market": "65", "codes": ["au2610"]}]
TASKS = [
    ("fuyao-snapshot.json", BASE + "multi_last_snapshot", {"code_list": CODE, "trade_class": "intraday", "data_fields": ["1", "6", "7", "8", "9", "10", "11", "13", "14", "15", "19", "55", "199112", "264648", "920456", "65558"], "lang": "zh-cn", "gpid": 0}),
    *[(f"fuyao-min-{minutes}.json", BASE + "single_kline", {"code_list": CODE, "trade_class": "intraday", "time_period": f"min_{minutes}", "trade_date": -1, "begin_time": -count, "end_time": NOW_MS, "adjust_type": "actual", "gpid": 0}) for minutes, count in [(1, 300), (5, 100)]],
    ("v6-time.js", "https://d.10jqka.com.cn/v6/time/qh_au2610/last.js", None),
    ("v6-daily.js", "https://d.10jqka.com.cn/v6/line/qh_au2610/01/last.js", None),
]

def fetch(task):
    name, url, body = task
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    request = urllib.request.Request(url, data=None if body is None else json.dumps(body, separators=(",", ":")).encode(), headers={"User-Agent": "Mozilla/5.0 TraceFang public protocol validation", "Content-Type": "application/json"})
    record = {"file": name, "url": url, "request": body, "requested_at": started, "authorization": "anonymous public request; no SDK license or token", "scope": "SHFE market 65 independently established by official SDK; exact existing AU2610 contract, distinct from current90 AU2612"}
    try:
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("response exceeds 32MiB limit")
            record.update(status=response.status, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            (DEST / name).write_bytes(data)
    except Exception as error:
        record["error"] = str(error)
    record["received_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    return record

if __name__ == "__main__":
    DEST.mkdir(parents=True, exist_ok=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        records = list(pool.map(fetch, TASKS))
    (DEST / "manifest.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"directory": str(DEST), "responses": [{"file": r["file"], "status": r.get("status"), "bytes": r.get("bytes"), "error": r.get("error")} for r in records]}))
