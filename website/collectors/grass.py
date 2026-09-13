#!/usr/bin/env python3
"""Record Grass native health, recovery, traffic and provider observations."""
import argparse
import base64
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import boto3
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('GRASS_DATA_DIR', str(Path.home() / '.hg1/grass')))
ORG = os.environ.get('GRASS_ORGANIZATION', 'cosmosgeeketh')
PROJECT = os.environ.get('GRASS_PROJECT', 'default')
GROUP = os.environ.get('GRASS_GROUP', 'grass')
PATH = '/projects/' + PROJECT + '/containers/' + GROUP
TARGETS_FILE = os.environ.get('GRASS_TARGETS_FILE')
LEGACY_GROUP_ID = '5717d5ea-5fea-47f0-839f-6e7744b911f5'
FRESH_MS = 90_000
PROVIDER_FRESH_MS = 180_000
REGISTRATION_SOURCE = 'https://api.grass.io/retrieveUser'
REGISTRATION_RETAIN_MS = 32 * 86400_000
REGISTRATION_MAX_ROWS = 50_000
RECONCILE_LOOKBACK_MS = 60 * 60_000
RECONCILE_SLICE_MS = 5 * 60_000
RECONCILE_EVERY_MS = 60_000
RECONCILE_CALLS = 16
RECONCILE_SECONDS = 20
LABELS = {'connected': 'Connected', 'starting': 'Starting', 'offline': 'Disconnected',
    'client_missing': 'Client not running', 'client_stalled': 'Client stalled',
    'network_rejected': 'Network quality rejected', 'login_required': 'Sign-in required',
    'unknown': 'Grass status unavailable', 'stopping': 'Stopping'}


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False))
    os.replace(temporary, path)


def source_path(target=None):
    return '/projects/' + (target['project'] if target else PROJECT) + '/containers/' + (target['group_name'] if target else GROUP)


def load_targets(path=None):
    if not path:
        return [dict(organization=ORG, project=PROJECT, group_name=GROUP, group_id=None, replicas=1)]
    manifest = json.loads(Path(path).read_text())
    if manifest.get('revision') != 1 or not isinstance(manifest.get('targets'), list) or not 1 <= len(manifest['targets']) <= 7:
        raise RuntimeError('Grass source manifest must contain one to seven revision-1 targets')
    targets, seen = [], set()
    for row in manifest['targets']:
        target = {k: row[k] for k in ('organization', 'project', 'group_name', 'group_id', 'replicas')}
        if any(not re.fullmatch('[a-z0-9-]+', target[k]) for k in ('organization', 'project', 'group_name')):
            raise RuntimeError('Grass source manifest has an invalid source name')
        target['group_id'] = str(uuid.UUID(target['group_id']))
        if target['group_id'] in seen or type(target['replicas']) is not int or not 0 <= target['replicas'] <= 100:
            raise RuntimeError('Grass source manifest has duplicate groups or invalid replicas')
        target['expected_account_id'] = manifest.get('expected_account_id')
        seen.add(target['group_id']); targets.append(target)
    if sum(t['replicas'] for t in targets) > 100:
        raise RuntimeError('Grass source manifest exceeds 100 total replicas')
    return targets


def state_name(name, target=None):
    return name + ':' + target['group_id'] if target else name


def source_state(db, target, now):
    # Only the original Grass UUID inherits the original saved cursor. Keep the
    # legacy keys intact as rollback evidence; no other group may borrow them.
    with db:
        for name in ('cursor', 'reconcile_due_ms', 'reconcile_end_ms'):
            value = db.execute('SELECT value FROM state WHERE name=?', (name,)).fetchone() if target['group_id'] == LEGACY_GROUP_ID else None
            if value or name == 'cursor':
                db.execute('INSERT OR IGNORE INTO state VALUES(?,?)', (state_name(name, target), value[0] if value else now-600000))
    saved = db.execute('SELECT body FROM sources WHERE group_id=?', (target['group_id'],)).fetchone()
    return json.loads(saved[0]) if saved else dict(target, inventory=[], inventory_ms=None, error='Awaiting source inventory')


def api(key, path, body=None, *, timeout_seconds=20, target=None):
    config = 'user-agent = "WDI/1.0"\nheader = ' + json.dumps('Salad-Api-Key: ' + key) + '\nheader = "Content-Type: application/json"\n'
    if body is not None:
        config += 'data-binary = ' + json.dumps(json.dumps(body)) + '\n'
    response = subprocess.run(['curl', '-sS', '--max-time', str(timeout_seconds), '--config', '-',
        '--write-out', '\n%{http_code}', 'https://api.salad.com/api/public/organizations/' + (target['organization'] if target else ORG) + path],
        input=config, text=True, capture_output=True, timeout=timeout_seconds+5)
    if response.returncode:
        raise RuntimeError('Salad transport request failed')
    raw, status = response.stdout.rsplit('\n', 1)
    if status != '200':
        raise RuntimeError('Salad read returned HTTP ' + status)
    return json.loads(raw)


def integer(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value) or not 0 <= value < 2**53:
        raise ValueError('Invalid ' + name)
    return int(value)


def parse_record(entry, received_ms):
    text = entry.get('text_log', '')
    if not text.startswith(('GRASS_SAMPLE_V1 ', 'GRASS_SAMPLE_V2 ', 'GRASS_EVENT_V2 ', 'GRASS_PRIVATE_EVENT_V1 ')):
        return None
    row = json.loads(text.split(' ', 1)[1])
    runtime_event = text.startswith('GRASS_PRIVATE_EVENT_V1 ')
    if runtime_event:
        if row.get('event') not in ('ip_classified','reallocation_requested','reallocation_result','reallocation_not_sent'):
            return None
        row.update(revision=2,kind='grass_event',run=None,source_format='GRASS_PRIVATE_EVENT_V1')
        policy = row.get('policy') if isinstance(row.get('policy'),dict) else {}
        row['ip'] = row.get('ip') or policy.get('ip')
        row['reason'] = row.get('reason') or policy.get('reason') or policy.get('error')
        if row['event'].startswith('reallocation_'):row['action']='reallocate'
    revision, kind = row.get('revision'), row.get('kind')
    if revision not in (1, 2) or kind not in ('grass_sample', 'grass_event'):
        return None
    for name in ('instance', 'machine'):
        if str(uuid.UUID(row[name])) != row[name]:
            raise ValueError('Invalid native identity')
    if not runtime_event and (not isinstance(row.get('run'), str) or not row['run'].startswith(row['instance'] + ':' + row['machine'] + ':')):
        raise ValueError('Invalid native run')
    row['source_ms'] = integer(row['source_ms'], 'source time')
    if row['source_ms'] > received_ms + 10_000:
        raise ValueError('Future native source time')
    if revision == 2 and str(uuid.UUID(row['group_id'])) != row['group_id']:
        raise ValueError('Invalid native group')
    if kind == 'grass_sample':
        row['monotonic_ms'] = integer(row['monotonic_ms'], 'monotonic_ms')
        for name in ('rx','tx'):
            row[name] = integer(row[name], name) if row.get(name) is not None else None
        if row.get('ip') is not None:
            row['ip'] = str(ipaddress.ip_address(row['ip']))
        if row.get('country') is not None and not re.fullmatch('[A-Z]{2}', row['country']):
            raise ValueError('Invalid native country')
        if revision == 2:
            row['seq'] = integer(row['seq'], 'sequence')
            health = row['health']
            if health.get('state') not in LABELS:
                raise ValueError('Invalid native health state')
            integer(health['observed_ms'], 'health time')
            if health.get('account_id') is not None and not isinstance(health['account_id'], str):
                raise ValueError('Invalid provider account identity')
            if health.get('pid') is not None:
                integer(health['pid'], 'client process')
            if health['state'] == 'connected' and (not health.get('pid') or not health.get('device_id') or not row.get('ip') and row.get('valid') is not False):
                raise ValueError('Connected status lacks native identity')
            if type(row.get('final')) is not bool:
                raise ValueError('Invalid final-sample flag')
            provider = row.get('provider') or {}
            if provider.get('observed_ms') is not None:
                integer(provider['observed_ms'], 'provider time')
                for name in ('network_points', 'total_points', 'reward_points', 'referral_points'):
                    value = provider.get(name)
                    if value is None:
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise ValueError('Invalid provider points')
    elif not isinstance(row.get('event'), str):
        raise ValueError('Invalid native event')
    row['recorded_ms'] = received_ms
    row['log_envelope'] = {k: entry[k] for k in ('time','timestamp','resource') if k in entry}
    return row


