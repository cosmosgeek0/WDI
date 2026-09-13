"""Versioned, conservative observed-transfer metrics; never capacity or earnings."""
from __future__ import annotations
import collections
import math
from contextlib import closing
from dashboard.ip_evidence import SERVICES, connection, number, signature

MINUTE=60000
HOUR=3600000

def percentile(values,p):
    if not values:return None
    values=sorted(values);x=(len(values)-1)*p;lo=int(x);hi=min(lo+1,len(values)-1)
    return values[lo]*(hi-x)+values[hi]*(x-lo) if lo!=hi else values[lo]


def summarize_minutes(points,first,last):
    """Points: (UTC minute, MB, covered ms, estimated). Zero remains measured."""
    selected=[]
    lower=max(first,last-72*HOUR)
    for point in points:
        at,mb,covered,estimated=point[:4]
        if not (number(mb) is not None and mb>=0 and 0<covered<=MINUTE):continue
        begin,end=point[4:6] if len(point)>=6 else (max(first,at),min(last,at+MINUTE))
        left,right=max(lower,begin),min(last,end)
        if end<=begin or right<=left:continue
        fraction=(right-left)/(end-begin)
        selected.append((at,mb*fraction,min(covered*fraction,right-left),bool(estimated or fraction<1)))
    rates=[p[1]*MINUTE/p[2] for p in selected]
    by_time={p[0]:p for p in selected};windows=[]
    # Non-overlapping quarter-hour windows avoid giving overlapping samples
    # the appearance of independent repeated evidence.
    for at in by_time:
        if at%(15*MINUTE):continue
        group=[by_time.get(at+i*MINUTE) for i in range(15)]
        if all(p is not None and p[2]>=.9*MINUTE for p in group):
            windows.append(sum(p[1] for p in group)*MINUTE/sum(p[2] for p in group))
    amount=sum(p[1] for p in selected);covered=sum(p[2] for p in selected)
    span=max(0,last-lower)
    top=max(1,math.ceil(len(selected)*.01))
    return dict(total_mb=amount if selected else None,duration_ms=covered if selected else None,average_mb_per_minute=amount*MINUTE/covered if covered else None,peak_mb_per_minute=max(rates) if rates else None,p95_mb_per_minute=percentile(rates,.95),
        median_mb_per_minute=percentile(rates,.5),sustained_15m_mb_per_minute=percentile(windows,.5),sustained_windows=len(windows),
        burst_top_1pct_fraction=sum(sorted((p[1] for p in selected),reverse=True)[:top])/amount if amount>0 else None,
        positive_minute_fraction=sum(p[1]>0 for p in selected)/len(selected) if selected else None,
        measured_minutes=covered/MINUTE if selected else None,coverage=min(1,covered/span) if span else None,
        estimated=any(p[3] for p in selected),window_start_ms=lower,window_end_ms=last,
        basis='Retained traffic over valid intervals in the final 72 hours of this episode. Positive bytes include overhead. Rates are observed transfer, not connection capacity.')


def _grass_boundary_minute(db, run, begin, end):
    """Recover only this run's accepted intervals within a cached boundary."""
    points=db.execute('''SELECT start_ms,source_ms,rx,tx FROM intervals
        WHERE run=? AND group_id IS ? AND instance=? AND machine=? AND ip IS ?
          AND source_ms>? AND source_ms<=? AND start_ms<? AND valid=1
          AND duration_ms=source_ms-start_ms AND duration_ms>0
          AND duration_ms<=90000 AND rx>=0 AND tx>=0
        ORDER BY start_ms,source_ms''',
        (run['run'],run['group_id'],run['instance'],run['machine'],run['ip'],
         begin,end+90000,end))
    amount=0.;covered=0;first=previous=None
    for p in points:
        left=max(begin,p['start_ms']);right=min(end,p['source_ms'])
        if right<=left:continue
        if previous is not None and left<previous:return None
        if first is None:first=left
        amount+=(p['rx']+p['tx'])/1e6*(right-left)/(p['source_ms']-p['start_ms'])
        covered+=right-left;previous=right
    return (amount,covered,first,previous) if first is not None else None


