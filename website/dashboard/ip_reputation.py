"""Signed, remembered IP reputation from retained observations, not earnings."""
import collections
import copy
import ipaddress
import math
from dashboard.ip_evidence import SERVICES

VERSION='ip-reputation-v2'
SCALE=[-1000,1000]
EXCLUDED_SCORE=-1000
RULES={'native_connection':100,'healthy_hour':20,'healthy_history_cap':200,
       'sustained_transfer_cap':400,'repeat_allocation':20,'repeat_allocation_cap':100,
       'ip_rejection':-400,'native_failure':-75,'network_failure':-150,
       'proxy':-500,'vpn':-600,'tor':-750,'provider_risk_multiplier':-7,
       'verified_connection_test':20,'attributable_connection_test_failure':-100}
HEALTHY={'connected','online','accepted','healthy','ready'}

def numeric(value):
    try:return type(value) in (int,float) and math.isfinite(value)
    except OverflowError:return False

def event_for(run,now):
    native=run.get('native') or {};at=native.get('observed_ms')
    if not numeric(at) or not run['first_seen_ms']<=at<=min(now,run['last_seen_ms']):return None
    category=native.get('category');state=str(native.get('state') or '').lower()
    # Account failures and absent telemetry do not establish poor IP quality.
    if category in ('account restriction','missing recording'):return None
    kind=None;penalty=0
    if category=='provider IP rejection':kind='ip_rejection';penalty=RULES[kind]
    elif category=='process / startup failure':kind='native_failure';penalty=RULES[kind]
    elif category=='connection failure; shared cause unproven':kind='network_failure';penalty=RULES[kind]
    elif state in HEALTHY:kind='native_connection'
    if kind is None:return None
    return {'kind':kind,'penalty':penalty,'observed_ms':at,'service':run['service'],
            'group_id':run.get('group_id'),'instance':run.get('instance'),'machine':run.get('machine'),
            'run':run.get('run'),'episode_id':run.get('id'),'current':run.get('current') is True,
            'reason':native.get('reason'),'scope':'observed app/IP outcome; not a claim about earnings'}

