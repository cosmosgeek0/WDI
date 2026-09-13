"""Observation-only supervisor. No network, process, credential or action client."""
import hashlib
import json
import math
import uuid
import copy
import sqlite3
from pathlib import Path
from dashboard.ip_allocations import allocation_id
from dashboard.ip_evidence import NATIVE_FRESH_MS

VERSION='ip-shadow-v1'
VALIDATION_VERSION='ip-shadow-v2'
HEALTHY={'connected','online','accepted','healthy','ready'}
LABELS={'keep':'Keep','investigate':'Investigate','local_repair':'Local repair (simulation)',
        'hold':'Hold','consider_replace':'Consider replacement (simulation)'}

def input_digest(row,controller=None):
    """Bind decision facts, excluding traffic: idle/low rates never drive eviction."""
    rank=row.get('allocation_ranking',row.get('ranking')) or {}
    subject=dict(identity={k:row.get(k) for k in ('group_id','instance','machine')},
        ip=row.get('allocation_ip',row.get('ip')),allocation_id=row.get('allocation_id',row.get('id')),
        current=row.get('allocation_current',row.get('current')),
        expires_ms=row.get('allocation_current_expires_ms',row.get('current_expires_ms')),
        inventory_ms=row.get('inventory_ms'),cloud_state=row.get('cloud_state'),
        input_revision=row.get('observation_revision'),flags=row.get('flags',[]),
        ranking={k:rank.get(k) for k in ('score','version','calculated_ms')},controller_context=controller or {},apps=[])
    for a in sorted(row.get('apps',[]),key=lambda a:a.get('service','')):
        n=a.get('native') or {};history=n.get('recent_identity_history') or {}
        native={k:n.get(k) for k in ('state','category','reason','device','run','binding_verified','source_ms','observed_ms','received_ms','provider','local')}
        native['identity_history']={k:history.get(k) for k in ('runs','devices','samples','partial','first_source_ms','last_source_ms')}
        subject['apps'].append(dict(service=a.get('service'),run=a.get('run'),ip=a.get('ip'),current=a.get('current'),expires_ms=a.get('current_expires_ms'),native=native))
    return hashlib.sha256(json.dumps(subject,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()

def fresh(at, now, limit):
    return type(at) in (int,float) and math.isfinite(at) and 0<=now-at<=limit

def evaluate(row, now, evaluation_ms, controller=None):
    """Describe the next diagnostic step. Ranking never creates eligibility."""
    apps=row.get('apps',[]);controller=controller or {}
    identities=[dict(service=a.get('service'),run=a.get('run'),ip=a.get('ip'),
        device=(a.get('native') or {}).get('device'),source_ms=(a.get('native') or {}).get('source_ms'),
        native_ms=(a.get('native') or {}).get('observed_ms'),received_ms=(a.get('native') or {}).get('received_ms')) for a in apps]
    deadline=min(row.get('current_expires_ms') or now,evaluation_ms+180000)
    holds=['observation_only_execution_forbidden']
    if controller.get('decision_status') not in (None,'ready','no_candidate'):
        holds.append('live_controller:'+str(controller['decision_status']))
    value=dict(version=VERSION,validation_version=VALIDATION_VERSION,simulation_only=True,executed=False,execution_allowed=False,input_digest=input_digest(row,controller),controller_context=copy.deepcopy(controller),
        allocation_id=row.get('allocation_id') or row.get('id'),decision_ms=now,evaluation_generated_ms=evaluation_ms,
        identity={k:row.get(k) for k in ('group_id','instance','machine','ip')},inputs=identities,
        inventory_ms=row.get('inventory_ms'),input_revision=row.get('observation_revision'),
        score=(row.get('ranking') or {}).get('score'),score_version=(row.get('ranking') or {}).get('version'),
        confidence='unknown',confidence_basis='Evidence sufficiency for this next step, not IP quality probability',
        hold_reasons=holds,app_decisions=[],valid_until_ms=deadline,
        budget=dict(max_simultaneous=1,cooldown_ms=600000,max_per_hour=3,max_per_day=12,
                    scope='hypothetical replacement selections only',selected=False),
        economic_replacement=False)
    def finish(action,reason,code,confidence='unknown'):
        value.update(action=action,reason=reason,reason_code=code,confidence=confidence)
        return value
    if not fresh(evaluation_ms,now,180000):
        return finish('hold','The ranking publication is stale; wait for fresh inputs.','stale_evaluator')
    if row.get('current') is not True or deadline<=now:
        return finish('hold','Cloud membership is unavailable; preserve the last known allocation.','inventory_unavailable')
    try:
        for k in ('group_id','instance','machine'):uuid.UUID(row.get(k))
        if value['allocation_id']!=allocation_id(row):raise ValueError('Allocation mismatch')
    except (ValueError,TypeError,AttributeError):
        return finish('investigate','Verify the allocation identity before judging this connection.','identity_unverified')
    if row.get('cloud_state') not in (None,'running'):
        return finish('hold','The cloud slot is '+str(row['cloud_state'])+'; wait for its native client.','cloud_not_running')
    healthy=[];issues=[];unknown=[]
    for app in apps:
        n=app.get('native') or {};service=app.get('service');limit=NATIVE_FRESH_MS.get(service,90000)
        app_deadline=app.get('current_expires_ms',row.get('current_expires_ms'))
        if app.get('current') is not True or type(app_deadline) not in (int,float) or not now<app_deadline or n.get('binding_verified') is not True or not fresh(n.get('observed_ms'),now,limit) or not fresh(n.get('source_ms'),now,limit) or not fresh(n.get('received_ms'),now,limit):
            unknown.append(service);continue
        deadline=min(deadline,app_deadline,n['observed_ms']+limit,n['source_ms']+limit,n['received_ms']+limit)
        value['valid_until_ms']=deadline
        category=n.get('category');provider=n.get('provider') or {}
        provider_clock=fresh(provider.get('observed_ms'),now,limit)
        provider_outcome=any(k in provider for k in ('confirmed','connected','ip_match','device_ip_present'))
        if provider_clock:
            deadline=min(deadline,provider['observed_ms']+limit);value['valid_until_ms']=deadline
        rejection=(provider_clock and
                   any(provider.get(k) is False for k in ('confirmed','connected','ip_match','device_ip_present')))
        if category=='account restriction':
            step=('hold','account_restriction')
        elif category=='provider IP rejection' or rejection:
            step=('investigate','provider_ip_rejection' if category=='provider IP rejection' else 'provider_not_confirmed')
        elif category=='process / startup failure':step=('local_repair','native_process_failure')
        elif category=='connection failure; shared cause unproven':step=('investigate','connection_failure_cause_unknown')
        elif provider_outcome and not provider_clock:step=('investigate','provider_observation_unavailable')
        elif str(n.get('state') or '').lower() in HEALTHY:
            healthy.append(service);step=('keep','native_connected')
        else:step=('investigate','native_starting_or_unconfirmed')
        item=dict(service=service,action=step[0],reason_code=step[1],run=app.get('run'),ip=app.get('ip'),native_reason=n.get('reason'),provider=n.get('provider'),local=n.get('local'))
        value['app_decisions'].append(item)
        if step[0]!='keep':issues.append(item)
    # A bad companion must never evict an accepted peer.
    value['protected_apps']=healthy
    flags=[f for f in row.get('flags',[]) if f.get('kind')=='datacenter' and f.get('ip')==row['ip']
           and (f.get('fresh') is True or f.get('owner_hold') is True) and f.get('source')
           and type(f.get('observed_ms')) in (int,float) and 0<f['observed_ms']<=now]
    other_egress=any(a.get('service') in healthy and a.get('ip')!=row.get('ip') for a in apps)
    if flags and (healthy or issues) and not unknown and not other_egress:
        value['classification_evidence']=flags
        return finish('consider_replace','The bound current IP has a dated datacenter exclusion; simulation only.','owner_datacenter_exclusion','high')
    if healthy:
        return finish('keep','Keep the connected apps'+('; investigate the companion separately.' if issues else ', including when traffic is idle.'),'healthy_connected','high')
    if any(a['reason_code']=='account_restriction' for a in issues):
        return finish('hold','Repair the app/account restriction; another IP is not an established remedy.','account_restriction','high')
    churn=[dict(service=a.get('service'),**((a.get('native') or {}).get('recent_identity_history') or {})) for a in apps
           if ((a.get('native') or {}).get('recent_identity_history') or {}).get('runs',0)>=3]
    if churn:
        value['identity_history']=churn
        return finish('investigate','Several native runs were recorded on this allocation; investigate runtime identity continuity.','native_identity_churn','medium')
    if not row.get('ip'):
        return finish('investigate','The slot has not reported a public IP; retain its run history.','awaiting_ip')
    if any(a['action']=='local_repair' for a in issues):
        return finish('local_repair','Recover the affected app inside its existing container first.','native_process_failure','high')
    if issues:
        code=issues[0]['reason_code']
        if code=='provider_not_confirmed':
            return finish('investigate','Keep the container running while checking the provider listing: the app has not confirmed this device at its current IP. Listing absence is not a native failure or an IP rejection.',code,'medium')
        return finish('investigate','Investigate the app-specific native report before any whole-container replacement.',code,'medium')
    return finish('hold','Native evidence is absent, stale, or does not match this run.','native_unavailable')

def read_decision(row,now,evaluation_ms=None,stale=False):
    """Read-only v2 compatibility gate; never backdate a new decision for v1."""
    v=copy.deepcopy(row.get('shadow') or {})
    full=v.get('evaluation_generated_ms') if evaluation_ms is None else evaluation_ms
    code=None
    if v.get('version')!=VERSION or v.get('validation_version')!=VALIDATION_VERSION:code='unsupported_simulation_version'
    elif v.get('simulation_only') is not True or v.get('executed') is not False or v.get('execution_allowed') is not False:code='execution_contract_invalid'
    elif stale or not fresh(full,now,180000):code='stale_evaluator'
    elif v.get('evaluation_generated_ms')!=full:code='evaluation_revision_mismatch'
    elif not fresh(v.get('decision_ms'),now,180000) or v['decision_ms']<full:code='decision_clock_invalid'
    elif row.get('allocation_current',row.get('current')) is not True:code='inventory_unavailable'
    elif type(v.get('valid_until_ms')) not in (int,float) or not now<v['valid_until_ms']:code='decision_expired'
    elif v.get('input_digest')!=input_digest(row,v.get('controller_context')):code='decision_inputs_changed'
    else:
        # Rechecking the finite time bounds does not refresh an observation.
        subject=dict(row,ip=row.get('allocation_ip',row.get('ip')),ranking=row.get('allocation_ranking',row.get('ranking')),
            current=row.get('allocation_current',row.get('current')),
            current_expires_ms=row.get('allocation_current_expires_ms',row.get('current_expires_ms')))
        expected=evaluate(subject,v['decision_ms'],full,v.get('controller_context'))
        if v['valid_until_ms']>expected['valid_until_ms']:code='deadline_exceeds_inputs'
        elif any(v.get(k)!=expected.get(k) for k in ('identity','allocation_id','inputs','inventory_ms','input_revision','score','score_version','app_decisions','protected_apps')):code='decision_references_mismatch'
        elif not set(expected['hold_reasons']).issubset(v.get('hold_reasons',[])):code='decision_holds_mismatch'
        elif v.get('action')!=expected['action'] or v.get('reason_code')!=expected['reason_code']:code='decision_result_mismatch'
    v['reader_status']='current' if code is None else code
    if code:
        v.update(action='hold',reason='Await a fresh, matching simulation: '+code.replace('_',' ')+'.',reason_code=code,executed=False,execution_allowed=False)
    return v

def expire_shadow(row,now,stale=False,evaluation_ms=None):
    if not isinstance(row.get('shadow'),dict):return
    v=read_decision(row,now,evaluation_ms,stale);row['shadow']=v
    row.update(recommendation=LABELS.get(v['action'],'Hold'),recommendation_reason=v['reason'],recommendation_source='simulation_only')

def attach(rows,now,path=Path('/var/lib/salad-ip-evidence/evidence.sqlite3')):
    """Opt-in read-only adapter for simulation consumers, not a live action gate."""
    result=[];full=None
    try:
        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=1) as db:
            db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
            saved=db.execute("SELECT body FROM state WHERE key='current'").fetchone()
            if not saved:raise ValueError('No publication')
            state=json.loads(saved[0]);full=state.get('evaluation_generated_ms')
            for observed in rows:
                key=allocation_id(observed);item=db.execute('SELECT body FROM allocations WHERE id=?',(key,)).fetchone()
                value=dict(status='not_evaluated',simulation_only=True,executed=False,execution_allowed=False)
                if item:
                    stored=json.loads(item[0]);app=next((a for a in stored.get('apps',[]) if a['service']=='grass'),{})
                    run=observed.get('run');matches=all(stored.get(k)==observed.get(k) for k in ('group_id','instance','machine')) and app.get('ip')==observed.get('ip')
                    matches=matches and ((isinstance(run,str) and bool(run) and app.get('run') in (run,str(observed.get('group_id'))+'|'+run)) or run is None and app.get('run') is None)
                    if matches:
                        v=read_decision(stored,now,full)
                        value.update(status=v['reader_status'],decision=v)
                    else:value['status']='native_run_or_ip_mismatch'
                result.append(dict(observed,simulation_reader=value))
        return result,dict(status='ready',version=VERSION,validation_version=VALIDATION_VERSION,simulation_only=True,executed=False,evaluation_generated_ms=full,
            matched=sum(r['simulation_reader']['status']=='current' for r in result),total=len(result))
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
        return [],dict(status='unavailable',version=VERSION,simulation_only=True,executed=False,evaluation_generated_ms=full)

def prepare(db,rows,now,evaluation_ms,controller=None):
    """Persist one audit per full publication/identity/decision, never an action."""
    db.execute('''CREATE TABLE IF NOT EXISTS shadow_decisions(
       id TEXT PRIMARY KEY,allocation_id TEXT,decision_ms INTEGER,action TEXT,
       selected INTEGER NOT NULL,executed INTEGER NOT NULL CHECK(executed=0),body TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS shadow_decision_time ON shadow_decisions(decision_ms)')
    selected=[r[0] for r in db.execute('SELECT decision_ms FROM shadow_decisions WHERE selected=1 AND decision_ms>?',(now-86400000,))]
    counts={}
    for row in sorted(rows,key=lambda r:((r.get('ranking') or {}).get('score') is None,(r.get('ranking') or {}).get('score') or 0,r['id'])):
        v=evaluate(row,now,evaluation_ms,controller)
        binding=[{k:i.get(k) for k in ('service','run','ip','device')} for i in v['inputs']]
        key=hashlib.sha256(json.dumps([VERSION,VALIDATION_VERSION,evaluation_ms,row['id'],binding,v['input_digest'],v['action'],v['reason_code']],sort_keys=True).encode()).hexdigest()
        old=db.execute('SELECT body FROM shadow_decisions WHERE id=?',(key,)).fetchone()
        budget=v['budget']
        if old:
            recorded=json.loads(old[0]);v['audit_id']=key;v['first_decision_ms']=recorded['decision_ms'];budget.update(recorded['budget'])
        else:
            budget.update(simulated_last_day=len(selected),simulated_last_hour=sum(t>now-3600000 for t in selected))
            if v['action']=='consider_replace':
                if selected and now-max(selected)<600000:v['hold_reasons'].append('simulated_replacement_cooldown')
                elif budget['simulated_last_hour']>=3:v['hold_reasons'].append('simulated_hour_budget')
                elif len(selected)>=12:v['hold_reasons'].append('simulated_day_budget')
                elif controller and controller.get('decision_status') not in (None,'ready','no_candidate'):
                    v['hold_reasons'].append('observed_live_controller_hold')
                else:budget['selected']=True;selected.append(now)
            v['audit_id']=key;v['first_decision_ms']=now
            db.execute('INSERT INTO shadow_decisions VALUES(?,?,?,?,?,?,?)',(key,row['id'],now,v['action'],int(budget['selected']),0,json.dumps(v,separators=(',',':'),allow_nan=False)))
        row['shadow']=v
        row.update(recommendation=LABELS[v['action']],recommendation_reason=v['reason'],recommendation_source='simulation_only')
        counts[v['action']]=counts.get(v['action'],0)+1
    return dict(version=VERSION,validation_version=VALIDATION_VERSION,simulation_only=True,executed=False,execution_allowed=False,
        generated_ms=now,evaluation_generated_ms=evaluation_ms,counts=counts,records=sum(counts.values()),
        source='recorded allocation/native observations',live_controller_context=controller or {})
