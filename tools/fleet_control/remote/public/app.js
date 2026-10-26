(function(root, factory) {
  'use strict';
  const exported = factory();
  if (typeof module === 'object' && module.exports) module.exports = exported;
  if (typeof document !== 'undefined') exported.start(document);
})(typeof globalThis === 'object' ? globalThis : this, function() {
  'use strict';
  const labels = {fleetHealth:'Fleet service',fleetState:'Live fleet observations',fleetMaster:'Startup and dispatch',quota:'Codex quota',missionHealth:'Mission Control service',missionState:'Mission Control lanes'};
  function freshness(snapshot, now = Date.now()) {
    const age = (now - Date.parse(snapshot?.generatedAt || '')) / 1000;
    const valid = snapshot?.schemaVersion === 1 && snapshot?.readOnly === true && Number.isFinite(age) && age >= -10;
    return { stale: !valid || age > 45, age: Number.isFinite(age) ? Math.max(0,Math.floor(age)) : null };
  }
  function displayModel(snapshot, now = Date.now()) {
    const fresh = freshness(snapshot, now);
    if (fresh.stale) return { ...fresh, dispatch:'Unknown', dispatchDetail:'Status feed is stale; current dispatch cannot be verified',working:'—',agentsDetail:'Current agent activity is unknown',verified:'—',verifiedDetail:'Current runtime health is unknown',quota:'—',quotaDetail:'Current allowance is unknown',startup:'Unknown',startupDetail:'Current startup state is unknown',sessions:[],lanes:[] };
    const s = snapshot, total = s.fleet?.summary?.total;
    // A recently published snapshot can contain an older provider/inspector
    // observation. Expire those observations independently as the page ages.
    const sourceFresh = value => {const age=now-Date.parse(value || '');return Number.isFinite(age)&&age>=-10000&&age<=45000;};
    const readinessFresh = s.readiness?.available && sourceFresh(s.readiness.observedAt);
    const quotaFresh = s.quota?.fresh && sourceFresh(s.quota.sampledAt);
    return { ...fresh,
      dispatch: ({enabled:'Enabled',paused:'Paused',blocked:'Blocked',unknown:'Unknown'})[s.dispatch?.state] || 'Unknown',
      dispatchDetail:s.dispatch?.reason || 'Dispatch status is unavailable',
      working:s.fleet?.runtimeAvailable && Number.isFinite(s.fleet?.summary?.working) ? String(s.fleet.summary.working) : '—',
      agentsDetail:s.fleet?.preservedSnapshot ? 'Saved roster only; live runtime unavailable' : !s.fleet?.runtimeAvailable ? 'Live fleet runtime is unavailable' : Number.isFinite(total) ? total+' agents observed · '+(s.fleet.summary.idle ?? '—')+' idle' : 'No live count available',
      verified:readinessFresh ? s.readiness.verified+' / '+s.readiness.total : '—',
      verifiedDetail:s.readiness?.inspectionPending ? 'Runtime inspection is in progress' : !readinessFresh ? 'Current runtime inspection is unavailable or stale' : s.readiness.runtimesReady===true ? 'Every expected runtime is verified' : (s.readiness.total-s.readiness.verified)+' expected runtimes are not verified',
      quota:quotaFresh && Number.isFinite(s.quota?.remainingPercent) ? s.quota.remainingPercent+'%' : '—',
      quotaDetail:!quotaFresh ? 'Quota telemetry unavailable or stale' : (s.quota.monitoredWindow || 'Selected window')+' remaining · guard '+(s.quota.armed === true ? (s.quota.guardAcknowledged === true ? 'armed and acknowledged' : 'awaiting acknowledgement') : 'not armed'),
      startup:s.startup?.available && s.startup?.phase ? s.startup.phase.replaceAll('_',' ') : 'Unknown',
      startupDetail:s.startup?.reconciliationRequired ? 'Reconciliation required on the PC' : s.startup?.errorCode || (s.startup?.restorePhase ? 'Restore: '+s.startup.restorePhase : 'No current restoration result'),
      sessions:Array.isArray(s.fleet?.sessions) ? s.fleet.sessions : [],
      lanes:s.missionControl?.available && Array.isArray(s.missionControl?.lanes) ? s.missionControl.lanes : [],
    };
  }
  function start(doc) {
    const $ = id => doc.getElementById(id);
    let last = null, failed = false, loading = false;
    const set = (id,value) => {$(id).textContent=value ?? '—';};
    const create = (tag,text,className) => {const el=doc.createElement(tag);if(text !== undefined)el.textContent=text;if(className)el.className=className;return el;};
    const localTime = value => {const date=new Date(value);return Number.isFinite(date.getTime())?date.toLocaleString():'Unknown';};
    function render() {
      const m=displayModel(last), sourceErrors=Object.values(last?.sources || {}).filter(source => !source.available).length;
      set('dispatch',m.dispatch);set('dispatch-detail',m.dispatchDetail);set('working',m.working);set('agents-detail',m.agentsDetail);
      set('verified',m.verified);set('verified-detail',m.verifiedDetail);
      set('quota',m.quota);set('quota-detail',m.quotaDetail);set('startup',m.startup);set('startup-detail',m.startupDetail);
      $('dispatch').className=m.stale?'':m.dispatch==='Enabled'?'good':m.dispatch==='Blocked'?'bad':m.dispatch==='Paused'?'warn':'';
      $('freshness').className='notice '+(m.stale?'bad':failed || sourceErrors?'warn':'good');
      set('freshness',m.stale ? (last?'Status feed is stale. Current fleet activity cannot be verified. Last update: '+localTime(last.generatedAt)+'.':'Status feed is unavailable. The PC or remote monitor may be offline.') : (failed?'Refresh failed; showing the most recent snapshot. ':sourceErrors?sourceErrors+' local status source'+(sourceErrors===1?' is':'s are')+' unavailable. ':'Status feed is current. ')+(m.age ?? 0)+' seconds since publication.');
      set('updated',last?'Snapshot: '+localTime(last.generatedAt):'No snapshot received');
      set('fleet-count',m.stale?'Unknown':m.sessions.length+' agents');
      set('fleet-note',m.stale?'Current activity is unknown while the feed is stale.':last?.fleet?.preservedSnapshot?'Preserved roster · these are not live agents.':'Observed worker and controller states');
      const rows=$('agents');rows.replaceChildren();
      for(const agent of m.sessions){const row=create('tr');const name=create('td',agent.name || agent.id || 'Agent');if(agent.id && agent.id!==agent.name)name.append(create('span',agent.id,'secondary'));row.append(name);row.append(create('td',[agent.harness,agent.model].filter(Boolean).join(' / ') || 'Unknown'));row.append(create('td',agent.role || 'Unknown'));row.append(create('td',agent.stateLabel || agent.activity || agent.status || 'Unknown'));row.append(create('td',(agent.controllerState || 'Unknown')+(agent.controllerVerified===true?' · verified':'')));rows.append(row);}
      if(!m.sessions.length){const row=create('tr'),cell=create('td',m.stale?'No fresh fleet observations available.':'No current roster available.','empty');cell.colSpan=5;row.append(cell);rows.append(row);}
      set('mission-status',!m.stale&&last?.missionControl?.available?'Current':'Unavailable');
      const lanes=$('lanes');lanes.replaceChildren();for(const lane of m.lanes){const card=create('article',undefined,'lane');card.append(create('h3',lane.id || 'Launch lane'),create('p',lane.activity || lane.status || 'Unknown','state'),create('p',[lane.model,lane.effort].filter(Boolean).join(' · ') || 'Model unavailable'));lanes.append(card);}
      if(!m.lanes.length)lanes.append(create('p','Launch lane telemetry is currently unavailable.'));
      const sources=$('sources');sources.replaceChildren();for(const [name,source] of Object.entries(last?.sources || {})){const row=create('div',undefined,'source');row.append(create('span',labels[name]||name),create('span',m.stale?'Stale':source.available?'Available':source.error || 'Unavailable'));sources.append(row);}
    }
    async function refresh(){if(loading)return;loading=true;$('refresh').disabled=true;try{const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),8000);try{const response=await fetch('./status.json',{cache:'no-store',credentials:'same-origin',signal:controller.signal});if(!response.ok)throw Error('Feed unavailable');const next=await response.json();if(next.schemaVersion!==1||next.readOnly!==true)throw Error('Unknown feed');last=next;failed=false;}finally{clearTimeout(timer);}}catch{failed=true;}finally{loading=false;$('refresh').disabled=false;render();}}
    $('refresh').addEventListener('click',refresh);doc.addEventListener('visibilitychange',()=>{if(!doc.hidden)refresh();});render();refresh();setInterval(()=>{if(!doc.hidden)refresh();},10000);setInterval(render,1000);
  }
  return { freshness, displayModel, start };
});
