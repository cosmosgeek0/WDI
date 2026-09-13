"""Record RP source samples and recovery evidence; publish a bounded read-only view."""
import argparse
from datetime import datetime, timezone
import fcntl
import boto3
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard.native_network import bucket_minutes

DATA = Path.home() / '.hg1/repocket'
ORGANIZATION = os.environ.get('RP_ORGANIZATION','cosmosgeeketh')
PROJECT = os.environ.get('RP_PROJECT','default')
GROUP_NAME = os.environ.get('RP_GROUP','repocket-eval')
GROUP = '/projects/'+PROJECT+'/containers/'+GROUP_NAME
API_KEY = None
FRESH_MS = 90000
UUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z', re.I)


def stamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def millis(value):
    if not value:
        return None
    value = re.sub(r'\.(\d+)(?=[+-])', lambda m: '.' + m.group(1)[:6].ljust(6, '0'), value.replace('Z', '+00:00'))
    return int(datetime.fromisoformat(value).timestamp() * 1000)


def api(path, body=None):
    if path not in (GROUP, GROUP + '/instances', '/log-entries'):
        raise ValueError('Outside Repocket read scope')
    global API_KEY
    if API_KEY is None:
        API_KEY = json.loads(boto3.client('secretsmanager',region_name='ap-south-1').get_secret_value(SecretId='wdi/salad-access')['SecretString'])['api_key']
    key = API_KEY
    config = 'user-agent = "WDI/1.0"\nheader = ' + json.dumps('Salad-Api-Key: ' + key) + '\nheader = "Content-Type: application/json"\n'
    if body is not None:
        config += 'data-binary = ' + json.dumps(json.dumps(body)) + '\n'
    r = subprocess.run(['curl', '-sS', '--max-time', '20', '--config', '-', '--write-out', '\n%{http_code}', 'https://api.salad.com/api/public/organizations/'+ORGANIZATION+path], input=config, text=True, capture_output=True, timeout=25)
    raw, code = r.stdout.rsplit('\n', 1)
    if r.returncode or code != '200':
        raise RuntimeError('Salad ' + path + ' read returned HTTP ' + code)
    return json.loads(raw)


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, separators=(',', ':'), allow_nan=False))
    os.replace(temporary, path)


def fetch(start, end, budget=None, depth=0):
    budget = budget if budget is not None else [128]
    budget[0] -= 1
    if budget[0] < 0 or depth > 20:
        raise RuntimeError('Log window could not be read completely; cursor preserved')
    body = {'query': 'resource.type = "container" and resource.labels.project_name = '+json.dumps(PROJECT)+' and resource.labels.container_group_name = '+json.dumps(GROUP_NAME)+' and text_log startswith "RP_MON "', 'start_time': stamp(start), 'end_time': stamp(end), 'page_size': 100, 'sort_order': 'asc'}
    rows = api('/log-entries', body).get('items', [])
    if len(rows) >= 100:
        middle = (start + end) // 2
        if middle <= start:
            raise RuntimeError('Too many worker records at one timestamp')
        return fetch(start, middle, budget, depth + 1) + fetch(middle, end, budget, depth + 1)
    return rows


def integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def final_window(state, beginning, now):
    # Revisit source times after the full lateness allowance has elapsed. Persist
    # progress only with a successful insert so outages cannot skip a slice.
    boundary = max(beginning, now - 3600000)
    cursor = state.setdefault('final_through_ms', max(beginning, boundary - 60000))
    end = min(boundary, cursor + 60000)
    return (max(beginning, cursor - 60000), end) if end > beginning else None


