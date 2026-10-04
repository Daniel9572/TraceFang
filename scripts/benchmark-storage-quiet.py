#!/usr/bin/env python3
"""Coordinated quiet-window, read-only recheck of three verified query paths.

Run after other agents' measurements/builds end. Reuses retained DBs and hashes;
does not reload, write, fault-inject, or claim OS/background activity is absent.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import time
from pathlib import Path

spec=importlib.util.spec_from_file_location('long_period_benchmark',Path(__file__).with_name('benchmark-storage-long-periods.py'))
long=importlib.util.module_from_spec(spec);spec.loader.exec_module(long)
bench=long.bench
OUT=bench.OUT


def host_snapshot():
    rows=bench.cmd('ps','-axo','comm').splitlines()
    names=['cargo','rustc','node','fileproviderd','cloudd','bird']
    return {'load_average':os.getloadavg(),'process_counts':{name:sum(Path(x.strip()).name==name for x in rows) for name in names},
            'scope':'development builds paused by agent coordination; OS/iCloud/app activity uncontrolled; node count is not a count of build jobs'}


def measured(engine,sql,expected_hash,count,samples,arrow=False):
    rows=bench.normalize(engine.query(sql),0)
    assert len(rows)==count and bench.digest(rows)==expected_hash,(len(rows),bench.digest(rows),expected_hash)
    for _ in range(3):engine.query(sql)
    values=[];parts={}
    for _ in range(samples):
        start=time.perf_counter()
        if arrow:table,timing=engine.query_arrow(sql)
        else:engine.query(sql);timing=getattr(engine,'last_timing',{})
        values.append((time.perf_counter()-start)*1000)
        for key,value in timing.items():parts.setdefault(key,[]).append(float(value))
    if arrow:
        actual=[[row[name] for name in table.column_names] for row in table.to_pylist()]
        assert bench.digest(actual)==expected_hash
    else:
        assert bench.digest(bench.normalize(engine.query(sql),0))==expected_hash
    return {**bench.stats(values),'timing_components':{key:bench.stats(v) for key,v in parts.items()},'result_sha256':expected_hash,'rows':count,'exact_roundtrip_passed':True}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--samples',type=int,default=30)
    args=p.parse_args();assert args.samples==30
    manifest=json.loads((OUT/'manifest.json').read_text())
    initial={kind:json.loads((OUT/f'{kind}.json').read_text()) for kind in ['postgres','questdb','clickhouse','duckdb']}
    oracle=json.loads((OUT/'long-period-oracle.json').read_text())
    lo,hi=oracle['window']['lo_inclusive_us'],oracle['window']['hi_exclusive_us']
    # Every original candidate hash was previously checked against fixture oracle.
    # Verify the retained results still describe this precise fixture manifest.
    for kind,result in initial.items():
        assert result['passed']
        for profile in ['scaled','wide']:
            assert result['profiles'][profile]['correctness']['bars_sha256']==manifest['profiles'][profile]['final_bars_sha256']
    report={'window':'coordinated development-build pause; warm sequential reads, n30; uncontrolled OS/background activity recorded',
            'dataset_manifest_created_at':manifest['created_at'],'long_period_window':oracle['window'],'host_before':host_snapshot(),'candidates':{}}
    failed=False
    for kind in initial:
        engine=None;result={'host_before':host_snapshot(),'initial_config':initial[kind]['config'],'profiles':{}}
        try:
            engine=long.connect_existing(kind)
            for profile in ['scaled','wide']:
                queries=bench.workloads(engine,profile,manifest['hot_symbol_minutes'],0)
                specs={name:(queries[name],initial[kind]['profiles'][profile]['query_correctness'][name+':0']['sha256'],initial[kind]['profiles'][profile]['query_correctness'][name+':0']['rows']) for name in ['exclusive_page_300','range_600']}
                specs['complete_utc_days_300']=(long.sql_for(kind,profile,lo,hi),oracle['oracle'][profile]['sha256'],300)
                result['profiles'][profile]={}
                for name,(sql,sha,count) in specs.items():
                    measured_row=measured(engine,sql,sha,count,args.samples)
                    entry={'sql':sql,'row_materialization':measured_row}
                    if kind=='duckdb':
                        entry['arrow']=measured(engine,sql,sha,count,args.samples,True)
                        path=engine.directory/f'{profile}-bars.parquet'
                        parquet_sql=sql.replace(f'{profile}_bars',f"read_parquet('{path}')")
                        entry['parquet_sql']=parquet_sql
                        entry['parquet_row']=measured(engine,parquet_sql,sha,count,args.samples)
                        entry['parquet_arrow']=measured(engine,parquet_sql,sha,count,args.samples,True)
                    elif kind=='postgres':entry['explain']=engine.query('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql)[0][0]
                    result['profiles'][profile][name]=entry
                    print(f"quiet {kind}/{profile}/{name}: hash passed, p50/p95 {measured_row['p50_ms']:.3f}/{measured_row['p95_ms']:.3f}ms",flush=True)
                    report['candidates'][kind]=result;bench.dump(OUT/'quiet-read.json',report)
            result['passed']=True
        except Exception as exc:
            result['passed']=False;result['error']=repr(exc);failed=True
            import traceback;traceback.print_exc()
        finally:
            if engine:engine.close()
            result['host_after']=host_snapshot();report['candidates'][kind]=result
            bench.dump(OUT/'quiet-read.json',report)
    report['host_after']=host_snapshot();report['passed']=not failed;bench.dump(OUT/'quiet-read.json',report)
    if failed:raise SystemExit(1)


if __name__=='__main__':main()
