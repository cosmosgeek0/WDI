"""Whole-allocation decisions from recorded facts; no cloud calls."""
import ipaddress
import json
import math
import uuid
from dashboard.ip_allocations import allocation_id
from dashboard.ip_evidence import NATIVE_FRESH_MS

VERSION='allocation-decisions-v1'
HEALTHY={'connected','online','accepted','healthy','ready'}

def numeric(value):
    try:return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:return False

def fresh(at,now,age):
    return numeric(at) and 0<=now-at<=age

def decide(row,now,evaluator_stale=False):
    """Low traffic, missing earnings and low relative rank never authorize eviction."""
    apps=row.get('apps') if isinstance(row.get('apps'),list) else []
    identity={k:row.get(k) for k in ('group_id','instance','machine')}
    ip=row.get('ip');deadline=row.get('current_expires_ms')
    def result(action,label,reason,code,eligible=False,protected=None,refs=None):
        return dict(version=VERSION,action=action,label=label,reason=reason,reason_code=code,
                    issued_ms=now,valid_until_ms=deadline if numeric(deadline) else now,
                    eligible_for_replacement=eligible,scope='whole_container',
                    identity={**identity,'ip':ip},protected_apps=protected or [],
                    evidence=refs or [],economic_replacement=False)
    if not row.get('allocation_id') or row.get('current') is False:
        return result('history','History','Use this dated record if the IP returns.','historical_only')
    if evaluator_stale or row.get('current') is not True or not numeric(deadline) or deadline<=now:
        return result('check_telemetry','Check data','Current allocation evidence is stale; keep the container.','stale_inventory')
    try:
        for value in identity.values():uuid.UUID(value)
        if row['allocation_id']!=allocation_id(row):raise ValueError('Allocation key mismatch')
    except (ValueError,TypeError,AttributeError):
        return result('check_telemetry','Check data','Verify allocation identity before changing this container.','unverified_identity')
    if not ip:
        return result('observe','Observe','Waiting for the allocation to report its public IP.','awaiting_ip')
    try:
        if not ipaddress.ip_address(ip).is_global:raise ValueError('Not public')
    except ValueError:
        return result('check_telemetry','Check data','Verify public egress; an internal address is not a bad connection.','unverified_egress')
    live=[];healthy=[];issues=[];native_deadlines=[]
    for app in apps:
        native=app.get('native') or {};age=NATIVE_FRESH_MS.get(app.get('service'),150000)
        if app.get('current') is not True or not numeric(app.get('current_expires_ms')) or app['current_expires_ms']<=now:continue
        if not fresh(native.get('observed_ms'),now,age) or native.get('fresh') is False:continue
        live.append(app)
        native_deadlines.append(min(native['observed_ms']+age,app['current_expires_ms']))
        category=native.get('category');provider=native.get('provider') or {}
        rejected=any(provider.get(k) is False for k in ('confirmed','connected','ip_match')) and fresh(provider.get('observed_ms'),now,age)
        state=str(native.get('state') or '').strip().lower()
        if state in HEALTHY and category not in ('account restriction','provider IP rejection','process / startup failure') and not rejected:healthy.append(app)
        elif (category and category not in ('unclassified','no classified failure','missing recording')) or rejected:issues.append(app)
    if native_deadlines:deadline=min(deadline,*native_deadlines)
    flags=[f for f in row.get('flags',[]) if isinstance(f,dict) and f.get('kind')=='datacenter' and f.get('ip')==ip
           and (f.get('fresh') is True or f.get('owner_hold') is True) and isinstance(f.get('source'),str) and f['source']
           and numeric(f.get('observed_ms')) and 0<=f['observed_ms']<=now]
    if flags:
        matching=[a for a in live if a.get('ip')==ip]
        other_healthy=[a for a in healthy if a.get('ip') and a['ip']!=ip]
        if matching and not other_healthy:
            return result('replace','Replace candidate','This current IP matches the owner’s datacenter exclusion policy.','owner_datacenter_exclusion',True,refs=flags)
        return result('check_telemetry','Check IP','A datacenter flag exists; verify active egress before replacing the container.','excluded_ip_needs_current_binding',protected=[a['service'] for a in other_healthy],refs=flags)
    if healthy:
        reason=', '.join(a.get('name') or a['service'] for a in healthy)+' connected; keep this container.'
        if issues:reason+=' Another app needs its own recovery.'
        return result('keep','Keep',reason,'healthy_connected',protected=[a['service'] for a in healthy])
    if issues:
        if any((a.get('native') or {}).get('category')=='account restriction' for a in issues):
            return result('repair_account','Fix account','An app reports an account problem; replacing the IP will not fix it.','account_restriction')
        return result('recover_app','Recover app','Use the existing protocol recovery before replacing this IP.','native_recovery')
    if any((a.get('native') or {}).get('observed_ms') is not None for a in apps):
        return result('check_telemetry','Check data','Native observations are stale or unconfirmed; keep the container while they are checked.','native_unverified')
    return result('observe','Observe','Waiting for first native acceptance; no failure is established.','awaiting_native')

def expire_decision(row,now,evaluator_stale=False):
    value=row.get('decision')
    if not isinstance(value,dict):return
    if evaluator_stale or row.get('allocation_current',row.get('current')) is not True or not numeric(value.get('valid_until_ms')) or now>=value['valid_until_ms']:
        row['decision']={**value,'action':'check_telemetry','label':'Check data','reason':'The decision expired; await fresh evaluation before changing the container.','reason_code':'decision_expired','eligible_for_replacement':False}
        row['recommendation']=row['decision']['label'];row['recommendation_reason']=row['decision']['reason']

def supervisor_feedback(root,now):
    """Small observed controller result, not a claim inferred from its process."""
    try:
        with (root/'grass/fleet-watch/latest.json').open('rb') as stream:raw=stream.read(2000001)
        if len(raw)>2000000:raise ValueError('Oversized controller view')
        value=json.loads(raw);at=value.get('observed_ms')
        if not fresh(at,now,180000):return {'connected':False,'status':'stale','observed_ms':at},{}
        evaluation=value.get('evaluator') or {}
        connected=evaluation.get('status')=='ready' and evaluation.get('matched',0)>0
        summary={'connected':connected,'status':evaluation.get('status','not_connected'),
                 'observed_ms':at,'configured_apply':value.get('mode')=='bounded_canary_actions',
                 'decision_status':value.get('decision_status'),'matched':evaluation.get('matched',0),
                 'evaluation_generated_ms':evaluation.get('evaluation_generated_ms')}
        allocations={}
        for row in value.get('rankings',[]):
            used=row.get('evaluator') or {};key=used.get('allocation_id')
            if not key or used.get('status')!='current':continue
            allocations[key]={'checked_ms':used.get('checked_ms'),'evaluation_ms':used.get('evaluation_ms'),
                              'recommendation_seen':used.get('label'),'decision_status':value.get('decision_status'),
                              'group_id':row.get('group_id'),'instance':row.get('instance'),'machine':row.get('machine')}
        return summary,allocations
    except (OSError,ValueError,TypeError,KeyError):
        return {'connected':False,'status':'unavailable'},{}
