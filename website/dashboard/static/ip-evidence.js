'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const valid = value => typeof value === 'number' && Number.isFinite(value);
  const list = value => Array.isArray(value) ? value : [];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const num = (value, digits = 2) => valid(value) ? value.toLocaleString('en-US', {maximumFractionDigits:digits}) : 'Unknown';
  const date = value => valid(value) && value > 0 ? window.saladTime.format(value) : 'Unknown';
  const duration = value => !valid(value) ? 'Unknown' : value < 60000 ? num(value / 1000, 0) + 's' : Math.floor(value / 3600000) + 'h ' + Math.floor(value % 3600000 / 60000) + 'm';
  const age = value => !valid(value) ? 'Unknown' : value > Date.now() + 5000 ? 'Future timestamp' : duration(Math.max(0, Date.now() - value));
  const make = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = text; if (cls) node.className = cls; return node; };
  const fact = (label, value) => `<div><dt>${esc(label)}</dt><dd>${esc(value)}</dd></div>`;
  const textValue = value => value == null ? 'Unknown' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  let regions;
  try { regions = new Intl.DisplayNames(['en'], {type:'region'}); } catch (_) {}
  const country = code => { try { return /^[a-z]{2}$/i.test(code) ? regions?.of(code.toUpperCase()) || code : code; } catch (_) { return code; } };
  const countries = row => list(row.countries).map(country).join(' / ') || 'Unknown';
  const SERVICES = [
    ['hg','Honeygain'], ['ps','PacketStream'], ['proxyrack','Proxyrack'], ['rp','Repocket'],
    ['earnfm','EarnFM'], ['tm','Traffic Monetizer'], ['pawns','Pawns.app'], ['grass','Grass']
  ];
  const APP_CODES = {hg:'HG',ps:'PS',proxyrack:'PR',rp:'RP',earnfm:'EF',tm:'TM',pawns:'PA',grass:'GR'};
  const APP_COLORS = {hg:'#70adf5',ps:'#52cf8d',proxyrack:'#a779ec',rp:'#dc6cda',earnfm:'#ed925b',tm:'#dfbd4c',pawns:'#46c9df',grass:'#3ed3b1'};
  const appColor = service => APP_COLORS[service] || '#6a756e';
  const retainedApps = row => list(row?.apps).filter(app => app && app.service !== 'earnapp');
  function supportedRow(row) {
    const scope = row.ranking?.scope_service || row.evaluation_window?.service || row.traffic_reporter;
    return {...row,apps:retainedApps(row),...(scope === 'earnapp' ? {ranking:{},metrics:null,current_rate:null,evaluation_window:null,traffic_reporter:null} : {})};
  }
  const serviceCode = service => APP_CODES[service] || '—';
  const serviceName = service => SERVICES.find(([id]) => id === service)?.[1] || service || 'Unknown app';
  for (const [value, label] of SERVICES) { const option = make('option', label); option.value = value; $('service').append(option); }
  const MINUTE = 60000;
  let view = 'current', offset = 0, data = null, selected = null, traffic = null;
  let request = null, generation = 0, chartRequest = null, chartGeneration = 0, detailRequest = null, detailGeneration = 0;
  let queryTimer = null, retryTimer = null, detailIP = null, detailOpener = null, detailSelection = null;
  let freshnessKey = '';
  let revisionRequest = null, tableBusy = false, observedRevision, deferredRevision = null;
  let revisionNotice = false, lastTableStatus = null;
  const allocationID = row => row?.allocation_id != null ? String(row.allocation_id) : row?.id != null && !String(row.id).startsWith('ip:') && row.current === true ? String(row.id) : null;
  const rowKey = row => view === 'current' && !allocationID(row) ? null : row?.id != null ? String(row.id) : allocationID(row) || row?.ip;
  const rowLabel = row => row.allocation_label || row.instance || row.allocation?.instance || allocationID(row) || 'Allocation unknown';
  const expired = (row, field) => valid(row?.[field]) && Date.now() >= row[field];
  const availability = row => expired(row,'current_expires_ms') && row.current === true ? null : row.current;
  const rateExpired = row => expired(row,'rate_expires_ms') || expired(row,'current_expires_ms');
  const scoreExpired = row => row.ranking?.version !== 'ip-reputation-v2' && row.current === true && row.ranking?.lane !== 'flagged' && (expired(row,'performance_expires_ms') || expired(row,'current_expires_ms'));
  const rankingTime = value => value?.evaluation_generated_ms ?? value?.generated_ms;
  let selectedTest = 'current', trafficKey = null;
  let acceptedSorts = new Set();
  let dataOrder = null;
  let hours = 24;
  try { const stored = Number(localStorage.getItem('ws-ip-hours')); if ([1,6,12,24].includes(stored)) hours = stored; } catch (_) {}
  const stateText = row => availability(row) === true ? 'Current container' : availability(row) === false ? 'Past allocation' : 'Availability unknown';
  const apps = row => retainedApps(row).filter(app => $('service').value === 'all' || app.service === $('service').value);

  // Additive evaluator fields live here. Never derive a score or substitute lifetime
  // traffic for an unavailable current evaluation window.
  function ranking(row) {
    const rank = row.ranking || {};
    if (rank.scope_service === 'earnapp') return {};
    return scoreExpired(row) ? {...rank,score:null,confidence:null,lane:'stale',reasons:['Performance observation expired; awaiting a fresh evaluation.',...list(rank.reasons)]} : rank;
  }
  function scoreScale(row) {
    const scale = row?.ranking?.scale || data?.scoring?.scale;
    return Array.isArray(scale) && scale.length === 2 && scale.every(valid) && scale[1] > scale[0] ? scale : null;
  }
  function scoreText(row) {
    const rank = ranking(row), scale = scoreScale(row);
    return valid(rank.score) ? (rank.score > 0 && scale?.[0] < 0 ? '+' : '') + num(rank.score,0) : rank.lane === 'flagged' ? 'Excluded' : scoreExpired(row) ? 'Stale' : 'Pending';
  }
  function scoreBars(row,large = false) {
    const rank = ranking(row), scale = scoreScale(row), bars = make('span',null,'score-bars' + (large ? ' large' : ''));
    bars.setAttribute('aria-hidden','true');
    const level = valid(rank.score) && scale && rank.score > 0 ? Math.min(5,Math.ceil(rank.score / scale[1] * 5)) : 0;
    bars.dataset.tone = rank.lane === 'flagged' || valid(rank.score) && rank.score < 0 ? 'bad' : level >= 4 ? 'good' : level ? 'developing' : 'neutral';
    for (let i = 0; i < 5; i++) { const bar = make('i'); if (i < level) bar.className = 'filled'; bars.append(bar); }
    return bars;
  }
  function dimension(row, key) {
    const supplied = list(ranking(row).dimensions).find(item => String(item.key || item.id || item.name).toLowerCase().replaceAll(/[^a-z0-9]+/g, '_').replace(/_$/, '') === key);
    if (supplied) return supplied;
    const watch = row.watch_evidence;
    if (key === 'failures' && valid(watch?.known_ms) && watch.known_ms > 0 && valid(watch.failure_episodes)) {
      return {name:'Observed native failures',value:watch.failure_episodes,unit:'episodes',basis:watch.basis,observed_ms:watch.source_ms};
    }
  }
  function nativeObservation(app) {
    const native = app?.native || {};
    // Native health has its own clock. Expired traffic does not imply an offline client.
    const stale = native.fresh === false || expired(app,'current_expires_ms') ||
      app?.service === 'grass' && [native.observed_ms,native.source_ms,native.received_ms].some(at => valid(at) && (Date.now() - at >= 90000 || at > Date.now()));
    return {state:native.binding_verified === false ? 'Unverified' : native.state || 'Not recorded',at:native.observed_ms,stale,reason:native.reason};
  }
  function providerCheck(app) {
    const native = app?.native || {}, provider = native.provider || {};
    if (native.binding_verified !== false && valid(provider.observed_ms) && provider.observed_ms > 0) {
      if (typeof provider.confirmed === 'boolean') return {state:provider.confirmed ? 'Confirmed' : 'Not confirmed',at:provider.observed_ms};
      if (typeof provider.device_ip_present === 'boolean') return {state:provider.device_ip_present ? 'Listed' : 'Not listed',at:provider.observed_ms};
    }
    const watch = app?.watch_evidence;
    const known = typeof watch?.provider_present === 'boolean' && valid(watch.provider_observed_ms) && watch.provider_observed_ms > 0;
    return {state:known ? watch.provider_present ? 'Listed' : 'Not listed' : 'Not reported',at:known ? watch.provider_observed_ms : null};
  }
  function appNote(app) {
    const native = nativeObservation(app), provider = providerCheck(app);
    return `${serviceName(app.service)} · Native: ${native.state}${native.stale ? ' (dated / stale)' : ''} · ${date(native.at)}` +
      (app.service === 'grass' ? ` · Last provider check: ${provider.state} · ${date(provider.at)}` : '') + (native.reason ? ' · ' + native.reason : '');
  }
  function preparedDecision(row) {
    const shadow = row.shadow, decision = shadow || row.decision;
    const identity = decision?.identity || {};
    const match = ['group_id','instance','machine'].every(key => row[key] && row[key] === identity[key]) && row.ip === identity.ip;
    if (shadow) {
      const trusted = match && shadow.version === 'ip-shadow-v1' && shadow.simulation_only === true && shadow.executed === false && shadow.execution_allowed === false;
      const fresh = trusted && valid(shadow.valid_until_ms) && !expired(shadow,'valid_until_ms');
      const labels = {keep:'Keep',investigate:'Investigate',local_repair:'App repair',hold:'Hold',consider_replace:'Consider replacement'};
      return {kind:'Simulated',label:fresh ? labels[shadow.action] || 'Observe' : 'Hold',reason:fresh ? shadow.reason : 'Awaiting fresh simulation inputs.',
        at:shadow.decision_ms,valid_until_ms:shadow.valid_until_ms,fresh,apps:fresh ? list(shadow.app_decisions).filter(app => app.service !== 'earnapp') : [],
        protected:fresh ? list(shadow.protected_apps).filter(service => service !== 'earnapp') : [],audit:trusted ? shadow.audit_id : null};
    }
    const fresh = availability(row) === true && match && valid(decision?.valid_until_ms) && !expired(decision,'valid_until_ms');
    return {kind:'Prepared',label:fresh ? decision.label || 'Observe' : decision ? 'Awaiting fresh recommendation' : 'Not supplied',
      reason:fresh ? decision.reason : null,at:decision?.issued_ms,valid_until_ms:decision?.valid_until_ms,fresh};
  }
  function measurementNote(row, key) {
    const m = metricApp(row);
    if (key === 'rate' && view === 'current') return 'Latest short interval, expressed as MB/min; source ' + date(row.rate_source_ms) +
      ' · Received ' + date(row.rate_received_ms) + (rateExpired(row) ? ' · Expired' : '') + '. Peak and totals use the separately dated episode window. This is observed transfer, not tested connection capacity.';
    return (m?.basis || 'No measurement supplied.') + (valid(m?.window_end_ms) ? ' Window ' + date(m.window_start_ms) + ' → ' + date(m.window_end_ms) + '.' : '');
  }
  const metricScope = row => { const scope = ranking(row).scope_service || row.evaluation_window?.service || row.traffic_reporter || ($('service').value !== 'all' ? $('service').value : null); return scope !== 'earnapp' ? scope : null; };
  function metricApp(row) {
    const shown = apps(row), scope = metricScope(row);
    if (row.metrics && scope && shown.some(app => app.service === scope)) {
      const m = row.metrics;
      return {...m,service:scope,total_mb:m.total_mb,average_mb_per_minute:view === 'current' ? (rateExpired(row) ? null : row.current_rate) : m.average_mb_per_minute,
        duration_ms:m.duration_ms ?? (valid(m.measured_minutes) ? m.measured_minutes * MINUTE : null),
        partial:m.partial || (valid(m.coverage) && m.coverage < 1),basis:m.basis || 'Selected evaluation episode: ' + serviceName(scope)};
    }
    if (shown.length !== 1) return null;
    if (view !== 'current') return shown[0];
    const current = list(row.current_apps || row.current_evaluation?.apps).find(app => app.service === shown[0].service);
    const result = current || shown[0].current_metrics || null;
    return result && rateExpired(row) ? {...result,average_mb_per_minute:null} : result;
  }
  const metric = (row, key) => metricApp(row)?.[key];
  const dimensionValue = (row, key) => dimension(row, key)?.value;
  const peak = row => metric(row, 'peak_mb_per_minute');
  const consistencyDimension = row => dimension(row,'consistency') || dimension(row,'repeated_observed_transfer');
  const consistency = row => consistencyDimension(row)?.value;
  const failures = row => dimensionValue(row, 'failures');
  const client = row => apps(row).map(app => app.native?.state || 'Unknown').join(' / ') || 'Unknown';
  const columns = [
    {key:'country',label:'Country',direction:'asc',value:row => countries(row) === 'Unknown' ? null : countries(row)},
    {key:'ip',label:'IP',direction:'asc'},
    {key:'score',label:'Score',value:row => ranking(row).score},
    {key:'apps',label:'Apps',direction:'asc',value:row => apps(row).map(app => app.name || serviceName(app.service)).join(', ')},
    {key:'inventory',label:'Inventory',direction:'asc',value:stateText},
    {key:'rate',label:'MB/min',value:row => metric(row,'average_mb_per_minute')},
    {key:'peak',label:'Peak MB/min',value:peak},
    {key:'consistency',label:'Consistency',value:consistency},
    {key:'total_mb',label:'Recorded MB',value:row => metric(row,'total_mb')},
    {key:'duration_ms',label:'Duration',value:row => metric(row,'duration_ms')},
    {key:'runs',label:'Runs',value:row => row.runs},
    {key:'failures',label:'Failures',value:failures},
    {key:'last_seen_ms',label:'Observation age',value:row => row.last_seen_ms}
  ];
  const baseSorts = new Set(['score','country','ip','first_seen_ms','last_seen_ms','runs','duration_ms','total_mb','rate','peak','consistency','cost']);
  const appSorts = new Set(['duration_ms','total_mb']);
  function serverSort() {
    const key = sorter.state.key;
    const advertised = acceptedSorts.has(key);
    if (appSorts.has(key) && ($('service').value === 'all' || view === 'current' && !advertised)) return 'last_seen_ms';
    return baseSorts.has(key) || advertised ? key : 'last_seen_ms';
  }
  const sorter = window.createTableSort({table:$('rankingTable'), controls:$('sortControls'), columns, defaultKey:'score', onChange:() => {
    offset = 0; load(false);
  }});

  function summary(value) {
    const rows = list(value?.rows), ranks = rows.map(ranking), totals = value?.summary;
    const metricNode = (label, amount, note) => {
      const box = make('div', null, 'metric'); box.title = note;
      box.append(make('div', label, 'metric-label'), make('div', amount, 'metric-value')); return box;
    };
    const loaded = !!value;
    $('totals').replaceChildren(
      metricNode('Current', num(totals?.current_containers ?? totals?.current_allocations,0), 'Freshly confirmed inventory membership; independent of the displayed app filter'),
      metricNode('Historical IPs', num(totals?.historical_ips,0), 'Indexed addresses with affirmative historical identity evidence; unknown inventory is separate'),
      metricNode(totals ? (view === 'current' ? 'Scored' : 'Scored IPs') : 'Scored · page', totals ? num(view === 'current' ? totals.scored_containers : totals.scored_ips,0) : loaded ? num(ranks.filter(r => valid(r.score)).length,0) : 'Unknown', totals ? 'Scored IPs across the index' : 'Supplied scores on the loaded page only'),
      metricNode(view === 'current' ? 'Pending' : 'Pending IPs', num(view === 'current' ? totals?.pending_containers : totals?.pending_ips,0), 'Evaluator pending count across the index; missing evidence is not a zero score'),
      metricNode(totals ? 'Flagged IPs' : 'Flagged · page', totals ? num(totals.flagged_ips,0) : loaded ? num(ranks.filter(r => r.lane === 'flagged').length,0) : 'Unknown', totals ? 'Flagged IPs across the index; inspect dated source evidence' : 'Supplied flagged lanes on the loaded page only'),
      metricNode('Retained IPs', num(value?.all_ips,0), 'All IP addresses in the retained evidence index; not assignable machines'),
      valid(totals?.unavailable_containers) ? metricNode('Inventory unavailable',num(totals.unavailable_containers,0),'Previously observed slots retained while inventory is unavailable; not confirmed offline') : metricNode('Apps · page', loaded ? num(new Set(rows.flatMap(row => apps(row).map(app => app.service))).size,0) : 'Unknown', 'Distinct matched apps on the loaded page'),
      metricNode('Ranking updated', age(rankingTime(value)), 'Full ranking ' + date(rankingTime(value)) + ' · Observations ' + date(value?.observations_refreshed_ms ?? value?.generated_ms))
    );
  }
  function sortedRows() {
    if (dataOrder?.sort === sorter.state.key && dataOrder.direction === sorter.state.direction) return list(data?.rows);
    const sorted = sorter.sort(list(data?.rows));
    // The evaluator owns lane assignment; flagged records follow the loaded score list.
    return sorter.state.key === 'score' ? sorted.filter(row => ranking(row).lane !== 'flagged').concat(sorted.filter(row => ranking(row).lane === 'flagged')) : sorted;
  }
  function dimensionLabel(row, key) {
    const item = key === 'consistency' ? consistencyDimension(row) : dimension(row, key);
    return item?.value == null ? '—' : (valid(item.value) ? num(item.value,2) : textValue(item.value));
  }
  function dimensionCell(row,key) {
    const item = key === 'consistency' ? consistencyDimension(row) : dimension(row,key), cell = make('td',dimensionLabel(row,key));
    cell.title = item ? [item.name,item.value == null ? 'Not measured' : textValue(item.value),item.unit,item.basis,valid(item.observed_ms) ? date(item.observed_ms) : null].filter(Boolean).join(' · ') : 'No measurement supplied for this dimension';
    return cell;
  }
  function renderRows() {
    const chartFocus = $('inlineChart').contains(document.activeElement) ? document.activeElement : null;
    parkChart();
    const focused = document.activeElement?.closest('#rows [data-row-key],#rows tr')?.dataset.rowKey;
    const rows = sortedRows().filter(row => rowKey(row));
    const missingAllocation = view === 'current' && list(data?.rows).some(row => !rowKey(row));
    $('rows').replaceChildren();
    for (const row of rows) {
      const rank = ranking(row), app = metricApp(row), tr = make('tr'); tr.dataset.rowIp = row.ip; tr.dataset.rowKey = rowKey(row);
      if (rank.lane === 'flagged') tr.classList.add('ip-flagged');
      const countryCell = make('td', countries(row));
      const ipCell = make('td'), link = make('button', row.ip || 'IP pending', 'record-link'); link.type = 'button';
      link.setAttribute('aria-label', 'Show traffic for ' + (row.ip || 'pending IP') + (allocationID(row) ? ' · ' + rowLabel(row) : '')); ipCell.append(link);
      ipCell.title = [row.ip,allocationID(row),row.group_id,row.instance,row.machine].filter(Boolean).join(' · ');
      const scoreCell = make('td'), score = make('button',null,'ip-score');
      score.append(make('span',scoreText(row),'score-number'),scoreBars(row));
      score.type = 'button'; score.dataset.detailKey = rowKey(row);
      score.setAttribute('aria-label', 'Details and score for ' + (row.ip || 'pending IP') + (allocationID(row) ? ' · ' + rowLabel(row) : ''));
      score.title = ['Score version: ' + (rank.version || 'Unknown'), 'State: ' + (rank.lane || 'Unscored'), (rank.version === 'ip-reputation-v2' ? 'Measurement coverage: ' : 'Confidence: ') + (valid(rank.confidence) ? num(rank.confidence * 100,1) + '%' : textValue(rank.confidence)), 'App: ' + serviceName(metricScope(row)), ...list(rank.reasons).map(textValue)].join('\n');
      scoreCell.append(score);
      const scope = metricScope(row), appsCell = make('td'), appGroup = make('span',null,'allocation-apps');
      for (const app of apps(row)) {
        const badge = make('button',null,'app-badge'); badge.type = 'button';
        const native = nativeObservation(app), state = String(native.state).toLowerCase(), stale = native.stale;
        const health = stale ? 'unknown' : /reject|overused|offline|disconnect|fail|error|exited|stopped|blocked|unusable|limit/.test(state) ? 'issue' : /^(connected|online|running|accepted|healthy|ready)$/.test(state) ? 'online' : /start|connect|wait|initializ/.test(state) ? 'waiting' : 'unknown';
        badge.dataset.app = app.service; badge.dataset.health = health; badge.classList.toggle('measured-app',app.service === scope);
        badge.style.setProperty('--app-color',appColor(app.service));
        const note = appNote(app);
        badge.title = note; badge.setAttribute('aria-label',note + ' · Show reporter traffic');
        window.attachIssue(badge,note); badge.onclick = event => { event.stopPropagation(); selectIP(row,app.service); };
        appGroup.append(badge);
      }
      appsCell.append(appGroup);
      const numeric = (value, digits = 2, approximate = true) => {
        const cell = make('td', valid(value) ? (approximate && (app?.partial || app?.estimated) ? '≈ ' : '') + num(value,digits) : '—');
        cell.title = !app ? (apps(row).length > 1 ? 'Choose one app to inspect its traffic; counters may overlap.' : 'Current evaluation window metrics have not been supplied.') : (app.basis || (view === 'current' ? 'Current evaluation window' : 'Retained app intervals'));
        return cell;
      };
      const durationCell = make('td', valid(app?.duration_ms) ? duration(app.duration_ms) : '—'); durationCell.title = app?.basis || (view === 'current' ? 'Observed app time in the current evaluation window, when supplied' : 'Retained app time; gaps are not filled');
      const runsCell = make('td', num(row.runs,0)); runsCell.title = 'Retained IP-attributed run segments; not a restart count. Samples without an IP are outside this count. Details preserve the current native run identity.';
      const ageCell = make('td', age(row.last_seen_ms)); ageCell.title = 'Last retained IP observation: ' + date(row.last_seen_ms);
      const rateCell = numeric(app?.average_mb_per_minute,4,view !== 'current' || !row.metrics), peakCell = numeric(peak(row),4), totalCell = numeric(app?.total_mb);
      rateCell.title = measurementNote(row,'rate'); peakCell.title = totalCell.title = durationCell.title = measurementNote(row,'episode');
      tr.append(countryCell,ipCell,scoreCell,appsCell,make('td',availability(row) === true ? 'Current' : availability(row) === false ? 'Past' : 'Unknown','ip-inventory'),rateCell,peakCell,dimensionCell(row,'consistency'),totalCell,durationCell,runsCell,dimensionCell(row,'failures'),ageCell);
      tr.title = stateText(row);
      const open = !!selected && rowKey(selected) === rowKey(row);
      link.setAttribute('aria-expanded',String(open)); link.setAttribute('aria-controls','inlineChart');
      tr.setAttribute('aria-expanded',String(open));
      window.bindIPRow(tr,rowKey(row),open,() => selectIP(row));
      $('rows').append(tr);
    }
    if (!rows.length) { const tr = make('tr'), td = make('td',missingAllocation ? 'Waiting for individual container allocation records.' : 'No records match this view and these filters.','empty-cell'); td.colSpan = columns.length; tr.append(td); $('rows').append(tr); }
    mountChart();
    if (chartFocus && !$('inlineChart').hidden) chartFocus.focus({preventScroll:true});
    if (focused) [...$('rows').rows].find(row => row.dataset.rowKey === focused)?.focus({preventScroll:true});
    $('nodeCount').textContent = missingAllocation ? 'Waiting' : num(data?.total,0) + (view === 'current' ? ' containers' : ' IPs');
    $('count').textContent = `${rows.length ? offset + 1 : 0}–${offset + rows.length} of ${num(data?.total,0)} matching ${view === 'current' ? 'containers' : 'records'}`;
    $('previous').disabled = offset === 0;
    $('next').disabled = !valid(data?.total) || offset + rows.length >= data.total;
    const limited = dataOrder?.sort !== sorter.state.key || dataOrder?.direction !== sorter.state.direction;
    $('sortNote').textContent = limited ? `${columns.find(c => c.key === sorter.state.key).label} order applies to these ${rows.length} loaded records. Pages are retrieved by last observation.` : 'Order applies to all matching records.';
    $('sortNote').hidden = true;
    if (limited) $('count').textContent += ' · page order';
    $('count').title = $('sortNote').textContent;
    summary(data);
  }
  function sourceCoverage(value) {
    $('rankingBasis').textContent = value.scoring?.basis || value.ranking_basis || value.basis || 'Scores, confidence, exclusions and quality dimensions are supplied by the evaluator. Missing evidence remains unknown.';
    $('coverageBody').innerHTML = list(value.coverage).filter(c => c.service !== 'earnapp').map(c => `<div class="source"><strong>${esc(c.name || serviceName(c.service))}</strong><p>${num(c.rows,0)} identity segments · ${num(c.tests,0)} retained tests<br>Last source ${esc(date(c.source_ms))}</p>${c.error ? `<p class="ip-error">${esc(c.error)}</p>` : ''}${list(c.notes).map(note => `<p>${esc(note)}</p>`).join('')}<p>Cost source ${esc(date(c.cost?.observed_ms))} · ${c.cost?.partial === false ? 'reported coverage' : 'partial / unknown coverage'}</p>${list(c.cost?.errors).map(e => `<p class="ip-error">${esc(e)}</p>`).join('')}</div>`).join('');
  }
  function setStatus(state, message, at = data?.generated_ms, remember = true) {
    if (remember) lastTableStatus = {state,message,at};
    $('status').textContent = message;
    $('status').hidden = state === 'live';
    $('status').classList.toggle('ip-error', state === 'error');
    window.setDataStatus($('connection'),state,at,message,state === 'live' ? 'Updated' : null);
  }
  async function read(url, signal) {
    return window.bandwidthData.read(url,signal);
  }
  async function load(reset = false, fromRevision = false) {
    if (!fromRevision) { revisionRequest?.abort(); revisionRequest = null; }
    tableBusy = true;
    $('rankingTable').setAttribute('aria-busy','true');
    clearTimeout(retryTimer); request?.abort(); request = new AbortController(); const ticket = ++generation;
    if (reset) { clearSelection(); data = null; summary(null); $('rows').innerHTML = `<tr><td colspan="${columns.length}" class="empty-cell">Reading this view…</td></tr>`; $('previous').disabled = $('next').disabled = true; $('nodeCount').textContent = '—'; $('count').textContent = 'Reading this view…'; }
    $('refresh').disabled = true;
    const sort = serverSort();
    const query = new URLSearchParams({service:$('service').value,state:view === 'current' ? 'current' : 'historical',availability:view === 'current' ? 'all' : $('historyState').value,q:view === 'current' ? '' : $('search').value.trim(),sort,direction:sort === sorter.state.key ? sorter.state.direction : 'desc',offset:String(offset),limit:'100'});
    try {
      const result = await read('/api/ip-rankings?' + query,request.signal);
      if (ticket !== generation) return;
      if (result.state === 'loading') { setStatus('loading','Preparing container rankings…'); return; }
      if (result.state === 'error') throw new Error(result.error || 'Rankings unavailable');
      if (!Array.isArray(result.rows)) throw new Error('Ranking response has no records');
      data = {...result,rows:result.rows.filter(row => !list(row.apps).length || retainedApps(row).length).map(supportedRow)}; observedRevision = result.generated_ms; deferredRevision = null; revisionNotice = false; dataOrder = {sort,direction:query.get('direction')}; acceptedSorts = new Set([...list(result.supported_sorts),...(result.scoring?.version ? ['score','country','peak','consistency'] : [])]);
      if (serverSort() !== sort) { await load(false,fromRevision); return; }
      renderRows(); sourceCoverage(result);
      const stale = !valid(rankingTime(result)) || Date.now() - rankingTime(result) > 180000;
      const scope = view === 'current' ? 'Current allocations. Each traffic value keeps its selected app and observation window; unavailable current measurements stay unknown.' : $('historyState').value === 'unknown' ? 'Availability is unknown for these records; missing inventory does not establish that a container is offline.' : 'Dated IP history. Unknown availability stays distinct from confirmed past allocations.';
      const notes = [scope, `Ranking calculated ${date(rankingTime(result))}. Observations updated ${date(result.observations_refreshed_ms ?? result.generated_ms)}.`, result.refreshing ? 'Refreshing saved sources.' : '', result.error || '', ...list(result.metric_errors).map(item => serviceName(item.service) + ': ' + (item.error || 'Metric source unavailable'))].filter(Boolean).join(' ');
      setStatus(result.error || list(result.metric_errors).length ? 'error' : stale ? 'stale' : result.refreshing ? 'partial' : 'live',notes);
      freshnessKey = '';
      if (selected) {
        const updated = data.rows.find(row => rowKey(row) === rowKey(selected));
        if (updated) {
          const appIdentities = row => list(row.apps).map(app => [app.service,app.ip]);
          if (updated.ip !== selected.ip || JSON.stringify(appIdentities(updated)) !== JSON.stringify(appIdentities(selected))) selectIP(updated,$('chartApp').value,selectedTest);
          else { selected = updated; renderProfile(); if (fromRevision) loadTraffic(); }
        } else { clearSelection(); }
      }
    } catch (error) {
      if (ticket === generation && error.name !== 'AbortError') {
        setStatus('error',error.message + (data ? ' · Displayed rankings have not been refreshed.' : ' · No rankings loaded for this view.'));
        if (!data) { $('rows').innerHTML = `<tr><td colspan="${columns.length}" class="empty-cell">${esc(error.message)}</td></tr>`; $('count').textContent = 'Rankings unavailable'; }
      }
    } finally { if (ticket === generation) { tableBusy = false; $('refresh').disabled = false; $('rankingTable').setAttribute('aria-busy','false'); } }
  }

  // Poll only the small publication marker. An unchanged revision never triggers
  // an automatic table read. Filter/page/manual actions still fetch their view.
  async function pollRevision() {
    if (document.hidden || revisionRequest || tableBusy) return;
    const controller = new AbortController(); revisionRequest = controller;
    try {
      const result = await read('/api/ip-rankings/revision',controller.signal);
      if (controller !== revisionRequest || document.hidden) return;
      if (!['ready','waiting'].includes(result.state) || result.revision !== null && !valid(result.revision)) throw new Error('Invalid ranking revision response');
      if (result.state === 'waiting' || result.revision === null) {
        observedRevision = null; deferredRevision = null; revisionNotice = true;
        setStatus('loading',data ? 'Waiting for published rankings; showing the last saved result.' : 'Waiting for published rankings.',data?.generated_ms,false);
        return;
      }
      if (result.revision !== observedRevision) {
        observedRevision = result.revision; deferredRevision = result.revision;
      }
      if (deferredRevision !== null && !$('detail').open) {
        // Consume this publication once, even if the table read fails. A new
        // publication or manual Refresh can retry without hammering the table.
        deferredRevision = null;
        await load(false,true);
      } else if (revisionNotice && lastTableStatus) {
        revisionNotice = false;
        const old = lastTableStatus;
        const stale = old.state === 'live' && (!valid(rankingTime(data)) || Date.now()-rankingTime(data) > 180000);
        setStatus(stale ? 'stale' : old.state,old.message,old.at,false);
      }
    } catch (error) {
      if (controller === revisionRequest && error.name !== 'AbortError') {
        revisionNotice = true;
        setStatus('error','Ranking update check failed: ' + error.message + (data ? ' · Displayed rankings have not been refreshed.' : ''),data?.generated_ms,false);
      }
    } finally { if (revisionRequest === controller) revisionRequest = null; }
  }

  // Reuse WS's chart engine, including aggregation, partial coverage, hover and keyboard.
  // Supply only a single IP/app series. The endpoint's earlier minutes feed its AVG line.
  const chart = window.createTimeChart({plot:$('trafficChart'),scroll:$('chartScroll'),popup:$('minutePopup'),unit:$('chartUnit'),controls:$('chartOptions'),hours,range:false});
  $('trafficChart').onpointerdown = event => $('trafficChart').onpointermove?.(event);
  const rangeGroup = make('div',null,'chart-select'), rangeButton = make('button',hours + 'H','chart-select-toggle'), rangeMenu = make('div',null,'chart-select-menu');
  rangeButton.type = 'button'; rangeButton.title = 'Time range'; rangeButton.setAttribute('aria-haspopup','listbox'); rangeButton.setAttribute('aria-expanded','false'); rangeButton.setAttribute('aria-label','Time range: ' + hours + 'H');
  rangeMenu.hidden = true; rangeMenu.id = 'ipRangeMenu'; rangeMenu.setAttribute('role','listbox'); rangeMenu.setAttribute('aria-label','Time range'); rangeButton.setAttribute('aria-controls',rangeMenu.id);
  const closeRange = () => { rangeMenu.hidden = true; rangeButton.setAttribute('aria-expanded','false'); rangeGroup.classList.remove('open'); };
  for (const value of [1,6,12,24]) {
    const option = make('button',value + 'H'); option.type = 'button'; option.value = value; option.tabIndex = -1; option.setAttribute('role','option'); option.setAttribute('aria-selected',String(value === hours));
    option.onclick = () => { hours = value; try { localStorage.setItem('ws-ip-hours',String(hours)); } catch (_) {} rangeButton.textContent = hours + 'H'; rangeButton.setAttribute('aria-label','Time range: ' + hours + 'H'); for (const o of rangeMenu.children) o.setAttribute('aria-selected',String(Number(o.value) === hours)); closeRange(); rangeButton.focus(); chart.setHours(hours); loadTraffic(); };
    rangeMenu.append(option);
  }
  const openRange = () => { rangeMenu.hidden = false; rangeGroup.classList.add('open'); rangeButton.setAttribute('aria-expanded','true'); const box = rangeButton.getBoundingClientRect(); rangeMenu.style.position = 'fixed'; rangeMenu.style.top = box.bottom + 4 + 'px'; rangeMenu.style.left = Math.max(8,Math.min(box.left,innerWidth-rangeMenu.offsetWidth-8)) + 'px'; [...rangeMenu.children].find(option => Number(option.value) === hours).focus(); };
  rangeButton.onclick = () => rangeMenu.hidden ? openRange() : closeRange();
  rangeButton.onkeydown = event => { if (['ArrowUp','ArrowDown'].includes(event.key)) { event.preventDefault(); openRange(); } };
  rangeMenu.onkeydown = event => { if (event.key === 'Escape') { event.preventDefault(); closeRange(); rangeButton.focus(); } else if (event.key === 'Tab') closeRange(); else if (['ArrowUp','ArrowDown','Home','End'].includes(event.key)) { event.preventDefault(); const options = [...rangeMenu.children], i = options.indexOf(document.activeElement); options[event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (i + (event.key === 'ArrowUp' ? options.length - 1 : 1)) % options.length].focus(); } };
  document.addEventListener('pointerdown',event => { if (!rangeGroup.contains(event.target)) closeRange(); });
  rangeGroup.append(rangeButton,rangeMenu); $('chartRange').append(rangeGroup);
  function drawTraffic(value, loading = false) {
    if ($('inlineChart').hidden) return;
    const row = selected, appName = serviceName($('chartApp').value), color = appColor($('chartApp').value), minutes = list(value?.minutes);
    const end = valid(value?.end_ms) ? value.end_ms : (Math.floor(Date.now() / MINUTE) + 1) * MINUTE;
    chart.update({end,now:Date.now(),loading,mode:'rate',unit:'MB/min',series:[{name:appName,color}],
      points:minutes.filter(m => valid(m.minute_ms)).map(m => ({at:m.minute_ms,values:[valid(m.mb) ? m.mb : null],partial:!!m.partial,estimated:!!m.estimated,count:m.count})),
      tip:bar => [
        {label:'Network',value:(bar.partial || bar.estimated ? '≈ ' : '') + (valid(bar.amounts[0]) ? num(bar.amounts[0],4) + ' MB' : 'Unknown'),color,meta:appName},
        {label:row?.ip || 'No IP selected',value:countries(row || {}),color,meta:valid(bar.values[0]) ? num(bar.values[0],4) + ' MB/min' : 'Rate unknown',title:bar.partial ? 'Partial or missing minute coverage' : 'Recorded interval'}
      ]
    });
  }
  function renderProfile() {
    const host = $('ipProfile'); if (!host) return; host.replaceChildren();
    if (!selected) return;
    const allocation = selected, app = retainedApps(allocation).find(app => app.service === $('chartApp').value);
    const sameIP = !app?.ip || app.ip === allocation.ip;
    const row = sameIP ? allocation : {...app,current:allocation.current,ranking:app.ranking || {},countries:list(app.countries),decision:allocation.decision};
    const lookup = row.public_lookup || {}, risk = lookup.risk || {};
    const decision = preparedDecision(allocation);
    host.append(make('h3','IP quality','profile-heading'));
    host.append(make('p',row.ip || 'IP pending','profile-address'));
    const score = make('div',null,'profile-score'); score.append(make('strong',scoreText(row)),scoreBars(row,true)); host.append(score);
    if (allocation.shadow || allocation.decision) {
      host.append(make('div',decision.kind + ' · ' + decision.label,'profile-decision'));
      const reason = make('p',decision.reason || 'Observation only. No action is executed here.','profile-reason');
      reason.title = 'Whole-container recommendation · Prepared ' + date(decision.at) + ' · Valid until ' + date(decision.valid_until_ms);
      host.append(reason);
      for (const item of decision.apps || []) {
        if (item.action === 'keep') continue;
        host.append(make('p',serviceName(item.service) + ' · ' + String(item.reason_code || item.action).replaceAll('_',' '),'profile-reason'));
      }
    }
    const facts = make('dl',null,'profile-facts');
    const add = (label,value,title) => { const item = make('div'); if (title) item.title = title; item.append(make('dt',label),make('dd',value)); facts.append(item); };
    add('Country',countries(row));
    const isp = lookup.isp || row.isp; if (isp) add('ISP',isp,isp);
    const network = risk.is_datacenter === true ? 'Datacenter' : lookup.network_type || row.network_type || (risk.is_mobile === true ? 'Mobile' : null);
    add('Network',network || 'Not classified');
    add('Proxy',risk.is_proxy === true ? 'Detected' : risk.is_proxy === false ? 'Not detected' : 'Not checked',lookup.provider ? lookup.provider + ' · ' + date(lookup.observed_ms) : 'Awaiting a sourced lookup');
    if (app) {
      const native = nativeObservation(app), provider = providerCheck(app);
      add('Native observation',native.state + (native.stale ? ' · stale' : ''),'Source ' + date(native.at) + (native.reason ? ' · ' + native.reason : ''));
      if (app.service === 'grass') add('Last provider check',provider.state,date(provider.at) + '. This dated provider listing is separate from native connection.');
    }
    const m = row.metrics || {}, failureCount = failures(row);
    if (valid(m.sustained_15m_mb_per_minute)) add('Sustained',num(m.sustained_15m_mb_per_minute,2) + ' MB/min',m.basis);
    if (valid(failureCount)) add('Failures',num(failureCount,0),dimension(row,'failures')?.basis);
    const test = app?.connection_test || row.connection_test;
    host._row = row; host._test = test;
    const testFresh = test?.status === 'available' && test.identity_match === true && valid(test.expires_ms) && test.expires_ms > Date.now();
    if (testFresh) {
      const successful = list(test.observations).filter(o => o.status === 'ok' || o.https_reachable === true);
      const transfer = successful.find(o => valid(o.goodput_bps));
      const timing = successful.find(o => valid(o.timing_ms?.total));
      if (transfer) add('Test transfer',num(transfer.goodput_bps / 1e6,2) + ' Mbps',transfer.goodput_basis);
      if (valid(test.icmp_ping_ms)) add('Ping',num(test.icmp_ping_ms,1) + ' ms');
      else if (timing) add('HTTPS check',num(timing.timing_ms.total,1) + ' ms',timing.timing_note || 'Complete HTTPS transaction time; not ICMP latency.');
      if (valid(test.jitter_ms)) add('Jitter',num(test.jitter_ms,1) + ' ms');
      if (valid(test.packet_loss_percent)) add('Packet loss',num(test.packet_loss_percent,1) + '%');
    } else add('Connection test',test?.status === 'stale' || expired(test,'expires_ms') ? 'Expired' : 'Not recorded');
    const history = row.ip_history_summary;
    if (valid(history?.runs) && row.ip) {
      const item = make('div'), value = make('dd'), button = make('button',num(history.runs,0) + ' runs','profile-history-link');
      button.type = 'button'; button.setAttribute('aria-label','View retained history for ' + row.ip);
      button.title = list(history.apps).filter(service => service !== 'earnapp').map(serviceName).join(' · ') + ' · ' + date(history.first_seen_ms) + ' → ' + date(history.last_seen_ms);
      button.onclick = () => detail({ip:row.ip,history_all_apps:true}); value.append(button); item.append(make('dt','IP history'),value); facts.append(item);
    }
    host.append(facts);
    const outcomes = Object.values(ranking(row).outcomes || {}).filter(outcome => outcome.service !== 'earnapp' && outcome.reason && outcome.penalty < 0);
    for (const outcome of outcomes) {
      const note = make('p',serviceName(outcome.service) + ': ' + String(outcome.reason).replaceAll('_',' ') + ' · ' + num(outcome.penalty,0) + ' points','profile-reason');
      note.title = 'Recorded IP history · ' + date(outcome.observed_ms) + ' · ' + (outcome.scope || 'App/IP outcome'); host.append(note);
    }
    if (lookup.provider) host.append(make('p',lookup.provider + ' · ' + age(lookup.observed_ms) + ' ago','profile-source'));
    const legend = make('div',null,'profile-apps'); legend.append(make('h4','Apps'));
    for (const item of retainedApps(allocation)) {
      const button = make('button',null,'profile-app'); button.type = 'button'; button.style.setProperty('--app-color',appColor(item.service));
      button.setAttribute('aria-pressed',String(item.service === $('chartApp').value));
      button.setAttribute('aria-label','Show ' + serviceName(item.service) + ' traffic');
      const identity = make('span',null,'profile-app-name'); identity.append(make('i'),make('span',serviceName(item.service)));
      const native = nativeObservation(item);
      button.append(identity,make('span',native.state + (native.stale ? ' · stale' : ''),'profile-app-state'));
      button.title = appNote(item);
      button.onclick = () => selectIP(allocation,item.service); legend.append(button);
    }
    host.append(legend);
    host.dataset.clock = profileClockKey();
  }
  function profileClockKey() {
    const host = $('ipProfile') || {}, row = host._row || {};
    return [scoreExpired(row),preparedDecision(selected || row).fresh,expired(host._test,'expires_ms'),...retainedApps(selected).map(app => nativeObservation(app).stale)].join(':');
  }
  function parkChart() {
    const host = $('inlineChart');
    if (host) { host.hidden = true; $('chartParking').append(host); }
    for (const row of $('rows').querySelectorAll('.ip-expanded-row')) row.remove();
  }
  function mountChart() {
    if (!selected) return;
    const parent = [...$('rows').children].find(row => row.dataset.rowKey === rowKey(selected));
    if (!parent) return;
    const expanded = make('tr',null,'ip-expanded-row'), cell = make('td'); cell.colSpan = columns.length;
    expanded.append(cell); parent.after(expanded); cell.append($('inlineChart')); $('inlineChart').hidden = false;
    $('inlineChart').style.width = $('rankingTable').closest('.table-scroll').clientWidth + 'px';
    renderProfile();
  }
  new ResizeObserver(() => { if (!$('inlineChart').hidden) $('inlineChart').style.width = $('rankingTable').closest('.table-scroll').clientWidth + 'px'; }).observe($('rankingTable').closest('.table-scroll'));
  function clearSelection() {
    parkChart();
    for (const row of $('rows').querySelectorAll('[data-row-key]')) { row.classList.remove('selected-row'); row.setAttribute('aria-expanded','false'); row.setAttribute('aria-selected','false'); row.querySelector('.record-link')?.setAttribute('aria-expanded','false'); }

    selected = null; selectedTest = 'current'; traffic = null; trafficKey = null; chartRequest?.abort(); chartGeneration++; chart.hide();
    $('chartIP').textContent = 'Select a container or IP'; $('chartApp').replaceChildren(); $('chartApp').disabled = true; $('ipDetails').disabled = true;
    $('chartNote').textContent = 'Select a row to inspect its recorded traffic.'; drawTraffic(null);
  }
  function selectIP(row, requestedApp = null, test = 'current') {
    if (!requestedApp && selected && rowKey(selected) === rowKey(row)) { clearSelection(); return; }
    parkChart();
    selectedTest = test; selected = row; traffic = null; $('chartIP').textContent = row.ip;
    $('chartTitle').title = allocationID(row) ? 'Container ' + rowLabel(row) : 'Recorded IP history'; $('chartIP').title = countries(row) + (allocationID(row) ? ' · Container ' + rowLabel(row) : ' · Recorded IP traffic'); $('ipDetails').disabled = false;
    const previousApp = requestedApp || ($('service').value !== 'all' ? $('service').value : row.traffic_reporter || $('chartApp').value), choices = retainedApps(row);
    $('chartApp').replaceChildren();
    for (const app of choices) { const option = make('option',serviceCode(app.service)); option.title = app.name || serviceName(app.service); option.value = app.service; $('chartApp').append(option); }
    if (choices.some(app => app.service === previousApp)) $('chartApp').value = previousApp;
    $('chartApp').disabled = choices.length < 2;
    for (const tr of $('rows').rows) { tr.classList.toggle('selected-row',tr.dataset.rowKey === rowKey(row)); if (tr.dataset.rowKey) tr.setAttribute('aria-selected',String(tr.dataset.rowKey === rowKey(row))); }
    mountChart();
    for (const tr of $('rows').querySelectorAll('[data-row-key]')) { const open = tr.dataset.rowKey === rowKey(row); tr.setAttribute('aria-expanded',String(open)); tr.querySelector('.record-link')?.setAttribute('aria-expanded',String(open)); }
    loadTraffic();
  }
  async function loadTraffic() {
    renderProfile();
    chartRequest?.abort(); const ticket = ++chartGeneration; chartRequest = new AbortController();
    if (!selected || !$('chartApp').value) { traffic = null; trafficKey = null; chart.hide(); drawTraffic(null); $('chartNote').textContent = selected ? 'No retained app is available for this IP.' : 'Select a row to inspect its recorded traffic.'; return; }
    const service = $('chartApp').value, allocation = allocationID(selected), app = list(selected.apps).find(app => app.service === service), ip = app?.ip || selected.ip;
    const legendKey = $('inlineChart').querySelector('.legend-key'); if (legendKey) legendKey.style.background = appColor(service);
    $('chartIP').textContent = ip || 'IP pending';
    if (!ip) { traffic = null; trafficKey = null; chart.hide(); drawTraffic(null); $('chartNote').textContent = 'Waiting for a recorded egress IP on this container.'; return; }
    const chartQuery = new URLSearchParams({service,ip,test:selectedTest,hours:String(hours)});
    if (allocation) chartQuery.set('allocation_id',allocation);
    const url = (allocation ? '/api/ip-rankings/traffic?' : '/api/ip-history?') + chartQuery;
    const keepTraffic = traffic && trafficKey === url;
    if (!keepTraffic) { traffic = null; chart.hide(); drawTraffic(null,true); $('chartNote').textContent = `Reading ${serviceName(service)} traffic for ${ip}…`; }
    try {
      const result = await read(url,chartRequest.signal);
      if (ticket !== chartGeneration) return;
      if (allocation && result.allocation_id != null && String(result.allocation_id) !== String(allocation)) throw new Error('This source has not returned traffic for the selected container allocation');
      if (!Array.isArray(result.minutes)) throw new Error('Traffic response has no minute records');
      traffic = result; trafficKey = url; drawTraffic(result);
      const shared = selected.measurement_scope === 'container_interface';
      const reportBasis = shared ? 'Container RX + TX reported by ' + serviceName(service) + (selected.shared_counter ? '; shared interface counter' : '') : 'Network traffic reported by ' + serviceName(service);
      const selectedMinutes = result.minutes.filter(m => m.minute_ms >= result.start_ms && m.minute_ms < result.end_ms);
      const partial = selectedMinutes.some(m => m.partial || m.estimated) || selectedMinutes.filter(m => valid(m.mb)).length < hours * 60;
      $('chartNote').textContent = `${serviceName(service)} reporter · ${valid(result.total_mb) ? (partial ? '≈ ' : '') + num(result.total_mb,2) + ' MB recorded' : 'No measured total'} · ${hours}h${partial ? ' · Partial coverage' : ''}${result.anchored ? ' · Last recorded activity' : ''}`;
      $('chartNote').title = `${reportBasis} · ${ip}${allocation ? ' · Container ' + rowLabel(selected) : ''} · ${result.test?.label || result.test?.id || 'Current test'} · ${date(result.start_ms)} → ${date(result.end_ms)}. ${result.basis || ''}`;
    } catch (error) { if (ticket === chartGeneration && error.name !== 'AbortError') { if (!keepTraffic) drawTraffic(null); $('chartNote').textContent = `Traffic refresh failed: ${error.message}.${keepTraffic ? ' Showing the previous dated result.' : ' No values are shown.'}`; } }
  }

  function rankingDetails(row) {
    const rank = ranking(row), dimensions = list(rank.dimensions);
    if (rank.version === 'ip-reputation-v2') {
      const outcomes = Object.values(rank.outcomes || {}).filter(outcome => outcome.service !== 'earnapp');
      return `<h3>IP reputation</h3><dl class="detail-grid">${fact('Score',scoreText(row)).replace('<dd>','<dd data-evaluation-score>')}${valid(row.rank) ? fact('Rank',num(row.rank,0)).replace('<dd>','<dd data-evaluation-rank>') : ''}${fact('History',num(rank.known_episodes,0) + ' recorded episodes')}${fact('Scope','Retained IP history across apps')}${fact('Calculated',date(rank.calculated_ms))}${fact('Version',rank.version)}</dl><p>${esc(rank.basis || '')}</p>${recommendationDetails(row)}<h3>Recorded app observations</h3><dl class="detail-grid">${retainedApps(row).map(app => {
        const native = nativeObservation(app), provider = providerCheck(app);
        return fact(serviceName(app.service) + ' native',native.state + (native.stale ? ' · stale' : '') + ' · ' + date(native.at)) + (app.service === 'grass' ? fact('Last Grass provider check',provider.state + ' · ' + date(provider.at)) : '');
      }).join('')}</dl>${nativeIdentityDetails(row)}${dimensions.length ? `<div class="table-scroll"><table class="table table-sm detail-table"><thead><tr><th>Score component</th><th>Points</th></tr></thead><tbody>${dimensions.map(d => `<tr><td>${esc(d.name || d.key)}</td><td>${esc(valid(d.value) ? (d.value > 0 ? '+' : '') + num(d.value,0) : 'Not supplied')}</td></tr>`).join('')}</tbody></table></div>` : ''}${outcomes.length ? `<h3>Dated reputation outcomes</h3><ul>${outcomes.map(outcome => `<li>${esc(serviceName(outcome.service))} · ${esc(outcome.reason || outcome.kind || 'Recorded outcome')} · ${esc(date(outcome.observed_ms))}${valid(outcome.penalty) ? ' · ' + esc(num(outcome.penalty,0)) + ' points' : ''}</li>`).join('')}</ul>` : ''}`;
    }
    return `<h3>Evaluation</h3><dl class="detail-grid">${fact('Score',valid(rank.score) ? num(rank.score,0) : scoreExpired(row) ? 'Expired' : 'Unknown').replace('<dd>','<dd data-evaluation-score>')}${fact('Version',rank.version || 'Unknown')}${fact(rank.version === 'ip-reputation-v2' ? 'Measurement coverage' : 'Confidence',valid(rank.confidence) ? num(rank.confidence*100,1) + '%' : textValue(rank.confidence))}${fact('Evaluation app',serviceName(metricScope(row)))}${fact('Lane',rank.lane || 'Unknown')}${fact('Rank',scoreExpired(row) ? 'Expired' : valid(row.rank) ? num(row.rank,0) : 'Not supplied').replace('<dd>','<dd data-evaluation-rank>')}${fact('Evaluation window',row.evaluation_window ? date(row.evaluation_window.start_ms) + ' → ' + date(row.evaluation_window.end_ms) : row.current_evaluation ? date(row.current_evaluation.start_ms) + ' → ' + date(row.current_evaluation.end_ms) : textValue(rank.window || rank.basis))}${fact('Episode',rank.episode_id || row.evaluation_window?.episode || 'Unknown')}${fact('Observation grace until',date(rank.grace_until_ms))}${fact('Eligible hours / minimum peers',num(rank.eligible_hours,0) + ' / ' + num(rank.peer_min,0))}</dl>${list(rank.reasons).length ? `<ul>${rank.reasons.map(reason => `<li>${esc(textValue(reason))}</li>`).join('')}</ul>` : '<p>No evaluator reasons supplied.</p>'}${dimensions.length ? `<div class="table-scroll"><table class="table table-sm detail-table"><thead><tr><th>Dimension</th><th>Value</th><th>Status</th><th>Evidence / coverage</th></tr></thead><tbody>${dimensions.map(d => `<tr><td>${esc(d.name || d.key || d.id || 'Unnamed')}</td><td>${d.value == null ? 'Unknown' : esc(valid(d.value) ? num(d.value,4) : textValue(d.value))}${d.unit ? ' ' + esc(d.unit) : ''}</td><td>${esc(d.status || 'Unknown')}</td><td>${esc(textValue(d.basis))}${d.coverage != null ? '<br>' + esc(textValue(d.coverage)) : ''}${d.observed_ms != null ? '<br>' + esc(date(d.observed_ms)) : ''}${d.source ? '<br>Source: ' + esc(textValue(d.source)) : ''}</td></tr>`).join('')}</tbody></table></div>` : '<p>Quality dimensions have not been supplied. Unknown evidence is not zero quality.</p>'}`;
  }
  function recommendationDetails(row) {
    if (!row.shadow && !row.decision) return '';
    const decision = preparedDecision(row);
    return `<h3>${esc(decision.kind)} recommendation · observation only</h3><p data-prepared-decision><strong>${esc(decision.label)}</strong>${decision.reason ? ' · ' + esc(decision.reason) : ''}</p><p>Prepared ${esc(date(decision.at))} · Valid until ${esc(date(decision.valid_until_ms))}. No action is executed here.</p>${list(decision.apps).length ? `<ul>${decision.apps.map(item => `<li>${esc(serviceName(item.service))} · ${esc(String(item.reason_code || item.action).replaceAll('_',' '))}</li>`).join('')}</ul>` : ''}`;
  }
  function nativeIdentityDetails(row) {
    if (!allocationID(row)) return '';
    const observed = retainedApps(row).filter(app => app.run || app.native?.run);
    if (!observed.length) return '';
    return `<details><summary>Current native run identities</summary>${observed.map(app => {
      const native = app.native || {}, history = native.recent_identity_history;
      return `<h3>${esc(serviceName(app.service))}</h3><dl class="detail-grid">${fact('Run',native.run || app.run)}${fact('Device',native.device || 'Not reported')}${fact('Sample source',date(native.source_ms))}${fact('Native observation',date(native.observed_ms))}${fact('First received',date(native.received_ms))}${history ? fact('Recent runtime history',num(history.runs,0) + ' runs · ' + num(history.devices,0) + ' devices' + (history.partial ? ' · partial' : '')) + fact('History window',date(history.window_start_ms) + ' → ' + date(history.window_end_ms)) : ''}</dl>`;
    }).join('')}</details>`;
  }
  function publicLookupDetails(row) {
    const lookup = row.public_lookup;
    if (!lookup || typeof lookup !== 'object' || Array.isArray(lookup)) return '';
    const risk = lookup.risk && typeof lookup.risk === 'object' && !Array.isArray(lookup.risk) ? lookup.risk : {};
    const location = lookup.location && typeof lookup.location === 'object' && !Array.isArray(lookup.location) ? lookup.location : {};
    const score = lookup.risk_score;
    const recorded = value => typeof value === 'string' && value.trim() !== '';
    const present = (label,value) => recorded(value) ? fact(label,value) : '';
    const detection = value => value === true ? 'Detected' : value === false ? 'Not detected' : 'Unknown';
    const error = lookup.error == null || lookup.error === '' ? 'None reported' : textValue(lookup.error);
    const riskValue = score && typeof score === 'object' && !Array.isArray(score) &&
      lookup.provider === 'IPQuery' && score.provider === lookup.provider && score.source === lookup.source &&
      Number.isInteger(lookup.observed_ms) && lookup.observed_ms > 0 && score.observed_ms === lookup.observed_ms &&
      score.units === 'score_points' && score.minimum === 0 && score.maximum === 100 &&
      score.direction === 'higher_is_more_risk' && Number.isInteger(score.value) && score.value >= 0 && score.value <= 100
        ? score.value + ' / 100 · higher means more risk' : 'Unknown';
    const coordinate = (value,limit) => typeof value === 'number' && Number.isFinite(value) && Math.abs(value) <= limit;
    const geo = present('Country',location.country || location.country_code || lookup.country) +
      present('Region / state',location.state) + present('City',location.city) + present('Postal code',location.zipcode) +
      (coordinate(location.latitude,90) && coordinate(location.longitude,180) ? fact('Approximate coordinates',location.latitude + ', ' + location.longitude) : '') +
      present('Timezone',location.timezone) + present('Local time at lookup',location.localtime);
    const identity = present('IP address',lookup.ip) + present('ASN',lookup.asn) + present('ISP',lookup.isp) +
      present('Organization',lookup.organization) + present('Reported network type',lookup.network_type);
    const pending = lookup.upgrade_pending === true ? '<p>Additional location details and provider risk score were not retained by the earlier lookup. They are pending a refreshed lookup.</p>' : '';
    return `<section class="public-lookup" aria-label="Public IP lookup"><h3>Public IP lookup</h3><dl class="detail-grid">${identity}${geo}${fact('Provider',textValue(lookup.provider))}${fact('Source',textValue(lookup.source))}${fact('Lookup received',date(lookup.observed_ms))}${fact('Lookup error',error)}${fact('Datacenter',detection(risk.is_datacenter))}${fact('VPN',detection(risk.is_vpn))}${fact('Proxy',detection(risk.is_proxy))}${fact('Tor',detection(risk.is_tor))}${fact('Mobile',detection(risk.is_mobile))}${lookup.cache_version === 2 ? fact('Provider risk score',riskValue) : ''}</dl>${pending}<p>Not detected means this provider did not flag that category in this observation. It does not establish that the IP is verified clean.</p></section>`;
  }
  function extraEvidence(row) {
    const m = row.metrics || {};
    return `<h3>Selected episode measurements</h3><p>${esc(m.basis || 'No episode measurement basis supplied.')}</p><dl class="detail-grid">${fact('Peak',valid(m.peak_mb_per_minute) ? num(m.peak_mb_per_minute,4) + ' MB/min' : 'Unknown')}${fact('95th percentile',valid(m.p95_mb_per_minute) ? num(m.p95_mb_per_minute,4) + ' MB/min' : 'Unknown')}${fact('Median rate',valid(m.median_mb_per_minute) ? num(m.median_mb_per_minute,4) + ' MB/min' : 'Unknown')}${fact('Sustained 15-minute rate',valid(m.sustained_15m_mb_per_minute) ? num(m.sustained_15m_mb_per_minute,4) + ' MB/min' : 'Unknown')}${fact('Sustained windows',num(m.sustained_windows,0))}${fact('Top 1% of minutes / traffic',valid(m.burst_top_1pct_fraction) ? num(m.burst_top_1pct_fraction*100,2) + '%' : 'Unknown')}${fact('Positive minute share',valid(m.positive_minute_fraction) ? num(m.positive_minute_fraction*100,2) + '%' : 'Unknown')}${fact('Recorded minutes',num(m.measured_minutes,2))}${fact('Coverage',valid(m.coverage) ? num(m.coverage*100,1) + '%' : 'Unknown')}</dl><h3>Network classification evidence</h3><dl class="detail-grid">${fact('Network organization',row.network_organization || 'Unknown')}${fact('ISP',row.isp || 'Unknown')}${fact('Lookup country',country(row.lookup_country || '') || 'Unknown')}${fact('Metadata observed',date(row.metadata_observed_ms))}${fact('Metadata source',textValue(row.metadata_source))}</dl><p>${esc(row.metadata_note || 'No sourced network classification supplied.')}</p>${list(row.flags).map(flag => `<div class="provider"><strong>${esc(flag.kind || 'Flag')}</strong><p>${esc(textValue(flag.scope))} · ${esc(flag.cidr || 'Prefix unknown')} · ${flag.fresh === true ? 'Fresh classification' : flag.fresh === false ? 'Stale classification' : 'Freshness unknown'}</p><p>${esc(date(flag.observed_ms))} · ${esc(textValue(flag.source))}</p><p>${esc(flag.note || flag.basis || '')}</p></div>`).join('')}`;
  }
  function renderDetail(value) {
    value = {...value,row:supportedRow(value.row),runs:list(value.runs).filter(run => run.service !== 'earnapp')};
    $('detailBody')._evidence = value;
    const row = {...value.row}, runs = list(value.runs);
    if (row.current === true && (valid(row.current_expires_ms) && row.current_expires_ms < Date.now() || valid(value.generated_ms) && Date.now()-value.generated_ms > 180000)) {
      if (row.ranking?.version !== 'ip-reputation-v2' && row.ranking?.lane !== 'flagged') row.ranking = {...ranking(row),score:null,confidence:null,lane:'stale'};
      row.current = null; row.current_rate = null;
    }
    const cost = row.cost || {};
    $('detailTitle').textContent = countries(row) + ' · ' + (row.ip || 'IP pending');
    $('detailSubtitle').textContent = (allocationID(row) ? stateText(row) : 'Retained IP history across allocations') + (metricScope(row) ? ' · Returned evaluation: ' + serviceName(metricScope(row)) : '') + ' · Last observation ' + date(row.last_seen_ms);
    if (!row.ip) {
      $('detailBody').innerHTML = `<h3>Awaiting public IP</h3><p>This observation has no reported public IP. IP-attributed measurements are unavailable.</p><dl class="detail-grid">${fact('Initial score',scoreText(row))}${fact('Cloud state',row.cloud_state || 'Not reported')}${fact('Inventory',stateText(row))}${fact('Container allocation',allocationID(row))}${fact('Group',row.group_id || 'Not reported')}${fact('Instance',row.instance || 'Not reported')}${fact('Machine',row.machine || 'Not reported')}</dl>${recommendationDetails(row)}<h3>Recorded app observations</h3><dl class="detail-grid">${retainedApps(row).map(app => {
        const native = nativeObservation(app);
        return fact(serviceName(app.service),native.state + (native.stale ? ' · stale' : '')) + (valid(native.at) ? fact('Native observation',date(native.at)) : '') + (native.reason ? fact('Reason',native.reason) : '');
      }).join('')}</dl>${nativeIdentityDetails(row)}`;
      return;
    }
    const canChart = view === 'historical' || !!allocationID(row);
    $('detailBody').innerHTML = `${rankingDetails(row)}${publicLookupDetails(row)}${extraEvidence(row)}<dl class="detail-grid">${fact('Container allocation',allocationID(row) || 'Historical IP lookup')}${fact('Group',row.group_id || row.group || 'Unknown')}${fact('Instance',row.instance || 'Unknown')}${fact('Machine',row.machine || 'Unknown')}${fact('Measurement scope',row.measurement_scope === 'container_interface' ? 'Whole container RX + TX; reporter ' + serviceName(row.traffic_reporter || metricScope(row)) : 'Retained reporter observations')}${fact('Shared counter',row.shared_counter === true ? 'Shared across apps; counted once' : row.shared_counter === false ? 'Not marked shared' : 'Unknown')}${fact('First observation',date(row.first_seen_ms))}${fact('Last observation',date(row.last_seen_ms))}${fact('Address scope',row.address_scope || 'Unknown')}${fact('ASN',textValue(row.asn))}${fact('Network type',textValue(row.network_type))}${fact('IP earnings','Unknown — no exclusive attribution')}${fact('Observed-span allocation cost',valid(cost.estimated_usd) ? '$' + num(cost.estimated_usd,4) + ' · partial estimate' : 'Unknown')}${fact('Matched allocations',num(cost.allocations,0))}${fact('Economics',row.economic_status || 'Unknown')}</dl><p>${esc(cost.basis || 'Observed-span allocation estimates do not establish the full per-IP bill.')}</p><h3>Dated app and run identities</h3><p>${runs.length ? num((value.run_offset || 0) + 1,0) + '–' + num((value.run_offset || 0) + runs.length,0) : '0'} of ${num(value.total_runs,0)} retained identity segments</p>${runs.map((r,i) => {
      const native = r.native || {}, events = r.events || {};
      return `<details ${i === 0 ? 'open' : ''}><summary><span class="run-summary"><span>${esc(serviceName(r.service))} · ${esc(native.state || 'Native evidence unavailable')}</span><span>${esc(date(r.first_seen_ms))} → ${esc(date(r.last_seen_ms))}</span></span></summary><p>${esc(r.test_label || r.test || 'Test identity unavailable')}${canChart ? ` · <button class="btn btn-outline-secondary btn-sm" data-run-chart="${i}">Show traffic</button>` : ''}</p><dl class="detail-grid">${fact('Group',r.group_id || r.group || 'Unknown')}${fact('Instance',r.instance || 'Unknown')}${fact('Machine',r.machine || 'Unknown')}${fact('Run',r.run || 'Unknown')}${fact('Device',native.device || 'Unknown')}${fact('Inventory / cloud',stateText(r) + ' / ' + (r.cloud_state || 'Unknown'))}${fact('Native source time',date(native.observed_ms))}${fact('Receipt time',date(native.received_ms))}${fact('Native failure category',native.category || 'Unclassified')}${fact('Reason',native.reason || 'No explicit failure reason retained')}${fact('Measured traffic',valid(r.total_mb) ? num(r.total_mb,4) + ' MB' : 'Unknown')}${fact('Average measured rate',valid(r.average_mb_per_minute) ? num(r.average_mb_per_minute,4) + ' MB/min' : 'Unknown')}${fact('Recorded duration',duration(r.duration_ms))}${fact('Recorded duration / observed span',valid(r.coverage_fraction) ? num(r.coverage_fraction * 100,1) + '%' + (r.partial ? ' · partial' : '') : 'Unknown')}${fact('Saved samples',num(r.samples,0))}${fact('Interval allocation',r.estimated ? 'Includes estimated intervals' : r.estimated === false ? 'Retained exact intervals' : 'Unknown')}</dl><h3>Provider observations · separate from IP earnings</h3>${list(r.provider_observations).map(p => `<div class="provider"><strong>${valid(p.amount) ? num(p.amount,5) + ' ' + esc(p.unit || 'Unknown unit') : 'Amount unavailable'}</strong> · ${esc(p.scope || 'Unknown scope')}<p>${esc(date(p.observed_ms))}${p.device ? ' · device ' + esc(p.device) : ''}${p.epoch != null ? ' · epoch ' + esc(p.epoch) : ''}</p><p>${esc(p.note || 'No exclusive IP attribution established.')}</p></div>`).join('') || '<p>No provider observations returned.</p>'}<h3>Disconnect and recovery evidence</h3><p>${esc(events.coverage || 'Coverage unavailable.')} Accepted requests alone do not prove successful recovery.</p>${list(events.rows).length ? `<div class="table-scroll"><table class="table table-sm detail-table"><thead><tr><th>Source / receipt · IST</th><th>Event / action</th><th>Observed reason</th><th>Response</th></tr></thead><tbody>${events.rows.map(e => `<tr><td>${esc(date(e.time_ms))}<br>Received ${esc(date(e.received_ms))}</td><td>${esc([e.event || e.kind,e.action].filter(Boolean).join(' / ') || 'Unspecified')}</td><td>${esc(e.reason || e.error || e.state || 'Not retained')}${e.category ? '<br>' + esc(e.category) : ''}</td><td>${esc(e.http_status ?? 'Unknown')}</td></tr>`).join('')}</tbody></table></div>` : '<p>No matching events returned. This does not establish a failure-free run.</p>'}</details>`;
    }).join('')}<div class="ip-detail-paging"><button class="btn btn-outline-secondary btn-sm" data-run-page="${Math.max(0,(value.run_offset || 0)-50)}" ${!value.run_offset ? 'disabled' : ''}>Newer runs</button><button class="btn btn-outline-secondary btn-sm" data-run-page="${(value.run_offset || 0)+50}" ${value.has_more ? '' : 'disabled'}>Older runs</button></div><details><summary>Allocation intervals counted once</summary>${list(cost.intervals).map(interval => `<p>${esc(interval.instance || 'Unknown instance')} · ${esc(date(interval.start_ms))} → ${esc(date(interval.end_ms))} · ${valid(interval.estimated_usd) ? '$' + num(interval.estimated_usd,5) : 'Unknown'}</p>`).join('') || '<p>No matching recorded billing intervals.</p>'}</details><ul>${list(value.uncertainty).map(note => `<li>${esc(note)}</li>`).join('')}</ul>`;
  }
  async function detail(row, runOffset = 0) {
    if (typeof row === 'string') row = {ip:row};
    detailSelection = row; const ip = row.ip, allocation = allocationID(row);
    detailIP = ip; detailRequest?.abort(); detailRequest = new AbortController(); const ticket = ++detailGeneration;
    if (!$('detail').open) { detailOpener = document.activeElement; $('detail').showModal(); }
    $('detailTitle').textContent = ip || 'Container ' + rowLabel(row); $('detailSubtitle').textContent = 'Exact dated identities and provider scopes'; $('detailBody').textContent = 'Reading container and IP details…';
    try {
      const params = new URLSearchParams({service:row.history_all_apps ? 'all' : $('service').value,run_offset:String(runOffset)});
      if (allocation) params.set('allocation_id',allocation); else params.set('ip',ip || '');
      const result = await read('/api/ip-rankings?' + params,detailRequest.signal);
      if (ticket !== detailGeneration) return;
      if (allocation && String(allocationID(result.row)) !== String(allocation)) throw new Error('Details for this container allocation are not available from this source yet');
      if (!result.row) throw new Error(result.error || 'No retained details for this IP');
      renderDetail(result);
    } catch (error) { if (ticket === detailGeneration && error.name !== 'AbortError') { $('detailBody').replaceChildren(make('p',error.message,'ip-error')); const retry = make('button','Retry','btn btn-outline-secondary btn-sm'); retry.onclick = () => detail(row,runOffset); $('detailBody').append(retry); } }
  }
  $('rows').addEventListener('click',event => { const button = event.target.closest('[data-detail-key]'); if (button) { const row = list(data?.rows).find(row => rowKey(row) === button.dataset.detailKey); if (row) detail(row); } });
  $('detailBody').addEventListener('click',event => { const button = event.target.closest('[data-run-page]'); if (button && !button.disabled) detail(detailSelection,Number(button.dataset.runPage));
    const chartButton = event.target.closest('[data-run-chart]');
    if (chartButton) { const value = $('detailBody')._evidence, run = value?.runs?.[Number(chartButton.dataset.runChart)]; if (run) { $('detail').close(); selectIP(value.row,run.service,run.test || 'current'); $('trafficChart').focus(); $('chartTitle').scrollIntoView({block:'start',behavior:'smooth'}); } } });
  $('closeDetail').onclick = () => $('detail').close();
  $('detail').addEventListener('close',() => { detailRequest?.abort(); detailGeneration++; if (detailOpener?.isConnected) detailOpener.focus({preventScroll:true}); pollRevision(); });
  $('closeChart').onclick = () => { const key = selected && rowKey(selected); clearSelection(); [...$('rows').children].find(row => row.dataset.rowKey === key)?.querySelector('.record-link')?.focus({preventScroll:true}); };
  $('ipDetails').onclick = () => { if (selected) detail(selected); };
  $('chartApp').onchange = () => { selectedTest = 'current'; loadTraffic(); };
  function changeView(value) {
    if (view === value) return; view = value; offset = 0; clearSelection(); clearTimeout(queryTimer);
    $('historyControls').hidden = view === 'current';
    for (const tab of document.querySelectorAll('[data-view]')) { const active = tab.dataset.view === view; tab.classList.toggle('active',active); tab.setAttribute('aria-selected',String(active)); tab.tabIndex = active ? 0 : -1; if (active) $('rankingView').setAttribute('aria-labelledby',tab.id); }
    load(true);
  }
  for (const tab of document.querySelectorAll('[data-view]')) tab.onclick = () => changeView(tab.dataset.view);
  document.querySelector('[aria-label="IP ranking views"]').onkeydown = event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return; event.preventDefault();
    const tabs = [...document.querySelectorAll('[data-view]')], index = tabs.findIndex(tab => tab.dataset.view === view);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (index + (event.key === 'ArrowLeft' ? tabs.length-1 : 1)) % tabs.length;
    changeView(tabs[next].dataset.view); tabs[next].focus();
  };
  $('historyState').onchange = () => { offset = 0; clearSelection(); load(true); };
  $('service').onchange = () => { offset = 0; clearSelection(); load(true); };
  $('search').oninput = () => { clearTimeout(queryTimer); queryTimer = setTimeout(() => { offset = 0; clearSelection(); load(true); },250); };
  $('previous').onclick = () => { offset = Math.max(0,offset-100); clearSelection(); load(true); };
  $('next').onclick = () => { offset += 100; clearSelection(); load(true); };
  $('refresh').onclick = () => { load(); if (selected) loadTraffic(); };
  function expireDisplayedValues() {
    if (data) {
      const updated = $('totals').querySelector('.metric:last-child .metric-value'); if (updated) updated.textContent = age(rankingTime(data));
      if (lastTableStatus && !revisionNotice) { const info = lastTableStatus, stale = info.state === 'live' && (!valid(rankingTime(data)) || Date.now()-rankingTime(data) > 180000); if (stale) setStatus('stale',info.message,info.at,false); else window.setDataStatus($('connection'),info.state,info.at,info.message,info.state === 'live' ? 'Updated' : null); }
    }
    const rows = list(data?.rows), key = rows.map(row => [rowKey(row),rateExpired(row),scoreExpired(row),availability(row),...retainedApps(row).map(app => nativeObservation(app).stale)].join(':')).join('|');
    if (data && key !== freshnessKey) { const changed = freshnessKey !== ''; freshnessKey = key; if (changed) renderRows(); }
    if (selected && !$('inlineChart').hidden && $('ipProfile') && $('ipProfile').dataset.clock !== profileClockKey()) renderProfile();
    const detailRow = $('detailBody')._evidence?.row;
    if ($('detail').open && detailRow && !preparedDecision(detailRow).fresh) {
      const recommendation = $('detailBody').querySelector('[data-prepared-decision]');
      if (recommendation) recommendation.textContent = preparedDecision(detailRow).label;
    }
    if ($('detail').open && detailRow && scoreExpired(detailRow)) {
      for (const node of $('detailBody').querySelectorAll('[data-evaluation-score],[data-evaluation-rank]')) { node.textContent = 'Expired'; node.title = 'Awaiting a fresh performance observation'; }
    }
  }
  summary(null); drawTraffic(null); load();
  setInterval(expireDisplayedValues,1000);
  setInterval(pollRevision,1000);
  document.addEventListener('visibilitychange',() => {
    if (document.hidden) revisionRequest?.abort();
    else { expireDisplayedValues(); pollRevision(); }
  });
})();
