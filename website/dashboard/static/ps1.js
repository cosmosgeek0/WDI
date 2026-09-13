(() => {
  "use strict";
  /* Tabler Icons — https://tabler.io/icons
MIT License

Copyright (c) 2020-2026 Paweł Kuna

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
  const serviceIcons={"HG":"<path d=\"M4 18v-5l4 -2l4 2v5l-4 2l-4 -2\" /> <path d=\"M8 11v-5l4 -2l4 2v5\" /> <path d=\"M12 13l4 -2l4 2v5l-4 2l-4 -2\" />","PS":"<path d=\"M12 4l-8 4l8 4l8 -4l-8 -4\" /> <path d=\"M4 12l8 4l8 -4\" /> <path d=\"M4 16l8 4l8 -4\" />","PR":"<path d=\"M3 7a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3v-2\" /> <path d=\"M3 15a3 3 0 0 1 3 -3h12a3 3 0 0 1 3 3v2a3 3 0 0 1 -3 3h-12a3 3 0 0 1 -3 -3l0 -2\" /> <path d=\"M7 8l0 .01\" /> <path d=\"M7 16l0 .01\" />","RP":"<path d=\"M4 7a8 4 0 1 0 16 0a8 4 0 1 0 -16 0\" /> <path d=\"M4 7c0 .664 .088 1.324 .263 1.965l2.737 10.035c.5 1.5 2.239 2 5 2s4.5 -.5 5 -2c.333 -1 1.246 -4.345 2.737 -10.035a7.45 7.45 0 0 0 .263 -1.965\" />","EF":"<path d=\"M4 15a2 2 0 0 1 2 -2h1a2 2 0 0 1 2 2v3a2 2 0 0 1 -2 2h-1a2 2 0 0 1 -2 -2l0 -3\" /> <path d=\"M15 15a2 2 0 0 1 2 -2h1a2 2 0 0 1 2 2v3a2 2 0 0 1 -2 2h-1a2 2 0 0 1 -2 -2l0 -3\" /> <path d=\"M4 15v-3a8 8 0 0 1 16 0v3\" />","TM":"<path d=\"M3 17l6 -6l4 4l8 -8\" /> <path d=\"M14 7l7 0l0 7\" />","PA":"<path d=\"M12 3a3 3 0 0 1 3 3c0 1.113 -.6 2.482 -1.5 3l1.5 7h-6l1.5 -7c-.9 -.518 -1.5 -1.887 -1.5 -3a3 3 0 0 1 3 -3\" /> <path d=\"M8 9h8\" /> <path d=\"M6.684 16.772a1 1 0 0 0 -.684 .949v1.279a1 1 0 0 0 1 1h10a1 1 0 0 0 1 -1v-1.28a1 1 0 0 0 -.684 -.948l-2.316 -.772h-6l-2.316 .772\" />","EA":"<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0\" /> <path d=\"M14 8h-4v8h4\" /> <path d=\"M10 12h2.5\" />","GR":"<path d=\"M2 9a10 10 0 1 0 20 0\" /> <path d=\"M12 19a10 10 0 0 1 10 -10\" /> <path d=\"M2 9a10 10 0 0 1 10 10\" /> <path d=\"M12 4a9.7 9.7 0 0 1 2.99 7.5\" /> <path d=\"M9.01 11.5a9.7 9.7 0 0 1 2.99 -7.5\" />","◫":"<path d=\"M4 5a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -4\" /> <path d=\"M14 5a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -4\" /> <path d=\"M4 15a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -4\" /> <path d=\"M14 15a1 1 0 0 1 1 -1h4a1 1 0 0 1 1 1v4a1 1 0 0 1 -1 1h-4a1 1 0 0 1 -1 -1l0 -4\" />"};
  window.serviceIcon=code=>'<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+serviceIcons[code]+'</svg>';
  const sidebar=document.getElementById('sidebar'),themeButton=document.getElementById('themeToggle');
  function applyTheme(theme){document.documentElement.dataset.bsTheme=theme;if(themeButton){const label=theme==='dark'?'Switch to light mode':'Switch to dark mode';themeButton.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(theme==='dark'?'<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.93 4.93l1.42 1.42M17.65 17.65l1.42 1.42M4.93 19.07l1.42-1.42M17.65 6.35l1.42-1.42"/>':'<path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z"/>')+'</svg>';themeButton.setAttribute('aria-label',label);themeButton.title=label;}}
  applyTheme(localStorage.getItem('salad-theme')==='dark'?'dark':'light');
  if(themeButton)themeButton.onclick=()=>{const theme=document.documentElement.dataset.bsTheme==='dark'?'light':'dark';applyTheme(theme);localStorage.setItem('salad-theme',theme);};
  if(sidebar){for(const mark of sidebar.querySelectorAll('.service-mark')){mark.innerHTML=window.serviceIcon(mark.textContent);}const setSidebar=expanded=>{sidebar.dataset.state=expanded?'expanded':'collapsed';document.body.classList.toggle('sidebar-collapsed',!expanded);document.getElementById('sidebarScrim').hidden=!expanded||!matchMedia('(max-width:900px)').matches;document.getElementById('sidebarExpand').setAttribute('aria-expanded',String(expanded));};document.getElementById('sidebarExpand').onclick=()=>setSidebar(true);document.getElementById('sidebarCollapse').onclick=()=>setSidebar(false);document.getElementById('sidebarScrim').onclick=()=>setSidebar(false);matchMedia('(max-width:900px)').addEventListener('change',()=>setSidebar(false));setSidebar(false);}
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const formats = new Map();
  const number = (value, digits = 1) => {
    if (!finite(value)) return "—";
    if (!formats.has(digits)) formats.set(digits, new Intl.NumberFormat(undefined, {maximumFractionDigits: digits}));
    const format=formats.get(digits), small=value>0&&value<10**-digits;
    return (small?"<":"")+format.format(small?10**-digits:value);
  };
  window.formatBandwidth=number;
  const localDate=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});
  window.saladTime={format:at=>finite(at)&&at>0?localDate.format(at)+' IST':'Not recorded'};
  document.addEventListener('DOMContentLoaded',()=>{
    const host=document.querySelector('.page-actions')||document.querySelector('.page-head');if(!host)return;
    const node=document.createElement('time');node.className='local-clock';node.title='India Standard Time · UTC+05:30';const group=document.createElement('div');group.className='header-status';const status=host.querySelector('#connection,#efStatus');group.append(node);if(status)group.append(status);host.prepend(group);
    const format=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Kolkata',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
    const tick=()=>{const now=new Date();node.dateTime=now.toISOString();node.textContent=format.format(now)+' IST';};tick();setInterval(tick,1000);
  });
  const warningMark='<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.25" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2 15 14H1Z"/><path d="M8 6v4m0 2v.1"/></svg>';
  let issuePopup=null,issueAnchor=null,issueTimer=null;
  function hideIssue(){if(issuePopup)issuePopup.hidden=true;issueAnchor=null;}
  function showIssue(anchor){
    clearTimeout(issueTimer);if(!anchor.dataset.issue)return;
    if(!issuePopup){issuePopup=document.createElement('div');issuePopup.id='issuePopover';issuePopup.className='issue-popover';issuePopup.setAttribute('role','tooltip');document.body.append(issuePopup);issuePopup.onpointerenter=()=>clearTimeout(issueTimer);issuePopup.onpointerleave=()=>{issueTimer=setTimeout(hideIssue,150);};}
    issueAnchor=anchor;issuePopup.textContent=anchor.dataset.issue;issuePopup.hidden=false;
    const box=anchor.getBoundingClientRect();issuePopup.style.left=Math.max(8,Math.min(box.left,innerWidth-issuePopup.offsetWidth-8))+'px';issuePopup.style.top=Math.max(8,box.bottom+issuePopup.offsetHeight+12<=innerHeight?box.bottom+6:box.top-issuePopup.offsetHeight-6)+'px';
  }
  window.attachIssue=(node,text)=>{
    node.dataset.issue=text||'';node.setAttribute('aria-describedby','issuePopover');
    node.onpointerenter=()=>showIssue(node);node.onpointerleave=()=>{issueTimer=setTimeout(hideIssue,150);};node.onfocus=()=>showIssue(node);node.onblur=hideIssue;
  };
  window.issueIcon=(text,severity='warning')=>{
    const button=document.createElement('button');button.type='button';button.className='issue-indicator';button.dataset.severity=severity;button.innerHTML=warningMark;button.setAttribute('aria-label','Show client issue');window.attachIssue(button,text);button.onclick=event=>{event.stopPropagation();showIssue(button);};return button;
  };
  document.addEventListener('pointerdown',event=>{if(issueAnchor&&!issueAnchor.contains(event.target)&&!issuePopup?.contains(event.target))hideIssue();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape')hideIssue();});
  window.setDataStatus=(node,state,at,detail='',labelOverride=null)=>{
    node.className='data-status';node.dataset.state=state;node.setAttribute('aria-live','off');node.tabIndex=0;
    const age=finite(at)&&Date.now()>=at?Math.floor((Date.now()-at)/1000):null,elapsed=age===null?'':age<60?age+'s':age<3600?Math.floor(age/60)+'m':Math.floor(age/3600)+'h';
    const label=labelOverride||{live:'Live',partial:'Partial',stale:'Stale',error:'Error',loading:'Loading'}[state],text=label+(elapsed&&state!=='loading'?' · '+elapsed:'');
    if(node.dataset.label!==text){const icon=document.createElement('span');icon.className='data-status-icon';icon.innerHTML=state==='error'?warningMark:state==='stale'?'<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.25"><circle cx="8" cy="8" r="6"/><path d="M8 4v4l3 2"/></svg>':'<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="3" fill="currentColor"/></svg>';const value=document.createElement('span');value.textContent=text;node.replaceChildren(icon,value);node.dataset.label=text;}
    node.removeAttribute('title');window.attachIssue(node,[detail,finite(at)?'Data observed '+window.saladTime.format(at):'No source observation yet.'].filter(Boolean).join('\n'));
  };
  window.bindIPRow=(row,ip,selected,onSelect)=>{
    if(!ip)return;row.classList.add('selectable-row');row.classList.toggle('selected-row',selected);row.dataset.ip=ip;row.setAttribute('aria-selected',String(selected));row.tabIndex=0;
    const link=row.querySelector('.record-link');if(link)link.onclick=null;
    const activate=keyboard=>{const body=row.parentElement;onSelect();if(keyboard){const next=row.isConnected?row:[...(body?.rows||[])].find(r=>r.dataset.ip===ip);next?.focus({preventScroll:true});}};
    row.onclick=event=>{if(event.target.closest('button,a,input,select,summary')&&!event.target.closest('.record-link'))return;activate(event.detail===0);};
    row.onkeydown=event=>{if(event.target===row&&['Enter',' '].includes(event.key)){event.preventDefault();activate(true);}};
  };
  function positionMenu(menu,button){
    const box=button.getBoundingClientRect(),below=innerHeight-box.bottom-12,above=box.top-12;
    menu.style.position='fixed';menu.style.minWidth=box.width+'px';menu.style.maxWidth=(innerWidth-16)+'px';menu.style.maxHeight=Math.max(60,Math.max(below,above))+'px';menu.style.right='auto';menu.style.bottom='auto';menu.style.top='0px';menu.style.left='0px';
    const width=menu.offsetWidth,height=menu.offsetHeight;menu.style.left=Math.max(8,Math.min(box.right-width,innerWidth-width-8))+'px';menu.style.top=Math.max(8,below>=height||below>=above?box.bottom+4:box.top-height-4)+'px';
  }
  window.createTableSort=({table,controls,columns,defaultKey,onChange})=>{
    const state={key:defaultKey,direction:columns.find(c=>c.key===defaultKey)?.direction||'desc'},collator=new Intl.Collator('en',{numeric:true,sensitivity:'base'});
    const group=make('div',null,'chart-select table-sort'),toggle=make('button',null,'chart-select-toggle'),menu=make('div',null,'chart-select-menu'),options=[];
    toggle.type='button';toggle.setAttribute('aria-haspopup','listbox');toggle.setAttribute('aria-expanded','false');menu.hidden=true;menu.setAttribute('role','listbox');menu.setAttribute('aria-label','Sort table');group.append(toggle,menu);controls.append(group);
    const close=()=>{menu.hidden=true;toggle.setAttribute('aria-expanded','false');group.classList.remove('open');};
    function sync(){const column=columns.find(c=>c.key===state.key);toggle.textContent='Sort: '+column.label+' '+(state.direction==='asc'?'↑':'↓');toggle.setAttribute('aria-label',toggle.textContent);for(const option of options)option.setAttribute('aria-selected',String(option.dataset.key===state.key));for(const [i,c]of columns.entries())table.tHead.rows[0].cells[i].setAttribute('aria-sort',state.key===c.key?(state.direction==='asc'?'ascending':'descending'):'none');}
    function set(key,direction){state.key=key;state.direction=direction;sync();close();onChange?.();}
    for(const [index,column]of columns.entries()){
      const th=table.tHead.rows[0].cells[index],button=make('button',column.label,'sort-heading');button.type='button';th.replaceChildren(button);button.onclick=()=>set(column.key,state.key===column.key&&state.direction==='desc'?'asc':state.key===column.key?'desc':column.direction||'desc');
      const option=make('button',column.label);option.type='button';option.dataset.key=column.key;option.setAttribute('role','option');option.tabIndex=-1;option.onclick=()=>{set(column.key,column.direction||'desc');toggle.focus();};menu.append(option);options.push(option);
    }
    const reverse=make('button','Reverse order ↑↓');reverse.type='button';reverse.setAttribute('role','option');reverse.tabIndex=-1;reverse.onclick=()=>{set(state.key,state.direction==='asc'?'desc':'asc');toggle.focus();};menu.append(reverse);options.push(reverse);
    const open=()=>{menu.hidden=false;toggle.setAttribute('aria-expanded','true');group.classList.add('open');positionMenu(menu,toggle);options.find(o=>o.getAttribute('aria-selected')==='true')?.focus();};
    toggle.onclick=()=>menu.hidden?open():close();toggle.onkeydown=event=>{if(['ArrowUp','ArrowDown'].includes(event.key)){event.preventDefault();open();}};
    menu.onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();close();toggle.focus();}else if(event.key==='Tab')close();else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const index=options.indexOf(document.activeElement),next=event.key==='Home'?0:event.key==='End'?options.length-1:(index+(event.key==='ArrowUp'?options.length-1:1))%options.length;options[next].focus();}};
    document.addEventListener('pointerdown',event=>{if(!group.contains(event.target))close();});sync();
    return {state,set,sort(rows){const c=columns.find(c=>c.key===state.key),value=row=>c.value?c.value(row):row[c.key],valid=v=>v!==null&&v!==undefined&&!(typeof v==='number'&&!Number.isFinite(v));return [...rows].sort((a,b)=>{const x=value(a),y=value(b);if(!valid(x)||!valid(y))return Number(valid(y))-Number(valid(x));const order=typeof x==='number'&&typeof y==='number'?x-y:collator.compare(String(x),String(y));return (state.direction==='asc'?1:-1)*order;});}};
  };
  const responses=new Map(),requests=new Map(),storage=caches.open('salad-responses').catch(()=>null);
  const responseKey=(service,hours)=>service.id+':'+hours;
  const responseURL=(service,hours)=>service.url+(service.url.includes('?')?'&':'?')+'compact=1&history=1&hours='+hours;
  async function read(url,signal,key){
    const deadline=AbortSignal.timeout(30000);
    try{
      const response=await fetch(url,{cache:'no-store',signal:signal?AbortSignal.any([signal,deadline]):deadline});
      if(!response.ok){const error=await response.json().catch(()=>({}));throw new Error(typeof error.error==='string'?error.error:'HTTP '+response.status);}
      const copy=key?response.clone():null,result=await response.json();
      if(key){
        const at=Date.now(),headers=new Headers(copy.headers);headers.set('X-Salad-Read-Ms',String(at));responses.set(key,{data:result,at});
        storage.then(cache=>cache?.put(url,new Response(copy.body,{status:copy.status,headers}))).catch(()=>{});
      }
      return result;
    }catch(error){if(deadline.aborted&&!signal?.aborted)throw new Error('The server response timed out after 30 seconds');throw error;}
  }
  async function restore(service,hours){
    const key=responseKey(service,hours);
    if(!responses.has(key))try{
      const cache=await storage,url=responseURL(service,hours);
      const response=await cache?.match(url)||await cache?.match(url.replace("&history=1", ""));
      if(response){const data=await response.json(),at=Number(response.headers.get('X-Salad-Read-Ms'));if(!responses.has(key))responses.set(key,{data,at});}
    }catch(_){}
    return responses.get(key)?.data;
  }
  let revisionRequest=null,revisionResult=null,revisionAt=0;
  function revisions(){
    // Overview shares this one request across its nine services on each tick.
    if(revisionRequest)return revisionRequest;
    if(revisionResult&&Date.now()-revisionAt<500)return Promise.resolve(revisionResult);
    revisionRequest=read('/api/revisions').then(value=>{
      if(!value?.revisions||typeof value.revisions!=='object')throw new Error('Source revisions unavailable');
      revisionResult=value.revisions;revisionAt=Date.now();return revisionResult;
    }).finally(()=>{revisionRequest=null;});
    return revisionRequest;
  }
  function load(service,hours,force=true){
    const key=responseKey(service,hours);
    if(!requests.has(key))requests.set(key,(async()=>{
      const entry=responses.get(key);
      let revision;
      try{revision=(await revisions())[service.id];}
      catch(error){if(!force&&entry&&Date.now()-entry.at<1000)throw error;}
      if(!force&&entry&&typeof revision==='string'&&entry.revision===revision)return entry.data;
      const result=await read(responseURL(service,hours),null,key);
      // Do not claim a revision newer than the observation we requested.
      responses.get(key).revision=revision;
      return result;
    })().finally(()=>requests.delete(key)));
    return requests.get(key);
  }
  window.bandwidthData={read,restore,load,get:(service,hours)=>responses.get(responseKey(service,hours))?.data,
    updated:(service,hours)=>responses.get(responseKey(service,hours))?.at,
    age:(at,now=Date.now())=>Number.isFinite(at)&&at<=now?Math.floor((now-at)/1000)+'s':'—',
    providerFresh:(service,p,now=Date.now())=>p?.fresh===true&&Number.isFinite(p.observed_ms)&&now>=p.observed_ms&&now-p.observed_ms<=(service==='hg'?360000:service==='proxyrack'?900000:service==='earnapp'?150000:180000)};
  const clockFormat = new Intl.DateTimeFormat(undefined, {hour: "2-digit", minute: "2-digit", hour12: false});
  const clock = value => value == null ? "—" : clockFormat.format(value);
  const make = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const countryCache = new Map();
  const countryAttempts = new Map();
  const countryCode = node => /^[A-Z]{2}$/i.test(node.country || "") ? node.country.toUpperCase() : countryCache.get(node.ip);
  const needsCountry = node => node.ip && !countryCode(node) && (!countryAttempts.has(node.ip) || Date.now() - countryAttempts.get(node.ip) >= 60000);
  const countryNames=new Intl.DisplayNames(['en'],{type:'region'});
  const countryName=node=>node.service==='grass'&&countryCode(node)?countryNames.of(countryCode(node)):countryCode(node)||'—';
  let countryLookup = null;
  async function lookupCountries(nodes) {
    if (!nodes.some(needsCountry)) return;
    if (countryLookup) await countryLookup;
    const ips=[...new Set(nodes.filter(needsCountry).map(n=>n.ip))];
    if (!ips.length) return;
    for (const ip of ips) countryAttempts.set(ip, Date.now());
    countryLookup=(async()=>{
      try {
        const response=await fetch("https://api.country.is/",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(ips),signal:AbortSignal.timeout(5000)});
        if (!response.ok) return;
        for (const row of await response.json()) if(ips.includes(row.ip) && /^[A-Z]{2}$/.test(row.country)) countryCache.set(row.ip,row.country);
      } catch (_) {}
    })();
    await countryLookup; countryLookup=null;
  }
  window.bandwidthCountries = {name: countryName, lookup: lookupCountries};
  const nodeColor=()=> 'var(--blue)';
  function nodeLabel(node,includeCountry=true) {
    const label=make('span',null,'node-label'),square=make('i',null,'node-square');square.style.backgroundColor=nodeColor(node);square.setAttribute('aria-hidden','true');
    label.append(square);if(includeCountry)label.append(make('span',countryName(node),'node-country'));label.append(make('span',node.ip||'—','node-ip'));label.title=node.ip||'';return label;
  }
  function clientStatus(node){if(!node)return '—';if(node.service==='grass'){if(node.stale)return 'Source delayed';if(node.pending)return 'Awaiting machine';if(node.displayStatus)return node.displayStatus==='Connected'?(node.working===true?'Online':'Provider status unavailable'):node.displayStatus;}if(node.stale||node.pending||node.unknown)return 'Unknown';if(node.working===true)return 'Online';if(/error|failed|rejected|exited|stopped|disconnect|limit|unusable|overused|offline/i.test(node.status||'')||['stopped','failed','succeeded'].includes(node.salad))return 'Offline';return 'Unknown';}
  const stateLabel=node=>!node?'—':node.stale||!node.salad?'Unknown':node.salad[0].toUpperCase()+node.salad.slice(1);
  window.bandwidthNodes = {color:nodeColor,label:nodeLabel,status:clientStatus,state:stateLabel};
  const MINUTE=60000, chartIntervals=[1,3,5,10,15,30,60,180,360,720,1440];
  const shortTime=n=>n>=1440?n/1440+'D':n>=60?n/60+'H':n+'M';
  const averageTime=n=>n==='day'?'DAY':n===1440?'24H':shortTime(n);
  window.chartMinutes=(history,points)=>{
    const rows=new Map((history||[]).map(p=>[p.minute_ms??p.time_ms,p]));
    for(const p of points||[]){const at=p.minute_ms??p.time_ms;if(finite(p.mb)||!rows.has(at))rows.set(at,p);}
    return [...rows.values()].sort((a,b)=>(a.minute_ms??a.time_ms)-(b.minute_ms??b.time_ms));
  };
  window.createTimeChart=({plot,scroll,popup,unit,controls,onRangeChange,hours=24,range=true,view='candles'})=>{
    if(plot.tagName.toLowerCase()!=='canvas'){const canvas=make('canvas');for(const a of plot.attributes)canvas.setAttribute(a.name,a.value);plot.replaceWith(canvas);plot=canvas;}
    plot.classList.add('time-chart');let data=null,bars=[],daily=[],geometry=null,hover=null;
    const state={hours,interval:'auto',view,scale:'linear',average:60},inputs={};
    const options={hours:[[1,'1H'],[6,'6H'],[12,'12H'],[24,'24H'],[72,'3D'],[120,'5D'],[168,'7D'],[720,'30D']],interval:[['auto','AUTO 1M'],...chartIntervals.map(n=>[n,shortTime(n)])],view:[['candles','Bars'],['line','Curve'],['step','Step']],scale:[['linear','Linear'],['log','Log']],average:[[0,'AVG —'],...[60,360,720,1440,4320,7200,10080].map(n=>[n,'AVG '+averageTime(n)]),['day','AVG DAY']]};
    controls.classList.add('chart-options');
    function sync(field){const input=inputs[field];if(!input)return;const option=input.options.find(o=>o.value===String(state[field]));input.button.textContent=option?.textContent||'';input.button.setAttribute('aria-label',input.button.title+': '+input.button.textContent);input.button.value=state[field];for(const row of input.options)row.setAttribute('aria-selected',String(row===option));}
    for(const [field,choices]of Object.entries(options)){
      if(field==='hours'&&!range)continue;const group=make('div',null,'chart-select'),button=make('button',null,'chart-select-toggle'),menu=make('div',null,'chart-select-menu'),label={hours:'Time range',interval:'Candle interval',view:'Chart view',scale:'Vertical scale',average:'Average line'}[field];
      button.type='button';button.setAttribute('aria-label',label);button.title=label;button.dataset.chartControl=field;button.setAttribute('aria-haspopup','listbox');button.setAttribute('aria-expanded','false');menu.id=plot.id+'-'+field+'-menu';button.setAttribute('aria-controls',menu.id);menu.setAttribute('role','listbox');menu.setAttribute('aria-label',label);menu.hidden=true;
      const rows=[],close=()=>{menu.hidden=true;button.setAttribute('aria-expanded','false');group.classList.remove('open');};
      for(const [value,text]of choices){const option=make('button',text);option.type='button';option.value=value;option.setAttribute('role','option');option.tabIndex=-1;rows.push(option);menu.append(option);option.onclick=()=>{state[field]=['hours','average'].includes(field)&&option.value!=='day'?Number(option.value):option.value;sync(field);close();button.focus();hide();if(field==='hours'&&onRangeChange)onRangeChange(state.hours);else rebuild();};}
      inputs[field]={button,menu,options:rows};group.append(button,menu);controls.append(group);sync(field);
      const open=()=>{menu.hidden=false;button.setAttribute('aria-expanded','true');group.classList.add('open');positionMenu(menu,button);(rows.find(o=>o.value===String(state[field])&&!o.disabled)||rows.find(o=>!o.disabled))?.focus();};
      button.onclick=()=>menu.hidden?open():close();button.onkeydown=event=>{if(['ArrowDown','ArrowUp'].includes(event.key)){event.preventDefault();open();}};
      menu.onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();close();button.focus();}else if(event.key==='Tab')close();else if(['ArrowDown','ArrowUp','Home','End'].includes(event.key)){event.preventDefault();const enabled=rows.filter(o=>!o.disabled),index=enabled.indexOf(document.activeElement),next=event.key==='Home'?0:event.key==='End'?enabled.length-1:(index+(event.key==='ArrowUp'?enabled.length-1:1))%enabled.length;enabled[next]?.focus();}};
      document.addEventListener('pointerdown',event=>{if(!group.contains(event.target))close();});
    }
    inputs.interval.options[0].textContent='AUTO '+shortTime(chartIntervals.find(n=>Math.ceil(hours*60/n)<=1440));sync('interval');
    const legend=make('span',null,'chart-average-legend');legend.append(make('i',null,'legend-key average'),make('span','AVG'));legend.title='Trailing average; gaps are not zero';controls.before(legend);
    const band=make('div',null,'chart-hover-band');band.hidden=true;scroll.parentElement.append(band);
    function hide(){hover=null;band.hidden=true;popup.classList.add('hidden');}
    function rebuild(){
      if(!data)return;const previous=hover,rangeMinutes=state.hours*60,size=state.interval==='auto'?chartIntervals.find(n=>Math.ceil(rangeMinutes/n)<=1440):Number(state.interval);
      inputs.interval.options[0].textContent='AUTO '+shortTime(chartIntervals.find(n=>Math.ceil(rangeMinutes/n)<=1440));sync('interval');
      for(const option of inputs.interval.options)option.disabled=option.value!=='auto'&&Number(option.value)>rangeMinutes;
      if(size>rangeMinutes){state.interval='auto';sync('interval');return rebuild();}
      const end=data.end??(Math.floor(Date.now()/MINUTE)+1)*MINUTE,start=end-rangeMinutes*MINUTE,first=state.average==='day'?new Date(start).setHours(0,0,0,0):start-state.average*MINUTE,n=Math.round((end-first)/MINUTE),count=data.series.length;
      const known=new Uint32Array(n+1),partial=new Uint32Array(n+1),sums=new Float64Array(n+1),points=new Map((data.points||[]).map(p=>[p.at,p]));
      for(let i=0;i<n;i++){const at=first+i*MINUTE,p=points.get(at),value=data.averageTotal?p?.values.reduce((s,v)=>finite(v)?(s??0)+v:s,null):p?.values[0],valid=finite(value)&&at+MINUTE<=Math.min(Date.now(),data.now??Date.now());sums[i+1]=sums[i]+(valid?value:0);known[i+1]=known[i]+Number(valid);partial[i+1]=partial[i]+Number(Boolean(p?.partial||p?.estimated));}
      const sourceStart=(data.points||[]).reduce((start,p)=>Math.min(start,(data.averageTotal?p.values.some(finite):finite(p.values[0]))?p.at:Infinity),Infinity);
      const mean=(from,to)=>{const left=Math.round((from-first)/MINUTE),right=Math.round((to-first)/MINUTE),expected=right-left,lo=Math.max(0,Math.min(n,left)),hi=Math.max(0,Math.min(n,right)),covered=known[hi]-known[lo],elapsed=to<=Math.min(Date.now(),data.now??Date.now())&&from>=sourceStart;return expected>0&&elapsed&&covered>0?{value:(sums[hi]-sums[lo])/covered,partial:covered<expected||partial[hi]-partial[lo]>0,covered,expected}:{value:null,partial:false,covered,expected};};
      daily=[];if(state.average==='day'){const date=new Date(start);date.setHours(0,0,0,0);while(date.getTime()<end){const at=date.getTime();date.setDate(date.getDate()+1);daily.push({at,end:date.getTime(),...mean(at,date.getTime())});}}
      bars=[];
      for(let at=start;at<end;at+=size*MINUTE){const stop=Math.min(end,at+size*MINUTE),duration=(stop-at)/MINUTE,values=Array(count).fill(null),amounts=Array(count).fill(null),seen=Array(count).fill(0);let incomplete=false,estimated=false,nodes=null;
        for(let t=at;t<stop;t+=MINUTE){const p=points.get(t);if(!p){incomplete=true;continue;}incomplete||=Boolean(p.partial);estimated||=Boolean(p.estimated);if(finite(p.count))nodes=Math.max(nodes??0,p.count);
          p.values.forEach((v,j)=>{if(finite(v)){amounts[j]=(amounts[j]??0)+v;values[j]=data.mode==='level'?v:amounts[j];seen[j]++;}});
        }
        for(let j=0;j<count;j++){if(data.mode==='level'&&seen[j]!==duration)values[j]=null;if(data.mode==='rate'&&!data.series[j].marker&&finite(values[j]))values[j]=data.series[j].event||seen[j]===duration?values[j]/duration:null;if(j===0&&seen[j]<duration)incomplete=true;}
        const averaged=state.average==='day'?daily.find(d=>at>=d.at&&at<d.end):state.average?mean(stop-state.average*MINUTE,stop):null,average=finite(averaged?.value)&&(data.averageTotal?values.some(finite):finite(values[0]))?averaged.value*(data.mode==='amount'?duration:1):null;
        bars.push({at,end:stop,values,amounts,partial:incomplete,estimated,count:nodes,average,averagePartial:averaged?.partial||false,averageCoverage:averaged});
      }
      legend.hidden=!state.average;legend.lastChild.textContent=state.average&&(state.average==='day'?daily.some(d=>finite(d.value)):bars.some(b=>finite(b.average)))?'AVG':'AVG —';legend.title=(state.average==='day'?'Calendar-day':'Trailing '+averageTime(state.average))+' average of '+(data.averageTotal?'total '+data.unit:(data.series[0]?.name||''))+'. The full window must have elapsed. Uses recorded minutes only; ≈ marks missing minutes or partial fleet coverage. Popup shows recorded/expected minutes.';
      plot.dataset.count=bars.length;plot.dataset.interval=size;plot.dataset.average=state.average;draw();
      if(previous!==null){const index=bars.findIndex(b=>b.at===previous);if(index>=0)inspect(index,null,true);else hide();}
    }
    function draw(){
      if(!data||!bars.length)return;const twoLines=state.hours>24&&state.hours<=168;
      const width=scroll.clientWidth,height=width<600?260:380,right=width-16,top=16,dpr=devicePixelRatio||1;let left=44,bottom=height-44,step;
      const dailyFactor=data.mode==='amount'?Number(plot.dataset.interval):1,maximum=bars.reduce((m,b)=>Math.max(m,b.average??0,data.stacked&&state.view==='candles'?b.values.reduce((s,v,j)=>s+(data.series[j].marker?0:finite(v)?v:0),0):Math.max(0,...b.values.map((v,j)=>data.series[j].marker?0:v??0))),Math.max(0,...daily.map(d=>(d.value??0)*dailyFactor)));
      const factor=data.unit.startsWith('MB')&&maximum>0&&maximum<.001?1e6:data.unit.startsWith('MB')&&maximum>0&&maximum<1?1e3:1,topValue=maximum>0?maximum*factor:1,raw=topValue*1.04/5,power=10**Math.floor(Math.log10(raw)),tick=[1,2,2.5,5,10].map(v=>v*power).find(v=>v>=raw),ceiling=Math.ceil(topValue*1.04/tick)*tick/factor;
      const minimum=bars.reduce((m,b)=>Math.min(m,...[b.average,...b.values.filter((v,j)=>!data.series[j].marker)].filter(v=>finite(v)&&v>0)),Infinity),logFloor=Number.isFinite(minimum)?10**(Math.floor(Math.log10(minimum))-1):.1;const logarithmic=state.scale==='log',y=v=>bottom-(logarithmic?(v>0?Math.log10(v/logFloor)/Math.log10(ceiling/logFloor):0):v/ceiling)*(bottom-top),css=getComputedStyle(plot),color=value=>value.startsWith('--')?css.getPropertyValue(value).trim():value;
      plot.width=Math.round(width*dpr);plot.height=Math.round(height*dpr);plot.style.height=height+'px';const ctx=plot.getContext('2d');ctx.scale(dpr,dpr);ctx.font='11px JBMono, monospace';ctx.lineWidth=1;
      unit.textContent=data.unit.replace('MB',factor===1e6?'B':factor===1e3?'kB':'MB');const ticks=[];if(logarithmic){ticks.push(0);for(let v=logFloor*10;v<ceiling;v*=10)ticks.push(v);ticks.push(ceiling);}else for(let v=0;v<=ceiling*factor+tick*.01;v+=tick)ticks.push(v/factor);
      const tickLabel=value=>number(value*factor,Math.max(1,Math.min(8,-Math.floor(Math.log10(logarithmic&&value>0?value*factor:tick))+1)));
      const inset=Math.max(44,Math.ceil(Math.max(...ticks.map(value=>ctx.measureText(tickLabel(value)).width)))+12);left=inset;bottom=height-inset;step=(right-left)/bars.length;geometry={left,right,top,bottom,step};
      ctx.fillStyle=color('--faint');ctx.strokeStyle=color('--plot-grid');ctx.textAlign='right';ctx.setLineDash([2,4]);
      for(const value of ticks){ctx.beginPath();ctx.moveTo(left,y(value));ctx.lineTo(right,y(value));ctx.stroke();ctx.fillText(tickLabel(value),left-8,y(value)+4);}
      const labels=Math.max(2,Math.min(9,Math.floor((right-left)/110))),first=bars[0].at,last=bars.at(-1).end,day=at=>new Date(at).toLocaleDateString([],{month:'short',day:'numeric'});
      for(let i=0;i<=labels;i++){const at=first+(last-first)*i/labels,x=left+(right-left)*i/labels;ctx.textAlign=i===0?'left':i===labels?'right':'center';ctx.fillText(state.hours<=24?clock(at):day(at),x,bottom+14);if(twoLines)ctx.fillText(clock(at),x,bottom+28);}
      ctx.setLineDash([]);ctx.save();ctx.beginPath();ctx.rect(left,top,right-left,bottom-top);ctx.clip();
      const line=(values,style,stroke,width=1)=>{ctx.strokeStyle=ctx.fillStyle=stroke;ctx.lineWidth=width;ctx.beginPath();const dots=[];for(let i=0;i<values.length;i++){const value=values[i];if(!finite(value))continue;const x=left+(i+.5)*step,cy=y(value);if(style==='step'){if(i&&finite(values[i-1]))ctx.lineTo(left+i*step,cy);else ctx.moveTo(left+i*step,cy);ctx.lineTo(left+(i+1)*step,cy);}else{if(i&&finite(values[i-1]))ctx.lineTo(x,cy);else ctx.moveTo(x,cy);if(!finite(values[i-1])&&!finite(values[i+1]))dots.push([x,cy]);}}ctx.stroke();if(dots.length){ctx.beginPath();for(const [x,cy]of dots){ctx.moveTo(x+2.5,cy);ctx.arc(x,cy,2.5,0,Math.PI*2);}ctx.fill();}};
      if(state.view==='candles')for(let i=0;i<bars.length;i++){const b=bars[i],order=data.series.map((_,j)=>j).filter(j=>!data.series[j].marker&&finite(b.values[j]));if(!data.stacked)order.sort((a,z)=>b.values[z]-b.values[a]);let base=0;for(const j of order){const value=b.values[j],w=Math.max(1/dpr,step*.86);ctx.fillStyle=color(data.series[j].color);ctx.globalAlpha=b.partial ? 0.85 : 1;ctx.fillRect(left+i*step+(step-w)/2,y(base+value),w,Math.max(.5,y(base)-y(base+value)));if(data.stacked)base+=value;}ctx.globalAlpha=1;}
      else data.series.forEach((series,j)=>{if(!series.marker)line(bars.map(b=>b.values[j]),state.view,color(series.color));});
      data.series.forEach((series,j)=>{if(!series.marker)return;ctx.fillStyle=color(series.color);for(let i=0;i<bars.length;i++)if(bars[i].amounts[j]>0){const x=left+(i+.5)*step;ctx.beginPath();ctx.moveTo(x,bottom-8);ctx.lineTo(x+3,bottom-1);ctx.lineTo(x-3,bottom-1);ctx.fill();}});
      if(state.average==='day'){ctx.strokeStyle=color('--avg');ctx.lineWidth=1.5;ctx.beginPath();for(const day of daily)if(finite(day.value)){const x=at=>left+(at-first)/(last-first)*(right-left),cy=y(day.value*dailyFactor);for(const bar of bars)if((data.averageTotal?bar.values.some(finite):finite(bar.values[0]))&&bar.end>day.at&&bar.at<day.end){ctx.moveTo(x(Math.max(bar.at,day.at)),cy);ctx.lineTo(x(Math.min(bar.end,day.end)),cy);}}ctx.stroke();}else if(state.average)line(bars.map(b=>b.average),'line',color('--avg'),1.5);
      ctx.restore();ctx.strokeStyle=color('--axis-line');ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(left,top);ctx.lineTo(left,bottom);ctx.lineTo(right,bottom);ctx.stroke();
      const available=bars.some(b=>b.values.some(finite));if(!available){ctx.fillStyle=color('--muted');ctx.textAlign='center';ctx.fillText(data.loading?'Loading…':'No measurements',(left+right)/2,(top+bottom)/2);}
      plot.setAttribute('aria-label',shortTime(state.hours*60)+', '+bars.length+' '+shortTime(Number(plot.dataset.interval))+' '+state.view+'. '+state.scale+' scale. '+(state.average?'AVG '+averageTime(state.average)+'. ':'')+'Unknown minutes remain gaps.');
      if(hover!==null){const i=bars.findIndex(b=>b.at===hover);if(i>=0)positionBand(i);}
    }
    function positionBand(index){band.style.left=geometry.left+index*geometry.step+'px';band.style.top=geometry.top+'px';band.style.height=geometry.bottom-geometry.top+'px';band.style.width=Math.max(1,geometry.step)+'px';band.hidden=false;}
    function inspect(index,event,preserve=false){
      const b=bars[index];if(!b||!geometry)return;const changed=hover!==b.at;hover=b.at;positionBand(index);const wrap=scroll.parentElement.getBoundingClientRect();
      if(!preserve){const width=Math.min(parseFloat(getComputedStyle(popup).width),wrap.width-8),x=event?event.clientX-wrap.left:geometry.left+(index+.5)*geometry.step;popup.style.left=Math.max(4,Math.min(x+width+16<=wrap.width?x+16:x-width-16,wrap.width-width-4))+'px';popup.style.top=Math.max(geometry.top+8,12-wrap.top)+'px';}
      if(changed||preserve){const heading=make('div',null,'popup-heading');heading.append(make('span',new Date(b.at).toLocaleDateString('en-CA')),make('span',clock(b.at)+'–'+(new Date(b.at).toDateString()!==new Date(b.end).toDateString()?new Date(b.end).toLocaleDateString([],{month:'short',day:'numeric'})+' ':'')+clock(b.end)));popup.replaceChildren(heading);
        const rows=data.tip?data.tip(b):data.series.map((series,i)=>({label:series.name,value:(b.partial?'≈ ':'')+number(b.values[i],data.unit==='USD'?6:1)+' '+data.unit,color:series.color}));
        if(state.average)rows.push({label:'AVG '+averageTime(state.average),meta:b.averageCoverage?number(b.averageCoverage.covered,0)+'/'+number(b.averageCoverage.expected,0)+' min':'',value:(b.averagePartial&&finite(b.average)?'≈ ':'')+number(b.average,data.unit==='USD'?6:1)+' '+data.unit,color:'--avg',title:legend.title});
        for(const entry of rows){const row=make('div',null,'popup-row'+(entry.color==='--blue'?' network':entry.color==='--earned'?' credited':'')+(entry.total?' popup-total':'')),label=make('span',null,'popup-label'),square=make('i',null,'popup-square');square.style.background=entry.color.startsWith('--')?'var('+entry.color+')':entry.color;label.append(square,make('span',entry.label));row.append(label,make('span',entry.meta||'','popup-meta'),make('span',entry.value,'popup-value'));if(entry.title)row.title=entry.title;popup.append(row);}}
      popup.classList.remove('hidden');popup.style.top=Math.max(8-wrap.top,Math.min(parseFloat(popup.style.top)||8,innerHeight-wrap.top-popup.offsetHeight-8))+'px';popup.style.left=Math.max(4,Math.min(parseFloat(popup.style.left)||4,wrap.width-popup.offsetWidth-4))+'px';
    }
    plot.onpointermove=event=>{if(!geometry)return;const x=event.clientX-plot.getBoundingClientRect().left;if(x<geometry.left||x>=geometry.right)return hide();inspect(Math.floor((x-geometry.left)/geometry.step),event);};plot.onpointerleave=hide;plot.onblur=hide;
    plot.onkeydown=event=>{if(!bars.length||!['ArrowLeft','ArrowRight','Home','End','Escape'].includes(event.key))return;event.preventDefault();if(event.key==='Escape')return hide();const current=hover===null?0:bars.findIndex(b=>b.at===hover);inspect(event.key==='Home'?0:event.key==='End'?bars.length-1:Math.max(0,Math.min(bars.length-1,current+(event.key==='ArrowLeft'?-1:1))));};
    new ResizeObserver(draw).observe(scroll);new MutationObserver(draw).observe(document.documentElement,{attributes:true,attributeFilter:['data-bs-theme']});document.fonts.ready.then(draw);
    return {hide,state,setHours(value){if(state.hours!==value){state.hours=value;sync('hours');hide();}},update(value){data=value;rebuild();}};
  };
  window.createMinuteChart=({plot,breakdown,pageCount,previous,next,scroll,popup,active,unit,controls,onRangeChange,sortControls,onIPSelect,metadata,selectedIP})=>{
    let data=null,series=[],page=0,postingMinutes=new Map(),breakdownRender=0;
    const engine=window.createTimeChart({plot,scroll,popup,unit,controls,onRangeChange});
    const currentNode=item=>data?.nodes?.find(n=>!n.pending&&(item.ip?n.ip===item.ip:item.instance&&n.id===item.instance));
    const ipMeta=item=>metadata?.()?.ips?.find(row=>row.ip===item.ip);
    const sorter=sortControls?window.createTableSort({table:breakdown.closest('table'),controls:sortControls,defaultKey:'total',onChange:()=>{page=0;buildBreakdown();},columns:[
      {key:'ip',label:'IP',direction:'asc'},{key:'country',label:'Country',direction:'asc',value:item=>countryName(item)==='—'?null:countryName(item)},
      {key:'client',label:'Client',direction:'asc',value:item=>clientStatus(currentNode(item))},{key:'state',label:'State',direction:'asc',value:item=>stateLabel(currentNode(item))},
      {key:'rate',label:'Avg MB/min'},{key:'total',label:'Period MB'},{key:'minutes',label:'Recorded min'},
      {key:'first',label:'First seen · IST',value:item=>ipMeta(item)?.first_seen_ms},{key:'last',label:'Last seen · IST',value:item=>ipMeta(item)?.last_seen_ms},{key:'uses',label:'Runs',value:item=>ipMeta(item)?.uses}]}):null;
    function buildBreakdown(){
      const renderID=++breakdownRender,grouped=new Map(),bars=data.breakdownCandles||data.candles;
      if(!data.ipRows)bars.forEach((bar,i)=>{for(const node of bar.nodes||[]){if(!finite(node.mb)||node.mb<0)continue;const key=node.ip||node.instance||'Unknown',item=grouped.get(key)||{...node,total:0,peak:0,minutes:0,lastMinute:-1,currentMinute:0};item.total+=node.mb;if(item.lastMinute!==i){item.lastMinute=i;item.currentMinute=0;item.minutes++;}item.currentMinute+=node.mb;item.peak=Math.max(item.peak,item.currentMinute);grouped.set(key,item);}});
      if(data.ipRows){grouped.clear();for(const record of data.ipRows){if(!finite(record.total_mb))continue;const key=record.ip||record.instance||record.id,item=grouped.get(key)||{ip:record.ip,country:record.country,instance:record.instance,total:0,minutes:0,partial:false};item.total+=record.total_mb;item.minutes+=(record.duration_ms||0)/60000;item.partial||=record.partial||record.estimated;grouped.set(key,item);}}
      for(const item of grouped.values())item.rate=item.minutes>0?item.total/item.minutes:null;
      series=sorter?sorter.sort([...grouped.values()]):[...grouped.values()].sort((a,b)=>b.total-a.total);
      page=Math.min(page,Math.max(0,Math.ceil(series.length/25)-1));const shown=series.slice(page*25,(page+1)*25);
      pageCount.textContent=series.length?(page*25+1)+'–'+Math.min((page+1)*25,series.length)+' of '+series.length+(series.some(n=>!n.ip)?' contributor records':' IPs'):'No recorded contributors';previous.disabled=page===0;next.disabled=(page+1)*25>=series.length;
      breakdown.replaceChildren();
      for(const item of shown){
        const current=currentNode(item),row=make('tr'),identity=make('td'),client=make('td'),status=clientStatus(current);if(item.ip&&onIPSelect){const link=make('button',null,'record-link');link.append(nodeLabel(item,false));link.onclick=()=>onIPSelect(item.ip);link.setAttribute('aria-label','Show traffic for '+item.ip);identity.append(link);}else identity.append(make('span','IP unavailable · '+String(item.instance||'unknown node').slice(0,12)));
        client.append(make('span',status,'status-pill '+(status==='Online'?'online':'')));if(status==='Unknown'||status==='Offline'){client.classList.add('has-issue');client.append(window.issueIcon(current?[current.ip,current.status,current.error,current.note].filter(Boolean).join(' · '):'No current client observation for this recorded IP.',status==='Offline'?'error':'warning'));}
        row.append(identity,make('td',countryName(item)),client,make('td',stateLabel(current)),make('td',number(item.rate),'numeric'),make('td',(item.partial?'≈ ':'')+number(item.total),'numeric'),make('td',number(item.minutes,0)),make('td',window.saladTime.format(ipMeta(item)?.first_seen_ms),'time-cell'),make('td',window.saladTime.format(ipMeta(item)?.last_seen_ms),'time-cell'),make('td',ipMeta(item)?.uses??'Unknown','numeric'));if(item.ip&&onIPSelect)window.bindIPRow(row,item.ip,selectedIP?.()===item.ip,()=>onIPSelect(item.ip));breakdown.append(row);
      }
      const rendered=data,renderedPage=page;lookupCountries(shown).then(()=>{if(data!==rendered||page!==renderedPage||renderID!==breakdownRender)return;for(const [i,item]of shown.entries())if(breakdown.rows[i])breakdown.rows[i].cells[1].textContent=countryName(item);});
      if(!series.length){const row=make('tr'),cell=make('td',data.loading?'Loading…':'IP contributions unavailable','empty-cell');cell.colSpan=breakdown.closest('table').tHead.rows[0].cells.length;row.append(cell);breakdown.append(row);}
    }

    previous.onclick=()=>{if(page>0){page--;buildBreakdown();}};next.onclick=()=>{if((page+1)*25<series.length){page++;buildBreakdown();}};
    return {hide:engine.hide,setHours:engine.setHours,count:()=>data?.loading&&!data?.breakdownCandles?null:series.length,update(next){
      const samePlot=next.plotRevision!==undefined&&data?.plotRevision===next.plotRevision;
      if((data?.scopeKey??data?.key)!==(next.scopeKey??next.key))page=0;data=next;buildBreakdown();if(samePlot)return;postingMinutes=new Map();
      for(const p of data.postings||[]){const at=Math.floor(p.end_ms/MINUTE)*MINUTE,posted=postingMinutes.get(at)||{credited_mb:null,earned_usd:null,start_ms:p.start_ms,end_ms:p.end_ms};posted.start_ms=Math.min(posted.start_ms,p.start_ms);posted.end_ms=Math.max(posted.end_ms,p.end_ms);for(const key of ['credited_mb','earned_usd'])if(finite(p[key]))posted[key]=(posted[key]??0)+p[key];postingMinutes.set(at,posted);}
      const points=new Map(window.chartMinutes(data.history,data.candles).filter(p=>finite(p.mb)).map(p=>[p.minute_ms??p.time_ms,{at:p.minute_ms??p.time_ms,values:[p.mb,null],partial:p.partial,estimated:p.estimated,count:p.count}]));
      for(const [at,p]of postingMinutes){const point=points.get(at)||{at,values:[null,null],partial:true};point.values[1]=p.credited_mb;points.set(at,point);}
      engine.update({points:[...points.values()],end:data.candles.at(-1)?.minute_ms+MINUTE,now:data.time_ms,mode:'rate',unit:'MB/min',loading:data.loading,series:data.ipOnly?[{name:'Network',color:'--blue'}]:[{name:'Network',color:'--blue'},{name:data.creditMarkers?'Credit update':'Credited',color:'--earned',marker:data.creditMarkers,event:true}],tip:bar=>{
        if(data.ipOnly)return [{label:'Network',meta:finite(bar.count)?number(bar.count,0)+' nodes':'',value:(bar.partial||bar.estimated?'≈ ':'')+number(bar.values[0])+' MB/min',color:'--blue'}];
        const posted={credited_mb:null,earned_usd:null,start_ms:Infinity,end_ms:0};for(let at=bar.at;at<bar.end;at+=MINUTE){const p=postingMinutes.get(at);if(!p)continue;for(const key of ['credited_mb','earned_usd'])if(finite(p[key]))posted[key]=(posted[key]??0)+p[key];posted.start_ms=Math.min(posted.start_ms,p.start_ms);posted.end_ms=Math.max(posted.end_ms,p.end_ms);}
        return [{label:'Network',meta:finite(bar.count)?(bar.end-bar.at>MINUTE?'max ':'')+number(bar.count,0)+' nodes':'',value:(bar.partial||bar.estimated?'≈ ':'')+number(bar.values[0])+' MB/min',color:'--blue'},
          {label:data.creditMarkers?'Credit update':'Credited',meta:finite(posted.earned_usd)?'$'+number(posted.earned_usd,6):'—',value:number(data.creditMarkers?posted.credited_mb:bar.values[1])+(data.creditMarkers?' MB':' MB/min'),color:'--earned',title:posted.end_ms?(data.creditMarkers?'Account credit update; not minute traffic. ':'')+'Changes observed between '+new Date(posted.start_ms).toLocaleString()+' and '+new Date(posted.end_ms).toLocaleString()+'. Posting time is unknown.'+(data.usdConversion?' USD equivalent.':''):'No provider change observed'}];
      }});
    }};
  };

})();

"use strict";
// Preserve the existing provider-tab bridge; it is separate from presentation.
(() => {
  const button = document.getElementById("psConnect"), message = document.getElementById("psFeedStatus") || {textContent:""};
  let providerWindow = null, state = null, pending = null;
  let channel = sessionStorage.getItem("ps-billing-channel") || "";
  function refresh() {
    if(pending) return pending;
    pending = (async () => {
      try {
        const response=await fetch("/api/ps1/view",{cache:"no-store",signal:AbortSignal.timeout(8000)});
        if(!response.ok)throw new Error();
        state=await response.json();
        message.textContent=state.feed_age_ms!=null&&state.feed_age_ms<150000?"Provider readings are connected.":"Provider readings are missing or stale. Keep the connected provider tab open.";
      } catch(_) {state=null;message.textContent="Provider connection status is unknown.";}
    })().finally(()=>{pending=null;});
    return pending;
  }
  button?.addEventListener("click",()=>{
    if(providerWindow&&!providerWindow.closed){providerWindow.focus();return;}
    if(!channel){channel=crypto.randomUUID();sessionStorage.setItem("ps-billing-channel",channel);}
    providerWindow=window.open(`https://app.packetstream.io/dashboard#ps-sync=${channel}`,"psBillingSync");
    message.textContent="Provider tab opened. Waiting for credited-traffic readings.";refresh();
  });
  window.addEventListener("message",async event=>{
    if(event.origin!=="https://app.packetstream.io"||!channel||event.data?.type!=="ps-credited"||event.data.channel!==channel)return;
    if(providerWindow&&event.source!==providerWindow)return;
    if(event.data.error){message.textContent="Provider reading could not be retrieved.";return;}
    await refresh();if(!state?.bridge_token||!Number.isFinite(event.data.gb))return;
    try{
      const save=()=>fetch("/api/ps1/credited",{method:"POST",headers:{"Content-Type":"application/json","X-PS-Token":state.bridge_token},body:JSON.stringify({gb:event.data.gb,bytes:event.data.bytes,time_ms:event.data.time_ms})});
      let response=await save();
      if(response.status===403){await refresh();if(!state?.bridge_token)throw new Error();response=await save();}
      if(!response.ok)throw new Error();message.textContent="Provider reading saved.";
    }
    catch(_){message.textContent="Provider reading could not be saved.";}
  });
  document.getElementById("history")?.addEventListener("toggle",event=>{if(event.currentTarget.open)refresh();});
})();