def fields(item, group_id):
    if item.get('service') != 'repocket' or item.get('kind') not in ('start', 'tick', 'minute', 'recovery'):
        raise ValueError('Unexpected record kind')
    if not integer(item.get('time_ms')) or not isinstance(item.get('run'), str) or not UUID.fullmatch(item.get('machine', '')):
        raise ValueError('Invalid source identity or timestamp')
    instance = item.get('instance')
    if not isinstance(instance, str) or not UUID.fullmatch(instance) or not integer(item.get('started_ms')):
        raise ValueError('Invalid runtime identity')
    if item['started_ms'] > item['time_ms'] or item['run'] != instance + ':' + str(item['started_ms']):
        raise ValueError('Inconsistent run identity')
    if item.get('format') not in (None, 2) or item.get('revision', 1) >= 11 and item.get('format') != 2:
        raise ValueError('Unsupported record format')
    key = None
    if item.get('format') == 2:
        if item.get('group_id') != group_id or not integer(item.get('seq')):
            raise ValueError('Wrong deployment identity or sequence')
        key = group_id + '/' + item['run'] + '/' + str(item['seq'])
    elif item.get('group_id') not in (None, '', group_id):
        raise ValueError('Wrong deployment identity')
    valid = True
    start = end = rx = tx = None
    if item['kind'] == 'minute':
        start, end, rx, tx = (item.get(k) for k in ('start_ms', 'end_ms', 'rx_bytes', 'tx_bytes'))
        valid = item.get('traffic_scope') == 'application' and item.get('application') == 'repocket' and item.get('valid', True) is True and all(integer(v) for v in (start, end, rx, tx)) and end > start >= item['started_ms'] and item.get('elapsed_ms') == end - start
    return {'group_id': group_id, 'instance': instance, 'record_key': key, 'valid': int(valid), 'start_ms': start, 'end_ms': end, 'rx_bytes': rx, 'tx_bytes': tx, 'ip': item.get('ip', ''), 'started_ms': item['started_ms']}


def database(group_id):
    db = sqlite3.connect(DATA / 'traffic.sqlite3', timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,time_ms INTEGER,machine TEXT,run TEXT,kind TEXT,body TEXT)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(events)')}
    additions = {'group_id': 'TEXT', 'instance': 'TEXT', 'record_key': 'TEXT', 'valid': 'INTEGER DEFAULT 1', 'start_ms': 'INTEGER', 'end_ms': 'INTEGER', 'rx_bytes': 'INTEGER', 'tx_bytes': 'INTEGER', 'ip': 'TEXT', 'started_ms': 'INTEGER', 'received_ms': 'INTEGER', 'indexed_ms': 'INTEGER'}
    for name, kind in additions.items():
        if name not in columns:
            db.execute('ALTER TABLE events ADD COLUMN ' + name + ' ' + kind)
    db.execute('CREATE TABLE IF NOT EXISTS writes(time_ms INTEGER,records INTEGER,bytes INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS provider_readings(time_ms INTEGER PRIMARY KEY,body TEXT,conflict INTEGER DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS members(instance TEXT PRIMARY KEY,machine TEXT,first_seen_ms INTEGER,last_seen_ms INTEGER,removed_ms INTEGER,connected_ms INTEGER)')
    if 'group_id' not in {r[1] for r in db.execute('PRAGMA table_info(members)')}:
        db.execute('ALTER TABLE members ADD COLUMN group_id TEXT')
    for name, columns in [('event_kind_time', 'kind,time_ms'), ('event_group_kind_time', 'group_id,kind,time_ms'), ('event_machine_time', 'group_id,machine,time_ms'), ('event_run_kind_time', 'group_id,run,kind,time_ms'), ('event_record_key', 'record_key')]:
        db.execute('CREATE INDEX IF NOT EXISTS ' + name + ' ON events(' + columns + ')')
    db.execute('CREATE INDEX IF NOT EXISTS event_summary ON events(group_id,valid,received_ms)')
    db.execute("CREATE INDEX IF NOT EXISTS event_totals ON events(group_id,run,rx_bytes,tx_bytes,start_ms,end_ms) WHERE kind='minute' AND valid=1")
    db.execute("CREATE INDEX IF NOT EXISTS event_chart ON events(group_id,end_ms,start_ms,rx_bytes,tx_bytes,instance,machine,ip,run,json_extract(body,'$.partial'),json_extract(body,'$.final')) WHERE kind='minute' AND valid=1")
    db.commit()
    return db


