"""Saved evaluator index: one writer, read-only indexed API, no cloud actions."""
from __future__ import annotations
import copy
import collections
import ipaddress
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from dashboard.ip_evidence import EvidenceIndex,SERVICES,NATIVE_FRESH_MS,connection,number
from dashboard.ip_enrichment import Enrichment
from dashboard.ip_metrics import MetricReader
from dashboard.ip_reputation import VERSION,SCALE,EXCLUDED_SCORE,RULES,score_ips
from dashboard.ip_allocations import allocation_id,current_allocations,fresh_workers
from dashboard.ip_decisions import decide as allocation_decision,expire_decision,supervisor_feedback
from dashboard.ip_shadow import expire_shadow

INDEX_PATH=Path('/var/lib/salad-ip-evidence/evidence.sqlite3')
RATE_FIELDS=('current_rate','rate_source_ms','rate_received_ms','rate_expires_ms')
RATE_REFRESH_SERVICES=('grass',)


class Publisher:
    def __init__(self,root,path=INDEX_PATH):
        self.root=Path(root);self.path=Path(path);self.metrics=MetricReader(self.root);self.enrichment=Enrichment(self.root)
        self.db=sqlite3.connect(self.path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,body TEXT);
          CREATE TABLE IF NOT EXISTS ips(ip TEXT PRIMARY KEY,current_state TEXT,expires_ms INTEGER,country TEXT,first_seen_ms INTEGER,last_seen_ms INTEGER,runs INTEGER,score REAL,rate REAL,peak REAL,consistency REAL,cost REAL,body TEXT);
          CREATE INDEX IF NOT EXISTS ips_score ON ips(current_state,score DESC,last_seen_ms DESC);
          CREATE INDEX IF NOT EXISTS ips_seen ON ips(last_seen_ms DESC);
          CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,ip TEXT,service TEXT,last_seen_ms INTEGER,body TEXT);
          CREATE INDEX IF NOT EXISTS runs_ip_time ON runs(ip,last_seen_ms DESC);
          CREATE TABLE IF NOT EXISTS apps(ip TEXT,service TEXT,duration_ms REAL,total_mb REAL,rate REAL,score REAL,peak REAL,consistency REAL,current_state TEXT,expires_ms INTEGER,body TEXT,PRIMARY KEY(ip,service));
          CREATE INDEX IF NOT EXISTS apps_service_score ON apps(service,score DESC);
          CREATE TABLE IF NOT EXISTS allocations(id TEXT PRIMARY KEY,ip TEXT,current_state TEXT,expires_ms INTEGER,country TEXT,first_seen_ms INTEGER,last_seen_ms INTEGER,runs INTEGER,score REAL,rate REAL,peak REAL,consistency REAL,cost REAL,body TEXT);
          CREATE INDEX IF NOT EXISTS allocations_score ON allocations(score DESC,rate DESC);
          CREATE TABLE IF NOT EXISTS allocation_apps(id TEXT,service TEXT,ip TEXT,duration_ms REAL,total_mb REAL,rate REAL,score REAL,peak REAL,consistency REAL,current_state TEXT,expires_ms INTEGER,body TEXT,PRIMARY KEY(id,service));
          CREATE INDEX IF NOT EXISTS allocation_apps_service ON allocation_apps(service,score DESC);
          CREATE TABLE IF NOT EXISTS allocation_history(id TEXT PRIMARY KEY,first_seen_ms INTEGER,last_seen_ms INTEGER,body TEXT);
          CREATE TABLE IF NOT EXISTS run_allocations(run_id TEXT PRIMARY KEY,allocation_id TEXT);
          CREATE INDEX IF NOT EXISTS run_allocation ON run_allocations(allocation_id,run_id);
          CREATE TABLE IF NOT EXISTS traffic(run_id TEXT,minute_ms INTEGER,mb REAL,coverage_ms REAL,estimated INTEGER,start_ms INTEGER,end_ms INTEGER,PRIMARY KEY(run_id,minute_ms));
          CREATE TABLE IF NOT EXISTS episode_scores(episode TEXT,version TEXT,input_hash TEXT,body TEXT,PRIMARY KEY(episode,version,input_hash));''')
    def publish(self,evidence):
        value=copy.deepcopy(evidence.value)
        if not value:raise ValueError('No evidence snapshot to publish')
        now=int(time.time()*1000);runs=value['runs'];metrics={};metric_errors=[];sources={};by_ip={}
        allocations={}
        for service in SERVICES:
            try:evidence.source(service,now,sources)
            except (OSError,ValueError):sources[service]={'workers':[],'error':'Published inventory unavailable'}
        current_workers=list(fresh_workers(sources,now));bundle_apps={}
        for w in current_workers:bundle_apps.setdefault(allocation_id(w),set()).add(w['service'])
        for r in runs:
            r['allocation_id']=allocation_id(r)
            bundle_apps.setdefault(r['allocation_id'],set()).add(r['service'])
            allocations.setdefault((r['service'],r.get('group_id') or r.get('group'),r['instance']),[]).append(r)
        for r in runs:
            r['current'],r['cloud_state']=evidence.current(r,now,sources)
            r['cloud_membership']=r['current']
            r['shared_counter']=len(bundle_apps[r['allocation_id']])>1
            r['measurement_scope']='container_interface'
            later=allocations[(r['service'],r.get('group_id') or r.get('group'),r['instance'])]
            if r['native'].get('final') or any(x['first_seen_ms']>=r['last_seen_ms'] and x['run']!=r['run'] for x in later):r['current']=False
            if r['current'] is True and r['cloud_state']!='running':r['current']=False
            r['_evaluation_end_ms']=now if r['current'] is True else r['last_seen_ms']
            r['performance_expires_ms']=r['last_seen_ms']+NATIVE_FRESH_MS[r['service']]
        self.metrics.honeygain=evidence.honeygain
        for service in SERVICES:
            selected=[r for r in runs if r['service']==service]
            try:metrics.update(self.metrics.read(service,selected))
            except (OSError,ValueError,KeyError,sqlite3.Error) as exc:metric_errors.append(dict(service=service,error='Transfer detail unavailable: '+type(exc).__name__))
        current_ips=set()
        for worker in current_workers:
            try:
                if worker.get('ip'):current_ips.add(str(ipaddress.ip_address(worker['ip'])))
            except ValueError:pass
        remembered={ip:json.loads(body) if body else {} for ip,body in self.db.execute("SELECT ip,json_extract(body,'$.ranking.outcomes') FROM ips")}
        registry={ip:json.loads(body) for ip,body in self.db.execute("SELECT ip,body FROM ips WHERE json_extract(body,'$.registry_only')=1")}
        registry={ip:body for ip,body in registry.items() if set(body.get('registry_services',[]))&set(SERVICES)}
        extra_ips=current_ips|set(registry)
        geo,health=self.enrichment.read(runs,now,extra_ips)
        try:
            with (self.root/'ip-checks/verified.json').open('rb') as stream:raw_checks=stream.read(4000001)
            checks=json.loads(raw_checks) if len(raw_checks)<=4000000 else []
            if not isinstance(checks,list):checks=[]
        except (OSError,ValueError):checks=[]
        ip_scores=score_ips(runs,metrics,geo,health,now,remembered,extra_ips,checks)
        for r in runs:
            r['metrics']={k:v for k,v in metrics.get(r['id'],{}).items() if k not in ('hours','reference_hours')}
            # Whole-IP reputation is recalculated from retained facts. A returning
            # allocation does not replace it with a fresh episode's neutral score.
            r['ranking']=ip_scores[r['ip']]
            r['watch_evidence']=health.get(r['id'])
            by_ip.setdefault(r['ip'],[]).append(r)
        def app_state(ar):
            current=[r for r in ar if r['current'] is True]
            known=True if current else None if any(r['current'] is None for r in ar) else False
            candidates=current or ar
            chosen=max(candidates,key=lambda r:(r['ranking']['confidence'],r['last_seen_ms']))
            ranking=dict(chosen['ranking'],scope_service=chosen['service'],episode_id=chosen['id'],episode_first_ms=chosen['first_seen_ms'],episode_last_ms=chosen['last_seen_ms'],scope='whole_ip_history')
            expires=None;rate=None;rate_expires=None;at=None;rate_received=None
            if current:
                expires=min((next((w.get('inventory_ms') for w in sources[r['service']].get('workers',[]) if w.get('id')==r['instance'] and w.get('machine')==r['machine'] and (not w.get('group_id') or w['group_id']==r['group_id'])),None) or 0)+(120000 if r['service'] in ('hg','ps') else 90000) for r in current)
                source=sources.get(chosen['service'],{})
                worker=next((w for w in source.get('workers',[]) if w.get('id')==chosen['instance'] and w.get('machine')==chosen['machine'] and w.get('ip')==chosen['ip'] and (not w.get('group_id') or w['group_id']==chosen['group_id'])),{})
                at=number(worker.get('sample_ms'))
                rate_expires=number(worker.get('rate_expires_ms'));rate_received=number(worker.get('rate_received_ms'))
                if at is not None and rate_expires is not None and at<=now<=rate_expires and worker.get('salad_state')=='running':rate=number(worker.get('mb_per_minute'))
            return dict(current=known,current_expires_ms=expires,current_rate=rate,rate_source_ms=at,rate_received_ms=rate_received,rate_expires_ms=rate_expires,performance_expires_ms=chosen['performance_expires_ms'],ranking=ranking,metrics=chosen['metrics'],watch_evidence=chosen['watch_evidence'],evaluation_window=dict(start_ms=chosen['metrics'].get('window_start_ms'),end_ms=chosen['metrics'].get('window_end_ms'),service=chosen['service'],episode=chosen['id']))
        payloads=[];app_rows=[];flagged_count=ranked_count=0
        for row in value['ips']:
            row['id']='ip:'+row['ip']
            rr=by_ip[row['ip']]
            app_values={service:app_state([r for r in rr if r['service']==service]) for service in {r['service'] for r in rr}}
            current_apps=[a for a in app_values.values() if a['current'] is True]
            candidates=current_apps or list(app_values.values())
            selected=max(candidates,key=lambda a:(a['ranking']['scope_service']=='grass' if current_apps else False,a['ranking']['confidence'],a['ranking']['episode_last_ms']))
            row.update(selected);row.update(geo.get(row['ip'],{}))
            row['current']=True if current_apps else None if any(a['current'] is None for a in app_values.values()) else False
            row['view']='current' if row['current'] is True else 'unverified' if row['current'] is None else 'historical'
            ranking=row['ranking'];flags=row.get('flags',[])
            if row.get('address_scope')!='public':
                ranking.update(score=0,lane='flagged',reasons=['Non-public or reserved address is not a usable public egress candidate.'])
            elif any(f.get('fresh') or f.get('owner_hold') for f in flags):
                ranking.update(score=EXCLUDED_SCORE,lane='flagged',reasons=['Excluded by owner policy: retained, attributed datacenter evidence. Inspect its supplier and observation date; this is not a universal provider rejection.'])
            elif flags:
                ranking.update(score=None,lane='flagged',reasons=['Historical hosting flag retained; source classification is stale and needs review.'])
            row['economic_status']='IP earnings unavailable; operational score only'
            row['recommendation']='Avoid on return' if any(f.get('fresh') or f.get('owner_hold') for f in flags) else 'Remember'
            row['recommendation_reason']='Use the recorded exclusion if this IP returns.' if row['recommendation']=='Avoid on return' else 'Use dated performance when this IP appears again; current allocation checks remain separate.'
            if ranking['lane']=='flagged':flagged_count+=1
            elif ranking['lane']!='unseen':ranked_count+=1
            for a in row['apps']:
                a.update(app_values[a['service']])
                if ranking['lane']=='flagged':a['ranking']=ranking
                state='current' if a['current'] is True else 'unverified' if a['current'] is None else 'historical'
                app_rows.append((row['ip'],a['service'],a['duration_ms'],a['total_mb'],a['current_rate'] if a['current'] is True else a['average_mb_per_minute'],a['ranking']['score'],a['metrics'].get('peak_mb_per_minute'),next((d['value'] for d in a['ranking']['dimensions'] if d['name']=='Repeated observed transfer'),None),state,a['current_expires_ms'],json.dumps(a,separators=(',',':'))))
            payloads.append((row['ip'],row['view'],row['current_expires_ms'],','.join(row['countries']),row['first_seen_ms'],row['last_seen_ms'],row['runs'],ranking['score'],row['current_rate'],row['metrics'].get('peak_mb_per_minute'),next((d['value'] for d in ranking['dimensions'] if d['name']=='Repeated observed transfer'),None),row['cost']['estimated_usd'],json.dumps(row,separators=(',',':'),allow_nan=False)))
        profiles={r['ip']:r for r in value['ips']}
        for ip in extra_ips-set(profiles):
            old=registry.get(ip,{})
            workers=[w for w in current_workers if w.get('ip')==ip]
            known=bool(workers);expires=max((w['inventory_ms']+(120000 if w['service'] in ('hg','ps') else 90000) for w in workers),default=None)
            row=dict(id='ip:'+ip,ip=ip,current=True if known else None,current_expires_ms=expires,view='current' if known else 'unverified',
                first_seen_ms=old.get('first_seen_ms',now),last_seen_ms=now if known else old.get('last_seen_ms',now),runs=0,apps=[],metrics={},
                countries=sorted({w['country'] for w in workers if w.get('country')}) or old.get('countries',[]),ranking=ip_scores[ip],
                cost={'estimated_usd':None,'partial':True,'intervals':[]},current_rate=None,registry_only=True,
                registry_services=sorted({w['service'] for w in workers}|(set(old.get('registry_services',[]))&set(SERVICES))),
                observation_basis='IP observed in allocation inventory; no retained native episode yet',
                address_scope='public' if ipaddress.ip_address(ip).is_global else 'non-public',**geo.get(ip,{}))
            profiles[ip]=row
            if row['ranking']['lane']=='flagged':flagged_count+=1
            elif row['ranking']['lane']!='unseen':ranked_count+=1
            payloads.append((ip,row['view'],expires,','.join(row['countries']),row['first_seen_ms'],row['last_seen_ms'],0,row['ranking']['score'],None,None,None,None,json.dumps(row,separators=(',',':'),allow_nan=False)))
        containers=current_allocations(runs,sources,profiles,app_state,now,workers=current_workers)
        supervisor,checked_allocations=supervisor_feedback(self.root,now)
        for container in containers:
            if container.get('ip') in ip_scores:
                container['ranking']=copy.deepcopy(ip_scores[container['ip']])
                for app in container['apps']:
                    if app.get('ip') in ip_scores:app['ranking']=copy.deepcopy(ip_scores[app['ip']])
                if container.get('allocation_exclusion') and container['ranking'].get('excluded'):
                    container['allocation_exclusion']['score']=EXCLUDED_SCORE
            container['allocation_current']=True
            container['decision']=allocation_decision(container,now)
            container['recommendation']=container['decision']['label']
            container['recommendation_reason']=container['decision']['reason']
            checked=checked_allocations.get(container['allocation_id'])
            if checked and all(checked.get(k)==container.get(k) for k in ('group_id','instance','machine')):
                container['supervisor']=checked
        current_ip_set={ip for row in containers for ip in row['ips']}
        status=dict(generated_ms=now,evaluation_generated_ms=now,supervisor=supervisor,source_index_ms=value['generated_ms'],coverage=value['coverage'],build_ms=value['build_ms'],metric_errors=metric_errors,
            all_ips=len(payloads),summary=dict(current_containers=len(containers),current_ips=len(current_ip_set),decisions=dict(collections.Counter(c['decision']['action'] for c in containers)),scored_containers=sum(c['ranking']['lane'] not in ('unseen','flagged') for c in containers),pending_containers=sum(c['ranking']['lane']=='unseen' for c in containers),historical_ips=sum(p[1]=='historical' for p in payloads),unverified_ips=sum(p[1]=='unverified' for p in payloads),scored_ips=ranked_count,flagged_ips=flagged_count,pending_ips=len(payloads)-ranked_count-flagged_count),
            scoring=dict(version=VERSION,scope='whole_ip_history',scale=SCALE,new_ip_score=0,excluded_score=EXCLUDED_SCORE,rules=RULES,replacement_enabled=False,economic_replacement_enabled=False,
                basis='Signed reputation from retained IP outcomes, sustained transfer and public classification. New IPs start at zero; missing data and low traffic are not failures.'),
            traffic_basis='Decimal MB, validated native RX+TX. Cross-app traffic is not summed. Scoring uses dated episode windows.',
            cost_basis='Partial observed-span cost, deduplicated by allocation; ambiguous cross-IP overlap excluded.')
        previous_containers=[json.loads(x[0]) for x in self.db.execute('SELECT body FROM allocations')]
        active_ids={c['id'] for c in containers}
        ended_ids={r['allocation_id'] for r in runs if r.get('cloud_membership') is False}
        with self.db:
            self.db.execute('DELETE FROM ips');self.db.execute('DELETE FROM apps');self.db.execute('DELETE FROM runs')
            self.db.executemany('INSERT INTO ips VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',payloads)
            self.db.executemany('INSERT INTO apps VALUES(?,?,?,?,?,?,?,?,?,?,?)',app_rows)
            self.db.executemany('INSERT INTO runs VALUES(?,?,?,?,?)',[(r['id'],r['ip'],r['service'],r['last_seen_ms'],json.dumps(r,separators=(',',':'),allow_nan=False)) for r in runs])
            self.db.execute('DELETE FROM allocations');self.db.execute('DELETE FROM allocation_apps');self.db.execute('DELETE FROM run_allocations')
            self.db.executemany('INSERT INTO run_allocations VALUES(?,?)',[(r['id'],r['allocation_id']) for r in runs])
            for old in previous_containers:
                if old['id'] in active_ids:continue
                old.update(current=False if old['id'] in ended_ids else None,current_rate=None,current_expires_ms=None,availability='not_current_in_monitored_inventory' if old['id'] in ended_ids else 'unverified')
                old['allocation_current']=old['current']
                expire_decision(old,now)
                old['ranking']=dict(old['ranking'],scope='last recorded allocation observation')
                for app in old['apps']:app.update(current=old['current'],current_rate=None,current_expires_ms=None)
                self.db.execute('UPDATE allocation_history SET body=? WHERE id=?',(json.dumps(old,separators=(',',':')),old['id']))
            for c in containers:
                m=c['metrics'];rank=c['ranking'];consistency=next((d['value'] for d in rank['dimensions'] if d['name']=='Repeated observed transfer'),None)
                body=json.dumps(c,separators=(',',':'),allow_nan=False)
                self.db.execute('INSERT INTO allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(c['id'],c['ip'],'current',c['current_expires_ms'],','.join(c['countries']),c['first_seen_ms'],c['last_seen_ms'],c['runs'],rank['score'],c['current_rate'],m.get('peak_mb_per_minute'),consistency,c['cost']['estimated_usd'],body))
                self.db.execute('INSERT INTO allocation_history VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET first_seen_ms=MIN(first_seen_ms,excluded.first_seen_ms),last_seen_ms=MAX(last_seen_ms,excluded.last_seen_ms),body=excluded.body',(c['id'],c['first_seen_ms'],c['last_seen_ms'],body))
                for a in c['apps']:
                    self.db.execute('INSERT INTO allocation_apps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(c['id'],a['service'],a.get('ip'),a.get('duration_ms'),a.get('total_mb'),a.get('current_rate'),a['ranking']['score'],a.get('metrics',{}).get('peak_mb_per_minute'),next((d['value'] for d in a['ranking']['dimensions'] if d['name']=='Repeated observed transfer'),None),'current',a['current_expires_ms'],json.dumps(a,separators=(',',':'),allow_nan=False)))
            for service,cache in self.metrics.cache.items():
                if not cache.get('last_changed'):continue
                cutoff=cache.get('last_cutoff',0)
                for r in (r for r in runs if r['service']==service):
                    self.db.execute('DELETE FROM traffic WHERE run_id=? AND minute_ms>=?',(r['id'],cutoff))
                    self.db.executemany('INSERT OR REPLACE INTO traffic VALUES(?,?,?,?,?,?,?)',[(r['id'],at,v[0],v[1],int(v[2]),v[3],v[4]) for at,v in cache['buckets'].get(r['id'],{}).items() if at>=cutoff])
                    self.db.execute('DELETE FROM traffic WHERE run_id=? AND minute_ms<?',(r['id'],r.get('_evaluation_end_ms',r['last_seen_ms'])-72*3600000))
            self.db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',('current',json.dumps(status,separators=(',',':'))))
            # Publish the full ranking and current-slot projection together.
            # Readers must not briefly lose pending/unavailable slots between
            # the full commit and the next observation pass.
            status=self.refresh_observations(evidence) or status
        return status

    def refresh_rates(self,evidence):
        """Project fresh Grass rates between full evaluations; no history reads.

        Only already evaluated allocations/runs can receive a rate. Keep the
        recorder's source/receipt expiry and all score/traffic inputs unchanged.
        """
        now=int(time.time()*1000)
        saved=self.db.execute("SELECT body FROM state WHERE key='current'").fetchone()
        if not saved:return None
        status=json.loads(saved[0]);evaluated=status.get('evaluation_generated_ms',status['generated_ms'])
        if not 0<=now-evaluated<=180000:return None
        rows=[json.loads(r[0]) for r in self.db.execute("SELECT body FROM allocations WHERE current_state='current' AND expires_ms>=?",(now,))]
        sources={}
        for service in {a['service'] for row in rows for a in row['apps'] if a.get('run') and a['service'] in RATE_REFRESH_SERVICES}:
            try:evidence.source(service,now,sources)
            except (OSError,ValueError):sources[service]={'workers':[]}
        workers=collections.defaultdict(list)
        for w in fresh_workers(sources,now):
            run=str(w['id'])+'/'+str(w['observer_ms']) if w.get('observer_ms') is not None else w.get('run')
            if run:workers[(allocation_id(w),w['service'],run,w.get('ip'))].append(w)
        projected={};updates=[]
        def project(target):
            episode=(target.get('evaluation_window') or {}).get('episode')
            rate=projected.get(episode)
            if target.get('current') is True and rate is not None:target.update(rate)
        for row in rows:
            original=json.dumps(row,separators=(',',':'))
            for app in row['apps']:
                if app.get('current') is not True or not app.get('run') or app['service'] not in RATE_REFRESH_SERVICES:continue
                candidates=workers.get((row['id'],app['service'],app['run'],app.get('ip')),[])
                rate={k:app.get(k) for k in RATE_FIELDS};rate['current_rate']=None
                if len(candidates)==1 and now<=(app.get('current_expires_ms') or 0):
                    w=candidates[0];at=number(w.get('sample_ms'));received=number(w.get('rate_received_ms'));expires=number(w.get('rate_expires_ms'))
                    # A regressed feed must not replace a later native reading.
                    if at is not None and at>=(number(app.get('rate_source_ms')) or 0):
                        rate.update(rate_source_ms=at,rate_received_ms=received,rate_expires_ms=expires)
                        value=number(w.get('mb_per_minute'))
                        if expires is not None and at<=now<=expires and (received is None or received<=now) and value is not None and value>=0:
                            rate['current_rate']=value
                app.update(rate)
                episode=(app.get('evaluation_window') or {}).get('episode')
                if episode:projected[episode]=rate
            project(row)
            body=json.dumps(row,separators=(',',':'),allow_nan=False)
            if body!=original:updates.append((row,body))
        if not updates:return None
        with self.db:
            for row,body in updates:
                self.db.execute('UPDATE allocations SET rate=?,body=? WHERE id=?',(row.get('current_rate'),body,row['id']))
                for app in row['apps']:
                    self.db.execute('UPDATE allocation_apps SET rate=?,body=? WHERE id=? AND service=?',(app.get('current_rate'),json.dumps(app,separators=(',',':'),allow_nan=False),row['id'],app['service']))
            # IP projections select the exact evaluated episode, including when
            # several containers share an IP. Never borrow another run's rate.
            for ip in {ip for row,_ in updates for ip in row.get('ips',[]) if ip}:
                saved=self.db.execute('SELECT body FROM ips WHERE ip=?',(ip,)).fetchone()
                if not saved:continue
                row=json.loads(saved[0]);project(row)
                for app in row['apps']:
                    project(app)
                    if app.get('current') is True:
                        self.db.execute('UPDATE apps SET rate=?,body=? WHERE ip=? AND service=?',(app.get('current_rate'),json.dumps(app,separators=(',',':'),allow_nan=False),ip,app['service']))
                self.db.execute('UPDATE ips SET rate=?,body=? WHERE ip=?',(row.get('current_rate'),json.dumps(row,separators=(',',':'),allow_nan=False),ip))
            # A rate publication wakes existing revision consumers, while the
            # full evaluation's original clock still bounds evaluator freshness.
            status.update(generated_ms=now,evaluation_generated_ms=evaluated,rate_refreshed_ms=now)
            self.db.execute("UPDATE state SET body=? WHERE key='current'",(json.dumps(status,separators=(',',':')),))
        return status

    def refresh_observations(self,evidence):
        """Refresh bounded recorded current inputs and the inert simulation."""
        from dashboard.ip_current import refresh
        saved=self.db.execute("SELECT body FROM state WHERE key='current'").fetchone()
        if not saved:return None
        return refresh(self.db,self.root,evidence,int(time.time()*1000),json.loads(saved[0]))


def select_app(row,service):
    app=next((a for a in row['apps'] if a['service']==service),None)
    if app is None:raise ValueError('App has no matching evidence')
    if row.get('allocation_id'):
        row.setdefault('allocation_ip',row.get('ip'))
        row.setdefault('allocation_ranking',copy.deepcopy(row.get('ranking')))
        row.setdefault('allocation_current_expires_ms',row.get('current_expires_ms'))
    for k in ('current','current_expires_ms','current_rate','rate_source_ms','rate_received_ms','rate_expires_ms','performance_expires_ms','ranking','metrics','watch_evidence','evaluation_window'):
        row[k]=copy.deepcopy(app.get(k))
    if app.get('ip') is not None:row['ip']=app['ip']
    row['runs']=app['runs']
    if row.get('allocation_exclusion'):row['ranking']=dict(row['ranking'],**row['allocation_exclusion'])


def expire(row,now,stale,evaluation_ms=None):
    for app in row.get('apps',[]):
        if app.get('current') is True and (stale or (app.get('current_expires_ms') or 0)<now or (app.get('rate_expires_ms') or 0)<now):
            app['current_rate']=None
        if app.get('current') is True and (stale or (app.get('current_expires_ms') or 0)<now):app['current']=None
    if row.get('current') is True and (stale or (row.get('current_expires_ms') or 0)<now):
        row['current']=None;row['current_rate']=None
        if row['ranking'].get('lane')!='flagged' and row['ranking'].get('version')!=VERSION:
            row['ranking']=dict(row['ranking'],score=None,lane='unknown',confidence=0,reasons=['Current evaluator or inventory evidence has expired.'])
    if row.get('current') is True:
        if (row.get('rate_expires_ms') or 0)<now:row['current_rate']=None
        if (row.get('performance_expires_ms') or 0)<now and row['ranking'].get('lane')!='flagged' and row['ranking'].get('version')!=VERSION:
            row['ranking']=dict(row['ranking'],score=None,lane='unknown',confidence=0,reasons=['Traffic recording has expired; cloud membership remains separately verified.'])
    expire_decision(row,now,stale)
    expire_shadow(row,now,stale,evaluation_ms)


class PublishedEvidence:
    def __init__(self,root,path=INDEX_PATH):
        self.root=Path(root);self.path=Path(path);self.details=EvidenceIndex(self.root,None,None,None)
    def revision(self):
        if not self.path.exists():return dict(revision=None,state='waiting')
        with closing(connection(self.path)) as db:
            row=db.execute("SELECT json_extract(body,'$.generated_ms') FROM state WHERE key='current'").fetchone()
        return dict(revision=row[0] if row else None,state='ready' if row else 'waiting')

    def snapshot(self,query):
        now=int(time.time()*1000)
        if not self.path.exists():return dict(state='loading',rows=[],total=0,generated_ms=None,error=None,refreshing=True)
        with closing(connection(self.path)) as db:
            db.execute('BEGIN');saved=db.execute("SELECT body FROM state WHERE key='current'").fetchone()
            if not saved:return dict(state='loading',rows=[],total=0,generated_ms=None,error=None,refreshing=True)
            status=json.loads(saved[0]);stale=not 0<=now-status.get('evaluation_generated_ms',status['generated_ms'])<=180000
            live_now=now if not stale else now+10**12
            if 'summary' in status:
                summary=status['summary']
                summary['current_containers']=db.execute("SELECT COUNT(*) FROM allocations WHERE current_state='current' AND expires_ms>=?",(live_now,)).fetchone()[0]
                summary['observed_containers']=db.execute('SELECT COUNT(*) FROM allocations').fetchone()[0]
                summary['unavailable_containers']=summary['observed_containers']-summary['current_containers']
                summary['current_ips']=db.execute("SELECT COUNT(DISTINCT ip) FROM allocation_apps WHERE current_state='current' AND expires_ms>=? AND ip IS NOT NULL",(live_now,)).fetchone()[0]
                summary['unverified_ips']=db.execute("SELECT COUNT(*) FROM ips WHERE current_state='unverified' OR current_state='current' AND expires_ms<?",(live_now,)).fetchone()[0]
            service=query.get('service',['all'])[0]
            if service!='all' and service not in SERVICES:raise ValueError('Unknown evidence service')
            allocation=query.get('allocation_id',[''])[0];ip=None if allocation else query.get('ip',[''])[0]
            if allocation or ip:
                if allocation:
                    found=db.execute('SELECT body FROM allocations WHERE id=?',(allocation,)).fetchone()
                    current_hit=found is not None
                    found=found or db.execute('SELECT body FROM allocation_history WHERE id=?',(allocation,)).fetchone()
                    if not found:raise ValueError('Unknown container allocation')
                    row=json.loads(found[0]);params=[allocation]
                    if not current_hit:row.update(current=False if row.get('availability')=='not_current_in_monitored_inventory' else None,current_rate=None,current_expires_ms=None)
                    row['allocation_current']=True if current_hit else row['current']
                    sql='SELECT r.body FROM runs r JOIN run_allocations x ON x.run_id=r.id WHERE x.allocation_id=?'
                    scope=allocation+':'+service
                else:
                    ip=str(ipaddress.ip_address(ip));found=db.execute('SELECT body FROM ips WHERE ip=?',(ip,)).fetchone()
                    if not found:raise ValueError('IP has no retained evidence')
                    row=json.loads(found[0]);params=[ip]
                    sql='SELECT r.body FROM runs r WHERE r.ip=?'
                    scope='ip:'+ip+':'+service
                if service!='all':sql+=' AND r.service=?';params.append(service)
                runs=[json.loads(r[0]) for r in db.execute(sql+' ORDER BY r.last_seen_ms DESC',params)]
                if allocation:
                    if service!='all':select_app(row,service)
                    if not current_hit:row.update(current=False if row.get('availability')=='not_current_in_monitored_inventory' else None,current_rate=None,current_expires_ms=None)
                offset=int(query.get('run_offset',['0'])[0])
                if not 0<=offset<=100000:raise ValueError('Invalid run page')
                if not allocation and service!='all':select_app(row,service)
                detail=dict(generated_ms=status['generated_ms'],ips=[row],runs=runs,detail_scope=scope)
                if allocation:detail['selected_row']=row
                result=self.details.detail(detail,row.get('ip') if allocation else ip,now,offset)
                expire(result['row'],now,stale,status.get('evaluation_generated_ms',status['generated_ms']))
                result['error']=('Evaluator is stale; current state unverified.' if allocation else 'Evaluator is stale; current state is unverified.') if stale else None
                return result
            state=query.get('state',['all'])[0];sort=query.get('sort',['score'])[0];direction=query.get('direction',['desc'])[0]
            if state not in ('all','current','historical','unverified'):raise ValueError('Unknown evidence state')
            sorts={'score':'i.score','ip':'i.ip','country':'i.country','first_seen_ms':'i.first_seen_ms','last_seen_ms':'i.last_seen_ms','runs':'i.runs','cost':'i.cost','peak':'i.peak','consistency':'i.consistency','rate':'i.rate','duration_ms':'a.duration_ms' if service!='all' else 'NULL','total_mb':'a.total_mb' if service!='all' else 'NULL'}
            if sort not in sorts or direction not in ('asc','desc'):raise ValueError('Unknown evidence sort')
            offset=int(query.get('offset',['0'])[0]);limit=int(query.get('limit',['100'])[0])
            if not 0<=offset<=100000 or not 1<=limit<=100:raise ValueError('Invalid evidence page')
            current_view=state=='current' # Retain unavailable monitored slots; expire their claims below.
            table='allocations' if current_view else 'ips'
            app_table='allocation_apps' if current_view else 'apps'
            key='id' if current_view else 'ip'
            where=[];params=[];join=''
            if service!='all':
                join=' JOIN '+app_table+' a ON a.'+key+'=i.'+key+' AND a.service=?';params.append(service)
                if sort in ('score','rate','peak','consistency'):sorts[sort]='a.'+sort
                if sort=='score':sorts[sort]="CASE WHEN json_extract(i.body,'$.ranking.lane')='flagged' THEN i.score ELSE a.score END"
            alias='a' if service!='all' else 'i'
            if state=='historical':
                where.append('NOT EXISTS(SELECT 1 FROM allocation_apps live_app WHERE live_app.ip=i.ip AND live_app.expires_ms>=?)');params.append(live_now)
                availability=query.get('availability',['all'])[0]
                if availability not in ('all','ended','unknown'):raise ValueError('Unknown availability filter')
                if availability=='ended':where.append(alias+".current_state='historical'")
                elif availability=='unknown':where.append(alias+".current_state<>'historical'")
            elif state=='unverified':where.append(alias+".current_state='unverified' OR "+alias+".current_state='current' AND "+alias+'.expires_ms<?');params.append(live_now)
            search=query.get('q',[''])[0][:200]
            if search:
                where.append("(i.body LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM runs r WHERE r.ip=i.ip AND r.body LIKE ? ESCAPE '\\'))")
                pattern='%'+search.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%';params.extend([pattern,pattern])
            clause=' WHERE '+' AND '.join('('+w+')' for w in where) if where else ''
            base=' FROM '+table+' i'+join+clause;field=sorts[sort]
            if sort=='score':
                field="CASE WHEN COALESCE(json_extract("+alias+".body,'$.ranking.version'),'')!='"+VERSION+"' AND "+alias+".current_state='current' AND ("+alias+'.expires_ms<'+str(live_now)+" OR COALESCE(json_extract("+alias+".body,'$.performance_expires_ms'),0)<"+str(now)+') THEN NULL ELSE '+field+' END'
            flagged="(json_extract(i.body,'$.ranking.lane')='flagged')," if sort=='score' else ''
            order=' ORDER BY '+flagged+'('+field+' IS NULL),'+field+' '+direction+',i.rate DESC,i.ip'
            total=db.execute('SELECT COUNT(*)'+base,params).fetchone()[0]
            rows=[json.loads(r[0]) for r in db.execute('SELECT i.body'+base+order+' LIMIT ? OFFSET ?',params+[limit,offset])]
            for rank,row in enumerate(rows,offset+1):
                row['rank']=rank
                if service!='all':
                    select_app(row,service)
                expire(row,now,stale,status.get('evaluation_generated_ms',status['generated_ms']))
            return dict(status,state='ready',served_ms=now,refreshing=False,error='Evaluator is stale; current state is unverified.' if stale else None,rows=rows,total=total,offset=offset,limit=limit,
                shadow_page_counts=dict(collections.Counter(r['shadow']['action'] for r in rows if isinstance(r.get('shadow'),dict))))

    def traffic(self,query):
        allocation=query.get('allocation_id',[''])[0];service=query.get('service',[''])[0]
        if service not in SERVICES:raise ValueError('Unknown traffic service')
        hours=int(query.get('hours',['24'])[0])
        if hours not in (1,6,12,24):raise ValueError('Invalid traffic range')
        now=int(time.time()*1000)
        with closing(connection(self.path)) as db:
            db.execute('BEGIN')
            found=db.execute('SELECT body FROM allocations WHERE id=?',(allocation,)).fetchone()
            current_hit=found is not None
            found=found or db.execute('SELECT body FROM allocation_history WHERE id=?',(allocation,)).fetchone()
            if not found:raise ValueError('Unknown container allocation')
            row=json.loads(found[0]);app=next((a for a in row['apps'] if a['service']==service),None)
            if app is None:raise ValueError('App was not observed in this allocation')
            ip=query.get('ip',[app.get('ip')])[0]
            if ip is not None:ip=str(ipaddress.ip_address(ip))
            runs=[dict(r) for r in db.execute('SELECT r.id,r.last_seen_ms FROM runs r JOIN run_allocations x ON x.run_id=r.id WHERE x.allocation_id=? AND r.service=? AND r.ip IS ?',(allocation,service,ip))]
            latest=max((r['last_seen_ms'] for r in runs),default=None)
            live=current_hit and app.get('ip')==ip and (row.get('current_expires_ms') or 0)>=now
            anchor=now if live or latest is None else latest;last=anchor//60000*60000;first=last-(hours*60-1)*60000;context=last-(72*60-1)*60000
            values=collections.defaultdict(list)
            for run in runs:
                for point in db.execute('SELECT * FROM traffic WHERE run_id=? AND minute_ms>=? AND minute_ms<=? ORDER BY minute_ms',(run['id'],context,last)):
                    values[point['minute_ms']].append(dict(point))
            minutes=[]
            for at,parts in sorted(values.items()):
                end=None;overlap=False
                for part in sorted(parts,key=lambda p:p['start_ms']):
                    if end is not None and part['start_ms']<end:overlap=True
                    end=max(end or part['end_ms'],part['end_ms'])
                covered=sum(p['coverage_ms'] for p in parts);overlap|=covered>60000
                amount=None if overlap else sum(p['mb'] for p in parts)
                minutes.append(dict(minute_ms=at,mb=amount,count=1,partial=overlap or covered<60000,estimated=any(p['estimated'] for p in parts),coverage_complete=not overlap and covered>=60000))
            selected=[p['mb'] for p in minutes if first<=p['minute_ms']<=last and p['mb'] is not None]
            return dict(allocation_id=allocation,service=service,ip=ip,test=dict(id='allocation',label='Current container' if live else 'Historical container'),minutes=minutes,start_ms=first,end_ms=last+60000,source_ms=latest,total_mb=sum(selected) if selected else None,anchored=not live,measurement_scope='container_interface',shared_counter=row.get('shared_counter'),basis='Container-visible interface traffic from one selected app reporter. Overlapping run intervals are excluded; this is not isolated per-app or provider-credited traffic.')
