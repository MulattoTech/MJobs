'use strict';
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {retainStoppedTargets}=require('./snapshot-retention');
const {execFile}=require('node:child_process');
const {promisify,isDeepStrictEqual}=require('node:util');
const {attachWindowsStoppedEvidence,runtimeAbsentEvidence,createRestorationScope}=require('./windows-runtime-evidence');
const {readBranchAdoptions,matchesEntry,workspaceProofMatches,makeBranchReceipt,receiptForRuntime,digest}=require('./branch-adoptions');
const {readPostSubmissionReview,verifyHistoryEvidence,verifyCompletionAudits,pendingReviewMatches,nativeProofMatches,
 matchesPostReviewEntry,makePostReviewReceipt,postReviewReceiptForRuntime,exactIds}=require('./post-submission-review');

const execAsync=promisify(execFile);
const GiB=1024**3;
const PHASES=['idle','preparing','waiting_watchdog','starting_ao','restoring_primary','restoring_workers','verifying','paused_resources','paused_quota','paused','partial','complete','cancelled','failed'];
const DEFAULT_CONFIG={batchSize:2,tickSeconds:12,minFreeRamGiB:7,minFreeDiskGiB:20,maxCpuPercent:95,maxAttempts:3};
const INITIAL_STATE={active:false,phase:'idle',startedAt:null,completedAt:null,cancelledAt:null,source:null,desiredIds:[],restored:{},skipped:{},errors:{},inflight:{},branchAdoptionReceipts:{},postRestoreReviewReceipts:{},allowDispatchAfter:false,autoDispatchBefore:false,startedAO:false,fromEmergency:false,lastTick:null,lastError:null,aoStartLastAttempt:null,aoLaunches:0,stats:null};