def score_ips(runs,metrics,metadata,health,now,remembered=None,extra_ips=(),tests=()):
    remembered=remembered or {};groups=collections.defaultdict(list)
    test_by_ip={}
    for test in tests:
        if not isinstance(test,dict) or test.get('verified') is not True or not numeric(test.get('observed_ms')) or not 0<test['observed_ms']<=now:continue
        key=test.get('ip')
        if test['observed_ms']>=test_by_ip.get(key,{}).get('observed_ms',0):test_by_ip[key]=test
    # Archive copies cannot create extra successes or repeat penalties.
    unique={}
    for run in runs:
        if run.get('service') not in SERVICES:continue
        key=tuple(run.get(k) for k in ('service','group_id','instance','machine','run','ip'))
        if any(v is None for v in key):key=('partial',run['id'])
        old=unique.get(key)
        if old is None or (run.get('duration_ms') or 0,run['last_seen_ms'])>(old.get('duration_ms') or 0,old['last_seen_ms']):unique[key]=run
    for run in unique.values():groups[run['ip']].append(run)
    output={}
    for ip in set(groups)|set(extra_ips):
        selected=groups.get(ip,[]);geo=metadata.get(ip,{})
        outcomes={service:copy.deepcopy(value) for service,value in remembered.get(ip,{}).items()
                  if service in SERVICES and isinstance(value,dict) and numeric(value.get('observed_ms')) and value['observed_ms']<=now}
        current_bad={};successes=set();healthy_hours=0.;transfer=0.;coverage=0.
        latest=max((v['observed_ms'] for v in outcomes.values()),default=0)
        for run in selected:
            event=event_for(run,now)
            if event:
                latest=max(latest,event['observed_ms']);service=run['service']
                if event['observed_ms']>=outcomes.get(service,{}).get('observed_ms',-1):outcomes[service]=event
                if event['current'] and event['penalty']<0 and event['observed_ms']>=current_bad.get(service,{}).get('observed_ms',-1):current_bad[service]=event
                if event['kind']=='native_connection' and all(run.get(k) for k in ('group_id','instance','machine')):
                    successes.add(tuple(run[k] for k in ('group_id','instance','machine')))
            m=metrics.get(run['id'],{});h=health.get(run['id'],{})
            known=h.get('known_ms');fraction=h.get('native_fraction')
            if numeric(known) and known>=0 and numeric(fraction) and 0<=fraction<=1:
                healthy_hours=max(healthy_hours,known*fraction/3600000)
            rate=m.get('sustained_15m_mb_per_minute');duration=m.get('duration_ms');covered=m.get('coverage')
            if numeric(covered) and 0<=covered<=1:
                coverage=max(coverage,covered)
                if numeric(rate) and rate>=0 and numeric(duration) and duration>0:
                    contribution=min(RULES['sustained_transfer_cap'],80*math.log2(1+rate))*min(duration/10800000,1)*covered
                    transfer=max(transfer,contribution)
                    if numeric(run.get('last_seen_ms')) and 0<run['last_seen_ms']<=now:latest=max(latest,run['last_seen_ms'])
        # A concurrent overused/failed allocation is not cleared by another
        # allocation's newer success on that same provider and IP.
        outcomes.update(current_bad)
        native=RULES['native_connection'] if successes or any(v.get('kind')=='native_connection' for v in outcomes.values()) else 0
        components={'native_connection':native,'healthy_history':min(RULES['healthy_history_cap'],healthy_hours*RULES['healthy_hour']),
                    'sustained_transfer':transfer,'repeat_success':min(RULES['repeat_allocation_cap'],max(0,len(successes)-1)*RULES['repeat_allocation']),
                    'app_outcomes':max(-1000,sum(v.get('penalty',0) for v in outcomes.values() if numeric(v.get('penalty')))),
                    'public_risk':0,'connection_tests':0}
        lookup=geo.get('public_lookup') or {};risk=lookup.get('risk') or {};score=lookup.get('risk_score') or {}
        observed=lookup.get('observed_ms')
        if lookup.get('ip')==ip and numeric(observed) and 0<observed<=now:
            penalties=[0];latest=max(latest,observed)
            for name in ('proxy','vpn','tor'):
                if risk.get('is_'+name) is True:penalties.append(RULES[name])
            if (lookup.get('provider')=='IPQuery' and score.get('provider')=='IPQuery' and score.get('source')==lookup.get('source')
                and score.get('observed_ms')==observed and score.get('direction')=='higher_is_more_risk'
                and type(score.get('value')) is int and 0<=score['value']<=100):penalties.append(RULES['provider_risk_multiplier']*score['value'])
            components['public_risk']=min(penalties)
        test=test_by_ip.get(ip,{})
        if test.get('status')=='passed':components['connection_tests']=RULES['verified_connection_test'];latest=max(latest,test['observed_ms'])
        elif test.get('status')=='failed' and test.get('attributable_to_ip') is True:
            components['connection_tests']=RULES['attributable_connection_test_failure'];latest=max(latest,test['observed_ms'])
        excluded=any(f.get('kind')=='datacenter' and f.get('ip')==ip and (f.get('fresh') is True or f.get('owner_hold') is True) for f in geo.get('flags',[]))
        value=max(-1000,min(1000,round(sum(components.values()))))
        if excluded:value=EXCLUDED_SCORE
        evidence=bool(outcomes or latest or any(components.values()))
        try:public=ipaddress.ip_address(ip).is_global
        except ValueError:public=False
        if not public:value=0
        reasons=[]
        if excluded:reasons.append('Datacenter IP excluded by owner policy; positive traffic cannot cancel the exclusion.')
        for name,amount in components.items():
            if amount:reasons.append(name.replace('_',' ')+': '+format(round(amount),'+d')+' points')
        if not reasons:reasons=['Starts at zero; no positive or negative quality evidence is available yet.']
        output[ip]=dict(score=value,ip_score=value,version=VERSION,scale=SCALE,new_ip_score=0,
            lane='flagged' if excluded else 'provisional' if evidence else 'unseen',excluded=excluded,
            scope='whole_ip_history',confidence=coverage,confidence_basis='measured interval coverage, not a probability',
            reasons=reasons,dimensions=[dict(name=k.replace('_',' '),value=round(v),unit='points',status='recorded evidence',basis='Versioned reputation rule') for k,v in components.items()],
            components={k:round(v) for k,v in components.items()},outcomes=outcomes,
            calculated_ms=now,evidence_as_of_ms=latest or None,successful_allocations=len(successes),
            known_episodes=len(selected),retains_history=True,grace_until_ms=None,
            basis='Signed evidence points. Missing data and low traffic are not failures. Historical observations follow the IP; current identity and recovery decisions remain separate.')
    return output