def collect(key, start, end, *, max_calls=64, budget_seconds=None, initial_slice_ms=None, retry_timeouts=False, machines=None, target=None):
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    calls = 0
    deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None

    def fetch(begin, stop, machine=None):
        nonlocal calls
        query = 'resource.type = "container" and resource.labels.project_name = "' + (target['project'] if target else PROJECT) + '" and resource.labels.container_group_name = "' + (target['group_name'] if target else GROUP) + '"'
        if machine:
            query += ' and resource.labels.machine_id = ' + json.dumps(str(uuid.UUID(machine)))
        query += ' and (text_log startswith "GRASS_SAMPLE_V" or text_log startswith "GRASS_EVENT_V2 " or text_log startswith "GRASS_PRIVATE_EVENT_V1 ")'
        for attempt in range(2 if retry_timeouts else 1):
            calls += 1
            if calls > max_calls:
                raise RuntimeError('Grass log window exceeded its pagination budget; cursor retained')
            remaining = deadline - time.monotonic() if deadline is not None else 20
            if remaining <= 0:
                raise RuntimeError('Grass log window exceeded its time budget; cursor retained')
            try:
                result = api(key, '/log-entries', {'query': query,
                    'start_time': iso(begin), 'end_time': iso(stop), 'page_size': 100, 'sort_order': 'asc'},
                    timeout_seconds=min(20, remaining), **({'target': target} if target else {}))
                break
            except RuntimeError as error:
                if not retry_timeouts or attempt or str(error) != 'Salad read returned HTTP 408':
                    raise
        rows = result.get('items')
        if not isinstance(rows, list):
            raise RuntimeError('Grass log response has no record list')
        if len(rows) < 100:
            return rows
        # Keep the complete timestamp prefix of an ascending page. Re-read
        # its last timestamp so equal-time entries are never silently lost.
        # Binary time splitting remains the fallback for missing/tied stamps.
        try:
            stamps = [int(datetime.fromisoformat(row['time'].replace('Z','+00:00')).timestamp()*1000) for row in rows]
            boundary = max(stamps)
            if begin < boundary < stop and min(stamps) >= begin:
                return [row for row, at in zip(rows,stamps) if at < boundary] + fetch(boundary, stop, machine)
        except (KeyError,ValueError,TypeError):
            pass
        middle = (begin + stop) // 2
        if middle <= begin:
            raise RuntimeError('Grass log timestamp contains an incomplete capped page; cursor retained')
        return fetch(begin, middle, machine) + fetch(middle, stop, machine)

    records = []
    entries = []
    position = start
    while position < end:
        stop = min(end, position+(initial_slice_ms or end-start))
        for machine in sorted(set(machines)) if machines else [None]:
            entries.extend(fetch(position, stop, machine))
        position = stop
    for entry in entries:
        try:
            row = parse_record(entry, int(time.time() * 1000))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError('A Grass native record is malformed; cursor retained') from None
        if row is not None:
            if target:
                row.update({k: target[k] for k in ('organization','project','group_name')})
            records.append(row)
    return records


def collect_fleet(key, start, end, target, **options):
    """Bound large-group reads by four disjoint time windows, never by node count.

    The API caps pages at 100. Each worker retains capped-page splitting and
    its time/call budgets. A failure rejects the whole window and its cursor.
    SQLite ingestion stays on the recorder thread after every read succeeds.
    """
    if not target or target.get('replicas', 0) <= 10:
        return collect(key, start, end, **dict(options, target=target))
    options = dict(options, machines=None, initial_slice_ms=None)
    bounds = [start+(end-start)*i//4 for i in range(5)]
    with ThreadPoolExecutor(max_workers=4) as readers:
        batches = list(readers.map(lambda span: collect(key, *span, target=target, **options), zip(bounds, bounds[1:])))
    return [row for batch in batches for row in batch]


def reconcile(db, key, group_id, now, machines=None, target=None):
    """Revisit older event times without moving the normal collection cursor."""
    due = db.execute('SELECT value FROM state WHERE name=?', (state_name('reconcile_due_ms', target),)).fetchone()
    if due and now < due[0]:
        return None
    latest, oldest = now-120_000, max(0, now-RECONCILE_LOOKBACK_MS)
    saved = db.execute('SELECT value FROM state WHERE name=?', (state_name('reconcile_end_ms', target),)).fetchone()
    end = saved[0] if saved and oldest < saved[0] <= latest else latest
    begin = max(oldest, end-RECONCILE_SLICE_MS)
    # A failed window is retried on the next cadence, never silently skipped.
    with db:
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (state_name('reconcile_due_ms', target), now+RECONCILE_EVERY_MS))
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (state_name('reconcile_end_ms', target), end))
    records = collect_fleet(key, begin, end, target, max_calls=RECONCILE_CALLS, budget_seconds=RECONCILE_SECONDS,
        initial_slice_ms=None if machines else 60_000, retry_timeouts=True, machines=machines, )
    inserted = ingest(db, records, group_id)
    with db:
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)', (state_name('reconcile_end_ms', target), begin))
    return {'start_ms': begin, 'end_ms': end, 'records': len(records), 'inserted': inserted}


def replay(db, key, group_id, minutes, data_dir, machine=None, target=None):
    """Back up all SQLite state, then replay a fixed, bounded source window."""
    if not 1 <= minutes <= 180:
        raise ValueError('Replay must be between 1 and 180 minutes')
    end = int(time.time()*1000)
    begin = max(0, end-minutes*60_000)
    backups = data_dir/'backups'
    backups.mkdir(mode=0o700, exist_ok=True)
    backup = backups/('traffic-before-replay-'+str(time.time_ns())+'.sqlite3')
    with sqlite3.connect(backup) as destination:
        db.backup(destination)
    inserted = records_seen = 0
    position = begin
    while position < end:
        stop = min(position+RECONCILE_SLICE_MS, end)
        records = collect(key, position, stop, max_calls=RECONCILE_CALLS, budget_seconds=RECONCILE_SECONDS,
            initial_slice_ms=None if machine else 60_000, retry_timeouts=True,
            machines=[machine] if machine else None, **({'target': target} if target else {}))
        inserted += ingest(db, records, group_id)
        records_seen += len(records)
        position = stop
    return {'backup': str(backup), 'start_ms': begin, 'end_ms': end,
            'machine': machine, 'records': records_seen, 'inserted': inserted}


def run_key(row):
    # Preserve the native run in the saved body; all ledger keys include group.
    return (row.get('group_id') or 'legacy') + '|' + row['run']


