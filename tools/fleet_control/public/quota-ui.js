'use strict';
const FleetQuotaUI=(()=>{
 const el=id=>document.getElementById(id);
 let current=null,requesting=false,busy=false,settingsDirty=false,minimumDirty=false;
 const prettyTime=value=>{if(!value)return 'Not reported';const d=new Date(value);return Number.isNaN(d.valueOf())?'Unknown':d.toLocaleString()};
 function colorFor(value){return value<=10?'critical':value<=25?'warning':''}
 function paintWindow(label,valueId,barId,resetId,data){
  const row=(data.windows||[]).find(x=>x.label===label),available=!!row;
  const number=el(valueId),fill=el(barId),reset=el(resetId);
  const fresh=available&&data.fresh;
  number.textContent=available?row.remainingPercent+'%'+(data.fresh?'':' last seen'):'—%';
  number.className='quota-number'+(available?(colorFor(row.remainingPercent)?' '+colorFor(row.remainingPercent):''):' unknown');
  fill.style.width=available?Math.max(0,Math.min(100,row.remainingPercent))+'%':'0%';
  fill.className='quota-fill'+(label==='5-hour'?' secondary':'')+(available&&colorFor(row.remainingPercent)?' '+colorFor(row.remainingPercent):'');
  fill.style.opacity=data.fresh?'1':'.4';
  reset.textContent=available?'Resets '+prettyTime(row.resetsAt):'Not exposed by this Codex account';
 }
 function render(data){
  current=data;
  const section=el('quotaSection'),live=el('quotaLive'),guard=el('quotaGuardStatus');
  const armed=!!data.config.autoKillArmed;
  const tripped=!!data.trigger.tripped;
  section.classList.toggle('guard-armed',armed);
  section.classList.toggle('guard-tripped',tripped);
  const liveState=tripped?'tripped':data.fresh?'fresh':'stale';
  live.className='quota-live '+liveState;
  live.textContent=tripped?'EMERGENCY TRIPPED · '+(data.fresh?'USAGE LIVE':'USAGE STALE'):data.fresh?'LIVE · '+data.ageSeconds+'s':'NO FRESH READING';
  el('quotaUpdated').textContent=data.fresh?'Sampled '+prettyTime(data.sampledAt):'Last good sample: '+prettyTime(data.sampledAt);
  paintWindow('Weekly','quotaWeeklyValue','quotaWeeklyBar','quotaWeeklyReset',data);
  paintWindow('5-hour','quotaFiveValue','quotaFiveBar','quotaFiveReset',data);
  const led=el('quotaSourceLed');
  led.className='quota-source-led '+(data.fresh?'fresh':'stale');
  el('quotaSourceText').textContent=tripped?
   (data.fresh?'Emergency lockout active · live, read-only Codex usage polling continues; all agent starts remain blocked.':'Emergency lockout active · quota reading unavailable or stale; all agent starts remain blocked.'):
   data.fresh?'Codex App Server · read-only · '+data.precision+' · '+(data.monitoredWindow||'Codex')+' monitored':
   (data.warning||data.error||'Codex rate-limit data unavailable');
  const snapshot=data.config;
  const minimum=snapshot.minimumRemainingPercent??Math.max(7,snapshot.thresholdPercent)+8;
  const policy=data.policy||{blocked:!data.fresh||data.selectedRemainingPercent<=minimum,reason:'Current '+data.selectedRemainingPercent+'%; start/work minimum '+minimum+'%; emergency '+snapshot.thresholdPercent+'%.'};
  el('quotaPolicyPanel').classList.toggle('blocked',policy.blocked);
  el('quotaPolicyState').textContent=policy.blocked?'START / NEW WORK BLOCKED BY QUOTA':'QUOTA ALLOWS START / NEW WORK';
  el('quotaPolicyReason').textContent=policy.reason+' Healthy agents may still be online while new work is blocked.';
  if(!minimumDirty)el('quotaMinimum').value=minimum;
  el('quotaMinimum').min=Math.max(7,snapshot.thresholdPercent);
  el('quotaMinimumSave').disabled=busy||tripped;

  if(!settingsDirty){
   el('quotaThreshold').value=snapshot.thresholdPercent;
   el('quotaWindow').value=snapshot.window;
  }
  el('quotaInterval').textContent=snapshot.pollSeconds+' seconds';
  if(tripped)guard.textContent='AUTOMATIC STOP FIRED · '+prettyTime(data.trigger.triggeredAt);
  else if(armed)guard.textContent='ARMED · new assignments brake at '+minimum+'%, emergency agent stop at '+snapshot.thresholdPercent+'% ('+
   (snapshot.window==='any'?'lowest available':snapshot.window)+')';
  else guard.textContent='Not armed · choose a threshold, save, then arm';
  const button=el('quotaArm');
  button.textContent=armed?'Disarm automatic stop':tripped?'Re-arm after recovery':'ARM AUTO-KILL';
  button.className='quota-arm'+(armed?' armed':'');
  button.disabled=busy||tripped||(!armed&&(!data.fresh||data.selectedRemainingPercent===null));
  el('quotaSave').disabled=busy||tripped;
  const top=el('quotaTopbar');
  if(top){
   top.textContent=tripped?(data.fresh&&data.selectedRemainingPercent!==null?'Codex '+data.selectedRemainingPercent+'% left · STOP TRIPPED':'CODEX STOP TRIPPED · USAGE STALE'):data.fresh&&data.selectedRemainingPercent!==null?
    'Usage '+data.selectedRemainingPercent+'% remaining'+(armed?' · STOP GUARD ARMED':''):
    'Codex usage: —';
   top.className='quota-topbar'+(armed?' armed':'');
  }
 }
 async function reload(){
  if(requesting||busy)return;
  requesting=true;
  try{
   const d=await api('/api/quota');
   render(d);
  }catch(e){
   el('quotaLive').textContent='MONITOR UNAVAILABLE';
   el('quotaSourceText').textContent=e.message;
  }finally{requesting=false}
 }
 async function save(){
  if(busy)return;
  const threshold=Number(el('quotaThreshold').value);
  const window=el('quotaWindow').value;
  if(!Number.isInteger(threshold)||threshold<0||threshold>50){
   toast('Enter a whole remaining percentage from 0 to 50.',true);return;
  }
  busy=true;
  try{
   const data=await api('/api/quota/config',{thresholdPercent:threshold,window,enabled:true});
   settingsDirty=false;render(data);
   toast('Codex threshold saved. '+(data.config.autoKillArmed?'Guard remains armed.':'Arm auto-stop when ready.'));
  }catch(e){toast(e.message,true)}
  finally{busy=false}
 }
 async function arm(){
  if(busy)return;
  if(current?.config.autoKillArmed){
   busy=true;
   try{
    const d=await api('/api/quota/disarm',{});
    render(d);toast('Automatic quota stop disarmed; usage monitor still updates.');
   }catch(e){toast(e.message,true)}
   finally{busy=false}
   return;
  }
  const saved=current?.config;
  const inputThreshold=Number(el('quotaThreshold').value);
  const window=el('quotaWindow').value;
  if(settingsDirty||inputThreshold!==saved?.thresholdPercent||window!==saved.window){
   toast('Save your threshold first, then arm automatic stop.',true);return;
  }
  if(!current?.fresh||current.selectedRemainingPercent===null){
   toast('Fresh Codex quota telemetry is required before arming.',true);return;
  }
  const threshold=saved.thresholdPercent,remaining=current.selectedRemainingPercent;
  const immediate=remaining<=threshold;
  openModal('<h2>Arm automatic emergency stop?</h2>'+
   '<p>The quota monitor checks in the background even with the browser closed. New assignments brake at <strong>'+(saved.minimumRemainingPercent??Math.max(7,threshold)+8)+'% remaining</strong>. At <strong>'+threshold+'% or below</strong>, the emergency stop terminates targeted agent runtimes and Orchestrator.inc. Only the verified isolated quota monitor and Windows Codex Sandbox Service are preserved.</p>'+
   '<div class="emergency-warning"><strong>Current verified remaining: '+remaining+'%.</strong> '+(immediate?
    'This is ALREADY at or below your threshold and may trigger an immediate shutdown.':
    'Provider reporting is rounded and can lag actual usage, so stopping at the precise percentage cannot be guaranteed.')+
   '</div>'+
   '<p>Automatic shutdown is a forced termination. Unsaved in-memory work can be lost. The monitor disables its auto-kill after firing and requires fresh authorization for another run.</p>'+
   '<div class="form-field"><label>Type <strong>ARM AUTO STOP</strong> to confirm</label><input id="quotaArmPhrase" class="emergency-input" autocomplete="off" spellcheck="false" placeholder="ARM AUTO STOP"></div>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button id="quotaArmConfirm" class="master-confirm" disabled>Arm automatic stop</button></div>');
  const entry=el('quotaArmPhrase'),confirm=el('quotaArmConfirm');
  entry.addEventListener('input',()=>{confirm.disabled=entry.value.trim()!=='ARM AUTO STOP'});
  confirm.addEventListener('click',async()=>{
   if(busy||entry.value.trim()!=='ARM AUTO STOP')return;
   busy=true;confirm.disabled=true;
   try{
    const d=await api('/api/quota/arm',{confirm:'ARM AUTO STOP'});
    closeModal();render(d);
    toast('Automatic kill armed at '+d.config.thresholdPercent+'% remaining. It will work with the browser closed.');
   }catch(e){closeModal();toast(e.message,true)}
   finally{busy=false}
  });
 }
 async function saveMinimum(){
  if(busy||!current)return;
  const value=Number(el('quotaMinimum').value),floor=Math.max(7,current.config.thresholdPercent);
  if(!Number.isInteger(value)||value<floor||value>99){toast('Start/new-work limit must be '+floor+' to 99%. Emergency protection cannot be bypassed.',true);return;}
  if(value<current.config.minimumRemainingPercent&&!window.confirm('Lower the start/new-work quota limit to '+value+'%? Already-enabled dispatch may resume. The '+current.config.thresholdPercent+'% emergency stop is unchanged; less headroom increases overshoot risk.'))return;
  busy=true;el('quotaMinimumSave').disabled=true;
  try{const data=await api('/api/quota/config',{minimumRemainingPercent:value});minimumDirty=false;render(data);el('quotaMinimumFeedback').textContent='SAVED: new starts/tasks require more than '+data.config.minimumRemainingPercent+'% remaining. Emergency stop is unchanged at '+data.config.thresholdPercent+'%. No Startup request was sent.';toast('Start/new-work quota limit saved. Emergency stop unchanged.');if(typeof FleetStartupUI!=='undefined')FleetStartupUI.refresh(true);}
  catch(e){el('quotaMinimumFeedback').textContent='Not saved: '+e.message;toast(e.message,true);}
  finally{busy=false;el('quotaMinimumSave').disabled=false;reload();}
 }
 el('quotaMinimum').addEventListener('input',()=>{minimumDirty=true;el('quotaMinimumFeedback').textContent='UNSAVED change. Save start/work limit to apply; emergency protection is separate.';});
 el('quotaMinimumSave').addEventListener('click',saveMinimum);
 for(const n of ['quotaThreshold','quotaWindow'])el(n).addEventListener('input',()=>{settingsDirty=true});
 el('quotaSave').addEventListener('click',save);
 el('quotaArm').addEventListener('click',arm);
 reload();
 setInterval(reload,2000);
 return {refresh:reload};
})();