def insert(db, rows, group_id, received_ms):
    added = size = rejected = 0
    with db:
        for row in rows:
            raw = row.get('text_log', '')
            if not raw.startswith('RP_MON '):
                continue
            try:
                item = json.loads(raw[7:])
                value = fields(item, group_id)
                labels = row.get('resource', {}).get('labels', {})
                if labels.get('project_name', PROJECT) != PROJECT or labels.get('container_group_name', GROUP_NAME) != GROUP_NAME:
                    raise ValueError('Wrong log scope')
                if item['time_ms'] > received_ms + 10000:
                    value['valid'] = 0
                indexed = millis(row.get('receive_time'))
            except (ValueError, KeyError, TypeError):
                rejected += 1
                continue
            body = json.dumps(item, separators=(',', ':'))
            identity = hashlib.sha256(body.encode()).hexdigest()
            if value['record_key'] and db.execute('SELECT 1 FROM events WHERE record_key=? AND id<>?', (value['record_key'], identity)).fetchone():
                db.execute('UPDATE events SET valid=0 WHERE record_key=?', (value['record_key'],))
                value['valid'] = 0
            record = {'id': identity, 'time_ms': item['time_ms'], 'machine': item['machine'], 'run': item['run'], 'kind': item['kind'], 'body': body, **value, 'received_ms': received_ms, 'indexed_ms': indexed}
            cursor = db.execute('INSERT OR IGNORE INTO events (' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
            if cursor.rowcount:
                added += 1
                size += len(raw.encode())
        if added:
            db.execute('INSERT INTO writes VALUES(?,?,?)', (received_ms, added, size))
    return added, rejected


def snapshot(db, deployment, group, instances, now, state, errors):
    gid = deployment['id']
    inventory_ms = state.get('inventory_ms')
    inventory_fresh = inventory_ms is not None and 0 <= now - inventory_ms <= FRESH_MS and not errors.get('inventory')
    totals = db.execute("SELECT sum(rx_bytes+tx_bytes),min(start_ms),count(*) FROM events WHERE group_id=? AND kind='minute' AND valid=1", (gid,)).fetchone()
    end = now // 60000 * 60000
    rows = db.execute("SELECT start_ms,end_ms,rx_bytes,tx_bytes,instance,machine,ip,run,json_extract(body,'$.partial'),json_extract(body,'$.final') FROM events WHERE group_id=? AND kind='minute' AND valid=1 AND end_ms>=? ORDER BY end_ms", (gid, end - 1439 * 60000)).fetchall()
    records = []
    for start, stop, rx, tx, instance, machine, ip, run, partial, final in rows:
        record = {'start_ms': start, 'end_ms': stop, 'rx': rx, 'tx': tx, 'bucket_ms': None, 'instance': instance, 'machine': machine, 'ip': ip}
        bucket = round(start / 60000) * 60000
        if not partial and not final and 59000 <= stop - start <= 61000 and abs(start - bucket) <= 1000:
            record.update(start_ms=bucket, end_ms=bucket + 60000, bucket_ms=bucket)
        records.append(record)
    chart = bucket_minutes(records, end - 1439 * 60000, end)
    desired = group.get('replicas', deployment['replicas'])
    for point in chart:
        point['coverage_complete'] &= desired > 0 and point['count'] >= desired
        point['partial'] |= point['count'] < desired or point['minute_ms'] + 60000 > now
    workers = []
    for instance in instances:
        minimum = (millis(instance.get('update_time')) or 0) - 60000
        row = db.execute("SELECT body,received_ms,indexed_ms FROM events WHERE group_id=? AND machine=? AND instance=? AND valid=1 AND kind IN ('start','tick','minute') ORDER BY time_ms DESC,rowid DESC LIMIT 1", (gid, instance['machine_id'], instance['id'])).fetchone()
        if row is None or now - json.loads(row[0])['time_ms'] > FRESH_MS:
            fallback = db.execute("SELECT body,received_ms,indexed_ms FROM events WHERE group_id=? AND machine=? AND valid=1 AND kind IN ('start','tick','minute') AND started_ms>=? ORDER BY time_ms DESC,rowid DESC LIMIT 1", (gid, instance['machine_id'], minimum)).fetchone()
            if fallback and (row is None or json.loads(fallback[0])['time_ms'] > json.loads(row[0])['time_ms']):
                row = fallback
        item = json.loads(row[0]) if row else {}
        if item and item.get('instance') != instance['id'] and (item.get('started_ms', 0) < minimum or item['time_ms'] < minimum + 60000):
            item = {}
        age = now - item['time_ms'] if item else None
        fresh = bool(item and inventory_fresh and -10000 <= age <= FRESH_MS)
        run = item.get('run')
        measured = db.execute("SELECT sum(rx_bytes+tx_bytes),sum(end_ms-start_ms),count(*) FROM events WHERE group_id=? AND run=? AND kind='minute' AND valid=1", (gid, run)).fetchone() if run else (None, None, 0)
        last = db.execute("SELECT body,valid FROM events WHERE group_id=? AND run=? AND kind='minute' ORDER BY time_ms DESC,rowid DESC LIMIT 1", (gid, run)).fetchone() if run else None
        last_valid = bool(last and last[1])
        last = json.loads(last[0]) if last else {}
        network_fresh = fresh and last_valid and -10000 <= now - last['end_ms'] <= 150000 and not last.get('final') and instance['state'] == 'running'
        diagnostic = item.get('diagnostics')
        diagnostics_fresh = fresh and isinstance(diagnostic, dict) and -10000 <= now - diagnostic.get('time_ms', 0) <= FRESH_MS
        phase = item.get('phase') if fresh else None
        status = instance['state'] if inventory_fresh and instance['state'] != 'running' else phase or 'unverified'
        policy = dict(item.get('policy') or {})
        if not diagnostics_fresh or not (-10000 <= now - policy.get('checked_ms', 0) <= FRESH_MS):
            policy = {'state': 'unverified'}
        workers.append({'current': True, 'machine': instance['machine_id'], 'salad_instance': instance['id'], 'instance': item.get('instance'), 'run': run, 'ip': item.get('ip'), 'revision': item.get('revision'), 'phase': phase, 'status': status, 'reason': item.get('reason', '') if fresh else 'Waiting for fresh native evidence', 'salad_state': instance['state'], 'source_ms': item.get('time_ms'), 'source_age_ms': age, 'received_ms': row[1] if row and item else None, 'indexed_ms': row[2] if row and item else None, 'started_ms': item.get('started_ms'), 'last_end_ms': last.get('end_ms'), 'last_mb_per_minute': (last['rx_bytes'] + last['tx_bytes']) / 1e6 * 60000 / last['elapsed_ms'] if network_fresh else None, 'total_mb': measured[0] / 1e6 if measured[0] is not None else None, 'recorded_ms': measured[1], 'minutes': measured[2], 'native_fresh': fresh, 'network_fresh': network_fresh, 'diagnostics_fresh': diagnostics_fresh, 'policy': policy, 'diagnostics': diagnostic if diagnostics_fresh else None, 'device_id': item.get('device_id')})
    with db:
        if inventory_fresh:
            for worker in workers:
                db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?) ON CONFLICT(instance) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,removed_ms=NULL,connected_ms=coalesce(members.connected_ms,excluded.connected_ms),group_id=excluded.group_id', (worker['salad_instance'], worker['machine'], inventory_ms, inventory_ms, None, now if worker['phase'] == 'connected' and worker['salad_state'] == 'running' else None, gid))
            current = [w['salad_instance'] for w in workers]
            db.execute('UPDATE members SET removed_ms=? WHERE group_id=? AND removed_ms IS NULL' + (' AND instance NOT IN (' + ','.join('?' for _ in current) + ')' if current else ''), (inventory_ms, gid, *current))
    recovery = []
    seen = set()
    for raw in db.execute("SELECT body FROM events WHERE group_id=? AND kind='recovery' AND valid=1 ORDER BY time_ms DESC LIMIT 100", (gid,)):
        item = json.loads(raw[0])
        request = item.get('request_id') or item['run']
        if request in seen:
            continue
        seen.add(request)
        member = db.execute('SELECT removed_ms FROM members WHERE instance=?', (item['instance'],)).fetchone()
        accepted = item.get('http_status') in (200, 204)
        gone = bool(inventory_fresh and not any(w['machine'] == item['machine'] for w in workers))
        result = 'old_instance_removed' if accepted and gone else 'accepted' if accepted else 'request_failed' if item.get('action') == 'failed' or item.get('http_status') is not None else 'requested'
        recovery.append({k: item.get(k) for k in ('time_ms', 'machine', 'instance', 'ip', 'reason', 'http_status', 'attempt')} | {'request_id': request, 'result': result, 'removed_ms': member[0] if member else None})
        if len(recovery) == 5:
            break
    while len(workers) < desired:
        workers.append({'current': True, 'machine': None, 'instance': None, 'salad_state': 'allocating' if inventory_fresh else 'unverified', 'status': 'allocating' if inventory_fresh else 'unverified', 'phase': None, 'policy': {'state': 'unverified'}, 'reason': 'Awaiting Salad allocation' if inventory_fresh else 'Inventory unavailable'})
    provider = None
    provider_error = None
    provider_path = DATA / 'provider.json'
    if provider_path.exists():
        try:
            raw = json.loads(provider_path.read_text())
            provider = {k: raw[k] for k in ('observed_ms', 'today_traffic_text', 'today_earned_text', 'traffic_text', 'earned_text', 'source') if k in raw}
            if not integer(provider.get('observed_ms')) or provider['observed_ms'] > now + 10000:
                raise ValueError('Invalid provider observation time')
            body = json.dumps(provider, sort_keys=True, separators=(',', ':'))
            with db:
                old = db.execute('SELECT body FROM provider_readings WHERE time_ms=?', (provider['observed_ms'],)).fetchone()
                if old and old[0] != body:
                    db.execute('UPDATE provider_readings SET conflict=1 WHERE time_ms=?', (provider['observed_ms'],))
                    provider_error = 'Conflicting provider values at one observation time'
                else:
                    db.execute('INSERT OR IGNORE INTO provider_readings(time_ms,body) VALUES(?,?)', (provider['observed_ms'], body))
            if db.execute('SELECT conflict FROM provider_readings WHERE time_ms=?', (provider['observed_ms'],)).fetchone()[0]:
                provider_error = 'Conflicting provider values at one observation time'
        except (OSError, ValueError, KeyError, TypeError):
            provider_error = 'Provider reading could not be verified'
            provider = None
    if provider and provider_error:
        provider['error'] = provider_error
    summary = db.execute('SELECT count(*),sum(valid=0),sum(received_ms IS NULL) FROM events WHERE group_id=?', (gid,)).fetchone()
    return {'generated_ms': now, 'inventory_ms': inventory_ms, 'last_log_success_ms': state.get('last_log_success_ms'), 'source_ms': max((w.get('source_ms') or 0 for w in workers), default=0), 'organization': ORGANIZATION, 'group': GROUP_NAME, 'group_id': gid, 'desired': desired, 'running': sum(i['state'] == 'running' for i in instances) if inventory_fresh else None, 'connected': sum(bool(w.get('native_fresh') and w['phase'] == 'connected' and w['salad_state'] == 'running') for w in workers) if inventory_fresh and not errors else None, 'workers': workers, 'provider': provider, 'provider_error': provider_error, 'provider_record_count': db.execute('SELECT count(*) FROM provider_readings WHERE conflict=0').fetchone()[0], 'chart_minutes': chart, 'total_network_mb': totals[0] / 1e6 if totals[0] is not None else None, 'test_started_ms': totals[1], 'record_count': summary[0], 'invalid_records': summary[1] or 0, 'legacy_receipt_unknown': summary[2] or 0, 'database_bytes': sum(p.stat().st_size for p in DATA.glob('traffic.sqlite3*')), 'saved_bytes_last_minute': db.execute('SELECT coalesce(sum(bytes),0) FROM writes WHERE time_ms>=?', (now - 60000,)).fetchone()[0], 'backfill_through_ms': state.get('through_ms'), 'error': '; '.join(errors.values()) or None, 'errors': errors, 'recent_recovery': recovery, 'recent_members': [dict(zip(('instance', 'machine', 'first_seen_ms', 'last_seen_ms', 'removed_ms', 'connected_ms', 'group_id'), row)) for row in db.execute('SELECT * FROM members ORDER BY first_seen_ms DESC LIMIT 20')], 'traffic_scope':'application', 'traffic_note':'Repocket application traffic is unavailable; shared container RX/TX is excluded.' if totals[0] is None else None, 'measurement':'Repocket application RX + TX; shared container totals and provider credits are separate.', 'provider_url': 'https://repocket.com/dashboard/share-internet', 'salad_url': 'https://portal.salad.com/organizations/'+ORGANIZATION+GROUP}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    DATA.mkdir(parents=True, exist_ok=True)
    lock = (DATA / 'observer.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    deployment = json.loads((DATA / 'deployment.json').read_text())
    db = database(deployment['id'])
    path = DATA / ('cursor.'+deployment['id']+'.json')
    state = json.loads(path.read_text()) if path.exists() else {}
    beginning = millis(deployment.get('start_requested_at', deployment['created_at']))
    state.setdefault('through_ms', beginning)
    state.setdefault('last_reconcile_ms', int(time.time() * 1000))
    group = {'replicas': deployment['replicas']}
    instances = []
    while True:
        began = time.monotonic()
        now = int(time.time() * 1000)
        errors = {}
        identity_changed = False
        try:
            group = api(GROUP)
            if group['id'] != deployment['id']:
                identity_changed = True
                raise RuntimeError('Repocket deployment identity changed')
            instances = api(GROUP + '/instances')['instances']
            state['inventory_ms'] = int(time.time() * 1000)
        except Exception as error:
            errors['inventory'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        try:
            if identity_changed:
                raise RuntimeError('Log capture paused: deployment identity changed')
            cursor = state['through_ms']
            end = min(now, cursor + 600000)
            rows = fetch(max(beginning, cursor - 60000), end)
            if end < now:
                rows += fetch(max(beginning, now - 60000), now)
            if state.get('reconcile_cursor') is None and now - state['last_reconcile_ms'] >= 300000:
                state['reconcile_cursor'], state['reconcile_end'] = max(beginning, now - 3600000), now
            reconcile_stop = None
            if state.get('reconcile_cursor') is not None:
                reconcile_stop = min(state['reconcile_end'], state['reconcile_cursor'] + 60000)
                rows += fetch(state['reconcile_cursor'], reconcile_stop)
            final = final_window(state, beginning, now)
            if final:
                rows += fetch(*final)
            added, rejected = insert(db, rows, deployment['id'], int(time.time() * 1000))
            state.update(through_ms=end, last_log_success_ms=int(time.time() * 1000))
            if final:
                state['final_through_ms'] = final[1]
            if reconcile_stop is not None:
                state['reconcile_cursor'] = reconcile_stop
                if reconcile_stop >= state['reconcile_end']:
                    state.update(reconcile_cursor=None, reconcile_end=None, last_reconcile_ms=now)
        except Exception as error:
            added = rejected = 0
            errors['logs'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        save(path, state)
        value = snapshot(db, deployment, group, instances, int(time.time() * 1000), state, errors)
        value['rejected_last_poll'] = rejected
        save(DATA / 'view.json', value)
        print(json.dumps({'checked_at': stamp(value['generated_ms']), 'running': value['running'], 'connected': value['connected'], 'inserted': added, 'rejected': rejected, 'network_mb': value['total_network_mb'], 'errors': errors}), flush=True)
        if args.once:
            return 1 if errors else 0
        time.sleep(max(1, (1 if state['through_ms'] < now - 60000 else 30) - (time.monotonic() - began)))


if __name__ == '__main__':
    raise SystemExit(main())
