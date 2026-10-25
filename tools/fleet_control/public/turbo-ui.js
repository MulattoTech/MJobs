'use strict';
const FleetTurboUI=(()=>{
 let last=null, busy=false, configured=false, settingsDirty=false, previewed=false, refreshInFlight=false;
 const el=id=>document.getElementById(id);
 const integer=id=>Number.parseInt(el(id).value,10);
 const stamp=iso=>{
  if(!iso)return 'Never';
  const date=new Date(iso);
  if(!Number.isFinite(date.getTime()))return 'Unknown';
  return date.toLocaleTimeString();
 };
 function fillSettings(c){
  el('tTarget').value=c.targetWorking;
  el('tActions').value=c.maxActionsPerCycle;
  el('tRam').value=c.minFreeRamGiB;
  el('tCooldown').value=c.perAgentCooldownMinutes;
  el('tPoll').value=c.pollingSeconds;
  el('tHourly').value=c.maxActionsPerHour;
 }
 function updateBadge(value,pausedReason){
  const status=el('turboStatus'),switcher=el('turboToggle'),label=el('turboToggleLabel'),nav=el('turboNavDot');
  const live=Boolean(value),paused=live&&Boolean(pausedReason);
  status.textContent=!live?'OFF':paused?'PAUSED':'ACTIVE';
  status.className='turbo-status '+(!live?'off':paused?'paused':'on');
  switcher.disabled=busy;
  switcher.classList.toggle('on',live);
  switcher.setAttribute('aria-pressed',String(live));
  label.textContent=live?'Pause Turbo':'Enable Turbo';
  nav.className='turbo-nav-indicator'+(live?(paused?' paused':' on'):'');
  el('turboRun').disabled=!live||busy;
 }
 function planRow(x){
  const kind=esc(x.kind||'queue'),id=esc(x.id||'?');
  return '<div class="turbo-task"><span class="turbo-task-badge '+kind+'">'+kind.toUpperCase()+'</span><div class="turbo-task-details"><strong>'+id+' · '+esc(x.name||'')+'</strong><small>'+esc(x.reason||'')+'</small></div></div>';
 }
 function setPlan(p){
  const node=el('turboPlan'),plan=p?.nextActions||[];
  if(p?.pausedReason){
   node.innerHTML='<div class="turbo-placeholder">Dispatch paused: '+esc(p.pausedReason)+'. The queue and project are preserved.</div>';
  }else if(!plan.length){
   node.innerHTML='<div class="turbo-placeholder">No eligible new assignments right now. Current work may be awaiting CI, review, or an ownership decision.</div>';
  }else node.innerHTML=plan.map(planRow).join('');
  const counts=p?.counts||{};
  el('turboMerged').textContent=counts.mergedLastHour??'—';
  el('turboGuard').textContent=p?.pausedReason==='Hourly action budget reached'?'Auto dispatch limit · All Hands available':(p?.pausedReason||'Within limits');
  el('turboGuard').classList.toggle('warning',Boolean(p?.pausedReason));
  el('turboLastTick').textContent=p?.snapshotAt?'Snapshot '+stamp(p.snapshotAt):'Not evaluated';
  el('turboHint').textContent=p?.counts?'Open PRs: '+(counts.openPRs??'—')+' · Failing: '+(counts.failedPRs??'—')+' · Working: '+(counts.working??'—'):'Automatic actions are throttled and audited.';
 }
 function draw(data){
  last=data;
  if(!configured||!settingsDirty){fillSettings(data.config);configured=true}
  el('turboTarget').textContent=data.config.targetWorking+' agents';
  el('turboHour').textContent=data.sentThisHour+' / '+data.config.maxActionsPerHour;
  const pausedReason=data.lastPlan?.pausedReason || (data.running?'Cycle running':null);
  updateBadge(data.config.enabled,pausedReason);
  if(data.lastPlan?.ready!==undefined)setPlan(data.lastPlan);
  if(data.lastError)el('turboHint').title='Latest scheduler error: '+data.lastError;
 }
 async function fetchState(){
  if(refreshInFlight||busy)return;
  refreshInFlight=true;
  try{
   const data=await api('/api/turbo');draw(data);
   if(!previewed){previewed=true;await preview(false);}
  }catch(e){el('turboGuard').textContent='Unavailable';el('turboGuard').classList.add('warning');el('turboHint').textContent=e.message}
  finally{refreshInFlight=false}
 }
 async function preview(notify=true){
  try{
   const d=await api('/api/turbo/preview');
   setPlan(d.preview);
   if(last){last.lastPlan=d.preview;updateBadge(last.config.enabled,d.preview.pausedReason);}
   if(notify)toast('Dry-run preview refreshed. No agent messages sent.');
  }catch(e){if(notify)toast(e.message,true)}
 }
 async function settings(){
  if(busy)return;
  const change={
   targetWorking:integer('tTarget'),
   maxActionsPerCycle:integer('tActions'),
   maxActionsPerHour:integer('tHourly'),
   perAgentCooldownMinutes:integer('tCooldown'),
   pollingSeconds:integer('tPoll'),
   minFreeRamGiB:integer('tRam')
  };
  busy=true;
  try{
   const d=await api('/api/turbo/config',change);
   configured=false;settingsDirty=false;draw(d);toast('Turbo safety limits saved.');
   await preview(false);
  }catch(e){toast('Settings rejected: '+e.message,true)}
  finally{busy=false}
 }
 async function toggle(){
  if(busy||!last)return;
  const now=last.config.enabled;
  if(now){
   busy=true;
   try{const d=await api('/api/turbo/toggle',{enabled:false});draw(d);toast('Turbo paused. Existing agent work continues.')}
   catch(e){toast(e.message,true)}finally{busy=false}
   return;
  }
  openModal(
   '<h2>Enable Turbo Mode?</h2>'+
   '<p>While this computer and dashboard server are running, Turbo will periodically send bounded instructions to the Sol primary, idle CI owners, available reviewers and unused workers. Actions consume the selected provider usage.</p>'+
   '<p>No automatic merges, additional agent spawns, force pushes or duplicate issue claims. Resource pressure pauses dispatch automatically.</p>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="enableTurboConfirmed">Enable automation</button></div>'
  );
  el('enableTurboConfirmed').addEventListener('click',async()=>{
   if(busy)return;busy=true;el('enableTurboConfirmed').disabled=true;
   try{
    const d=await api('/api/turbo/toggle',{enabled:true});
    closeModal();draw(d);window.FleetSustainUI?.refresh();toast('Turbo enabled under the configured limits. Sustained Mode is off.');
    await preview(false);
   }catch(e){closeModal();toast(e.message,true)}finally{busy=false}
  });
 }
 async function runOnce(){
  if(!last?.config.enabled||busy)return;
  openModal('<h2>Run a Turbo dispatch cycle now?</h2><p>This may send up to '+esc(last.config.maxActionsPerCycle)+' new agent instructions, subject to cooldowns, resource guards and the hourly action limit. It will not spawn new agents or merge PRs.</p><div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="runTurboConfirmed">Run dispatch</button></div>');
  el('runTurboConfirmed').addEventListener('click',async()=>{
   if(busy)return;busy=true;el('runTurboConfirmed').disabled=true;
   try{
    const d=await api('/api/turbo/run',{dryRun:false});
    closeModal();
    const total=d.executed?.length||0;
    toast(total?'Submitted '+total+' dispatch(es). Check agent activity for their responses.':'No dispatch sent: '+(d.reason||'No eligible work'));
    await fetchStateNow();
   }catch(e){closeModal();toast(e.message,true)}finally{busy=false}
  });
 }
 async function fetchStateNow(){try{const d=await api('/api/turbo');draw(d)}catch(e){toast(e.message,true)}}
 async function allHands(){
  if(busy)return;
  busy=true;
  const button=el('turboAllHands');
  button.disabled=true;
  button.textContent='Checking idle agents…';
  try{
   const p=await api('/api/turbo/all-hands/preview');
   const count=p.plan?.length||0;
   const working=p.working?.length||0;
   const skipped=p.skipped?.length||0;
   if(p.resourceGuard)throw Error('Resource safety pause: '+p.resourceGuard);
   if(p.globalCooldownMinutes>0)toast('The prior one-time burst is cooling down. Sustained Mode can start immediately and refill tasks automatically.');
   if(!count){toast('No eligible idle agents to activate. '+working+' already working.');return}
   openModal(
    '<h2>⚡ All Hands — activate '+count+' idle agents?</h2>'+
    '<p>This sends one bounded burst to eligible idle agents. Turbo and Sustained Mode settings stay unchanged; enable a continuous mode separately if desired.</p>'+
    '<div class="allhands-preview"><div><strong>'+count+'</strong><small>Will contact</small></div><div><strong>'+working+'</strong><small>Already working</small></div><div><strong>'+skipped+'</strong><small>Cooldown skips</small></div></div>'+
    '<div class="allhands-warning">This manual burst has its own cooldown. Provider quota, resource and file ownership guards still apply. Agents will use their existing PRs or ask the primary for nonoverlapping assignments; active worktrees and branch protections are preserved.</div>'+
    '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="all-hands-button" id="confirmAllHands">⚡ ACTIVATE ALL IDLE</button></div>'
   );
   el('confirmAllHands').addEventListener('click',async()=>{
    if(busy)return;
    busy=true;
    const confirm=el('confirmAllHands');
    confirm.disabled=true;confirm.textContent='Sending requests…';
    try{
     const r=await api('/api/turbo/all-hands',{confirm:'ALL HANDS'});
     closeModal();
     openModal(
      '<h2>All Hands dispatched</h2>'+
      '<div class="allhands-preview"><div><strong>'+esc(r.submitted)+'</strong><small>Submitted</small></div><div><strong>'+esc(r.skipped)+'</strong><small>Skipped</small></div><div><strong>'+esc(r.uncertain)+'</strong><small>Uncertain</small></div></div>'+
      '<p>'+esc(r.note||'Messages submitted to the fleet.')+'</p>'+
      '<p>Agents may return to idle after their turns. The live roster shows which are actually executing.</p>'+
      '<div class="modal-actions"><button class="primary-button" data-modal="cancel">Return to fleet</button></div>'
     );
     toast(r.submitted+' agent instructions submitted. Automatic mode settings were unchanged.');
     await fetchStateNow();
     if(typeof refresh==='function')await refresh(true);
    }catch(e){closeModal();toast('All Hands failed: '+e.message,true)}
    finally{busy=false}
   });
  }catch(e){toast(e.message,true)}
  finally{busy=false;button.disabled=false;button.innerHTML='<span class="all-hands-icon">⚡</span> ALL HANDS · START IDLE AGENTS'}
 }
 function bind(){
  ['tTarget','tActions','tRam','tCooldown','tPoll','tHourly'].forEach(id=>el(id).addEventListener('input',()=>{settingsDirty=true}));
  el('turboAllHands').addEventListener('click',allHands);
  el('turboToggle').addEventListener('click',toggle);
  el('turboPreview').addEventListener('click',()=>preview());
  el('turboRun').addEventListener('click',runOnce);
  el('turboSave').addEventListener('click',settings);
  el('turboRefresh').addEventListener('click',()=>preview());
  el('openTurbo').addEventListener('click',()=>{
   el('turboSection').scrollIntoView({behavior:'smooth',block:'start'});
   preview(false);
  });
  fetchState();
  setInterval(()=>{if(!document.hidden)fetchState()},8000);
 }
 bind();
 return {refresh:fetchStateNow,preview};
})();