'use strict';
// Operational projection only: never export prompts, command lines, private paths,
// provider conversation IDs, tokens, raw errors or terminal/history contents.
const clean=(v,n=180)=>typeof v==='string'?v.replace(/[\x00-\x1f\x7f]/g,'').slice(0,n):null;
const iso=v=>typeof v==='string'&&Number.isFinite(Date.parse(v))?new Date(v).toISOString():null;
const count=v=>Number.isSafeInteger(v)&&v>=0?v:null;
const positive=v=>Number.isSafeInteger(v)&&v>0;
const fresh=(v,now)=>Number.isFinite(Date.parse(v||''))&&now-Date.parse(v)>=-10000&&now-Date.parse(v)<=45000;
const relativePath=v=>typeof v==='string'&&/^(tests|docs|apps|services|packages|tools|planning)\/[a-zA-Z0-9_./-]{1,240}$/.test(v)&&!v.split('/').includes('..')?v:null;
const knownAction=new Set(['issue','review','pr','ci','root','coord','refill','nudge','status','find-work']);
function operationalDetails(session,sustain){
 const state=sustain?.status||{},agent=state.agents?.[session.id]||{};
 return {
  assignments:Object.values(state.leases||{}).filter(x=>x.session===session.id).slice(0,12).map(x=>({issue:positive(x.issue)?x.issue:null,state:clean(x.state,40),at:iso(x.at),scope:(Array.isArray(x.scope)?x.scope:[]).map(relativePath).filter(Boolean).slice(0,12)})),
  recentActions:(Array.isArray(state.dispatches)?state.dispatches:[]).filter(x=>x.id===session.id).slice(-8).reverse().map(x=>({at:iso(x.at),type:knownAction.has(x.type)?x.type:'instruction',status:['submitted','uncertain','reserved'].includes(x.status)?x.status:'recorded',issue:positive(x.issue)?x.issue:null})),
  dispatchObservation:{lastSent:iso(agent.lastSent),completedAt:iso(agent.completedAt),awaiting:agent.awaiting===true,noProgressCount:count(agent.noProgress)||0},
 };
}
function projectAgent(s,{live=false,readiness={},now=Date.now()}={}){
 const mode=['tui','chat'].includes(s.mode)?s.mode:null;
 const observed=live&&!s.isTerminated&&!['exited','stopped'].includes(s.activity);
 const readinessFresh=fresh(readiness.at,now)&&readiness.inspectionPending!==true&&readiness.stale!==true;
 const runtimeVerified=observed&&readinessFresh&&Array.isArray(readiness.healthyIds)&&readiness.healthyIds.includes(s.id);
 const kind=mode==='tui'?'Terminal (TUI)':mode==='chat'?'Chat controller':'Interface not reported';
 let runtimeStatus=!live?'Offline / saved':!observed?'Stopped':runtimeVerified?'Verified':readinessFresh?'Not verified':'Verification unavailable';
 const controllerState=mode==='chat'&&live?clean(s.controllerState,50):null;
 const controllerLabel=mode==='tui'?'Terminal · '+runtimeStatus.toLowerCase():mode==='chat'?'Chat · '+(controllerState||'not reported'):runtimeStatus;
 return {
  id:clean(s.id,128),name:clean(s.name),role:clean(s.role),harness:clean(s.harness,60),model:clean(s.model,120),mode,
  activity:clean(s.activity,50),status:clean(s.status,60),stateLabel:clean(s.stateLabel,160),
  controllerState,controllerVerified:mode==='chat'&&live?typeof s.controllerVerified==='boolean'?s.controllerVerified:null:null,
  controllerLabel,controlKind:kind,runtimeVerified,runtimeStatus,runtimeObservedAt:readinessFresh?iso(readiness.at):null,
  busy:observed&&s.busy===true,idle:observed&&(s.isIdle===true||s.idle===true),openPRs:count(s.openPRs),lastActivity:iso(s.lastActivity),createdAt:iso(s.createdAt),
  branch:typeof s.branch==='string'&&/^[a-zA-Z0-9_./-]{1,220}$/.test(s.branch)?s.branch:null,
  prs:(Array.isArray(s.prs)?s.prs:[]).filter(p=>positive(p.number)).slice(0,20).map(p=>({number:p.number,url:'https://github.com/MulattoTech/MJobs/pull/'+p.number,state:clean(p.state,40),ci:clean(p.ci,40),review:clean(p.review,40),mergeability:clean(p.mergeability,40)})),
  assignments:(Array.isArray(s.assignments)?s.assignments:[]).slice(0,12).map(x=>({issue:positive(x.issue)?x.issue:null,state:clean(x.state,40),at:iso(x.at),scope:(Array.isArray(x.scope)?x.scope:[]).map(relativePath).filter(Boolean).slice(0,12)})),
  recentActions:(Array.isArray(s.recentActions)?s.recentActions:[]).slice(0,8).map(x=>({at:iso(x.at),type:knownAction.has(x.type)?x.type:'instruction',status:['submitted','uncertain','reserved'].includes(x.status)?x.status:'recorded',issue:positive(x.issue)?x.issue:null})),
  dispatchObservation:{lastSent:iso(s.dispatchObservation?.lastSent),completedAt:iso(s.dispatchObservation?.completedAt),awaiting:s.dispatchObservation?.awaiting===true,noProgressCount:count(s.dispatchObservation?.noProgressCount)||0},
 };
}
function providerOverview(sessions,lanes,{fleetAvailable=false,missionAvailable=false}={}){
 if(!fleetAvailable&&!missionAvailable)return [];
 const mapping={codex:['codex'],antigravity:['agy','gemini','antigravity'],ollama:['ollama'],claude:['claude','claude-code']};
 return Object.entries(mapping).map(([id,harnesses])=>{
  const own=lanes.find(x=>x.id===id)||{},agents=sessions.filter(s=>harnesses.includes(s.harness)&&!['exited','stopped'].includes(s.activity));
  const working=agents.filter(x=>x.busy).length,verified=agents.filter(x=>x.runtimeVerified).length;
  const independentRunning=missionAvailable&&['RUNNING','WORKING','STARTING','WAITING_INPUT'].includes(String(own.status).toUpperCase());
  const status=fleetAvailable&&agents.length?(working?'WORKING':'IDLE'):independentRunning?'RUNNING':fleetAvailable&&missionAvailable?'NO MANAGED AGENTS':'UNKNOWN';
  return {id,model:agents.length?[...new Set(agents.map(s=>s.model).filter(Boolean))].join(', '):own.model||null,effort:own.effort||null,status,activity:status,
   managedBy:agents.length?'AO Fleet':independentRunning?'Mission Control':'AO + Mission Control',agents:agents.length,working,verified,
   independentStatus:missionAvailable?own.status||'Not configured':'Unavailable',
   detail:agents.length?`${agents.length} AO runtimes · ${working} working · ${verified} verified; independent launcher: ${own.status||'unavailable'}`:independentRunning?'Independent Mission Control process is running':'No running agent in the managed fleet or launcher. Unmanaged desktop apps are not claimed stopped.',
   startedAt:own.startedAt||null,stoppedAt:own.stoppedAt||null};
 });
}
module.exports={operationalDetails,projectAgent,providerOverview};
