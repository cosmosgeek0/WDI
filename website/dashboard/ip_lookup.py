"""Small public-IP lookup queue. No cloud/provider credentials or workload access."""
import argparse,fcntl,ipaddress,json,math,os,re,sqlite3,time,unicodedata,urllib.error,urllib.parse,urllib.request
from pathlib import Path
from contextlib import closing

HOST='api.ipquery.io'
SOURCE='https://ipquery.io/'
FLAGS=('is_datacenter','is_vpn','is_proxy','is_tor','is_mobile')
CACHE_VERSION=2
LOCATION_TEXT=('country','country_code','state','city','zipcode','timezone','localtime')
NOTE=('Dated supplier classification; false means not detected by this supplier. '
      'Missing flags do not prove residential or clean status. Coordinates are approximate. '
      'This is not a speed test or proof of other installed apps.')


def lookup_text(value):
    """Keep bounded plain labels, rejecting markup/control text rather than repairing it."""
    if not isinstance(value,str):return None
    value=value.strip()
    if not value or len(value)>200:return None
    if any(unicodedata.category(c).startswith('C') or c in '<>' for c in value):return None
    if re.search(r'(?i)(?:https?://|javascript:|data:|\b(?:token|password|secret|api[_-]?key|authorization|cookie)\s*[:=]|\bbearer\s|\beyJ[A-Za-z0-9_.-]+)',value):return None
    return value


def coordinate(value,limit):
    # Range checks precede isfinite so even huge JSON integers are harmless.
    return value if type(value) in (int,float) and -limit<=value<=limit and math.isfinite(value) else None


def timestamp(value):
    return value if type(value) is int and value>=0 else None


def risk_score(value,observed_ms):
    # IPQuery documents integer 0–100 malicious-activity risk, not reputation %.
    return dict(value=value if type(value) is int and 0<=value<=100 else None,
        units='score_points',minimum=0,maximum=100,direction='higher_is_more_risk',
        meaning='Provider estimate of malicious activity likelihood',provider='IPQuery',
        source=SOURCE,provider_field='risk.risk_score',observed_ms=observed_ms)

class SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        parsed=urllib.parse.urlparse(newurl)
        if parsed.scheme!='https' or parsed.hostname!=HOST:raise ValueError('Unexpected lookup redirect')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def clean(ip,raw,now):
    if not isinstance(raw,dict) or not isinstance(raw.get('ip'),str) or ipaddress.ip_address(raw['ip'])!=ipaddress.ip_address(ip):raise ValueError('IP lookup identity mismatch')
    risk=raw.get('risk') if isinstance(raw.get('risk'),dict) else {}
    isp=raw.get('isp') if isinstance(raw.get('isp'),dict) else {}
    location=raw.get('location') if isinstance(raw.get('location'),dict) else {}
    geo={key:lookup_text(location.get(key)) for key in LOCATION_TEXT}
    code=geo['country_code']
    geo['country_code']=code if code and re.fullmatch('[A-Z]{2}',code) else None
    geo.update(latitude=coordinate(location.get('latitude'),90),longitude=coordinate(location.get('longitude'),180))
    observed=timestamp(now)
    return dict(ip=str(ipaddress.ip_address(ip)),cache_version=CACHE_VERSION,observed_ms=observed,source=SOURCE,provider='IPQuery',
        risk={key:risk[key] if type(risk.get(key)) is bool else None for key in FLAGS},risk_score=risk_score(risk.get('risk_score'),observed),
        asn=lookup_text(isp.get('asn')),organization=lookup_text(isp.get('org')),isp=lookup_text(isp.get('isp')),
        country=geo['country_code'],location=geo,
        # No documented general network-type field today. Preserve only an explicit
        # same-named extension if supplied; never derive residential from false flags.
        network_type=lookup_text(raw.get('network_type')),note=NOTE)


def public_lookup(ip,value,attempt_ms=None,error=None):
    """Project only validated retained facts, with null additions for legacy rows.

    Reading never refreshes or upgrades the cache. Version-1 rows gain version 2
    only on their next normal, current-IP-only lookup after the existing 24h TTL.
    """
    supplied=value is not None
    try:
        if isinstance(value,str):value=json.loads(value)
        if supplied and (not isinstance(value,dict) or value.get('provider')!='IPQuery' or value.get('source')!=SOURCE):
            raise ValueError('Invalid cached lookup')
        value=value or {}
        version=value.get('cache_version',1) if supplied else None
        if supplied and (type(version) is not int or version not in (1,CACHE_VERSION)):
            raise ValueError('Unknown lookup cache version')
        location=value.get('location') if version==CACHE_VERSION and isinstance(value.get('location'),dict) else {'country_code':value.get('country')}
        risk=value.get('risk') if isinstance(value.get('risk'),dict) else {}
        score=value.get('risk_score')
        normalized=clean(ip,dict(ip=value.get('ip') if supplied else ip,
            isp=dict(asn=value.get('asn'),org=value.get('organization'),isp=value.get('isp')),
            location=location,network_type=value.get('network_type') if version==CACHE_VERSION else None,
            risk={**risk,'risk_score':score.get('value') if version==CACHE_VERSION and isinstance(score,dict) else None}),value.get('observed_ms'))
    except (ValueError,TypeError,OverflowError):
        normalized=clean(ip,{'ip':ip},None);version=None;error='Invalid cached lookup'
    normalized.update(cache_version=version,upgrade_pending=version==1,attempt_ms=timestamp(attempt_ms),error=lookup_text(error))
    return normalized


