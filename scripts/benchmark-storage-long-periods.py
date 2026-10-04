#!/usr/bin/env python3
"""Read-only 300 complete UTC-day aggregation on existing benchmark data.

Run only after benchmark-storage.py completes. No load, schema or data writes.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import time
from decimal import Decimal
from pathlib import Path

spec=importlib.util.spec_from_file_location('storage_benchmark',Path(__file__).with_name('benchmark-storage.py'))
bench=importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)
DAY=86_400_000_000
BASE=bench.BASE
OUT=bench.OUT
COLUMNS=['bucket_us','open','high','low','close','known_volume_sum','known_volume_count','component_count','revision_sum','all_final']


def oracle(profile,lo,hi):
    groups={}
    with (OUT/'fixtures'/f'{profile}-bars.csv').open() as handle:
        for row in csv.reader(handle):
            if row[:2]!=['SYM000','source_a']:continue
            at=int(row[2])
            if not lo<=at<hi:continue
            bucket=at//DAY*DAY
            o,h,l,c=map(Decimal,row[3:7])
            volume=Decimal(row[7]) if row[7] else None
            if bucket not in groups:
                groups[bucket]=[bucket,o,h,l,c,None,0,0,0,1]
            g=groups[bucket]
            g[2]=max(g[2],h);g[3]=min(g[3],l);g[4]=c
            if volume is not None:
                g[5]=(g[5] or Decimal(0))+volume;g[6]+=1
            g[7]+=1;g[8]+=int(row[8]);g[9]&=int(row[10]=='final')
    rows=[groups[k] for k in sorted(groups)]
    assert len(rows)==300 and sum(r[7] for r in rows)==432000
    return rows


def sql_for(kind,profile,lo,hi,table=None):
    table=table or f'{profile}_bars'+(' FINAL' if kind=='clickhouse' else '')
    where=f"symbol='SYM000' AND source='source_a' AND time_us>={lo} AND time_us<{hi}"
    tail="sum(p_volume) AS known_volume_sum,count(p_volume) AS known_volume_count,count(*) AS component_count,sum(revision) AS revision_sum,min(CASE WHEN state='final' THEN 1 ELSE 0 END) AS all_final"
    if kind=='questdb':
        return f"SELECT cast(ts as long) AS bucket_us,first(p_open) AS open,max(p_high) AS high,min(p_low) AS low,last(p_close) AS close,{tail} FROM {table} WHERE symbol='SYM000' AND source='source_a' AND ts>=cast({lo} as timestamp) AND ts<cast({hi} as timestamp) SAMPLE BY 1d ALIGN TO CALENDAR TIME ZONE 'UTC' ORDER BY bucket_us"
    if kind=='clickhouse':
        return f"SELECT intDiv(time_us,{DAY})*{DAY} AS bucket_us,argMin(p_open,time_us) AS open,max(p_high) AS high,min(p_low) AS low,argMax(p_close,time_us) AS close,if(count(p_volume)=0,NULL,sum(p_volume)) AS known_volume_sum,count(p_volume) AS known_volume_count,count(*) AS component_count,sum(revision) AS revision_sum,min(if(state='final',1,0)) AS all_final FROM {table} WHERE {where} GROUP BY bucket_us ORDER BY bucket_us"
    if kind=='duckdb':
        return f"SELECT (time_us//{DAY})*{DAY} AS bucket_us,arg_min(p_open,time_us) AS open,max(p_high) AS high,min(p_low) AS low,arg_max(p_close,time_us) AS close,{tail} FROM {table} WHERE {where} GROUP BY bucket_us ORDER BY bucket_us"
    return f"SELECT (time_us/{DAY})*{DAY} AS bucket_us,(array_agg(p_open ORDER BY time_us))[1] AS open,max(p_high) AS high,min(p_low) AS low,(array_agg(p_close ORDER BY time_us DESC))[1] AS close,{tail} FROM {table} WHERE {where} GROUP BY bucket_us ORDER BY bucket_us"


def connect_existing(kind):
    directory=OUT/kind
    engine=bench.Engine(kind,directory)
    if kind=='duckdb':
        engine.connection=bench.duckdb.connect(str(directory/'database.duckdb'),read_only=True)
        engine.query('SET threads=4');engine.query("SET memory_limit='3GB'")
        return engine
    label=bench.cmd('docker','inspect','--format','{{index .Config.Labels "tracefang.purpose"}}',engine.container)
    assert label=='storage-benchmark'
    running=bench.cmd('docker','inspect','--format','{{.State.Running}}',engine.container)
    assert running=='false','Run after the initial benchmark stops all candidate services'
    bench.cmd('docker','start',engine.container)
    deadline=time.monotonic()+60
    while True:
        try:
            if kind=='postgres':
                engine.connection=bench.psycopg.connect('host=127.0.0.1 port=25432 dbname=benchmark user=benchmark password=isolated-benchmark',autocommit=True)
                engine.query('SET statement_timeout=30000')
            engine.query('SELECT 1');return engine
        except Exception:
            if time.monotonic()>deadline:raise
            time.sleep(.1)


def measure(engine,sql,expected,samples,arrow=False):
    actual=bench.normalize(engine.query(sql),0)
    assert bench.digest(actual)==bench.digest(expected),(actual[:2],expected[:2])
    for _ in range(3):engine.query(sql)
    vals=[];parts={}
    for _ in range(samples):
        start=time.perf_counter()
        if arrow:
            table,timing=engine.query_arrow(sql)
        else:
            engine.query(sql);timing=getattr(engine,'last_timing',{})
        vals.append((time.perf_counter()-start)*1000)
        for key,value in timing.items():parts.setdefault(key,[]).append(float(value))
    if arrow:
        materialized=[[row[name] for name in table.column_names] for row in table.to_pylist()]
        assert bench.digest(materialized)==bench.digest(expected)
    return {**bench.stats(vals),'timing_components':{key:bench.stats(v) for key,v in parts.items()},'result_sha256':bench.digest(actual),'passed':True}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidates',default='postgres,questdb,clickhouse,duckdb')
    p.add_argument('--samples',type=int,default=30)
    args=p.parse_args();assert 10<=args.samples<=100
    manifest=json.loads((OUT/'manifest.json').read_text())
    end_day=int(manifest['hot_symbol_minutes'])//1440
    lo,hi=BASE+(end_day-300)*DAY,BASE+end_day*DAY
    expected={profile:oracle(profile,lo,hi) for profile in ['scaled','wide']}
    report={'window':{'calendar':'UTC 300 complete days; excludes current partial day','lo_inclusive_us':lo,'hi_exclusive_us':hi,'minute_count':432000,'output_count':300},
            'columns':COLUMNS,'oracle':{k:{'sha256':bench.digest(v),'rows':[[bench.canonical(x) for x in r] for r in v]} for k,v in expected.items()},
            'scope':'read-only existing final facts; no ingest/schema changes; warm sequential local exploratory samples, non-exclusive host; trading-session calendar verified separately in Rust','candidates':{}}
    bench.dump(OUT/'long-periods.json',report)
    failed=False
    for kind in args.candidates.split(','):
        engine=None;result={'profiles':{},'host_load_before':os.getloadavg()}
        try:
            engine=connect_existing(kind)
            for profile in expected:
                sql=sql_for(kind,profile,lo,hi)
                stats=measure(engine,sql,expected[profile],args.samples)
                result['profiles'][profile]={'row_materialization':stats,'sql':sql}
                if kind=='duckdb':
                    result['profiles'][profile]['arrow']=measure(engine,sql,expected[profile],args.samples,True)
                    parquet=engine.directory/f'{profile}-bars.parquet'
                    parquet_sql=sql_for(kind,profile,lo,hi,f"read_parquet('{parquet}')")
                    result['profiles'][profile]['parquet_row']=measure(engine,parquet_sql,expected[profile],args.samples)
                    result['profiles'][profile]['parquet_arrow']=measure(engine,parquet_sql,expected[profile],args.samples,True)
                elif kind=='postgres':
                    result['profiles'][profile]['explain']=engine.query('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql)[0][0]
                print(f"{kind}/{profile}: 300 UTC days,432000 minutes,exact hash passed; p50/p95 {stats['p50_ms']:.3f}/{stats['p95_ms']:.3f}ms",flush=True)
                report['candidates'][kind]=result;bench.dump(OUT/'long-periods.json',report)
            result['passed']=True
        except Exception as exc:
            result['passed']=False;result['error']=repr(exc);failed=True
            import traceback;traceback.print_exc()
        finally:
            if engine:engine.close()
            report['candidates'][kind]=result;bench.dump(OUT/'long-periods.json',report)
    if failed:raise SystemExit(1)


if __name__=='__main__':main()
