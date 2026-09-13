"""Read only already-retained metadata/watch evidence. No network calls."""
import collections
import ipaddress
import json
from pathlib import Path
from contextlib import closing
from dashboard.ip_evidence import connection,number,read_json,safe_text
from dashboard.ip_metrics import HOUR,MINUTE
from dashboard.ip_lookup import public_lookup

class Enrichment:
    def __init__(self,root):
        self.root=root;self.watch={};self.watch_cursor=0;self.watch_inode=None
    def read(self,runs,now,extra_ips=()):
        folder=self.root/'grass'/'fleet-watch';prefixes=collections.defaultdict(dict)
        for name in ('aws-ranges.json','google-cloud-ranges.json'):
            saved=read_json(folder/name)
            if not saved.get('observed_ms') or not saved.get('source'):continue
            for text in saved.get('networks',[]):
                network=ipaddress.ip_network(text)
                prefixes[(network.version,network.prefixlen)][int(network.network_address)]=dict(source=saved['source'],observed_ms=saved['observed_ms'],cidr=text)
        ips={}
        for ip in {r['ip'] for r in runs}|set(extra_ips):
            address=ipaddress.ip_address(ip);flags=[]
            for (version,bits),ranges in prefixes.items():
                if address.version!=version:continue
                base=(int(address)>>(address.max_prefixlen-bits))<<(address.max_prefixlen-bits)
                if base in ranges:
                    p=ranges[base];flags.append(dict(kind='datacenter',scope='owner bundle exclusion',ip=ip,**p,fresh=0<=now-p['observed_ms']<=24*HOUR,
                        basis='Address matches a retained official cloud-provider prefix. This is a dated classification, not a speed test.'))
            saved=read_json(folder/('ip-'+address.compressed.replace(':','_')+'.json'))
            metadata=saved.get('connection') or {}
            ips[ip]=dict(asn=metadata.get('asn') if type(metadata.get('asn')) is int else None,
                network_organization=safe_text(metadata.get('org')),isp=safe_text(metadata.get('isp')),
                lookup_country=safe_text(saved.get('country')),metadata_observed_ms=number(saved.get('observed_ms')),metadata_source=saved.get('source') if saved.get('ok') else None,
                flags=flags,network_type='datacenter' if flags else None,
                metadata_note='An ASN organization name is not a hosting/VPN verdict. No cloud-prefix match does not prove residential status.')
        lookup_path=Path('/var/lib/salad-ip-enrichment/lookups.sqlite3')
        if lookup_path.exists():
            with closing(connection(lookup_path)) as db:
                for entry in db.execute('SELECT ip,body,error,attempt_ms FROM entries'):
                    ip=entry['ip']
                    if ip not in ips:continue
                    value=public_lookup(ip,entry['body'],entry['attempt_ms'],entry['error'])
                    ips[ip]['public_lookup']=value
                    if value.get('observed_ms') is not None:
                        asn=value.get('asn') or ''
                        ips[ip].update(asn=int(asn[2:]) if asn.startswith('AS') and asn[2:].isdigit() else ips[ip].get('asn'),network_organization=safe_text(value.get('organization')),isp=safe_text(value.get('isp')),lookup_country=value.get('country'),metadata_observed_ms=value['observed_ms'],metadata_source=value['source'])
                for entry in db.execute('SELECT ip,observed_ms,body FROM flags'):
                    if entry['ip'] not in ips:continue
                    value=json.loads(entry['body'])
                    ips[entry['ip']]['flags'].append(dict(kind='datacenter',scope='owner exclusion retained from supplier evidence',ip=entry['ip'],observed_ms=entry['observed_ms'],source=value['source'],fresh=0<=now-entry['observed_ms']<=86400000,owner_hold=True,basis='IPQuery reported this IP as datacenter. The dated flag is retained; it is not proof of installed software or speed.'))
                    ips[entry['ip']]['network_type']='supplier-reported datacenter'
        health={};path=folder/'history.sqlite3'
        if path.exists():
            def state(w):
                at=w['observed_ms']
                if not all(number(w.get(k)) is not None and 0<=at-w[k]<=90000 for k in ('source_ms','health_ms','inventory_ms')):return None
                if w.get('state')=='login_required' or w.get('http') in (401,403):return None
                if w.get('native_status')=='CONNECTED' and (w.get('established_tcp') or 0)>0:return True
                if w.get('native_failure') is True and w.get('state') in ('client_missing','client_stalled','offline'):return False
                return None
            inode=path.stat().st_ino
            with closing(connection(path)) as db:
                high=db.execute('SELECT COALESCE(MAX(rowid),0) FROM observations').fetchone()[0]
                if inode!=self.watch_inode or high<self.watch_cursor:
                    self.watch={};self.watch_cursor=0;self.watch_inode=inode
                # Watch observations are INSERT OR IGNORE immutable minute rows.
                # Only new rows are decoded after the initial read.
                for item in db.execute('SELECT body FROM observations WHERE rowid>? AND rowid<=? ORDER BY rowid',(self.watch_cursor,high)):
                    raw=json.loads(item[0])
                    w={k:raw.get(k) for k in ('group_id','instance','machine','run','ip','observed_ms','source_ms','inventory_ms','health_ms',
                        'state','http','native_status','established_tcp','native_failure','quality','provider_present','provider_ms')}
                    key=(w['group_id'],w['instance'],w['machine'],str(w['group_id'] or 'legacy')+'|'+str(w['run']),w['ip'])
                    record=self.watch.setdefault(key,dict(last=None,hours={},failures=[]))
                    old=record['last']
                    if old and old.get('source_ms')==w.get('source_ms'):continue
                    sb=state(w);sa=state(old) if old else None
                    if sb is False and sa is not False:record['failures'].append(w['observed_ms'])
                    if old and 0<w['observed_ms']-old['observed_ms']<=2*MINUTE and sa is not None and sa==sb:
                        for h in range(old['observed_ms']//HOUR*HOUR,(w['observed_ms']-1)//HOUR*HOUR+1,HOUR):
                            begin=max(old['observed_ms'],h);stop=min(w['observed_ms'],h+HOUR);ms=stop-begin
                            bucket=record['hours'].setdefault(h,[0,0,begin,stop]);bucket[0]+=ms;bucket[1]+=ms if sa else 0
                            bucket[2]=min(bucket[2],begin);bucket[3]=max(bucket[3],stop)
                    if old is None or w['observed_ms']>old['observed_ms']:record['last']=w
                self.watch_cursor=high
            for r in runs:
                key=(r['group_id'],r['instance'],r['machine'],r['run'],r['ip']);record=self.watch.get(key)
                if not record or not record['last']:continue
                end=r.get('_evaluation_end_ms',r['last_seen_ms']);first=max(r['first_seen_ms'],end-72*HOUR)
                known=healthy=0;days=set()
                for h,(k,g,begin,stop) in record['hours'].items():
                    fraction=max(0,min(end,stop)-max(first,begin))/(stop-begin) if stop>begin else 0
                    if fraction and k:
                        known+=k*fraction;healthy+=g*fraction;days.add(h//(24*HOUR))
                latest=record['last']
                health[r['id']]=dict(failure_episodes=sum(first<=at<=end for at in record['failures']) if known else None,native_fraction=healthy/known if known else None,known_ms=known,healthy_ms=healthy,days=len(days),
                    provider_quality=number(latest.get('quality')),provider_present=latest.get('provider_present'),provider_observed_ms=number(latest.get('provider_ms')),
                    source_ms=number(latest.get('source_ms')),native_status=safe_text(latest.get('native_status')),
                    basis='Consecutive distinct Grass watch samples with matching native state, fresh clocks and gaps ≤2 minutes. Transitions/account errors/stale intervals unknown; partial boundary hours estimated.')
        return ips,health
