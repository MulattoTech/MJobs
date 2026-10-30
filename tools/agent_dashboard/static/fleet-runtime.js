(function(){
 'use strict';
 const harnesses={codex:['codex'],antigravity:['agy','gemini','antigravity'],claude:['claude','claude-code'],ollama:['ollama']};
 let snapshot=null,readiness=null,receivedAt=0,error=null,pending=false,refreshing=false,health=null;
 function provider(id){
  const fresh=!!snapshot&&Array.isArray(snapshot.sessions)&&!snapshot.offline&&!error&&Date.now()-receivedAt<25000;
  const sessions=fresh?snapshot.sessions.filter(s=>(harnesses[id]||[]).includes(s.harness)&&!s.isTerminated&&!['exited','stopped'].includes(s.activity)):[];
  return {available:fresh,present:sessions.length>0,total:sessions.length,working:sessions.filter(s=>s.busy).length,models:[...new Set(sessions.map(s=>s.model).filter(Boolean))],sessions};
 }
 globalThis.MJobsFleetObservation={provider};
 if(typeof document==='undefined'){if(typeof module==='object')module.exports={provider};return;}
 const panel=document.createElement('section');panel.className='fleet-runtime-panel';panel.id='fleet-runtime-panel';
 panel.innerHTML='<div class="fleet-runtime-heading"><div><h2>Live AO fleet</h2><p id="live-fleet-summary">Loading managed sessions...</p></div><a href="/fleet/">Open Fleet Control &#8599;</a></div><div class="fleet-runtime-controls"><strong id="fleet-work-mode" role="status">Checking work mode...</strong><a class="live-agent-link" href="/fleet/">Change work mode and capacity</a><button id="fleet-pause" type="button">Pause new assignments</button></div><p id="fleet-control-result" role="status">Choose Paused, Steady, or Turbo in Fleet Control. One mode runs at a time. Pausing lets active turns finish.</p><div id="live-provider-cards"></div>';
 const anchor=document.querySelector('.advanced-control')||document.querySelector('main')||document.body;
 if(anchor.tagName==='MAIN'||anchor===document.body)anchor.prepend(panel);else anchor.insertAdjacentElement('beforebegin',panel);
 const el=(tag,text,cls)=>{const x=document.createElement(tag);if(text!=null)x.textContent=text;if(cls)x.className=cls;return x;};
 const $=id=>document.getElementById(id);
 function render(){
  const live=provider('codex').available;
  const proofAge=Date.now()-Date.parse(readiness?.at||'');
  const currentReadiness=readiness&&Number.isFinite(proofAge)&&proofAge>=0&&proofAge<25000&&!readiness.inspectionPending&&!readiness.stale&&Number.isInteger(readiness.total)&&Number.isInteger(readiness.verified);
  const managed=live?snapshot.sessions.filter(s=>!s.isTerminated&&!['exited','stopped'].includes(s.activity)):[];
  $('live-fleet-summary').textContent=!live?'Fleet observation unavailable or stale. Independent launcher status does not describe the AO fleet.':managed.length+' managed sessions; '+managed.filter(s=>s.busy).length+' working; '+(currentReadiness?'startup targets '+readiness.verified+' / '+readiness.total+' verified':'runtime verification pending');
  const knownMode=live&&health?.ok===true&&typeof health.turboEnabled==='boolean'&&typeof health.sustainedEnabled==='boolean'&&!(health.turboEnabled&&health.sustainedEnabled);
  $('fleet-work-mode').textContent=!knownMode?'Mode unavailable':health.turboEnabled?'Mode: Turbo':health.sustainedEnabled?'Mode: Steady':'Mode: Paused';
  const holder=$('live-provider-cards');holder.replaceChildren();
  for(const [id,label]of Object.entries({codex:'Codex',antigravity:'Antigravity / Gemini',ollama:'Ollama',claude:'Claude'})){
   const data=provider(id),card=el('article',null,'live-provider-card');card.append(el('h3',label),el('strong',!data.available?'Unknown':data.present?(data.working?'Working':'Idle'):'No AO agents'),el('p',data.present?data.total+' runtimes  /  '+data.working+' working':'Independent launcher activity is listed separately below.'));
   for(const session of data.sessions){const a=el('a',session.name||session.id);a.href='/fleet/#agent='+encodeURIComponent(session.id);a.className='live-agent-link';card.append(a);}
   holder.append(card);
  }
  $('fleet-pause').disabled=pending;
 }
 async function read(route){const r=await fetch('/fleet'+route,{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('Fleet service unavailable');return r.json();}
 async function refresh(){
  if(refreshing)return;refreshing=true;
  try{const [s,start,currentHealth]=await Promise.all([read('/api/state'),read('/api/startup'),read('/api/health')]);snapshot=s;readiness=start.readiness;health=currentHealth;receivedAt=Date.now();error=null;}
  catch(e){error=e.message;}finally{refreshing=false;}render();
 }
 async function action(route,body,description){
  if(pending)return;pending=true;render();$('fleet-control-result').textContent='Submitting one request...';
  try{const r=await fetch('/fleet'+route,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-AO-Fleet':'1'},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});const result=await r.json();if(!r.ok)throw Error(result.error||'Local safety checks rejected the request');$('fleet-control-result').textContent=description+' Request accepted; inspect the current fleet state.';await refresh();}
  catch(e){$('fleet-control-result').textContent=e.name==='TimeoutError'?'Outcome unknown. Inspect current state before retrying; no automatic retry was sent.':e.message;}
  finally{pending=false;render();}
 }
 $('fleet-pause').onclick=()=>action('/api/master/pause',{},'Pause new automatic assignments');
 refresh();setInterval(()=>{if(!document.hidden)refresh();},8000);setInterval(render,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
