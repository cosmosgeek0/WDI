#!/usr/bin/env python3
"""Read-only retained network aggregate, with metadata-only change checks.

Private cache is separate from canonical ledgers. Only fixed aggregate leaves
reach latest.json. Does not import or call any recorder or provider client.
"""
import argparse
from contextlib import closing
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from validate_flow import MINUTE, history_periods, num as number, stamp, validate

AGE = 90000
SAMPLE_AGE = 150000
FOLDERS = ('hg-fleet', 'packetstream', 'proxyrack', 'repocket', 'earnfm',
           'traffmonetizer', 'pawns', 'grass')
INVENTORY_SOURCES = ('grass',)


def public_ip(v):
    try:
        a = ipaddress.ip_address(v)
        return str(a) if a.is_global else None
    except (TypeError, ValueError):
        return None


def signature(paths):
    parts = []
    for p in paths:
        try:
            s = p.stat()
            parts.append((str(p), s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns))
        except FileNotFoundError:
            parts.append((str(p), None))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def atomic_write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as f:
        tmp = f.name
        json.dump(value, f, separators=(',', ':'), allow_nan=False)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    try:
        os.chmod(tmp, mode); os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def read_json(path):
    with Path(path).open('rb') as f:
        raw = f.read(32 * 1024 * 1024 + 1)
    if len(raw) > 32 * 1024 * 1024: raise ValueError('bounded input')
    return json.loads(raw)


def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON'); db.execute('BEGIN')
    return db


