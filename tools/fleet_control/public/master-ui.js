'use strict';
const FleetMasterUI=(()=>{
 const byId=id=>document.getElementById(id);
 let current=null,busy=false,fetching=false,lastPoll=0;
 function dateTime(x){if(!x)return '—';const d=new Date(x);return Number.isFinite(d.getTime())?d.toLocaleTimeString():'—'}
 function masterState(data){
  const latched=!!data.emergency?.latched;
  const drain=data.drain||{};
  const paused=!data.dispatch?.sustainedEnabled&&!data.dispatch?.turboEnabled;
  const badge=byId('masterState'),panel=byId('masterSection'),led=byId('masterDot');
  const stopBtn=byId('masterKill'),topBtn=byId('topbarKill');
  let status,variant,detail,extra;
  if(latched){
   const e=data.emergency||{},count=e.remainingTotal;
   const checkedAt=Date.parse(e.checkedAt||''),age=Date.now()-checkedAt;
   const fresh=Number.isFinite(age)&&age>=0&&age<35000;
   const zero=count===0&&e.phase==='holding_zero'&&fresh;
   status=!fresh?'STOP STATUS STALE':zero?'AGENTS STOPPED':e.phase==='partial_stop'?'PARTIAL STOP':'EMERGENCY LOCKED';
   variant=zero?'stopped':'emergency';
   detail=!fresh?'The watchdog confirmation is stale or unavailable. Do not treat the last count as current; launch remains blocked.':zero?'All targeted Codex agent and Orchestrator processes are stopped. The verified read-only quota monitor remains available; emergency lockout is still armed.':
    (e.phase==='watchdog_start_error'||e.phase==='watchdog_exited_without_sweep')?'Watchdog failed — check the emergency runtime log. Agent dispatch is blocked.':
    e.phase==='partial_stop'&&e.accessDeniedPids?.length?'Agent dispatch and AO are stopped, but '+e.accessDeniedPids.length+' protected Codex process(es) could not be terminated. Windows reported access denied.':
    'Emergency lockout active. Verifying Codex agent and AO shutdown; the dashboard remains online.';
   extra=count===null||count===undefined?'Checking process count…':count+' targeted processes remain'+(e.excludedQuotaMonitorCount?' · '+e.excludedQuotaMonitorCount+' verified quota monitor(s) excluded':'');
   stopBtn.textContent='■ RETRY EMERGENCY';
   topBtn.textContent='■ EMERGENCY LOCKED';
   byId('masterPause').disabled=true;byId('masterDrain').disabled=true;
   byId('masterRearm').hidden=false;
   byId('masterCancelDrain').hidden=true;
   panel.classList.add('emergency-latched');
   for(const id of ['turboAllHands','turboRun','turboToggle','sustainToggle']){const b=byId(id);if(b)b.disabled=true}
  }else if(data.restore?.active){
   status='RESTORING FLEET';variant='draining';
   detail='Recovering saved sessions in resource-checked batches. New automatic assignments remain paused.';
   extra=(data.restore.restoredCount||0)+' of '+(data.restore.total||0)+' runtimes verified';
   byId('masterPause').disabled=false;byId('masterDrain').disabled=true;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else if(!paused&&!drain.enabled&&(data.dispatch?.blockedReason||data.currentReadiness?.ready===false)){
   status='DISPATCH HELD';variant='paused';
   detail=data.dispatch.blockedReason||data.currentReadiness.reason||'Current runtime readiness is not verified.';
   extra='Scheduler enabled, but no new work is being dispatched. Use protected Startup for stopped runtimes.';
   byId('masterPause').disabled=false;byId('masterDrain').disabled=false;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else if(!drain.enabled&&data.restore?.phase==='complete'&&data.currentReadiness?.ready===true){
   status='FLEET RESTORED';variant='stopped';
   detail='Current session runtimes and primary controllers are verified. Automatic dispatch is '+(paused?'paused.':'enabled.');
   extra=(data.restore.restoredCount||0)+' restored sessions';
   byId('masterPause').disabled=false;byId('masterDrain').disabled=false;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else if(drain.enabled){
   status='WINDING DOWN';variant='draining';
   const c=drain.counts||{};
   detail='No new dispatch. Waiting for active turns; exiting idle agent runtimes.';
   extra=(c.working??'—')+' working · '+(c.idle??'—')+' waiting · '+(c.exited??'—')+' exited';
   byId('masterPause').disabled=true;byId('masterDrain').disabled=true;
   byId('masterCancelDrain').hidden=false;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else if(drain.phase==='completed'){
   status='GRACEFULLY STOPPED';variant='stopped';
   detail='All MJobs agent runtimes exited. Session metadata and Git worktrees remain preserved.';
   extra=Object.keys(drain.processed||{}).length+' agent exits recorded';
   byId('masterPause').disabled=false;byId('masterDrain').disabled=false;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else if(paused){
   status='DISPATCH PAUSED';variant='paused';
   detail='Automatic task dispatch is off. Already running Codex sessions have NOT been stopped.';
   extra='Controllers paused · '+(drain.lastCheck?'last drain '+dateTime(drain.lastCheck):'no active drain');
   byId('masterPause').disabled=false;byId('masterDrain').disabled=false;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }else{
   status='PRODUCTION ACTIVE';variant='on';
   detail='Dispatch is active. Use Pause, Wind Down or Emergency Stop as needed.';
   extra=(data.dispatch.sustainedEnabled?'Sustained Mode':'Turbo Mode')+' running';
   byId('masterPause').disabled=false;byId('masterDrain').disabled=false;
   byId('masterCancelDrain').hidden=true;byId('masterRearm').hidden=true;
   panel.classList.remove('emergency-latched');
  }
  badge.className='master-status-badge '+variant;badge.textContent=status;
  led.className='master-strip-indicator '+(latched?'emergency':variant==='paused'?'paused':variant==='draining'?'paused':'');
  byId('masterText').textContent=detail;
  byId('masterExtra').textContent=extra;
  byId('masterKill').disabled=busy;
  byId('topbarKill').disabled=busy;
 }
 async function refresh(force=false){
  if(fetching||busy)return;
  fetching=true;
  try{const data=await api('/api/master');current=data;masterState(data)}
  catch(e){
   byId('masterState').textContent='CONTROL STATUS UNAVAILABLE';byId('masterState').className='master-status-badge paused';
   byId('masterText').textContent='Shutdown controls temporarily unavailable: '+e.message;
   if(force)toast(e.message,true);
  }finally{fetching=false}
 }
 async function execute(url,data){
  busy=true;
  try{
   const result=await api(url,data);
   current=result;masterState(result);
   return result;
  }finally{busy=false}
 }
 async function pause(){
  if(busy)return;
  try{
   const result=await execute('/api/master/pause',{});
   toast('Automatic dispatch paused; active agents can finish their current turns.');
  }catch(e){toast(e.message,true)}
 }
 function askDrain(){
  if(busy)return;
  openModal('<h2>Graceful wind-down</h2>'+
   '<p>Stop new automatic assignments now. Agents already executing may finish their current work. As each session becomes idle, its runtime will exit, while all branches, uncommitted files and PRs remain available for later recovery.</p>'+
   '<p>This affects MJobs sessions. Other independent Codex sessions are only targeted by Emergency Kill All.</p>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="confirmGraceful">Begin graceful stop</button></div>');
  byId('confirmGraceful').addEventListener('click',async()=>{
   if(busy)return;
   byId('confirmGraceful').disabled=true;
   try{
    await execute('/api/master/drain',{confirm:'GRACEFUL STOP'});
    closeModal();toast('Graceful stop in progress. New dispatch disabled; active turns will drain.');
   }catch(e){closeModal();toast(e.message,true)}
  });
 }
 async function cancelDrain(){
  if(busy)return;
  try{await execute('/api/master/cancel-drain',{});toast('Graceful drain canceled. Dispatch remains paused until you explicitly resume it.')}
  catch(e){toast(e.message,true)}
 }
 function askEmergency(){
  if(busy)return;
  const locked=!!current?.emergency?.latched;
  if(locked){
   openModal('<h2>Emergency watchdog</h2><p>The emergency lockout is already armed and checks for relaunched Codex processes. You can request another sweep without restoring your Codex sessions.</p><div class="modal-actions"><button class="ghost-button" data-modal="cancel">Close</button><button class="master-confirm" id="retryEmergency">Recheck watchdog</button></div>');
   byId('retryEmergency').addEventListener('click',async()=>{
    try{await execute('/api/master/emergency',{confirm:'KILL ALL CODEX'});closeModal();toast('Emergency watchdog verified or restarted.')}
    catch(e){closeModal();toast(e.message,true)}
   });
   return;
  }
  openModal('<h2>Emergency: Kill ALL Codex</h2>'+
   '<div class="emergency-warning"><strong>This is an immediate, non-graceful shutdown.</strong> It terminates targeted Codex agent sessions, REPL helpers, and Orchestrator.inc on MulattoTechBox, including agents outside MJobs. Shared Codex app-server backends are included. Only the verified isolated quota monitor and Windows Codex Sandbox Service are preserved. Elevated protected processes may require separate administrator action. In-memory uncommitted work may be lost.</div>'+
   '<p>The web dashboard will remain running. An emergency watchdog continually checks for relaunched agent processes until you explicitly release the lockout. This does not bypass usage limits or undo consumption already incurred.</p>'+
   '<div class="form-field"><label for="emergencyPhrase">Type <strong>KILL ALL CODEX</strong> to confirm</label><input id="emergencyPhrase" class="emergency-input" autocomplete="off" spellcheck="false" placeholder="KILL ALL CODEX"></div>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="master-confirm" id="confirmEmergency" disabled>■ KILL EVERYTHING NOW</button></div>');
  const phrase=byId('emergencyPhrase'),confirm=byId('confirmEmergency');
  phrase.addEventListener('input',()=>{confirm.disabled=phrase.value.trim()!=='KILL ALL CODEX'});
  phrase.focus();
  confirm.addEventListener('click',async()=>{
   if(busy||phrase.value.trim()!=='KILL ALL CODEX')return;
   confirm.disabled=true;confirm.textContent='ARMING EMERGENCY STOP…';
   try{
    await execute('/api/master/emergency',{confirm:'KILL ALL CODEX'});
    closeModal();
    toast('Emergency lockout activated. Process termination and verification are running.');
    byId('masterSection').scrollIntoView({block:'start',behavior:'smooth'});
   }catch(e){
    closeModal();toast('Emergency action error: '+e.message,true);
   }
  });
 }
 function askRearm(){
  if(busy)return;
  openModal('<h2>Release emergency lockout?</h2><p>Only do this once your Codex allowance has reset. This releases the process shutdown latch but does not restart anything. You must then wait for a fresh usage reading and explicitly re-arm the 7% quota guard before Restore Fleet can run.</p>'+
   '<div class="form-field"><label for="rearmPhrase">Type <strong>REARM</strong></label><input id="rearmPhrase" class="emergency-input" placeholder="REARM" spellcheck="false" autocomplete="off"></div>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="master-confirm" id="confirmRearm" disabled>Re-arm controls</button></div>');
  const field=byId('rearmPhrase'),confirm=byId('confirmRearm');
  field.addEventListener('input',()=>confirm.disabled=field.value.trim()!=='REARM');
  confirm.addEventListener('click',async()=>{
   if(busy||field.value.trim()!=='REARM')return;
   confirm.disabled=true;
   try{await execute('/api/master/rearm',{confirm:'REARM'});closeModal();toast('Emergency lockout released. Codex stays stopped until restarted manually.')}
   catch(e){closeModal();toast(e.message,true)}
  });
 }
 byId('masterPause').addEventListener('click',pause);
 byId('masterDrain').addEventListener('click',askDrain);
 byId('masterKill').addEventListener('click',askEmergency);
 byId('topbarKill').addEventListener('click',askEmergency);
 byId('masterCancelDrain').addEventListener('click',cancelDrain);
 byId('masterRearm').addEventListener('click',askRearm);
 refresh();
 setInterval(()=>{if(!document.hidden||current?.emergency?.latched)refresh()},5000);
 return {refresh,askEmergency};
})();
