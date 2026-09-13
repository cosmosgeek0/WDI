"""Container identity and counter scope. An IP is never an allocation identity."""
import copy
import hashlib
import ipaddress
import json
from dashboard.ip_evidence import SERVICES,number,safe_text
from dashboard.ip_reputation import VERSION,EXCLUDED_SCORE


def allocation_id(row):
    group=row.get('group_id') or (row.get('service','unknown')+':'+str(row.get('group') or 'unbound'))
    return 'alloc:'+hashlib.sha256(json.dumps([group,row.get('instance') or row.get('id'),row.get('machine')]).encode()).hexdigest()[:24]


def fresh_workers(sources,now):
    for service,source in sources.items():
        if source.get('poll_error') or source.get('cloud_error'):continue
        if source.get('error') and service not in ('grass','earnapp'):continue
        for w in source.get('workers',[]):
            at=number(w.get('inventory_ms'))
            if not w.get('id') or w.get('salad_state')!='running' or w.get('source_error') or at is None or not 0<=now-at<=(120000 if service in ('hg','ps') else 90000):continue
            yield dict(w,service=service,instance=w['id'])


def pending_app(w,now):
    service=w['service'];at=number(w.get('sample_ms'));reason=safe_text(w.get('error') or w.get('client_error') or w.get('reason'))
    state=w.get('client_status') or w.get('client_state') or w.get('state')
    state=state.get('state') if isinstance(state,dict) else state
    return dict(service=service,name=SERVICES[service][0],ip=w.get('ip'),current=True,
        current_expires_ms=w['inventory_ms']+(120000 if service in ('hg','ps') else 90000),current_rate=None,rate_expires_ms=w.get('rate_expires_ms'),performance_expires_ms=None,
        native=dict(state=safe_text(state),reason=reason,observed_ms=at,category='missing recording',device=safe_text(w.get('uuid') or w.get('device_id'))),
        metrics={},watch_evidence=None,runs=0,total_mb=None,duration_ms=None,average_mb_per_minute=None,partial=True,
        ranking=dict(score=0,confidence=0,lane='unseen',version=VERSION,scope_service=service,dimensions=[],reasons=['New IP starts at zero; waiting for quality observations.']),
        evaluation_window=None)


def current_allocations(runs,sources,ip_rows,app_state,now,workers=None):
    records={}
    for w in (fresh_workers(sources,now) if workers is None else workers):records.setdefault(allocation_id(w),[]).append(w)
    run_groups={}
    for r in runs:run_groups.setdefault(allocation_id(r),[]).append(r)
    output=[]
    for key,workers in records.items():
        apps=[];matched_runs=run_groups.get(key,[])
        for w in workers:
            matching=[r for r in matched_runs if r['service']==w['service'] and r['current'] is True and r['ip']==w.get('ip')]
            if matching:
                chosen=max(matching,key=lambda r:r['last_seen_ms'])
                old=next((a for a in ip_rows.get(chosen['ip'],{}).get('apps',[]) if a['service']==w['service']),{})
                app=copy.deepcopy(old);app.update(app_state(matching));app['ip']=chosen['ip'];app['run']=chosen['run']
                app['native']=chosen['native'];app['runs']=len({r['run'] for r in matched_runs if r['service']==w['service'] and r.get('run')})
            else:app=pending_app(w,now)
            app.update(total_mb=app.get('metrics',{}).get('total_mb'),duration_ms=app.get('metrics',{}).get('duration_ms'),average_mb_per_minute=app.get('metrics',{}).get('average_mb_per_minute'))
            app.update(measurement_scope='container_interface',app_isolated_rx_bytes=None,app_isolated_tx_bytes=None)
            apps.append(app)
        # Never sum app reporters. Prefer a fresh existing Grass stream, then a
        # deterministic app name. This is a reporter choice, not a profit score.
        chosen=min(apps,key=lambda a:(not (number(a.get('metrics',{}).get('total_mb')) is not None and (a.get('performance_expires_ms') or 0)>=now),a['service']!='grass',a['service']))
        try:address=ipaddress.ip_address(chosen['ip']) if chosen.get('ip') else None
        except ValueError:address=None
        ip=str(address) if address is not None else None
        profile=copy.deepcopy(ip_rows.get(ip,{}))
        prior=dict(runs=profile.get('runs'),first_seen_ms=profile.get('first_seen_ms'),last_seen_ms=profile.get('last_seen_ms'),apps=[a['service'] for a in profile.get('apps',[])])
        first=min((r['first_seen_ms'] for r in matched_runs),default=min(w['inventory_ms'] for w in workers))
        last=max((r['last_seen_ms'] for r in matched_runs),default=max(w['inventory_ms'] for w in workers))
        base=workers[0]
        countries=sorted({str(w['country']) for w in workers if w.get('country')}) or profile.get('countries',[])
        ips=sorted({a['ip'] for a in apps if a.get('ip')})
        for k in ('ranking','metrics','watch_evidence','current_rate','rate_source_ms','rate_received_ms','rate_expires_ms','performance_expires_ms','evaluation_window'):
            profile[k]=copy.deepcopy(chosen.get(k))
        if any(f.get('fresh') or f.get('owner_hold') for f in profile.get('flags',[])):
            profile['ranking']=dict(profile['ranking'],score=EXCLUDED_SCORE,lane='flagged',reasons=['Excluded by owner policy: dated, attributed datacenter evidence. Inspect the classification source and date.'])
        shared=len({a['service'] for a in apps})>1
        profile.update(id=key,allocation_id=key,group_id=base.get('group_id'),instance=base['instance'],machine=base.get('machine'),
            ip=ip,ips=ips,countries=countries,apps=apps,current=True,current_expires_ms=max(a['current_expires_ms'] for a in apps),
            first_seen_ms=first,last_seen_ms=last,runs=len({(r['service'],r['run']) for r in matched_runs if r.get('run')}),
            ip_history_summary=prior,measurement_scope='container_interface',traffic_reporter=chosen['service'],shared_counter=shared,
            traffic_scope_note='One container-interface RX+TX stream, reported by '+SERVICES[chosen['service']][0]+'. '+('Multiple app reporters overlap; true per-app traffic is unavailable.' if shared else 'Includes all traffic inside this container, not provider-sold traffic.'),
            recommendation='Observe — evaluator has no replacement permissions',economic_status='No attributable container profit established')
        old_cost=profile.get('cost') or {}
        intervals=[x for x in old_cost.get('intervals',[]) if x.get('group_id')==base.get('group_id') and x.get('instance')==base['instance'] and x.get('machine')==base.get('machine')]
        amounts=[x['estimated_usd'] for x in intervals if number(x.get('estimated_usd')) is not None]
        profile['cost']=dict(estimated_usd=sum(amounts) if amounts else None,allocations=1 if intervals else 0,partial=True,intervals=intervals,basis='Matched running cost for this allocation only, counted once across app reporters; source coverage remains partial.')
        profile.setdefault('flags',[]);profile.setdefault('asn',None);profile.setdefault('network_type',None)
        profile.setdefault('address_scope','unknown' if address is None else 'public' if address.is_global else 'non-public')
        if address is not None and not address.is_global:
            profile['ranking']=dict(profile['ranking'],score=0,lane='flagged',reasons=['Non-public address is not a public egress candidate.'])
        if profile['ranking'].get('lane')=='flagged':
            profile['allocation_exclusion']=dict(score=profile['ranking']['score'],lane='flagged',reasons=profile['ranking']['reasons'])
            for a in profile['apps']:a['ranking']=dict(a['ranking'],**profile['allocation_exclusion'])
        output.append(profile)
    return output
