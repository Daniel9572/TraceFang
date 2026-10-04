#!/usr/bin/env python3
"""Read-only real WebSocket comparison: long CP101 ->102/137 versus cold empty prefixes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import urllib.request
import websockets


def canonical(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode()


def compare(a, b):
    for key in ('stream_sequence', 'input_watermark', 'logical_at_ns', 'actual_received_at_ns', 'state_hash', 'items'):
        if a.get(key) != b.get(key):
            raise AssertionError('warm/cold prefix differs at ' + key)
    for key in ('input_hash', 'snapshot_hash', 'input_count', 'confirmed_count'):
        if a['quant_snapshot']['evidence'][key] != b['quant_snapshot']['evidence'][key]:
            raise AssertionError('warm/cold shared quant differs at ' + key)


def summary(event, elapsed):
    evidence = event['quant_snapshot']['evidence']
    return {'cursor': event['stream_sequence'], 'input_watermark': event['input_watermark'],
            'checkpoint_sequence': event.get('checkpoint_sequence'),
            'decoded_tail_frames': event['decoded_tail_frames'], 'state': event['state'],
            'logical_at_ns': event['logical_at_ns'], 'received_at_ns': event.get('actual_received_at_ns'),
            'state_hash': event['state_hash'], 'input_hash': evidence['input_hash'],
            'snapshot_hash': evidence['snapshot_hash'], 'input_count': evidence['input_count'],
            'confirmed_count': evidence['confirmed_count'], 'view_sha256': hashlib.sha256(canonical(event['items'])).hexdigest(),
            'wall_ms_to_snapshot_and_paused_status': elapsed, 'warmup_incomplete': event['warmup_incomplete'],
            'physical_store_epoch': evidence['token']['store_epoch']}


async def receive_snapshot(socket, expected, epoch):
    started = time.perf_counter()
    snapshot = None
    while True:
        value = json.loads(await asyncio.wait_for(socket.recv(), 90))
        if value.get('state') == 'unavailable' or value.get('kind') == 'error':
            raise AssertionError('actual replay failed: ' + json.dumps(value))
        if value.get('kind') == 'snapshot':
            assert value['stream_sequence'] == str(expected)
            assert value['input_watermark']['sequence'] == str(expected)
            assert value['input_watermark']['epoch'] == epoch
            assert value['quant_snapshot']['evidence']['token']['committed_frame_seq'] == str(expected)
            assert value['state'] == 'paused' and value['paused'] is True
            snapshot = value
        if snapshot and value.get('kind') == 'status' and value.get('state') == 'paused':
            assert value['input_watermark'] == snapshot['input_watermark']
            return snapshot, (time.perf_counter() - started) * 1000


async def run(args):
    root = args.base_url.rstrip('/')
    ws = root.replace('http://', 'ws://').replace('https://', 'wss://')
    def url(sequence):
        return ws + f'/api/replay/stream/AU8888?period=1m&source_id=tonghuashun_futures&start_sequence={sequence}&end_sequence=137&paused=true'
    report = {'kind': 'actual_deployed_complete_facts_shared_quant_long_state_short_tail',
              'base_url': root, 'capture_epoch': args.capture_epoch,
              'binary_sha256': args.binary_sha256,
              'initial_seed': 'empty retained prefix; no live or legacy PG seed',
              'timing_scope': 'native actual WebSocket control->inclusive snapshot+explicit paused; not exclusive SLO',
              'snapshots': {}}
    warm_events = {}
    async with websockets.connect(url(101), proxy=None, max_size=16*1024*1024) as socket:
        first, elapsed = await receive_snapshot(socket, 101, args.capture_epoch)
        assert first['checkpoint_sequence'] is None and first['decoded_tail_frames'] == '101'
        assert int(first['quant_snapshot']['evidence']['confirmed_count']) > 20000
        report['snapshots']['cold_101'] = summary(first, elapsed)
        for target in (102, 101, 137):
            await socket.send(json.dumps({'command': 'seek', 'sequence': str(target)}))
            value, elapsed = await receive_snapshot(socket, target, args.capture_epoch)
            assert value['checkpoint_sequence'] == '101'
            assert value['decoded_tail_frames'] == str(target - 101)
            warm_events[target] = value
            report['snapshots'][f'cp101_to_{target}'] = summary(value, elapsed)
        # Repeated target preserves business hashes while restoring complete saved facts+quant.
        await socket.send(json.dumps({'command': 'seek', 'sequence': '137'}))
        repeated, elapsed = await receive_snapshot(socket, 137, args.capture_epoch)
        assert repeated['checkpoint_sequence'] == '137' and repeated['decoded_tail_frames'] == '0'
        compare(repeated, warm_events[137])
        report['snapshots']['repeat_137'] = summary(repeated, elapsed)
        await socket.send(json.dumps({'command': 'stop'}))
    for target in (102, 137):
        async with websockets.connect(url(target), proxy=None, max_size=16*1024*1024) as socket:
            cold, elapsed = await receive_snapshot(socket, target, args.capture_epoch)
            assert cold['checkpoint_sequence'] is None and cold['decoded_tail_frames'] == str(target)
            compare(warm_events[target], cold)
            assert cold['quant_snapshot']['evidence']['token']['store_epoch'] != warm_events[target]['quant_snapshot']['evidence']['token']['store_epoch']
            report['snapshots'][f'empty_prefix_to_{target}'] = summary(cold, elapsed)
            await socket.send(json.dumps({'command': 'stop'}))
    report['all_business_hashes_and_exact_display_rows_equal'] = True
    report['all_intervening_frames_preserved'] = True
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=args.report.parent, delete=False) as file:
        json.dump(report, file, indent=2); file.flush(); os.fsync(file.fileno()); temporary = file.name
    os.replace(temporary, args.report)
    print(json.dumps({'report': str(args.report.resolve()), 'complete_prefix_and_short_tail_pass': True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--capture-epoch', required=True)
    parser.add_argument('--binary-sha256', required=True)
    parser.add_argument('--report', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))

if __name__ == '__main__':
    main()
