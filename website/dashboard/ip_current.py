"""Bounded joins from recorded Grass inventory to exact native samples."""
import copy
import json
import sqlite3
from contextlib import closing
from dashboard.ip_allocations import allocation_id,current_allocations,traffic_scope
from dashboard.ip_evidence import connection,observed_state,number
from dashboard.ip_reputation import score_ips
from dashboard.ip_shadow import prepare

def attach_native(root,source,now):
    path=root/'grass/traffic.sqlite3'
    if not path.exists():return
    with closing(connection(path)) as db:
        db.execute('BEGIN')
        for w in source.get('workers',[]):
            w['native_snapshot']={'state':None,'category':'missing recording','binding_verified':False}
            run=w.get('run');at=number(w.get('sample_ms'))
            if not run or at is None:continue
            item=db.execute('SELECT body FROM samples WHERE run=? AND source_ms=?',(run,at)).fetchone()
            if not item:continue
            sample=json.loads(item[0]);received=number(sample.get('recorded_ms'))
            exact=all(sample.get(k)==w.get('id' if k=='instance' else k) for k in ('group_id','instance','machine','ip'))
            exact=exact and sample.get('source_ms')==at and run in (sample.get('run'),str(w.get('group_id'))+'|'+str(sample.get('run')))
            conflict=db.execute('SELECT 1 FROM conflicts WHERE run=? AND source_ms=? LIMIT 1',(run,at)).fetchone()
            if not exact or conflict or sample.get('final') or received is None or received>now:
                w['native_snapshot']['reason']='Conflicting, final, or mismatched recorded sample'
                continue
            n=observed_state(sample,'grass')
            n.update(source_ms=at,received_ms=received,binding_verified=True,run=run)
            provider=(sample.get('health') or {}).get('provider_listing') or {}
            n['provider']={k:provider[k] for k in ('observed_ms','confirmed','connected','ip_match','device_ip_present','source') if k in provider}
            local=(sample.get('health') or {}).get('native') or {}
            n['local']={k:local[k] for k in ('observed_ms','cached_status','status_is_heartbeat','established_tcp','process_identity','store_modified_ms') if k in local}
            w['native_snapshot']=n
            history=db.execute('''SELECT run,source_ms,json_extract(body,'$.health.device_id')
                FROM samples WHERE json_extract(body,'$.group_id')=?
                AND json_extract(body,'$.instance')=? AND json_extract(body,'$.machine')=?
                AND source_ms>? AND source_ms<=? ORDER BY source_ms DESC LIMIT 128''',
                (w.get('group_id'),w.get('id'),w.get('machine'),now-600000,now)).fetchall()
            n['recent_identity_history']=dict(window_start_ms=now-600000,window_end_ms=now,
                runs=len({r[0] for r in history}),devices=len({r[2] for r in history if r[2]}),
                samples=len(history),partial=len(history)==128,scope='same group/instance/machine; not an IP failure',
                first_source_ms=min((r[1] for r in history),default=None),last_source_ms=max((r[1] for r in history),default=None))

def inventory_rows(source,now):
    """Keep each observed slot even when its source or native state is unknown."""
    groups={}
    for w in source.get('workers',[]):
        if not w.get('id'):continue
        groups.setdefault((w.get('group_id'),w['id']),[]).append(w)
    result=[]
    for variants in groups.values():
        newest=max(number(w.get('inventory_ms')) or 0 for w in variants)
        candidates=[w for w in variants if (number(w.get('inventory_ms')) or 0)==newest]
        # Duplicate envelopes collapse. Conflicting identities at one inventory
        # clock stay visible but cannot supply health, rates or a decision.
        signatures={tuple(w.get(k) for k in ('machine','run','ip','sample_ms')) for w in candidates}
        w=copy.deepcopy(candidates[0]);w['instance']=w['id'];at=number(w.get('inventory_ms'))
        valid=at is not None and 0<=now-at<=90000 and not (source.get('poll_error') or source.get('cloud_error') or w.get('source_error'))
        w['inventory_verified']=valid and len(signatures)==1
        if len(signatures)>1:
            w.update(ip=None,run=None,mb_per_minute=None,native_snapshot={'category':'missing recording','reason':'Conflicting inventory identities','binding_verified':False})
        if not w['inventory_verified']:w['mb_per_minute']=None
        result.append(w)
    return result

