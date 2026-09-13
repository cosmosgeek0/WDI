"use strict";
(() => {
  const valid = value => typeof value === "number" && Number.isFinite(value);
  const formats = new Map();
  const num = (value, digits = 3) => {
    if (!valid(value)) return "Unknown";
    if (!formats.has(digits)) formats.set(digits, new Intl.NumberFormat(undefined, {maximumFractionDigits: digits}));
    return formats.get(digits).format(value);
  };
  const responseAge = (data, now) => {
    const source=data.latest?.hg;
    const at=valid(source?.status_observed_ms)&&valid(source?.status_age_ms)?source.status_observed_ms+source.status_age_ms:data.generated_ms;
    return valid(at)?Math.max(0,now-at):0;
  };
  const fresh = (age, limit) => valid(age) && age >= 0 && age <= limit;
  const CLIENTS = {running: "Accepted", error: "Client error", accepted: "Accepted", connected: "Connected", exited: "Client exited", device_limit: "Device limit", device_already_active: "Device already active", network_overused: "Network overused", network_unusable: "Network unusable", authorization_error: "Authorization failed", connected_last_observed_telemetry_stale: "Connection unverified", connected_last_observed_telemetry_unverified: "Connection unverified", starting: "Starting", waiting: "Waiting", rejected: "Rejected", unknown: "Unknown", unverified: "Unverified", disconnected: "Disconnected", reallocating: "Reallocating"};
  const ERRORS = {device_limit: "Provider device limit reached.", device_already_active: "Provider reports this device is already active.", network_overused: "Provider reports this network is overused.", network_unusable: "Provider cannot use this network.", authorization_error: "Provider authorization failed.", observer_error: "Observer could not verify this container.", disconnected: "Client disconnected; automatic reallocation after 2 minutes without recovery."};
  function workerState(service, worker, data, now=Date.now()) {
    const elapsed=responseAge(data,now);
    const source = data.latest?.[service] || {};
    const native = service === "ps" && (worker.native_status_only === true || ["native_status_only", "native_sysfs_1s", "sysfs_via_salad_logs_1s"].includes(worker.sample_source));
    const observerFresh = !data.recorder_stale && (service !== "hg" || !source.status_stale && fresh(now-source.status_observed_ms,120000)) && (!native || fresh(source.status_age_ms, 90000));
    const clientFresh = observerFresh && fresh(service === "hg" ? (valid(worker.received_age_ms)?worker.received_age_ms+elapsed:null) : worker.client_status_age_ms, native ? 240000 : 120000);
    const knownState = ["running", "stopped", "stopping", "pending", "starting", "allocating", "creating", "deploying", "failed", "succeeded"].includes(worker.state);
    const client = CLIENTS[worker.client_status] || "Unknown";
    const accepted = service === "hg" ? worker.client_status === "connected" : ["accepted", "running"].includes(worker.client_status);
    const bad = ["error", "exited", "device_limit", "device_already_active", "network_overused", "network_unusable", "authorization_error", "rejected"].includes(worker.client_status);
    const working = clientFresh && worker.state === "running" && accepted && !worker.error_code;
    const knownFailure = observerFresh && ((knownState && worker.state !== "running") || (clientFresh && (bad || Boolean(worker.error_code) && worker.error_code !== "observer_error")));
    const networkFresh = observerFresh && (service === "hg" ? fresh(valid(worker.received_age_ms)?worker.received_age_ms+elapsed:null, 120000) : fresh(worker.received_age_ms, ["native_sysfs_1s", "sysfs_via_salad_logs_1s"].includes(worker.sample_source) ? 45000 : worker.sample_source === "salad_logs_10s" ? 25000 : 15000) && valid(worker.source_age_ms) && worker.source_age_ms >= -15000 && worker.source_age_ms <= 60000);
    return {working, unknown: !working && !knownFailure, client, networkFresh, observerFresh, clientFresh,
      label: working ? "Working" : knownFailure ? "Needs attention" : "Unknown",
      tone: working ? "ok" : knownFailure ? "bad" : "warn"};
  }
  function serviceModel(service, data, now=Date.now()) {
    const source = data.latest?.[service] || {};
    const desired = valid(data.experiment?.[service]?.desired) ? data.experiment[service].desired : null;
    const workers = [...(source.workers || [])];
    while (workers.length < desired) workers.push({state: "unknown", client_status: "unknown"});
    const states = workers.map(worker => workerState(service, worker, data, now));
    const providerFresh = !data.recorder_stale && fresh(valid(source.provider_age_ms)?source.provider_age_ms+responseAge(data,now):null, service === "hg" ? 360000 : 180000);
    const earnings = data.summary?.[service === "hg" ? "hg_gathering_credits" : "ps_credited_mb"];
    const network = data.summary?.[`${service}_network_mb`];
    const networkKnown = !data.recorder_stale && (service !== "hg" || !source.status_stale) && source.network_complete === true && states.length >= desired && states.every(s => s.networkFresh) && valid(network);
    return {source, desired, workers, states, working: states.filter(s => s.working).length, unknown: states.filter(s => s.unknown).length,
      providerFresh, earnings, earningsKnown: providerFresh && valid(earnings), network, networkKnown};
  }
  function chartValue(metric) {
    if (!metric || !valid(metric.delta) || ["counter_reset", "gap"].includes(metric.status) || (metric.status === "incomplete" && metric.delta === 0)) return null;
    return metric.delta;
  }
  function windowStart(service, data) {
    const value = data.service_windows?.[service]?.start_ms;
    return valid(value) ? value : data.experiment?.started_ms;
  }
  function displayedIntervals(service, kind, data) {
    const start = windowStart(service, data);
    return (data.timeline?.[service]?.[kind] || []).filter(p => !valid(start) || p.start_ms >= start);
  }
  const SERVICES=[
    {id:'hg',path:'/honeygain',hash:'#hg1',name:'Honeygain',url:'/api/fleet-test?service=hg'},
    {id:'ps',path:'/packetstream',hash:'#ps1',name:'PacketStream',url:'/api/ps1/minutes'},
    {id:'proxyrack',path:'/proxyrack',hash:'#proxyrack',name:'Proxyrack',url:'/api/proxyrack'},
    {id:'rp',path:'/repocket',hash:'#rp',name:'Repocket',url:'/api/repocket?chart=1'},
    {id:'earnfm',path:'/earnfm',hash:'#earnfm',name:'EarnFM',url:'/api/earnfm'},
    {id:'tm',path:'/traffmonetizer',hash:'#tm',name:'TraffMonetizer',url:'/api/traffmonetizer?chart=1'},
    {id:'pawns',path:'/pawns-app',hash:'#pawns',name:'Pawns.app',url:'/api/pawns'},
    {id:'grass',path:'/grass',hash:'#grass',name:'Grass',url:'/api/grass'}
  ];
  const aliases={'/rp':'rp','/tm':'tm','/pawns':'pawns'};
  function pageService(path,hash){const clean=path.replace(/\/$/,'').replace(/\.html$/,'');return SERVICES.find(s=>s.hash===hash)||SERVICES.find(s=>s.path===clean||s.id===aliases[clean]);}
  const shown=(value,digits=3)=>valid(value)?num(value,digits):'—';
  const mb=(value,digits=3)=>valid(value)?shown(value,digits)+' MB':'Not reported';
  const usd=value=>valid(value)?'$'+shown(value,6):'Not reported';
  const prNumber=value=>!valid(value)?'—':value>0&&value<0.000001?value.toExponential(2):shown(value,value>0&&value<0.01?6:3);
  const prMB=value=>valid(value)?prNumber(value)+' MB':'Not reported';
  const diff=(end,start)=>valid(end)&&valid(start)&&end>=start?end-start:null;
  const normalizeMinute=(time,amount,nodes=[],partial=false,estimated=false)=>({minute_ms:time,mb:valid(amount)?amount:null,partial,estimated,count:nodes.length,nodes:[...nodes].sort((a,b)=>b.mb-a.mb).slice(0,20)});
  function normalize(service,d,now=Date.now(),billing=null){
    if(Array.isArray(d.chart_nodes))for(const key of ['hg_minutes','candles','minutes','chart_minutes'])for(const minute of d[key]||[])if(Array.isArray(minute.contributions)&&!minute.nodes)minute.nodes=minute.contributions.map(([index,amount])=>({...d.chart_nodes[index],mb:amount}));
    const result={at:now,statusFresh:true,name:SERVICES.find(s=>s.id===service)?.name,desired:null,running:null,working:0,total:null,start:null,source:d.generated_ms,credited:'Not reported',earned:'Not reported',creditNote:'',notice:'',nodes:[],minutes:[],estimated:false};
    const delay=valid(d.generated_ms)?now-d.generated_ms:Infinity,recordingFresh=delay>=-10000&&delay<=90000;
    if(service==='hg'){
      const m=serviceModel('hg',d,now),s=m.source;
      result.desired=m.desired;result.running=s.running;result.working=m.working;result.total=valid(s.network_total_bytes)?s.network_total_bytes/1e6:m.network;result.start=windowStart('hg',d);result.source=s.status_observed_ms;result.minutes=d.hg_minutes||[];result.estimated=true;result.statusFresh=Boolean(d.latest?.hg)&&!d.recorder_stale&&!s.status_stale&&fresh(now-s.status_observed_ms,120000)&&fresh(valid(d.recorder_age_ms)?d.recorder_age_ms+responseAge(d,now):null,180000);
      const intervals=displayedIntervals('hg','provider',d).filter(p=>valid(p.per_second)&&chartValue(p)!==null),credits=intervals.length?intervals.reduce((sum,p)=>sum+p.delta,0):null;
      result.credited=valid(credits)?shown(credits)+' credits':'Not reported';result.earned=valid(credits)?shown(credits)+' credits':'Not reported';
      result.creditNote='Honeygain gathering credits from recorded intervals'+(m.providerFresh?'':'; provider reading is stale');
      if(!result.statusFresh)result.notice='Honeygain recorder or client status is stale.';
      result.nodes=m.workers.map((w,i)=>({id:w.run||w.machine||w.group||String(i),ip:w.ip,country:w.country,status:m.states[i].clientFresh?m.states[i].client:'Unverified',salad:w.state,working:m.states[i].working,
        rate:m.states[i].networkFresh&&valid(w.network_interval?.per_second)?w.network_interval.per_second*60/1e6:null,total:valid(w.network_bytes)?w.network_bytes/1e6:null,
        duration:diff(w.network_last_ms,w.network_first_ms),age:valid(w.received_age_ms)?w.received_age_ms+responseAge(d,now):null,error:[ERRORS[w.error_code],w.recovery_note].filter(Boolean).join(' · ')}));
    }else if(service==='ps'){
      result.desired=valid(d.desired)?d.desired:null;result.running=(d.nodes||[]).filter(n=>n.salad_state==='running').length;result.working=d.running;result.total=d.total_mb;result.start=d.started_ms;result.source=valid(d.inventory_age_ms)?d.time_ms-d.inventory_age_ms:null;result.minutes=d.candles||[];result.estimated=result.minutes.some(m=>m.estimated);result.statusFresh=d.inventory_fresh&&fresh(now-result.source,120000);
      result.notice=d.collector?.error||(!result.statusFresh?'PacketStream inventory is stale.':'');
      result.nodes=(d.nodes||[]).map(n=>({id:n.instance,ip:n.ip,country:n.country,status:n.status,salad:n.salad_state,working:n.status==='Running',rate:n.metrics_fresh&&valid(n.sample_age_ms)&&fresh(n.sample_age_ms+Math.max(0,now-d.time_ms),150000)?n.latest_mb_per_minute:null,total:n.total_mb,duration:valid(n.elapsed_seconds)?n.elapsed_seconds*1000:null,age:valid(n.sample_age_ms)?n.sample_age_ms+Math.max(0,now-d.time_ms):null,error:n.error||''}));
      const provider=billing?.provider,providerFresh=provider&&!provider.error&&fresh(now-provider.observed_ms,180000);
      result.credited=valid(provider?.rolling_bytes)?mb(provider.rolling_bytes/1e6):'Not reported';
      result.balance=valid(provider?.balance_usd)?usd(provider.balance_usd):'Not reported';
      if(!providerFresh){if(result.credited!=='Not reported')result.credited+=' (stale)';if(result.balance!=='Not reported')result.balance+=' (stale)';}
      result.creditNote='PacketStream account balance and rolling 14-day traffic. Neither is an interval earning.'+(provider?.observed_ms?' Observed '+new Date(provider.observed_ms).toLocaleString()+'.':'')+(!providerFresh?' Provider reading is stale or unavailable.':'');
    }else if(service==='proxyrack'){
      const inventoryFresh=recordingFresh&&valid(d.inventory_ms)&&now-d.inventory_ms>=-10000&&now-d.inventory_ms<=90000&&!d.cloud_error;
      result.desired=valid(d.requested)?d.requested:null;result.running=inventoryFresh?(d.workers||[]).filter(w=>w.salad_state==='running').length:null;
      result.total=valid(d.total_recorded_mb)?d.total_recorded_mb:d.network_mb;result.start=Date.parse(d.started_at);result.source=valid(d.inventory_ms)?d.inventory_ms:null;result.recorder=d.generated_ms;
      result.minutes=(d.minutes||[]).map(m=>({...m,partial:m.partial||m.count<result.desired}));result.statusFresh=inventoryFresh;
      result.credited=valid(d.provider_bandwidth_value)?String(d.provider_bandwidth_value):'Not reported';result.earned=d.balance!=null&&d.balance!==''?String(d.balance)+(valid(d.balance_checked_ms)&&now-d.balance_checked_ms>=-10000&&now-d.balance_checked_ms<=900000&&!d.balance_error?'':' (stale)'):'Not reported';result.creditNote=(recordingFresh?'':'Last saved: ')+(d.credit_note||'Waiting for PR provider and recovery observations.');
      result.notice=d.cloud_error||(!recordingFresh?'PR recorder is stale.':!inventoryFresh?'PR inventory is stale.':d.backfilling?'PR is backfilling retained history; totals are incomplete until catch-up finishes.':'');
      result.nodes=(d.workers||[]).map(w=>{
        const sourceAge=valid(w.last_sample_ms)?now-w.last_sample_ms:null;
        const recent=inventoryFresh&&valid(sourceAge)&&sourceAge>=-10000&&sourceAge<=150000;
        return {id:w.instance||w.name,ip:w.ip,country:w.country,status:recent?(w.status||'Unverified'):'Unverified',salad:w.salad_state,
          working:recent&&w.connected===true&&w.salad_state==='running',rate:recent&&w.fresh&&w.salad_state==='running'?w.mb_per_minute:null,total:w.network_mb,
          duration:w.recorded_duration_ms,age:sourceAge,receivedAge:valid(w.recorded_ms)?now-w.recorded_ms:null,error:(recent?'':'Last saved: ')+(w.status_note||'Waiting for the recorded recovery policy.')};
      });result.working=result.nodes.filter(n=>n.working).length;
    }else if(service==='rp'){
      const inventoryFresh=recordingFresh&&valid(d.inventory_ms)&&fresh(now-d.inventory_ms,90000)&&!d.error;
      result.desired=valid(d.desired)?d.desired:null;result.running=inventoryFresh?d.running:null;result.total=d.total_network_mb;result.start=d.test_started_ms;result.source=d.inventory_ms||d.generated_ms;result.statusFresh=inventoryFresh;
      result.notice=d.error||(!recordingFresh?'Repocket recorder is stale.':!inventoryFresh?'Repocket inventory is stale.':'');
      result.nodes=(d.workers||[]).filter(w=>w.current!==false).map((w,i)=>{
        const native=inventoryFresh&&w.native_fresh===true&&fresh(now-w.source_ms,90000),network=native&&w.network_fresh===true&&fresh(now-w.last_end_ms,150000),policy=w.policy;
        const policyFresh=native&&w.diagnostics_fresh===true&&fresh(now-policy?.checked_ms,90000),notes=[];
        let status=inventoryFresh&&w.salad_state&&w.salad_state!=='running'?w.salad_state:native?w.status:'Unverified';
        if(status==='connected')status='Connected (control)';
        if(w.reason&&w.reason!=='Waiting for fresh native evidence')notes.push(w.reason);
        if(policyFresh&&policy.state==='observing')notes.push('Observing '+shown(policy.observed_ms/60000,1)+'/10 min');
        else if(policyFresh&&policy.state==='keeping')notes.push('Keeping: '+shown(policy.target_bytes/1e6)+' MB target traffic / 10 min');
        else if(status==='reallocating')notes.push('Replacement requested');
        else if(w.salad_state==='running')notes.push('Recovery decision unverified');
        if(policyFresh){const errors=Object.entries(policy.errors||{}).filter(([,n])=>n>0).sort((a,b)=>b[1]-a[1]).slice(0,2);if(errors.length)notes.push(errors.map(([name,n])=>name+': '+n).join('; '));}
        return {id:w.salad_instance||w.instance||'awaiting-'+i,ip:w.ip,country:w.country,status,salad:w.salad_state,working:native&&w.phase==='connected'&&w.salad_state==='running',rate:network?w.last_mb_per_minute:null,total:w.total_mb,duration:w.recorded_ms,age:diff(now,w.source_ms),error:notes.join(' · ')};
      });
      result.working=inventoryFresh&&valid(d.connected)?result.nodes.filter(n=>n.working).length:null;
      result.minutes=d.chart_minutes||[];
      const provider=d.provider,providerFresh=provider&&!provider.error&&fresh(now-provider.observed_ms,180000);
      if(providerFresh){result.earned=provider.today_earned_text||provider.earned_text||'Not reported';result.credited=provider.today_traffic_text||provider.traffic_text||'Not reported';}
      result.creditNote='RP records original source, Salad receipt and recorder receipt times. Network totals include both directions and overhead; provider credits are separate account readings. '+(providerFresh?'Provider observed '+new Date(provider.observed_ms).toLocaleString():provider?.observed_ms?'Provider reading unavailable now; last observed '+new Date(provider.observed_ms).toLocaleString()+' ('+(provider.today_traffic_text||provider.traffic_text||'traffic unavailable')+' / '+(provider.today_earned_text||provider.earned_text||'earnings unavailable')+').':'Provider credit feed has not supplied a reading.');
      result.creditNote+=' '+shown(d.record_count,0)+' records · '+mb(d.database_bytes/1e6)+' on disk · '+shown(d.saved_bytes_last_minute/1000)+' KB saved in the last minute.';
      if(d.legacy_receipt_unknown)result.creditNote+=' '+shown(d.legacy_receipt_unknown,0)+' historical records have unknown receipt times.';
      if(d.invalid_records)result.creditNote+=' '+shown(d.invalid_records,0)+' invalid/conflicting records are excluded from measured totals.';
      const action=d.recent_recovery?.[0];if(action)result.creditNote+=' Recovery '+(action.ip||'worker')+': '+({old_instance_removed:'old machine left inventory',accepted:'Salad accepted request; removal not yet verified',request_failed:'request failed',requested:'request sent'}[action.result]||'unverified')+' · '+action.reason+'. Fleet now '+shown(result.working,0)+'/'+result.desired+' confirmed. New instances are confirmed from fresh native records.';
    }else if(service==='tm'){
      result.desired=valid(d.deployment?.desired_replicas)?d.deployment.desired_replicas:null;result.running=d.inventory_fresh?d.running:null;result.total=valid(d.recorded?.rx)&&valid(d.recorded?.tx)?(d.recorded.rx+d.recorded.tx)/1e6:null;result.start=d.deployment?.measurement_started_ms||Date.parse(d.deployment?.started_at);result.source=d.inventory_ms;result.statusFresh=recordingFresh&&!d.stale&&d.inventory_fresh&&fresh(now-d.inventory_ms,90000)&&!d.errors?.recorder;
      const paidFresh=d.credited?.fresh&&fresh(now-d.credited.observed_ms,180000);
      result.credited=mb(valid(d.credited?.credited_bytes)?d.credited.credited_bytes/1e6:null);result.earned=usd(d.credited?.credited_usd);
      if(d.credited&&!paidFresh){result.credited+=' (stale)';result.earned+=' (stale)';}
      result.creditNote=(d.credited?.scope||'TM provider account figures')+(paidFresh?'':' Provider reading is stale or unavailable.');
      result.notice=Object.values(d.errors||{}).join(' · ')||(!recordingFresh||d.stale?'Traffic Monetizer recorder is stale.':!d.inventory_fresh?'Salad inventory is stale.':'');
      result.nodes=(d.workers||[]).map(w=>{
        const recent=recordingFresh&&!d.stale&&w.fresh&&fresh(now-w.time_ms,45000)&&fresh(now-w.received_ms,45000),connected=recent&&w.connection_fresh,notes=[];
        if(w.error)notes.push(w.error);
        if(!recent)notes.push(w.received_ms?'Measurement or receipt is stale':'Receipt time has not been recorded');
        if(valid(w.source_receipt_difference_ms)&&Math.abs(w.source_receipt_difference_ms)>30000)notes.push('Source/receipt time differ by '+shown(w.source_receipt_difference_ms/1000,0)+'s');
        if(recent&&valid(w.native_recent_bytes))notes.push('Relay observed: '+mb(w.native_recent_bytes/1e6)+' / '+shown(w.native_recent_requests,0)+' requests over '+shown(w.native_recent_ms/60000,1)+' min');
        const policy=w.policy;
        if(recent&&policy){notes.push(policy.state==='reallocating'?'Automatic replacement requested':policy.state==='stopped'?'Client stopped':policy.state==='waiting_connection'?'Connection recovery: '+shown(policy.unavailable_seconds,1)+'/'+shown(policy.failure_limit_seconds,0)+' unavailable seconds':policy.reallocate_on_idle===false?'Automatic failure recovery active':valid(policy.payload_remaining_seconds)?'No-payload replacement in '+shown(Math.max(0,policy.payload_remaining_seconds-Math.max(0,now-w.time_ms)/1000)/60,1)+' min if payload remains unchanged':'Recovery policy is incomplete');}
        else notes.push('Recovery policy has no fresh reading');
        return {id:w.instance,ip:w.ip,country:w.country,status:connected?w.state:'Unverified',salad:w.salad_state,working:connected&&w.working,rate:recent?w.raw_mb_per_minute:null,total:valid(w.test_rx)&&valid(w.test_tx)?(w.test_rx+w.test_tx)/1e6:null,duration:w.test_duration_ms,age:diff(now,w.time_ms),error:notes.join('; ')};
      });
      result.working=result.nodes.filter(n=>n.working).length;
      result.minutes=d.chart_minutes||[];
      const action=d.recent_recovery?.[0];
      if(action){const outcomes={request_failed:'request failed; container exit fallback',awaiting_response:'awaiting IMDS response',inventory_unverified:'replacement outcome unverified',awaiting_retirement:'accepted; old instance still listed',replacement_pending:'old instance left; replacement fleet pending',fleet_restored:'old instance left; full fleet confirmed'};result.creditNote+=' Last recovery: '+new Date(action.time_ms).toLocaleString()+' · '+action.instance.slice(0,8)+' · '+action.reason+(action.http_status?' · IMDS HTTP '+action.http_status:action.accepted===false?' · IMDS request failed':'')+' · '+(outcomes[action.outcome]||action.outcome)+(valid(action.fleet_confirmed_ms)?' at '+new Date(action.fleet_confirmed_ms).toLocaleString():'')+'.';}
      if(valid(d.chart_generated_ms))result.creditNote+=' Chart recorded through '+new Date(d.chart_generated_ms).toLocaleTimeString()+'.';
    }else if(service==='earnfm'){
      const inventoryFresh=recordingFresh&&fresh(now-d.inventory_ms,90000),providerFresh=!d.provider?.error&&fresh(now-(d.provider?.devices_observed_ms??d.provider?.observed_ms),180000);
      result.desired=valid(d.desired)?d.desired:null;result.running=inventoryFresh?(d.workers||[]).filter(w=>w.salad_state==='running').length:null;result.total=d.total_mb;result.start=d.test_started_ms;result.source=d.inventory_ms;result.statusFresh=inventoryFresh&&!d.error;result.notice=d.error||(!recordingFresh?'EarnFM recorder is stale.':!inventoryFresh?'EarnFM inventory is stale.':'');
      if(d.collection?.rejected)result.notice+=(result.notice?' · ':'')+shown(d.collection.rejected,0)+' malformed telemetry records saved separately; valid recording continues.';
      result.nodes=(d.workers||[]).map(w=>{
        const recent=inventoryFresh&&!d.error&&fresh(now-w.sample_ms,90000)&&fresh(now-w.recorded_ms,90000),h=w.health||{},runtime=w.runtime_instance||w.instance;
        const present=Array.isArray(d.provider?.devices)?d.provider.devices.some(device=>device.instance===runtime):w.provider_online===true?true:null;
        const working=recent&&!w.final&&w.client_state==='running'&&w.salad_state==='running'&&providerFresh&&present===true;
        const names={authorization_error:'Account key rejected',provider_rejected:'Provider rejected',starting:'Starting',connecting:'Connecting',connection_error:'Connection errors',rate_limited:'Provider cooldown',observer_unknown:'Connection unverified',retrying:'Restarting',awaiting_sample:'Awaiting first sample'};
        let status=names[w.client_state]||w.client_state,notes=[];
        if(w.salad_state&&w.salad_state!=='running')status=w.salad_state;
        else if(!recent)status='Unverified';
        else if(w.client_state==='reallocating')status='Reallocating';
        else if(w.final)status='Stopped';
        else if(w.client_state==='running')status=!providerFresh?'Provider status unavailable':present===false?'Provider offline':working?'Provider online':'Awaiting provider confirmation';
        if(w.error)notes.push(w.error);
        if(w.telemetry_note)notes.push(w.telemetry_note);
        if(!w.ip&&w.last_provider_ip&&valid(w.last_provider_seen_ms))notes.push('Last provider IP observed '+new Date(w.last_provider_seen_ms).toLocaleString());
        if(recent&&valid(h.retry_at_ms)&&h.retry_at_ms>now)notes.push('Retry '+new Date(h.retry_at_ms).toLocaleString());
        if(recent&&h.error&&h.error!=='none')notes.push(h.error.replaceAll('_',' '));
        if(recent&&h.activity_min_bytes===0&&w.client_state==='running')notes.push('No traffic-based restart');
        if(recent&&h.activity_observing===0&&h.activity_min_bytes!==0&&w.client_state==='running')notes.push('Activity check starts after 60 seconds of control connection warmup');
        if(recent&&h.activity_observing!==0&&h.activity_window_ms&&valid(h.activity_min_bytes)&&h.activity_min_bytes>0&&w.client_state==='running')notes.push('Activity check '+shown(h.activity_elapsed_ms/60000,1)+'/'+shown(h.activity_window_ms/60000,0)+' min: '+mb(h.activity_bytes/1e6)+' RX + TX; below '+mb(h.activity_min_bytes/1e6)+' restarts once, then replaces after another full low window');
        if(!providerFresh&&d.provider?.error)notes.push(d.provider.error);
        return {id:runtime,ip:w.ip,country:w.country,status,salad:w.salad_state,working,rate:recent&&!w.final&&valid(w.kb_per_second)&&w.salad_state==='running'?w.kb_per_second*.06:null,total:w.total_mb,duration:w.recorded_duration_ms,age:diff(now,w.sample_ms),error:notes.join(' · ')};
      });result.working=result.nodes.filter(n=>n.working).length;
      result.minutes=(d.points||[]).map(m=>{
        const nodes=Array.isArray(m.contributions)?m.contributions.map(([index,amount])=>({...d.history_nodes?.[index],mb:amount})):Object.entries(m.by_instance||{}).map(([id,amount])=>({instance:id,ip:(d.workers||[]).find(w=>(w.runtime_instance||w.instance)===id)?.ip,country:(d.workers||[]).find(w=>(w.runtime_instance||w.instance)===id)?.country,mb:amount}));
        return normalizeMinute(m.time_ms,m.mb,nodes,m.partial,m.estimated);
      });
      const creditFresh=!d.provider?.error&&fresh(now-d.provider?.observed_ms,180000);
      result.earned=usd(d.provider?.earned);result.credited=mb(d.provider?.credited_mb);
      if(!creditFresh){if(result.earned!=='Not reported')result.earned+=' (stale)';if(result.credited!=='Not reported')result.credited+=' (stale)';}
      result.creditNote='Recorded traffic is interface RX + TX, including relay crossings and overhead. Provider online confirms the device listing, not paid traffic. Credits and gross balance change cover the EarnFM account since '+(valid(d.provider?.baseline_ms)?new Date(d.provider.baseline_ms).toLocaleString():'its saved baseline')+'; per-device earnings are not reported.';
      if(valid(d.provider?.observed_ms))result.creditNote+=' Provider observation: '+new Date(d.provider.observed_ms).toLocaleString()+(creditFresh?'':' (stale)')+'.';
      if(d.provider_history?.observations)result.creditNote+=' '+shown(d.provider_history.observations,0)+' provider observations saved.';
      const action=d.recent_recovery?.[0];
      if(action)result.creditNote+=' Latest recovery: '+(action.machine?.slice(0,8)||'worker')+' · '+new Date(action.time_ms).toLocaleString()+' · '+action.action.replaceAll('_',' ')+' ('+action.reason.replaceAll('_',' ')+')'+(action.http_status?' · HTTP '+action.http_status:'')+(action.action==='reallocate_accepted'?' · '+(inventoryFresh?(action.old_instance_present?'old allocation still present':'old allocation removed; current provider-confirmed fleet '+result.working+'/'+result.desired):'inventory stale; removal unverified'):'')+'.';
    }else if(['pawns','earnapp','grass'].includes(service)){
      const inventoryFresh=recordingFresh&&valid(d.inventory_ms)&&fresh(now-d.inventory_ms,90000);
      result.desired=valid(d.desired)?d.desired:null;result.running=valid(d.running)?d.running:null;result.working=valid(d.working)?d.working:null;
      result.total=valid(d.total_mb)?d.total_mb:null;result.start=d.started_ms;result.source=d.inventory_ms;result.statusFresh=inventoryFresh;
      result.credited=d.credited_text||'Not reported';result.earned=d.earned_text||'Not reported';result.creditNote=d.credit_note||'Provider credited traffic and earnings have not been reported.';
      if(service==='grass'){result.working=valid(d.confirmed)?d.confirmed:valid(d.working)?d.working:null;result.nativeConnected=d.native_connected;result.nativeKnown=d.native_known;result.confirmedKnown=d.confirmed_known;result.supervisor=d.supervisor;result.points=d.provider_points;result.registration=d.account_registration;}
      if(service==='earnapp'){result.working=valid(d.confirmed)?d.confirmed:null;result.confirmedPartial=d.confirmed_partial;result.providerOnlineKnown=d.provider_online_known;result.providerOnlinePartial=d.provider_online_partial;result.nativeRegistered=d.native_registered;result.providerLinked=d.provider_linked;result.providerOnline=d.provider_online;result.statusFresh=recordingFresh&&(d.sources?.length?d.sources.some(s=>valid(s.inventory_ms)&&fresh(now-s.inventory_ms,90000)&&!s.error):inventoryFresh);result.basis=d.basis||'';result.received=valid(d.received_mb)?d.received_mb:null;result.sent=valid(d.sent_mb)?d.sent_mb:null;}
      if(!recordingFresh){if(result.credited!=='Not reported')result.credited+=' (stale)';if(result.earned!=='Not reported')result.earned+=' (stale)';}
      result.notice=d.error||(!valid(d.generated_ms)?'Waiting for '+result.name+' measurements.':!recordingFresh?result.name+' recorder is stale.':!inventoryFresh?result.name+' inventory is stale.':'');
      result.nodes=(d.workers||[]).map(w=>{
        const sourceFresh=['earnapp','grass'].includes(service)&&d.sources?.length?recordingFresh&&valid(w.inventory_ms)&&fresh(now-w.inventory_ms,90000)&&!w.source_error:inventoryFresh&&!d.error;
        const recent=sourceFresh&&(service==='earnapp'?valid(w.sample_expires_ms)&&valid(w.sample_ms)&&w.sample_ms<=now+10000&&now<=w.sample_expires_ms:valid(w.sample_ms)&&fresh(now-w.sample_ms,90000)&&valid(w.recorded_ms)&&fresh(now-w.recorded_ms,90000));
        const healthFresh=service!=='grass'||valid(w.health_ms)&&fresh(now-w.health_ms,90000);
        return {id:w.id,ip:w.ip,country:w.country,service,nativeConnected:service==='grass'&&recent&&healthFresh?w.native_connected:null,displayStatus:service==='grass'?(sourceFresh?(w.salad_state==='running'?(recent?(healthFresh?w.client_status:'Provider status delayed'):w.sample_ms?'Source delayed':'Awaiting native sample'):w.salad_state?.replace(/^./,c=>c.toUpperCase())):'Source delayed'):null,unknown:service==='pawns'&&(w.working===null||!recent),status:recent?(service==='pawns'?(w.client_status==='running'?'SDK running':CLIENTS[w.client_status]||w.client_status):w.client_status):'Unverified',salad:w.salad_state,
          working:recent&&healthFresh&&w.working===true&&(service==='earnapp'||w.salad_state==='running')&&!w.error,
          rate:recent&&(service==='earnapp'||w.salad_state==='running')&&valid(w.mb_per_minute)?w.mb_per_minute:null,total:w.total_mb,duration:valid(w.recorded_duration_ms)?w.recorded_duration_ms:service==='earnapp'?diff(w.sample_ms,w.observer_ms):null,age:valid(w.sample_ms)?diff(now,w.sample_ms):null,error:w.error||'',note:w.note||''};
      });
      if(service==='grass'){result.nativeKnown=result.nodes.filter(n=>typeof n.nativeConnected==='boolean').length;result.nativeConnected=result.nativeKnown?result.nodes.filter(n=>n.nativeConnected===true).length:null;}
      result.minutes=(d.minutes||[]).map(m=>({...m,nodes:[...(m.nodes||[])].sort((a,b)=>b.mb-a.mb)}));
    }
    if(d.traffic_note)result.notice=[result.notice,d.traffic_note].filter(Boolean).join(' ');
    result.estimated=result.estimated||result.minutes.some(m=>m.estimated);
    if(result.statusFresh){
      for(const node of result.nodes){
        if(service==='proxyrack')continue;
        const state=node.salad?.toLowerCase();
        if(service==='earnapp'&&state==='creating')continue;
        if(['allocating','downloading','creating','stopping','stopped'].includes(state)){
          node.status=state[0].toUpperCase()+state.slice(1);node.working=false;node.rate=null;
          if(['allocating','downloading','creating'].includes(state))node.error='Waiting for the client to start on this machine.';
        }
      }
      if(result.working!==null)result.working=result.nodes.filter(node=>node.working).length;
      while(result.nodes.length<result.desired)result.nodes.push({service,id:service+'-pending-'+result.nodes.length,pending:true,status:'Awaiting inventory',working:false,error:'No machine listed in the latest inventory.'});
    }
    if(!result.statusFresh){result.working=null;result.running=null;if(service==='grass'){result.nativeConnected=null;result.nativeKnown=null;result.confirmedKnown=null;}result.nodes=result.nodes.map(n=>({...n,working:false,status:"Unverified",stale:true,rate:null}));}
    result.providerMetrics=d.provider_metrics||null;result.trafficSummary=d.traffic_summary||null;result.costs=d.costs||null;
    const sourceNodes=service==='hg'?d.latest?.hg?.workers:service==='ps'?d.nodes:d.workers,costById=new Map();
    for(const worker of sourceNodes||[])if(worker.cost)for(const key of ['id','run','instance','salad_instance','runtime_instance','machine','group','name'])if(worker[key])costById.set(worker[key],worker.cost);
    for(const node of result.nodes)node.cost=costById.get(node.id)||null;
    return result;
  }
  function periodMinutes(minutes,hours,now=Date.now()){
    const end=Math.floor(now/60000)*60000,start=end-(hours*60-1)*60000,byTime=new Map(minutes.map(m=>[m.minute_ms,m]));
    return Array.from({length:hours*60},(_,i)=>byTime.get(start+i*60000)||{minute_ms:start+i*60000,mb:null,partial:true,count:0,nodes:[]});
  }
  function grassRegistration(value,now=Date.now(),formatDate=at=>valid(at)?new Date(at).toISOString():'Unknown'){
    const r=value||{},count=v=>Number.isSafeInteger(v)&&v>=0&&r.account_matches===true?v:null;
    const registered=count(r.device_count),cap=count(r.device_cap),known=r.valid===true&&registered!==null&&cap!==null;
    const current=known&&fresh(valid(r.observed_ms)?now-r.observed_ms:null,180000);
    const state=known?(current?'':' · stale'):' · unknown';
    const remaining=known?(registered>cap?num(registered-cap,0)+' over cap':num(Math.max(cap-registered,0),0)+' remaining'):'Remaining unknown';
    const reasons={no_baseline:'partial history',account_change:'account changed',unavailable_observation:'unavailable observation',cap_change:'cap changed',count_decrease:'registered count decreased',observation_gap:'observation gap'};
    const growth=['hour','day'].map((name,i)=>{
      const g=r.growth?.[name],label=i?'24h':'1h';
      if(!g||!valid(g.delta))return label+' growth unknown'+(g?.boundary?' ('+(reasons[g.boundary]||'missing baseline')+')':'');
      return label+' observed growth +'+num(g.delta,0)+(g.complete?'':' · partial')+' ('+num(g.start_count,0)+' → '+num(g.end_count,0)+'; '+formatDate(g.start_ms)+' → '+formatDate(g.end_ms)+'; '+num(g.covered_ms/60000,1)+'m coverage'+(g.boundary?'; '+(reasons[g.boundary]||'partial history')+(valid(g.boundary_ms)?' at '+formatDate(g.boundary_ms):''):'')+')';
    }).join(' · ');
    const error=r.error_code?' · '+r.error_code.replaceAll('_',' ')+(r.http_status?' (HTTP '+r.http_status+')':''):'';
    const detail='Registered devices: '+num(registered,0)+' / '+num(cap,0)+' · '+remaining+state+' · Account '+(r.account_id||'Unknown')+' · retrieveUser observed '+formatDate(r.observed_ms)+'; received '+formatDate(r.received_ms)+error+'. '+growth+'. Counts are account registrations, separate from active nodes. Changes are observed net count growth; registration times and expired-device reclamation are not supplied.';
    return {label:'Registered devices'+state,value:num(registered,0)+' / '+num(cap,0),remaining:(known&&!current?'Last: ':'')+remaining,detail};
  }
  if(typeof module!=='undefined'&&module.exports){module.exports={workerState,serviceModel,chartValue,windowStart,displayedIntervals,normalize,periodMinutes,SERVICES,pageService,prNumber,prMB,grassRegistration};return;}
  const el=id=>document.getElementById(id);
  const displayMB=window.formatBandwidth;
  const make=(tag,text,cls)=>{const node=document.createElement(tag);if(text!=null)node.textContent=text;if(cls)node.className=cls;return node;};
  const duration=ms=>!valid(ms)?'Not recorded':ms<60000?shown(ms/1000,0)+'s':Math.floor(ms/3600000)+'h '+Math.floor(ms%3600000/60000)+'m';
  const age=ms=>!valid(ms)?'Unknown':ms<60000?shown(ms/1000,0)+'s':ms<3600000?shown(ms/60000,1)+'m':shown(ms/3600000,1)+'h';
  const date=window.saladTime.format;
  const providerPeriod=period=>{if(typeof period==='string')return period;if(!period)return '';const dates=[period.first_date||(period.start_approximate?'≈ ':'')+date(period.start_ms),period.last_date||date(period.end_ms)].filter(value=>value&&value!=='Not recorded');return [period.kind?.replaceAll('_',' '),dates.join(' – '),period.timezone,period.stage!=null?'Stage '+period.stage:'',period.epoch,valid(period.covered_ms)?duration(period.covered_ms)+' observed':'',period.partial?'partial':''].filter(Boolean).join(' · ');};
  const buttons=new Map(),feeds=window.bandwidthData,read=feeds.read;
  let warmed=false;
  let active=pageService(location.pathname,location.hash);
  if(!active){location.replace('/overview');return;}
  history.replaceState(null,'',active.path+location.search);
  let rowRender=0;
  let hours=24,data=null,model=null,busy=false,generation=0,billing=null,billingPending=false,billingChecked=0;
  let nodeSummary=null,nodeRecords=new Map(),ipSummaries=new Map(),summaryKey='',summaryError='',selectedIP=null,ipData=null,ipError='',ipTicket=0,ipRequest=null,ipKey='';
  let historyVisible=false,historyData=null,historyOffset=0,historyTicket=0,historyRequest=null,historyTimer=null,breakdownData=null,breakdownKey='';
  const panel=el('bandwidthPanel');
  panel.innerHTML=`
    <div class="page-head"><h1 id="serviceTitle"></h1><div class="page-actions"><span id="connection" role="status"></span><button id="refresh" class="quiet-button" aria-label="Refresh" title="Refresh"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 11a9 9 0 0 1 15-6l3 3M21 3v5h-5M21 13a9 9 0 0 1-15 6l-3-3M3 21v-5h5"/></svg></button></div></div>
    <div id="totals" class="metrics" aria-label="Service measurements"></div>
    <section class="panel chart-panel corners" aria-labelledby="chartTitle">
      <div class="panel-head"><h2 id="chartTitle">Traffic</h2><span id="chartUnit" class="panel-subtitle">MB</span><div id="ipFilter" class="ip-filter"><strong id="ipFilterLabel" hidden></strong><button id="allIPs" aria-label="Show all IPs">All IPs</button><button id="ipDetails" disabled>Details</button><span id="ipFilterNote" hidden></span></div><div class="chart-controls"><div class="chart-legend"><span><i class="legend-key"></i>Network</span><span id="earnedLegend"><i class="legend-key earned"></i>Credited —</span></div><div id="chartOptions"></div></div></div>
<div class="traffic-layout"><div class="ps-chart"><div id="chartScroll" class="ps-chart-scroll"><canvas id="trafficChart" tabindex="0" role="img"></canvas></div><div id="minutePopup" class="ps-popup hidden" role="tooltip"></div></div></div>

    </section>
    <section class="panel corners" aria-label="Node records">
      <div class="node-head"><div class="view-tabs nav nav-tabs" role="tablist" aria-label="Node views"><button class="nav-link active" id="currentTab" role="tab" aria-selected="true" aria-controls="currentView">Current nodes</button><button class="nav-link" id="breakdownTab" role="tab" aria-selected="false" aria-controls="breakdownView" tabindex="-1">IP breakdown</button><button class="nav-link" id="historyTab" role="tab" aria-selected="false" aria-controls="historyView" tabindex="-1">IP history</button></div><span id="nodeCount" class="view-count" role="status">—</span></div>
      <div id="breakdownView" role="tabpanel" aria-labelledby="breakdownTab" hidden><div class="table-scroll"><table class="breakdown-table"><thead><tr><th>IP</th><th>Country</th><th>Client</th><th>State</th><th class="numeric">Peak MB/min</th><th class="numeric">Period MB</th><th>Minutes</th><th>First seen · IST</th><th>Last seen · IST</th><th>Runs</th></tr></thead><tbody id="ipBreakdown"></tbody></table></div><div class="table-foot"><span id="ipPageCount"></span><nav aria-label="IP pages"><ul class="pagination pagination-sm"><li class="page-item"><button class="page-link" id="ipPrevious" disabled>Previous</button></li><li class="page-item"><button class="page-link" id="ipNext" disabled>Next</button></li></ul></nav></div></div><div id="currentView" role="tabpanel" aria-labelledby="currentTab">
        <div class="table-scroll" tabindex="0" aria-label="Current node measurements"><table class="container-table"><thead><tr><th scope="col">IP</th><th scope="col">Country</th><th scope="col">Client</th><th scope="col">State</th><th scope="col" class="numeric">MB/min</th><th scope="col" class="numeric">Recorded MB</th><th scope="col">Duration</th><th scope="col">Started · IST</th><th scope="col">Runs</th></tr></thead><tbody id="workerRows"></tbody></table></div>
      </div>
      <div id="historyView" role="tabpanel" aria-labelledby="historyTab" hidden>
        <div class="history-controls"><label class="search-field">Find an IP or node<input id="historySearch" type="search" placeholder="Search recorded IP, instance or run" autocomplete="off"></label><label>Period<select id="historyHours"><option value="all" selected>All recorded history</option><option value="selected">Chart period</option></select></label><label>Test<select id="historyTest"><option value="current">Current test</option></select></label><label class="unidentified-toggle"><input id="historyUnidentified" type="checkbox">Include records without an IP</label></div>
        <p id="historyNote" class="history-note" role="status">Reading recorded IP history…</p>
        <div class="table-scroll" tabindex="0" aria-label="Historical node contributions"><table class="history-table"><thead><tr><th scope="col">IP</th><th scope="col">Country</th><th scope="col">Client</th><th scope="col">State</th><th scope="col" class="numeric">Avg MB/min</th><th scope="col" class="numeric">Period MB</th><th scope="col">Duration</th><th scope="col">Started · IST</th><th scope="col">Ended / last seen · IST</th><th scope="col">Runs</th></tr></thead><tbody id="historyRows"></tbody></table></div>
        <div class="table-foot"><span id="historyCount"></span><nav aria-label="History pages"><ul class="pagination pagination-sm"><li class="page-item"><button class="page-link" id="historyPrevious" disabled>Previous</button></li><li class="page-item"><button class="page-link" id="historyNext" disabled>Next</button></li></ul></nav></div>
      </div>
      <details class="provider-note"><summary>Details</summary><p id="notice" class="notice hidden"></p><p id="elapsed" class="source-note"></p><p id="currentNote" class="source-note"></p><p id="supervisorNote" class="source-note" hidden></p><p id="chartNote" class="source-note"></p><p id="creditNote" class="source-note"></p><p id="costNote" class="source-note"></p><a id="earnfmDetail" href="/earnfm/details" hidden>EarnFM detailed records and download</a><details id="originalProvider" hidden><summary>Original provider values</summary><pre id="providerRaw"></pre></details></details>
    </section>
    <dialog id="recordDetail" class="detail-dialog" aria-labelledby="detailTitle"><div class="dialog-head"><div><h2 id="detailTitle">IP record</h2><p id="detailSubtitle"></p></div><button id="closeDetail" aria-label="Close record details">Close</button></div><div id="detailBody" class="dialog-body"></div></dialog>`;
  for(const node of panel.querySelectorAll('button:not(.page-link):not(.nav-link)'))node.classList.add('btn','btn-outline-secondary','btn-sm');
  for(const node of panel.querySelectorAll('select'))node.classList.add('form-select-sm');
  for(const node of panel.querySelectorAll('input:not([type=checkbox])'))node.classList.add('form-control','form-control-sm');
  for(const node of panel.querySelectorAll('table'))node.classList.add('table','table-sm','table-hover');
  for(const [i,service]of SERVICES.entries()){const button=make('button',null,'btn btn-outline-secondary btn-sm'),mark=make('span',['HG','PS','PR','RP','EF','TM','PA','EA','GR'][i],'service-mark');mark.setAttribute('aria-hidden','true');mark.innerHTML=window.serviceIcon(mark.textContent);button.append(mark,make('span',service.name,'service-name'));button.setAttribute('aria-label',service.name);button.title=service.name;button.id='tab-'+service.id;button.setAttribute('role','tab');button.setAttribute('aria-controls','bandwidthPanel');button.onclick=()=>select(service);buttons.set(service.id,button);el('services').append(button);}
  el('services').onkeydown=event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(event.key))return;event.preventDefault();const index=SERVICES.indexOf(active),next=event.key==='Home'?0:event.key==='End'?SERVICES.length-1:(index+(['ArrowLeft','ArrowUp'].includes(event.key)?SERVICES.length-1:1))%SERVICES.length;select(SERVICES[next]);buttons.get(active.id).focus();};

  function identityCell(node,includeCountry=true){
    const cell=make('td');
    if(node.ip){const button=make('button',null,'record-link');button.append(window.bandwidthNodes.label(node,includeCountry));button.setAttribute('aria-label','Show traffic for '+node.ip);button.onclick=()=>selectIP(node.ip);cell.append(button);}
    else{cell.append(make('span',node.pending?'Unassigned':'IP unavailable'));cell.title=node.pending?'Waiting for a machine':('Node '+String(node.id||node.instance||node.run||'unknown'));}
    if(node.id&&!node.pending)cell.title=node.id;return cell;
  }
  function nodeRecord(node){return nodeRecords.get(node.id+'|'+(node.ip||'*'))||null;}
  function usesCell(ip){const item=ipSummaries.get(ip),cell=make('td',item?.uses??(ip?'Unknown':'—'),'numeric');cell.title='Exact count of distinct recorded run IDs in this test; records without a run ID are not counted.';return cell;}
  function timeCell(at,ongoing=false){const cell=make('td',at===undefined&&!nodeSummary?(summaryError?'History unavailable':'Loading…'):(ongoing?'Active · ':'')+date(at),'time-cell');cell.title='Original recorded observation time in India Standard Time; not an inferred boot or shutdown time.';return cell;}
  const sortHosts={};for(const name of ['current','breakdown','history']){sortHosts[name]=make('div',null,'node-sort-controls');sortHosts[name].hidden=name!=='current';el('nodeCount').before(sortHosts[name]);}
  const currentSort=window.createTableSort({table:el('workerRows').closest('table'),controls:sortHosts.current,defaultKey:'duration',onChange:render,columns:[
    {key:'ip',label:'IP',direction:'asc'},{key:'country',label:'Country',direction:'asc',value:n=>window.bandwidthCountries.name(n)==='—'?null:window.bandwidthCountries.name(n)},
    {key:'client',label:'Client',direction:'asc',value:n=>window.bandwidthNodes.status(n)},{key:'salad',label:'State',direction:'asc'},
    {key:'rate',label:'MB/min'},{key:'total',label:'Recorded MB'},{key:'duration',label:'Duration'},
    {key:'started',label:'Started · IST',value:n=>nodeRecord(n)?.first_seen_ms},{key:'uses',label:'Runs',value:n=>ipSummaries.get(n.ip)?.uses}]});
  const historySort=window.createTableSort({table:el('historyRows').closest('table'),controls:sortHosts.history,defaultKey:'duration',onChange:()=>{historyOffset=0;loadHistory();},columns:[
    {key:'ip',label:'IP',direction:'asc'},{key:'country',label:'Country',direction:'asc'},{key:'client',label:'Client',direction:'asc'},{key:'state',label:'State',direction:'asc'},
    {key:'rate',label:'Avg MB/min'},{key:'total_mb',label:'Period MB'},{key:'duration',label:'Duration'},{key:'first_seen',label:'Started · IST'},{key:'last_seen',label:'Ended / last seen · IST'},{key:'uses',label:'Runs'}]});
  const chart=window.createMinuteChart({plot:el('trafficChart'),breakdown:el('ipBreakdown'),pageCount:el('ipPageCount'),previous:el('ipPrevious'),next:el('ipNext'),scroll:el('chartScroll'),popup:el('minutePopup'),active:()=>true,unit:el('chartUnit'),controls:el('chartOptions'),onRangeChange:selectHours,sortControls:sortHosts.breakdown,onIPSelect:selectIP,metadata:()=>nodeSummary,selectedIP:()=>selectedIP?.test==='current'?selectedIP.ip:null});
  async function loadNodeSummary(){
    if(summaryKey.startsWith(active.id+':')&&Date.now()-Number(summaryKey.split(':')[1])<60000)return;const key=active.id+':'+Date.now();summaryKey=key;const service=active;
    try{const result=await read('/api/history?'+new URLSearchParams({service:service.id,hours:'all',summary:'1'}));if(active!==service||summaryKey!==key)return;nodeSummary=result;nodeRecords=new Map();ipSummaries=new Map(result.ips.map(r=>[r.ip,r]));for(const row of result.nodes)for(const id of [row.instance,row.run,row.machine,row.current===true?row.group:null].filter(Boolean))for(const ip of [row.ip,'*']){const key=id+'|'+ip,old=nodeRecords.get(key);if(!old||old.last_seen_ms<row.last_seen_ms)nodeRecords.set(key,row);}summaryError='';render();}
    catch(error){if(active===service){summaryError='IP history unavailable: '+error.message;el('currentNote').textContent=summaryError;}}
  }
  let plotInputs=[],plotRevision=0;
  function drawTraffic(bars){
    const inputs=[data,ipData,selectedIP?.ip,selectedIP?.test,active.id,hours,Math.floor(Date.now()/60000)];
    if(inputs.some((value,i)=>value!==plotInputs[i])){plotInputs=inputs;plotRevision++;}
    el('ipFilter').hidden=false;el('allIPs').textContent=selectedIP?.ip||'All IPs';el('allIPs').setAttribute('aria-pressed',String(!selectedIP));el('earnedLegend').hidden=!!selectedIP;el('ipDetails').disabled=!ipData?.runs?.length;
    if(!selectedIP){el('chartTitle').textContent='Traffic';el('chartTitle').title='All recorded IPs';el('allIPs').title='Showing the whole network';chart.update({plotRevision,scopeKey:active.id+':'+hours,key:active.id+':'+hours,time_ms:Date.now(),candles:bars,history:data?.chart_history?.minutes,ipRows:breakdownData,nodes:model.nodes,postings:model.providerMetrics?.intervals||[],creditMarkers:active.id==='rp',usdConversion:model.providerMetrics?.usd_conversion});return;}
    el('chartTitle').textContent='Traffic';el('chartTitle').title='Traffic for '+selectedIP.ip;el('ipFilterLabel').textContent=selectedIP.ip;
    el('ipFilterNote').textContent=ipError|| (ipData?(ipData.anchored?'Last recorded activity · through '+date(ipData.end_ms)+' · ':'')+displayMB(ipData.total_mb)+' MB in view · Network only; account credits have no IP attribution':'Loading this IP’s recorded traffic…');
    el('allIPs').title=el('ipFilterNote').textContent+' · Click to show all IPs';
    const minutes=ipData?.minutes||[],end=ipData?.end_ms||Date.now();
    chart.update({plotRevision,scopeKey:active.id+':'+hours,breakdownCandles:bars,key:active.id+':ip:'+selectedIP.ip+':'+hours,time_ms:Date.now(),candles:periodMinutes(minutes,hours,end-1),history:minutes,ipRows:breakdownData,nodes:model.nodes,postings:[],ipOnly:true,loading:!ipData});loadIP();
  }
  function selectIP(ip,test='current'){
    if(selectedIP?.ip===ip&&selectedIP.test===test){clearIP();return;}
    selectedIP={ip,test};ipData=null;ipError='';ipKey='';ipTicket++;ipRequest?.abort();chart.hide();render();
  }
  async function loadIP(){
    if(!selectedIP)return;const selected=selectedIP,key=active.id+':'+selected.ip+':'+selected.test+':'+hours+':'+Math.floor(Date.now()/30000);if(ipKey===key)return;ipKey=key;const ticket=++ipTicket;
    ipRequest?.abort();ipRequest=new AbortController();
    try{const result=await read('/api/ip-history?'+new URLSearchParams({service:active.id,ip:selected.ip,test:selected.test,hours:String(hours)}),ipRequest.signal);if(ticket!==ipTicket)return;ipData=result;ipError='';render();}
    catch(error){if(ticket===ipTicket){ipError='IP traffic unavailable: '+error.message;el('ipFilterNote').textContent=ipError;}}
  }
  function clearIP(){selectedIP=null;ipData=null;ipError='';ipKey='';ipTicket++;ipRequest?.abort();chart.hide();render();}
  el('allIPs').onclick=clearIP;
  el('ipDetails').onclick=()=>{const record=ipData?.runs?.slice().sort((a,b)=>b.last_seen_ms-a.last_seen_ms)[0];if(record)showRecord(record,ipData);};

  function emptyRow(body,text){body.replaceChildren();const row=make('tr'),cell=make('td',text,'empty-cell');cell.colSpan=body.closest('table').tHead.rows[0].cells.length;row.append(cell);body.append(row);}
  function metric(label,value,unit,note,cls=''){
    if(value==='—'||value==='≈ —'){value='Not reported';unit='';}
    const card=make('div',null,'metric card '+cls),valueNode=make('div',value,'metric-value');if(unit)valueNode.append(make('span',unit,'metric-unit'));
    card.title=note;card.append(make('p',label,'metric-label'),valueNode);return card;
  }
  function grassRegistrationCard(){
    const r=grassRegistration(model.registration,Date.now(),date),card=metric(r.label,r.value,'',r.detail);
    card.id='grassRegistration';card.append(make('div',r.remaining,'metric-unit'));return card;
  }
  function earnappStatusNote(model){
    const confirmed=valid(model.working)?shown(model.working,0)+' confirmed clients'+(model.confirmedPartial?' (partial)':''):'Client confirmation unavailable';
    const online=valid(model.providerOnline)&&model.providerOnlineKnown>0?shown(model.providerOnline,0)+' provider online'+(model.providerOnlinePartial?' ('+shown(model.providerOnlineKnown,0)+'/'+shown(model.desired,0)+' statuses known)':''):'Provider status unavailable';
    return confirmed+' · '+shown(model.nativeRegistered,0)+' native registered · '+shown(model.providerLinked,0)+' account linked · '+online;
  }
  function render(){
    if(!model)return;
    const bars=periodMinutes(model.minutes,hours,valid(model.trafficSummary?.end_ms)?model.trafficSummary.end_ms-1:Date.now()),measured=bars.filter(b=>valid(b.mb)),estimated=measured.some(b=>b.estimated);
    const networkNumber=displayMB,creditMarkers=active.id==='rp';
    const t=model.trafficSummary||{},p=model.providerMetrics||{},observed=p.observed_period||{},comparison=t.provider_comparison||{},rates=model.nodes.filter(n=>!n.pending).map(n=>n.rate).filter(valid),partialRate=rates.length!==t.containers_total,currentRate=model.statusFresh&&rates.length?rates.reduce((sum,n)=>sum+n,0):null;
    const balanceChange=active.id==='earnfm',moneyValue=balanceChange?observed.balance_change_usd:observed.earned_usd,moneyWindow=balanceChange?observed.balance_window:observed.earned_window;
    const providerNumber=value=>shown(value,6),providerStale=valid(p.observed_ms)&&!feeds.providerFresh(active.id,p);
    const isEarnApp=active.id==='earnapp',matched=t.earnings_comparison||{},effective=isEarnApp?matched.usd_per_recorded_gb:p.effective_usd_per_gb,effectiveUnit=isEarnApp?'USD/recorded GB':'USD/GB';
    const effectiveText=(isEarnApp&&valid(effective)&&(matched.partial||matched.estimated)?'≈ ':'')+shown(effective,4);
    const effectiveNote=isEarnApp?(matched.basis||'Matching earnings and traffic observations unavailable')+' · '+providerPeriod(matched)+' · '+shown(matched.earned_usd,6)+' USD / '+shown(matched.measured_mb)+' recorded MB · '+(matched.provider_scope||'device')+' / '+(matched.traffic_scope||'recorded fleet'):(p.scope||'Unknown scope')+' · '+providerPeriod(p.window);
    el('totals').replaceChildren(metric('Containers',(model.statusFresh&&fresh(Date.now()-t.inventory_ms,active.id==='hg'||active.id==='ps'?120000:90000)&&valid(t.containers_running)?shown(t.containers_running,0):valid(t.inventory_ms)?'Stale':'Not reported')+' / '+shown(t.containers_total,0),'',isEarnApp?earnappStatusNote(model):shown(model.working,0)+' confirmed clients'),metric('Current rate'+(valid(currentRate)&&partialRate?' · partial':''),valid(currentRate)?displayMB(currentRate):model.statusFresh?'Waiting':'Stale',valid(currentRate)?'MB/min':'',rates.length+' / '+shown(t.containers_total,0)+' containers have fresh native rates'+(partialRate?'; sum of available fresh rates while other rates are unavailable':'')),metric('Measured · 24h',(t.last_24h_partial||t.estimated?'≈ ':'')+displayMB(t.last_24h_mb),'MB','Recorded last-day chart intervals'),hours===24?metric('Average rate',(t.average_partial?'≈ ':'')+displayMB(t.average_mb_per_minute),'MB/min',t.average_basis||'Completed observed minutes'):metric('Measured · '+hours+'h',(t.partial||t.estimated?'≈ ':'')+displayMB(t.selected_mb),'MB','Selected-period chart intervals'),metric('Credited'+(providerStale?' · stale':model.cached?' · saved':''),(observed.credited_window?.partial?'≈ ':'')+displayMB(observed.credited_mb),'MB',(p.missing||p.changes_error||p.error||p.scope)+' · observed changes · '+providerPeriod(observed.credited_window)),metric((balanceChange?'Balance change':p.usd_conversion?'USD equivalent':'Earned')+(providerStale?' · stale':model.cached?' · saved':''),(moneyWindow?.partial?'≈ ':'')+providerNumber(moneyValue),'USD',(p.missing||p.changes_error||p.error||p.scope)+' · observed changes · '+providerPeriod(moneyWindow)+(p.usd_conversion?' · '+(p.usd_conversion.basis||String(p.usd_conversion.credits_per_usd)+' credits per USD')+' · '+p.usd_conversion.source:'')),isEarnApp?metric('Provider rate'+(providerStale?' · stale':''),shown(p.monthly_rate_usd,2),'USD/month','Provider-reported device rate for qualifying usage · observed '+date(p.observed_ms)):metric('Credit / measured %',(valid(comparison.ratio_percent)&&(comparison.estimated||comparison.partial)?'≈ ':'')+shown(comparison.ratio_percent,1),'',(comparison.basis||'Unavailable')+' · '+providerPeriod(comparison)+' · '+shown(comparison.credited_mb)+' credited MB / '+shown(comparison.measured_mb)+' measured MB'),balanceChange?metric('Account balance',providerNumber(p.balance_usd),'USD','Provider bandwidth balance · observed '+date(p.observed_ms)):metric('Effective rate'+(isEarnApp&&providerStale?' · stale':''),effectiveText,effectiveUnit,effectiveNote));
    if(active.id==='grass'){
      const g=model.points||{},watch=model.supervisor||{},watchFresh=watch.fresh&&valid(watch.observed_ms)&&fresh(Date.now()-watch.observed_ms,150000),pointNote='Account '+(g.account_id||'unavailable')+' · '+(g.epoch||'epoch unavailable')+' · observed '+date(g.observed_ms)+(g.error?' · '+g.error:'');
      const stalePoints=!feeds.providerFresh('grass',g),pointsSuffix=stalePoints?' · stale':'';
      const stateNote=shown(model.running,0)+' cloud running · '+shown(model.desired,0)+' requested · '+shown(watch.target_replicas,0)+' target';
      el('totals').replaceChildren(metric('Cloud running / requested',shown(model.running,0)+' / '+shown(model.desired,0),'',stateNote),metric('Native connected',shown(model.nativeConnected,0)+' / '+shown(model.desired,0),'',shown(model.nativeKnown,0)+' nodes have fresh native connection evidence; cached CONNECTED plus process-owned transport'),metric('Provider confirmed',shown(model.working,0)+' / '+shown(model.desired,0),'','Fresh provider device and current IP confirmation; '+shown(model.confirmedKnown,0)+' states known'),metric('Current rate'+(partialRate?' · partial':''),displayMB(currentRate),'MB/min',rates.length+' nodes with measured rates'),metric('Measured · '+hours+'h',(t.partial||t.estimated?'≈ ':'')+displayMB(t.selected_mb),'MB','Recorded traffic in the selected chart period'),metric('Total points'+pointsSuffix,shown(g.total_points,3),'points',pointNote),metric('Network Points'+pointsSuffix,shown(g.network_points,3),'points',pointNote+(g.network_points==null?' · proxyPoints was not reported':'')),grassRegistrationCard());
      const waiting=model.running!==model.desired?'Waiting for requested cloud nodes':model.working!==model.desired?'Waiting for provider confirmation of the requested nodes':'Requested nodes confirmed; the rollout operator controls the next addition';
      el('supervisorNote').hidden=false;el('supervisorNote').textContent='Rollout: '+stateNote+'. '+waiting+'. Supervisor '+(watchFresh?watch.mode||'active':'stale or unavailable')+' · '+(watch.status||'No observation')+' · observed '+date(watch.observed_ms)+(watch.policy?' · replacement limits '+shown(watch.policy.max_actions_hour,0)+'/hour, '+shown(watch.policy.max_actions_day,0)+'/day; '+shown(watch.requests_24h,0)+' requests recorded in 24h.':'');
    }else el('supervisorNote').hidden=true;
    el('totals').classList.add('overview-metrics');
    el('notice').textContent=[model.notice,data?.chart_history?.error].filter(Boolean).join(' · ');el('notice').classList.toggle('hidden',!el('notice').textContent);
    el('elapsed').textContent=valid(model.start)?'Test window starts '+date(model.start)+' · '+duration(diff(Date.now(),model.start))+' elapsed':'Recording start has not been reported';

    el('earnedLegend').lastChild.textContent=(creditMarkers?'Credit updates':'Credited')+(model.providerMetrics?.intervals?.some(p=>valid(p.credited_mb))?'':' —');el('earnedLegend').title=creditMarkers?'Green triangles mark account credit updates; hover for their amounts. Marker height is not traffic volume.':'Provider counter changes shown when observed; their posting time is unknown.';
    el('chartNote').textContent=(estimated?'≈ Estimated minute allocation.':'')+' Network traffic includes overhead.'+(creditMarkers?' Repocket credit updates are green triangles at their observation time; hover for the account amount.':'')+(active.id==='earnapp'?' EarnApp has not reported credited traffic in MB; network MB is separate from earnings. '+model.basis:'');
    if(active.id==='earnapp')el('chartNote').textContent+=' '+earnappEarningsStatus(data?.provider_credits).detail;
    const costs=model.costs;el('costNote').textContent=costs?'Salad running time: '+shown(costs.running_minutes,1)+' node-minutes · Estimated cost: '+usd(costs.estimated_usd)+' · '+providerPeriod(costs.window)+(costs.partial?' · Partial coverage':'')+' · '+costs.basis+(costs.errors?.length?' · '+costs.errors.join('; '):''):'Salad runtime and cost have not been reported.';el('earnfmDetail').hidden=active.id!=='earnfm';
    const body=el('workerRows');body.replaceChildren();
    const renderID=++rowRender,displayedNodes=currentSort.sort(model.nodes);
    for(const n of displayedNodes){
      const row=make('tr'),client=make('td'),status=window.bandwidthNodes.status(n),state=make('td',window.bandwidthNodes.state(n));
      client.append(make('span',status,'status-pill '+(status==='Online'?'online':'')));if(status!=='Online'){client.classList.add('has-issue');client.append(window.issueIcon([n.ip||n.id,n.status,n.error,n.note].filter(Boolean).join(' · ')||'Client state is unverified',status==='Offline'?'error':'warning'));}state.title=n.stale?'Last recorded: '+(n.salad||'Unknown'):n.salad||'';
      const durationCell=make('td',duration(n.duration));durationCell.title=(valid(n.age)?'Last source sample '+age(n.age)+' ago':'Source sample unavailable')+(n.cost?' · Salad running '+duration(valid(n.cost.running_minutes)?n.cost.running_minutes*60000:null)+' · Estimated Salad cost '+usd(n.cost.estimated_usd)+(n.cost.partial?' · Partial coverage':''):'');
      row.append(identityCell(n,active.id==='grass'),make('td',window.bandwidthCountries.name(n)),client,state,make('td',active.id==='earnfm'?shown(n.rate,valid(n.rate)&&n.rate>0&&n.rate<.001?6:3):networkNumber(n.rate),'numeric'),make('td',networkNumber(n.total),'numeric'),durationCell,timeCell(nodeRecord(n)?.first_seen_ms),usesCell(n.ip));if(n.ip)window.bindIPRow(row,n.ip,selectedIP?.ip===n.ip&&selectedIP.test==='current',()=>selectIP(n.ip));body.append(row);
    }
    if(!model.nodes.length)emptyRow(body,'No current nodes');
    const renderedModel=model;
    window.bandwidthCountries.lookup(model.nodes.filter(n=>!n.pending)).then(()=>{
      if(model!==renderedModel||renderID!==rowRender)return;
      for(let i=0;i<displayedNodes.length;i++)if(body.rows[i])body.rows[i].cells[1].textContent=window.bandwidthCountries.name(displayedNodes[i]);
    });
    el('currentNote').textContent=!data?'No inventory has been received.':model.statusFresh?'Current inventory · source observed '+date(model.source):'Current state is unverified · showing last recorded identities';if(summaryError)el('currentNote').textContent+=' · '+summaryError;
    el('creditNote').textContent=model.creditNote+(active.id==='proxyrack'?' Account balance: '+model.earned+' · '+date(data?.balance_checked_ms):'');if(model.providerMetrics){const p=model.providerMetrics,t=model.trafficSummary||{};el('creditNote').textContent=model.creditNote+' '+[p.scope,providerPeriod(p.window),'Observed '+date(p.observed_ms),'Average '+shown(t.average_mb_per_minute)+' MB/min','Peak '+shown(t.peak_mb_per_minute)+' MB/min','Credit / measured '+(t.provider_comparison?.estimated||t.provider_comparison?.partial?'≈ ':'')+shown(t.provider_comparison?.ratio_percent,1)+'% ('+shown(t.provider_comparison?.credited_mb)+' credited MB / '+shown(t.provider_comparison?.measured_mb)+' measured MB; '+providerPeriod(t.provider_comparison)+'; '+(t.provider_comparison?.provider_scope||'unknown provider scope')+' / '+(t.provider_comparison?.traffic_scope||'unknown traffic scope')+')','Effective '+effectiveText+' '+effectiveUnit+(isEarnApp?' · '+effectiveNote:''),isEarnApp?'Provider rate '+shown(p.monthly_rate_usd,2)+' USD/month':'',p.raw?.account_balance_credits!=null?'Account balance '+p.raw.account_balance_credits+' credits · observed '+date(p.raw.account_balance_observed_ms):'Account balance '+shown(p.balance_usd)+' USD','Credited observation window: '+providerPeriod(p.observed_period?.credited_window),(active.id==='earnfm'?'Balance-change observation window: ':'Earned observation window: ')+providerPeriod(active.id==='earnfm'?p.observed_period?.balance_window:p.observed_period?.earned_window),'Account-source totals: '+shown(p.credited_mb)+' MB / '+shown(active.id==='earnfm'?p.balance_change_usd:p.earned_usd)+(active.id==='earnfm'?' USD balance change':' USD'),p.error,p.missing].filter(Boolean).join(' · ');}
    if(active.id==='grass'){const g=model.points||{};el('creditNote').textContent=grassRegistration(model.registration,Date.now(),date).detail+' '+model.creditNote;el('providerRaw').textContent=JSON.stringify(g.raw||{},null,2);}
    el('originalProvider').hidden=!model.providerMetrics?.raw;el('providerRaw').textContent=model.providerMetrics?.raw?JSON.stringify(active.id==='grass'?(model.points?.raw||model.providerMetrics.raw):model.providerMetrics.raw,null,2):'';
    updateClock(false);
    drawTraffic(bars);renderHistoryClients();updateNodeCount();loadBreakdown();if(!busy)loadNodeSummary();
  }
  function earnappEarningsStatus(credits,now=Date.now()){
    if(!credits)return {state:'stale',label:'Provider stale',summary:'No EarnApp earnings observation yet.',detail:'EarnApp earnings have no source observation.'};
    const errors=[credits.credit_error,credits.status_error,credits.mutation_error].filter(Boolean);
    const cooldown=valid(credits.retry_after_ms)&&now<credits.retry_after_ms;
    const mutationLimited=Boolean(credits.mutation_error?.includes('HTTP 429'));
    const failures=[credits.credit_error,credits.status_error].filter(error=>error&&(!cooldown||!error.includes('HTTP 429')));
    if(credits.mutation_error&&!mutationLimited)failures.push(credits.mutation_error);
    const scopes=[['Current devices','current'],['Retired devices','retired'],['Account device list','account']];
    const coverage=scopes.map(([label,key])=>{const c=credits[key]||{};return label+': '+shown(c.amounts_reported,0)+'/'+shown(c.devices,0)+' amounts reported · '+(c.complete?'total ':'known sum ')+usd(c.complete?c.earned_total:c.reported_sum)+(c.complete?'':'; complete total unknown');}).join('. ');
    const stale=!valid(credits.observed_ms)||!fresh(now-credits.observed_ms,150000);
    const statusStale=!valid(credits.status_observed_ms)||!fresh(now-credits.status_observed_ms,150000);
    const partial=credits.current?.complete!==true||credits.retained?.complete!==true;
    const state=failures.length?'error':stale||statusStale?'stale':partial||cooldown||mutationLimited?'partial':'live';
    const nextWrite=Math.max(credits.mutation_next_attempt_ms||0,credits.mutation_retry_after_ms||0);
    const summary=[failures.join(' · '),cooldown?'EarnApp HTTP 429; reads resume after '+date(credits.retry_after_ms)+'.':mutationLimited?'Linking rate-limited (HTTP 429); '+(nextWrite>now?'next eligible link after '+date(nextWrite):'retry pending')+'.':'',
      credits.credit_error||credits.status_error?'Provider read error.':stale?'Earnings read is stale.':statusStale?'Online-status read is stale.':'Provider reads are current.',
      valid(credits.observed_ms)?'Last earnings read '+date(credits.observed_ms)+'.':'',
      shown(credits.current?.amounts_reported,0)+'/'+shown(credits.current?.devices,0)+' current-device amounts reported.'].filter(Boolean).join(' ');
    return {state,label:state==='stale'?'Provider stale':state==='error'?'Provider error':state==='partial'?'Partial earnings':'Live',summary,
      observed_ms:credits.observed_ms,
      detail:[stale?'Earnings reading stale or unavailable.':'',statusStale?'Provider online status stale or unavailable.':'',coverage,
        valid(credits.observed_ms)?'Earnings observed '+date(credits.observed_ms):'',
        ...errors,cooldown?'Provider cooldown until '+date(credits.retry_after_ms):'',
        'Device lifetime amounts are separate from account balance and observed earnings changes.'].filter(Boolean).join(' · ')};
  }
  function updateClock(recheck=true){
    if(!model)return;
    const now=Date.now();
    if(recheck&&data){
      const current=normalize(active.id,{...data,hg_minutes:[],candles:[],minutes:[],chart_minutes:[],points:[],timeline:{}},now,billing);
      const changed=active.id==='grass'&&grassRegistration(model.registration,now).label!==grassRegistration(model.registration,model.at).label||current.nativeConnected!==model.nativeConnected||current.nativeKnown!==model.nativeKnown||current.statusFresh!==model.statusFresh||current.running!==model.running||current.working!==model.working||current.nodes.length!==model.nodes.length||current.nodes.some((node,i)=>['status','working','rate','stale','unknown'].some(key=>node[key]!==model.nodes[i]?.[key]))||feeds.providerFresh(active.id,model.providerMetrics,now)!==feeds.providerFresh(active.id,model.providerMetrics,model.at);
      if(changed){
        model={...model,at:now,statusFresh:current.statusFresh,nativeConnected:current.nativeConnected,nativeKnown:current.nativeKnown,confirmedKnown:current.confirmedKnown,running:current.running,working:current.working,nodes:current.nodes,notice:model.cached?model.notice:current.notice};
        render();return;
      }
    }
    const current=model.statusFresh&&fresh(now-model.source,active.id==='hg'||active.id==='ps'?120000:90000),node=el('connection');
    const sourceErrors=[selectedIP&&ipError,summaryError,data?.error,data?.cloud_error,data?.collector?.error,...Object.values(data?.errors||{}),active.id==='earnapp'?null:data?.provider_metrics?.error,data?.provider_metrics?.changes_error,data?.latest?.collection_error?'The recorder reported a collection error.':null].filter(Boolean);
    const earnings=active.id==='earnapp'?earnappEarningsStatus(data?.provider_credits,now):null;
    if(earnings?.state==='error')sourceErrors.push(earnings.summary);
    const error=model.cached?model.notice:sourceErrors.join(' · ');
    const stamps=[model.source,active.id==='hg'?data?.latest?.time_ms:data?.generated_ms].filter(valid),at=stamps.length?Math.min(...stamps):null;
    const state=error?'error':!current?'stale':earnings?.state||'live';
    window.setDataStatus(node,state,earnings&&state==='stale'&&valid(earnings.observed_ms)?Math.min(at??earnings.observed_ms,earnings.observed_ms):at,[error,!current?model.notice||'Recording is stale or unavailable.':'Traffic recording is current.',earnings?.state==='error'?null:earnings?.summary].filter(Boolean).join(' '),earnings&&current&&!error?earnings.label:null);
  }
  function restoreView(service,range){
    const ticket=generation;
    feeds.restore(service,range).then(value=>{
      if(ticket!==generation||data||!value)return;
      const failure=model?.cached?model.notice:null;data=value;model=failure?savedModel(service,data):normalize(service.id,data,Date.now(),billing);if(failure)model.notice=failure.replace('No readings are available.','Saved measurements are shown.');render();
    });
  }
  function savedModel(service,payload){
    const value=normalize(service.id,payload,Date.now(),billing);value.cached=true;return value;
  }
  async function refresh(force=true){
    if(busy)return;
    busy=true;const ticket=generation,service=active,range=hours;el('refresh').disabled=true;
    try{
      const result=await feeds.load(service,range,force);
      if(ticket!==generation)return;if(result!==data||model?.cached){data=result;model=normalize(service.id,data,Date.now(),billing);render();}else updateClock();
      if(!warmed){warmed=true;for(const next of SERVICES)if(next!==service)feeds.restore(next,range).then(()=>feeds.load(next,range,false)).catch(()=>{});}
    }catch(error){
      if(ticket!==generation)return;
      model=savedModel(service,data||{});if(!data)model.nodes=[];model.notice='Cannot refresh '+service.name+': '+error.message+(data?' Saved measurements are shown.':' No readings are available.');render();
    }finally{if(ticket===generation){busy=false;el('refresh').disabled=false;loadNodeSummary();}}
    if(service.id==='ps'&&!billingPending&&Date.now()-billingChecked>60000){
      billingPending=true;billingChecked=Date.now();
      read('/api/ps1/view').then(value=>{billing=value;if(active.id==='ps'&&data&&model){const credit=normalize('ps',data,Date.now(),billing);model.credited=credit.credited;model.earned=credit.earned;model.balance=credit.balance;model.creditNote=credit.creditNote;render();}}).catch(()=>{}).finally(()=>{billingPending=false;});
    }
  }
  function updateNodeCount(){
    const kind=historyVisible?'history':el('breakdownView').hidden?'current':'breakdown';
    const count=kind==='history'?historyData?.total:kind==='breakdown'?chart.count():data&&model?model.nodes.filter(n=>!n.pending).length:null;
    const node=el('nodeCount'),text=shown(count,0)+' '+(kind==='history'?'records':kind==='breakdown'?'IPs':'nodes');if(node.textContent!==text)node.textContent=text;
    node.setAttribute('aria-label',text);
  }
  async function loadBreakdown(){
    if(el('breakdownView').hidden)return;const key=active.id+':'+hours+':'+Math.floor((data?.traffic_summary?.end_ms||Date.now())/60000);if(breakdownKey===key)return;breakdownKey=key;const ticket=generation;
    try{const query=new URLSearchParams({service:active.id,hours:String(hours),sort:'total_mb',limit:'100'}),rows=[];let result;
      do{query.set('offset',String(rows.length));result=await feeds.read('/api/history?'+query);rows.push(...(result.rows||[]));if(result.test?.id)query.set('test',result.test.id);}while(result.has_more&&result.rows?.length);
      if(ticket===generation&&breakdownKey===key){breakdownData=rows;render();}
    }catch(error){if(ticket===generation){breakdownKey='';el('ipPageCount').textContent='History unavailable: '+error.message;}}
  }
  function showNodeView(view){
    historyVisible=view==='history';for(const name of ['breakdown','current','history']){const selected=name===view;el(name+'View').hidden=!selected;el(name+'Tab').setAttribute('aria-selected',String(selected));el(name+'Tab').classList.toggle('active',selected);el(name+'Tab').tabIndex=selected?0:-1;}
    for(const name of ['current','breakdown','history'])sortHosts[name].hidden=name!==view;
    if(historyVisible)loadHistory();else{historyRequest?.abort();historyTicket++;}if(view==='breakdown')loadBreakdown();updateNodeCount();
  }
  function renderHistoryClients(){
    if(!historyVisible||!historyData)return;
    for(const [i,record]of historyData.rows.entries()){
      const row=el('historyRows').rows[i],pill=row?.cells[2]?.firstElementChild;if(!pill)continue;
      if(record.ip)window.bindIPRow(row,record.ip,selectedIP?.ip===record.ip&&selectedIP.test===(historyData.test?.id||'current'),()=>selectIP(record.ip,historyData.test?.id||'current'));
      pill.textContent=record.client||'Unverified';pill.className='status-pill';
    }
  }
  async function loadHistory(){
    if(!historyVisible)return;historyRequest?.abort();historyRequest=new AbortController();const ticket=++historyTicket,service=active;
    historyData=null;emptyRow(el('historyRows'),'Loading recorded IP contributions…');el('historyCount').textContent='';el('historyNote').textContent='Reading saved IP records…';el('historyRows').setAttribute('aria-busy','true');el('historyPrevious').disabled=true;el('historyNext').disabled=true;updateNodeCount();
    const query=new URLSearchParams({service:service.id,hours:el('historyHours').value==='all'?'all':String(hours),test:el('historyTest').value,q:el('historySearch').value.trim(),sort:historySort.state.key,direction:historySort.state.direction,unidentified:el('historyUnidentified').checked?'1':'0',offset:String(historyOffset),limit:'50'});
    try{
      const result=await read('/api/history?'+query,historyRequest.signal);if(ticket!==historyTicket)return;historyData=result;
      const selection=el('historyTest').value;el('historyTest').replaceChildren();
      for(const test of result.tests||[]){const option=make('option',test.label+(test.archived?' · archived':''));option.value=test.id;el('historyTest').append(option);}
      if(!el('historyTest').options.length){const option=make('option','Current test');option.value='current';el('historyTest').append(option);}
      if([...el('historyTest').options].some(o=>o.value===selection))el('historyTest').value=selection;else el('historyTest').value=result.test?.id||'current';
      const period=el('historyHours').value==='all'?'All recorded history':'Last '+hours+' hours';
      el('historyNote').textContent=period+' · '+(result.test?.label||'Current test')+(result.test?.archived?' · Archived test':'')+'. '+(result.coverage_note||'Coverage has not been reported.')+(valid(result.source_ms)?' Source observed '+date(result.source_ms)+'.':'');
      const body=el('historyRows');body.replaceChildren();
      for(const record of result.rows||[]){
        const row=make('tr'),identity=make('td'),client=make('td'),button=make('button',null,'record-link');
        button.append(record.ip?window.bandwidthNodes.label(record,false):make('span','IP unavailable · '+String(record.instance||record.run||'unidentified node').slice(0,12)));button.setAttribute('aria-label','View record for '+(record.ip||record.instance||'unknown IP'));button.onclick=()=>record.ip?selectIP(record.ip,result.test?.id||'current'):showRecord(record,result);identity.append(button);
        client.append(make('span',null,'status-pill'));
        const average=valid(record.total_mb)&&valid(record.duration_ms)&&record.duration_ms>0?record.total_mb*60000/record.duration_ms:null,prefix=record.estimated?'≈ ':'';
        const amount=make('td',valid(record.total_mb)?prefix+displayMB(record.total_mb):'No valid sample','numeric');if(record.partial&&valid(record.total_mb))amount.title='Partial retained coverage';
        row.append(identity,make('td',record.country||'Not recorded'),client,make('td',record.lifecycle||'Unknown'),make('td',valid(average)?prefix+displayMB(average):'Unavailable','numeric'),amount,make('td',duration(record.duration_ms)),timeCell(record.first_seen_ms),timeCell(record.last_seen_ms,record.current===true),make('td',record.ip_uses??'Unknown','numeric'));body.append(row);
      }
      if(result.unattributed&&!el('historyUnidentified').checked)el('historyNote').textContent+=' '+result.unattributed+' records have no captured IP; enable “Include records without an IP” to inspect their node identities.';
      renderHistoryClients();
      if(!result.rows?.length)emptyRow(body,'No recorded IPs match this period and search. Try all recorded history or another test.');
      el('historyCount').textContent=(result.rows?.length?shown(historyOffset+1,0)+'–'+shown(historyOffset+result.rows.length,0):'0')+' of '+shown(result.total,0)+' records · period contributions';
      el('historyPrevious').disabled=historyOffset===0;el('historyNext').disabled=!result.has_more;
    }catch(error){if(ticket!==historyTicket)return;historyData=null;emptyRow(el('historyRows'),'History could not be loaded. Use Refresh to try again.');el('historyNote').textContent='History unavailable: '+error.message;el('historyCount').textContent='No history response';}
    finally{if(ticket===historyTicket){el('historyRows').removeAttribute('aria-busy');updateNodeCount();}}
  }
  function showRecord(record,result){
    el('detailTitle').textContent=record.ip||'IP not recorded';el('detailSubtitle').textContent=(record.country||'Country not recorded')+' · '+(record.lifecycle||'Lifecycle unknown');
    const body=el('detailBody'),grid=make('dl',null,'detail-grid');body.replaceChildren(grid);
    const fields=[[Array.isArray(result.runs)?'Recorded contribution for this run':'Period contribution',mb(record.total_mb,6)],[Array.isArray(result.runs)?'Recorded time for this run':'Recorded time in period',duration(record.duration_ms)],['First retained observation',valid(record.first_seen_ms)?date(record.first_seen_ms):'Not recorded'],['Last retained observation',valid(record.last_seen_ms)?date(record.last_seen_ms):'Not recorded'],['Test',(result.test?.label||'Unknown')+(result.test?.archived?' · archived':'')],['Saved samples',shown(record.samples,0)],['Group',record.group_id||record.group],['Instance',record.instance],['Machine',record.machine],['Run',record.run],['Coverage',[record.partial?'Partial':'',record.estimated?'Estimated interval allocation':''].filter(Boolean).join(' · ')||'See source coverage note']];
    if('run_first_observed_ms' in record)fields.push(['First retained run observation',valid(record.run_first_observed_ms)?date(record.run_first_observed_ms):'Not recorded'],['Last retained run observation',valid(record.run_last_observed_ms)?date(record.run_last_observed_ms):'Not recorded']);
    fields.push(['Recorded runs for this IP',record.ip_uses||ipData?.ip_summary?.uses||'Unknown']);
    if(record.cost)fields.push(['Salad running time in period',duration(valid(record.cost.running_minutes)?record.cost.running_minutes*60000:null)],['Estimated Salad cost',usd(record.cost.estimated_usd)],['Cost scope',record.cost.scope+(record.cost.partial?' · Partial coverage':'')]);
    for(const [label,value]of fields){const item=make('div',null,['Group','Instance','Machine','Run'].includes(label)?'full':'');item.append(make('dt',label),make('dd',value==null?'Not recorded':String(value),['Group','Instance','Machine','Run'].includes(label)?'mono':''));grid.append(item);}
    body.append(make('p',(result.coverage_note||result.basis||'Coverage has not been reported.')+' Provider credit per IP is not reported. Account totals are kept separate.','dialog-note'));
    el('recordDetail').showModal();
  }
  function clearDetails(){el('costNote').textContent='';el('providerRaw').textContent='';el('originalProvider').hidden=true;el('earnfmDetail').hidden=active.id!=='earnfm';el('creditNote').textContent='Waiting for provider and measurement notes.';}
  function select(service,changePath=true){
    if(service===active&&model)return;
    historyRequest?.abort();historyTicket++;generation++;busy=false;active=service;chart.hide();el('recordDetail').close();
    nodeSummary=null;nodeRecords.clear();ipSummaries.clear();summaryKey='';summaryError='';selectedIP=null;ipData=null;ipKey='';ipTicket++;ipRequest?.abort();
    breakdownData=null;breakdownKey='';data=feeds.get(service,hours)||null;model=data?normalize(service.id,data,Date.now(),billing):null;historyData=null;historyOffset=0;
    el('historyTest').replaceChildren(Object.assign(make('option','Current test'),{value:'current'}));el('historySearch').value='';
    panel.setAttribute('aria-label',service.name);if(changePath&&location.pathname!==service.path)history.pushState(null,'',service.path);document.title='WDI · '+service.name;
    el('serviceTitle').textContent=service.name;for(const item of SERVICES){const chosen=item.id===service.id;buttons.get(item.id).setAttribute('aria-selected',String(chosen));buttons.get(item.id).classList.toggle('active',chosen);buttons.get(item.id).tabIndex=chosen?0:-1;}window.setDataStatus(el('connection'),'loading',null,'Reading recorded measurements.');
    if(model){render();}else{
      clearDetails();
    el('totals').classList.add('overview-metrics');el('totals').replaceChildren(...['Containers','Current rate','Measured · 24h',hours===24?'Average rate':'Measured · '+hours+'h','Credited','Earned',service.id==='earnapp'?'Provider rate':'Credit / measured %','Effective rate'].map(label=>metric(label,'\u00a0','','Reading measurements')));
      emptyRow(el('workerRows'),'Loading '+service.name+' inventory…');el('notice').classList.add('hidden');el('nodeCount').textContent='—';el('elapsed').textContent='Reading saved measurements…';el('currentNote').textContent='Reading current inventory…';chart.update({time_ms:Date.now(),candles:periodMinutes([],hours),loading:true});
    }
    if(!data)restoreView(service,hours);refresh(false);if(historyVisible)loadHistory();
  }
  function selectHours(value){hours=value;ipData=null;ipKey='';ipTicket++;ipRequest?.abort();chart.setHours(value);data=feeds.get(active,hours)||null;model=data?normalize(active.id,data,Date.now(),billing):null;chart.hide();generation++;busy=false;breakdownData=null;breakdownKey='';if(model)render();else{clearDetails();chart.update({time_ms:Date.now(),candles:periodMinutes([],hours),loading:true});for(const value of el('totals').querySelectorAll('.metric-value'))value.firstChild.textContent='—';el('earnedLegend').lastChild.textContent='Credited —';}updateNodeCount();if(!data)restoreView(active,hours);refresh(false);historyOffset=0;if(historyVisible)loadHistory();}
  el('refresh').onclick=()=>{clearIP();breakdownKey='';refresh();if(historyVisible)loadHistory();};
  const nodeViews=['current','breakdown','history'];
  for(const [index,name]of nodeViews.entries()){
    el(name+'Tab').onclick=()=>showNodeView(name);
    el(name+'Tab').onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();const next=event.key==='Home'?0:event.key==='End'?2:(index+(event.key==='ArrowLeft'?2:1))%3;showNodeView(nodeViews[next]);el(nodeViews[next]+'Tab').focus();}};
  }
  for(const id of ['historyHours','historyTest','historyUnidentified'])el(id).onchange=()=>{historyOffset=0;loadHistory();};
  el('historySearch').oninput=()=>{clearTimeout(historyTimer);historyOffset=0;historyTimer=setTimeout(loadHistory,250);};
  el('historyPrevious').onclick=()=>{historyOffset=Math.max(0,historyOffset-50);loadHistory();};el('historyNext').onclick=()=>{if(historyData?.has_more){historyOffset+=50;loadHistory();}};
  el('closeDetail').onclick=()=>el('recordDetail').close();
  document.querySelector('.skip-link')?.addEventListener('click',event=>{event.preventDefault();panel.focus();});
  window.addEventListener('popstate',()=>{const service=pageService(location.pathname,location.hash);if(service)select(service,false);else location.replace('/overview');});
  window.addEventListener('hashchange',()=>{const service=SERVICES.find(s=>s.hash===location.hash);if(service){history.replaceState(null,'',service.path+location.search);select(service,false);}});
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
  setInterval(()=>{if(!document.hidden)updateClock();},1000);
  select(active,false);setInterval(()=>{if(!document.hidden)refresh(false);},1000);
})();
