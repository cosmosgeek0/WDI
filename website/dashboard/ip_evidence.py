"""Read-only, provider-scoped IP evidence. No cloud/provider clients or ledger writes.

Traffic reuses the dashboard's identity-aware history reader. An independent
background cache keeps cold history work off both request and Grass feed locks.
Only changed ledgers are rebuilt; the minute-scale cache exposes its own age.
"""
from __future__ import annotations
import copy
import hashlib
import ipaddress
import json
import math
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

SERVICES = {'hg': ('Honeygain', 'fleet-tests'), 'ps': ('PacketStream', 'packetstream'),
 'proxyrack': ('Proxyrack', 'proxyrack'), 'rp': ('Repocket', 'repocket'),
 'earnfm': ('EarnFM', 'earnfm'), 'tm': ('Traffic Monetizer', 'traffmonetizer'),
 'pawns': ('Pawns.app', 'pawns'), 'grass': ('Grass', 'grass')}
REFRESH_SECONDS = 60
NATIVE_FRESH_MS={'hg':120000,'ps':120000,'grass':90000,'tm':45000,'earnfm':90000,'rp':90000,'proxyrack':90000,'pawns':90000}
RUN_FIELDS = 'group group_id instance machine run ip country first_seen_ms last_seen_ms total_mb duration_ms samples partial estimated'.split()


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def safe_text(value):
    if not isinstance(value, (str, int, float, bool)):
        return None
    value = str(value)
    value = re.sub(r'https?://\S+', '[URL omitted]', value)
    value = re.sub(r'(?i)(bearer\s+|(?:token|password|secret|api[_-]?key|authorization|cookie)\s*[:=]\s*)\S+', r'\1[redacted]', value)
    value = re.sub(r'[A-Za-z0-9_+-]+@[A-Za-z0-9_.-]+', '[account omitted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_.-]+', '[assertion omitted]', value)
    return value[:400]


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else {}


def connection(path):
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def normalize_view(service,raw,now):
    source=dict(raw)
    def stamp(value):
        if number(value) is not None:return value
        if not isinstance(value,str):return None
        try:return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)
        except ValueError:return None
    inventory=stamp(source.get('inventory_ms',source.get('generated_ms')))
    if service=='ps':inventory=stamp(source.get('last_success_at') or source.get('checked_at'))
    if service=='hg':inventory=stamp(source.get('status_observed_ms'))
    source['inventory_ms']=inventory
    workers=[]
    for original in source.get('workers',source.get('nodes',[])):
        w=dict(original);network=w.get('network') or {}
        w['id']=next((w[k] for k in ('id','instance','instance_id','salad_instance','runtime_instance') if w.get(k)),w.get('run') if service=='hg' else None)
        w['machine']=w.get('machine',w.get('machine_id'))
        w['group_id']=w.get('group_id') or source.get('group_id')
        w['salad_state']=w.get('salad_state',w.get('state'))
        w['inventory_ms']=stamp(w.get('inventory_ms',inventory))
        sample=w.get('sample_ms');rate=w.get('mb_per_minute');age_limit=NATIVE_FRESH_MS[service]
        if service=='ps':
            sample=network.get('time_ms');rate=network.get('bytes_per_second')
            rate=rate*60/1e6 if number(rate) is not None and network.get('available') else None
            if network.get('observer') is not None:w['run']=str(w['id'])+'/'+str(network['observer'])
        elif service=='proxyrack':sample=w.get('last_sample_ms')
        elif service=='rp':sample=w.get('last_end_ms');rate=w.get('last_mb_per_minute')
        elif service=='earnfm':rate=w.get('kb_per_second')*.06 if number(w.get('kb_per_second')) is not None else None
        elif service=='tm':sample=w.get('time_ms');rate=w.get('raw_mb_per_minute')
        elif service=='hg':
            interval=w.get('network_interval') or {};rate=interval.get('per_second')
            rate=rate*60/1e6 if number(rate) is not None else None
        sample=stamp(sample);expires=sample+age_limit if sample is not None else None
        if number(w.get('sample_expires_ms')) is not None:expires=min(expires or w['sample_expires_ms'],w['sample_expires_ms'])
        receipt=stamp(w.get('recorded_ms') if service in ('grass','earnfm','proxyrack') else network.get('received_ms') if service=='ps' else w.get('received_ms'))
        if receipt is not None and expires is not None:expires=min(expires,receipt+age_limit)
        if service=='hg' and number(w.get('received_age_ms')) is not None and expires is not None:expires=min(expires,now+age_limit-w['received_age_ms'])
        w.update(sample_ms=sample,mb_per_minute=number(rate),rate_expires_ms=expires,rate_received_ms=receipt)
        workers.append(w)
    source['workers']=workers
    return source


