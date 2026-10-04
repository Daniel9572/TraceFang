"""Capture exact public source responses for fixed/current market verification.

This writes only a new evidence directory. No installed service/database is changed.
"""
import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, UTC
from pathlib import Path
import httpx

MAIN = "https://ftapi.10jqka.com.cn/futgwapi/api/market/v1/contract/getMainContractDetailList"
SHFE = "https://www.shfe.com.cn"

async def collect(root, baseline):
    root.mkdir(parents=True, exist_ok=False)
    records = []
    gate = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"Referer":"https://q.10jqka.com.cn/"}) as client:
        async def fetch(name, url, scope):
            started = time.monotonic()
            async with gate:
                record = {"file":name,"url":url,"scope":scope,"requested_at":datetime.now(UTC).isoformat()}
                try:
                    async with client.stream("GET",url) as response:
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body)+len(chunk)>32*1024*1024:
                                raise ValueError("response exceeds decoded 32MiB limit")
                            body.extend(chunk)
                        record.update(status=response.status_code,received_at=datetime.now(UTC).isoformat(),
                                      elapsed_ms=round((time.monotonic()-started)*1000,3),
                                      sha256=hashlib.sha256(body).hexdigest(),bytes=len(body),content_type=response.headers.get("content-type"))
                        (root/name).write_bytes(body)
                except Exception as error:
                    record.update(error=str(error),received_at=datetime.now(UTC).isoformat())
                records.append(record)
                (root/"manifest.json").write_text(json.dumps(records,ensure_ascii=False,indent=2))
                print(json.dumps({"file":name,"status":record.get("status"),"bytes":record.get("bytes"),"error":record.get("error")},ensure_ascii=False),flush=True)
                if record.get("status")==200:
                    return (root/name).read_bytes()
                return None
        fixed=json.loads((baseline/"ths-main-contracts.json").read_text())["data"]["result"]
        current_bytes=await fetch("current-futures.json",MAIN,{"kind":"current_main_contract_reference"})
        current=json.loads(current_bytes)["data"]["result"] if current_bytes else []
        identities={(r["marketCode"],r["contractCode"].upper()):r for r in fixed}
        identities.update({(r["marketCode"],r["contractCode"].upper()):r for r in current})
        (root/"futures-scopes.json").write_text(json.dumps({"fixed":fixed,"current":current},ensure_ascii=False,indent=2))
        products=sorted({r["COMMODITYID"] for r in json.loads((baseline/"options-source-2.json").read_text())["OptionContractBaseInfo"]})
        day_bytes=await fetch("trading-day.json",SHFE+"/data/config/currentTradingday.dat",{"kind":"exchange_trading_day"})
        if day_bytes:
            day=json.loads(day_bytes)
            await fetch("master.json",SHFE+f'/data/busiparamdata/option/ContractBaseInfo{day["currentTradingday"]}.dat',{"kind":"master"})
            await fetch("daily.json",SHFE+f'/data/tradedata/option/dailydata/kx{day["lastTradingday"]}.dat',{"kind":"daily_reference"})
        tasks=[]
        for (exchange,code),row in identities.items():
            scope={"kind":"exact_future","exchange":exchange,"contract":code,"reference":row}
            for kind,suffix in [("time",f"/v6/time/qh_{code.lower()}/last.js"),("minute",f"/v6/line/qh_{code.lower()}/61/last.js")]:
                tasks.append(fetch(f"ths-{code}-{kind}.raw","https://d.10jqka.com.cn"+suffix,scope))
        for product in products:
            for kind,path in [("option",f"/data/tradedata/option/delaymarket/delaymarket_{product}Q.dat"),
                              ("underlying",f"/data/tradedata/future/delaymarket/delaymarket_{product}.dat")]:
                tasks.append(fetch(f"shfe-{product}-{kind}.json",SHFE+path,{"kind":kind,"product":product}))
        await asyncio.gather(*tasks)
        (root/"capture-summary.json").write_text(json.dumps({"captured_at":datetime.now(UTC).isoformat(),"fixed_reference_count":len(fixed),
            "current_reference_count":len(current),"exact_identity_count":len(identities),"option_products":products,
            "concurrency":4,"single_body_limit":32*1024*1024,"record_count":len(records),
            "status_counts":{str(status):sum(r.get("status")==status for r in records)for status in sorted({r.get("status",0)for r in records})},
            "errors":sum("error" in r for r in records)},indent=2))
if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("output",type=Path);parser.add_argument("--baseline",type=Path,required=True)
    args=parser.parse_args();asyncio.run(collect(args.output,args.baseline))
