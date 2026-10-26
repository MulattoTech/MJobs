'use strict';
window.FleetSustainUI=(()=>{
 let enabled=false,busy=false,last=null,inFlight=false;
 const $s=id=>document.getElementById(id);
 const time=x=>{if(!x)return '—';const d=new Date(x);return Number.isNaN(d.getTime())?'—':d.toLocaleTimeString()};
 const escS=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const isEnabled=()=>enabled;
 function draw(data){
  if(!data)return;
  last=data;enabled=Boolean(data.enabled);
  const block=data.latest?.blocked||data.quotaPausedUntil&&(Date.parse(data.quotaPausedUntil)>Date.now()?'Provider quota pause':'');
  const state=!enabled?'off':block?'paused':'on',throttled=data.latest?.resources?.throttledReason||null;
  const badge=$s('sustainBadge'),pulse=$s('sustainPulse'),toggle=$s('sustainToggle');
  $s('turboSection').classList.toggle('sustaining',enabled);
  badge.className='sustain-badge '+state;badge.textContent=!enabled?'OFF':block?'PAUSED':throttled?'THROTTLED':'RUNNING';
  pulse.className='sustain-orbit '+state;toggle.textContent=enabled?'Pause sustained mode':'Start sustained mode';toggle.classList.toggle('on',enabled);
  $s('sustainFeedback').textContent=!enabled?'Continuous dispatch is off':block?'Paused: '+String(block).slice(0,74):throttled?'CPU busy: pacing dispatch':data.running?'Refilling work now':'Continuous mode enabled; Turbo is off';
  $s('sustainLast').textContent='Last check: '+time(data.lastTick);
  $s('sustainNext').textContent='Next check: '+(enabled?time(data.nextTick):'paused');
  $s('sustainLeases').textContent='Reserved issues: '+(data.leases?.length||0);
 }
 async function load(force=false){
  if(inFlight||busy)return;
  inFlight=true;
  try{
   const data=await api('/api/sustain');
   draw(data);
   if(force&&enabled){
    const p=await api('/api/sustain/preview');
    draw({...data,latest:p});
   }
  }catch(e){$s('sustainBadge').textContent='UNKNOWN';$s('sustainBadge').className='sustain-badge paused';$s('sustainFeedback').textContent='Controller unavailable: '+e.message}
  finally{inFlight=false}
 }
 async function toggle(){
  if(busy)return;
  if(enabled){
   busy=true;$s('sustainToggle').disabled=true;
   try{
    const data=await api('/api/sustain/toggle',{enabled:false});
    draw(data);toast('Sustained dispatch paused. Already active agent work can finish.');
    await load();
   }catch(e){toast(e.message,true)}
   finally{busy=false;$s('sustainToggle').disabled=false}
   return;
  }
  openModal(
   '<h2>Keep your agents working continuously?</h2>'+
   '<p>The local server will check every '+escS(last?.config.intervalSeconds||45)+' seconds for newly idle agents, live PR work, and reserved P0/P1 issues. It can refill these turns for hours without asking you to click again.</p>'+
   '<p>Persistent mode does not use Turbo’s separate 18-dispatch/hour counter. It never creates additional agents, bypasses Codex provider limits, or changes dirty main. Genuine low RAM, CPU pressure and quota errors still pause it.</p>'+
   '<div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="confirmSustain">Start continuous operation</button></div>'
  );
  $s('confirmSustain').addEventListener('click',async()=>{
   if(busy)return;busy=true;$s('confirmSustain').disabled=true;
   try{
    const data=await api('/api/sustain/toggle',{enabled:true});
    closeModal();draw(data);if(typeof FleetTurboUI!=='undefined')FleetTurboUI.refresh();
    toast('Sustained Mode is active. No repeated button presses needed.');
   }catch(e){closeModal();toast(e.message,true)}
   finally{busy=false}
  });
 }
 $s('sustainToggle').addEventListener('click',toggle);
 load();
 setInterval(()=>{if(!document.hidden)load()},8000);
 return {isEnabled,refresh:()=>load(true)};
})();
