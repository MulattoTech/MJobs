'use strict';
const FleetRestoreUI=(()=>{
 const el=id=>document.getElementById(id);
 let working=false,status=null,loading=false;
 const escV=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 function phaseName(value){return ({idle:'Not restoring',preparing:'Preparing session manifest',waiting_watchdog:'Waiting for emergency watchdog',starting_ao:'Starting Orchestrator.inc',restoring_primary:'Restoring primary orchestrator',restoring_workers:'Restoring workers and reviewers',verifying:'Verifying runtime health',paused_resources:'Waiting for host resources',partial:'Partial recovery — checking blockers',complete:'Fleet restored',paused:'Recovery paused',failed:'Recovery needs attention',cancelled:'Recovery stopped'})[value]||value||'Unknown'}
 function draw(s,currentReadiness=null){
  status=s;
  const progress=el('restoreProgress'),active=!!s.active;
  progress.hidden=!(active||['paused','complete','partial','paused_resources','failed'].includes(s.phase));
  const total=s.total||0,done=s.restoredCount||0;
  const pct=total?Math.round(done*100/total):0;
  const historical=s.phase==='complete'&&currentReadiness?.ready!==true;
  el('restorePhase').textContent=historical?'Previous restore completed · current health unverified':phaseName(s.phase);
  el('restoreMeterFill').style.width=pct+'%';
  el('restoreNumbers').textContent=done+' / '+total+(historical?' runtimes recorded in the previous restore':' session runtimes verified')+' ('+pct+'%)';
  el('restoreCurrent').textContent=s.lastError?
   'Latest issue: '+String(s.lastError).slice(0,125):
   historical?(currentReadiness?.reason||'Use current Startup status to check the fleet now'):
   s.phase==='complete'?'Session IDs preserved · current runtime health verified':
   'Checking every '+s.nextCheckSeconds+' seconds';
  el('masterRestore').disabled=working||active;
  el('topbarRestore').disabled=working||active;
  el('masterCancelRestore').hidden=!active;
  el('masterCancelRestore').disabled=working;
  if(active){
   const badge=el('masterState');
   if(!document.querySelector('#masterSection')?.classList.contains('emergency-latched')){
    badge.textContent='RESTORING FLEET';badge.className='master-status-badge draining';
    el('masterText').textContent=phaseName(s.phase)+'. New automatic work remains paused until recovery is complete.';
    el('masterExtra').textContent=done+'/'+total+' session runtimes';
   }
  }
 }
 async function readStatus(){
  if(loading||working)return;
  loading=true;
  try{
   const data=await api('/api/master');
   draw(data.restore||{},data.currentReadiness);
  }catch(e){el('restorePhase').textContent='Restore controller unavailable'}
  finally{loading=false}
 }
 async function preview(){
  if(working)return;
  working=true;
  el('masterRestore').disabled=true;
  el('topbarRestore').disabled=true;
  try{
   const data=await api('/api/master/restore/preview');
   const counts={all:data.totalTargets||0,online:data.alreadyOnline||0,toResume:data.willResume||0};
   const conflicts=data.rootConflicts||[];
   const locked=!!data.emergencyLatched;
   let notes=locked?'The emergency stop is active. Use Startup above to verify the reset, release this stop, and arm 7% protection before restoration. This advanced Restore button does not release the stop.':
    'Preserved session IDs, branches and worktrees will be reused. No force-pushes, branch resets or duplicate project roots.';
   const emergencyClass=locked?' warn':'';
   if(!counts.all){toast('No recoverable AO session manifest is available. Live Codex sessions will not be duplicated.',true);return}
   openModal(
    '<h2>Restore the Codex fleet?</h2>'+
    '<p>Restore Orchestrator.inc and saved AO-managed Codex sessions. The primary coordinator comes up first, followed by workers and reviewers in small, health-checked batches.</p>'+
    '<div class="restore-modal-stats"><div><strong>'+counts.all+'</strong><small>Saved sessions</small></div><div><strong>'+counts.online+'</strong><small>Already online</small></div><div><strong>'+counts.toResume+'</strong><small>Need recovery</small></div></div>'+
    '<div class="restore-confirm-note'+emergencyClass+'">'+escV(notes)+'</div>'+
    (conflicts.length?'<div class="restore-confirm-note warn">'+conflicts.length+' conflicting primary sessions require attention; duplicates will not be launched.</div>':'')+
    '<p>This restores saved Orchestrator.inc sessions. Independently launched Codex sessions outside AO are not recreated automatically.</p>'+
    '<label class="restore-checkbox"><input id="restoreAutoDispatch" type="checkbox"> Resume sustained automatic task dispatch after all saved sessions are healthy (otherwise leave dispatch paused).</label>'+
    '<div class="form-field"><label for="restorePhrase">Type <strong>RESTORE FLEET</strong> to confirm</label><input id="restorePhrase" autocomplete="off" spellcheck="false" placeholder="RESTORE FLEET"></div>'+
    '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button id="confirmRestore" class="primary-button" disabled>Begin graceful restore</button></div>'
   );
   const entry=el('restorePhrase'),confirm=el('confirmRestore');
   entry.addEventListener('input',()=>confirm.disabled=locked||entry.value.trim()!=='RESTORE FLEET');
   confirm.addEventListener('click',async()=>{
    if(working||locked||entry.value.trim()!=='RESTORE FLEET')return;
    working=true;confirm.disabled=true;
    try{
     const start=await api('/api/master/restore',{confirm:'RESTORE FLEET',autoDispatchAfter:!!el('restoreAutoDispatch').checked});
     closeModal();draw(start.restore||{});
     toast('Graceful restoration started. Primary first, then worker batches; existing worktrees preserved.');
     el('masterSection').scrollIntoView({behavior:'smooth',block:'start'});
    }catch(e){closeModal();toast(e.message,true)}
    finally{working=false}
   });
   entry.focus();
  }catch(e){toast('Cannot preview restore: '+e.message,true)}
  finally{working=false;el('masterRestore').disabled=false;el('topbarRestore').disabled=false}
 }
 async function pauseRestore(){
  if(working)return;
  working=true;
  try{
   const data=await api('/api/master/restore/pause',{});
   draw(data.restore||{});
   toast('Restoration paused. Already recovered sessions remain intact. Click Restore Fleet to continue.');
  }catch(e){toast(e.message,true)}
  finally{working=false}
 }
 el('masterRestore').addEventListener('click',preview);
 el('topbarRestore').addEventListener('click',preview);
 el('masterCancelRestore').addEventListener('click',pauseRestore);
 readStatus();
 setInterval(readStatus,5000);
 return {refresh:readStatus};
})();
