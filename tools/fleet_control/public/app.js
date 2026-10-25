'use strict';
const $=(sel,root=document)=>root.querySelector(sel);
const $$=(sel,root=document)=>Array.from(root.querySelectorAll(sel));
const escapeHTML=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const esc=escapeHTML;
const modelName=id=>({'gpt-6-luna':'Luna','gpt-5.6-sol':'Sol 5.6','gpt-6-sol':'Sol 6','gpt-6.1-sol':'Sol 6.1','gpt-6-astra':'Astra'}[id]||id||'Unknown');
const className=id=>String(id||'').includes('astra')?'astra':String(id||'').includes('sol')?'sol':'luna';
const relativeTime=x=>{if(!x)return 'Unknown';const delta=(Date.now()-Date.parse(x))/1000;if(!Number.isFinite(delta))return 'Unknown';if(delta<0)return 'just now';if(delta<60)return Math.round(delta)+'s ago';if(delta<3600)return Math.round(delta/60)+'m ago';if(delta<86400)return Math.round(delta/3600)+'h ago';return Math.round(delta/86400)+'d ago'};
const state={data:null,selected:null,nav:'all',pending:false,firstLoad:true,drawerId:null,search:'',role:'all',model:'all',activity:'all'};
const fleetPrefix=location.pathname.startsWith('/fleet/')?'/fleet':'';
Object.assign(state,{refreshPromise:null,receivedAt:0,refreshError:null,drafts:{},agentPlans:[],agentPlansFresh:false,providerCatalog:null,providerCatalogFresh:false,preflight:null});
function rosterIsFresh(){return !!state.data&&!state.refreshError&&Date.now()-state.receivedAt<25000}
function executionUnavailable(){return !rosterIsFresh()||!!state.data?.offline||!state.preflight?.ready}
function canSavePlan(s,c){
 return s?.kind==='worker'&&!s.replacedBy&&c.valid&&!state.pending&&state.providerCatalogFresh===true&&state.agentPlansFresh===true&&!['applying','unverified'].includes(c.plan?.status);
}
function canApplyPlan(s,c){
 return s?.kind==='worker'&&!s.busy&&(s.isIdle||s.activity==='exited')&&!s.replacedBy&&!executionUnavailable()&&!state.pending&&state.providerCatalogFresh===true&&state.agentPlansFresh===true&&c.plan?.status==='pending'&&c.planValid;
}
function markFreshness(){
 const stale=!rosterIsFresh();
 if(stale&&state.data){count('#lastSync','STALE · last successful refresh '+relativeTime(new Date(state.receivedAt).toISOString()));const label=$('#connectionText');if(label)label.textContent='DATA STALE'}
 document.body.classList.toggle('roster-stale',stale);
 $$('button[data-action="nudge"],button[data-action="find-work"],button[data-action="replace"],button[data-action="apply-plan"],button[data-action="save-plan"]').forEach(b=>{
  const s=state.data?.sessions?.find(x=>x.id===b.dataset.id),action=b.dataset.action;
  if(action==='save-plan'||action==='apply-plan'){
   const c=s?planChoices(s):null;b.disabled=!c||!(action==='save-plan'?canSavePlan(s,c):canApplyPlan(s,c));
  }else b.disabled=!s||!s.isIdle||!!s.replacedBy||executionUnavailable()||state.pending;
  b.title=b.disabled?(state.pending?'Another control action is pending':'Current state or safety checks do not permit this action'):'';
 });
}
const limit=v=>Math.max(0,Math.min(100,Number(v)||0));
const count=(sel,text)=>$(sel).textContent=String(text);
const formatMemory=bytes=>(bytes/1073741824).toFixed(1)+' GB';
function toast(message,error=false){const x=document.createElement('div');x.className='toast'+(error?' error':'');x.textContent=message;$('#toastRoot').appendChild(x);setTimeout(()=>x.remove(),6200)}
async function api(path,data){
 const writing=data!==undefined&&data!==null,controller=new AbortController();
 const timeoutMs=writing?(/\/api\/(replace|agent-plan\/apply)$/.test(path)?90000:45000):10000;
 const timeout=setTimeout(()=>controller.abort(),timeoutMs);
 const options={method:writing?'POST':'GET',cache:'no-store',signal:controller.signal,headers:writing?{'Content-Type':'application/json','X-AO-Fleet':'1'}:{}};
 if(writing)options.body=JSON.stringify(data);
 try{
  const response=await fetch(fleetPrefix+path,options);
  let body;try{body=await response.json()}catch{throw Error('Unreadable response. '+(writing?'Action outcome is unknown; inspect state before retrying.':'Refresh again when connection recovers.'))}
  if(!response.ok)throw Error(body.error||'Request failed (HTTP '+response.status+')');
  return body;
 }catch(e){if(e.name==='AbortError')throw Error(writing?'Request timed out. The action may still be running; inspect its result before retrying. No automatic retry was sent.':'Refresh timed out; displayed data is stale.');throw e}
 finally{clearTimeout(timeout)}
}
function updateCounts(d){
 const q=d.summary,offline=!!d.offline;const connection=$('#connectionText');if(connection)connection.textContent=offline?'AO OFFLINE · SAVED ROSTER':'CONNECTED TO AO';
 count('#mTotal',q.total);count('#mWorking',q.working);count('#mIdle',q.idleFree);count('#mAttention',offline?'—':q.ciAttention);
 count('#navTotal',q.total);count('#navIdle',q.idle);count('#navWorking',q.working);count('#navAttention',offline?'—':q.ciAttention);
 count('#idleFoot',offline?'Orchestrator stopped · PR/CI status unavailable until restored':q.idlePending+' idle sessions also own pending PRs');
 count('#dTotal',q.total);count('#lWorking',q.working);count('#lReady',q.idleFree);
 count('#lPending',offline?'—':q.idlePending);count('#lOther',Math.max(0,q.total-q.working-q.idleFree-q.idlePending));
 const denom=Math.max(1,q.total),wp=q.working/denom*100,rp=q.idleFree/denom*100,pp=q.idlePending/denom*100;
 $('#donut').style.background='conic-gradient(#5bd1c5 0 '+wp+'%, #8cacc1 '+wp+'% '+(wp+rp)+'%, #e8c37c '+(wp+rp)+'% '+(wp+rp+pp)+'%, #415265 '+(wp+rp+pp)+'% 100%)';
 count('#cpuValue',d.metrics.cpu+'%');$('#cpuBar').style.width=limit(d.metrics.cpu)+'%';
 const ramPct=limit((d.metrics.ramTotal-d.metrics.ramFree)/d.metrics.ramTotal*100);
 count('#ramValue',Math.round(ramPct)+'%');$('#ramBar').style.width=ramPct+'%';
 count('#memoryDetail',formatMemory(d.metrics.ramTotal-d.metrics.ramFree)+' used of '+formatMemory(d.metrics.ramTotal));
 count('#cores',d.metrics.logicalCores+' logical cores');
 count('#lastSync',offline?'OFFLINE · saved roster '+(d.snapshotAt?new Date(d.snapshotAt).toLocaleTimeString():'timestamp unknown'):'Synced '+new Date(d.timestamp).toLocaleTimeString());
}
function setOptions(id,values,titleFn){
 const sel=$(id),last=sel.value;
 sel.innerHTML='<option value="all">'+(id==='#roleFilter'?'All roles':'All models')+'</option>'+values.map(x=>'<option value="'+esc(x)+'">'+esc(titleFn?titleFn(x):x)+'</option>').join('');
 sel.value=values.includes(last)?last:'all';
}
function updateFilters(d){
 const roles=[...new Set(d.sessions.map(s=>s.role))].sort();
 const models=[...new Set(d.sessions.map(s=>s.model).filter(Boolean))].sort();
 setOptions('#roleFilter',roles);setOptions('#modelFilter',models,modelName);
}
function statusClass(s){if(s.busy)return 'working';if(s.blocked)return 'attention';if(s.openPRs)return 'pending';return 'idle'}
function filtered(){
 if(!state.data)return[];
 const search=state.search.trim().toLowerCase();
 let arr=state.data.sessions.filter(s=>{
  const nav=state.nav;
  if(nav==='idle'&&!s.isIdle)return false;
  if(nav==='working'&&!s.busy)return false;
  if(nav==='attention'&&!s.blocked)return false;
  if(nav==='review'&&!s.role.toLowerCase().includes('review'))return false;
  if(state.activity==='idle'&&!s.isIdle)return false;
  if(state.activity==='working'&&!s.busy)return false;
  if(state.activity==='attention'&&!s.blocked)return false;
  if(state.activity==='pending'&&!s.openPRs)return false;
  if(state.role!=='all'&&state.role!==s.role)return false;
  if(state.model!=='all'&&state.model!==s.model)return false;
  if(search&&!([s.name,s.id,s.model,s.role,s.status,s.branch].join(' ').toLowerCase().includes(search)))return false;
  return true;
 });
 return arr.sort((a,b)=>{
  const rank=x=>x.blocked?0:x.busy?1:x.openPRs?2:x.isIdle?3:4;
  return rank(a)-rank(b)||a.id.localeCompare(b.id,undefined,{numeric:true});
 });
}
function renderTable(){
 const arr=filtered(),tbody=$('#tableRows');
 count('#rosterCount','/ '+arr.length+' shown');
 count('#tableCaption',state.data?.offline?'OFFLINE / CONFIGURATION AVAILABLE · '+arr.length+' of '+state.data.sessions.length+' preserved agents · PR/CI status is not live':arr.length+' of '+(state.data?.sessions.length||0)+' agents · Refresh every 8s');
 if(!arr.length){tbody.innerHTML='<tr><td colspan="7" class="loading-cell">No agents match these filters.</td></tr>';return}
 tbody.innerHTML=arr.map(s=>{
  const cs=statusClass(s);
  const badge=s.model?'<span class="model-chip '+className(s.model)+'">'+esc(modelName(s.model))+'</span>':'<span class="model-chip">Provisioning</span>';
  const prText=state.data?.offline?'<span class="pr-info">Not live · check GitHub</span>':s.prs.length?'<div class="pr-inline"><span class="'+(s.blocked?'mini-alert':'pr-info')+'">'+s.openPRs+' open</span> '+(s.blocked?'<span class="mini-alert">⚑ failing CI</span>':'<span class="pr-info">'+s.prs.length+' tracked</span>')+'</div>':'<span class="pr-info">—</span>';
  const sub=s.replacedBy?'<small>Replaced by '+esc(s.replacedBy)+'</small>':'<small>'+esc(s.id)+'</small>';
  return '<tr><td><div class="agent-id"><span class="agent-glyph '+className(s.model)+'">'+(className(s.model)==='astra'?'A':className(s.model)==='sol'?'S':'L')+'</span><div title="'+esc(s.name)+'"><strong>'+esc(s.name)+'</strong>'+sub+'</div></div></td><td class="role-text">'+esc(s.role)+'</td><td>'+badge+'</td><td><span class="state-chip '+cs+'"><i class="state-dot"></i>'+esc(s.stateLabel)+'</span></td><td>'+prText+'</td><td>'+esc(relativeTime(s.lastActivity))+'</td><td class="right"><div class="row-actions"><button class="small-action find-work-row" data-action="find-work" data-id="'+esc(s.id)+'" '+(s.isIdle&&!s.replacedBy?'':'disabled title="Only idle agents can find work"')+'>Find Work</button><button class="small-action" data-action="configure" data-id="'+esc(s.id)+'">Change model</button><button class="small-action accent" data-action="view" data-id="'+esc(s.id)+'">Manage →</button></div></td></tr>';
 }).join('');
 if(state.data?.offline)tbody.querySelectorAll('button[data-action="find-work"]').forEach(button=>{button.disabled=true;button.title='Agents are stopped. Configuration plans remain available through Manage.'});
 markFreshness();
}
function captureDrawerDraft(){
 if(!state.drawerId||$('#overlay').hidden)return;
 const draft=state.drafts[state.drawerId]||{};
 for(const [key,selector] of Object.entries({harness:'#changeProvider',model:'#changeModel',effort:'#changeEffort',mode:'#changeMode',message:'#customMessage'})){const el=$(selector);if(el)draft[key]=el.value}
 state.drafts[state.drawerId]=draft;
}
function planChoices(s){
 const plan=state.agentPlans.find(p=>p.id===s.id),draft=state.drafts[s.id]||{};
 const providers=state.providerCatalog?.providers||[];
 const harness=draft.harness||plan?.harness||s.harness||'codex';
 const knownProvider=providers.find(p=>p.id===harness);
 const provider=knownProvider||{id:harness,name:harness+' (unavailable)',available:false,models:[]},models=provider.models||[];
 const wantedModel=Object.hasOwn(draft,'model')?draft.model:(plan?.model||s.model);
 const model=wantedModel?(models.find(m=>m.id===wantedModel)||{id:wantedModel,name:wantedModel+' (unavailable)',efforts:[],unavailable:true}):models[0];
 const supported=model?.efforts||[],wantedEffort=Object.hasOwn(draft,'effort')?draft.effort:(plan?.effort||model?.defaultEffort);
 const effort=wantedEffort||model?.defaultEffort||supported[0]||'default';
 const efforts=supported.includes(effort)?supported:[...supported,effort];
 const mode=draft.mode||plan?.mode||s.mode||'tui';
 const valid=provider.available===true&&!!model&&!model.unavailable&&supported.includes(effort)&&['tui','chat'].includes(mode);
 const savedProvider=providers.find(p=>p.id===plan?.harness),savedModel=savedProvider?.models?.find(m=>m.id===plan?.model);
 const planValid=!!(savedProvider?.available&&savedModel?.efforts?.includes(plan?.effort)&&['tui','chat'].includes(plan?.mode));
 return {plan,draft,providers:knownProvider?providers:[provider,...providers],provider,models:model?.unavailable?[model,...models]:models,model,efforts,effort,mode,valid,planValid};
}
function showDrawer(id,preserve=true){
 if(preserve)captureDrawerDraft();
 const s=state.data?.sessions.find(x=>x.id===id);
 if(!s){toast('Agent is no longer in the roster.',true);return}
 state.selected=id;state.drawerId=id;
 const c=planChoices(s),offline=!!state.data.offline;
 const canSave=canSavePlan(s,c);
 const eligible=s.kind==='worker'&&!s.busy&&(s.isIdle||s.activity==='exited')&&!s.replacedBy;
 const canApply=canApplyPlan(s,c);
 const canNudge=s.isIdle&&!s.replacedBy&&!executionUnavailable()&&!state.pending;
 const option=(v,l,s,d=false)=>'<option value="'+esc(v)+'"'+(s?' selected':'')+(d?' disabled':'')+'>'+esc(l)+'</option>';
 const providers=c.providers.map(p=>option(p.id,p.name||p.id,c.provider?.id===p.id,p.available===false)).join('');
 const models=c.models.map(m=>option(m.id,m.name||m.id,c.model?.id===m.id,m.unavailable===true)).join(''),efforts=c.efforts.map(e=>option(e,e,c.effort===e,!c.model?.efforts?.includes(e))).join('');
 const planStatus=c.plan?'Saved plan: '+c.plan.harness+' · '+c.plan.model+' · '+c.plan.effort+' · '+c.plan.status+' ('+relativeTime(c.plan.updatedAt)+')'+(state.agentPlansFresh===false?' · STATUS STALE':''):'No saved plan. Save first; applying is a separate confirmed action.';
 const reason=!state.providerCatalogFresh?'Provider catalogue is stale. Draft selections are preserved; wait for a fresh catalogue.':!c.valid?'Selected provider, model or reasoning is unavailable. Choose a supported option; no substitute has been selected.':state.pending?'A control action is pending.':s.kind!=='worker'?'Primary orchestrator changes require a controlled handoff; worker plans only.':s.replacedBy?'This original session already has a replacement.':offline?'Orchestrator is offline. Save a plan now; apply after preflight is ready.':executionUnavailable()?'Launch is blocked by safety preflight. Settings can still be saved.':!eligible?'Wait until the agent is idle or exited; its existing PRs and worktree will be preserved.':'Applying parks the original work safely and creates an isolated replacement under primary assignment.';
 const prs=offline?'<p class="helper">PR and CI data is not live while Orchestrator is offline.</p>':s.prs.length?s.prs.map(p=>{const url=/^https:\/\/github\.com\/[^?#]+$/.test(p.url||'')?p.url:null;return '<div class="pr-item"><span>'+(url?'<a href="'+esc(url)+'" target="_blank" rel="noreferrer noopener">PR #'+esc(p.number)+' ↗</a>':'PR #'+esc(p.number))+'</span><span>'+esc(p.state||'?')+' · '+esc(p.ci||'not checked')+'</span></div>'}).join(''):'<p class="helper">No PRs tracked for this session.</p>';
 $('#drawerBody').innerHTML=
 '<div class="drawer-name">'+esc(s.name)+'</div><div class="drawer-id">'+esc(s.id)+' · '+esc(s.kind)+' · '+esc(s.harness||'codex')+' · '+esc(s.mode||'native')+'</div>'+
 '<div class="drawer-state"><span class="state-chip '+statusClass(s)+'"><i class="state-dot"></i>'+esc(s.stateLabel)+'</span><span class="small-muted">'+esc(relativeTime(s.lastActivity))+'</span></div>'+
 '<div class="drawer-meta"><div class="meta-box"><label>Current model</label><strong>'+esc(modelName(s.model))+'</strong></div><div class="meta-box"><label>Running reasoning</label><strong>Not exposed by AO API</strong></div><div class="meta-box"><label>Role</label><strong>'+esc(s.role)+'</strong></div><div class="meta-box"><label>Git branch</label><strong>'+esc(s.branch||'—')+'</strong></div></div>'+
 '<section class="drawer-section"><h3>Wake & delegate</h3><p class="helper">Nudges use provider quota. Stale data, a safety lock or a running turn blocks delivery.</p><div class="control-row"><button class="ghost-button" data-action="nudge" data-type="progress" data-id="'+esc(id)+'" '+(!canNudge?'disabled':'')+'>Ask for status</button><button class="primary-button" data-action="find-work" data-id="'+esc(id)+'" '+(!canNudge?'disabled':'')+'>Find Work Now ↗</button></div>'+
 '<div class="form-field"><label for="customMessage">Custom instruction</label><textarea id="customMessage" maxlength="2400" placeholder="Describe a bounded task or status request…">'+esc(c.draft.message||'')+'</textarea></div><button class="ghost-button full" data-action="nudge" data-type="custom" data-id="'+esc(id)+'" '+(!canNudge?'disabled':'')+'>Send custom instruction</button><div class="drawer-action-note">Acceptance is not proof of completed work. Timed-out actions are not automatically retried.</div></section>'+
 '<section class="drawer-section"><h3>Provider, model & reasoning</h3><p class="helper">Save a future configuration even while stopped. Apply creates a verified replacement in a new worktree and preserves the original.</p>'+
 '<div class="form-field"><label for="changeProvider">Agent harness</label><select id="changeProvider">'+providers+'</select></div><div class="form-field"><label for="changeModel">New model</label><select id="changeModel">'+models+'</select></div><div class="form-field"><label for="changeEffort">Reasoning for new session</label><select id="changeEffort">'+efforts+'</select></div>'+
 '<p class="helper">'+(c.model?.effortMode==='model-id'?'AGY reasoning is encoded in the selected model ID. No unsupported effort flag is sent.':'Only validated reasoning choices are listed.')+'</p>'+
 '<div class="form-field"><label for="changeMode">Interface mode</label><select id="changeMode">'+option('chat','Chat',c.mode==='chat')+option('tui','Terminal',c.mode==='tui')+'</select></div>'+
 '<button class="primary-button full" data-action="save-plan" data-id="'+esc(id)+'" '+(!canSave?'disabled':'')+'>Save settings for next launch</button><div class="drawer-action-note">'+esc(planStatus)+'</div>'+
 '<button class="ghost-button full" data-action="apply-plan" data-id="'+esc(id)+'" '+(!canApply?'disabled':'')+'>Apply saved plan…</button><div class="drawer-action-note">'+esc(reason)+'</div>'+
 (s.replacedBy?'<div class="replacement-banner">Replacement: '+esc(s.replacedBy)+'. Original preserved.</div><button class="danger-button full" data-action="retire" data-id="'+esc(id)+'" '+(offline||state.pending?'disabled':'')+'>Archive original session…</button>':'')+
 '</section><section class="drawer-section"><h3>Tracked pull requests</h3>'+prs+'</section>';
 $('#overlay').hidden=false;document.body.style.overflow='hidden';markFreshness();
}
function closeDrawer(){captureDrawerDraft();state.selected=null;state.drawerId=null;$('#overlay').hidden=true;document.body.style.overflow='';}
function closeModal(){$('#modalOverlay').hidden=true;$('#modalBody').innerHTML=''}
function openModal(html){$('#modalBody').innerHTML=html;$('#modalOverlay').hidden=false}
async function takeAction(action,id,type){
 const s=state.data.sessions.find(x=>x.id===id);if(!s){toast('Agent not found',true);return}
 if(state.pending){toast('A control action is already in progress.');return}
 if(['nudge','replace','apply-plan'].includes(action)&&executionUnavailable()){toast('Safety preflight or a fresh roster is required to start work. Settings can still be saved.',true);return}
 if(action==='save-plan'){
  captureDrawerDraft();if(!canSavePlan(s,planChoices(s))){toast('Refresh the provider catalogue and saved-plan state, then select a supported configuration.',true);return}const draft=state.drafts[id];if(!draft?.model||!draft.harness||!draft.effort){toast('Select a supported provider, model and reasoning level.',true);return}
  try{state.pending=true;markFreshness();const r=await api('/api/agent-plan',{id,harness:draft.harness,model:draft.model,effort:draft.effort,mode:draft.mode});if(r.plan)state.agentPlans=[...state.agentPlans.filter(p=>p.id!==id),r.plan];toast(r.note||'Agent settings saved. No agent started.');await refresh(true)}
  catch(e){toast(e.message,true)}finally{state.pending=false;if(state.drawerId===id)showDrawer(id)}
  return;
 }
 if(action==='apply-plan'){
  const plan=state.agentPlans.find(p=>p.id===id);if(!canApplyPlan(s,planChoices(s))){toast('Refresh and save a pending plan first.',true);return}
  openModal('<h2>Apply saved agent plan?</h2><p>Saved settings: <strong>'+esc(plan.harness)+' · '+esc(plan.model)+' · '+esc(plan.effort)+'</strong>. This starts a replacement and may consume provider usage. Existing work and PRs stay in the original worktree. Unsaved dropdown changes are not applied.</p><div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="confirmApplyPlan">Apply saved plan</button></div>');
  $('#confirmApplyPlan').addEventListener('click',async()=>{
   if(state.pending)return;state.pending=true;$('#confirmApplyPlan').disabled=true;$('#confirmApplyPlan').textContent='Applying…';markFreshness();
   try{const r=await api('/api/agent-plan/apply',{id,confirm:'APPLY AGENT PLAN'});closeModal();toast(r.note||'Apply request completed. Inspect the replacement.');await refresh(true)}
   catch(e){toast(e.message,true);closeModal()}finally{state.pending=false;if(state.drawerId===id)showDrawer(id)}
  });return;
 }
 if(action==='nudge'){
  const message=type==='custom'?$('#customMessage')?.value||'':null;
  if(type==='custom'&&message.trim().length<10){toast('Custom instructions need at least 10 characters.',true);return}
  try{state.pending=true;const r=await api('/api/nudge',{id,type,message});toast('Nudge accepted for '+id+'. Check its live activity for progress.');await refresh(true)}
  catch(e){toast(e.message,true)}finally{state.pending=false}
 }
 if(action==='replace'){
  const model=$('#changeModel')?.value,effort=$('#changeEffort')?.value;
  if(!model||!effort)return;
  openModal('<h2>Replace '+esc(s.id)+'?</h2><p>This creates a <strong>'+esc(modelName(model))+' · '+esc(effort)+'</strong> agent with its own worktree. The existing session remains intact and may be retired separately after verification. This is not an in-place model switch.</p><div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="primary-button" id="confirmReplace">Create replacement</button></div>');
  $('#confirmReplace').addEventListener('click',async()=>{
   if(state.pending)return;state.pending=true;$('#confirmReplace').disabled=true;$('#confirmReplace').textContent='Creating…';
   try{const r=await api('/api/replace',{id,model,effort});closeModal();toast('Replacement '+r.newId+' is live. Original '+r.oldId+' preserved.');await refresh(true);showDrawer(id)}
   catch(e){toast(e.message,true);closeModal()}finally{state.pending=false}
  });
 }
 if(action==='retire'){
  openModal('<h2>Archive original session?</h2><p>Only archive after verifying the replacement is ready. The original agent history will be backed up and the AO session terminated. Branches and worktrees are not cleaned up.</p><div class="form-field"><label>Type <strong>'+esc(id)+'</strong> to confirm</label><input id="retireConfirm" autocomplete="off" placeholder="'+esc(id)+'"></div><div class="modal-actions"><button class="ghost-button" data-modal="cancel">Cancel</button><button class="danger-button" id="confirmRetire">Archive old agent</button></div>');
  $('#confirmRetire').addEventListener('click',async()=>{
   if(state.pending)return;state.pending=true;$('#confirmRetire').disabled=true;
   try{const confirm=$('#retireConfirm').value;const r=await api('/api/retire',{id,confirm});closeModal();closeDrawer();toast(id+' archived. Replacement remains live.');await refresh(true)}
   catch(e){toast(e.message,true);$('#confirmRetire').disabled=false}finally{state.pending=false}
  });
 }
}
function guide(){
 openModal('<h2>Fleet controls</h2><p><strong>Working</strong> means an agent is executing a turn. <strong>Idle · available</strong> means the agent has no active turn and no tracked open PR or failing CI. <strong>Idle · PR pending</strong> means it owns work but is waiting for the next instruction, validation, or review.</p><p><strong>Nudge:</strong> send a status request or ask the agent to request another bounded task from the Sol primary. Nudge is never automated: it consumes model usage.</p><p><strong>Model / reasoning:</strong> choose a new model and effort, then create a replacement session. You can archive the old session separately once the new agent is verified. Models cannot be changed in place in this build.</p><p><strong>Security:</strong> this dashboard binds to 127.0.0.1 only, and no daemon control endpoint is exposed to the network.</p><div class="modal-actions"><button class="primary-button" data-modal="cancel">Got it</button></div>');
}
function renderReadiness(){
 let panel=$('#providerReadiness');if(!panel){panel=document.createElement('section');panel.id='providerReadiness';panel.className='drawer-section provider-readiness';const anchor=$('#quotaSection');if(!anchor)return;anchor.insertAdjacentElement('afterend',panel)}
 const p=state.preflight,issues=p?.issues||[],usage=state.providerUsage?.providers||[];
 panel.innerHTML='<h2>Launch readiness & provider usage</h2><p class="helper">'+(p?.ready?'Preflight ready. Starting work requires an explicit action.':p?'Launch is blocked until these checks are resolved.':'Preflight unavailable. Launch controls remain blocked.')+'</p>'+
 (issues.length?'<ul>'+issues.map(i=>'<li>'+esc(i.message||i.code||i)+'</li>').join('')+'</ul>':'')+
 '<div class="drawer-meta">'+usage.map(u=>'<div class="meta-box"><label>'+esc(u.name||u.id)+'</label><strong>'+(Number.isFinite(u.remainingPercent)?esc(u.remainingPercent)+'% lowest reported bucket':'Not available')+'</strong><p class="helper">'+esc(u.reason||u.warning||u.source||u.status||'No verified data')+'</p>'+
 (u.windows||[]).map(w=>'<p class="helper">'+esc(w.label||w.bucket)+': '+(w.disabled?'disabled':Number.isFinite(w.remainingPercent)?esc(w.remainingPercent)+'%':Number.isFinite(w.remainingAmount)?esc(w.remainingAmount)+' remaining':'unknown')+(u.status==='stale'?' · STALE':'')+(w.resetsAt?' · resets '+esc(new Date(w.resetsAt).toLocaleString()):'')+'</p>').join('')+
 '<p class="helper">'+(u.observedAt?'Observed '+esc(relativeTime(u.observedAt)):'Awaiting native observation')+'</p></div>').join('')+'</div><p class="helper">AGY and Gemini CLI are separate sources. Missing quota is unknown. Refreshing this panel sends no model turns. Google percentage auto-stop is not implemented.</p>';
}
async function refresh(force=false){
 if(state.refreshPromise)return state.refreshPromise;
 state.refreshPromise=(async()=>{
  try{
   const [roster,plans,preflight,providers,usage]=await Promise.allSettled([api('/api/state'),api('/api/agent-plan'),api('/api/preflight'),api('/api/providers'),api('/api/gemini-usage')]);
   state.preflight=preflight.status==='fulfilled'?preflight.value:null;
   state.agentPlansFresh=plans.status==='fulfilled';
   if(state.agentPlansFresh)state.agentPlans=Array.isArray(plans.value.plans)?plans.value.plans:[];
   state.providerCatalogFresh=providers.status==='fulfilled';
   if(state.providerCatalogFresh)state.providerCatalog=providers.value;
   state.providerUsage=usage.status==='fulfilled'?usage.value:null;
   renderReadiness();
   if(roster.status!=='fulfilled')throw roster.reason;
   const data=roster.value;state.data=data;state.receivedAt=Date.now();state.refreshError=null;updateCounts(data);
   if(state.firstLoad){state.firstLoad=false;updateFilters(data)}else{
    const oldRoles=[...$('#roleFilter').options].slice(1).map(o=>o.value),newRoles=[...new Set(data.sessions.map(s=>s.role))].sort();
    const oldModels=[...$('#modelFilter').options].slice(1).map(o=>o.value),newModels=[...new Set(data.sessions.map(s=>s.model).filter(Boolean))].sort();
    if(JSON.stringify(oldRoles)!==JSON.stringify(newRoles)||JSON.stringify(oldModels)!==JSON.stringify(newModels))updateFilters(data);
   }
   renderTable();
   const editing=typeof document!=='undefined'&&document.activeElement&&['SELECT','TEXTAREA','INPUT'].includes(document.activeElement.tagName);
   if(state.drawerId&&!state.pending&&!editing)showDrawer(state.drawerId);
   if(typeof FleetFindWorkUI!=='undefined')FleetFindWorkUI.updateCounter(data);
  }catch(e){
   state.refreshError=e.message||String(e);count('#lastSync','STALE · '+state.refreshError);if(!state.data)$('#tableRows').innerHTML='<tr><td colspan="7" class="loading-cell">Unable to connect. '+esc(state.refreshError)+'</td></tr>';
   if(force)toast(state.refreshError,true);
  }finally{state.refreshPromise=null;markFreshness()}
 })();return state.refreshPromise;
}
document.addEventListener('click',e=>{
 const action=e.target.closest('[data-action]');if(action){if(action.disabled)return;const name=action.dataset.action,id=action.dataset.id;if(name==='view')showDrawer(id);else if(name==='find-work'){if(typeof FleetFindWorkUI!=='undefined')FleetFindWorkUI.runOne(id);else toast('Find Work controls are loading; refresh the page.',true)}else if(name==='configure'){showDrawer(id);setTimeout(()=>$('#changeModel')?.scrollIntoView({behavior:'smooth',block:'center'}),100)}else takeAction(name,id,action.dataset.type);return}
 if(e.target.closest('[data-modal="cancel"]'))closeModal();
 const nav=e.target.closest('[data-nav]');if(nav){
  state.nav=nav.dataset.nav;$$('[data-nav]').forEach(x=>x.classList.toggle('active',x===nav));
  renderTable();document.querySelector('.roster-section').scrollIntoView({behavior:'smooth',block:'start'});
 }
});
$('#closeDrawer').addEventListener('click',closeDrawer);
$('#closeDrawerBackdrop').addEventListener('click',closeDrawer);
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeDrawer()}});
$('#refreshBtn').addEventListener('click',()=>refresh(true));
$('#refreshRoster').addEventListener('click',()=>refresh(true));
$('#showGuide').addEventListener('click',guide);
$('#showIdleBtn').addEventListener('click',()=>{state.nav='idle';$$('[data-nav]').forEach(x=>x.classList.toggle('active',x.dataset.nav==='idle'));renderTable();document.querySelector('.roster-section').scrollIntoView({behavior:'smooth',block:'start'})});
$('#search').addEventListener('input',e=>{state.search=e.target.value;renderTable()});
$('#roleFilter').addEventListener('change',e=>{state.role=e.target.value;renderTable()});
$('#modelFilter').addEventListener('change',e=>{state.model=e.target.value;renderTable()});
$('#activityFilter').addEventListener('change',e=>{state.activity=e.target.value;renderTable()});
document.addEventListener('change',e=>{if(!state.drawerId||!['changeProvider','changeModel','changeEffort','changeMode'].includes(e.target.id))return;captureDrawerDraft();const d=state.drafts[state.drawerId];if(e.target.id==='changeProvider'){d.model='';d.effort=''}else if(e.target.id==='changeModel')d.effort='';showDrawer(state.drawerId,false)});
refresh();setInterval(()=>{if(!document.hidden)refresh()},8000);setInterval(markFreshness,1000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh(true)});
window.addEventListener('focus',()=>refresh());