class Aggregate:
    def __init__(self, now):
        self.last = now // MINUTE * MINUTE
        self.first = self.last - 1439 * MINUTE
        self.amount = 0
        self.count = 0
        self.start = None
        self.end = None
        self.ips = set()
        self.owners = {}
        self.minutes = {}

    def observe(self, start, end, ip):
        if stamp(start) and stamp(end) and start <= end:
            self.start = min(self.start or start, start)
            self.end = max(self.end or end, end)
        ip = public_ip(ip)
        if ip: self.ips.add(ip)
        return ip

    def point(self, minute, amount, ip, estimated):
        if not self.first <= minute <= self.last: return
        p = self.minutes.setdefault(minute, [0, set(), False])
        p[0] += amount
        if ip: p[1].add(ip)
        p[2] |= bool(estimated)

    def interval(self, start, end, amount, ip=None, instance=None, exact=None, scope='container'):
        ip = self.observe(start, end, ip)
        if not number(amount) or not stamp(start) or not stamp(end) or end < start: return
        if instance: self.owners.setdefault(instance, set()).add(scope)
        self.amount += amount; self.count += 1
        if exact is not None:
            self.point(exact, amount, ip, False)
        elif start == end:
            self.point(end // MINUTE * MINUTE, amount, ip, True)
        else:
            for minute in range(max(self.first, start // MINUTE * MINUTE),
                                min(self.last, (end - 1) // MINUTE * MINUTE) + MINUTE, MINUTE):
                overlap = min(end, minute + MINUTE) - max(start, minute)
                if overlap > 0: self.point(minute, amount * overlap / (end - start), ip, True)

    def saved(self):
        return dict(amount=self.amount if self.count else None, start=self.start, end=self.end,
                    ips=sorted(self.ips), owners={k: sorted(v) for k, v in self.owners.items()},
                    minutes={str(t): [p[0], sorted(p[1]), p[2]] for t, p in self.minutes.items()})


def read_hg(path, now):
    # These are pure canonical readers/constants, never the recorder entrypoint.
    from dashboard.fleet_test import GAP_MS, HG_EXCLUDED_IPS, timestamp_ms
    series = {}
    with path.open() as f:
        for line in f:
            try: row = json.loads(line)
            except ValueError: continue
            for w in row.get('workers', []):
                sample = w.get('sample') or {}
                try: at = timestamp_ms(sample.get('time'))
                except (TypeError, ValueError): continue
                if not stamp(at) or at > now: continue
                key = (w.get('organization'), w.get('group'), w.get('machine'), w.get('instance'))
                if not all(key): continue
                rx, tx = sample.get('rx_total'), sample.get('tx_total')
                amount = rx + tx if number(rx) and number(tx) and w.get('ip') not in HG_EXCLUDED_IPS else None
                points = series.setdefault(key, {})
                pair = (amount, w.get('ip'))
                points[at] = pair if at not in points or points[at] == pair else (None, None)
    agg = Aggregate(now)
    for key, raw in series.items():
        previous = None
        for at, (amount, ip) in sorted(raw.items()):
            agg.observe(at, at, ip)
            if previous:
                before, old, old_ip = previous
                if ip == old_ip and number(old) and number(amount) and amount >= old and 0 < at - before <= GAP_MS:
                    agg.interval(before, at, amount - old, ip, key[-1])
            previous = at, amount, ip
    return agg.saved()


def static_source(root, folder, now):
    if folder == 'hg-fleet': return read_hg(root / folder / 'observations.jsonl', now)
    agg = Aggregate(now)
    p = root / folder / ('minutes.sqlite3' if folder == 'packetstream' else 'traffic.sqlite3')
    with closing(connect(p)) as db:
        if folder in ('packetstream', 'proxyrack', 'pawns'):
            for row in db.execute('SELECT start_ms,end_ms,rx+tx amount,ip,instance,valid,seq,bucket_ms FROM minutes'):
                amount = row['amount'] if row['valid'] == 1 and row['seq'] > 0 else None
                agg.interval(row['start_ms'], row['end_ms'], amount, row['ip'], row['instance'], row['bucket_ms'])
        elif folder == 'repocket':
            for row in db.execute("SELECT start_ms,end_ms,rx_bytes+tx_bytes amount,ip,instance,valid,json_extract(body,'$.partial') partial,json_extract(body,'$.final') final,json_extract(body,'$.traffic_scope') scope,json_extract(body,'$.application') application FROM events WHERE kind='minute'"):
                s, e = row['start_ms'], row['end_ms']
                agg.observe(s, e, row['ip'])
                if row['valid'] != 1 or not stamp(s) or not stamp(e): continue
                bucket = round(s / MINUTE) * MINUTE
                exact = not row['partial'] and not row['final'] and 59000 <= e - s <= 61000 and abs(s - bucket) <= 1000
                scope = 'application:repocket' if row['scope'] == 'application' and row['application'] == 'repocket' else 'container'
                agg.interval(bucket if exact else s, bucket + MINUTE if exact else e, row['amount'], row['ip'], row['instance'], bucket if exact else None, scope)
        elif folder == 'earnfm':
            for row in db.execute('SELECT time_ms,rx+tx amount,ip,instance,estimated FROM minute_cache'):
                agg.interval(row['time_ms'], row['time_ms'] + MINUTE, row['amount'], row['ip'], row['instance'], None if row['estimated'] else row['time_ms'])
        elif folder == 'traffmonetizer':
            meta = read_json(root / folder / 'latest.json').get('deployment') or {}
            baseline = meta.get('measurement_started_ms') or meta.get('started_ms') or 0
            for row in db.execute('SELECT time_ms,rx+tx amount,ip,instance FROM samples WHERE start_ms>=? AND rx IS NOT NULL AND tx IS NOT NULL', (baseline,)):
                minute = row['time_ms'] // MINUTE * MINUTE
                agg.interval(minute, minute + MINUTE, row['amount'], row['ip'], row['instance'])
        elif folder == 'grass':
            for row in db.execute('SELECT r.first_ms,r.last_ms,r.bytes,r.ip,r.instance,r.duration_ms,m.scope FROM run_ips r LEFT JOIN measurement_groups m ON m.group_id=r.group_id'):
                agg.observe(row['first_ms'], row['last_ms'], row['ip'])
                if row['duration_ms'] > 0 and number(row['bytes']):
                    agg.amount += row['bytes']; agg.count += 1
                    scope = 'application:grass' if row['scope'] == 'application' else 'container'
                    agg.owners.setdefault(row['instance'], set()).add(scope)
            for row in db.execute('SELECT minute_ms,bytes,ip,estimated FROM minute_nodes WHERE minute_ms>=? AND minute_ms<=? AND coverage_ms>0', (agg.first, agg.last)):
                if number(row['bytes']): agg.point(row['minute_ms'], row['bytes'], public_ip(row['ip']), row['estimated'])
    return agg.saved()




def inventory_ip(worker, now):
    sample = worker.get('sample_ms')
    if not stamp(sample) or not 0 <= now-sample <= SAMPLE_AGE or worker.get('final'):
        return None
    return public_ip(worker.get('ip'))


def inventory(view, now):
    result = dict(active=None, known_active=None, active_without_ip=None, observed_ms=None,
                  valid_until_ms=None, status='unavailable')
    if not isinstance(view, dict): return result
    observed, generated = view.get('inventory_ms'), view.get('generated_ms')
    if not stamp(observed) or not stamp(generated) or max(observed, generated) > now: return result
    result['observed_ms'] = observed
    result['valid_until_ms'] = min(observed, generated) + AGE
    if now > result['valid_until_ms']:
        result['status'] = 'stale'; return result
    workers, sources = view.get('workers'), view.get('sources')
    if (not isinstance(workers, list) or not isinstance(sources, list) or not sources or view.get('error')
            or any(not isinstance(item, dict) for item in workers + sources)): return result
    identities = [(w.get('group_id'), w.get('id')) for w in workers]
    if any(not all(isinstance(value, str) and value for value in pair) for pair in identities): return result
    if len({pair[1] for pair in identities}) != len(identities): return result
    groups = [s.get('group_id') for s in sources if s.get('group_id') is not None]
    if any(not isinstance(group, str) or not group for group in groups) or len(set(groups)) != len(groups): return result
    clocks = [s.get('inventory_ms') for s in sources] + [w.get('inventory_ms') for w in workers]
    if any(not stamp(t) or t > now for t in clocks): return result
    if any(s.get('error') for s in sources) or any(w.get('source_error') for w in workers): return result
    result['valid_until_ms'] = min([observed, generated] + clocks) + AGE
    if now > result['valid_until_ms']:
        result['status'] = 'stale'; return result
    running = [w for w in workers if w.get('salad_state') == 'running']
    if type(view.get('running')) is not int or view['running'] != len(running): return result
    ips, missing, expires = set(), 0, []
    for w in running:
        ip = inventory_ip(w, now)
        if not ip:
            missing += 1
        else:
            ips.add(ip); expires.append(w['sample_ms'] + SAMPLE_AGE)
    result.update(active=len(ips) if not missing else None, known_active=len(ips), active_without_ip=missing,
                  status='partial' if missing else 'current', valid_until_ms=min([result['valid_until_ms']] + expires))
    return result


def selected_inventory(views, now):
    """Merge only configured inventories; an absent or invalid member is unknown."""
    values = list(views.values())
    generated = [v.get('generated_ms') if isinstance(v, dict) else None for v in values]
    observed = min(generated) if generated and all(stamp(t) and t <= now for t in generated) else None
    source = dict(observed_ms=observed, valid_until_ms=observed+AGE if observed else None,
                  status=('current' if now-observed <= AGE else 'stale') if observed else 'unavailable')
    result = inventory({}, now)
    states = [inventory(v, now) for v in values]
    if not states: return source, result
    for key in ('observed_ms', 'valid_until_ms'):
        clocks = [state[key] for state in states]
        result[key] = min(clocks) if all(stamp(t) for t in clocks) else None
    if any(state['status'] == 'unavailable' for state in states):
        source['status'] = 'unavailable'; return source, result
    if any(state['status'] == 'stale' for state in states):
        result['status'] = 'stale'; return source, result

    workers = [w for v in values for w in v['workers']]
    identities = [w['id'] for w in workers]
    groups = [s['group_id'] for v in values for s in v['sources'] if s.get('group_id') is not None]
    if len(set(identities)) != len(identities) or len(set(groups)) != len(groups):
        source['status'] = 'unavailable'; return source, result
    ips = {ip for w in workers if w.get('salad_state') == 'running' and (ip := inventory_ip(w, now))}
    missing = sum(state['active_without_ip'] for state in states)
    result.update(active=len(ips) if not missing else None, known_active=len(ips), active_without_ip=missing,
                  status='partial' if missing else 'current')
    return source, result


def build_payload(sources, view, revision, now, inventory_views=None):
    minute = now // MINUTE * MINUTE
    first = minute - 1439 * MINUTE
    points, per_ip = [], {}
    available = [s for s in sources.values() if s and number(s.get('amount'))]
    all_ips = set().union(*(s['ips'] for s in available))
    # A shared container can contribute separate app counters, but its interface
    # counter must never be added to another recording of that same traffic.
    owned = {}
    for s in available:
        for instance, scopes in s['owners'].items():
            previous = owned.setdefault(instance, set())
            scopes = set(scopes)
            if previous and ('container' in previous | scopes or previous & scopes):
                raise ValueError('shared source identity requires reconciliation')
            previous.update(scopes)
    for t in range(first, minute + MINUTE, MINUTE):
        found = [s['minutes'][str(t)] for s in available if str(t) in s['minutes']]
        ipset = set().union(*(p[1] for p in found))
        per_ip[t] = ipset
        points.append([t, round(sum(p[0] for p in found) / 1e9, 12) if found else None,
                       len(ipset), True, any(p[2] for p in found)])
    periods = []
    for known, period in history_periods(points, minute+MINUTE):
        period.update(gb=round(sum(p[1] for p in known), 12) if known else None,
                      distinct_ips=len(set().union(*(per_ip[p[0]] for p in known))) if known else None)
        periods.append(period)
    # The positional view remains supported for existing single-view callers.
    # Producer supplies the explicitly selected views, separately from history.
    source, counts = selected_inventory({'legacy': view} if inventory_views is None else inventory_views, now)
    return dict(schema_version=1, generated_ms=now, revision=revision, scope='retained_network_recordings',
                source=source,
                recorded_traffic=dict(gb=round(sum(s['amount'] for s in available)/1e9, 12) if available else None,
                                      start_ms=min((s['start'] for s in available if s['start']), default=None),
                                      end_ms=max((s['end'] for s in available if s['end']), default=None),
                                      coverage='partial_recordings', status='partial' if available else 'unavailable'),
                ips=dict(all_recorded=len(all_ips) if available else None, **counts),
                traffic_24h=dict(start_ms=first, end_ms=minute+MINUTE, bucket_ms=MINUTE, points=points), periods=periods)


class Producer:
    def __init__(self, root, state, inventory_sources=None):
        if not inventory_sources or any(folder not in INVENTORY_SOURCES for folder in inventory_sources):
            raise ValueError('explicit inventory source required: grass')
        self.inventory_sources = tuple(sorted(set(inventory_sources)))
        self.root, self.state = Path(root), Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        self.cache = {}
        p = self.state / 'private-cache.json'
        if p.exists(): self.cache = read_json(p)
        self.last_revision = None
        self.view = {}
        self.inventory_views = {}

    def paths(self, folder):
        root = self.root / folder
        if folder == 'hg-fleet': return [root / 'observations.jsonl']
        db = root / ('minutes.sqlite3' if folder == 'packetstream' else 'traffic.sqlite3')
        return [db, Path(str(db)+'-wal')] + ([root/'latest.json'] if folder == 'traffmonetizer' else [])

    def update(self, now=None):
        explicit_now = now is not None
        now = now or int(time.time()*1000)
        tokens = {f: signature(self.paths(f)) for f in FOLDERS}
        inventory_paths = {folder: self.root/folder/'view.json' for folder in self.inventory_sources}
        inventory_tokens = {folder: signature([path]) for folder, path in inventory_paths.items()}
        # Minute rollover expires the chart's oldest bin without rescanning static data.
        revision = hashlib.sha256(json.dumps([tokens, inventory_tokens, now//MINUTE], sort_keys=True).encode()).hexdigest()
        if revision == self.last_revision: return None
        sources = {}
        for folder in FOLDERS:
            saved = self.cache.get(folder)
            if not saved or 'owners' not in saved['aggregate'] or saved['revision'] != tokens[folder]:
                result = static_source(self.root, folder, now)
                # SQLite readers hold a transaction snapshot while writers continue.
                if folder == 'hg-fleet' and signature(self.paths(folder)) != tokens[folder]:
                    raise BlockingIOError('source changing')
                saved = dict(revision=tokens[folder], aggregate=result)
                self.cache[folder] = saved
            sources[folder] = saved['aggregate']
        selected = {}
        for folder, path in inventory_paths.items():
            inventory_tokens[folder] = signature([path])
            try:
                selected[folder] = read_json(path)
            except (OSError, ValueError):
                selected[folder] = None
            if signature([path]) != inventory_tokens[folder]:
                raise BlockingIOError('inventory publication changing')
        if not explicit_now:
            now = int(time.time()*1000)
        revision = hashlib.sha256(json.dumps([tokens, inventory_tokens, now//MINUTE], sort_keys=True).encode()).hexdigest()
        self.inventory_views = selected
        view = selected.get('grass')
        self.view = view if isinstance(view, dict) else {}
        result = build_payload(sources, self.view, revision, now, inventory_views=self.inventory_views)
        if not validate(result, now): raise ValueError('invalid public contract')
        atomic_write(self.state/'private-cache.json', self.cache)
        atomic_write(self.state/'private-reconciliation.json', dict(generated_ms=now, source_totals={k:v['amount'] for k,v in sources.items()},
                      source_ip_counts={k:len(v['ips']) for k,v in sources.items()},
                      active_view_mb=self.view.get('total_mb'), public_gb=result['recorded_traffic']['gb']))
        atomic_write(self.state/'latest.json', result, 0o644)
        self.last_revision = revision
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', default='/home/salad/.hg1')
    p.add_argument('--state', required=True)
    p.add_argument('--inventory-source', action='append', choices=INVENTORY_SOURCES, required=True,
                   help='active inventory to include; repeat to merge sources (independent of retained history)')
    p.add_argument('--once', action='store_true')
    args = p.parse_args()
    producer = Producer(args.root, args.state, inventory_sources=args.inventory_source)
    while True:
        try:
            result = producer.update()
            if result: print(json.dumps({'generated_ms':result['generated_ms'],'source':result['source']['status']}), flush=True)
        except BlockingIOError:
            if args.once: return 2
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            print('{"export":"unavailable"}', flush=True)
            if args.once: return 1
        if args.once: return 0
        time.sleep(2)


if __name__ == '__main__': raise SystemExit(main())
