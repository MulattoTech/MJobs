'use strict';
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const { Turbo } = require('./turbo');
const { Sustain } = require('./sustain');
const { MasterControls } = require('./master-controls');
const { StartupController } = require('./startup-control');
const { StartupAdapter } = require('./startup-adapter');
const { completeReviewedRestore } = require('./post-review-control');
const { OrchestratorLauncher } = require('./orchestrator-launcher');
const { runtimeHealthy, addControllerHealth, controllerStopped } = require('./runtime-health');
const { QuotaControl } = require('./quota-control');
const { FindWork } = require('./find-work');
const { DispatchLedger } = require('./dispatch-ledger');
const { operationalDetails } = require('./agent-observability');
const { AgentPlans } = require('./agent-plans');
const { preflight } = require('./preflight');
const { createProviderRuntime, validateSelection } = require('./provider-catalog');
const { readGeminiUsage } = require('./gemini-usage');
const { randomUUID } = require('node:crypto');
const {completeKnownPaste}=require('./terminal-delivery');
const {NativeDeliveryReceipts}=require('./native-delivery-receipts');
const deliveryReceipts=new NativeDeliveryReceipts();
const {DeliveryReconciliation}=require('./delivery-reconciliation');
const receiptReconciliation=new DeliveryReconciliation(__dirname);
const exec = promisify(execFile);
const root = __dirname;
const publicRoot = path.join(root, 'public');
const AO = path.join(os.homedir(),'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe');
const CODEX_EXE = path.join(process.env.APPDATA || path.join(os.homedir(),'AppData','Roaming'),'npm','node_modules','@openai','codex','node_modules','@openai','codex-win32-x64','vendor','x86_64-pc-windows-msvc','bin','codex.exe');
async function verifyCodexExecutable(){
 if(!fs.existsSync(CODEX_EXE))throw Error('Codex Windows executable is missing. Repair the npm @openai/codex Windows binary before replacing agents; the original agent was preserved.');
 try{
  const result=await exec(CODEX_EXE,['--version'],{timeout:8000,windowsHide:true,maxBuffer:10000});
  if(!/codex-cli\s+\d+\.\d+\.\d+/i.test(result.stdout))throw Error('Unexpected version output');
  return result.stdout.trim();
 }catch(e){throw Error('Codex Windows binary exists but cannot launch: '+String(e.code||e.message).slice(0,130)+'. Original agent preserved.')}
}
const statePath = path.join(root, 'replacement-state.json');
const auditPath = path.join(root, 'audit.jsonl');
const listenHost = '127.0.0.1';
const port = Number(process.env.FLEET_DASHBOARD_PORT || 8787);
const projectId = 'mjobs-luna';
const MODEL_OPTIONS = [
  {id:'gpt-6-luna',name:'GPT-6 Luna'},
  {id:'gpt-5.6-sol',name:'GPT-5.6 Sol'},
  {id:'gpt-6-sol',name:'GPT-6 Sol'},
  {id:'gpt-6.1-sol',name:'GPT-6.1 Sol'},
  {id:'gpt-6-astra',name:'GPT-6 Astra'}
];
const EFFORT_OPTIONS=['low','medium','high','xhigh'];
const providerRuntime=createProviderRuntime({codexModels:MODEL_OPTIONS,codexEfforts:EFFORT_OPTIONS,codexExe:CODEX_EXE});
let mutationLock=false;
let dispatchLedger=null;
let agentPlans=null;
let stateInFlight=null;
let turbo=null;
let sustain=null;
let master=null;
let quota=null;
let findWork=null;
let startup=null, startupAdapter=null, orchestratorLauncher=null;
let manualRestoreAuthorized=false;
let cached={at:0, result:null};
let cpuLast=os.cpus().map(c=>c.times);
let cpuUsage=0;
function calculateCPU(){
 const now=os.cpus().map(c=>c.times), diff=(a,b)=>b-a;
 let busy=0,total=0;
 for(let i=0;i<now.length;i++){
  const old=cpuLast[i],cur=now[i];
  if(!old)continue;
  const all=['user','nice','sys','idle','irq'].reduce((a,k)=>a+diff(old[k],cur[k]),0);
  total+=all;busy+=all-diff(old.idle,cur.idle);
 }
 cpuLast=now; if(total>0)cpuUsage=Math.max(0,Math.min(100,busy/total*100));
 return Math.round(cpuUsage);
}
function json(res,code,data){
 res.writeHead(code,{'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'"});
 res.end(JSON.stringify(data));
}
function audit(event,data){
 fs.appendFileSync(auditPath,JSON.stringify({time:new Date().toISOString(),event,...data})+'\n');
}
function readReplacements(){
 try{const data=JSON.parse(fs.readFileSync(statePath,'utf8').replace(/^\uFEFF/,''));if(!Array.isArray(data.pairs))throw Error('Invalid replacement ledger');return data;}
 catch(e){if(e.code==='ENOENT')return {pairs:[]};throw Error('Replacement ledger unreadable; refusing unsafe duplicate dispatch');}
}
function saveReplacements(x){const tmp=statePath+'.tmp';fs.writeFileSync(tmp,JSON.stringify(x,null,2));fs.renameSync(tmp,statePath);}
function startupBlockReason({allowRestore=false}={}){
 const s=startup?.status();
 if(!s)return 'Startup safety controller has not initialized';
 if(s.reconciliationRequired||s.phase==='uncertain')return 'Interrupted Startup needs reconciliation before more work';
 if(s.phase==='resumable')return 'Interrupted Startup is verified resumable; run Startup to finish restoring the fleet before dispatch';
 if(s.active&&!(allowRestore&&['starting_restore','restoring'].includes(s.phase)))return 'Protected Startup is running; wait for runtime verification';
 return null;
}
function workBlockReason({allowStartupRestore=false}={}){
 if(master?.locked())return 'Emergency shutdown lockout is active';
 if(master?.drainState?.enabled)return 'Graceful wind-down is active';
 return quota?.dispatchBlockReason()||startupBlockReason({allowRestore:allowStartupRestore})||null;
}
function dispatchHealthBlockReason(){
 // Report the enabled scheduler's observed hold as well as the control gates.
 // This projection is display-only; starting work keeps its existing guards.
 return workBlockReason()||
  (sustain?.config.enabled&&typeof sustain.latest?.blocked==='string'?sustain.latest.blocked:null)||
  (turbo?.config.enabled&&typeof turbo.latest?.pausedReason==='string'?turbo.latest.pausedReason:null)||null;
}
function assertCanStartWork(options){
 const reason=workBlockReason(options);
 if(reason)throw Error(reason);
}
async function command(args,ms=30000,{windDown=false,startupRestore=false}={}){
 const startsWork=['send','spawn'].includes(args[0])||(args[0]==='review'&&args[1]==='trigger')||(args[0]==='session'&&['resume-agent','restore'].includes(args[1]));
 const message=args[args.indexOf('--message')+1]||'';
 const stopNotice=windDown&&args[0]==='send'&&master?.drainState?.enabled&&!master?.locked()&&message.startsWith('GRACEFUL WIND-DOWN ORDER FROM USER:');
 const authorizedRestore=startupRestore&&args[0]==='session'&&['resume-agent','restore'].includes(args[1])&&master?.restore?.state.active;
 if(startsWork&&!stopNotice)assertCanStartWork({allowStartupRestore:authorizedRestore});
 const id=args[args.indexOf('--session')+1];
 const reserved=args[0]==='send'&&!stopNotice&&dispatchLedger?dispatchLedger.reserve(id,message):null;
 try{
  let before=null;
  if(args[0]==='send'&&!stopNotice&&!args.includes('--steer')){
   const sessions=(await daemon('/api/v1/sessions')).sessions;
   const matches=Array.isArray(sessions)?sessions.filter(s=>s?.id===id):[];
   if(matches.length!==1)throw Error('Target changed before terminal delivery');
   before=matches[0];assertCanStartWork();
   const supportedHarness=['codex','agy'].includes(before.harness);
   const needsTerminalVerification=before.harness==='codex';
   if(before.isTerminated||before.mode!=='tui'||!supportedHarness||!['idle','waiting_input'].includes(before.activity?.state)||(needsTerminalVerification&&(typeof before.terminalGeneration!=='string'||!before.terminalGeneration)))throw Error('Target changed before terminal delivery');
  }
  const deliveryBaseline=before?.mode==='tui'&&before.harness==='codex'?deliveryReceipts.capture({id,generation:before.terminalGeneration,createdAt:before.createdAt}):null;
  if(reserved&&deliveryBaseline)dispatchLedger.attach(id,reserved.id,{baseline:deliveryBaseline,normalizedHash:require('node:crypto').createHash('sha256').update(message.replace(/\r\n/g,'\n').trimEnd()).digest('hex')});
  const sentAt=Date.now();
  const x=await exec(AO,args,{windowsHide:true,timeout:ms,maxBuffer:1024*1024,cwd:root});
  if(before?.mode==='tui'&&before.harness==='codex'){
   const delivery=await completeKnownPaste({root,id,generation:before.terminalGeneration,message,sentAt,
    getSession:async()=> (await daemon('/api/v1/sessions')).sessions.find(s=>s.id===id),guard:()=>assertCanStartWork(),
    receipt:()=>deliveryReceipts.inspect({id,messageHash:require('node:crypto').createHash('sha256').update(message).digest('hex'),normalizedHash:require('node:crypto').createHash('sha256').update(message.replace(/\r\n/g,'\n').trimEnd()).digest('hex'),sentAt,baseline:deliveryBaseline})});
   audit('terminal_delivery_checked',{id,state:delivery.state,extraEnter:delivery.extraEnter});
   if(delivery.state!=='native-observed')throw Error('Terminal delivery remains unverified; inspect before retrying');
  }
  if(reserved)dispatchLedger.finish(id,reserved.id,'submitted');
  return String(x.stdout||'').trim();
 }catch(e){if(reserved)dispatchLedger.finish(id,reserved.id,'uncertain');throw e}
}
async function daemon(uri){
 const response=await fetch('http://127.0.0.1:3001'+uri,{signal:AbortSignal.timeout(5500)});
 if(!response.ok)throw Error('Orchestrator daemon HTTP '+response.status);
 return response.json();
}
async function getLiveSessions(){
 const value=await daemon('/api/v1/sessions');
 if(!Array.isArray(value.sessions))throw Error('Orchestrator session list is unavailable');
 return addControllerHealth(value.sessions,id=>daemon('/api/v1/sessions/'+encodeURIComponent(id)+'/conversation?limit=1'));
}
function getClass(s){
 const n=s.displayName||'';
 if(s.kind==='orchestrator'||n.includes('F29-ORCH'))return 'Orchestration';
 if(n.includes('F29-REVIEW-CRIT'))return 'Critical review';
 if(n.includes('F29-REVIEW-ESC'))return 'Escalation review';
 if(n.includes('F29-REVIEW-FAST')||s.id==='mjobs-luna-11')return 'Fast review';
 if(n.includes('F29-DEEP'))return 'Deep implementation';
 if(n.includes('F29-ARCH'))return 'Architecture';
 if(n.includes('F29-COMPLEX'))return 'Complex implementation';
 return 'Fast implementation';
}
function transform(s,replacements){
 const prs=(s.prs||[]).map(p=>({number:p.number,url:p.url,state:p.state,ci:p.ci,review:p.review,mergeability:p.mergeability}));
 const openPRs=prs.filter(p=>p.state==='open'||p.state==='draft');
 const controllerUnavailable=s.mode==='chat'&&(s.controllerVerified!==true||!['ready','busy'].includes(s.controllerState));
 const activity=controllerUnavailable?'exited':s.activity?.state || 'unknown';
 const busy=activity==='active'||activity==='working';
 const isIdle=activity==='idle'||activity==='waiting_input';
 // Finished PRs retain their last recorded CI result as history, not a live
 // assignment blocker. Ignore an aggregate ci_failed only when every tracked
 // reference is positively completed; missing/unknown references stay cautious.
 const currentPRs=prs.filter(p=>!['merged','closed'].includes(String(p.state||'').trim().toLowerCase()));
 const allTrackedPRsCompleted=prs.length>0&&currentPRs.length===0;
 const blocked=currentPRs.some(p=>p.ci==='failing'||p.ci==='failed') ||(s.status==='ci_failed'&&!allTrackedPRsCompleted);
 const replacementFor=replacements.pairs.find(p=>p.newId===s.id);
 const replacedBy=replacements.pairs.find(p=>p.oldId===s.id);
 return {
  id:s.id,name:s.displayName||s.id,model:s.model||null,harness:s.harness||'codex',
  kind:s.kind,role:getClass(s),status:s.status||'unknown',activity,
  lastActivity:s.activity?.lastActivityAt||s.updatedAt||null,
  createdAt:s.createdAt||null,branch:s.branch||null,
  branchState:s.branchState||{},mode:s.mode||null,prs,openPRs:openPRs.length,
  busy,isIdle,blocked,isTerminated:!!s.isTerminated,
  stateLabel:controllerUnavailable?'Controller '+(s.controllerState||'unverified'):busy?'Working':isIdle?(blocked?'Idle · CI attention':openPRs.length?'Idle · PR pending':activity==='waiting_input'?'Awaiting instruction':'Idle · available'):(activity==='exited'?'Exited':activity),
  controllerState:s.controllerState||null,controllerVerified:s.controllerVerified??null,
  replacementFor:replacementFor?.oldId||null,replacedBy:replacedBy?.newId||null,
  ...operationalDetails(s,sustain),
  effortKnown:false
 };
}
function emergencyOfflineSnapshot(reason='Emergency stop is locked; Orchestrator.inc daemon is intentionally offline'){
 // During Emergency Kill the AO daemon is intentionally stopped. A missing
 // daemon must not turn the dashboard into an error page or fake LIVE agents.
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'restore-snapshot.json'),'utf8'));
 if(!Array.isArray(manifest.sessions)||!manifest.sessions.length)throw Error('No saved emergency roster');
 const replacements=readReplacements();
 const sessions=manifest.sessions.filter(s=>s.projectId===projectId&&!s.isTerminated).map(s=>({
  ...transform(s,replacements),busy:false,isIdle:false,blocked:false,openPRs:0,prs:[],
  activity:'exited',status:'emergency_stopped',stateLabel:'Stopped · preserved snapshot',
  lastActivity:s.observedAt||manifest.at||null
 }));
 const metrics={cpu:calculateCPU(),ramTotal:os.totalmem(),ramFree:os.freemem(),logicalCores:os.cpus().length,uptime:os.uptime()};
 return {
  timestamp:new Date().toISOString(),projectId,version:'1.0.0',offline:true,
  snapshotAt:manifest.at,offlineReason:reason,
  sessions,summary:{total:sessions.length,working:0,idle:0,idleFree:0,idlePending:0,ciAttention:0,other:sessions.length},
  metrics,models:MODEL_OPTIONS,efforts:EFFORT_OPTIONS,
  settings:{loopback:true,changeMode:'replacement',noAutomaticTaskAssignments:true},
  replacements:replacements.pairs
 };
}
async function state(force=false){
 if(stateInFlight)return stateInFlight;
 stateInFlight=loadState(force);
 try{return await stateInFlight}finally{stateInFlight=null}
}
async function freshMutationState(){
 let result;
 try{result=await loadState(true)}catch{throw Error('Fresh live session state unavailable. No lifecycle mutation was submitted.')}
 if(!result||result.offline||!Array.isArray(result.sessions))throw Error('Fresh live session state unavailable. No lifecycle mutation was submitted.');
 return result;
}
async function loadState(force=false){
 if(!force && cached.result && Date.now()-cached.at<1800)return cached.result;
 let res;
 try{res={sessions:await getLiveSessions()}}
 catch(e){
  const reason=master?.locked()?'Emergency stop is locked; Orchestrator.inc daemon is intentionally offline':'Orchestrator daemon unavailable; displaying preserved sessions, not live activity';
  const result=emergencyOfflineSnapshot(reason);
  cached={at:Date.now(),result};
  return result;
 }
 const all=(res.sessions||[]).filter(s=>s.projectId===projectId);
 const replacements=readReplacements();
 const active=all.filter(s=>!s.isTerminated).map(s=>transform(s,replacements));
 const metrics={cpu:calculateCPU(),ramTotal:os.totalmem(),ramFree:os.freemem(),logicalCores:os.cpus().length,uptime:os.uptime()};
 const summary={
  total:active.length,working:active.filter(x=>x.busy).length,
  idle:active.filter(x=>x.isIdle).length,
  idleFree:active.filter(x=>x.isIdle&&!x.openPRs&&!x.blocked).length,
  idlePending:active.filter(x=>x.isIdle&&x.openPRs).length,
  ciAttention:active.filter(x=>x.blocked).length,
  other:active.filter(x=>!x.busy&&!x.isIdle).length
 };
 const result={timestamp:new Date().toISOString(),projectId,version:'1.0.0',sessions:active,summary,metrics,models:MODEL_OPTIONS,efforts:EFFORT_OPTIONS,settings:{loopback:true,changeMode:'replacement',noAutomaticTaskAssignments:!(sustain?.config.enabled||turbo?.config.enabled)},replacements:replacements.pairs};
 cached={at:Date.now(),result};return result;
}
async function body(req){
 let parts=[],size=0;
 for await(const chunk of req){size+=chunk.length;if(size>20000)throw Error('Request too large');parts.push(chunk)}
 let value;try{value=JSON.parse(Buffer.concat(parts).toString('utf8')||'{}')}catch{throw Error('Invalid JSON')}
 // A slow request body must not bypass a Startup that began after headers.
 if(startup?.status().active&&!['/api/startup','/api/startup/cancel','/api/startup/reconcile','/api/master/emergency','/api/master/pause','/api/master/drain','/api/master/restore/pause','/api/quota/disarm','/api/agent-plan'].includes(req.url))throw Error('Protected Startup began while this request was being read; no competing action was submitted');
 return value;
}
function checkWrite(req){
 const origin=req.headers.origin;
 if(origin&&origin!=='http://127.0.0.1:'+port&&origin!=='http://localhost:'+port)throw Error('Cross-origin request rejected');
 if(req.headers['x-ao-fleet']!=='1')throw Error('Missing local action header');
 if(!(req.headers['content-type']||'').includes('application/json'))throw Error('JSON required');
}
function assertSession(active,id){
 if(typeof id!=='string'||!/^[a-z0-9-]{4,90}$/.test(id))throw Error('Invalid session ID');
 const s=active.find(x=>x.id===id);if(!s)throw Error('Session not found');return s;
}
function isCleanCandidate(s){
 return s.kind==='worker' && s.isIdle && !s.busy && !s.isTerminated && !s.openPRs && !s.blocked &&
  Number(s.branchState?.commits||0)===0 && Number(s.branchState?.unpushed||0)===0 &&
  !s.replacedBy;
}
function isRetirementCandidate(s,pair){
 return s?.kind==='worker'&&s.isIdle&&!s.busy&&!s.isTerminated&&!s.openPRs&&!s.blocked&&
  Number(s.branchState?.commits||0)===0&&Number(s.branchState?.unpushed||0)===0&&
  s.replacedBy===pair?.newId;
}
function sameMutationCandidate(before,current,eligible){
 if(!before||!current||!eligible(current))return false;
 const keys=['id','createdAt','branch','kind','model','harness','mode','activity','status','isIdle','busy','isTerminated','openPRs','blocked','replacedBy'];
 return keys.every(key=>(before[key]??null)===(current[key]??null))&&
  Number(before.branchState?.commits||0)===Number(current.branchState?.commits||0)&&
  Number(before.branchState?.unpushed||0)===Number(current.branchState?.unpushed||0)&&
  JSON.stringify(before.prs||[])===JSON.stringify(current.prs||[]);
}
async function assertWorktreeClean(s){
 if(!s.branch)throw Error('Cannot identify the original branch; stopping safely.');
 const result=await exec('git',['-C','C:\\Tools\\Dev\\MJobs','worktree','list','--porcelain'],{windowsHide:true,timeout:12000,maxBuffer:1024*1024});
 const chunks=result.stdout.replace(/\r/g,'').split(/\n\n+/);
 let worktree=null;
 for(const chunk of chunks){
  const pathLine=chunk.split('\n').find(line=>line.startsWith('worktree '));
  const branchLine=chunk.split('\n').find(line=>line.startsWith('branch '));
  if(branchLine==='branch refs/heads/'+s.branch&&pathLine){
   worktree=pathLine.slice(9);break;
  }
 }
 if(!worktree)throw Error('Cannot verify the session worktree. No replacement or retirement performed.');
 const status=await exec('git',['-C',worktree,'status','--porcelain=v1','--untracked-files=normal'],{windowsHide:true,timeout:15000,maxBuffer:1024*1024});
 if(status.stdout.trim())throw Error('Session worktree contains uncommitted or untracked changes. Preserve or resolve them before replacing this agent.');
}
async function nudge(x){
 const snap=await state(true),s=assertSession(snap.sessions,x.id);
 const primary=snap.sessions.find(x=>x.kind==='orchestrator'&&!x.isTerminated&&!x.replacedBy&&x.activity!=='exited')?.id||'the primary orchestrator';
 if(!s.isIdle)throw Error('Agent is not idle. Nudge disabled for active or exited sessions.');
 if(s.replacedBy)throw Error('This session has a replacement. Nudge the replacement.');
 const type=x.type||'progress';
 let message;
 if(type==='progress'){
  message='Please provide a concise current checkpoint: assigned issue, progress, blockers, outstanding PR/CI, and next useful bounded action. Do not change code or start new work solely because of this status request.';
 }else if(type==='next'){
  message='You appear idle. Review your previous assignment and any owned PRs first. If your work is complete, request a new nonoverlapping P0/P1 task from the primary orchestrator '+primary+'; do not independently claim files, duplicate an existing worker, or begin a new implementation without explicit ownership.';
 }else if(type==='custom'){
  if(typeof x.message!=='string'||x.message.trim().length<10||x.message.length>2400)throw Error('Custom message must contain 10–2400 characters');
  message=x.message.trim();
 }else throw Error('Unknown nudge mode');
 const args=['send','--session',s.id,'--message',message];
 if(s.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
 const output=await command(args,20000);
 audit('nudge',{id:s.id,type,acceptedOutput:output.slice(0,600)});
 cached.at=0;return {ok:true,sessionId:s.id,accepted:true,note:'Message submitted. Agent completion is not yet confirmed.',output:output.slice(0,800)};
}
async function replace(x){
 const snap=await state(true),old=assertSession(snap.sessions,x.id);
 const newModel=MODEL_OPTIONS.find(m=>m.id===x.model);
 if(!newModel)throw Error('Unsupported model selection');
 if(!EFFORT_OPTIONS.includes(x.effort))throw Error('Unsupported effort');
 if(!isCleanCandidate(old))throw Error('For safety, only idle worker sessions without PRs, CI failures, or tracked commits are eligible. The existing session has work or is not idle.');
 if(old.kind==='orchestrator')throw Error('Use a separate controlled handoff for the primary orchestrator');
 await verifyCodexExecutable();
 await assertWorktreeClean(old);
 const recheck=await freshMutationState(),current=recheck.sessions.find(s=>s.id===old.id);
 if(!sameMutationCandidate(old,current,isCleanCandidate))throw Error('Original session state changed during replacement validation. No replacement was launched.');
 const name=old.name.replace(/^↪ /,'').slice(0,71)+' / '+newModel.name.slice(0,21);
 const primary=recheck.sessions.find(s=>s.kind==='orchestrator'&&!s.isTerminated&&!s.replacedBy&&s.activity!=='exited')?.id||'the primary orchestrator';
 const prompt='You are a replacement for session '+old.id+' ('+old.name+'). Your class is '+old.role+'. Preserve all existing branches, PRs and user changes; do not edit main, force-push, or auto-merge. First read the existing session history with ao session history '+old.id+' --limit 8 and report to primary orchestrator '+primary+' for a specifically assigned, nonoverlapping issue and file scope. Do NOT take over old PRs or start implementing until delegated.';
 let output;
 try{
  output=await command(['spawn','--project',projectId,'--kind','worker','--mode','tui','--harness','codex','--model',newModel.id,'--effort',x.effort,'--name',name,'--prompt',prompt],45000);
 }catch(e){
  const message=String(e?.message||e);
  const category=message.includes('AGENT_BINARY_NOT_FOUND')?'AGENT_BINARY_NOT_FOUND':message.includes('RATE_LIMIT')?'RATE_LIMIT':'SPAWN_FAILED';
  audit('replacement_spawn_failed',{oldId:old.id,model:newModel.id,effort:x.effort,category});
  throw Error(category==='AGENT_BINARY_NOT_FOUND'?
   'Codex Windows executable could not be found by AO. Repair the Codex installation and retry. Original session is preserved.':
   'AO could not start the replacement ('+category+'). The original session is preserved. See Fleet Control logs for diagnostics.');
 }
 cached.at=0;
 let latest=await state(true);
 let fresh=latest.sessions.filter(s=>s.name===name&&!s.isTerminated).sort((a,b)=>Date.parse(b.createdAt)-Date.parse(a.createdAt));
 if(!fresh.length){await new Promise(r=>setTimeout(r,1400));latest=await state(true);fresh=latest.sessions.filter(s=>s.name===name&&!s.isTerminated)}
 if(fresh.length!==1||fresh[0].model!==newModel.id){audit('replace_unverified',{from:old.id,name,model:newModel.id,output:output.slice(0,700)});throw Error('Spawn submitted but replacement could not be verified. Refresh and inspect the session list before retrying.')}
 const record={oldId:old.id,newId:fresh[0].id,model:newModel.id,effort:x.effort,at:new Date().toISOString(),retired:false};
 const records=readReplacements();records.pairs.push(record);saveReplacements(records);
 audit('replace_created',{...record,output:output.slice(0,500)});cached.at=0;
 return {ok:true,...record,note:'Replacement verified. Original preserved; retire it separately only after reviewing ownership.'};
}
async function applyAgentPlan(x){
 assertCanStartWork();
 if(master?.restore?.state?.active)throw Error('Wait for fleet restoration to finish before applying agent changes');
 if(x.confirm!=='APPLY AGENT PLAN')throw Error('Explicit agent-plan confirmation required');
 const snap=await state(true),old=assertSession(snap.sessions,x.id);
 if(snap.offline)throw Error('Orchestrator must be online before applying a saved plan.');
 if(old.kind!=='worker'||old.replacedBy||old.busy||!['idle','exited'].includes(old.activity))throw Error('Only an idle or stopped, unreplaced worker can be changed. Active work is never interrupted.');
 const saved=agentPlans.all().plans.find(p=>p.id===old.id&&p.status==='pending');
 if(!saved)throw Error('Save a new agent plan before applying it.');
 const selection=await agentPlans.validate(saved);
 if(selection.harness==='codex')await verifyCodexExecutable();
 const plan=agentPlans.begin(old.id),priorIds=new Set(snap.sessions.map(s=>s.id));
 let spawnRequested=false;
 try{
  if(old.activity==='idle'){
   await command(['session','exit-agent',old.id,'--project',projectId,'--json'],20000);
   let stopped=false;
   for(let i=0;i<8;i++){
    const latest=await state(true),entry=latest.sessions.find(s=>s.id===old.id);
    if(!latest.offline&&entry?.activity==='exited'){stopped=true;break;}
    await new Promise(r=>setTimeout(r,300));
   }
   if(!stopped)throw Error('Original runtime exit was not confirmed. No replacement was launched.');
  }
  const name=(old.name||old.id).slice(0,50)+' / '+selection.harness+' '+plan.planId.slice(0,8);
  const primary=snap.sessions.find(s=>s.kind==='orchestrator'&&!s.isTerminated&&!s.replacedBy&&s.activity!=='exited')?.id||'the project primary';
  const prompt='CONTROLLED WORKER HANDOFF: Replaces parked session '+old.id+' in a NEW isolated worktree. The original branch '+(old.branch||'(unknown)')+' and all uncommitted work are preserved, not transferred or deleted. Existing PRs: '+old.prs.map(p=>'#'+p.number).join(', ')+'. Read AGENTS.md and docs/AI_AGENT_CONTINUITY.md, inspect the saved task checkpoint and previous session history (ao session history '+old.id+' --limit 8), then coordinate with '+primary+' before any implementation. Do not independently claim old PRs or overlapping files. Main integration is single-writer; only exact-head green required CI/reviews can permit merge. No force push, credential access, extra agents, real employer submissions, or dirty-main changes. First report the precise handoff and wait for exclusive task/file ownership.';
  const args=['spawn','--project',projectId,'--kind','worker','--mode',selection.mode,'--harness',selection.harness,'--model',selection.model,'--name',name,'--prompt',prompt];
  if(selection.harness==='codex'&&selection.effort&&selection.effort!=='default')args.push('--effort',selection.effort);
  spawnRequested=true;
  await command(args,45000);
  let verified=null;
  for(let i=0;i<8;i++){
   const latest=await state(true);
   const candidates=latest.sessions.filter(s=>!priorIds.has(s.id)&&s.name===name&&!s.isTerminated);
   if(candidates.length===1&&candidates[0].model===selection.model&&candidates[0].harness===selection.harness){verified=candidates[0];break;}
   await new Promise(r=>setTimeout(r,400));
  }
  if(!verified)throw Error('Spawn submitted but new worker identity was not confirmed; inspect roster before retrying.');
  const record={oldId:old.id,newId:verified.id,...selection,at:new Date().toISOString(),retired:false,originalParked:true};
  const records=readReplacements();records.pairs.push(record);saveReplacements(records);
  const completed=agentPlans.finish(old.id,plan.planId,'applied',{newId:verified.id});
  audit('agent_plan_applied',record);cached.at=0;
  return {ok:true,plan:completed,...record,note:'Replacement identity verified. Original session, branch, PRs, worktree, and uncommitted files are preserved; no automatic ownership transfer or archive occurred.'};
 }catch(e){
  agentPlans.finish(old.id,plan.planId,spawnRequested?'unverified':'failed',{error:String(e.message).slice(0,400)});
  audit('agent_plan_apply_failed',{id:old.id,spawnRequested,message:String(e.message).slice(0,400)});
  throw e;
 }
}
async function retire(x){
 const snap=await state(true);const original=assertSession(snap.sessions,x.id);
 const records=readReplacements(),pair=records.pairs.find(p=>p.oldId===x.id&&!p.retired);
 if(!pair)throw Error('No verified replacement record for that agent.');
 if(!x.confirm||x.confirm!==original.id)throw Error('Type the exact original session ID to confirm retirement.');
 if(!isRetirementCandidate(original,pair))throw Error('Original has activity/PRs/commits or is no longer the verified replaced worker. Cannot retire automatically.');
 const fresh=assertSession(snap.sessions,pair.newId);
 if(fresh.model!==pair.model)throw Error('Replacement verification mismatch');
 await assertWorktreeClean(original);
 const directory=path.join(root,'backups');fs.mkdirSync(directory,{recursive:true});
 const stamp=Date.now();
 const output=await command(['session','history',original.id,'--limit','50'],15000);
 fs.writeFileSync(path.join(directory,'history-'+original.id+'-'+stamp+'.txt'),output);
 const recheck=await freshMutationState(),current=recheck.sessions.find(s=>s.id===original.id);
 if(!sameMutationCandidate(original,current,s=>isRetirementCandidate(s,pair)))throw Error('Original session state changed during retirement validation. No session was killed.');
 const currentReplacement=recheck.sessions.find(s=>s.id===pair.newId);
 if(!currentReplacement||currentReplacement.isTerminated||currentReplacement.model!==pair.model||currentReplacement.createdAt!==fresh.createdAt)throw Error('Replacement state changed during retirement validation. No session was killed.');
 const kill=await command(['session','kill',original.id,'--project',projectId],20000);
 pair.retired=true;pair.retiredAt=new Date().toISOString();saveReplacements(records);
 audit('retired',{oldId:original.id,newId:fresh.id,result:kill.slice(0,400)});cached.at=0;
 return {ok:true,note:'Old session archived; its history was backed up. No git cleanup was performed.',oldId:original.id,newId:fresh.id};
}
async function main(req,res){
 if(!['127.0.0.1:'+port,'localhost:'+port].includes(req.headers.host||''))return json(res,403,{error:'Local host only'});
 if(master?.locked()&&req.method==='POST'&&!['/api/master/emergency','/api/master/rearm','/api/master/restore','/api/master/pause','/api/agent-plan','/api/startup','/api/startup/cancel','/api/startup/reconcile'].includes(req.url))return json(res,423,{error:'Emergency shutdown lockout is active; use protected Startup after the allowance reset'});
 if(master?.drainState.enabled&&req.method==='POST'&&req.url!=='/api/agent-plan'&&!req.url.startsWith('/api/master/')&&!req.url.startsWith('/api/startup'))return json(res,423,{error:'Graceful shutdown is draining sessions. New work is disabled'});
 if(req.method==='GET'&&req.url==='/api/startup'){
  const readiness=await startupAdapter.readiness();
  let preview;try{const {targets}=startupAdapter.manifest();preview={targetCount:targets.length,primaryIds:targets.filter(s=>s.kind==='orchestrator').map(s=>s.id)};}catch(e){preview={targetCount:0,reason:e.message};}
  return json(res,200,{run:startup.status(),readiness,preview,launcher:orchestratorLauncher.status()});
 }
 if(req.method==='POST'&&['/api/startup','/api/startup/cancel','/api/startup/reconcile','/api/orchestrator/open'].includes(req.url)){
  let owns=false;
  try{
   checkWrite(req);const data=await body(req);
   if(req.url==='/api/startup/cancel'){const r=startup.cancel(data);master.disableAutomaticDispatch();master.restore.pause();return json(res,r.statusCode,r);}
   if(mutationLock||sustain.running||turbo.running||findWork.running)return json(res,409,{error:'Another control operation is running; Startup did not submit work'});
   mutationLock=true;owns=true;
   if(req.url==='/api/startup'){const policy=quota.status().policy;if(policy?.blocked)return json(res,409,{error:policy.reason,code:'QUOTA_BLOCKED',quotaPolicy:policy});const r=startup.start(data);return json(res,r.statusCode,r);}
   if(req.url==='/api/startup/reconcile'){const r=await startup.reconcile(data);return json(res,r.statusCode||200,r);}
   assertCanStartWork();
   const r=await orchestratorLauncher.launch('ui',{beforeTrigger:()=>assertCanStartWork()});
   return json(res,200,{ok:true,launch:r,note:'Orchestrator is open in your standard user session. No fleet restore or prompt was submitted by this button.'});
  }catch(e){audit('startup_control_error',{route:req.url,code:e.code||null,message:String(e.message).slice(0,450)});return json(res,409,{error:e.message,code:e.code||null});}
  finally{if(owns)mutationLock=false;}
 }
 if(req.method==='POST'&&startup?.status().active){
  const safe=['/api/master/emergency','/api/master/pause','/api/master/drain','/api/master/restore/pause','/api/quota/disarm','/api/agent-plan'];
  if(!safe.includes(req.url))return json(res,409,{error:'Protected Startup is running. Agent changes and dispatch must wait until it completes.'});
 }
 if(req.method==='GET'&&req.url==='/api/providers')return json(res,200,await providerRuntime.get());
 if(req.method==='GET'&&req.url==='/api/gemini-usage'){
  const catalog=await providerRuntime.get();return json(res,200,readGeminiUsage({agyAvailable:catalog.providers.some(p=>p.id==='agy'&&p.available),geminiAvailable:catalog.providers.some(p=>p.id==='gemini'&&p.installed)}));
 }
 if(req.method==='GET'&&req.url==='/api/preflight'){
  let snapshot=null;try{snapshot=await state(true)}catch{}
  const report=preflight({quota:quota.status(),master:master.status(),snapshot,controlBlockReason:startupBlockReason(),runtimeReadiness:await startupAdapter.readiness()});
  try{const v=JSON.parse(fs.readFileSync(path.join(root,'diagnostics','preflight-results.json'),'utf8'));report.verification={at:v.at,passed:v.passed,failed:v.failed,notRun:v.notRun,sourceStable:v.sourceStable,currentSourcesMatch:v.sourceFingerprint===require('./check-fleet').sourceFingerprint(root)};}catch{report.verification={status:'NOT_RUN'};}
  return json(res,200,report);
 }
 if(req.method==='GET'&&req.url==='/api/agent-plan')return json(res,200,agentPlans.all());
 if(req.method==='POST'&&req.url==='/api/agent-plan'){
  try{checkWrite(req);const data=await body(req);const snap=await state(true);const session=assertSession(snap.sessions,data.id);return json(res,200,await agentPlans.save(session,data));}
  catch(e){return json(res,400,{error:e.message});}
 }
 if(req.method==='GET'&&req.url==='/api/find-work/preview'){
  try{return json(res,200,await findWork.preview())}
  catch(e){return json(res,409,{error:e.message})}
 }
 if(req.method==='GET'&&req.url==='/api/find-work/status')return json(res,200,findWork.status());
 if(req.method==='POST'&&['/api/find-work','/api/find-work/all'].includes(req.url)){
  let ownsManualLock=false;
  try{
   checkWrite(req);
   if(mutationLock||findWork.running) return json(res,409,{error:'Another manual agent control is running; retry shortly'});
   mutationLock=true;ownsManualLock=true;
   const data=await body(req);
   // Reserve the manual lane first; allow an already executing automated cycle
   // to finish without forcing the user to retry a button press.
   for(let i=0;(sustain.running||turbo.running)&&i<80;i++)await new Promise(r=>setTimeout(r,250));
   if(sustain.running||turbo.running)return json(res,409,{error:'An automatic dispatch is still finishing. Try again after it completes.'});
   if(req.url==='/api/find-work'){
    if(typeof data.id!=='string'||!/^[a-z0-9-]{4,90}$/.test(data.id))throw Error('Valid session ID required');
    return json(res,200,await findWork.sendOne(data.id));
   }
   if(data.confirm!=='FIND WORK ALL')throw Error('Explicit fleet-wide Find Work confirmation required');
   return json(res,200,await findWork.sendAll());
  }catch(e){audit('find_work_control_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
  finally{if(ownsManualLock)mutationLock=false}
 }
 if(req.method==='GET'&&req.url==='/api/quota')return json(res,200,quota.status());
 if(req.method==='POST'&&req.url.startsWith('/api/quota/')){
  try{
   checkWrite(req);
   const data=await body(req);
   if(req.url==='/api/quota/config')return json(res,200,quota.update(data));
   if(req.url==='/api/quota/arm'){
    if(data.confirm!=='ARM AUTO STOP')throw Error('Automatic stop confirmation required');
    return json(res,200,quota.arm());
   }
   if(req.url==='/api/quota/disarm')return json(res,200,quota.disarm());
   return json(res,404,{error:'Unknown quota operation'});
  }catch(e){audit('quota_control_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
 }
 if(req.method==='GET'&&req.url==='/api/master')return json(res,200,{...master.status(),dispatch:{...master.status().dispatch,blockedReason:dispatchHealthBlockReason()},startup:startup.status(),currentReadiness:await startupAdapter.readiness()});
 if(req.method==='GET'&&req.url==='/api/master/restore/preview'){
  try{return json(res,200,await master.restorePreview())}
  catch(e){return json(res,409,{error:e.message})}
 }
 if(req.method==='POST'&&req.url.startsWith('/api/master/')){
  let ownsMasterLane=false;
  try{
   checkWrite(req);
   const data=await body(req);
   if(['/api/master/pause','/api/master/drain','/api/master/restore/pause','/api/master/emergency'].includes(req.url)&&startup?.status().active){
    if(req.url==='/api/master/drain'&&data.confirm!=='GRACEFUL STOP')throw Error('Graceful stop confirmation missing');
    if(req.url==='/api/master/emergency'&&data.confirm!=='KILL ALL CODEX')throw Error('Explicit emergency confirmation missing');
    startup.cancel({runId:startup.status().runId});master.disableAutomaticDispatch();master.restore.pause();
   }
   if(req.url==='/api/master/pause')return json(res,200,master.pause());
   if(req.url==='/api/master/drain'){
    if(data.confirm!=='GRACEFUL STOP')throw Error('Graceful stop confirmation missing');
    return json(res,200,await master.drain());
   }
   if(req.url==='/api/master/cancel-drain')return json(res,200,master.cancelDrain());
   if(req.url==='/api/master/restore'){
    if(data.confirm!=='RESTORE FLEET')throw Error('Exact fleet restoration confirmation required');
    if(mutationLock||sustain.running||turbo.running||findWork.running)throw Error('Another control operation is running');
    mutationLock=true;ownsMasterLane=true;
    if(startupBlockReason())throw Error(startupBlockReason());
    if(master.restore.state.active||master.restore.running)throw Error('A restore operation is already running');
    if(Object.keys(master.restore.state.inflight||{}).length)throw Error('An earlier resume is unverified. Use Startup reconciliation before sending another restore.');
    manualRestoreAuthorized=true;
    const result=await master.restoreStart({autoDispatchAfter:data.autoDispatchAfter===true});
    return json(res,202,result);
   }
   if(req.url==='/api/master/restore/complete-reviewed'){
    if(mutationLock||sustain.running||turbo.running||findWork.running)throw Error('Another control operation is running');
    mutationLock=true;ownsMasterLane=true;
    const result=await completeReviewedRestore(data,{master,startup,quota,launcher:orchestratorLauncher,adapter:startupAdapter,sustain,turbo,findWork});
    return json(res,200,result);
   }
   if(req.url==='/api/master/restore/pause')return json(res,200,master.restorePause());
   if(req.url==='/api/master/emergency'){
    if(data.confirm!=='KILL ALL CODEX')throw Error('Explicit emergency confirmation missing');
    return json(res,202,await master.emergency());
   }
   if(req.url==='/api/master/rearm'){
    if(data.confirm!=='REARM')throw Error('Explicit rearm confirmation missing');
    return json(res,200,master.rearm());
   }
   return json(res,404,{error:'Unknown master action'});
  }catch(e){if(ownsMasterLane)manualRestoreAuthorized=false;audit('master_action_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
  finally{if(ownsMasterLane)mutationLock=false;}
 }
 if(req.method==='GET'&&req.url==='/api/environment'){
  try{return json(res,200,{codexExecutableReady:true,codexVersion:await verifyCodexExecutable(),replacementMethod:'preserved-original-new-session'})}
  catch(e){return json(res,503,{codexExecutableReady:false,error:e.message})}
 }
 if(req.method==='GET'&&req.url==='/api/state'){try{return json(res,200,await state(true))}catch(e){return json(res,503,{error:String(e.message)})}}
 if(req.method==='GET'&&req.url==='/api/health'){return json(res,200,{ok:true,port,host:listenHost,turboEnabled:turbo?.configuration().enabled,sustainedEnabled:sustain?.config.enabled,dispatchBlockedReason:dispatchHealthBlockReason()})}
 if(req.method==='GET'&&req.url==='/api/sustain')return json(res,200,sustain.summary());
 if(req.method==='GET'&&req.url==='/api/sustain/preview'){
  try{return json(res,200,await sustain.preview())}catch(e){return json(res,409,{error:e.message})}
 }
 if(req.method==='POST'&&['/api/sustain/toggle','/api/sustain/config','/api/sustain/tick'].includes(req.url)){
  try{
   checkWrite(req);
   const data=await body(req);
   if(req.url==='/api/sustain/toggle'&&data.enabled===false)return json(res,200,sustain.enable(false));
   if(mutationLock||turbo.running||sustain.running)return json(res,409,{error:'Another controller operation is running'});
   if(req.url==='/api/sustain/toggle'){
    if(data.enabled)assertCanStartWork();
    const outcome=sustain.enable(data.enabled);
    if(data.enabled&&turbo.config.enabled)turbo.enable(false);
    return json(res,200,outcome);
   }
   if(req.url==='/api/sustain/config')return json(res,200,sustain.update(data));
   if(req.url==='/api/sustain/tick')return json(res,200,await sustain.tick({forced:false,dryRun:!!data.dryRun}));
  }catch(e){audit('sustain_control_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
 }
 if(req.method==='GET'&&req.url==='/api/turbo'){return json(res,200,turbo.summary())}
 if(req.method==='GET'&&req.url==='/api/turbo/all-hands/preview'){
  try{return json(res,200,await turbo.previewAllHands())}catch(e){return json(res,409,{error:e.message})}
 }
 if(req.method==='GET'&&req.url==='/api/turbo/preview'){
  try{return json(res,200,await turbo.preview())}catch(e){return json(res,409,{error:e.message})}
 }
 if(req.method==='POST'&&['/api/turbo/toggle','/api/turbo/config','/api/turbo/run','/api/turbo/all-hands'].includes(req.url)){
  try{
   checkWrite(req);
   const data=await body(req);
   if(req.url==='/api/turbo/toggle'&&data.enabled===false)return json(res,200,turbo.enable(false));
   if(mutationLock||sustain?.running)return json(res,409,{error:'Another dashboard operation or sustained dispatch is running'});
   if(req.url==='/api/turbo/toggle'){if(data.enabled){assertCanStartWork();if(sustain.config.enabled)sustain.enable(false);}return json(res,200,turbo.enable(data.enabled));}
   if(req.url==='/api/turbo/config'){
    if(turbo.running)return json(res,409,{error:'Wait for the active cycle to finish before editing settings'});
    return json(res,200,turbo.update(data));
   }
   if(req.url==='/api/turbo/run')return json(res,200,await turbo.cycle({manual:true,dryRun:!!data.dryRun}));
   if(req.url==='/api/turbo/all-hands'){
    if(data.confirm!=='ALL HANDS')throw Error('Missing explicit All Hands confirmation');
    assertCanStartWork();
    const burst=await turbo.allHands();
    return json(res,200,{...burst,sustained:sustain.config.enabled,note:burst.note+' Automatic modes were not changed.'});
   }
  }catch(e){audit('turbo_control_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
 }
 if(req.method==='POST'&&['/api/nudge','/api/replace','/api/retire','/api/agent-plan/apply'].includes(req.url)){
  let ownsManualLock=false;
  try{
   checkWrite(req);
   if(mutationLock)return json(res,409,{error:'Another manual control operation is in progress. Please retry shortly.'});
   mutationLock=true;ownsManualLock=true;
   const data=await body(req);
   for(let i=0;(sustain.running||turbo.running)&&i<80;i++)await new Promise(r=>setTimeout(r,250));
   if(sustain.running||turbo.running)return json(res,409,{error:'Automatic dispatch is still finishing; no manual action was submitted.'});
   let result;
   if(req.url==='/api/nudge')result=await nudge(data);
   if(req.url==='/api/replace')result=await replace(data);
   if(req.url==='/api/retire')result=await retire(data);
   if(req.url==='/api/agent-plan/apply')result=await applyAgentPlan(data);
   return json(res,200,result);
  }catch(e){audit('action_error',{route:req.url,message:e.message});return json(res,400,{error:e.message})}
  finally{if(ownsManualLock)mutationLock=false}
 }
 if(req.method!=='GET')return json(res,405,{error:'Method not allowed'});
 const item=(req.url||'/').split('?')[0];
 const assets={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/turbo-ui.js':'turbo-ui.js','/sustain-ui.js':'sustain-ui.js','/master-ui.js':'master-ui.js','/restore-ui.js':'restore-ui.js','/quota-ui.js':'quota-ui.js','/find-work-ui.js':'find-work-ui.js','/style.css':'style.css','/turbo.css':'turbo.css','/master.css':'master.css','/restore.css':'restore.css','/quota.css':'quota.css','/find-work.css':'find-work.css'};
 Object.assign(assets,{'/startup-ui.js':'startup-ui.js','/startup.css':'startup.css'});
 const file=assets[item];
 if(!file)return json(res,404,{error:'Not found'});
 try{
  const data=fs.readFileSync(path.join(publicRoot,file));
  const mime=file.endsWith('.js')?'application/javascript; charset=utf-8':file.endsWith('.css')?'text/css; charset=utf-8':'text/html; charset=utf-8';
  res.writeHead(200,{'Content-Type':mime,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'","Referrer-Policy":'no-referrer'});
  res.end(data);
 }catch(e){json(res,500,{error:e.message})}
}
if(!fs.existsSync(AO)){console.error('Missing Orchestrator CLI at '+AO);process.exit(1)}
dispatchLedger=new DispatchLedger({root});
setInterval(()=>{if(mutationLock||sustain?.running||turbo?.running||startup?.status()?.active||master?.restore?.state?.active)return;try{receiptReconciliation.run(dispatchLedger,sustain);}catch(e){audit('delivery_reconcile_unavailable',{reason:String(e.message).slice(0,180)});}},15000);
orchestratorLauncher=new OrchestratorLauncher({root});
agentPlans=new AgentPlans({root,audit,validate:async x=>validateSelection(x,await providerRuntime.get())});
turbo=new Turbo({root,getState:state,command,audit,isManualBusy:()=>mutationLock||!!sustain?.running||!!master?.locked()||!!master?.drainState.enabled||!!master?.restore.state.active||!!quota?.dispatchBlockReason()||!!startupBlockReason(),projectId});
sustain=new Sustain({root,getState:state,turbo,command,audit,isManualBusy:()=>mutationLock||!!master?.locked()||!!master?.drainState.enabled||!!master?.restore.state.active||!!quota?.dispatchBlockReason()||!!startupBlockReason(),projectId});
turbo.sustain=sustain;sustain.deliveryLedger=dispatchLedger;turbo.deliveryLedger=dispatchLedger;
master=new MasterControls({root,sustain,turbo,command,audit,getState:state,getAllSessions:getLiveSessions,projectId,
 restoreOptions:{snapshotFrozen:()=>!!startup?.status().active||!!startup?.status().reconciliationRequired,
 launchAO:()=>orchestratorLauncher.launch('daemon',{beforeTrigger:()=>{
  if(!master.restore.state.active||master.locked()||quota.dispatchBlockReason()||startupBlockReason({allowRestore:true}))throw Error('Restore was stopped before Orchestrator could launch');
 }}),runtimeHealth:runtimeHealthy,controllerStopped},
 restoreCommand:(args,ms)=>command(args,ms,{startupRestore:true}),
 startBlockReason:()=>startupBlockReason({allowRestore:true})||(!startup?.status().active&&!manualRestoreAuthorized?'Restoration awaits an explicit Startup or Restore request after this dashboard launch':null)});
quota=new QuotaControl({root,audit,locked:()=>master.locked()});
master.quota=quota;
startupAdapter=new StartupAdapter({root,master,quota,launcher:orchestratorLauncher,providers:providerRuntime,
 verifyCodex:verifyCodexExecutable,getSessions:getLiveSessions,runtimeHealth:runtimeHealthy,
 operationBusy:()=>mutationLock||sustain.running||turbo.running||!!findWork?.running});
startup=new StartupController({root,deps:startupAdapter.deps(),audit,timeouts:{readMs:30000,actionMs:35000}});
// A server restart never authorizes replay of a saved restore operation.
if(master.restore.state.active)master.restore.pause();
if(startupBlockReason())master.disableAutomaticDispatch();
findWork=new FindWork({root,getState:state,command,audit,guard:()=>{
 if(master?.locked())return 'Emergency lockout is active';
 if(master?.drainState?.enabled)return 'Graceful wind-down is active';
 if(master?.restore?.state?.active)return 'Fleet restoration is active';
 return quota?.dispatchBlockReason()||startupBlockReason()||null;
}});
const server=http.createServer((req,res)=>{main(req,res).catch(e=>json(res,500,{error:e.message}))});
server.listen(port,listenHost,()=>console.log('MJobs Fleet Control listening on http://'+listenHost+':'+port));