def refresh(db,root,evidence,now,status):
    evaluation=status.get('evaluation_generated_ms',status['generated_ms'])
    if not 0<=now-evaluation<=180000:return None
    sources={}
    try:
        evidence.source('grass',now,sources)
        source=sources['grass'];attach_native(root,source,now)
    except (OSError,ValueError,sqlite3.Error):source={'workers':[],'cloud_error':'Current recording unavailable'}
    observed=inventory_rows(source,now)
    previous=[json.loads(r[0]) for r in db.execute('SELECT body FROM allocations')]
    existing={r['id']:r for r in previous}
    regressed_allocations=set();inventory=[]
    for w in observed:
        prior=[r for r in previous if r.get('group_id')==w.get('group_id') and r.get('instance')==w['id']]
        if any((number(r.get('inventory_ms')) or 0)>(number(w.get('inventory_ms')) or 0) for r in prior):
            regressed_allocations.update(r['id'] for r in prior)
        else:inventory.append(dict(w,service='grass',instance=w['id']))
    profiles={};runs=[];by_key={}
    for w in inventory:
        key=allocation_id(w);by_key[key]=w
        for item in db.execute('SELECT r.body FROM runs r JOIN run_allocations a ON a.run_id=r.id WHERE a.allocation_id=?',(key,)):
            run=json.loads(item[0]);run['current']=bool(run.get('service')=='grass' and run.get('run')==w.get('run') and run.get('ip')==w.get('ip') and w['inventory_verified'] and w.get('salad_state')=='running')
            runs.append(run)
        ip=w.get('ip')
        if ip:
            p=db.execute('SELECT body FROM ips WHERE ip=?',(ip,)).fetchone()
            if p:profiles[ip]=json.loads(p[0])
    def state(selected):
        run=max(selected,key=lambda r:r['last_seen_ms'])
        return dict(measurement_scope=run.get('measurement_scope','container_interface'),current=True,current_expires_ms=now+90000,current_rate=None,
            performance_expires_ms=run.get('performance_expires_ms'),ranking=run['ranking'],metrics=run.get('metrics',{}),
            watch_evidence=run.get('watch_evidence'),evaluation_window=dict(episode=run['id'],service=run['service'],start_ms=run.get('metrics',{}).get('window_start_ms'),end_ms=run.get('metrics',{}).get('window_end_ms')))
    rows=current_allocations(runs,{},profiles,state,now,workers=inventory)
    for row in rows:
        w=by_key[row['id']];old=existing.get(row['id']);app=row['apps'][0]
        # Preserve peer observations; Grass joining cannot create or reset them.
        if old:
            row['apps'] += [copy.deepcopy(a) for a in old.get('apps',[]) if a['service']!='grass']
        peer_deadlines=[a['current_expires_ms'] for a in row['apps'] if a['service']!='grass' and a.get('current') is True and (number(a.get('current_expires_ms')) or 0)>now]
        membership=True if w['inventory_verified'] or peer_deadlines else None
        row.update(current=membership,allocation_current=membership,
            current_expires_ms=(w.get('inventory_ms') or 0)+90000,inventory_ms=w.get('inventory_ms'),cloud_state=w.get('salad_state'),
            observation_revision=source.get('generated_ms'),observation_source='grass/view.json + exact recorded native sample')
        app.update(current=True if w['inventory_verified'] else None,current_expires_ms=row['current_expires_ms'],run=w.get('run'),
                   native=copy.deepcopy(w.get('native_snapshot') or {'category':'missing recording','binding_verified':False}))
        app['measurement_scope']=app['native'].get('measurement_scope','unavailable')
        row.update(current_expires_ms=max([row['current_expires_ms'],*peer_deadlines]),
            ips=sorted({a['ip'] for a in row['apps'] if a.get('ip')}),**traffic_scope(row['apps'],app))
        app.update(rate_source_ms=w.get('sample_ms'),rate_received_ms=w.get('rate_received_ms'),rate_expires_ms=w.get('rate_expires_ms'),current_rate=None)
        at=number(app['rate_source_ms']);receipt=number(app['rate_received_ms']);deadline=number(app['rate_expires_ms']);rate=number(w.get('mb_per_minute'))
        old_app=next((a for a in (old or {}).get('apps',[]) if a['service']=='grass'),{})
        regressed=old_app.get('run')==w.get('run') and old_app.get('ip')==w.get('ip') and (number(old_app.get('rate_source_ms')) or 0)>(at or 0)
        if app['current'] is True and w.get('salad_state')=='running' and w.get('ip') and app['native'].get('binding_verified') and not regressed and at is not None and receipt is not None and deadline is not None and at<=now<=deadline and receipt<=now and rate is not None and rate>=0:
            app['current_rate']=rate
        for k in ('current_rate','rate_source_ms','rate_received_ms','rate_expires_ms'):row[k]=app[k]
        if w.get('ip') in profiles:
            row['ranking']=copy.deepcopy(profiles[w['ip']]['ranking']);app['ranking']=copy.deepcopy(row['ranking'])
        if old:
            # The legacy live watcher owns its own decision. Preserve only an
            # exact matching old decision; shadow results use their own field.
            decision=old.get('decision')
            same_runs={a['service']:a.get('run') for a in old.get('apps',[])}=={a['service']:a.get('run') for a in row['apps']}
            if decision and same_runs and decision.get('identity')=={k:row.get(k) for k in ('group_id','instance','machine','ip')}:
                row['decision']=decision;row['recommendation']=old.get('recommendation');row['recommendation_reason']=old.get('recommendation_reason')
    active={r['id'] for r in rows}
    source_fresh=number(source.get('inventory_ms')) is not None and 0<=now-source['inventory_ms']<=90000 and not source.get('cloud_error') and not source.get('poll_error')
    departed=[]
    for old in previous:
        if old['id'] in active:continue
        if not any(a['service']=='grass' for a in old.get('apps',[])):rows.append(old);continue
        if not source_fresh or old['id'] in regressed_allocations:
            old.update(current=None,allocation_current=None,current_rate=None)
            for a in old['apps']:
                if a['service']=='grass':a.update(current=None,current_rate=None)
            rows.append(old)
        else:departed.append(old)
    with db:
        shadow=prepare(db,rows,now,evaluation,status.get('supervisor'))
        for old in departed:
            old.update(current=False,allocation_current=False,current_rate=None,current_expires_ms=None,availability='not_current_in_monitored_inventory')
            for a in old.get('apps',[]):a.update(current=False,current_rate=None,current_expires_ms=None)
            db.execute('UPDATE allocation_history SET body=? WHERE id=?',(json.dumps(old,separators=(',',':')),old['id']))
            db.execute('DELETE FROM allocation_apps WHERE id=?',(old['id'],));db.execute('DELETE FROM allocations WHERE id=?',(old['id'],))
        for row in rows:
            rank=row.get('ranking') or {};m=row.get('metrics') or {};state_name='current' if row['current'] is True else 'unverified'
            body=json.dumps(row,separators=(',',':'),allow_nan=False)
            db.execute('INSERT OR REPLACE INTO allocations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(row['id'],row.get('ip'),state_name,row.get('current_expires_ms'),','.join(row.get('countries',[])),row['first_seen_ms'],row['last_seen_ms'],row.get('runs'),rank.get('score'),row.get('current_rate'),m.get('peak_mb_per_minute'),None,(row.get('cost') or {}).get('estimated_usd'),body))
            db.execute('INSERT INTO allocation_history VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET first_seen_ms=MIN(first_seen_ms,excluded.first_seen_ms),last_seen_ms=MAX(last_seen_ms,excluded.last_seen_ms),body=excluded.body',(row['id'],row['first_seen_ms'],row['last_seen_ms'],body))
            db.execute('DELETE FROM allocation_apps WHERE id=?',(row['id'],))
            for a in row['apps']:
                db.execute('INSERT INTO allocation_apps VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(row['id'],a['service'],a.get('ip'),a.get('duration_ms'),a.get('total_mb'),a.get('current_rate'),(a.get('ranking') or {}).get('score'),(a.get('metrics') or {}).get('peak_mb_per_minute'),None,'current' if a.get('current') is True else 'unverified',a.get('current_expires_ms'),json.dumps(a,separators=(',',':'),allow_nan=False)))
            ip=row.get('ip')
            if ip and ip not in profiles:
                neutral=score_ips([],{}, {},{},now,extra_ips=[ip])[ip]
                profile=dict(id='ip:'+ip,ip=ip,current=row['current'],current_expires_ms=row.get('current_expires_ms'),view=state_name,
                    countries=row.get('countries',[]),first_seen_ms=now,last_seen_ms=now,runs=0,apps=[],metrics={},ranking=neutral,
                    cost={'estimated_usd':None,'partial':True,'intervals':[]},current_rate=None,registry_only=True,registry_services=['grass'])
                db.execute('INSERT OR IGNORE INTO ips(ip,current_state,expires_ms,country,first_seen_ms,last_seen_ms,runs,score,body) VALUES(?,?,?,?,?,?,?,?,?)',(ip,state_name,row.get('current_expires_ms'),','.join(row.get('countries',[])),now,now,0,0,json.dumps(profile,separators=(',',':'))))
        summary=dict(status.get('summary') or {})
        summary.update(observed_containers=len(rows),current_containers=sum(r['current'] is True for r in rows),unavailable_containers=sum(r['current'] is not True for r in rows),current_ips=len({r['ip'] for r in rows if r['current'] is True and r.get('ip')}))
        summary.update(scored_containers=sum((r.get('ranking') or {}).get('lane') not in ('unseen','flagged') for r in rows),pending_containers=sum((r.get('ranking') or {}).get('lane')=='unseen' for r in rows))
        status.update(generated_ms=now,evaluation_generated_ms=evaluation,observations_refreshed_ms=now,summary=summary,shadow=shadow,all_ips=db.execute('SELECT COUNT(*) FROM ips').fetchone()[0],
            inventory_sources={'grass':{k:source.get(k) for k in ('inventory_ms','generated_ms','error','cloud_error','poll_error')}})
        if 'scoring' in status:status['scoring']['replacement_enabled']=False
        db.execute("UPDATE state SET body=? WHERE key='current'",(json.dumps(status,separators=(',',':'),allow_nan=False),))
    return status
