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

// Capacity settings do not launch work. The existing guarded config route persists them.
(function installProductiveSettings(){
 const root=document.getElementById('turboSection');if(!root)return;
 const panel=document.createElement('div');panel.className='productive-settings';
 panel.innerHTML='<h3>Keep workers productive</h3><p>Use evidence-backed maintenance when priority work or CI is waiting. Reserved workers are labeled on-call, never counted as executing.</p><div class="productive-controls"><label><input id="productiveEnabled" type="checkbox"> Discover useful maintenance work</label><label>Concurrent turn target <input id="productiveTarget" type="number" min="3" max="64" value="31"></label><label>Explicit review reserves <input id="productiveReserves" type="number" min="0" max="8" value="0"></label><button id="productiveAll" class="ghost-button">Use all current workers</button><button id="productiveSave" class="ghost-button">Save capacity policy</button></div><p id="productiveStatus" class="helper"></p><a href="http://127.0.0.1:20800/mission-control/analytics/" target="_blank" rel="noopener">Open Fleet Atlas · turn statistics &amp; queue ↗</a>';
 root.appendChild(panel);let dirty=false,busy=false;
 for(const id of ['productiveEnabled','productiveTarget','productiveReserves'])document.getElementById(id).addEventListener('input',()=>{dirty=true;});
 async function read(){if(dirty||busy||document.hidden)return;try{const v=await api('/api/sustain');document.getElementById('productiveEnabled').checked=v.config.productiveWork===true;document.getElementById('productiveTarget').value=v.config.targetWorking;document.getElementById('productiveReserves').value=v.config.reserveWorkers||0;const p=v.productivity||{};document.getElementById('productiveStatus').textContent=(p.catalogCount||0)+' real opportunities · '+(p.assigned||0)+' assigned · '+(p.artifactsReady||0)+' drafts awaiting review. Resource and 7% quota protection remain enforced.';}catch{document.getElementById('productiveStatus').textContent='Policy status unavailable; no change has been sent.';}}
 document.getElementById('productiveAll').addEventListener('click',async()=>{if(busy)return;try{const roster=await api('/api/state');if(roster.offline)throw Error('A live roster is required');const count=roster.sessions.filter(s=>!s.isTerminated&&!s.replacedBy).length;if(count<3||count>64)throw Error('Roster outside supported capacity');document.getElementById('productiveEnabled').checked=true;document.getElementById('productiveTarget').value=count;document.getElementById('productiveReserves').value=0;dirty=true;document.getElementById('productiveStatus').textContent='All '+count+' current runtimes selected, with no reserve. Save the policy, then use Startup or Resume Sustained. Nothing has started yet.';}catch(e){document.getElementById('productiveStatus').textContent=e.message;}});
 document.getElementById('productiveSave').addEventListener('click',async()=>{if(busy)return;const button=document.getElementById('productiveSave');busy=true;button.disabled=true;try{const config={productiveWork:document.getElementById('productiveEnabled').checked,targetWorking:Number(document.getElementById('productiveTarget').value),reserveWorkers:Number(document.getElementById('productiveReserves').value)};await api('/api/sustain/config',config);dirty=false;document.getElementById('productiveStatus').textContent='Capacity policy saved. Existing dispatch mode was not changed.';}catch(e){document.getElementById('productiveStatus').textContent=e.message;}finally{busy=false;button.disabled=false;read();}});
 read();setInterval(read,10000);
})();