def identity(row):
    return tuple(row.get(k) for k in ('service','group_id','group','instance','machine','run','ip'))


def signature(paths):
    values = []
    for path in paths:
        try:
            stat = path.stat()
            values.append((str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            values.append((str(path), None))
    return tuple(values)


def cause(state, reason):
    """Describe observed causes, never infer a bad IP from an empty byte window."""
    text = (str(state or '')+' '+str(reason or '')).lower().replace('_',' ')
    if any(x in text for x in ('invalid credential', 'invalid password', 'unauthorized', 'account banned', 'account restricted', 'authorization error')):
        return 'account restriction'
    if any(x in text for x in ('ip rejected', 'ip overused', 'overused', 'ip blocked', 'vpn', 'proxy detected', 'residential required', 'ip not allowed', 'network unusable', 'duplicate ip', '1 client per ip')):
        return 'provider IP rejection'
    if any(x in text for x in ('crash', 'process exit', 'not running', 'process missing', 'client exited', 'bootstrap failed', 'startup failed')):
        return 'process / startup failure'
    if any(x in text for x in ('telemetry', 'recording', 'stale', 'missing sample', 'awaiting sample')):
        return 'missing recording'
    if any(x in text for x in ('disconnected', 'connection failed', 'network unreachable', 'connection stalled')):
        return 'connection failure; shared cause unproven'
    return 'unclassified' if reason else 'no classified failure'


def observed_state(sample, service):
    health = sample.get('health') or {}
    health = health if isinstance(health, dict) else {}
    state = health.get('state') or sample.get('state') or sample.get('client_status')
    reason = health.get('error') or sample.get('error') or sample.get('reason')
    if reason in ('none', 'None', ''):
        reason = None
    at = health.get('observed_ms') or sample.get('source_ms') or sample.get('end_ms') or sample.get('time_ms')
    result = dict(state=safe_text(state), reason=safe_text(reason), observed_ms=number(at),
                  received_ms=number(sample.get('recorded_ms') or sample.get('received_ms') or sample.get('log_time_ms')),
                  category=cause(state,reason), final=sample.get('final') in (True,1),
                  device=safe_text(sample.get('uuid') or health.get('device_id') or sample.get('device')))
    provider = health.get('provider_listing') or {}
    if isinstance(provider,dict):
        result['provider'] = {k:provider.get(k) for k in ('observed_ms','confirmed','connected','ip_match') if type(provider.get(k)) in (int,float,bool)}
    return result


def native_record(db, row):
    """Last retained sample within the exact run/IP segment, never the newest IP alone."""
    service, run, ip = row['service'],row.get('run'),row.get('ip')
    first,last = row['first_seen_ms'],row['last_seen_ms']
    if service == 'grass':
        item = db.execute("SELECT body FROM samples WHERE run=? AND source_ms BETWEEN ? AND ? AND json_extract(body,'$.ip')=? ORDER BY source_ms DESC LIMIT 1", (run,first,last,ip)).fetchone()
        if item:
            sample = json.loads(item[0])
            at=sample.get('source_ms')
            if db.execute('SELECT 1 FROM conflicts WHERE run=? AND source_ms=? LIMIT 1',(run,at)).fetchone():
                return {'state':'Conflicting native sample','source_ms':at,'error':'Recording identity conflict; health unavailable'}
            return sample
    elif service in ('ps','proxyrack','pawns'):
        observer=int(run.rsplit('/',1)[1])
        item=db.execute('SELECT * FROM minutes WHERE group_id=? AND instance=? AND observer=? AND ip=? AND end_ms BETWEEN ? AND ? ORDER BY seq DESC LIMIT 1',(row['group_id'],row['instance'],observer,ip,first,last)).fetchone()
        return dict(item) if item else {}
    elif service == 'rp':
        item=db.execute("SELECT body,received_ms FROM events WHERE group_id IS ? AND instance IS ? AND machine IS ? AND run IS ? AND ip=? AND kind='minute' AND end_ms BETWEEN ? AND ? ORDER BY end_ms DESC LIMIT 1",(row['group_id'],row['instance'],row['machine'],run,ip,first,last)).fetchone()
        if item:
            return dict(json.loads(item[0]),received_ms=item[1])
    elif service in ('earnfm','tm'):
        at='end_ms' if service=='earnfm' else 'time_ms'
        item=db.execute('SELECT * FROM samples WHERE run=? AND instance IS ? AND machine IS ? AND ip=? AND '+at+' BETWEEN ? AND ? ORDER BY seq DESC LIMIT 1',(run,row['instance'],row['machine'],ip,first,last)).fetchone()
        return dict(item) if item else {}
    return {}


def merge_intervals(intervals):
    merged=[]
    for left,right in sorted((a,b) for a,b in intervals if number(a) is not None and number(b) is not None and b>a):
        if merged and left<=merged[-1][1]:
            merged[-1][1]=max(right,merged[-1][1])
        else:
            merged.append([left,right])
    return merged


def allocation_cost(rows, cost_nodes, price, all_rows=None):
    """Deduplicate one billed allocation/time across app rows; do not add full-run costs to each IP."""
    allocations={}
    for row in rows:
        key=(row.get('group_id'),row.get('instance'),row.get('machine'))
        if all(key):
            allocations.setdefault(key,[]).append((row['first_seen_ms'],row['last_seen_ms']))
    amounts=[];intervals=[];seen=set()
    for node in cost_nodes:
        key=(node.get('group_id'),node.get('instance'),node.get('machine'))
        if key not in allocations or key in seen:
            continue
        seen.add(key)
        slices=[]
        for a,b in merge_intervals(allocations[key]):
            for period in node.get('intervals',[]):
                left,right=max(a,period['start_ms']),min(b,period['end_ms'])
                if right>left:
                    slices.append((left,right))
        # A span crossing another IP is ambiguous; exclude the overlap from
        # both IP allocations instead of counting the billed time twice.
        others=merge_intervals([(r['first_seen_ms'],r['last_seen_ms']) for r in all_rows or []
            if (r.get('group_id'),r.get('instance'),r.get('machine'))==key and r['ip']!=rows[0]['ip']])
        unambiguous=[]
        for left,right in merge_intervals(slices):
            parts=[(left,right)]
            for a,b in others:
                parts=[piece for x,y in parts for piece in ((x,min(y,a)),(max(x,b),y)) if piece[1]>piece[0]]
            unambiguous.extend(parts)
        for left,right in unambiguous:
            amount=price(left,right,node.get('resources')) if not node.get('error') else None
            intervals.append(dict(group_id=key[0],instance=key[1],machine=key[2],start_ms=left,end_ms=right,estimated_usd=amount))
            if amount is not None:
                amounts.append(amount)
    return dict(estimated_usd=sum(amounts) if amounts else None,partial=True,allocations=len(seen),intervals=intervals,
        basis='Estimated running cost inside observed IP spans; one group/instance allocation counted once across apps. Gaps, IP handovers and missing billing periods remain uncertain. Not an invoice or complete machine cost.')


class EvidenceIndex:
    def __init__(self, root, history, costs, price, honeygain=None):
        self.root=Path(root);self.history=history;self.costs=costs;self.price=price;self.honeygain=honeygain
        self.lock=threading.Lock();self.refresh_lock=threading.Lock()
        self.services={};self.value=None;self.checked=0;self.building=False;self.error=None
        self.detail_cache={}

    def paths(self, service):
        folder=self.root/SERVICES[service][1]
        if service=='hg':
            return sorted(folder.glob('*.json*'))+sorted(folder.glob('archives/*/*.json*'))
        path=folder/('minutes.sqlite3' if service=='ps' else 'traffic.sqlite3')
        return [path,Path(str(path)+'-wal')]+sorted(folder.glob('*.json'))

    def service_rows(self, service):
        result=self.history(service,'current')
        results=[result];unique={}
        # Current multi-group sources can include archived groups already. Exact
        # identity deduplication also handles HG archive copies of the same test.
        included_groups={r.get('group_id') for r in result['nodes']}
        for test in result['tests']:
            if test['id']!='current' and (service=='hg' or test['id'] not in included_groups):
                results.append(self.history(service,test['id']))
        for value in results:
            for original in value['nodes']:
                if not original.get('ip'):
                    continue
                try:
                    ip=str(ipaddress.ip_address(original['ip']))
                except ValueError:
                    continue
                row={k:original.get(k) for k in RUN_FIELDS}
                row.update(service=service,ip=ip,test=value['test']['id'],test_label=value['test']['label'],history_current=original.get('current'),history_inventory_ms=value.get('inventory_ms'))
                key=identity(row)
                if key not in unique:
                    unique[key]=row
        rows=list(unique.values())
        if service=='hg' and self.honeygain:
            observations={}
            for value in results:
                snapshot=self.honeygain(value['test']['id'])
                for observation in snapshot.get('observations',[]):
                    for worker in observation.get('hg',{}).get('workers',[]):
                        key=(worker.get('group'),worker.get('machine'),worker.get('run'),worker.get('ip'))
                        at=worker.get('sample_ms') or observation['time_ms']
                        if key not in observations or at>observations[key][0]:
                            observations[key]=(at,worker)
            for row in rows:
                entry=observations.get((row['group'],row['machine'],row['run'],row['ip']))
                if entry:
                    at,worker=entry
                    row['native']=observed_state(dict(state=worker.get('client_status'),error=worker.get('error_code'),source_ms=at,device=worker.get('device')),'hg')
                    row['device_credit']=dict(scope='device lifetime',device=safe_text(worker.get('device')),amount=number(worker.get('credits')),unit='Honeygain credits',observed_ms=number(worker.get('credit_observed_ms')),attributed_to_ip=False,note='Dated device total, not credits earned exclusively on this IP. No lifetime amount is assigned to an IP.')
        path=self.root/SERVICES[service][1]/('minutes.sqlite3' if service=='ps' else 'traffic.sqlite3')
        if path.exists():
            with closing(connection(path)) as db:
                db.execute('BEGIN')
                for row in rows:
                    try:
                        row['native']=observed_state(native_record(db,row),service)
                    except (ValueError,TypeError,KeyError,sqlite3.Error):
                        row['native']=dict(state=None,reason='Native detail unavailable for this retained identity',category='missing recording')
        for row in rows:
            row.setdefault('native',dict(state=None,reason=None,category='unclassified'))
            row['id']=hashlib.sha256(json.dumps(identity(row)).encode()).hexdigest()[:24]
            duration=number(row.get('duration_ms'));amount=number(row.get('total_mb'))
            row['average_mb_per_minute']=amount*60000/duration if amount is not None and duration and duration>0 else None
            span=max(0,row['last_seen_ms']-row['first_seen_ms'])
            row['coverage_fraction']=min(1,duration/span) if duration is not None and span>0 else None
        return dict(rows=rows,source_ms=result['source_ms'],inventory_ms=result['inventory_ms'],
                    notes=list(dict.fromkeys(r['coverage_note'] for r in results)),tests=len(results),error=None)

    def refresh(self):
        if not self.refresh_lock.acquire(False):
            return
        self.building=True
        started=time.monotonic()
        try:
            now=int(time.time()*1000);all_rows=[];coverage=[];cost_nodes=[]
            for service,(name,folder) in SERVICES.items():
                stamp=signature(self.paths(service));saved=self.services.get(service)
                if saved is None or saved[0]!=stamp:
                    try:
                        data=self.service_rows(service)
                        self.services[service]=(stamp,data)
                    except Exception as exc:
                        data=dict(rows=copy.deepcopy(saved[1]['rows']) if saved else [],source_ms=saved[1]['source_ms'] if saved else None,
                                  notes=[],tests=None,error='Retained '+name+' reader failed: '+type(exc).__name__)
                else:
                    data=saved[1]
                all_rows.extend(data['rows'])
                coverage.append(dict(service=service,name=name,rows=len(data['rows']),source_ms=data['source_ms'],tests=data['tests'],notes=data['notes'],error=data['error']))
                cost_value=self.costs(service,0,now)
                cost_nodes.extend(cost_value.get('nodes',[]))
                coverage[-1]['cost']=dict(observed_ms=cost_value.get('observed_ms'),partial=cost_value.get('partial',True),errors=[safe_text(e) for e in cost_value.get('errors',[])])
            by_ip={}
            for row in all_rows:
                by_ip.setdefault(row['ip'],{}).setdefault(row['service'],[]).append(row)
            ips=[]
            for ip,services in by_ip.items():
                runs=[row for selected in services.values() for row in selected]
                apps=[]
                for service,selected in services.items():
                    amounts=[r['total_mb'] for r in selected if number(r['total_mb']) is not None]
                    durations=[r['duration_ms'] for r in selected if number(r['duration_ms']) is not None]
                    total=sum(amounts) if amounts else None;duration=sum(durations) if durations else None
                    latest=max(selected,key=lambda r:r['last_seen_ms'])
                    count=len(set((r['group_id'] or r['group'],r['instance'],r['run']) for r in selected if r['run']))
                    apps.append(dict(service=service,name=SERVICES[service][0],runs=count or None,identity_partial=any(not r['run'] or not r['machine'] for r in selected),
                        total_mb=total,duration_ms=duration,average_mb_per_minute=total*60000/duration if total is not None and duration else None,
                        last_seen_ms=latest['last_seen_ms'],native=latest['native'],partial=any(r['partial'] for r in selected),
                        ip_earnings=None,earnings_basis='No provider amount proven to belong exclusively to this dated IP.',
                        categories=sorted(set(r['native']['category'] for r in selected if r['native'].get('category') not in ('no classified failure','unclassified')))))
                countries=sorted(set(r['country'] for r in runs if r.get('country')))
                ips.append(dict(ip=ip,countries=countries,asn=None,network_type=None,
                    geography_basis='Retained country values; some legacy protocols use a saved IP lookup without lookup time. ASN and connection type were not evidenced.',
                    first_seen_ms=min(r['first_seen_ms'] for r in runs),last_seen_ms=max(r['last_seen_ms'] for r in runs),
                    runs=sum(a['runs'] or 0 for a in apps),apps=apps,cost=allocation_cost(runs,cost_nodes,self.price,all_rows),
                    economic_status='Insufficient attributable earnings',recommendation='Observe; no economic replacement decision',
                    address_scope='public' if ipaddress.ip_address(ip).is_global else 'non-public / reserved address; not a proven public egress IP',
                    recommendation_reason='Native health is app-specific. Missing earnings and low traffic do not establish an unprofitable IP.'))
            with self.lock:
                self.value=dict(generated_ms=now,ips=ips,runs=all_rows,coverage=coverage,build_ms=round((time.monotonic()-started)*1000))
                self.error=None;self.checked=time.monotonic();self.detail_cache.clear()
        except Exception as exc:
            self.error='Evidence refresh failed: '+type(exc).__name__
            self.checked=time.monotonic()
        finally:
            self.building=False;self.refresh_lock.release()

    def source(self,service,now,sources):
        if service not in sources:
            folder=self.root/SERVICES[service][1]
            filename='native-census.json' if service=='ps' else 'latest.json' if service=='tm' else 'view.json'
            raw=(self.honeygain('current').get('latest') or {}).get('hg',{}) if service=='hg' and self.honeygain else read_json(folder/filename)
            sources[service]=normalize_view(service,raw,now)
        return sources[service]

    def current(self, run, now, sources=None):
        service=run['service'];sources=sources if sources is not None else {}
        source=self.source(service,now,sources);inventory=source.get('inventory_ms')
        if source.get('poll_error') or source.get('cloud_error') or source.get('error') and service!='grass':return None,None
        limit=120000 if service in ('hg','ps') else 90000
        for worker in source.get('workers',[]):
            if worker.get('id')!=run['instance']:continue
            if worker.get('group_id') and worker['group_id']!=run['group_id']:continue
            if service=='hg' and worker.get('group')!=run.get('group'):continue
            at=worker.get('inventory_ms')
            if number(at) is None or not 0<=now-at<=limit or worker.get('source_error'):return None,None
            if worker.get('ip')!=run['ip'] or worker.get('machine')!=run['machine']:return False,None
            active_run=worker.get('run')
            if worker.get('observer_ms') is not None:active_run=str(worker['id'])+'/'+str(worker['observer_ms'])
            if active_run:
                return (active_run==run['run']),safe_text(worker.get('salad_state'))
            sample=worker.get('sample_ms')
            if run.get('history_current') is True and number(sample) is not None and run['first_seen_ms']<=sample<=run['last_seen_ms']:
                return True,safe_text(worker.get('salad_state'))
            return None,None
        if number(inventory) is None or not 0<=now-inventory<=limit or source.get('error'):return None,None
        return False,None

    def snapshot(self, query):
        now=int(time.time()*1000)
        if not self.building and (self.value is None or time.monotonic()-self.checked>=REFRESH_SECONDS):
            # Mark before starting to prevent thread bursts on simultaneous reads.
            self.building=True
            threading.Thread(target=self.refresh,daemon=True,name='ip-evidence-refresh').start()
        with self.lock:
            value=self.value
        if value is None:
            return dict(state='loading' if not self.error else 'error',generated_ms=None,rows=[],total=0,error=self.error,refreshing=self.building)
        ip=query.get('ip',[''])[0]
        if ip:
            ip=str(ipaddress.ip_address(ip))
            run_offset=int(query.get('run_offset',['0'])[0])
            if not 0<=run_offset<=100000:raise ValueError('Invalid run page')
            return self.detail(value,ip,now,run_offset)
        service=query.get('service',['all'])[0]
        if service!='all' and service not in SERVICES:raise ValueError('Unknown evidence service')
        sort=query.get('sort',['last_seen_ms'])[0];direction=query.get('direction',['desc'])[0]
        if sort not in ('ip','first_seen_ms','last_seen_ms','runs','duration_ms','total_mb','rate','cost'):raise ValueError('Unknown evidence sort')
        if direction not in ('asc','desc'):raise ValueError('Unknown sort direction')
        offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['100'])[0])
        if not 0<=offset<=100000 or not 1<=limit<=100:raise ValueError('Invalid evidence page')
        search=query.get('q',[''])[0][:200].casefold()
        state=query.get('state',['all'])[0]
        if state not in ('all','current','historical','unverified'):raise ValueError('Unknown evidence state')
        rows=[];sources={}
        for original in value['ips']:
            if service!='all' and not any(a['service']==service for a in original['apps']):continue
            if search and search not in json.dumps(original,ensure_ascii=False).casefold() and not any(search in json.dumps(r,ensure_ascii=False).casefold() for r in value['runs'] if r['ip']==original['ip']):continue
            row=copy.deepcopy(original)
            runs=[r for r in value['runs'] if r['ip']==row['ip']]
            currents=[self.current(r,now,sources)[0] for r in runs]
            row['current']=True if True in currents else None if None in currents else False
            if state=='current' and row['current'] is not True:continue
            if state=='historical' and row['current'] is not False:continue
            if state=='unverified' and row['current'] is not None:continue
            rows.append(row)
        def metric(row):
            if sort=='ip':return int(ipaddress.ip_address(row['ip']))
            if sort=='cost':return row['cost']['estimated_usd']
            if sort in ('total_mb','duration_ms','rate'):
                # Cross-app RX+TX can double-count the same shared network.
                # Numeric traffic sorting requires an explicit app filter.
                if service=='all':return None
                return next(a[{'rate':'average_mb_per_minute'}.get(sort,sort)] for a in row['apps'] if a['service']==service)
            return row[sort]
        known=[r for r in rows if metric(r) is not None];unknown=[r for r in rows if metric(r) is None]
        known.sort(key=lambda r:(metric(r),r['ip']),reverse=direction=='desc')
        rows=known+sorted(unknown,key=lambda r:r['ip'])
        return dict(state='ready',generated_ms=value['generated_ms'],served_ms=now,refreshing=self.building,error=self.error,
                    rows=rows[offset:offset+limit],total=len(rows),all_ips=len(value['ips']),offset=offset,limit=limit,
                    coverage=value['coverage'],build_ms=value['build_ms'],scoring='Awaiting owner metrics; no weighted score or replacement enabled.',
                    traffic_basis='Container RX + TX, decimal MB. Rates are averages over recorded valid intervals. App totals are not combined into sold traffic.',
                    cost_basis='Partial estimate over observed IP spans; shared allocations are counted once. Cost recording may be stale.')

    def detail(self,value,ip,now,offset=0):
        selected=value.get('selected_row') or next((r for r in value['ips'] if r['ip']==ip),None)
        if selected is None:raise ValueError('IP has no retained evidence')
        with self.lock:
            cache_key=(value.get('detail_scope') or ip,offset)
            cached=self.detail_cache.get(cache_key)
        if cached and cached[0]==value['generated_ms']:
            result=copy.deepcopy(cached[1])
        else:
            full_runs=sorted((r for r in value['runs'] if value.get('detail_scope') or r['ip']==ip),key=lambda r:r['last_seen_ms'],reverse=True)
            runs=copy.deepcopy(full_runs[offset:offset+50])
            for row in runs:
                row['events']=self.events(row)
                try:
                    row['provider_observations']=self.provider(row)
                except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
                    row['provider_observations']=[dict(scope='unavailable',amount=None,unit=None,observed_ms=None,attributed_to_ip=False,note='Retained provider observation could not be read.')]
            result=dict(state='ready',generated_ms=value['generated_ms'],row=selected,runs=runs,total_runs=len(full_runs),run_offset=offset,has_more=offset+len(runs)<len(full_runs),
                uncertainty=['An IP is a dated lookup key, not a permanent machine identity or an assignable Salad pool.',
                    'Device lifetime earnings remain device-scoped. Account balances and Grass points are never divided among IPs.',
                    'A connection failure does not prove a shared-network cause. Native health does not prove customer demand or earnings.',
                    'Recovery requests and accepted responses are separate from replacement health. Event coverage is bounded and may be incomplete.'])
            with self.lock:
                if len(self.detail_cache)>=40:self.detail_cache.pop(next(iter(self.detail_cache)))
                self.detail_cache[cache_key]=(value['generated_ms'],copy.deepcopy(result))
        sources={}
        for row in result['runs']:
            row['current'],row['cloud_state']=self.current(row,now,sources)
            at=row['native'].get('observed_ms')
            row['native']['fresh']=number(at) is not None and 0<=now-at<=NATIVE_FRESH_MS[row['service']]
        result['served_ms']=now
        return result

    def events(self,row):
        service=row['service'];folder=self.root/SERVICES[service][1]
        path=folder/('minutes.sqlite3' if service=='ps' else 'traffic.sqlite3')
        output=dict(rows=[],coverage='No joined recovery/event ledger available for this protocol.',partial=True)
        if service=='hg':
            path=self.root/'hg-fleet'/'recovery-events.sqlite3'
        if not path.exists():return output
        first,last=row['first_seen_ms'],row['last_seen_ms']
        try:
            with closing(connection(path)) as db:
                if service=='hg':
                    items=db.execute("SELECT event_json FROM recovery_events WHERE group_name=? AND source_time_ms BETWEEN ? AND ? AND json_extract(event_json,'$.source_labels.machine_id')=? ORDER BY source_time_ms DESC LIMIT 101",(row['group'],first,last,row['machine'])).fetchall()
                elif service=='tm':
                    items=db.execute("SELECT payload FROM events WHERE time_ms BETWEEN ? AND ? AND json_extract(payload,'$.instance')=? AND json_extract(payload,'$.machine')=? AND json_extract(payload,'$.observer_start_ms')=? ORDER BY time_ms DESC LIMIT 101",(first,last,row['instance'],row['machine'],int(row['run'].rsplit('/',1)[1]))).fetchall()
                elif service=='grass':
                    items=db.execute('SELECT body FROM events WHERE instance=? AND machine=? AND source_ms BETWEEN ? AND ? ORDER BY source_ms DESC LIMIT 101',(row['instance'],row['machine'],first,last)).fetchall()
                elif service=='proxyrack':
                    items=db.execute('SELECT body FROM pr_events WHERE group_id=? AND instance=? AND machine=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms DESC LIMIT 101',(row['group_id'],row['instance'],row['machine'],first,last)).fetchall()
                elif service=='pawns':
                    items=db.execute("SELECT data FROM pawns_events WHERE group_id=? AND instance=? AND observer=? AND kind<>'PROVIDER' AND time_ms BETWEEN ? AND ? ORDER BY time_ms DESC LIMIT 101",(row['group_id'],row['instance'],int(row['run'].rsplit('/',1)[1]),first,last)).fetchall()
                elif service=='earnfm':
                    items=db.execute('SELECT body FROM events WHERE run=? AND instance=? AND machine=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms DESC LIMIT 101',(row['run'],row['instance'],row['machine'],first,last)).fetchall()
                elif service=='rp':
                    items=db.execute("SELECT body FROM events WHERE group_id IS ? AND instance IS ? AND machine IS ? AND run IS ? AND kind<>'minute' AND time_ms BETWEEN ? AND ? ORDER BY time_ms DESC LIMIT 101",(row['group_id'],row['instance'],row['machine'],row['run'],first,last)).fetchall()
                else:return output
                for item in items[:100]:
                    raw=json.loads(item[0]);raw=raw if isinstance(raw,dict) else {}
                    if service=='hg':
                        raw=dict(raw,event=raw.get('event_type'),source_ms=raw.get('source_time_ms'))
                    # Additional recorded identities must agree; no IP-only joins.
                    if raw.get('group_id') and row.get('group_id') and raw['group_id']!=row['group_id']:continue
                    if raw.get('ip') and raw['ip']!=row['ip']:continue
                    event={k:safe_text(raw.get(k)) for k in ('event','kind','action','reason','state','error')}
                    event.update(time_ms=number(raw.get('source_ms') or raw.get('time_ms')),received_ms=number(raw.get('recorded_ms') or raw.get('received_ms')),
                        http_status=number(raw.get('http_status') or raw.get('http')),category=cause(raw.get('state') or raw.get('kind'),raw.get('reason') or raw.get('error')))
                    output['rows'].append(event)
                output['coverage']='Latest 100 recorded events matching this identity and observed IP time span; events before/after the span are not assigned to this IP.'
                output['truncated']=len(items)>100
        except (ValueError,TypeError,KeyError,sqlite3.Error):
            output['coverage']='Event source could not be joined to this identity.'
        return output

    def provider(self,row):
        """Amounts remain at their provider scope; no IP delta without continuous attribution."""
        service=row['service'];folder=self.root/SERVICES[service][1]
        if service=='hg' and row.get('device_credit'):
            return [row['device_credit']]
        if service=='grass':
            with closing(connection(folder/'traffic.sqlite3')) as db:
                item=db.execute('SELECT body FROM provider ORDER BY observed_ms DESC LIMIT 1').fetchone()
                if item:
                    saved=json.loads(item[0])
                    return [dict(scope='account / finalized epoch',amount=number(saved.get('network_points')),unit='Network Points',
                        observed_ms=number(saved.get('observed_ms')),epoch=safe_text(saved.get('epoch')),attributed_to_ip=False,
                        note='Account observation only, not matched to this run’s account. Collection run is not point ownership. No USD valuation or payout is inferred.')]
        return [dict(scope='unavailable' if service=='hg' else 'account only',amount=None,unit=None,observed_ms=None,
                     attributed_to_ip=False,note='No IP-attributable provider earnings have been established. Existing protocol pages retain separately scoped provider figures.')]