class MetricReader:
    def __init__(self,root,honeygain=None):
        self.root=root;self.honeygain=honeygain;self.cache={}

    def read(self,service,runs):
        folder=self.root/SERVICES[service][1];path=folder/('minutes.sqlite3' if service=='ps' else 'traffic.sqlite3')
        paths=[path,path.with_name(path.name+'-wal')] if service!='hg' else sorted(folder.glob('*.json*'))+sorted(folder.glob('archives/*/*.json*'))
        stamp=signature(paths);saved=self.cache.get(service)
        binding=stamp[0][1] if stamp else None
        state=saved if saved and saved['binding']==binding else dict(binding=binding,buckets={},rows={},stamp=None,watermark=0,metrics={},source_cursor=0,sequences={})
        changed=state['stamp']!=stamp
        snapshots={r['id']:(r['first_seen_ms'],r['last_seen_ms'],r['samples'],r['total_mb']) for r in runs}
        # A retained episode can catch up to a minute cache already read.
        changed=changed or (service=='grass' and snapshots!=state['rows'])
        # Immutable source interval changes and late delivery are reflected in
        # recorded totals/counts. A changed old episode rewinds to its start.
        cutoff=max(0,state['watermark']-2*HOUR) if state['stamp'] else 0
        native=service in ('ps','proxyrack','pawns')
        source_high=state['source_cursor']
        if changed and path.exists() and service!='hg':
            with closing(connection(path)) as cursor_db:
                table='intervals' if service=='grass' else 'minutes' if native else 'events' if service=='rp' else 'samples'
                source_high=cursor_db.execute('SELECT COALESCE(MAX(rowid),0) FROM '+table).fetchone()[0]
                if source_high<state['source_cursor']:state['source_cursor']=0;cutoff=0
                # Row IDs catch old source-time backfills in continuing runs.
                # Grass interval replacements receive new row IDs as well.
                extra=" AND kind='minute'" if service=='rp' else ''
                earliest=cursor_db.execute('SELECT MIN(start_ms) FROM '+table+' WHERE rowid>? AND rowid<=?'+extra,(state['source_cursor'],source_high)).fetchone()[0]
                if earliest is not None:cutoff=min(cutoff,max(0,earliest))
                if native:
                    added=collections.defaultdict(lambda:[0,0.])
                    for r in cursor_db.execute('SELECT group_id,instance,machine,observer,seq,ip,rx,tx,valid FROM minutes WHERE rowid>? AND rowid<=?',(state['source_cursor'],source_high)):
                        key=(r['group_id'],r['instance'],r['machine'],r['instance']+'/'+str(r['observer']),r['ip'])
                        added[key][0]+=1
                        if r['valid'] and r['seq']>0 and r['rx'] is not None and r['tx'] is not None:added[key][1]+=(r['rx']+r['tx'])/1e6
                    for r in runs:
                        old=state['rows'].get(r['id']);inc=added[(r['group_id'],r['instance'],r['machine'],r['run'],r['ip'])]
                        if old and ((number(old[3]) is not None and number(r['total_mb']) is not None and abs(old[3]+inc[1]-r['total_mb'])>1e-7) or old[2]+inc[0]!=r['samples']):
                            cutoff=min(cutoff,r['first_seen_ms'])
        if changed:
            for r in runs:
                old=state['rows'].get(r['id'])
                if old is None or (old!=snapshots[r['id']] and r['last_seen_ms']<cutoff) or (old and ((old[3] is None)!=(r['total_mb'] is None) or r['first_seen_ms']<old[0] or r['samples']<old[2] or number(r['total_mb']) is not None and number(old[3]) is not None and r['total_mb']<old[3])):
                    cutoff=min(cutoff,r['first_seen_ms'])
            cutoff=cutoff//HOUR*HOUR
            if service=='hg':cutoff=0
            for bins in state['buckets'].values():
                for at in list(bins):
                    if at>=cutoff:del bins[at]
            def add(r,at,mb,ms,estimated,begin=None,end=None):
                if at<cutoff or ms<=0:return
                if at<max(r['first_seen_ms']//MINUTE*MINUTE,r['last_seen_ms']-72*HOUR):return
                v=state['buckets'].setdefault(r['id'],{}).setdefault(at,[0.,0.,False,begin if begin is not None else at,end if end is not None else at+MINUTE])
                v[0]+=mb;v[1]+=ms;v[2]|=bool(estimated)
                v[3]=min(v[3],begin if begin is not None else at);v[4]=max(v[4],end if end is not None else at+MINUTE)
            def interval(r,start,end,amount,estimated):
                if not (number(start) is not None and number(end) is not None and number(amount) is not None and end>start and amount>=0):return
                for at in range(max(cutoff,int(start)//MINUTE*MINUTE),int(end-1)//MINUTE*MINUTE+1,MINUTE):
                    begin,stop=max(start,at),min(end,at+MINUTE);ms=stop-begin
                    if ms>0:add(r,at,amount/1e6*ms/(end-start),ms,estimated,begin,stop)
            if service=='hg' and self.honeygain:
                points=collections.defaultdict(dict)
                for test in set(r['test'] for r in runs):
                    for obs in self.honeygain(test).get('observations',[]):
                        for w in obs.get('hg',{}).get('workers',[]):
                            key=(w.get('group'),w.get('machine'),w.get('run'));at=w.get('sample_ms') or obs['time_ms'];value=(w.get('network_bytes'),w.get('ip'))
                            values=points[key]
                            values[at]=value if at not in values or values[at]==value else (None,None)
                lookup={(r['group'],r['machine'],r['run'],r['ip']):r for r in runs}
                for key,values in points.items():
                    previous=None
                    for at,(amount,ip) in sorted(values.items()):
                        if previous and previous[2]==ip and number(amount) is not None and number(previous[1]) is not None and 0<at-previous[0]<=3*MINUTE and amount>=previous[1]:
                            r=lookup.get(key+(ip,))
                            if r:interval(r,previous[0],at,amount-previous[1],True)
                        previous=(at,amount,ip)
            elif path.exists():
                with closing(connection(path)) as db:
                    db.execute('BEGIN')
                    cached=service in ('grass','earnapp','earnfm')
                    if service=='grass':
                        sql='SELECT group_id,instance,machine,NULL run,ip,minute_ms at,bytes/1e6 mb,coverage_ms ms,estimated FROM minute_nodes WHERE minute_ms>=? AND coverage_ms>0'
                    elif service=='earnapp':
                        sql="SELECT group_id,instance,machine,instance||'/'||observer run,ip,minute_ms at,bytes/1e6 mb,duration_ms ms,estimated FROM minute_nodes WHERE minute_ms>=? AND duration_ms>0"
                    elif service=='earnfm':
                        sql='SELECT NULL group_id,instance,machine,run,ip,time_ms at,(rx+tx)/1e6 mb,duration_ms ms,estimated FROM minute_cache WHERE time_ms>=? AND duration_ms>0 AND rx>=0 AND tx>=0'
                    elif native:
                        sql="SELECT group_id,instance,machine,observer,seq,instance||'/'||observer run,ip,start_ms,end_ms,rx+tx amount,bucket_ms IS NULL estimated FROM minutes WHERE group_id=? AND instance=? AND observer=? AND seq>=? AND end_ms>? AND valid=1 AND seq>0 AND end_ms>start_ms AND rx>=0 AND tx>=0"
                    elif service=='rp':
                        sql="SELECT group_id,instance,machine,run,ip,start_ms,end_ms,rx_bytes+tx_bytes amount,1 estimated FROM events WHERE end_ms>? AND kind='minute' AND valid=1 AND end_ms>start_ms AND rx_bytes>=0 AND tx_bytes>=0"
                    elif service=='tm':
                        sql="SELECT NULL group_id,instance,machine,run,ip,start_ms,time_ms end_ms,interval_ms,rx+tx amount,1 estimated,(SELECT following.interval_ms FROM samples following WHERE following.run=samples.run AND following.seq=samples.seq+1) next_interval_ms FROM samples WHERE time_ms>? AND rx>=0 AND tx>=0 AND time_ms>start_ms"
                    else:sql=None
                    if sql:
                        index=collections.defaultdict(list)
                        for r in runs:
                            key=(None if service in ('tm','earnfm') else r['group_id'],r['instance'],r['machine'],None if service=='grass' else r['run'],r['ip'])
                            index[key].append(r)
                        def records():
                            if not native:
                                yield from db.execute(sql,(cutoff,));return
                            scopes=dict.fromkeys((r['group_id'],r['instance'],int(r['run'].rsplit('/',1)[1])) for r in runs if r['last_seen_ms']>cutoff)
                            rewind=cutoff<state['watermark']-3*HOUR
                            for key in scopes:
                                seqs=state['sequences'].get(key,{})
                                seq=0 if rewind else min((seq for seq,end in seqs.items() if end>cutoff),default=0)
                                yield from db.execute(sql,(*key,seq,cutoff))
                        for p in records():
                            if native:
                                seq_key=(p['group_id'],p['instance'],p['observer'])
                                state['sequences'].setdefault(seq_key,{})[p['seq']]=p['end_ms']
                            candidates=index.get((p['group_id'],p['instance'],p['machine'],p['run'],p['ip']),[])
                            start=p['at'] if cached else p['start_ms'];end=start+MINUTE if cached else p['end_ms']
                            matching=[r for r in candidates if r['first_seen_ms']<end and r['last_seen_ms']>start]
                            # Minutes without a unique run are retained as gaps.
                            if len(matching)!=1:continue
                            if cached:
                                r=matching[0];begin=max(start,r['first_seen_ms']);stop=min(end,r['last_seen_ms'])
                                if 0<p['ms']<=min(MINUTE,stop-begin)+1:add(r,start,p['mb'],p['ms'],p['estimated'],begin,stop)
                                elif service=='grass' and 0<p['ms']<=MINUTE:
                                    # The minute cache may be newer than this episode.
                                    # Clip native intervals, not the aggregate byte rate;
                                    # retain the unique-run and overfull-minute guards.
                                    recovered=_grass_boundary_minute(db,r,begin,stop)
                                    if recovered:
                                        mb,ms,left,right=recovered
                                        add(r,start,mb,ms,True,left,right)
                            else:
                                if service=='tm':
                                    wall=end-start;mono=p['interval_ms'];following=p['next_interval_ms']
                                    if wall>mono+1 and following is not None and wall>mono+following+1:
                                        # Match the recorder's clock-discontinuity bound.
                                        start=end-min(wall,mono+1)
                                    elif wall>mono+1 and following is None:
                                        continue  # Await the following interval before asserting coverage.
                                interval(matching[0],start,end,p['amount'],p['estimated'])
            state['stamp']=stamp;state['rows']=snapshots;state['source_cursor']=source_high
            state['watermark']=max((r['last_seen_ms'] for r in runs),default=0)
            for seqs in state['sequences'].values():
                for seq,end in list(seqs.items()):
                    if end<state['watermark']-3*HOUR:del seqs[seq]
        output={}
        for r in runs:
            end=r.get('_evaluation_end_ms',r['last_seen_ms']);bins=state['buckets'].get(r['id'],{})
            # Retired metrics need not be resorted every minute. Active metrics
            # use now, so recording gaps lower current coverage immediately.
            cached_result=state['metrics'].get(r['id'])
            if not changed and cached_result and cached_result['window_end_ms']==end:
                output[r['id']]=cached_result;continue
            m=summarize_minutes([(t,*v) for t,v in bins.items()],r['first_seen_ms'],end)
            output[r['id']]=m
            # Keep minute granularity for the current 72-hour display window.
            for at in list(bins):
                if at<end-72*HOUR:del bins[at]
        state['metrics']=output;state['last_changed']=changed;state['last_cutoff']=cutoff;self.cache[service]=state
        return output
