#!/usr/bin/env python3
"""Isolated, deterministic TraceFang storage comparison; never touches production.

Run with validation/bench-env (psycopg[binary] 3.3.2, duckdb 1.5.6, requests).
The timed API is end-to-end from this client through result decoding. Each
candidate runs alone, with 4 CPUs/3 GiB for Docker, or 4 DuckDB threads/3 GiB.
This is a local workload comparison, not a universal database ranking.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import socket
import statistics
import subprocess
import time
import threading
import sys
from decimal import Decimal, getcontext
from pathlib import Path

import duckdb
import psycopg
import requests
import pyarrow

getcontext().prec = 90
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".mypowers/work/rust-migration/validation/storage"
BASE = 1_759_449_600_000_000
BAR_COLS = ["symbol", "source", "time_us", "p_open", "p_high", "p_low", "p_close",
            "p_volume", "revision", "received_seq", "state", "finalized_us"]
TICK_COLS = ["symbol", "source", "time_us", "event_id", "price", "volume"]
SOURCES = ["source_a", "source_b"]


def cmd(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, **kwargs).stdout.strip()


def canonical(value):
    if value is None:
        return None
    if isinstance(value, (Decimal, int)):
        return format(Decimal(value), "f").rstrip("0").rstrip(".") if Decimal(value) % 1 else str(int(value))
    if isinstance(value, float):
        raise AssertionError("floating point entered exact comparison")
    return str(value)


def digest(rows):
    h = hashlib.sha256()
    for row in rows:
        h.update((json.dumps([canonical(x) for x in row], separators=(",", ":")) + "\n").encode())
    return h.hexdigest()


def stats(samples):
    samples = sorted(samples)
    return {"n": len(samples), "p50_ms": statistics.median(samples),
            "p95_ms": samples[max(0, int(len(samples) * .95 + .999) - 1)],
            "min_ms": min(samples), "max_ms": max(samples), "samples_ms": samples}


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str) + "\n")


def value(profile, s, i, offset=0):
    if profile == "scaled":
        return 10_000_000 + s * 10_000 + (i * 37 % 1500) + offset
    return Decimal("1234567890123456789.123456789012345678") + Decimal(s * 1000 + (i * 37 % 1500) + offset) / Decimal(10**18)


def dataset(profile, symbols, minutes, hot_minutes=0):
    bars, ticks, revisions = [], [], []
    for i in range(minutes):
        for s in range(symbols):
            symbol, source = f"SYM{s:03}", SOURCES[s % 2]
            at = BASE + i * 60_000_000
            o = value(profile, s, i)
            unit = 1 if profile == "scaled" else Decimal(".000000000000000001")
            vol = None if i % 9 == 0 else (i % 101 if profile == "scaled" else Decimal(i % 101) / Decimal(10**18))
            row = [symbol, source, at, o, o + 50 * unit, o - 50 * unit, o + 7 * unit,
                   vol, 1, i * symbols + s + 1, "provisional_quote", None]
            bars.append(row)
            # Revision 3 arrives before revision 2: high must retract, null must
            # replace previous volume, and stale state must not undo finality.
            if i % 13 == 0:
                final = row.copy()
                final[4:8] = [o + 20 * unit, o - 20 * unit, o - 4 * unit, None]
                final[8:] = [3, minutes * symbols + i * symbols + s + 1, "final", at + 60_000_000]
                stale = row.copy()
                stale[8:11] = [2, 2 * minutes * symbols + i * symbols + s + 1, "provisional_authoritative"]
                revisions.extend([final, stale])
            for k in range(2):
                eid = (i * symbols + s) * 2 + k + 1
                # Same timestamp distinct identity, plus timestamp out of order.
                observed = at + (0 if i % 11 == 0 else k * 2_000_000)
                if i % 17 == 0 and k == 1:
                    observed -= 1_000_000
                ticks.append([symbol, source, observed, eid, value(profile, s, i, k * 5), vol])
        # Same instrument/timestamp, different source with distinct values. A
        # missing source predicate now fails, and this series has all-NULL volume.
        other = bars[-symbols].copy()
        other[1] = 'source_b'
        other[3:7] = [v + 9999 * unit for v in other[3:7]]
        other[7] = None
        bars.append(other)
        for k in range(2):
            ticks.append(['SYM000','source_b',BASE+i*60_000_000,minutes*symbols*2+i*2+k+1,value(profile,0,i,9999+k),None])
    for i in range(minutes,hot_minutes):
        o=value(profile,0,i)
        unit=1 if profile=='scaled' else Decimal('.000000000000000001')
        at=BASE+i*60_000_000
        vol=None if i%9==0 else (i%101 if profile=='scaled' else Decimal(i%101)/Decimal(10**18))
        row=['SYM000','source_a',at,o,o+50*unit,o-50*unit,o+7*unit,vol,1,i+3*minutes*symbols,'provisional_quote',None]
        bars.append(row)
        ticks.append(['SYM000','source_a',at,minutes*(symbols+1)*2+i+1,o,vol])
        if i%13==0:
            final=row.copy();final[4:8]=[o+20*unit,o-20*unit,o-4*unit,None]
            final[8:]=[3,4*minutes*symbols+i,'final',at+60_000_000]
            stale=row.copy();stale[8:11]=[2,5*minutes*symbols+i,'provisional_authoritative']
            revisions.extend([final,stale])
    final_map = {(r[0], r[1], r[2]): r for r in bars}
    rejected = 0
    for r in revisions:
        key = tuple(r[:3])
        if tuple(r[8:10]) > tuple(final_map[key][8:10]):
            final_map[key] = r
        else:
            rejected += 1
    final_rows = sorted(final_map.values(), key=lambda r: tuple(r[:3]))
    return bars, ticks, revisions, final_rows, rejected


def csv_text(rows):
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    return buf.getvalue()


class Engine:
    def __init__(self, kind, directory):
        self.kind, self.directory = kind, directory
        self.session = requests.Session()
        self.container = f"tracefang-storage-bench-{kind}-20261003"
        self.connection = None
        self.accepted = {}
        self.config = {}

    def start(self):
        if self.kind == "duckdb":
            self.connection = duckdb.connect(str(self.directory / "database.duckdb"))
            self.query("SET threads=4")
            self.query("SET memory_limit='3GB'")
            self.config = {"version": duckdb.__version__, "threads": 4, "memory_limit": "3GB",
                           "durability": "native on-disk transaction commit; WAL enabled; OS fsync semantics"}
            return
        ports = {"postgres": [25432], "questdb": [18812, 19000], "clickhouse": [18123]}[self.kind]
        for port in ports:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"isolated benchmark port {port} is occupied")
        existing = cmd("docker", "ps", "-a", "--filter", f"name=^{self.container}$", "--format", "{{.Names}}")
        if existing:
            label = cmd("docker", "inspect", "--format", '{{index .Config.Labels "tracefang.purpose"}}', self.container)
            if label != "storage-benchmark":
                raise RuntimeError("refusing to remove an unowned container")
            cmd("docker", "rm", "-f", self.container)
        args = ["docker", "run", "-d", "--name", self.container, "--label", "tracefang.purpose=storage-benchmark",
                "--cpus=4", "--memory=3g"]
        if self.kind == "postgres":
            args += ["-p", "127.0.0.1:25432:5432", "-e", "POSTGRES_USER=benchmark", "-e",
                     "POSTGRES_PASSWORD=isolated-benchmark", "-e", "POSTGRES_DB=benchmark",
                     "postgres:17-alpine", "-c", "max_parallel_workers_per_gather=3", "-c", "shared_buffers=512MB"]
        elif self.kind == "questdb":
            args += ["-p", "127.0.0.1:18812:9000", "-p", "127.0.0.1:19000:8812", "-e",
                     "QDB_CAIRO_COMMIT_MODE=sync", "-e", "QDB_SHARED_WORKER_COUNT=4", "questdb/questdb:10.0.1"]
        else:
            config = self.directory / "limits.xml"
            config.write_text("<clickhouse><max_server_memory_usage>2500000000</max_server_memory_usage>"
                              "<profiles><default><max_threads>4</max_threads><max_memory_usage>2500000000</max_memory_usage>"
                              "</default></profiles><logger><level>warning</level></logger></clickhouse>")
            args += ["-p", "127.0.0.1:18123:8123", "-e", "CLICKHOUSE_SKIP_USER_SETUP=1", "--ulimit", "nofile=262144:262144",
                     "-v", f"{config}:/etc/clickhouse-server/config.d/benchmark.xml:ro", "clickhouse/clickhouse-server:26.8.15.10"]
        cmd(*args)
        deadline = time.monotonic() + 60
        while True:
            try:
                if self.kind == "postgres":
                    self.connection = psycopg.connect("host=127.0.0.1 port=25432 dbname=benchmark user=benchmark password=isolated-benchmark", autocommit=True)
                    self.query("SET statement_timeout=30000")
                self.query("SELECT 1")
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(.25)
        if self.kind == "postgres":
            self.config = {"version": self.query("SELECT version()")[0][0], "settings": self.query(
                "SELECT name,setting FROM pg_settings WHERE name IN ('fsync','full_page_writes','synchronous_commit','shared_buffers','max_parallel_workers_per_gather') ORDER BY name")}
        elif self.kind == "questdb":
            self.config = {"version": self.query("SELECT build()")[0][0], "commit_mode_env": "sync",
                           "worker_count_env": 4, "configuration": self.query("(SHOW PARAMETERS) WHERE property_path = 'cairo.commit.mode'")}
        else:
            self.config = {"version": self.query("SELECT version()")[0][0], "max_threads": 4,
                           "fsync_after_insert": True, "fsync_part_directory": True, "async_insert": False}
            self.config['actual_session_settings']=self.query("SELECT getSetting('max_threads'),getSetting('max_memory_usage')")
        self.config["docker_limits"] = {"cpu": 4, "memory_bytes": 3 * 1024**3}

    def query(self, sql):
        start=time.perf_counter()
        if self.kind in {"postgres", "duckdb"}:
            cur = self.connection.execute(sql)
            executed=time.perf_counter()
            if self.kind == "postgres" and cur.description is None:
                return []
            rows=[list(row) for row in cur.fetchall()]
            self.last_timing={'execute_ms':(executed-start)*1000,'decode_ms':(time.perf_counter()-executed)*1000}
            return rows
        if self.kind == "questdb":
            response = self.session.get("http://127.0.0.1:18812/exec", params={"query": sql, "count": "true", "limit": "0,2000000"}, timeout=30)
            obj = json.loads(response.text, parse_float=Decimal)
            if response.status_code != 200 or "error" in obj:
                raise RuntimeError(f"QuestDB: {obj}")
            return obj.get("dataset", [])
        response = self.session.post("http://127.0.0.1:18123", params={"output_format_json_quote_decimals": 1},
                                     data=(sql.rstrip(";") + " FORMAT JSONCompact").encode(), timeout=30)
        response.raise_for_status()
        executed=time.perf_counter()
        obj=json.loads(response.text,parse_float=Decimal)
        self.last_timing={'server_ms':obj.get('statistics',{}).get('elapsed',0)*1000,'execute_ms':(executed-start)*1000,'decode_ms':(time.perf_counter()-executed)*1000}
        return obj.get("data", [])

    def query_arrow(self,sql):
        start=time.perf_counter();cur=self.connection.execute(sql);executed=time.perf_counter()
        table=cur.to_arrow_table()
        return table,{'execute_ms':(executed-start)*1000,'arrow_fetch_ms':(time.perf_counter()-executed)*1000}

    def execute(self, sql):
        if self.kind != "clickhouse":
            return self.query(sql)
        response = self.session.post("http://127.0.0.1:18123", data=sql.encode(), timeout=60)
        response.raise_for_status()
        return []

    def schema(self, profile):
        number = "BIGINT" if profile == "scaled" else "DECIMAL(38,18)"
        if self.kind == "questdb":
            number = "LONG" if profile == "scaled" else "DECIMAL(38,18)"
            common = f"symbol SYMBOL INDEX, source SYMBOL INDEX, time_us LONG, p_open {number}, p_high {number}, p_low {number}, p_close {number}, p_volume {number}, revision INT, received_seq LONG, state SYMBOL, finalized_us LONG, ts TIMESTAMP"
            for table in ["bars", "states"]:
                self.execute(f"CREATE TABLE {profile}_{table} ({common}) TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts,symbol,source)")
            self.execute(f"CREATE TABLE {profile}_ticks (symbol SYMBOL INDEX, source SYMBOL INDEX, time_us LONG, event_id LONG, price {number}, volume {number}, ts TIMESTAMP) TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts,symbol,source,event_id)")
        elif self.kind == "clickhouse":
            number = "Int64" if profile == "scaled" else "Decimal(38,18)"
            common = f"symbol LowCardinality(String), source LowCardinality(String), time_us Int64, p_open {number}, p_high {number}, p_low {number}, p_close {number}, p_volume Nullable({number}), revision UInt32, received_seq UInt64, state LowCardinality(String), finalized_us Nullable(Int64), version UInt64"
            for table in ["bars", "states"]:
                self.execute(f"CREATE TABLE {profile}_{table} ({common}) ENGINE=ReplacingMergeTree(version) ORDER BY (symbol,source,time_us) SETTINGS fsync_after_insert=1,fsync_part_directory=1")
            self.execute(f"CREATE TABLE {profile}_ticks (symbol LowCardinality(String), source LowCardinality(String), time_us Int64, event_id Int64, price {number}, volume Nullable({number})) ENGINE=ReplacingMergeTree ORDER BY (symbol,source,event_id) SETTINGS fsync_after_insert=1,fsync_part_directory=1")
        else:
            common = f"symbol VARCHAR, source VARCHAR, time_us BIGINT, p_open {number} NOT NULL, p_high {number} NOT NULL, p_low {number} NOT NULL, p_close {number} NOT NULL, p_volume {number}, revision INTEGER, received_seq BIGINT, state VARCHAR, finalized_us BIGINT"
            for table in ["bars", "states"]:
                self.execute(f"CREATE TABLE {profile}_{table} ({common}, PRIMARY KEY(symbol,source,time_us))")
            self.execute(f"CREATE TABLE {profile}_ticks (symbol VARCHAR, source VARCHAR, time_us BIGINT, event_id BIGINT PRIMARY KEY, price {number}, volume {number})")
            self.execute(f"CREATE INDEX {profile}_tick_replay ON {profile}_ticks(symbol,source,event_id)")
            self.execute(f"CREATE INDEX {profile}_tick_time ON {profile}_ticks(symbol,source,time_us,event_id)")

    def insert(self, table, rows, columns, revision_gate=False):
        rejected = 0
        start = time.perf_counter()
        if self.kind == "questdb" and revision_gate:
            accepted = []
            current = self.accepted.setdefault(table, {})
            for row in rows:
                key = tuple(row[:3])
                if key not in current or tuple(row[8:10]) > current[key]:
                    current[key] = tuple(row[8:10])
                    accepted.append(row)
                else:
                    rejected += 1
            rows = accepted
        if not rows:
            return (time.perf_counter() - start) * 1000, 0.0, rejected
        if self.kind == "postgres":
            with self.connection.transaction():
                target = table
                if revision_gate or columns == TICK_COLS:
                    target = "stage"
                    self.execute(f"CREATE TEMP TABLE stage (LIKE {table} INCLUDING DEFAULTS) ON COMMIT DROP")
                with self.connection.cursor().copy(f"COPY {target} ({','.join(columns)}) FROM STDIN") as copy:
                    for row in rows:
                        copy.write_row(row)
                if revision_gate:
                    updates = ','.join(f'{c}=EXCLUDED.{c}' for c in columns[3:])
                    self.execute(f"INSERT INTO {table} SELECT DISTINCT ON(symbol,source,time_us) * FROM stage ORDER BY symbol,source,time_us,revision DESC,received_seq DESC ON CONFLICT(symbol,source,time_us) DO UPDATE SET {updates} WHERE (EXCLUDED.revision,EXCLUDED.received_seq)>({table}.revision,{table}.received_seq)")
                elif columns == TICK_COLS:
                    self.execute(f"INSERT INTO {table} SELECT DISTINCT ON(event_id) * FROM stage ORDER BY event_id ON CONFLICT(event_id) DO NOTHING")
        elif self.kind == "duckdb":
            # Arrow carries Decimal128 and Int64 directly. A CSV roundtrip or
            # Python float conversion would handicap the native Rust candidate.
            table_arrow=pyarrow.Table.from_arrays([pyarrow.array([r[i] for r in rows]) for i in range(len(columns))],names=columns)
            self.connection.register('input_batch',table_arrow)
            self.execute("BEGIN")
            try:
                if revision_gate or columns == TICK_COLS:
                    self.execute(f"CREATE TEMP TABLE stage AS SELECT * FROM {table} LIMIT 0")
                    self.execute("INSERT INTO stage SELECT * FROM input_batch")
                    if revision_gate:
                        updates = ','.join(f'{c}=EXCLUDED.{c}' for c in columns[3:])
                        self.execute(f"INSERT INTO {table} SELECT DISTINCT ON(symbol,source,time_us) * FROM stage ORDER BY symbol,source,time_us,revision DESC,received_seq DESC ON CONFLICT(symbol,source,time_us) DO UPDATE SET {updates} WHERE (EXCLUDED.revision,EXCLUDED.received_seq)>({table}.revision,{table}.received_seq)")
                    else:
                        self.execute(f"INSERT INTO {table} SELECT DISTINCT ON(event_id) * FROM stage ORDER BY event_id ON CONFLICT(event_id) DO NOTHING")
                    self.execute("DROP TABLE stage")
                else:
                    self.execute(f"INSERT INTO {table} SELECT * FROM input_batch")
                self.execute("COMMIT")
            except Exception:
                self.execute("ROLLBACK")
                raise
            finally:
                self.connection.unregister('input_batch')
        elif self.kind == "questdb":
            lines = []
            for row in rows:
                fields = []
                for col, val in zip(columns[2:], row[2:]):
                    if val is None:
                        continue
                    fields.append(f'{col}="{val}"' if isinstance(val, str) else f"{col}={val}{'d' if isinstance(val,Decimal) else 'i'}")
                lines.append(f"{table},symbol={row[0]},source={row[1]} {','.join(fields)} {row[2]*1000}\n")
            response = self.session.post("http://127.0.0.1:18812/write", data=''.join(lines).encode(), timeout=60)
            if response.status_code not in (200, 204):
                raise RuntimeError(response.text)
        else:
            payload_rows = [row + [row[8] * 1_000_000_000 + row[9]] for row in rows] if columns == BAR_COLS else rows
            response = self.session.post("http://127.0.0.1:18123", params={"query": f"INSERT INTO {table} FORMAT CSV", "async_insert": 0},
                                         data=csv_text(payload_rows).encode(), timeout=60)
            response.raise_for_status()
        ack = (time.perf_counter() - start) * 1000
        visible_start = time.perf_counter()
        if self.kind == "questdb":
            while True:
                status = self.query(f"SELECT writerTxn,sequencerTxn,suspended FROM wal_tables() WHERE name='{table}'")
                if status and not status[0][2] and status[0][0] == status[0][1]:
                    break
                if time.perf_counter() - visible_start > 30:
                    raise RuntimeError(f"WAL visibility timeout: {status}")
                time.sleep(.001)
        return ack, ack + (time.perf_counter() - visible_start) * 1000, rejected

    def fact(self, profile, table="bars"):
        return f"{profile}_{table}" + (" FINAL" if self.kind == "clickhouse" else "")

    def precision_probe(self):
        columns = "id INTEGER, mantissa DECIMAL(38,0), scale INTEGER, timestamp_ns BIGINT"
        if self.kind == "questdb":
            columns = "id INT, mantissa DECIMAL(38,0), scale INT, timestamp_ns LONG"
        elif self.kind == "clickhouse":
            columns = "id Int32, mantissa Decimal(38,0), scale Int32, timestamp_ns Int64"
        self.execute(f"CREATE TABLE precision_probe ({columns})" + (" ENGINE=MergeTree ORDER BY id SETTINGS fsync_after_insert=1,fsync_part_directory=1" if self.kind == "clickhouse" else ""))
        expected = [[1, Decimal(1), 28, 1759449600123456789],
                    [2, Decimal(79228162514264337593543950335), 0, 9007199254740993],
                    [3, Decimal(-79228162514264337593543950335), 28, 1759449600123456790],
                    [4, Decimal(99999999999999999999123456789012345678), 18, 1759449600123456791]]
        if self.kind == "questdb":
            for row in expected:
                self.execute(f"INSERT INTO precision_probe VALUES({row[0]},{row[1]}m,{row[2]},{row[3]})")
        elif self.kind == "clickhouse":
            self.execute("INSERT INTO precision_probe VALUES " + ",".join(f"({r[0]},'{r[1]}',{r[2]},{r[3]})" for r in expected))
        else:
            for row in expected:
                self.connection.execute("INSERT INTO precision_probe VALUES (?,?,?,?)" if self.kind == "duckdb" else "INSERT INTO precision_probe VALUES (%s,%s,%s,%s)", row)
        actual = self.query("SELECT id,mantissa,scale,timestamp_ns FROM precision_probe ORDER BY id")
        # CH quotes Int64/Decimal by default. Numeric text normalizes without float.
        actual = [[int(r[0]), Decimal(r[1]), int(r[2]), int(r[3])] for r in actual]
        assert digest(actual) == digest(expected), (actual, expected)
        return {"passed": True, "encoding": "mantissa DECIMAL(38,0), scale INTEGER, timestamp_ns INT64",
                "sha256": digest(actual), "rows": [[canonical(x) for x in r] for r in actual],
                "limit": "roundtrip only; scale-aware arithmetic remains the exact application kernel"}

    def close(self):
        if self.connection:
            self.connection.close()
        self.session.close()
        if self.kind != "duckdb":
            cmd("docker", "stop", "--time", "10", self.container)

    def reader(self):
        other=Engine(self.kind,self.directory)
        if self.kind=='postgres':
            other.connection=psycopg.connect("host=127.0.0.1 port=25432 dbname=benchmark user=benchmark password=isolated-benchmark",autocommit=True)
        elif self.kind=='duckdb':
            other.connection=self.connection.cursor()
        return other

    def kill_restart(self):
        if self.connection:
            self.connection.close();self.connection=None
        if self.kind=='duckdb':
            # Separate writer exits abruptly after a committed transaction and an
            # uncommitted mutation. Reopen checks WAL commit/rollback semantics.
            code="import duckdb,os,sys; c=duckdb.connect(sys.argv[1]); c.execute('CREATE TABLE recovery_marker(v INTEGER)'); c.execute('INSERT INTO recovery_marker VALUES(7)'); c.execute('BEGIN'); c.execute('INSERT INTO recovery_marker VALUES(8)'); os._exit(137)"
            process=subprocess.run([sys.executable,'-c',code,str(self.directory/'database.duckdb')],capture_output=True,text=True)
            assert process.returncode==137,process.stderr
            self.connection=duckdb.connect(str(self.directory/'database.duckdb'))
            assert self.query('SELECT v FROM recovery_marker ORDER BY v')==[[7]]
        else:
            cmd('docker','kill','--signal','KILL',self.container)
            cmd('docker','start',self.container)
            deadline=time.monotonic()+60
            while True:
                try:
                    if self.kind=='postgres':
                        self.connection=psycopg.connect("host=127.0.0.1 port=25432 dbname=benchmark user=benchmark password=isolated-benchmark",autocommit=True)
                    self.query('SELECT 1');break
                except Exception:
                    if time.monotonic()>deadline:raise
                    time.sleep(.1)


def normalize(rows, numeric_start=2):
    return [[v if i < numeric_start or isinstance(v, str) and not v.lstrip('-').replace('.', '').isdigit()
             else Decimal(v) if v is not None else None for i, v in enumerate(row)] for row in rows]


def workloads(engine, profile, minutes, symbol_index):
    s = symbol_index
    symbol, source = f"SYM{s:03}", SOURCES[s % 2]
    where = f"symbol='{symbol}' AND source='{source}'"
    cols = ','.join(BAR_COLS)
    bars, ticks = engine.fact(profile), engine.fact(profile, "ticks")
    cursor = BASE + (minutes - 301) * 60_000_000
    lo, hi = BASE + (minutes // 3) * 60_000_000, BASE + (minutes // 3 + 600) * 60_000_000
    if engine.kind == "questdb":
        first, last = "first(p_open)", "last(p_close)"
        # Chronological input is explicit; first/last cannot rely on accidental order.
        aggregate = f"SELECT min(time_us),{first},max(p_high),min(p_low),{last},sum(p_volume) AS known_volume_sum,sum(revision),count(*),count(p_volume) AS known_volume_count,min(CASE WHEN state='final' THEN 1 ELSE 0 END) FROM (SELECT * FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi} ORDER BY time_us)"
    elif engine.kind == "clickhouse":
        aggregate = f"SELECT min(time_us),argMin(p_open,time_us),max(p_high),min(p_low),argMax(p_close,time_us),if(count(p_volume)=0,NULL,sum(p_volume)) AS known_volume_sum,sum(revision),count(*),count(p_volume) AS known_volume_count,min(if(state='final',1,0)) FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi}"
    else:
        first = f"(SELECT p_open FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi} ORDER BY time_us LIMIT 1)"
        last = f"(SELECT p_close FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi} ORDER BY time_us DESC LIMIT 1)"
        aggregate = f"SELECT min(time_us),{first},max(p_high),min(p_low),{last},sum(p_volume) AS known_volume_sum,sum(revision),count(*),count(p_volume) AS known_volume_count,min(CASE WHEN state='final' THEN 1 ELSE 0 END) FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi}"
    queries = {
        "latest_state": f"SELECT {cols} FROM {engine.fact(profile,'states')} WHERE {where}",
        "latest_300": f"SELECT * FROM (SELECT {cols} FROM {bars} WHERE {where} ORDER BY time_us DESC LIMIT 300) recent ORDER BY time_us",
        "exclusive_page_300": f"SELECT * FROM (SELECT {cols} FROM {bars} WHERE {where} AND time_us<{cursor} ORDER BY time_us DESC LIMIT 300) recent ORDER BY time_us",
        "range_600": f"SELECT {cols} FROM {bars} WHERE {where} AND time_us>={lo} AND time_us<{hi} ORDER BY time_us LIMIT 1000",
        "same_source_ohlcv": aggregate,
        "ordered_event_replay_1000": f"SELECT {','.join(TICK_COLS)} FROM {ticks} WHERE {where} AND event_id>0 ORDER BY event_id LIMIT 1000",
        "research_scan": f"SELECT source,count(*),min(p_low),max(p_high),sum(p_volume),sum(revision) FROM {bars} GROUP BY source ORDER BY source",
    }
    if engine.kind=='questdb':
        # The designated timestamp is QuestDB's native range/order index. Using
        # its redundant LONG time_us for these predicates unfairly forces scans.
        for name,sql in queries.items():
            sql=re.sub(r'time_us(>=|<)([0-9]+)',r'ts\1cast(\2 as timestamp)',sql)
            sql=sql.replace('ORDER BY time_us DESC LIMIT','ORDER BY ts DESC LIMIT')
            if name=='range_600':sql=sql.replace('ORDER BY time_us LIMIT','ORDER BY ts LIMIT')
            if name=='same_source_ohlcv':sql=sql.replace('ORDER BY time_us)','ORDER BY ts)')
            queries[name]=sql
    return queries


def expected_workloads(final, ticks, minutes, symbol_index):
    rows = [r for r in final if r[0] == f"SYM{symbol_index:03}" and r[1] == SOURCES[symbol_index%2]]
    rows.sort(key=lambda r: r[2])
    cursor = BASE + (minutes - 301) * 60_000_000
    lo, hi = BASE + (minutes // 3) * 60_000_000, BASE + (minutes // 3 + 600) * 60_000_000
    selected = [r for r in rows if lo <= r[2] < hi]
    volume = sum((r[7] for r in selected if r[7] is not None), Decimal(0)) if any(r[7] is not None for r in selected) else None
    aggregate = [[min(r[2] for r in selected), selected[0][3], max(r[4] for r in selected), min(r[5] for r in selected), selected[-1][6], volume, sum(r[8] for r in selected), len(selected), sum(r[7] is not None for r in selected), int(all(r[10]=='final' for r in selected))]]
    research = []
    for source in SOURCES:
        group = [r for r in final if r[1] == source]
        research.append([source,len(group),min(r[5] for r in group),max(r[4] for r in group),sum((r[7] for r in group if r[7] is not None),Decimal(0)),sum(r[8] for r in group)])
    return {"latest_state": rows[-1:], "latest_300": rows[-300:], "exclusive_page_300": [r for r in rows if r[2]<cursor][-300:],
            "range_600": selected, "same_source_ohlcv": aggregate,
            "ordered_event_replay_1000": sorted([r for r in ticks if r[0]==f"SYM{symbol_index:03}" and r[1]==SOURCES[symbol_index%2]],key=lambda r:r[3])[:1000], "research_scan": research}


def concurrent_check(engine,profile,final,args):
    latest={}
    for row in final:latest[tuple(row[:2])]=row
    base_rows=list(latest.values())
    stop=threading.Event();samples=[];errors=[]
    reader=engine.reader()
    sql=workloads(engine,profile,max(args.minutes,args.hot_minutes),0)['latest_300']
    def read_loop():
        try:
            while not stop.is_set() or len(samples)<30:
                start=time.perf_counter();rows=normalize(reader.query(sql));samples.append((time.perf_counter()-start)*1000)
                assert len(rows)==300
                assert all(r[0]=='SYM000' and r[1]=='source_a' for r in rows)
                assert all(r[4]>=max(r[3],r[6]) and r[5]<=min(r[3],r[6]) for r in rows)
        except Exception as exc:errors.append(repr(exc))
        finally:
            if reader.connection:reader.connection.close()
            reader.session.close()
    thread=threading.Thread(target=read_loop,daemon=True);thread.start()
    write_samples=[];updates=[]
    try:
        for i in range(50):
            updates=[]
            for original in base_rows:
                row=original.copy();row[8]=7;row[9]=100_000_000+i
                row[10]='final';row[11]=row[2]+60_000_000
                row[6]=row[3];row[7]=None
                updates.append(row)
            ack,visible,_=engine.insert(f'{profile}_bars',updates,BAR_COLS,True)
            write_samples.append(visible)
    finally:
        stop.set();thread.join(timeout=60)
    assert not thread.is_alive() and not errors,errors
    replacement={tuple(r[:3]):r for r in updates}
    expected=[replacement.get(tuple(r[:3]),r) for r in final]
    actual=normalize(engine.query(f"SELECT {','.join(BAR_COLS)} FROM {engine.fact(profile)} ORDER BY symbol,source,time_us"))
    assert digest(actual)==digest(expected)
    return {'writer_batches':50,'batch_rows':len(updates),'writer_durable_visible':stats(write_samples),
            'concurrent_latest_300':stats(samples),'whole_row_and_source_checks_passed':True,'final_bars_sha256':digest(expected)}


def run_candidate(kind, args, data, manifest):
    directory = OUT / kind
    directory.mkdir(parents=True, exist_ok=True)
    if kind == "duckdb":
        for path in directory.glob("database.duckdb*"):
            path.unlink()
    engine = Engine(kind, directory)
    result = {"candidate": kind, "profiles": {}, "timing_scope": "client encoding + network/embedded call + server durable acknowledgement + result decoding; warm sequential reads", "errors": []}
    executables=cmd('ps','-axo','comm').splitlines()
    result['host_load_before']={'load_average':os.getloadavg(),'concurrent_build_processes':{name:sum(Path(x.strip()).name==name for x in executables) for name in ['cargo','rustc','node']},'host_exclusive':False}
    try:
        engine.start()
        result["config"] = engine.config
        result["precision_probe"] = engine.precision_probe()
        print(f"{kind}: full mantissa/scale/ns precision gate passed", flush=True)
        for profile, (bars, ticks, revisions, final, rejected) in data.items():
            engine.schema(profile)
            metrics = {}
            for table, rows, columns, gate in [("bars",bars,BAR_COLS,True),("ticks",ticks,TICK_COLS,False),("bars",revisions,BAR_COLS,True)]:
                samples_ack, samples_visible, rejected_actual = [], [], 0
                started = time.perf_counter()
                for offset in range(0,len(rows),args.batch):
                    ack, visible, dropped = engine.insert(f"{profile}_{table}",rows[offset:offset+args.batch],columns,gate)
                    samples_ack.append(ack); samples_visible.append(visible); rejected_actual += dropped
                elapsed = time.perf_counter() - started
                phase = "revisions" if rows is revisions else table
                metrics[phase] = {"rows_submitted":len(rows),"elapsed_s":elapsed,"rows_per_s":len(rows)/elapsed,
                                  "ack":stats(samples_ack),"visible":stats(samples_visible),"app_rejected_stale":rejected_actual}
            latest={}
            for row in final:
                latest[tuple(row[:2])]=row
            states=list(latest.values())
            engine.insert(f"{profile}_states",states,BAR_COLS,True)
            # Exact retries must not create extra events, even with same timestamp
            # distinct identities. The digest below checks the complete event set.
            retries=ticks[:min(10000,len(ticks))]
            retry_start=time.perf_counter()
            for offset in range(0,len(retries),args.batch):
                engine.insert(f"{profile}_ticks",retries[offset:offset+args.batch],TICK_COLS)
            metrics['idempotent_event_retry']={'rows_submitted':len(retries),'elapsed_s':time.perf_counter()-retry_start}
            if kind == "postgres":
                engine.execute(f"ANALYZE {profile}_bars");engine.execute(f"ANALYZE {profile}_ticks")
            actual_bars = normalize(engine.query(f"SELECT {','.join(BAR_COLS)} FROM {engine.fact(profile)} ORDER BY symbol,source,time_us"))
            actual_ticks = normalize(engine.query(f"SELECT {','.join(TICK_COLS)} FROM {engine.fact(profile,'ticks')} ORDER BY event_id"))
            correctness = {"bars_count":len(actual_bars),"ticks_count":len(actual_ticks),"bars_sha256":digest(actual_bars),"ticks_sha256":digest(actual_ticks)}
            assert correctness["bars_sha256"] == manifest["profiles"][profile]["final_bars_sha256"], correctness
            assert correctness["ticks_sha256"] == manifest["profiles"][profile]["ticks_sha256"], correctness
            null_query=f"SELECT count(*),count(p_volume),"+("if(count(p_volume)=0,NULL,sum(p_volume))" if kind=='clickhouse' else "sum(p_volume)")+f" FROM {engine.fact(profile)} WHERE symbol='SYM000' AND source='source_b'"
            null_actual=normalize(engine.query(null_query),0)
            assert digest(null_actual)==digest([[args.minutes,0,None]]),null_actual
            correctness['all_null_volume']={'passed':True,'rows':args.minutes,'known_count':0,'known_sum':None}
            queries, query_correctness = {}, {}
            for s in [0,1,args.symbols-1]:
                effective_minutes=max(args.minutes,args.hot_minutes) if s==0 else args.minutes
                expected = expected_workloads(final,ticks,effective_minutes,s)
                for name, sql in workloads(engine,profile,effective_minutes,s).items():
                    actual = normalize(engine.query(sql), 1 if name=="research_scan" else 0 if name=="same_source_ohlcv" else 2)
                    assert digest(actual)==digest(expected[name]), (kind,profile,name,s,actual[:3],expected[name][:3])
                    query_correctness[f"{name}:{s}"] = {"rows":len(actual),"sha256":digest(actual)}
            for name in workloads(engine,profile,max(args.minutes,args.hot_minutes),0):
                samples = []
                pieces={}
                sqls = [workloads(engine,profile,max(args.minutes,args.hot_minutes) if s==0 else args.minutes,s)[name] for s in range(args.symbols)]
                # Three warmups; measured queries rotate through symbols.
                for sql in sqls[:3]:
                    engine.query(sql)
                for i in range(args.samples):
                    start = time.perf_counter(); engine.query(sqls[i%args.symbols]); samples.append((time.perf_counter()-start)*1000)
                    for key,val in getattr(engine,'last_timing',{}).items():pieces.setdefault(key,[]).append(float(val))
                queries[name] = stats(samples)
                queries[name]['timing_components']={key:stats(vals) for key,vals in pieces.items()}
            hot_queries={}
            for name,sql in workloads(engine,profile,max(args.minutes,args.hot_minutes),0).items():
                samples=[];pieces={}
                for i in range(args.samples):
                    start=time.perf_counter();engine.query(sql);samples.append((time.perf_counter()-start)*1000)
                    for key,val in getattr(engine,'last_timing',{}).items():pieces.setdefault(key,[]).append(float(val))
                hot_queries[name]=stats(samples)
                hot_queries[name]['timing_components']={key:stats(vals) for key,vals in pieces.items()}
                if kind=='duckdb':
                    arrow_samples=[];arrow_parts={}
                    for i in range(args.samples):
                        start=time.perf_counter();table,parts=engine.query_arrow(sql);arrow_samples.append((time.perf_counter()-start)*1000)
                        for key,val in parts.items():arrow_parts.setdefault(key,[]).append(val)
                    actual=[[r[c] for c in table.column_names] for r in table.to_pylist()]
                    assert digest(actual)==digest(expected_workloads(final,ticks,max(args.minutes,args.hot_minutes),0)[name])
                    hot_queries[name]['arrow']={**stats(arrow_samples),'timing_components':{key:stats(vals) for key,vals in arrow_parts.items()},'exact_roundtrip_passed':True}
            parquet = None
            if kind == "duckdb":
                parquet_path = directory / f"{profile}-bars.parquet"
                start = time.perf_counter()
                engine.execute(f"COPY (SELECT * FROM {profile}_bars ORDER BY symbol,source,time_us) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 12288)")
                # Explicit file durability barrier; directory fsync for new file.
                with parquet_path.open('rb') as handle:
                    os.fsync(handle.fileno())
                descriptor = os.open(directory,os.O_RDONLY);os.fsync(descriptor);os.close(descriptor)
                parquet = {"export_fsync_s":time.perf_counter()-start,"bytes":parquet_path.stat().st_size,"queries":{}}
                for name,sql in workloads(engine,profile,max(args.minutes,args.hot_minutes),0).items():
                    if name in {"ordered_event_replay_1000","latest_state"}:
                        continue
                    sql=sql.replace(f"{profile}_bars",f"read_parquet('{parquet_path}')")
                    actual=normalize(engine.query(sql),1 if name=='research_scan' else 0 if name=='same_source_ohlcv' else 2)
                    expected=expected_workloads(final,ticks,max(args.minutes,args.hot_minutes),0)[name]
                    assert digest(actual)==digest(expected)
                    samples=[]
                    for i in range(args.samples):
                        start=time.perf_counter();engine.query(sql);samples.append((time.perf_counter()-start)*1000)
                    parquet['queries'][name]=stats(samples)
            result["profiles"][profile] = {"writes":metrics,"correctness":correctness,"query_correctness":query_correctness,
                                             "warm_queries":queries,"hot_symbol_queries":hot_queries,"query_sql":workloads(engine,profile,max(args.minutes,args.hot_minutes),0),"parquet":parquet}
            if kind=='clickhouse':
                result['profiles'][profile]['actual_table_settings']=engine.query(f"SELECT name,engine_full FROM system.tables WHERE database=currentDatabase() AND name IN ('{profile}_bars','{profile}_ticks') ORDER BY name")
            if kind=='postgres':
                result['profiles'][profile]['postgres_explain']={name:engine.query('EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) '+sql)[0][0] for name,sql in workloads(engine,profile,max(args.minutes,args.hot_minutes),0).items()}
            dump(OUT/f"{kind}.json",result)
            print(f"{kind}/{profile}: all-row digests and 21 business queries passed; writes {metrics['bars']['rows_per_s']:.0f} bars/s; latest300 {queries['latest_300']['p50_ms']:.3f}ms",flush=True)
        for profile,(_,_,_,final,_) in data.items():
            result['profiles'][profile]['concurrent']=concurrent_check(engine,profile,final,args)
            dump(OUT/f'{kind}.json',result)
            print(f'{kind}/{profile}: concurrent revision writes + whole-row reads passed',flush=True)
        restart_start=time.perf_counter();engine.kill_restart();restart_elapsed=time.perf_counter()-restart_start
        recovery={}
        for profile in data:
            actual=normalize(engine.query(f"SELECT {','.join(BAR_COLS)} FROM {engine.fact(profile)} ORDER BY symbol,source,time_us"))
            tick_actual=normalize(engine.query(f"SELECT {','.join(TICK_COLS)} FROM {engine.fact(profile,'ticks')} ORDER BY event_id"))
            assert digest(actual)==result['profiles'][profile]['concurrent']['final_bars_sha256']
            assert digest(tick_actual)==manifest['profiles'][profile]['ticks_sha256']
            recovery[profile]={'passed':True,'bars_sha256':digest(actual),'ticks_sha256':digest(tick_actual),
                               'bars_count':len(actual),'ticks_count':len(tick_actual)}
        result['abrupt_process_restart']={'restart_s':restart_elapsed,'profiles':recovery,
                                          'scope':'SIGKILL process/container; acknowledges verified fsync policy. Does not simulate device/power failure or validate replica recovery.'}
        print(f'{kind}: SIGKILL recovery all-row digests passed',flush=True)
        result["passed"] = True
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        result["passed"] = False
        print(f"{kind}: FAILED {exc}",flush=True)
        import traceback;traceback.print_exc()
    finally:
        try:
            engine.close()
        except Exception as exc:
            result["errors"].append(f"cleanup: {exc}")
        dump(OUT/f"{kind}.json",result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidates',default='postgres,questdb,clickhouse,duckdb')
    parser.add_argument('--symbols',type=int,default=90)
    parser.add_argument('--minutes',type=int,default=1440)
    parser.add_argument('--batch',type=int,default=1000)
    parser.add_argument('--samples',type=int,default=100)
    parser.add_argument('--hot-minutes',type=int,default=0)
    args=parser.parse_args()
    assert args.symbols>=2 and args.minutes>=1000 and args.samples>=20
    OUT.mkdir(parents=True,exist_ok=True)
    data={p:dataset(p,args.symbols,args.minutes,args.hot_minutes) for p in ['scaled','wide']}
    manifest={"created_at":time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),"seed":"formula-v1; no random", "base_time_us":BASE,
              "symbols":args.symbols,"sources":SOURCES,"minutes_per_symbol":args.minutes,"hot_symbol_minutes":max(args.minutes,args.hot_minutes),"bounded_batch_rows":args.batch,"read_samples":args.samples,
              "profiles":{},"columns":{"bars":BAR_COLS,"ticks":TICK_COLS},"client":{"python":platform.python_version(),"platform":platform.platform(),"machine":platform.machine(),"psycopg":psycopg.__version__,"duckdb":duckdb.__version__,"requests":requests.__version__,'pyarrow':pyarrow.__version__},
              "docker":cmd('docker','info','--format','{{.Architecture}} {{.NCPU}} {{.MemTotal}}'),
              "precision_contract":"exact mantissa+scale for Rust 96-bit decimal and SQL NUMERIC(38,18), signed64 timestamp_ns; no client floating conversion",
              "caveats":["warm sequential local client timings; no OS cold-cache claim","Docker Linux vs native macOS DuckDB; resource limits comparable but not identical execution environments","bar schema is compact business projection; excludes raw JSON payload and cross-store atomic event cursor","QuestDB stale revision gate is included but in-memory; durable gate reconstruction/atomic log remains integration work","OHLCV volume is known_volume_sum plus known_volume_count/total count, not an exact total where inputs are unknown","no replica/HA, power-cut testing, concurrent acquisition or multi-client contention", "kdb+/q unmeasured: licensed runtime not available; cost is not an exclusion criterion"]}
    for profile,(bars,ticks,revisions,final,rejected) in data.items():
        manifest['profiles'][profile]={"encoding":"Int64 price scale=4, volume scale=0" if profile=='scaled' else 'DECIMAL(38,18)',
                                       "bars":len(bars),"ticks":len(ticks),"revisions_submitted":len(revisions),"stale_revisions_expected":rejected,
                                       "final_bars_sha256":digest(final),"ticks_sha256":digest(sorted(ticks,key=lambda r:r[3]))}
        fixtures=OUT/'fixtures';fixtures.mkdir(exist_ok=True)
        (fixtures/f'{profile}-bars.csv').write_text(csv_text(final))
        (fixtures/f'{profile}-ticks.csv').write_text(csv_text(sorted(ticks,key=lambda r:r[3])))
    dump(OUT/'manifest.json',manifest)
    results = [run_candidate(kind,args,data,manifest) for kind in args.candidates.split(',')]
    if not all(result['passed'] for result in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