def cache_interval(db, row, sign):
    run, source_ms, instance, machine, group, ip, country, start, rx, tx, duration, valid, reason = row
    if not valid:
        return
    db.execute("UPDATE run_ips SET bytes=bytes+?,duration_ms=duration_ms+?,intervals=intervals+?,max_interval_ms=max(max_interval_ms,?) WHERE run=? AND ip=?",
        (sign*(rx+tx), sign*duration, sign, duration, run, ip or ''))
    if not 0 < duration <= 90000:
        return
    bucket = start//60000*60000
    while bucket < source_ms:
        overlap = max(0, min(source_ms,bucket+60000)-max(start,bucket))
        if overlap:
            db.execute("""INSERT INTO minute_nodes VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(minute_ms,group_id,instance,machine,ip)
                DO UPDATE SET bytes=bytes+excluded.bytes,coverage_ms=coverage_ms+excluded.coverage_ms,
                estimated=estimated+excluded.estimated""", (bucket,group or '',instance,machine,ip or '',country,
                sign*(rx+tx)*overlap/duration,sign*overlap,sign*int(start!=bucket or source_ms!=bucket+60000)))
        bucket += 60000


def cache_sample(db, row, inserted):
    db.execute("""INSERT INTO run_ips VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run,ip)
        DO UPDATE SET first_ms=min(first_ms,excluded.first_ms),last_ms=max(last_ms,excluded.last_ms),
        received_ms=NULLIF(max(COALESCE(received_ms,0),COALESCE(excluded.received_ms,0)),0),samples=samples+excluded.samples,
        country=COALESCE(excluded.country,country)""", (run_key(row),row.get('ip') or '',row.get('group_id'),row['instance'],row['machine'],row.get('country'),
        row['source_ms'],row['source_ms'],row.get('recorded_ms'),int(inserted),0,0,0,0))


def update_interval(db, run, source_ms):
    current = db.execute('SELECT body FROM samples WHERE run=? AND source_ms=?', (run, source_ms)).fetchone()
    previous = db.execute('SELECT body FROM samples WHERE run=? AND source_ms<? ORDER BY source_ms DESC LIMIT 1', (run, source_ms)).fetchone()
    if current is None or previous is None:
        return
    current, previous = json.loads(current[0]), json.loads(previous[0])
    elapsed = current['source_ms'] - previous['source_ms']
    monotonic = current['monotonic_ms'] - previous['monotonic_ms']
    rx = current['rx'] - previous['rx'] if current['rx'] is not None and previous['rx'] is not None else None
    tx = current['tx'] - previous['tx'] if current['tx'] is not None and previous['tx'] is not None else None
    required = db.execute('SELECT scope FROM measurement_groups WHERE group_id=?', (current.get('group_id'),)).fetchone()
    app_counter = lambda row: (row.get('traffic_scope') == 'application' and row.get('application') == 'grass' and row.get('valid',True) is True and isinstance(row.get('counter_id'),str) and bool(row['counter_id']))
    reason = None
    if required and (not app_counter(current) or not app_counter(previous)):
        reason = 'application traffic unavailable; shared container counters excluded'
    elif required and current['counter_id'] != previous['counter_id']:
        reason = 'application counter boundary'
    elif rx is None or tx is None:
        reason = 'application counter unavailable'
    elif elapsed <= 0 or monotonic <= 0 or abs(elapsed - monotonic) > 5000:
        reason = 'clock boundary'
    elif current.get('group_id') != previous.get('group_id') or current['instance'] != previous['instance'] or current['machine'] != previous['machine']:
        reason = 'identity boundary'
    elif rx < 0 or tx < 0:
        reason = 'counter reset'
    elif not current.get('ip') or not previous.get('ip') or current['ip'] != previous['ip']:
        reason = 'IP boundary'
    elif db.execute('SELECT 1 FROM conflicts WHERE run=? AND source_ms IN (?,?) LIMIT 1', (run, source_ms, previous['source_ms'])).fetchone():
        reason = 'conflicting native sample'
    valid = reason is None
    old = db.execute('SELECT * FROM intervals WHERE run=? AND source_ms=?', (run, source_ms)).fetchone()
    old_bytes, old_duration, old_count = (old[8]+old[9], old[10], 1) if old and old[11] else (0, 0, 0)
    if old:
        cache_interval(db, old, -1)
    new_bytes, new_duration, new_count = (rx + tx, elapsed, 1) if valid else (0, 0, 0)
    interval = (run, source_ms, current['instance'], current['machine'], current.get('group_id'), current.get('ip'), current.get('country'), previous['source_ms'], rx, tx, elapsed, int(valid), reason)
    db.execute('INSERT OR REPLACE INTO intervals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', interval)
    cache_interval(db, interval, 1)
    db.execute('INSERT INTO totals VALUES(?,?,?,?,?,?) ON CONFLICT(run) DO UPDATE SET bytes=bytes+excluded.bytes, duration_ms=duration_ms+excluded.duration_ms, intervals=intervals+excluded.intervals, started_ms=min(started_ms,excluded.started_ms)',
        (run, current['instance'], new_bytes-old_bytes, new_duration-old_duration, new_count-old_count, previous['source_ms']))


