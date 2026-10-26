'use strict';
const FleetFindWorkUI=(()=>{
 const el=id=>document.getElementById(id);
 let busy=false;
 let lastSend=null;
 function updateCounter(data){
  const b=el('findWorkAll');
  if(!b)return;
  const sessions=data?.sessions||[];
  const count=sessions.filter(s=>s.isIdle&&['idle','waiting_input'].includes(s.activity)&&!s.replacedBy).length;
  b.textContent='⚡ Find Work · '+count+' Idle';
  b.disabled=busy||count===0||executionUnavailable();
  b.title=count>0?'Ask all currently idle agents to finish their PRs or find scoped work':'No idle agents available';
 }
 async function runOne(id){
  if(busy)return;
  if(executionUnavailable()){toast('Fresh roster and ready safety preflight are required before dispatch.',true);return}
  const s=state.data?.sessions.find(x=>x.id===id);
  if(!s){toast('Agent disappeared from the live fleet; refresh and try again.',true);return}
  if(!s.isIdle||!['idle','waiting_input'].includes(s.activity)||s.replacedBy){toast('This agent is not currently eligible; it must be idle and not superseded.',true);return}
  busy=true;
  const control=document.querySelector('[data-action="find-work"][data-id="'+id+'"]');
  if(control){control.disabled=true;control.textContent='Sending…'}
  try{
   const response=await api('/api/find-work',{id});
   toast('Find Work submitted to '+id+'. Priority: '+response.category+'.');
   await refresh(true);
  }catch(e){toast(e.message,true)}
  finally{
   busy=false;
   if(control&&document.body.contains(control)){control.textContent='Find Work';control.disabled=executionUnavailable()||!(s.isIdle&&!s.replacedBy)}
   updateCounter(state.data);
  }
 }
 async function runAll(){
  if(busy)return;
  if(executionUnavailable()){toast('Fresh roster and ready safety preflight are required before dispatch.',true);return}
  busy=true;
  const btn=el('findWorkAll');
  btn.disabled=true;btn.textContent='Checking candidates…';
  try{
   const preview=await api('/api/find-work/preview');
   if(!preview.eligibleCount){
    toast('No new Find Work messages are eligible right now. Previously nudged agents have a short anti-duplicate cooldown.');
    return;
   }
   const categories={};
   for(const s of preview.eligible)categories[s.category]=(categories[s.category]||0)+1;
   const breakdown=Object.entries(categories).map(([name,count])=>count+' '+name).join(' · ');
   openModal(
    '<h2>Find Work — All Idle</h2>'+
    '<p>Send useful work instructions to <strong>'+preview.eligibleCount+' currently idle agents</strong>. Existing unfinished PRs come first; reviewers seek independent reviews; free implementation workers locate an unclaimed issue and request exclusive file ownership.</p>'+
    '<div class="find-work-preview"><div><strong>'+preview.eligibleCount+'</strong><small>Eligible idle</small></div><div><strong>'+preview.working+'</strong><small>Already working</small></div><div><strong>'+preview.skippedCount+'</strong><small>Skipped / cooldown</small></div></div>'+
    '<p class="find-work-breakdown">'+esc(breakdown)+'</p>'+
    '<p>No new agent sessions, automatic merges, force pushes or overlapping issue/file claims. Agent message acceptance is not a guarantee of sustained work, and Codex quota usage still applies.</p>'+
    '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button id="confirmFindWorkAll" class="find-work-confirm">Send to all eligible idle</button></div>'
   );
   el('confirmFindWorkAll').addEventListener('click',async()=>{
    if(busy)return;
  if(executionUnavailable()){toast('Fresh roster and ready safety preflight are required before dispatch.',true);return}
    busy=true;
    const button=el('confirmFindWorkAll');
    button.disabled=true;
    button.textContent='Dispatching…';
    try{
     const result=await api('/api/find-work/all',{confirm:'FIND WORK ALL'});
     closeModal();
     toast('Find Work: '+result.submittedCount+' submitted; '+result.skippedCount+' skipped; '+result.uncertainCount+' uncertain.');
     if(result.uncertainCount)toast('Some deliveries were uncertain. Check agent activity before retrying those sessions.',true);
     await refresh(true);
    }catch(e){closeModal();toast(e.message,true)}
    finally{busy=false;updateCounter(state.data)}
   });
  }catch(e){toast(e.message,true)}
  finally{
   busy=false;
   updateCounter(state.data);
  }
 }
 el('findWorkAll').addEventListener('click',runAll);
 return {runOne,runAll,updateCounter};
})();