const parseTime=value=>{const x=Date.parse(value||'');return Number.isFinite(x)?x:0};
const nowIso=()=>new Date().toISOString();
function read(file,fallback){try{return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch{return JSON.parse(JSON.stringify(fallback))}}
function atomic(file,body){
 const tmp=file+'.tmp';
 fs.writeFileSync(tmp,JSON.stringify(body,null,2),'utf8');
 fs.renameSync(tmp,file);
}
function detail(e){return String(e?.message||e).slice(0,500)}
function sessionsFromResponse(response){
 const entries=response?.sessions??response?.data?.sessions??[];
 if(!Array.isArray(entries))throw Error('Orchestrator session list is unavailable');
 return entries.filter(x=>x&&typeof x.id==='string'&&typeof x.projectId==='string'&&x.id.length>2);
}
function normalizeRecord(s){
 return {
  id:String(s.id),projectId:String(s.projectId),kind:String(s.kind||'worker'),
  harness:String(s.harness||''),displayName:String(s.displayName||s.id),
  model:String(s.model||''),mode:String(s.mode||''),
  branch:s.branch||null,isTerminated:!!s.isTerminated,
  ...(typeof s.createdAt==='string'&&Number.isFinite(Date.parse(s.createdAt))?{createdAt:s.createdAt}:{}),
  activity:String(s.activity?.state||s.status||'unknown'),
  observedAt:nowIso()
 };
}
function targetsFromSnapshot(snapshot){
 return (snapshot.sessions||[]).filter(s=>['codex','agy','gemini'].includes(s.harness)&&!s.isTerminated&&s.activity!=='exited')
  .filter((s,i,a)=>a.findIndex(x=>x.id===s.id)===i);
}
function hasSameRoot(sessions,record){
 return sessions.some(s=>s.projectId===record.projectId && s.kind==='orchestrator' && s.id!==record.id && !s.isTerminated && (s.activity?.state||s.activity||s.status)!=='exited');
}
function matchesRestoreTarget(target,session){
 return !!session&&session.id===target.id&&session.projectId===target.projectId&&
  ['mode','harness','kind','model'].every(key=>!target[key]||session[key]===target[key])&&
  ['branch','createdAt'].every(key=>!Object.hasOwn(target,key)||(target[key]??null)===(session[key]??null));
}
function findRestoreTarget(sessions,target){
 const matches=sessions.filter(s=>s.id===target.id);
 return matches.length===1&&matchesRestoreTarget(target,matches[0])?matches[0]:null;
}
function sameMutationIdentity(previous,current,allowExitTransition){
 const keys=['id','projectId','mode','harness','kind','model','branch','createdAt'];
 if(!allowExitTransition)keys.push('updatedAt','terminalGeneration','isTerminated','statusReadiness','provisionState','hibernatedAt','controllerState','controllerVerified');
 return keys.every(key=>(previous[key]??null)===(current[key]??null))&&
  (allowExitTransition||(previous.activity?.state||previous.status)===(current.activity?.state||current.status));
}
function unresolvedNativeOperations(state){
 const ids=new Set(Object.keys(state?.inflight||{}));
 for(const [id,error] of Object.entries(state?.errors||{}))if(error?.uncertain===true)ids.add(id);
 return [...ids];
}
function unresolvedNativeReason(state){
 const ids=unresolvedNativeOperations(state);
 return ids.length?'Previous native restore outcome is unverified for '+ids.slice(0,6).join(', ')+(ids.length>6?' and '+(ids.length-6)+' more':'')+'. Recheck Startup/current runtime health before any new restore; the previous submission ledger was preserved.':null;
}
function validConfig(c){
 for(const [k,min,max] of [['batchSize',1,5],['tickSeconds',8,60],['minFreeRamGiB',5,24],['minFreeDiskGiB',10,100],['maxCpuPercent',70,100],['maxAttempts',1,6]]){
  if(!Number.isInteger(c[k])||c[k]<min||c[k]>max)throw Error(k+' must be '+min+'–'+max);
 }
 return c;
}
class RestoreFleet {
 constructor({root,command,audit,locked=()=>false,dispatchOff=()=>{},
    getGlobalSessions=null,now=()=>Date.now(),launchAO=null,resourceReader=null,runtimeInspector=null,onComplete=null,snapshotFrozen=()=>false,watchdogInspector=null,
    autoSchedule=true,startBlockReason=()=>null,runtimeHealth=null,controllerStopped=()=>false,workspaceVerifier=null,reviewedRuntimeVerifier=null}){
  this.root=root;this.command=command;this.audit=audit;this.startBlockReason=startBlockReason;
  this.locked=locked;this.dispatchOff=dispatchOff;this.now=now;
  this.snapshotPath=path.join(root,'restore-snapshot.json');
  this.statePath=path.join(root,'restore-state.json');
  this.configPath=path.join(root,'restore-config.json');
  this.watchdogPath=path.join(root,'emergency-status.json');
  this.config=validConfig({...DEFAULT_CONFIG,...read(this.configPath,{})});
  this.state={...INITIAL_STATE,...read(this.statePath,{})};
  for(const key of ['desiredIds','restored','skipped','errors','inflight','branchAdoptionReceipts','postRestoreReviewReceipts']){
   if(key==='desiredIds'){if(!Array.isArray(this.state.desiredIds))this.state.desiredIds=[]}
   else if(!this.state[key]||typeof this.state[key]!=='object')this.state[key]={};
  }
  this.getGlobalSessions=getGlobalSessions||this.defaultGlobalSessions.bind(this);
  this.launchAO=launchAO||this.defaultLaunchAO.bind(this);
  this.resourceReader=resourceReader||this.defaultResourceReader.bind(this);
  this.runtimeInspector=runtimeInspector||this.defaultRuntimeInspector.bind(this);
  this.runtimeHealth=runtimeHealth||((s,count)=>!s.isTerminated&&['active','working','idle'].includes(s.activity?.state||s.status)&&count>0);
  this.controllerStopped=controllerStopped;
  this.workspaceVerifier=workspaceVerifier||((entry)=>require('./branch-workspace-evidence').verifyBranchWorkspace(entry));
  this.reviewedRuntimeVerifier=reviewedRuntimeVerifier||((entry)=>require('./post-review-native-runtime').verifyReviewedNativeRuntime(entry));
  this.reviewCompleting=false;
  this.onComplete=onComplete||(()=>{});
  this.snapshotFrozen=snapshotFrozen;
  this.watchdogInspector=watchdogInspector||this.defaultWatchdogInspector.bind(this);
  this.running=false;this.tickTimer=null;this.captureTimer=null;
  this.cpuPrev=os.cpus().map(c=>c.times);this.cpuPct=0;
  if(autoSchedule){
   this.captureTimer=setInterval(()=>{
    if(!this.state.active&&!this.locked()&&!this.snapshotFrozen())this.captureSnapshot().catch(()=>{});
   },45000);
   this.captureTimer.unref?.();
   if(this.state.active)this.schedule();
   if(!this.state.active&&!this.locked()&&!this.snapshotFrozen())setTimeout(()=>this.captureSnapshot().catch(()=>{}),900).unref?.();
  }
 }
 replacementIds(){
  const file=path.join(this.root,'replacement-state.json');
  try{const saved=JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''));if(!Array.isArray(saved.pairs))throw Error('Invalid replacement ledger');return new Set(saved.pairs.map(p=>p.oldId));}
  catch(e){if(e.code==='ENOENT')return new Set();throw Error('Replacement ledger unreadable; restoration is blocked to prevent duplicate workers');}
 }
 targets(snapshot){const replaced=this.replacementIds();return targetsFromSnapshot(snapshot).filter(s=>!s.replacedBy&&!replaced.has(s.id));}
 branchApproval(){const snapshot=this.getSnapshot();return readBranchAdoptions(this.root,snapshot,this.targets(snapshot),this.now(),value=>{this.branchAdoptionDiagnostic=value});}
 async runtimeTarget(target,session,count){
  if(matchesRestoreTarget(target,session))return {matches:true,adoption:null};
  if(!session||!this.runtimeHealth(session,count))return {matches:false,adoption:null};
  const approval=this.branchApproval(),bound=receiptForRuntime(approval,this.state,target,session,this.now());
  if(!bound)return this.postReviewedRuntimeTarget(target,session,count);
  const bindingFingerprint=()=>digest({receipt:this.state.branchAdoptionReceipts?.[target.id],source:this.state.source,
   startedAt:this.state.startedAt,inflight:this.state.inflight?.[target.id]});
  const expectedBinding=bindingFingerprint(),expectedReceiptId=bound.receipt.receiptId;
  const durableProof=workspaceProofMatches(bound.receipt.workspaceAfter,bound.entry,bound.receipt.submittedAt,this.now());
  const workspace=durableProof?bound.receipt.workspaceAfter:await this.workspaceVerifier(bound.entry);
  if(!workspaceProofMatches(workspace,bound.entry,bound.receipt.submittedAt,this.now()))return {matches:false,adoption:null};
  if(!durableProof){
   const live=(await this.getGlobalSessions()).filter(s=>s.id===session.id);
   if(live.length!==1||!matchesEntry(bound.entry,live[0],{expectedBranch:true})||!this.runtimeHealth(live[0],count))return {matches:false,adoption:null};
   const freshApproval=this.branchApproval(),fresh=receiptForRuntime(freshApproval,this.state,target,live[0],this.now());
   if(freshApproval?.revision!==approval.revision||!fresh||fresh.receipt.receiptId!==expectedReceiptId||bindingFingerprint()!==expectedBinding)return {matches:false,adoption:null};
  }
  // A read-only observation never edits the approval, manifest or submission
  // ledger. Restore.tick alone records verified receipts while observing health.
  return {matches:true,adoption:{receiptId:bound.receipt.receiptId,approvalRevision:approval.revision,workspace}};
 }
 postReview(){const snapshot=this.getSnapshot();return readPostSubmissionReview(this.root,snapshot,this.targets(snapshot),this.now());}
 async postReviewedRuntimeTarget(target,session,count){
  const review=this.postReview(),bound=postReviewReceiptForRuntime(review,this.state,target,session,this.now());
  if(!bound||!this.runtimeHealth(session,count))return {matches:false,adoption:null,postReview:null};
  const binding=()=>digest({receipt:this.state.postRestoreReviewReceipts?.[target.id],source:this.state.source,
   startedAt:this.state.startedAt,desiredIds:this.state.desiredIds,inflight:this.state.inflight?.[target.id],error:this.state.errors?.[target.id]});
  const expected=binding(),expectedReceipt=bound.receipt.receiptId;
  const [workspace,native]=await Promise.all([this.workspaceVerifier(bound.entry),this.reviewedRuntimeVerifier(bound.entry)]);
  if(!workspaceProofMatches(workspace,bound.entry,bound.receipt.completedAt,this.now())||
   this.now()-Date.parse(workspace.observedAt)>30000||!nativeProofMatches(native,bound.entry,bound.receipt.completedAt,this.now()))return {matches:false,adoption:null,postReview:null};
  const current=(await this.getGlobalSessions()).filter(value=>value.id===session.id),freshReview=this.postReview();
  if(current.length!==1||!this.runtimeHealth(current[0],count)||!matchesPostReviewEntry(bound.entry,current[0])||
   freshReview?.revision!==review.revision||binding()!==expected)return {matches:false,adoption:null,postReview:null};
  const fresh=postReviewReceiptForRuntime(freshReview,this.state,target,current[0],this.now());
  if(!fresh||fresh.receipt.receiptId!==expectedReceipt)return {matches:false,adoption:null,postReview:null};
  return {matches:true,adoption:null,postReview:{receiptId:expectedReceipt,reviewRevision:review.revision}};
 }
 durableReviewState(){
  const bytes=fs.readFileSync(this.statePath),disk=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
  if(!disk||typeof disk!=='object'||Array.isArray(disk)||typeof disk.active!=='boolean'||
   !disk.source||!Array.isArray(disk.desiredIds)||!disk.inflight||Array.isArray(disk.inflight)||typeof disk.inflight!=='object'||
   !disk.errors||Array.isArray(disk.errors)||typeof disk.errors!=='object')throw Error('Durable restore ledger is unavailable for post-submission review');
  if(!Object.hasOwn(disk,'postRestoreReviewReceipts')&&this.state.postRestoreReviewReceipts&&
   !Array.isArray(this.state.postRestoreReviewReceipts)&&Object.keys(this.state.postRestoreReviewReceipts).length===0)disk.postRestoreReviewReceipts={};
  if(!isDeepStrictEqual(disk,JSON.parse(JSON.stringify(this.state))))throw Error('Durable restore ledger differs from the current owner');
  return bytes;
 }
 async completeReviewed({confirm,runId,reviewRevision}={}, {assertSafe}={}){
  if(confirm!=='COMPLETE REVIEWED RESTORE'||typeof assertSafe!=='function')throw Error('Explicit protected post-submission completion is required');
  if(this.reviewCompleting||this.running||this.state.active)throw Error('A restore operation is still running');
  assertSafe();
  if(this.locked()||runId!==this.state.source?.startupRunId)throw Error('Post-submission completion must identify the paused current restore');
  const review=this.postReview();
  if(!review||review.revision!==reviewRevision||review.startupRunId!==runId)throw Error('The exact post-submission review revision is required');
  const bytes=this.durableReviewState();
  if(this.state.phase==='complete'&&unresolvedNativeOperations(this.state).length===0&&review.entries.every(entry=>{
   const target=this.state.source.targets.find(value=>value.id===entry.id);
   return target&&postReviewReceiptForRuntime(review,this.state,target,{...entry,branch:entry.expectedBranch,isTerminated:false,terminalGeneration:entry.nativeRuntime.terminalGeneration},this.now());
  }))return {...this.status(),idempotent:true,reviewCompleted:true};
  const expectedState=digest(this.state),expectedBytes=digest(bytes),startedAt=new Date(this.now()).toISOString();
  if(!pendingReviewMatches(review,this.state,bytes))throw Error('The review does not bind every exact pending native restore fence');
  const snapshot=this.getSnapshot(),targets=this.targets(snapshot);
  if(!this.restorationScope()||!exactIds(targets.map(target=>target.id),this.state.desiredIds))throw Error('The preserved restore source is not complete');
  const history=verifyHistoryEvidence(this.root,review),audits=verifyCompletionAudits(this.root,review);
  const check=()=>{
   assertSafe();
   if(this.locked()||this.running||this.state.active||digest(this.state)!==expectedState||digest(this.durableReviewState())!==expectedBytes||
    this.postReview()?.revision!==review.revision||!this.restorationScope())throw Error('Protected post-submission completion changed during inspection');
  };
  this.reviewCompleting=true;
  try{
   const proofs=await Promise.all(review.entries.map(async entry=>{
    const [workspace,native]=await Promise.all([this.workspaceVerifier(entry),this.reviewedRuntimeVerifier(entry)]);
    return {id:entry.id,workspace,native};
   }));check();
   const counts=await this.runtimeInspector([...new Set(targets.map(target=>target.projectId))],{restorationScope:this.restorationScope()});check();
   const sessions=await this.getGlobalSessions();check();
   if(!Array.isArray(sessions))throw Error('Current native sessions are unavailable');
   for(const target of targets){
    const found=sessions.filter(session=>session.id===target.id),current=found.length===1?found[0]:null;
    if(!current||!this.runtimeHealth(current,counts.get(target.id))||(target.kind==='orchestrator'&&hasSameRoot(sessions,target)))
     throw Error('Every preserved runtime must be healthy before reviewed completion');
    const entry=review.entries.find(value=>value.id===target.id);
    if(entry){
     const proof=proofs.find(value=>value.id===target.id);
     if(!matchesPostReviewEntry(entry,current)||!workspaceProofMatches(proof.workspace,entry,startedAt,this.now())||
      this.now()-Date.parse(proof.workspace.observedAt)>30000||!nativeProofMatches(proof.native,entry,startedAt,this.now()))
      throw Error('The reviewed native outcome or workspace changed before completion');
    }else{
     const match=await this.runtimeTarget(target,current,counts.get(target.id));check();
     if(!match.matches)throw Error('An unreviewed preserved identity does not match its healthy runtime');
    }
   }
   // Re-read the native DTO after all asynchronous receipt validation. No
   // mutable native generation, local review, source or fence can cross commit.
   const finalSessions=await this.getGlobalSessions();check();
   for(const target of targets){
    const before=sessions.filter(session=>session.id===target.id),after=finalSessions.filter(session=>session.id===target.id);
    if(before.length!==1||after.length!==1||!sameMutationIdentity(before[0],after[0],false)||!this.runtimeHealth(after[0],counts.get(target.id)))
     throw Error('Native runtime identity changed before reviewed completion');
   }
   if(targets.some(target=>target.kind==='orchestrator'&&hasSameRoot(finalSessions,target)))throw Error('A conflicting primary appeared during completion');
   verifyHistoryEvidence(this.root,review);verifyCompletionAudits(this.root,review);check();
   const now=this.now(),next=structuredClone(this.state);
   for(const entry of review.entries){
    const proof=proofs.find(value=>value.id===entry.id);
    next.postRestoreReviewReceipts[entry.id]=makePostReviewReceipt(review,entry,this.state,audits[entry.id],proof.workspace,proof.native,history[entry.id],now);
    delete next.inflight[entry.id];
   }
   for(const target of targets){
    next.restored[target.id]={status:'ready',at:new Date(now).toISOString(),action:review.entries.some(entry=>entry.id===target.id)?'post-submission-reviewed':'verified-online'};
    delete next.errors[target.id];
   }
   next.phase='complete';next.active=false;next.completedAt=new Date(now).toISOString();next.lastError=null;next.allowDispatchAfter=false;
   next.stats={...(next.stats||{}),tally:{ready:targets.length,waiting:0,missing:0,conflict:0,failed:0}};
   // Persist before updating the live owner: a failed atomic write must leave
   // both the original inflight fences and its in-memory view intact.
   atomic(this.statePath,next);this.state=next;this.schedule();
   this.log('post_submission_review_completed',{runId,reviewRevision,reviewedIds:review.entries.map(entry=>entry.id),sessions:targets.length});
   return {...this.status(),reviewCompleted:true};
  }finally{this.reviewCompleting=false;}
 }
 async prepareBranchAdoption(current){
  const approval=this.branchApproval(),entry=approval?.entries.find(x=>x.id===current.id);
  if(!entry)return null;
  if(this.state.source?.branchAdoptions?.revision!==approval.revision||!this.state.source.startupRunId||
   !matchesEntry(entry,current)||current.isTerminated!==true)throw Error('Branch adoption is not bound to this protected archived restore');
  const workspace=await this.workspaceVerifier(entry);
  if(!workspaceProofMatches(workspace,entry,approval.reviewedAt,this.now()))throw Error('The reviewed workspace branch could not be verified');
  return {approval,entry,workspace};
 }
 restorationScope(){
  const snapshot=this.getSnapshot(),targets=this.targets(snapshot),source=this.state.source;
  const exactIds=(left,right)=>Array.isArray(left)&&Array.isArray(right)&&left.length===right.length&&new Set(left).size===left.length&&new Set(right).size===right.length&&left.every(id=>typeof id==='string'&&right.includes(id));
  const ids=targets.map(s=>s.id);
  if(!source||source.snapshotAt!==snapshot.at||!Array.isArray(source.targets)||
   !exactIds(source.targets.map(s=>s?.id),ids)||!exactIds(this.state.desiredIds,ids))return null;
  // Older source records omitted branch/creation. The preserved snapshot still
  // binds those fields at the mutation boundary; do not widen to all archives.
  for(const target of targets){
   const previous=source.targets.find(s=>s.id===target.id);
   if(!['id','projectId','kind','harness','model','mode'].every(key=>(previous[key]??null)===(target[key]??null))||
    !['branch','createdAt'].every(key=>!Object.hasOwn(previous,key)||(previous[key]??null)===(target[key]??null)))return null;
  }
  return createRestorationScope(snapshot,targets);
 }
 save(){atomic(this.statePath,this.state)}
 log(event,value={}){this.audit('restore_'+event,value)}
 async defaultGlobalSessions(){
  const response=await fetch('http://127.0.0.1:3001/api/v1/sessions',{signal:AbortSignal.timeout(2500)});
  if(!response.ok)throw Error('Orchestrator daemon HTTP '+response.status);
  return sessionsFromResponse(await response.json());
 }
 async defaultLaunchAO(){
  const ao=path.join(os.homedir(),'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe');
  if(!fs.existsSync(ao))throw Error('Installed Orchestrator CLI not found: '+ao);
  const value=await execAsync(ao,['start','--json'],{windowsHide:true,timeout:25000,maxBuffer:160000});
  if(value.stderr?.includes('Error:'))throw Error(value.stderr.slice(0,350));
  return value.stdout||'start requested';
 }
 async defaultRuntimeInspector(projects,{restorationScope=null}={}){
  const ao=path.join(os.homedir(),'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe');
  const processes=new Map();
  for(const project of projects){
   const result=await execAsync(ao,['session','top','--all','--project',project,'--json'],{
    windowsHide:true,timeout:12000,maxBuffer:8*1024*1024
   });
   let data;try{data=JSON.parse(result.stdout)}catch{throw Error('Orchestrator top returned malformed JSON')}
   const rows=data.data;
   if(!Array.isArray(rows))throw Error('Orchestrator top did not return a session array');
   for(const row of rows){
    if(typeof row.id==='string'&&Object.hasOwn(row,'processCount')){
     if(processes.has(row.id))throw Error('Orchestrator top returned a duplicate session identity');
     // null, missing and malformed counts remain unknown. Native absence is a
     // separate, short-lived sidecar; it must never manufacture processCount=0.
     processes.set(row.id,row.processCount);
    }
   }
  }
  const requested=new Set(projects);
  const readSessions=async()=>(await this.getGlobalSessions()).filter(s=>requested.has(s.projectId));
  return attachWindowsStoppedEvidence(processes,await readSessions(),{root:this.root,now:this.now,readSessions,restorationScope});
 }
 async inspectMutationTarget(source,{allowExit=false,expectedSession=null,allowExitTransition=false}={}){
  const restorationScope=this.restorationScope();
  const counts=await this.runtimeInspector([source.projectId],{restorationScope});
  const live=await this.getGlobalSessions();
  const current=findRestoreTarget(live,source);
  if(!current)throw Error('The preserved session identity changed or is unavailable; native mutation was withheld');
  if(expectedSession&&!sameMutationIdentity(expectedSession,current,allowExitTransition))throw Error('The native session changed during runtime verification; mutation was withheld');
  if(source.kind==='orchestrator'&&hasSameRoot(live,source))throw Error('Another root is active; native mutation was withheld');
  const count=counts.get(source.id),state=current.activity?.state||current.status||'unknown';
  if(this.runtimeHealth(current,count))throw Error('The runtime is already healthy; no additional resume was submitted');
  const currentScope=this.restorationScope();
  const absent=runtimeAbsentEvidence(current,counts,this.now(),{restorationScope:currentScope});
  if(current.isTerminated&&(!restorationScope||restorationScope.fingerprint!==currentScope?.fingerprint||!absent))throw Error('Archived session requires fresh native absence for the unchanged preserved restoration target; no restore was submitted');
  const needsExit=!current.isTerminated&&state!=='exited'&&(count===0||(current.mode==='chat'&&this.controllerStopped(current)));
  if(allowExit&&needsExit&&Number.isInteger(count)&&count>=0)return {current,needsExit:true};
  if(!current.isTerminated&&state!=='exited')throw Error('The runtime is no longer verified stopped; resume was withheld');
  if(count!==0&&!absent)throw Error('Fresh native runtime absence is unavailable or contradicted; resume was withheld');
  return {current,needsExit:false};
 }
 async defaultResourceReader(){
  const avail=os.freemem()/GiB;
  let freeDiskGiB=null;
  try{
   const disk=fs.statfsSync(this.root);
   freeDiskGiB=disk.bavail*disk.bsize/GiB;
  }catch{}
  const cpu=os.cpus().map(x=>x.times);
  let total=0,busy=0;
  for(let i=0;i<cpu.length;i++){
   const p=this.cpuPrev[i],c=cpu[i];if(!p)continue;
   const keys=['user','nice','sys','idle','irq'];
   const t=keys.reduce((s,k)=>s+Math.max(0,c[k]-p[k]),0);
   total+=t;busy+=t-Math.max(0,c.idle-p.idle);
  }
  this.cpuPrev=cpu;
  if(total>0)this.cpuPct=Math.round(100*busy/total);
  return {ramGiB:+avail.toFixed(1),diskGiB:freeDiskGiB===null?null:+freeDiskGiB.toFixed(1),cpuPercent:this.cpuPct};
 }
 async captureSnapshot({force=false}={}){
  const pendingRecovery=Array.isArray(this.state.desiredIds)&&this.state.desiredIds.length>0 &&
   ['paused','preparing','waiting_watchdog','starting_ao','restoring_primary','restoring_workers',
    'verifying','paused_resources','paused_quota','partial','failed','cancelled'].includes(this.state.phase);
  const blocked=()=>this.state.active||this.reviewCompleting||this.locked()||(!force&&(this.snapshotFrozen()||pendingRecovery));
  if(blocked())return null;
  const previous=this.getSnapshot(),beforeState=digest(this.state),beforeSnapshot=digest(previous);
  let entries;
  try{entries=await this.getGlobalSessions()}catch{return null}
  if(!entries?.length||blocked()||digest(this.state)!==beforeState||digest(this.getSnapshot())!==beforeSnapshot)return null;
  const observedAt=nowIso(),oldTargets=this.targets(previous);
  const sessions=retainStoppedTargets(oldTargets,entries.map(normalizeRecord),observedAt);
  const nextTargets=this.targets({sessions});
  // Never replace a primary-bearing recovery roster with orphaned workers.
  for(const primary of oldTargets.filter(s=>s.kind==='orchestrator')){
   const group=nextTargets.filter(s=>s.projectId===primary.projectId);
   if(group.length&&group.filter(s=>s.kind==='orchestrator').length!==1)return null;
  }
  const snapshot={at:nowIso(),kind:'AO-preserved-session-manifest-v1',scope:'AO-managed-Codex-AGY-Gemini-sessions',
    sessions,restorableCount:this.targets({sessions}).length};
  if(snapshot.restorableCount===0)return null;
  atomic(this.snapshotPath,snapshot);
  return {at:snapshot.at,restorableCount:snapshot.restorableCount};
 }
 getSnapshot(){return read(this.snapshotPath,{at:null,sessions:[],restorableCount:0})}
 async preview(){
  const saved=this.getSnapshot();
  const snap=this.targets(saved);
  let live=[],daemonReachable=false;
  try{live=await this.getGlobalSessions();daemonReachable=true}catch{}
  const resource=await this.resourceReader();
  let processCounts=null;
  if(daemonReachable){try{processCounts=await this.runtimeInspector([...new Set(live.map(s=>s.projectId))])}catch{}}
  const snapshotReady=snap.length>0;
  const desired=snapshotReady?snap:this.targets({sessions:live.map(normalizeRecord)});
  const work=desired.map(s=>{
   const match=live.find(x=>x.id===s.id&&x.projectId===s.projectId);
   const activity=match?.activity?.state||'unknown';
   let action='resume';
   if(!match){action='unavailable';}
   else if(match.isTerminated){action='restore';}
   else if(activity==='exited'){action='resume';}
   else if(['active','working','idle'].includes(activity)){
    action=processCounts?.has(s.id) ? (this.runtimeHealth(match,processCounts.get(s.id))?'already-online':processCounts.get(s.id)===0?'restart-runtime':'inspect') : 'verify-runtime';
   }
   else{action='inspect';}
   if(s.kind==='orchestrator'&&hasSameRoot(live,s))action='root-conflict';
   return {id:s.id,projectId:s.projectId,role:s.kind,name:s.displayName,action,activity};
  });
  const actionable=work.filter(x=>x.action==='resume'||x.action==='restore'||x.action==='restart-runtime'||x.action==='verify-runtime'||x.action==='inspect'||x.action==='unavailable').length;
  return {
   emergencyLatched:this.locked(),
   savedAt:saved.at,
   snapshotAvailable:snapshotReady,
   liveDaemon:daemonReachable,
   totalTargets:work.length,
   alreadyOnline:work.filter(x=>x.action==='already-online').length,
   willResume:actionable,
   rootConflicts:work.filter(x=>x.action==='root-conflict'),
   resources:resource,
   working:work,
   note:daemonReachable?'Existing session identities and worktrees will be preserved.':'Orchestrator will be launched before restoring its recorded sessions.'
  };
 }
 status(){
  const restoredCount=Object.values(this.state.restored).filter(x=>x.status==='ready').length;
  return {
   active:!!this.state.active,phase:this.state.phase,
   startedAt:this.state.startedAt,completedAt:this.state.completedAt,
   lastTick:this.state.lastTick,lastError:this.state.lastError,
   nextCheckSeconds:this.config.tickSeconds,
   snapshotAt:this.getSnapshot().at,
   total:this.state.desiredIds.length,
   restoredCount,
   skippedCount:Object.keys(this.state.skipped).length,
   errorCount:Object.keys(this.state.errors).length,
   errors:Object.entries(this.state.errors).slice(-8).map(([id,val])=>({id,...val})),
   recentlyRestored:Object.entries(this.state.restored).slice(-10).map(([id,val])=>({id,...val})),
   autoDispatchAfter:!!this.state.allowDispatchAfter,
   startedAO:!!this.state.startedAO,
   resource:this.state.stats?.resource||null,
   watchdogWait:this.state.phase==='waiting_watchdog',
   config:this.config,
   branchAdoptionDiagnostic:this.branchAdoptionDiagnostic||null
  };
 }
 start({autoDispatchAfter=false,manifest=null,fromEmergency=false,startupRunId=null,branchAdoptionsRevision=null}={}){
  if(this.reviewCompleting)throw Error('Post-submission completion is still being verified');
  if(this.state.active)return this.status();
  if(this.running)throw Error('A previous restore tick is still running; no new restore was submitted');
  const previousNative=unresolvedNativeReason(this.state);
  if(previousNative)throw Error(previousNative);
  if(this.locked())throw Error('Emergency lockout must be explicitly released before restoring');
  if(typeof autoDispatchAfter!=='boolean')throw Error('autoDispatchAfter must be boolean');
  const snap=manifest||this.getSnapshot();
  const targets=this.targets(snap);
  if(!targets.length)throw Error('No previously active supported AO session snapshot available. Refresh the restore preview before continuing.');
  const roots=targets.filter(x=>x.kind==='orchestrator');
  const keys=new Set();
  for(const r of roots){if(keys.has(r.projectId))throw Error('Multiple root sessions in the recovery manifest for '+r.projectId);keys.add(r.projectId)}
  const approval=readBranchAdoptions(this.root,snap,targets,this.now());
  if((approval?.revision||null)!==branchAdoptionsRevision||(approval&&(!startupRunId||typeof startupRunId!=='string')))
   throw Error('Reviewed branch adoption requires the exact new protected Startup binding');
  const carried={},carriedHashes={},carriedPost={},carriedPostHashes={};
  if(approval&&this.state.source?.branchAdoptions?.revision===approval.revision){
   for(const [id,receipt]of Object.entries(this.state.branchAdoptionReceipts||{})){
    const target=targets.find(s=>s.id===id);
    if(receipt.status==='verified'&&target&&receiptForRuntime(approval,this.state,target,{...receipt.entry,branch:receipt.entry.expectedBranch,isTerminated:false},this.now())){
     carried[id]=structuredClone(receipt);carriedHashes[id]=digest(receipt);
    }
   }
  }
  const postReview=readPostSubmissionReview(this.root,snap,targets,this.now());
  if(postReview){
   for(const [id,receipt]of Object.entries(this.state.postRestoreReviewReceipts||{})){
    const target=targets.find(value=>value.id===id),entry=postReview.entries.find(value=>value.id===id);
    if(target&&entry&&postReviewReceiptForRuntime(postReview,this.state,target,{...entry,branch:entry.expectedBranch,isTerminated:false,terminalGeneration:entry.nativeRuntime.terminalGeneration},this.now())){
     carriedPost[id]=structuredClone(receipt);carriedPostHashes[id]=digest(receipt);
    }
   }
  }
  this.dispatchOff();
  this.state={...INITIAL_STATE,
   active:true,phase:'preparing',startedAt:nowIso(),
   source:{snapshotAt:snap.at,targets:targets.map(x=>({id:x.id,projectId:x.projectId,kind:x.kind,harness:x.harness,model:x.model,mode:x.mode,branch:x.branch??null,
    ...(typeof x.createdAt==='string'?{createdAt:x.createdAt}:{}),displayName:x.displayName})),
    ...(startupRunId?{startupRunId}:{}),...(approval?{branchAdoptions:{revision:approval.revision,snapshotSha256:approval.snapshotSha256},carriedBranchReceipts:carriedHashes}:{}),
    ...(Object.keys(carriedPostHashes).length?{carriedPostRestoreReviews:carriedPostHashes}:{})},
   branchAdoptionReceipts:carried,
   postRestoreReviewReceipts:carriedPost,
   desiredIds:targets.map(x=>x.id),allowDispatchAfter:autoDispatchAfter,fromEmergency:fromEmergency===true
  };
  this.save();this.log('start',{targets:targets.length,autoDispatchAfter});
  this.schedule();
  setTimeout(()=>this.tick().catch(e=>this.failure(e)),50).unref?.();
  return this.status();
 }
 schedule(){
  if(this.tickTimer)clearInterval(this.tickTimer);
  if(!this.state.active){this.tickTimer=null;return}
  this.tickTimer=setInterval(()=>this.tick().catch(e=>this.failure(e)),this.config.tickSeconds*1000);
  this.tickTimer.unref?.();
 }
 pause(){
  if(!this.state.active)return this.status();
  this.state.active=false;this.state.phase='paused';
  this.state.cancelledAt=nowIso();this.save();
  if(this.tickTimer)clearInterval(this.tickTimer);this.tickTimer=null;
  this.log('paused',{});
  return this.status();
 }
 failure(e){
  this.state.lastError=detail(e);
  this.state.phase='partial';
  this.state.lastTick=nowIso();
  this.save();this.log('error',{message:detail(e)});
 }
 async defaultWatchdogInspector(){
  const script=path.join(this.root,'Check-AO-Watchdog.ps1');
  if(!fs.existsSync(script))throw Error('Missing watchdog process inspection script');
  const exe='C:\\Program Files\\PowerShell\\7\\pwsh.exe';
  const result=await execAsync(exe,['-NoProfile','-NonInteractive','-File',script],{
   windowsHide:true,timeout:9000,maxBuffer:20000
  });
  const data=JSON.parse(result.stdout.trim());
  if(typeof data.active!=='boolean'||!Array.isArray(data.watchdogPids))
   throw Error('Invalid watchdog process-inspection result');
  return data;
 }
 async watchdogDisarmed(requireExplicit=false){
  const e=read(this.watchdogPath,{});
  // The emergency latch is authoritative; never disarm an active emergency.
  if(this.locked())return false;
  const confirmedAge=this.now()-parseTime(e.checkedAt);
  if(e.phase==='disarmed'&&e.active===false&&!requireExplicit&&confirmedAge>=0&&confirmedAge<15000)return true;
  if(!requireExplicit&&(!e.startedAt&&!e.watchdogPid&&!e.phase))return true;
  if(!requireExplicit&&e.phase==='inactive')return true;
  // A reboot may leave "starting" in a persistent file after the process died.
  // Reconcile it only after confirming no emergency worker is alive.
  const age=this.now()-parseTime(e.checkedAt||e.startedAt);
  if(e.phase!=='disarmed'&&(!Number.isFinite(age)||age<15000))return false;
  try{
   const inspected=await this.watchdogInspector();
   if(inspected.active)return false;
   if(this.locked())return false;
   const updated={...e,phase:'disarmed',active:false,watchdogPid:null,
     checkedAt:nowIso(),disarmedReason:'Watchdog process verified absent after emergency latch released'};
   atomic(this.watchdogPath,updated);
   this.log('stale_watchdog_reconciled',{previousPhase:e.phase||'unknown'});
   return true;
  }catch(e){
   this.state.lastError='Emergency watchdog could not be verified: '+detail(e);
   this.save();
   return false;
  }
 }
 async tick(){
  if(!this.state.active||this.running)return {skipped:'inactive or busy'};
  this.running=true;
  try{
   this.state.lastTick=nowIso();
   if(this.locked()){
    this.state.phase='waiting_watchdog';
    this.save();return {paused:'Emergency latch remains armed'};
   }
   const startBlocked=this.startBlockReason();
   if(startBlocked){this.state.phase='paused_quota';this.state.lastError=startBlocked;this.save();return {paused:startBlocked};}
   if(!(await this.watchdogDisarmed(this.state.fromEmergency===true))){
    this.state.phase='waiting_watchdog';
    this.save();return {paused:'Waiting for emergency watchdog to disarm'};
   }
   let live;
   try{live=await this.getGlobalSessions()}
   catch{
    this.state.phase='starting_ao';
    const now=this.now();
    if(now-parseTime(this.state.aoStartLastAttempt)>90000||!this.state.aoStartLastAttempt){
     this.state.aoStartLastAttempt=nowIso();
     this.state.aoLaunches++;
     this.save();
     try{await this.launchAO();this.state.startedAO=true;this.log('ao_started',{attempt:this.state.aoLaunches})}
     catch(e){this.state.lastError='Could not start Orchestrator.inc: '+detail(e);this.log('ao_start_failed',{message:detail(e)})}
    }
    this.save();
    return {waiting:'Starting Orchestrator.inc daemon'};
   }
   const superseded=this.replacementIds();
   const expected=(this.state.source?.targets||[]).filter(s=>!superseded.has(s.id));
   for(const s of (this.state.source?.targets||[])){if(superseded.has(s.id))this.state.skipped[s.id]={reason:'Preserved original has a verified replacement'};}
   if(!expected.length)throw Error('Recovery manifest has no sessions');
   let processCounts;
   const restorationScope=this.restorationScope();
   try{processCounts=await this.runtimeInspector([...new Set(expected.map(x=>x.projectId))],{restorationScope})}
   catch(e){this.state.phase='partial';this.state.lastError='Cannot verify session process trees: '+detail(e);this.save();return {waiting:'Waiting for reliable AO runtime inspection'}}
   const first=expected.filter(s=>s.kind==='orchestrator').sort((a,b)=>(a.projectId==='mjobs-luna'?0:1)-(b.projectId==='mjobs-luna'?0:1));
   const second=expected.filter(s=>s.kind!=='orchestrator');
   const pending=[];
   const readyNow=new Set();
   const tally={ready:0,waiting:0,missing:0,conflict:0,failed:0};
   for(const s of [...first,...second]){
    const sameId=live.filter(session=>session.id===s.id),candidate=sameId.length===1?sameId[0]:null;
    const match=await this.runtimeTarget(s,candidate,processCounts.get(s.id));
    const current=match.matches?candidate:null;
    if(!current){tally.missing++;continue}
    if(s.kind==='orchestrator'&&hasSameRoot(live,s)){
      this.state.errors[s.id]={...(this.state.errors[s.id]||{}),message:'Another root is already active for this project; duplicate root prevented'};
      tally.conflict++;continue;
    }
    const state=current.activity?.state||current.status||'unknown';
    const count=processCounts.get(s.id);
    const currentScope=this.restorationScope();
    const absent=runtimeAbsentEvidence(current,processCounts,this.now(),{restorationScope:currentScope});
    if(current.isTerminated&&(!restorationScope||restorationScope.fingerprint!==currentScope?.fingerprint||!absent)){
     this.state.errors[s.id]={...(this.state.errors[s.id]||{}),message:'Archived session native absence is unverified for the preserved restore target; no restore was submitted',at:nowIso()};
     tally.waiting++;continue;
    }
    if((!Number.isInteger(count)||count<0)&&!absent){
     this.state.errors[s.id]={...(this.state.errors[s.id]||{}),message:'Runtime process evidence is unavailable; no resume was submitted',at:nowIso()};
     tally.waiting++;continue;
    }
    if(this.runtimeHealth(current,processCounts.get(s.id))){
     if(match.adoption){
      const receipt=this.state.branchAdoptionReceipts[s.id];
      if(receipt.status!=='verified')Object.assign(receipt,{status:'verified',verifiedAt:new Date(this.now()).toISOString(),workspaceAfter:match.adoption.workspace});
     }
     this.state.restored[s.id]={status:'ready',at:nowIso(),action:'already-online'};
     delete this.state.inflight[s.id];
     delete this.state.errors[s.id];
     readyNow.add(s.id);tally.ready++;continue;
    }
    delete this.state.restored[s.id];
    let attempts=this.state.errors[s.id]?.attempts||0;
    if(attempts>=this.config.maxAttempts){tally.failed++;continue}
    if(this.state.inflight[s.id]){
      const age=this.now()-parseTime(this.state.inflight[s.id].at);
      if(age<35000){tally.waiting++;continue}
      // A submitted command may have succeeded despite a timeout. Keep its
      // identity and verify on later ticks; never blindly submit it again.
      this.state.errors[s.id]={attempts,uncertain:true,message:'Resume was submitted but a healthy runtime has not been confirmed. No duplicate resume was sent.',at:nowIso()};
      tally.waiting++;continue;
    }
    if(this.state.errors[s.id]?.uncertain){tally.waiting++;continue;}
    if(processCounts.get(s.id)>0&&!current.isTerminated&&state!=='exited'&&!this.controllerStopped(current)){
      this.state.errors[s.id]={attempts,uncertain:true,message:'A runtime exists but its controller is not healthy. Inspect it before resuming; no duplicate controller was started.',at:nowIso()};
      tally.waiting++;continue;
    }
    pending.push({source:s,current,needsExit:!current.isTerminated&&state!=='exited'&&(processCounts.get(s.id)===0||this.controllerStopped(current))});
   }
   const rootsPending=pending.filter(x=>x.source.kind==='orchestrator');
   const unresolvedRoots=first.filter(s=>!readyNow.has(s.id));
   if(unresolvedRoots.length&&!rootsPending.length){
    this.state.phase=tally.failed||tally.conflict||tally.missing?'partial':'verifying';
    this.state.lastError='Workers held until primary controllers are healthy: '+unresolvedRoots.map(s=>s.id).join(', ');
    this.state.stats={tally};this.save();
    return {waiting:true,tally,blockedRoots:unresolvedRoots.map(s=>s.id)};
   }
   if(rootsPending.length){
    this.state.phase='restoring_primary';
   }else if(pending.length){
    this.state.phase='restoring_workers';
   }else if(tally.ready===expected.length){
    this.state.phase='complete';this.state.active=false;
    this.state.completedAt=nowIso();
    this.state.lastError=null;
    this.save();this.schedule();
    this.log('completed',{sessions:tally.ready});
    if(this.state.allowDispatchAfter){
      try{await this.onComplete({autoDispatchAfter:true})}
      catch(e){this.state.lastError='Sessions restored but dispatch could not restart: '+detail(e);this.save()}
    }
    return {complete:true,restored:tally.ready};
   }else{
    this.state.phase=tally.failed||tally.conflict||tally.missing?'partial':'verifying';
    this.state.stats={tally};
    this.save();
    return {waiting:true,tally};
   }
   const resource=await this.resourceReader();
   this.state.stats={tally,resource};
   let guard=null;
   if(!Number.isFinite(resource.ramGiB)||!Number.isFinite(resource.diskGiB)||!Number.isFinite(resource.cpuPercent))guard='Current RAM, disk and CPU measurements are required before restoring another runtime';
   else if(resource.ramGiB<this.config.minFreeRamGiB)guard='RAM headroom is only '+resource.ramGiB+' GiB';
   else if(resource.diskGiB<this.config.minFreeDiskGiB)guard='Disk free is only '+resource.diskGiB+' GiB';
   else if(resource.cpuPercent>=this.config.maxCpuPercent)guard='CPU load is '+resource.cpuPercent+'%';
   if(guard){
    this.state.phase='paused_resources';this.state.lastError=guard;this.save();
    return {paused:guard};
   }
   const batch=(rootsPending.length?rootsPending:pending).slice(0,this.config.batchSize);
   const submitted=[];
   // Process one by one to avoid competing worktree startups.
   for(const job of batch){
    if(!this.state.active||this.locked())break;
    const blockedNow=this.startBlockReason();
    if(blockedNow){this.state.phase='paused_quota';this.state.lastError=blockedNow;this.save();break;}
    const {source:s}=job;
    let nativeSubmitted=false;
    try{
     // Poll-time absence is not a permit. Re-read counts, native ownership and
     // exact session identity immediately before any native command. The native
     // operation/host locks remain the final concurrency authority.
     let boundary=await this.inspectMutationTarget(s,{allowExit:job.needsExit,expectedSession:job.current});
     const adoption=boundary.current.isTerminated?await this.prepareBranchAdoption(boundary.current):null;
     if(adoption)boundary=await this.inspectMutationTarget(s,{expectedSession:boundary.current});
     if(!this.state.active||this.locked()||this.startBlockReason())break;
     if(this.state.inflight[s.id]||this.state.errors[s.id]?.uncertain)continue;
     let adoptionReceipt=null;
     if(adoption){
      if(this.branchApproval()?.revision!==adoption.approval.revision)throw Error('Reviewed branch adoption changed before native submission');
      adoptionReceipt=makeBranchReceipt(adoption.approval,adoption.entry,boundary.current,this.state.source,this.state.startedAt,adoption.workspace,this.now());
     }
     this.state.inflight[s.id]={at:nowIso(),command:boundary.current.isTerminated?'restore':'resume-agent'};
     if(adoptionReceipt){this.state.branchAdoptionReceipts[s.id]=adoptionReceipt;this.state.inflight[s.id].branchAdoptionReceiptId=adoptionReceipt.receiptId;}
     this.save();
     if(boundary.needsExit){
      nativeSubmitted=true;
      await this.command(['session','exit-agent',s.id,'--project',s.projectId,'--json'],15000);
      boundary=await this.inspectMutationTarget(s,{expectedSession:boundary.current,allowExitTransition:true});
     }
     if(!this.state.active||this.locked()||this.startBlockReason()){
      delete this.state.inflight[s.id];
      this.log('resume_withheld_after_exit',{id:s.id});
      this.save();break;
     }
     const subcommand=boundary.current.isTerminated?'restore':'resume-agent';
     const args=['session',subcommand,s.id,'--project',s.projectId];
     if(subcommand==='resume-agent')args.push('--json');
     this.state.inflight[s.id].command=subcommand;this.save();
     nativeSubmitted=true;
     await this.command(args,25000);
     if(adoption){
      const receipt=this.state.branchAdoptionReceipts[s.id];receipt.status='command_succeeded';this.save();
      const after=await this.workspaceVerifier(adoption.entry);
      if(!workspaceProofMatches(after,adoption.entry,receipt.submittedAt,this.now()))throw Error('Restored workspace did not match the reviewed branch');
      receipt.workspaceAfter=after;this.save();
     }
     submitted.push(s.id);
     this.log('resume_submitted',{id:s.id,project:s.projectId,action:subcommand});
    }catch(e){
     const previous=this.state.errors[s.id]||{};
     const attempts=(previous.attempts||0)+(nativeSubmitted?1:0);
     this.state.errors[s.id]={...previous,attempts,...(nativeSubmitted?{uncertain:true}:{}),message:(nativeSubmitted?'Native command outcome is unverified; no automatic retry. ':'Fresh runtime verification blocked native mutation; no resume was submitted. ')+detail(e),at:nowIso()};
     this.log(nativeSubmitted?'resume_error':'resume_withheld',{id:s.id,attempts,message:detail(e)});
    }
    this.save();
   }
   return {submitted,phase:this.state.phase,tally};
  }catch(e){this.failure(e);return {error:detail(e)}}
  finally{this.running=false}
 }
}
module.exports={RestoreFleet,INITIAL_STATE,PHASES,targetsFromSnapshot,validConfig,unresolvedNativeOperations,unresolvedNativeReason,matchesRestoreTarget};