def open_database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS run_ips(run TEXT,ip TEXT,group_id TEXT,instance TEXT,machine TEXT,country TEXT,
            first_ms INTEGER,last_ms INTEGER,received_ms INTEGER,samples INTEGER,bytes INTEGER,duration_ms INTEGER,intervals INTEGER,max_interval_ms INTEGER,PRIMARY KEY(run,ip));
        CREATE TABLE IF NOT EXISTS minute_nodes(minute_ms INTEGER,group_id TEXT,instance TEXT,machine TEXT,ip TEXT,country TEXT,
            bytes REAL,coverage_ms INTEGER,estimated INTEGER,PRIMARY KEY(minute_ms,group_id,instance,machine,ip));
        CREATE TABLE IF NOT EXISTS sources(group_id TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS samples(run TEXT,source_ms INTEGER,body TEXT,PRIMARY KEY(run,source_ms));
        CREATE TABLE IF NOT EXISTS state(name TEXT PRIMARY KEY,value INTEGER);
        CREATE INDEX IF NOT EXISTS sample_instance_time ON samples(json_extract(body,'$.instance'),source_ms);
        CREATE INDEX IF NOT EXISTS sample_group_instance_time ON samples(json_extract(body,'$.group_id'),json_extract(body,'$.instance'),json_extract(body,'$.machine'),source_ms);
        CREATE INDEX IF NOT EXISTS sample_source_time ON samples(source_ms);
        CREATE TABLE IF NOT EXISTS conflicts(run TEXT,source_ms INTEGER,body TEXT,PRIMARY KEY(run,source_ms,body));
        CREATE TABLE IF NOT EXISTS intervals(run TEXT,source_ms INTEGER,instance TEXT,machine TEXT,group_id TEXT,ip TEXT,country TEXT,start_ms INTEGER,rx INTEGER,tx INTEGER,duration_ms INTEGER,valid INTEGER,reason TEXT,PRIMARY KEY(run,source_ms));
        CREATE INDEX IF NOT EXISTS interval_time ON intervals(source_ms);
        CREATE TABLE IF NOT EXISTS totals(run TEXT PRIMARY KEY,instance TEXT,bytes INTEGER,duration_ms INTEGER,intervals INTEGER,started_ms INTEGER);
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,source_ms INTEGER,instance TEXT,machine TEXT,event TEXT,action TEXT,body TEXT);
        CREATE INDEX IF NOT EXISTS event_time ON events(source_ms);
        CREATE INDEX IF NOT EXISTS event_kind_time ON events(event,action,source_ms);
        CREATE INDEX IF NOT EXISTS event_instance_time ON events(instance,machine,event,source_ms);
        CREATE TABLE IF NOT EXISTS provider(scope TEXT,observed_ms INTEGER,body TEXT,PRIMARY KEY(scope,observed_ms));
        CREATE INDEX IF NOT EXISTS provider_time ON provider(observed_ms);
        CREATE TABLE IF NOT EXISTS measurement_groups(group_id TEXT PRIMARY KEY,scope TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS account_registration(observed_ms INTEGER PRIMARY KEY,body TEXT NOT NULL);
    ''')
    if 'reserved' in {row[1] for row in db.execute('PRAGMA table_info(run_ips)')}:
        with db:
            db.execute('ALTER TABLE run_ips RENAME COLUMN reserved TO max_interval_ms')
            db.execute("UPDATE run_ips SET max_interval_ms=(SELECT COALESCE(MAX(duration_ms),0) FROM intervals WHERE run=run_ips.run AND ip IS NULLIF(run_ips.ip,''))")
    if not db.execute("SELECT 1 FROM state WHERE name='group_keys_v1'").fetchone():
        with db:
            for old, body in db.execute('SELECT run,body FROM samples GROUP BY run').fetchall():
                new = run_key(json.loads(body))
                if old == new:
                    continue
                for table in ('samples','conflicts','intervals','totals','run_ips'):
                    db.execute('UPDATE '+table+' SET run=? WHERE run=?', (new,old))
                db.execute("UPDATE provider SET body=json_set(body,'$.sample_run',?) WHERE json_extract(body,'$.sample_run')=?", (new,old))
            db.execute("INSERT INTO state VALUES('group_keys_v1',1)")
    if not db.execute("SELECT 1 FROM state WHERE name='caches_v1'").fetchone():
        with db:
            db.execute('DELETE FROM run_ips')
            db.execute('DELETE FROM minute_nodes')
            for (body,) in db.execute('SELECT body FROM samples'):
                cache_sample(db, json.loads(body), True)
            for row in db.execute('SELECT * FROM intervals'):
                cache_interval(db, row, 1)
            db.execute("INSERT INTO state VALUES('caches_v1',1)")
    if not db.execute("SELECT 1 FROM state WHERE name='derived_v2'").fetchone():
        with db:
            for run, source_ms in db.execute('SELECT run,source_ms FROM samples ORDER BY run,source_ms'):
                update_interval(db, run, source_ms)
            db.execute("INSERT INTO state VALUES('derived_v2',1)")
    return db


def ingest(db, records, group_id):
    inserted = 0
    with db:
        for row in sorted(records, key=lambda r: (r['source_ms'], r['run'] or '')):
            if row.get('revision') == 2 and row.get('group_id') != group_id:
                continue
            if row['kind'] == 'grass_event':
                native = {k: v for k, v in row.items() if k not in ('recorded_ms','log_envelope','organization','project','group_name')}
                event_id = hashlib.sha256(json.dumps(native, sort_keys=True).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)', (event_id, row['source_ms'], row['instance'], row['machine'], row['event'], row.get('action'), json.dumps(row)))
                continue
            storage_run = run_key(row)
            old = db.execute('SELECT body FROM samples WHERE run=? AND source_ms=?', (storage_run, row['source_ms'])).fetchone()
            if old:
                original = json.loads(old[0])
                clean = lambda value: {k: v for k, v in value.items() if k not in ('recorded_ms','log_envelope','organization','project','group_name')}
                if clean(original) == clean(row):
                    continue
                db.execute('INSERT OR IGNORE INTO conflicts VALUES(?,?,?)', (storage_run, row['source_ms'], json.dumps(row)))
            else:
                db.execute('INSERT INTO samples VALUES(?,?,?)', (storage_run, row['source_ms'], json.dumps(row)))
                inserted += 1
            cache_sample(db, original if old else row, old is None)
            update_interval(db, storage_run, row['source_ms'])
            next_row = db.execute('SELECT source_ms FROM samples WHERE run=? AND source_ms>? ORDER BY source_ms LIMIT 1', (storage_run, row['source_ms'])).fetchone()
            if next_row:
                update_interval(db, storage_run, next_row[0])
            provider = row.get('provider') or {}
            if provider.get('account_id') and provider.get('observed_ms') and not provider.get('error') and not db.execute('SELECT 1 FROM conflicts WHERE run=? AND source_ms=?', (storage_run, row['source_ms'])).fetchone():
                scope = '|'.join(str(provider.get(k, '')) for k in ('account_id', 'stage', 'start_date', 'end_date'))
                reading = dict(provider, sample_run=storage_run, sample_ms=row['source_ms'])
                db.execute('INSERT OR IGNORE INTO provider VALUES(?,?,?)', (scope, provider['observed_ms'], json.dumps(reading)))
    return inserted


def view(db, inventory, inventory_ms, desired, error=None, history_note=None, now=None, sources=None):
    now = int(time.time() * 1000) if now is None else now
    totals = {(row[0],row[1]): row[2:] for row in db.execute("SELECT substr(run,1,instr(run,'|')-1),instance,SUM(bytes),SUM(duration_ms),SUM(intervals),MIN(started_ms) FROM totals GROUP BY 1,2")}
    active_groups = {source['group_id'] for source in sources or []}
    if active_groups:totals = {key:value for key,value in totals.items() if key[0] in active_groups}
    minutes = {}
    cutoff = (now // 60000 - 1439) * 60000
    for bucket, group, instance, machine, ip, country, amount, coverage, estimated in db.execute(
            'SELECT * FROM minute_nodes WHERE minute_ms>=? AND coverage_ms>0 ORDER BY minute_ms', (cutoff,)):
        if active_groups and group not in active_groups:continue
        minute = minutes.setdefault(bucket, {'minute_ms': bucket, 'mb': 0.0, 'estimated': False, 'coverage': {}, 'nodes': {}})
        amount = max(0, amount)/1e6
        identity = (group, instance, machine)
        minute['nodes'][(group,instance,machine,ip)] = dict(group_id=group,instance=instance,machine=machine,ip=ip,country=country,mb=amount)
        minute['mb'] += amount
        minute['coverage'][identity] = minute['coverage'].get(identity,0)+coverage
        minute['estimated'] |= estimated > 0
    workers, native = [], []
    for instance in inventory:
        ident = instance['id']
        group_filter = " AND json_extract(body,'$.group_id')=?" if instance.get('group_id') else ''
        params = (ident, instance['machine_id']) + ((instance['group_id'],) if instance.get('group_id') else ())
        record = db.execute("SELECT body FROM samples WHERE json_extract(body,'$.instance')=? AND json_extract(body,'$.machine')=?" + group_filter + " ORDER BY source_ms DESC LIMIT 1", params).fetchone()
        sample = json.loads(record[0]) if record else None
        fresh = sample is not None and 0 <= now-sample['source_ms'] <= FRESH_MS
        health = (sample or {}).get('health') or {}
        current_health = fresh and 0 <= now-health.get('observed_ms', 0) <= FRESH_MS
        conflict = sample and db.execute('SELECT 1 FROM conflicts WHERE run=? AND source_ms=? LIMIT 1', (run_key(sample), sample['source_ms'])).fetchone()
        state, problem, working, note = 'Awaiting native sample', 'No fresh measurement from this instance', None, None
        recovery = (sample or {}).get('recovery') or {}
        evidence = health.get('native') or {}
        native_current = current_health and not conflict and 0 <= now-evidence.get('observed_ms', 0) <= FRESH_MS
        native_connected = (evidence.get('cached_status') == 'CONNECTED' and type(evidence.get('established_tcp')) is int and evidence['established_tcp'] > 0 and bool(health.get('process_identity')) and evidence.get('process_identity') == health.get('process_identity')) if native_current else None
        if current_health and not conflict:
            state = LABELS[health['state']]
            problem = health.get('error')
            if health['state'] == 'unknown':
                state = 'Native connected; awaiting provider' if native_connected else 'Awaiting provider confirmation' if health.get('telemetry_missing') else 'Provider status unavailable'
                if health.get('telemetry_missing'):
                    problem = 'Grass has not yet confirmed this device at its current IP.'
            elif health['state'] == 'offline' and health.get('failure_source') == 'native_disconnect':
                state = 'Reconnecting'
            working = None if health['state'] == 'unknown' else health['state'] == 'connected' and not problem
            if health['state'] == 'connected' and not sample.get('ip'):
                working, state = None, 'Connected; current IP unavailable'
            if health.get('quality') is not None:
                note = f"Provider network quality: {health['quality']:g}%"
            if problem and health.get('next_check_ms', 0) > now:
                note = f"Next status attempt in {(health['next_check_ms']-now)/1000:.0f}s"
            if recovery.get('phase') == 'restarting':
                state, working = 'Restarting Grass', False
            elif recovery.get('phase') == 'reallocating':
                state, working = 'Reallocating', False
            if recovery.get('requested_ms'):
                when = datetime.fromtimestamp(recovery['requested_ms']/1000, timezone.utc).isoformat(timespec='seconds')
                result = recovery.get('phase', recovery.get('last_action', 'requested'))
                detail = f"Recovery {result}: {recovery.get('last_reason', 'unknown')}; requested at {when}"
                note = '; '.join(x for x in (note, detail, recovery.get('error')) if x)
            if not (sample or {}).get('replacement_ready'):
                note = '; '.join(x for x in (note, 'Machine replacement awaits the initial login backup') if x)
        elif sample and not fresh:
            state, problem = 'Source delayed', 'The latest native sample is stale; current client state is unverified'
        elif fresh and sample.get('revision') == 1:
            state, problem = 'Legacy sampler; health unavailable', 'This instance still has the old sampler, without live provider checks'
        elif conflict:
            state, problem = 'Conflicting native data', 'Conflicting records share the same run and source timestamp'
        elif fresh:
            state, problem = 'Grass status delayed', health.get('error') or 'The native health observation is stale'
        boot = db.execute("SELECT body FROM events WHERE instance=? AND machine=? AND event='bootstrap_failed' AND source_ms>? ORDER BY source_ms DESC LIMIT 1", (ident, instance['machine_id'], max(now-FRESH_MS, sample['source_ms'] if sample else 0))).fetchone() if not fresh else None
        if boot:
            boot = json.loads(boot[0])
            state, problem = 'Setup failed', f"Grass startup failed at {boot.get('reason')}; exit {boot.get('exit_code')}"
        interval = db.execute('SELECT rx+tx,duration_ms,valid,reason FROM intervals WHERE run=? AND source_ms=?', (run_key(sample), sample['source_ms'])).fetchone() if sample else None
        total = totals.get((instance.get('group_id') or (sample or {}).get('group_id') or 'legacy',ident))
        valid_total = total is not None and total[2] > 0
        rate = interval[0] * 60000 / interval[1] / 1e6 if interval and interval[2] and 0 < interval[1] <= 90000 and fresh else None
        if interval and not interval[2]:
            explanation = 'New IP baseline pending; the next interval at this IP can be measured' if interval[3] == 'IP boundary' else 'Traffic interval excluded: '+interval[3]
            note = '; '.join(x for x in (note, explanation) if x)
        if sample and sample.get('final'):
            state, working, rate = 'Stopping', False, None
        if instance['state'] != 'running':
            working, rate, native_connected = False, None, None
        source_ms = instance.get('inventory_ms', inventory_ms)
        source_error = instance.get('source_error') if sources is not None else error
        if source_error or not source_ms or now-source_ms > FRESH_MS:
            working, rate = None, None
            problem = source_error or 'Source inventory is stale'
            state, native_connected = 'Source delayed', None
        workers.append({'id': ident, 'group_id': instance.get('group_id'), 'organization': instance.get('organization'),
            'project': instance.get('project'), 'group_name': instance.get('group_name'), 'machine': instance['machine_id'],
            'run': run_key(sample) if sample else None, 'native_run': sample.get('run') if sample else None, 'inventory_ms': source_ms, 'source_error': source_error, 'ip': sample.get('ip') if sample else None, 'country': sample.get('country') if sample else None,
            'native_connected': native_connected, 'health_ms': health.get('observed_ms'),
            'provider_confirmed': working, 'provider_observed_ms': (health.get('provider_listing') or {}).get('observed_ms'),
            'traffic_status': interval[3] if interval and not interval[2] else 'measured' if rate is not None else 'baseline pending',
            'client_status': state, 'salad_state': instance['state'], 'working': working, 'mb_per_minute': rate,
            'total_mb': total[0]/1e6 if valid_total else None, 'recorded_duration_ms': total[1] if valid_total else None,
            'recorded_ms': sample.get('recorded_ms') if sample else None, 'sample_ms': sample['source_ms'] if sample else None,
            'error': problem, 'note': note})
        if sample:
            native.append(sample)
    history = []
    for minute in sorted(minutes.values(), key=lambda row: row['minute_ms']):
        coverage = minute.pop('coverage')
        minute['nodes'] = sorted(minute['nodes'].values(), key=lambda node: -node['mb'])
        minute['count'] = len(coverage)
        minute['partial'] = len(coverage) < desired or any(value < 60000 for value in coverage.values())
        history.append(minute)
    good_provider = "NOT EXISTS (SELECT 1 FROM conflicts WHERE run=json_extract(provider.body,'$.sample_run') AND source_ms=json_extract(provider.body,'$.sample_ms'))"
    accounts = {source.get('expected_account_id') for source in sources or [] if source.get('expected_account_id')}
    accounts = accounts or {x.get('health', {}).get('account_id') for x in native if x.get('health', {}).get('account_id')}
    account_filter = " AND json_extract(body,'$.account_id') IN ("+','.join('?' for _ in accounts)+')' if accounts else ''
    reading = db.execute('SELECT scope,body FROM provider WHERE '+good_provider+account_filter+' ORDER BY observed_ms DESC LIMIT 1', tuple(sorted(accounts))).fetchone()
    credited, provider_note, provider = None, 'Provider points have not been recorded yet.', None
    checks = [s['provider_check'] for s in sources or [] if s.get('provider_check')]
    provider_errors = [c.get('error') for c in checks] if checks else [x.get('provider', {}).get('error') for x in native if x.get('provider')]
    if reading:
        scope, provider = reading[0], json.loads(reading[1])
        stale = not 0 <= now-provider['observed_ms'] <= PROVIDER_FRESH_MS or any(provider_errors)
        provider = dict(provider, fresh=not stale, error=next((x for x in provider_errors if x),None))
        def point_text(key, label):
            value = provider.get(key)
            return f"{value:g} {label}" if value is not None else label+' not reported'
        credited = point_text('network_points','Network Points') if provider.get('network_points') is not None else point_text('total_points','total points') if provider.get('total_points') is not None else point_text('reward_points','rewardPoints')
        if stale: credited += ' (stale)'
        when = datetime.fromtimestamp(provider['observed_ms']/1000, timezone.utc).isoformat(timespec='seconds')
        provider_note = ('Provider account-wide '+str(provider.get('epoch','epoch'))+': '+
            '; '.join(point_text(k,label) for k,label in (('total_points','total points'),('network_points','Network Points'),('reward_points','rewardPoints'),('referral_points','referralPoints')))+
            f". Observed {when}; provider modification time {provider.get('modified_at') or 'not reported'}. Cash earnings are not reported; account points are not assigned to individual IPs.")
    if any(provider_errors):
        provider_note += ' '+next(value for value in provider_errors if value)
    if desired == 1 and (sources is None or len(sources) == 1):
        for sample, worker in zip(native, [w for w in workers if w['sample_ms'] is not None]):
            if worker['working'] is not True:
                continue
            for event_id, raw in db.execute("SELECT id,body FROM events WHERE event='accepted' AND action='reallocate' ORDER BY source_ms DESC LIMIT 20").fetchall():
                event = json.loads(raw)
                first_seen = db.execute("SELECT MIN(source_ms) FROM samples WHERE json_extract(body,'$.instance')=?", (sample['instance'],)).fetchone()[0]
                if event.get('group_id') == sample.get('group_id') and event['machine'] != sample['machine'] and first_seen > event['source_ms']:
                    confirmation = dict(kind='grass_event', revision=2, event='replacement_connected', action='reallocate',
                        source_ms=sample['source_ms'], recorded_ms=now, group_id=sample.get('group_id'), instance=sample['instance'],
                        machine=sample['machine'], previous_instance=event['instance'], previous_machine=event['machine'],
                        request_id=event_id, reason=event.get('reason'), requested_ms=event.get('request_ms'), ip=sample.get('ip'))
                    with db:
                        db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)', (event_id+':connected', sample['source_ms'], sample['instance'], sample['machine'], 'replacement_connected', 'reallocate', json.dumps(confirmation)))
                    break
    last_recovery = db.execute("SELECT body FROM events WHERE action IN ('restart_app','reallocate','replacement_requires_seed') ORDER BY source_ms DESC, CASE event WHEN 'replacement_connected' THEN 6 WHEN 'recovered' THEN 5 WHEN 'failed' THEN 4 WHEN 'unconfirmed' THEN 4 WHEN 'replacement_unavailable' THEN 4 WHEN 'accepted' THEN 3 WHEN 'response' THEN 2 ELSE 1 END DESC LIMIT 1").fetchone()
    recovery_note = ''
    if last_recovery:
        event = json.loads(last_recovery[0])
        when = datetime.fromtimestamp(event['source_ms']/1000, timezone.utc).isoformat(timespec='seconds')
        recovery_note = (f" Latest recorded recovery: {event['action'].replace('_', ' ')} — {event['event'].replace('_', ' ')}"
            + (f" ({event['reason'].replace('_', ' ')})" if event.get('reason') else '')
            + f" at {when}; instance {event['instance']}, machine {event['machine']}.")
        if event['event'] == 'accepted' and event['action'] == 'reallocate':
            recovery_note += ' Waiting for a provider-confirmed replacement.'
    starts = [row[0] for row in db.execute('SELECT MIN(source_ms) FROM samples') if row[0] is not None]
    known = sum(row[2] for row in totals.values())
    inventory_fresh = inventory_ms is not None and 0 <= now-inventory_ms <= FRESH_MS and (sources is not None or not error)
    unknown = any(w['working'] is None for w in workers if w['salad_state'] == 'running')
    traffic_missing = any(w['mb_per_minute'] is None for w in workers if w['salad_state']=='running')
    return {'generated_ms': now, 'inventory_ms': inventory_ms, 'desired': desired,
        'group_id': next(iter(active_groups)) if len(active_groups)==1 else None,
        'traffic_scope':'application',
        'traffic_note':'Grass application traffic is unavailable; shared container RX/TX is excluded.' if traffic_missing else None,
        'sources': [dict({k: source.get(k) for k in ('organization','project','group_name','group_id','replicas','inventory_ms','error')},
            sample_ms=max((w['sample_ms'] or 0 for w in workers if w.get('group_id') == source['group_id']), default=None),
            running=sum(x['state']=='running' for x in source.get('inventory', [])) if source.get('inventory_ms') and 0 <= now-source['inventory_ms'] <= FRESH_MS else None,
            confirmed=sum(w['working'] is True for w in workers if w.get('group_id') == source['group_id'])) for source in sources or []],
        'provider_points': provider,
        'account_registration': registration_view(db, accounts, now),
        'native_connected': sum(w['native_connected'] is True for w in workers),
        'native_known': sum(w['native_connected'] is not None for w in workers),
        'confirmed_known': sum(w['working'] is not None for w in workers),
        'confirmed': sum(w['working'] is True for w in workers),
        'unverified': sum(w['working'] is None for w in workers),
        'running': sum(x['state'] == 'running' for x in inventory) if inventory_fresh else None,
        'working': sum(w['working'] is True for w in workers) if inventory_fresh and not unknown else None,
        'started_ms': min(starts) if starts else None, 'total_mb': sum(row[0] for row in totals.values())/1e6 if known else None,
        'credited_text': credited, 'earned_text': None, 'error': error, 'workers': workers, 'minutes': history,
        'credit_note': ('Current Grass measurements are process-family socket-I/O payload bytes, including application protocol traffic. They exclude shared container counters and are not provider-credited bandwidth. Archived groups retain their original container measurements. '
            'Minute allocation is estimated when intervals cross boundaries. Long gaps contribute measured endpoint totals without invented minute bars. '
            + provider_note + recovery_note + (' History recovery: ' + history_note if history_note else ''))}


def epoch_points(body, expected_account, observed_ms):
    """Validate available original point fields independently; absent is not zero."""
    rows = body.get('result', {}).get('data', {}).get('data')
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise RuntimeError('Grass points response has no epoch record')
    epoch = rows[0]
    if epoch.get('userId') != expected_account:
        raise RuntimeError('Grass points account does not match the configured account')
    points = {}
    for original, name in (('proxyPoints','network_points'),('totalPoints','total_points'),('rewardPoints','reward_points'),('referralPoints','referral_points')):
        value = epoch.get(original)
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value < 0):
            raise RuntimeError('Grass points response has an invalid '+original)
        points[name] = value
    if all(value is None for value in points.values()):
        raise RuntimeError('Grass points response has no reported point amounts')
    return dict(points, observed_ms=observed_ms, account_id=expected_account, epoch=epoch.get('epochName'),
        stage=epoch.get('stage'), start_date=epoch.get('startDate'), end_date=epoch.get('endDate'),
        modified_at=epoch.get('modified'), scope='account_finalized_epoch', source='https://api.grass.io/epochEarnings',
        collector='account_observer', error=None,
        raw={k:epoch[k] for k in ('userId','epochId','epochName','stage','startDate','endDate','modified','proxyPoints','totalPoints','rewardPoints','referralPoints','totalUptime') if k in epoch})


def account_token(group, target):
    env = group['container']['environment_variables']
    seed = json.loads(base64.b64decode(env.get('GRASS_SESSION_B64') or env['GRASS_STATE_B64'], validate=True))
    def decoded(value):
        try: return json.loads(value) if isinstance(value, str) else value
        except ValueError: return value
    if not target.get('expected_account_id') or decoded(seed.get('wynd:user_id')) != target['expected_account_id']:
        raise RuntimeError('Configured Grass session belongs to a different account')
    token = decoded(seed.get('accessToken'))
    if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
        raise RuntimeError('Grass account session has no usable access token')
    return token


def registration_observation(body, expected, checked_ms, observed_ms):
    """The only account metadata allowed to leave the private request path."""
    out = dict(account_id=expected, expected_account_id=expected, account_matches=False,
        device_count=None, device_cap=None, checked_ms=checked_ms, observed_ms=observed_ms,
        received_ms=observed_ms, valid=False, error_code=None, http_status=200)
    result = body.get('result') if isinstance(body, dict) else None
    user = result.get('data') if isinstance(result, dict) else None
    if not isinstance(user, dict):
        out['error_code'] = 'invalid_response'
    elif not expected or user.get('userId') != expected:
        out['error_code'] = 'account_mismatch'
    else:
        out['account_matches'] = True
        for original, name in (('deviceCount', 'device_count'), ('deviceCapOverride', 'device_cap')):
            value = user.get(original)
            if value is None:
                out['error_code'] = out['error_code'] or 'missing_fields'
            else:
                try: out[name] = integer(value, original)
                except ValueError: out['error_code'] = 'invalid_fields'
        out['valid'] = out['error_code'] is None
    return out


def account_registration(group, target, previous):
    now = int(time.time()*1000)
    if (now < previous.get('retry_ms', 0) and
        (previous.get('observation') or {}).get('account_id') == target.get('expected_account_id')):
        return previous
    result = dict(retry_ms=now+60000)
    observation = registration_observation({}, target.get('expected_account_id'), now, now)
    observation.update(http_status=None, error_code='session_unavailable')
    try:
        token = account_token(group, target)
        observation['error_code'] = 'transport_error'
        reply = subprocess.run(['curl', '-sS', '--max-time', '8', '--config', '-', '--dump-header', '/dev/stderr',
            '--write-out', '\n%{http_code}', REGISTRATION_SOURCE],
            input='header = '+json.dumps('Authorization: '+token)+'\n', text=True, capture_output=True, timeout=10)
        if not reply.returncode:
            raw, status = reply.stdout.rsplit('\n', 1)
            observation['http_status'] = int(status)
            observation['error_code'] = 'http_error'
            if status == '200':
                observation['error_code'] = 'invalid_response'
                observation = registration_observation(json.loads(raw), target['expected_account_id'], now, int(time.time()*1000))
            else:
                retry = re.search(r'(?im)^retry-after:\s*(\d+)', reply.stderr)
                result['retry_ms'] = now+max(60000, int(retry[1])*1000 if retry else 300000 if status == '429' else 60000)
    except Exception:
        # No provider payload, curl header or exception text enters saved data.
        pass
    observation['received_ms'] = int(time.time()*1000)
    if not observation['valid']:
        observation['observed_ms'] = observation['received_ms']
    result['observation'] = observation
    return result


def save_registration(db, check):
    observation = (check or {}).get('observation')
    if not observation:
        return
    with db:
        inserted = db.execute('INSERT OR IGNORE INTO account_registration VALUES(?,?)',
            (observation['observed_ms'], json.dumps(observation))).rowcount
        if inserted:
            db.execute('DELETE FROM account_registration WHERE observed_ms<?', (observation['observed_ms']-REGISTRATION_RETAIN_MS,))
            db.execute('DELETE FROM account_registration WHERE observed_ms < (SELECT observed_ms FROM account_registration ORDER BY observed_ms DESC LIMIT 1 OFFSET ?)',
                (REGISTRATION_MAX_ROWS-1,))


def registration_boundary(before, after):
    if before.get('account_id') != after.get('account_id'):
        return 'account_change'
    if not before.get('valid') or not after.get('valid'):
        return 'unavailable_observation'
    if before['device_cap'] != after['device_cap']:
        return 'cap_change'
    if after['device_count'] < before['device_count']:
        return 'count_decrease'
    if not 0 < after['observed_ms']-before['observed_ms'] <= PROVIDER_FRESH_MS:
        return 'observation_gap'
    return None


def registration_growth(rows, window_ms):
    end = rows[-1] if rows else {}
    out = dict(delta=None, start_ms=None, end_ms=end.get('observed_ms'), start_count=None,
        end_count=end.get('device_count'), window_ms=window_ms, covered_ms=0,
        complete=False, boundary='no_baseline', boundary_ms=None)
    if not end.get('valid'):
        out['boundary'] = 'unavailable_observation'
        return out
    begin = end
    cutoff = end['observed_ms']-window_ms
    for before in reversed(rows[:-1]):
        boundary = registration_boundary(before, begin)
        if boundary:
            out.update(boundary=boundary, boundary_ms=begin['observed_ms'])
            break
        begin = before
        if begin['observed_ms'] <= cutoff:
            out.update(complete=True, boundary=None)
            break
    if begin is not end:
        out.update(delta=end['device_count']-begin['device_count'], start_ms=begin['observed_ms'],
            start_count=begin['device_count'], covered_ms=end['observed_ms']-begin['observed_ms'])
    return out


def registration_view(db, accounts, now):
    # The index bounds both retained data and each publication's 24h calculation.
    rows = [json.loads(row[0]) for row in db.execute(
        'SELECT body FROM account_registration WHERE observed_ms>=? ORDER BY observed_ms DESC LIMIT 2048',
        (now-86400_000-PROVIDER_FRESH_MS,))][::-1]
    if not rows:
        last = db.execute('SELECT body FROM account_registration ORDER BY observed_ms DESC LIMIT 1').fetchone()
        rows = [json.loads(last[0])] if last else []
    latest = rows[-1] if rows else {}
    out = dict(latest, scope='provider_account_registered_devices', source=REGISTRATION_SOURCE,
        source_time_basis='Collector receipt of retrieveUser; provider snapshot time is not supplied',
        remaining=None, over_cap=None, growth={})
    if len(accounts) != 1 or latest.get('account_id') not in accounts:
        out.update(device_count=None, device_cap=None, valid=False, account_matches=False,
            error_code='no_observation' if not rows else 'binding_unavailable' if len(accounts) != 1 else 'account_mismatch')
    out['fresh'] = bool(out.get('valid') and 0 <= now-out['observed_ms'] <= PROVIDER_FRESH_MS)
    if out.get('valid'):
        out.update(remaining=max(out['device_cap']-out['device_count'], 0), over_cap=max(out['device_count']-out['device_cap'], 0))
        out['growth'] = {name: registration_growth(rows, span) for name, span in (('hour', 3600000), ('day', 86400000))}
    return out


def account_points(group, target, previous):
    """Read once per account cadence using the existing private Salad session.

    Credentials exist only in memory/curl stdin, never saved in recorder state.
    No refresh, sign-in, or account mutation is attempted.
    """
    now = int(time.time()*1000)
    if now < previous.get('retry_ms', 0):
        return previous
    result = dict(previous, checked_ms=now, retry_ms=now+60000, error=None)
    try:
        expected = target['expected_account_id']
        token = account_token(group, target)
        config = 'header = '+json.dumps('Authorization: '+token)+'\n'
        query = quote(json.dumps({'limit':1,'isLatestOnly':True}))
        reply = subprocess.run(['curl','-sS','--max-time','8','--config','-','--dump-header','/dev/stderr',
            '--write-out','\n%{http_code}','https://api.grass.io/epochEarnings?input='+query],
            input=config,text=True,capture_output=True,timeout=10)
        if reply.returncode:
            raise RuntimeError('Grass points request timed out or failed in transport')
        raw, status = reply.stdout.rsplit('\n',1)
        if status != '200':
            retry = re.search(r'(?im)^retry-after:\s*(\d+)', reply.stderr)
            result['retry_ms'] = now+max(60000,int(retry[1])*1000 if retry else 300000 if status=='429' else 60000)
            raise RuntimeError('Grass points request returned HTTP '+status+('; sign-in required' if status in ('401','403') else ''))
        result['reading'] = epoch_points(json.loads(raw), expected, int(time.time()*1000))
    except Exception as failure:
        result['error'] = safe_error(failure)
    return result


def safe_error(failure):
    return str(failure) if isinstance(failure, RuntimeError) else type(failure).__name__


def poll_source(key, target, previous, registration_owner=True):
    source = dict(previous, **target)
    source['error'] = None
    try:
        group = api(key, source_path(target), target=target)
        if group['id'] != target['group_id']:
            raise RuntimeError('Grass group identity changed; manifest readback is required')
        source['replicas'] = group['replicas']
        if target.get('expected_account_id') and registration_owner:
            source['registration_check'] = account_registration(group, target, previous.get('registration_check') or {})
        response = api(key, source_path(target)+'/instances', target=target)
        if not isinstance(response.get('instances'), list):
            raise RuntimeError('Grass source has no inventory list')
        source['inventory'] = response['instances']
        source['inventory_ms'] = int(time.time()*1000)
        records = collect_fleet(key, source['inventory_ms']-120000, source['inventory_ms'], dict(target, replicas=source['replicas']),
            max_calls=32, budget_seconds=20, retry_timeouts=True)
        if target.get('expected_account_id'):
            source['provider_check'] = account_points(group, target, previous.get('provider_check') or {})
        return source, records
    except Exception as failure:
        source['error'] = safe_error(failure)
        return source, []


def grass_supervisor(db, sources, now):
    """Describe the native IP owner from its original events, never old host policy."""
    groups=[source['group_id'] for source in sources]
    rows=db.execute("SELECT body FROM events WHERE json_extract(body,'$.source_format')='GRASS_PRIVATE_EVENT_V1' "
        "AND json_extract(body,'$.group_id') IN ("+','.join('?' for _ in groups)+") ORDER BY source_ms DESC LIMIT 1", groups).fetchall() if groups else []
    latest=json.loads(rows[0][0]) if rows else {};policy=latest.get('policy') or {}
    status='Awaiting native IP classification'
    if latest.get('event')=='ip_classified':
        status='Datacenter IP rejected' if policy.get('is_datacenter') is True else 'IP admitted by native check' if policy.get('allowed') is True else 'IP classification unavailable; admission held'
    elif latest.get('event')=='reallocation_result':
        status='Salad accepted native reallocation' if policy.get('reallocation_accepted') is True else 'Native reallocation response uncertain'
    elif latest.get('event')=='reallocation_requested':status='Native reallocation requested'
    elif latest.get('event')=='reallocation_not_sent':status='Native reallocation was not sent'
    return dict(scope='container_ip_policy',mode='In container',target_replicas=sum(s['replicas'] for s in sources),
        status=status,observed_ms=latest.get('source_ms'),fresh=False)


def publish(db, sources, history_note=None):
    inventory = []
    for source in sources:
        for node in source.get('inventory', []):
            inventory.append(dict(node, **{k: source.get(k) for k in ('organization','project','group_name','group_id','inventory_ms')}, source_error=source.get('error')))
    inventory_ms = min((s.get('inventory_ms') or 0 for s in sources), default=0) or None
    error = '; '.join(s['organization']+'/'+s['group_name']+': '+s['error'] for s in sources if s.get('error')) or None
    output = view(db, inventory, inventory_ms, sum(s['replicas'] for s in sources), error, history_note, sources=sources)
    output['supervisor'] = grass_supervisor(db, sources, output['generated_ms'])
    save(DATA/'view.json', output)
    print(json.dumps({k: output[k] for k in ('generated_ms','running','confirmed','unverified','total_mb','error','sources')}, separators=(',', ':')), flush=True)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--once', action='store_true')
    action.add_argument('--replay-minutes', type=int, metavar='1..180',
        help='Back up SQLite and replay a bounded historical window; leave the recorder cursor and feed writer alone')
    parser.add_argument('--replay-machine', type=lambda value: str(uuid.UUID(value)), metavar='UUID',
        help='Restrict replay to one verified Salad machine identity')
    args = parser.parse_args()
    if args.replay_minutes is not None and not 1 <= args.replay_minutes <= 180:
        parser.error('--replay-minutes must be between 1 and 180')
    if args.replay_machine and not args.replay_minutes:
        parser.error('--replay-machine requires --replay-minutes')
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    # Historical ingestion uses SQLite transactions and a separate replay lock.
    # The ordinary recorder continues publishing fresh measurements throughout.
    lock = (DATA/('replay.lock' if args.replay_minutes else 'recorder.lock')).open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    key = json.loads(boto3.client('secretsmanager', region_name='ap-south-1').get_secret_value(SecretId='wdi/salad-access')['SecretString'])['api_key']
    db = open_database(DATA/'traffic.sqlite3')
    targets = load_targets(TARGETS_FILE)
    for target in targets:
        if not target['group_id']:
            target['group_id'] = api(key, source_path(target), target=target)['id']
    if args.replay_minutes:
        for target in targets:
            result = replay(db, key, target['group_id'], args.replay_minutes, DATA, args.replay_machine, target=target)
            print(json.dumps({'source': target['group_id'], 'replay': result}), flush=True)
        db.close()
        return
    history_index = 0
    with ThreadPoolExecutor(max_workers=7) as pool:
        while True:
            began, now = time.monotonic(), int(time.time()*1000)
            targets = load_targets(TARGETS_FILE) if TARGETS_FILE else targets
            previous = {target['group_id']: source_state(db, target, now) for target in targets}
            with db:
                db.executemany("INSERT OR IGNORE INTO measurement_groups VALUES(?,'application')", ((target['group_id'],) for target in targets))
            # One central registration request per account, even in a multi-group manifest.
            registration_owners = {}
            for target in targets:
                registration_owners.setdefault(target.get('expected_account_id'), target['group_id'])
            results = list(pool.map(lambda target: poll_source(key, target, previous[target['group_id']],
                registration_owners.get(target.get('expected_account_id')) == target['group_id']), targets))
            for source, records in results:
                ingest(db, records, source['group_id'])
                save_registration(db, source.get('registration_check'))
                reading = (source.get('provider_check') or {}).get('reading')
                if reading:
                    scope = '|'.join(str(reading.get(k, '')) for k in ('account_id','stage','start_date','end_date'))
                    with db:
                        db.execute('INSERT OR IGNORE INTO provider VALUES(?,?,?)', (scope, reading['observed_ms'], json.dumps(reading)))
                with db:
                    db.execute('INSERT OR REPLACE INTO sources VALUES(?,?)', (source['group_id'], json.dumps(source)))
                    cursor = db.execute('SELECT value FROM state WHERE name=?', (state_name('cursor', source),)).fetchone()[0]
                    if not source.get('error') and cursor >= source['inventory_ms']-120000:
                        db.execute('UPDATE state SET value=? WHERE name=?', (source['inventory_ms'], state_name('cursor', source)))
            sources = [result[0] for result in results]
            publish(db, sources)
            target = targets[history_index % len(targets)]; history_index += 1
            source = next(s for s in sources if s['group_id'] == target['group_id'])
            try:
                cursor = db.execute('SELECT value FROM state WHERE name=?', (state_name('cursor', target),)).fetchone()[0]
                stop = source.get('inventory_ms') or now
                if cursor < stop-120000:
                    end = min(cursor+300000, stop)
                    ingest(db, collect_fleet(key, max(0, cursor-120000), end, target,
                        max_calls=32, budget_seconds=20, initial_slice_ms=60000, retry_timeouts=True), target['group_id'])
                    with db:
                        db.execute('UPDATE state SET value=? WHERE name=?', (end, state_name('cursor', target)))
                else:
                    reconcile(db, key, target['group_id'], now,
                        machines=[n['machine_id'] for n in source.get('inventory', []) if n.get('machine_id')], target=dict(target, replicas=source['replicas']))
            except Exception as failure:
                publish(db, sources, safe_error(failure))
            if args.once:
                break
            time.sleep(max(1, 15-(time.monotonic()-began)))
    db.close()


if __name__ == '__main__':
    main()