def tick(db,index,now,opener=None):
    if not index.exists():return dict(state='waiting_for_index',checked_ms=now)
    with closing(sqlite3.connect(index.as_uri()+'?mode=ro',uri=True)) as source:
        candidates=[r[0] for r in source.execute('SELECT DISTINCT ip FROM allocation_apps WHERE expires_ms>=? AND ip IS NOT NULL',(now,))]
    day=time.strftime('%Y-%m-%d',time.gmtime(now/1000));count=db.execute('SELECT attempts FROM budgets WHERE day=?',(day,)).fetchone();used=count[0] if count else 0
    done=[];opener=opener or urllib.request.build_opener(SameHostRedirect())
    for value in candidates:
        address=ipaddress.ip_address(value)
        if not address.is_global:continue
        ip=str(address);old=db.execute('SELECT observed_ms,retry_ms FROM entries WHERE ip=?',(ip,)).fetchone()
        if old and (old[0] is not None and 0<=now-old[0]<86400000 or old[1] is not None and now<old[1]):continue
        backoff=db.execute("SELECT value FROM state WHERE key='retry_ms'").fetchone()
        if backoff and now<int(backoff[0]):break
        if used>=240 or len(done)>=5:break
        used+=1
        with db:
            db.execute('INSERT INTO budgets VALUES(?,1) ON CONFLICT(day) DO UPDATE SET attempts=attempts+1',(day,))
            db.execute('INSERT INTO entries(ip,attempt_ms,retry_ms) VALUES(?,?,?) ON CONFLICT(ip) DO UPDATE SET attempt_ms=excluded.attempt_ms,retry_ms=excluded.retry_ms',(ip,now,now+3600000))
        try:
            request=urllib.request.Request('https://'+HOST+'/'+urllib.parse.quote(ip,safe='')+'?format=json',headers={'User-Agent':'WDI-IP-Evidence/1.0','Accept':'application/json'})
            with opener.open(request,timeout=8) as response:
                raw=response.read(65537)
            if len(raw)>65536:raise ValueError('Lookup response too large')
            value=clean(ip,json.loads(raw),int(time.time()*1000));body=json.dumps(value,separators=(',',':'))
            with db:
                db.execute('UPDATE entries SET observed_ms=?,retry_ms=?,body=?,error=NULL WHERE ip=?',(value['observed_ms'],value['observed_ms']+86400000,body,ip))
                if value['risk'].get('is_datacenter') is True:
                    db.execute('INSERT INTO flags VALUES(?,?,?) ON CONFLICT(ip) DO UPDATE SET observed_ms=excluded.observed_ms,body=excluded.body',(ip,value['observed_ms'],body))
            done.append(dict(ip=ip,status='recorded'))
        except Exception as exc:
            error='HTTP '+str(exc.code) if isinstance(exc,urllib.error.HTTPError) else type(exc).__name__
            with db:
                db.execute('UPDATE entries SET error=? WHERE ip=?',(error,ip))
                if isinstance(exc,urllib.error.HTTPError) and exc.code==429:db.execute("INSERT OR REPLACE INTO state VALUES('retry_ms',?)",(str(now+3600000),))
            done.append(dict(ip=ip,status=error))
        time.sleep(2)
    return dict(checked_ms=now,lookups=done,attempts_today=used,budget=240)


def open_database(path):
    db=sqlite3.connect(path,timeout=10);db.execute('PRAGMA journal_mode=WAL')
    db.executescript('CREATE TABLE IF NOT EXISTS entries(ip TEXT PRIMARY KEY,observed_ms INTEGER,attempt_ms INTEGER,retry_ms INTEGER,body TEXT,error TEXT); CREATE TABLE IF NOT EXISTS flags(ip TEXT PRIMARY KEY,observed_ms INTEGER,body TEXT); CREATE TABLE IF NOT EXISTS budgets(day TEXT PRIMARY KEY,attempts INTEGER); CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT);')
    return db


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--index',type=Path,default=Path('/var/lib/salad-ip-evidence/evidence.sqlite3'));p.add_argument('--data',type=Path,default=Path('/var/lib/salad-ip-enrichment'));p.add_argument('--once',action='store_true');args=p.parse_args()
    os.umask(0o027);args.data.mkdir(parents=True,exist_ok=True)
    lock=(args.data/'lookup.lock').open('a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    db=open_database(args.data/'lookups.sqlite3')
    while True:
        try:result=tick(db,args.index,int(time.time()*1000))
        except Exception as exc:result=dict(error=type(exc).__name__,checked_ms=int(time.time()*1000))
        print(json.dumps(result),flush=True)
        if args.once:break
        time.sleep(15)

if __name__=='__main__':main()
