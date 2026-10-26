(function(){
 'use strict';
 const harnesses={codex:['codex'],antigravity:['agy','gemini','antigravity'],claude:['claude','claude-code'],ollama:['ollama']};
 let snapshot=null,readiness=null,receivedAt=0,error=null,pending=false,selectedTarget=false,refreshing=false;
 function provider(id){
  const fresh=!!snapshot&&!snapshot.offline&&!error&&Date.now()-receivedAt<25000;
  const sessions=fresh?snapshot.sessions.filter(s=>(harnesses[id]||[]).includes(s.harness)&&!s.isTerminated&&!['exited','stopped'].includes(s.activity)):[];
  return {available:fresh,present:sessions.length>0,total:sessions.length,working:sessions.filter(s=>s.busy).length,models:[...new Set(sessions.map(s=>s.model).filter(Boolean))],sessions};
 }
 globalThis.MJobsFleetObservation={provider};
 if(typeof document==='undefined'){if(typeof module==='object')module.exports={provider};return;}
 const panel=document.createElement('section');panel.className='fleet-runtime-panel';panel.id='fleet-runtime-panel';
 panel.innerHTML='<div class="fleet-runtime-heading"><div><h2>Live AO fleet</h2><p id="live-fleet-summary">Loading actual managed runtimes…</p></div><a href="/fleet/">Open Fleet Control →</a></div><div class="fleet-runtime-controls"><button id="fleet-pause" type="button">Pause new assignments</button><button id="fleet-resume" type="button">Resume Sustained</button><label>Working target <input id="fleet-target" type="number" min="3" max="29" value="12"></label><button id="fleet-save-target" type="button">Set target</button></div><p id="fleet-control-result" role="status">Pause does not kill active turns. These controls affect the AO fleet, not independent launchers.</p><div id="live-provider-cards"></div>';
 const anchor=document.querySelector('.advanced-control')||document.querySelector('main')||document.body;
 if(anchor.tagName==='MAIN'||anchor===document.body)anchor.prepend(panel);else anchor.insertAdjacentElement('beforebegin',panel);
 const el=(tag,text,cls)=>{const x=document.createElement(tag);if(text!=null)x.textContent=text;if(cls)x.className=cls;return x;};
 const $=id=>document.getElementById(id);
 function render(){
  const live=provider('codex').available;
  const proofAge=Date.now()-Date.parse(readiness?.at||'');
  const currentReadiness=readiness&&Number.isFinite(proofAge)&&proofAge>=0&&proofAge<25000&&!readiness.inspectionPending&&!readiness.stale&&Number.isInteger(readiness.total)&&Number.isInteger(readiness.verified);
  $('live-fleet-summary').textContent=!live?'Fleet observation unavailable or stale. Independent launcher status does not describe the AO fleet.':snapshot.summary.total+' runtimes observed · '+snapshot.summary.working+' working · '+(currentReadiness?readiness.verified+' / '+readiness.total+' verified':'runtime verification pending');
  const holder=$('live-provider-cards');holder.replaceChildren();
  for(const [id,label]of Object.entries({codex:'Codex',antigravity:'Antigravity / Gemini',ollama:'Ollama',claude:'Claude'})){
   const data=provider(id),card=el('article',null,'live-provider-card');card.append(el('h3',label),el('strong',!data.available?'Unknown':data.present?(data.working?'Working':'Idle'):'No AO agents'),el('p',data.present?data.total+' runtimes · '+data.working+' working':'Independent launcher activity is listed separately below.'));
   for(const session of data.sessions){const a=el('a',session.name||session.id);a.href='/fleet/#agent='+encodeURIComponent(session.id);a.className='live-agent-link';card.append(a);}
   holder.append(card);
  }
  $('fleet-resume').disabled=pending||!live||!currentReadiness||readiness.ready!==true;
  $('fleet-resume').title=$('fleet-resume').disabled?'Protected Startup must verify the fleet before enabling new work.':'';
  $('fleet-save-target').disabled=pending||!!error||!snapshot||Date.now()-receivedAt>=25000;
  $('fleet-pause').disabled=pending;
 }
 async function read(route){const r=await fetch('/fleet'+route,{cache:'no-store',signal:AbortSignal.timeout(8000)});if(!r.ok)throw Error('Fleet service unavailable');return r.json();}
 async function refresh(){
  if(refreshing)return;refreshing=true;
  try{const [s,start,sustain]=await Promise.all([read('/api/state'),read('/api/startup'),read('/api/sustain')]);snapshot=s;readiness=start.readiness;receivedAt=Date.now();error=null;if(!selectedTarget&&document.activeElement!==$('fleet-target'))$('fleet-target').value=String(sustain.config?.targetWorking||12);}
  catch(e){error=e.message;}finally{refreshing=false;}render();
 }
 async function action(route,body,description){
  if(pending)return;if(!confirm(description))return;pending=true;render();$('fleet-control-result').textContent='Submitting one request…';
  try{const r=await fetch('/fleet'+route,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-AO-Fleet':'1'},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)});const result=await r.json();if(!r.ok)throw Error(result.error||'Local safety checks rejected the request');$('fleet-control-result').textContent=description+' Request accepted; inspect the current fleet state.';selectedTarget=false;await refresh();}
  catch(e){$('fleet-control-result').textContent=e.name==='TimeoutError'?'Outcome unknown. Inspect current state before retrying; no automatic retry was sent.':e.message;}
  finally{pending=false;render();}
 }
 $('fleet-target').addEventListener('input',()=>{selectedTarget=true;});
 $('fleet-pause').onclick=()=>action('/api/master/pause',{},'Pause new automatic assignments without killing active turns?');
 $('fleet-resume').onclick=()=>action('/api/sustain/toggle',{enabled:true},'Enable Sustained dispatch under the existing quota and ownership safeguards?');
 $('fleet-save-target').onclick=()=>{const target=Number($('fleet-target').value);if(!Number.isInteger(target)||target<3||target>29){$('fleet-control-result').textContent='Choose a target from 3 to 29.';return;}action('/api/sustain/config',{targetWorking:target},'Set the target to '+target+' simultaneous working turns without creating or terminating agents?');};
 refresh();setInterval(()=>{if(!document.hidden)refresh();},8000);setInterval(render,5000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
})();
