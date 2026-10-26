'use strict';

const fs=require('node:fs');
const path=require('node:path');
const {isDeepStrictEqual}=require('node:util');
const {createHash}=require('node:crypto');
const {configRevision}=require('./quota-guard');
const {unresolvedNativeOperations,unresolvedNativeReason,matchesRestoreTarget}=require('./restore-fleet');
const {runtimeAbsentEvidence,nativeSessionIdentity,createRestorationScope}=require('./windows-runtime-evidence');

const exactIds=(left,right)=>Array.isArray(left)&&Array.isArray(right)&&left.every(id=>typeof id==='string')&&right.every(id=>typeof id==='string')&&new Set(left).size===left.length&&new Set(right).size===right.length&&JSON.stringify([...left].sort())===JSON.stringify([...right].sort());
const stateFingerprint=value=>createHash('sha256').update(JSON.stringify(value)).digest('hex');
function verifiedStopped(session,counts,now,restorationScope){
 if(runtimeAbsentEvidence(session,counts,now,{restorationScope}))return true;
 const identity=nativeSessionIdentity(session);
 return counts.get(session?.id)===0&&!!identity&&!identity.isTerminated&&!identity.hibernatedAt&&identity.provisionState==='ready'&&identity.statusReadiness==='ready'&&identity.activity==='exited'&&
  (identity.mode==='tui'||identity.controllerVerified&&identity.controllerState==='stopped');
}

// This adapter joins existing safety and restoration controllers. It never
// creates a second scheduler and never treats saved completion as live health.
class StartupAdapter {
 constructor({root,master,quota,launcher,providers,verifyCodex,getSessions,
  runtimeHealth,operationBusy=()=>false,now=Date.now,readinessWaitMs=1000}) {
  if(!Number.isFinite(readinessWaitMs)||readinessWaitMs<1||readinessWaitMs>5000)throw Error('Readiness response wait must be between 1 and 5000 ms');
  Object.assign(this,{root,master,quota,launcher,providers,verifyCodex,getSessions,runtimeHealth,operationBusy,now});
  this.live=null;this.liveAt=0;this.reading=null;this.inspectionSerial=0;this.readinessWaitMs=readinessWaitMs;
 }
 check(ctx){if(ctx?.signal?.aborted)throw Error('Startup operation was cancelled before the next action');}
 emergency(){
  const bytes=fs.readFileSync(path.join(this.root,'emergency-latch.json'));
  const value=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
  if(typeof value.enabled!=='boolean')throw Error('Emergency state is invalid');
  return {latched:value.enabled,revision:createHash('sha256').update(bytes).digest('hex')};
 }
 safety(ctx){
  this.check(ctx);const s=this.quota.status();
  return {quota:{fresh:s.fresh,remainingPercent:s.selectedRemainingPercent,
   ordinaryUsageAllowed:s.ordinaryUsageAllowed,armed:s.config.autoKillArmed,
   tripped:s.config.tripped,thresholdPercent:s.config.thresholdPercent,
   monitorEnabled:s.config.enabled,pollSeconds:s.config.pollSeconds,window:s.config.window},
   emergency:this.emergency(),dispatchPaused:!this.master.sustain.config.enabled&&!this.master.turbo.config.enabled};
 }
 safeForMutation(ctx,{allowLatched=false,requireGuard=false}={}){
  this.check(ctx);const s=this.safety(ctx);
  if(s.quota.fresh!==true||!Number.isFinite(s.quota.remainingPercent)||s.quota.remainingPercent<=15||s.quota.remainingPercent>100||s.quota.ordinaryUsageAllowed===false)throw Error('Fresh Codex allowance above 15% is required');
  if(s.emergency.latched&&!allowLatched)throw Error('Emergency stop was engaged; no launch performed');
  if(!s.dispatchPaused)throw Error('Automatic dispatch must remain paused during Startup');
  if(requireGuard){
   const reason=this.quota.dispatchBlockReason();if(reason)throw Error(reason);
   if(s.quota.thresholdPercent!==7||s.quota.pollSeconds!==5||s.quota.window!=='any')throw Error('Startup guard settings changed');
   if(ctx?.guardReceipt?.token!==configRevision(this.quota.config()))throw Error('Startup guard revision changed');
  }
  return s;
 }
 manifest(){
  const saved=this.master.restore.getSnapshot();
  if(!Array.isArray(saved.sessions)||!saved.sessions.length)throw Error('No preserved fleet manifest is available');
  const ids=new Set();
  for(const s of saved.sessions){
   if(typeof s.id!=='string'||!/^[a-z0-9-]{3,90}$/.test(s.id)||typeof s.projectId!=='string'||!/^[a-z0-9-]{3,90}$/.test(s.projectId))throw Error('Saved session identity is invalid');
   if(ids.has(s.id))throw Error('Duplicate session identity in the saved manifest');ids.add(s.id);
  }
  const targets=this.master.restore.targets(saved);
  if(!targets.length)throw Error('No supported preserved sessions can be restored');
  for(const project of new Set(targets.map(x=>x.projectId))){
   const roots=targets.filter(x=>x.projectId===project&&x.kind==='orchestrator');
   if(roots.length!==1)throw Error('Exactly one preserved primary is required for project '+project);
  }
  const identities=targets.map(s=>({id:s.id,projectId:s.projectId,kind:s.kind,harness:s.harness,model:s.model,mode:s.mode,branch:s.branch||null,
   ...(typeof s.createdAt==='string'?{createdAt:s.createdAt}:{})})).sort((a,b)=>a.id.localeCompare(b.id));
  const revision=createHash('sha256').update(JSON.stringify(identities)).digest('hex');
  return {saved,targets,revision};
 }
 async bootstrap(ctx){
  this.check(ctx);
  try{
   if(this.operationBusy())throw Error('Another agent or dispatch control operation is running');
   if(this.master.drainState.enabled)throw Error('Graceful wind-down must finish before Startup');
   if(this.master.restore.state.active||this.master.restore.running)throw Error('A fleet restore operation is already running');
   const {targets,revision}=this.manifest();
   const approval=this.master.restore.branchApproval?.()||null;
   const manifest={targetCount:targets.length,manifestRevision:revision,targetIds:targets.map(s=>s.id),primaryIds:targets.filter(s=>s.kind==='orchestrator').map(s=>s.id),
    branchAdoptionsRevision:approval?.revision||null};
   // Preserve these identities on an uncertain prior operation so the explicit
   // Recheck path can adopt verified health without replaying its submission.
   const needsRecheck=reason=>({ok:false,...manifest,reconciliationRequired:true,reason:String(reason).slice(0,450)});
   const previousNative=unresolvedNativeReason(this.master.restore.state);
   if(previousNative)return needsRecheck(previousNative);
   const q=this.quota.status();
   if(q.fresh!==true||!Number.isFinite(q.selectedRemainingPercent)||q.selectedRemainingPercent<=15||q.selectedRemainingPercent>100||q.ordinaryUsageAllowed===false)throw Error('Fresh Codex allowance above 15% is required');
   const e=this.master.status().emergency;
   if(e.latched){
    const age=this.now()-Date.parse(e.checkedAt||'');
    if(!Number.isFinite(age)||age<0||age>=35000||e.remainingTotal!==0||e.phase!=='holding_zero')throw Error('Emergency shutdown must be freshly verified at zero target processes');
   }
   if(targets.some(s=>s.harness==='codex'))await this.verifyCodex();
   const catalog=await this.providers.get();
   for(const s of targets){
    const provider=catalog.providers.find(p=>p.id===s.harness);
    if(!provider?.available)throw Error('The preserved '+s.harness+' harness is unavailable');
   }
   this.check(ctx);
   const resource=await this.master.restore.resourceReader(),c=this.master.restore.config;
   if(!Number.isFinite(resource.ramGiB)||resource.ramGiB<c.minFreeRamGiB)throw Error('Insufficient verified RAM headroom for restoration');
   if(!Number.isFinite(resource.diskGiB)||resource.diskGiB<c.minFreeDiskGiB)throw Error('Insufficient verified disk space for restoration');
   if(!Number.isFinite(resource.cpuPercent)||resource.cpuPercent>=c.maxCpuPercent)throw Error('CPU load is too high or unavailable for restoration');
   this.check(ctx);
   // This only retires an already-verified terminal nonce. Cancellation still
   // requires explicit Recheck; bootstrap never triggers a native task here.
   const launch=this.launcher.reconcile();
   if(launch.pending||launch.requiresReconciliation)return needsRecheck('A prior Orchestrator launch needs verification. Use Recheck safely; no launch will be replayed.');
   const current=await this.readiness(ctx,{force:true});
   const nativeAfterPreview=unresolvedNativeReason(this.master.restore.state);
   if(nativeAfterPreview)return needsRecheck(nativeAfterPreview);
   if(current.conflicts?.length)throw Error('Another primary controller conflicts with the saved fleet');
   return {ok:true,...manifest,
    alreadyOnline:current.runtimesReady===true,resources:resource,quota:{fresh:q.fresh,remainingPercent:q.selectedRemainingPercent}};
  }catch(error){this.check(ctx);return {ok:false,targetCount:0,reason:String(error.message).slice(0,450)}}
 }
 holdDispatch(ctx){this.check(ctx);this.master.disableAutomaticDispatch();return {paused:true};}
 unlock(ctx){
  const s=this.safeForMutation(ctx,{allowLatched:true});
  if(s.emergency.latched&&s.emergency.revision!==ctx.expectedEmergencyRevision)throw Error('A newer emergency stop superseded this Startup request');
  if(s.emergency.latched)this.master.rearm({expectedRevision:ctx.expectedEmergencyRevision});
  return {released:!this.master.locked()};
 }
 async watchdogDisarmed(ctx){this.check(ctx);return this.master.restore.watchdogDisarmed(true);}
 arm(ctx){
  this.safeForMutation(ctx);
  this.quota.update({enabled:true,pollSeconds:5,thresholdPercent:7,window:'any'});
  this.safeForMutation(ctx);
  const s=this.quota.arm();
  return {armed:s.config.autoKillArmed&&!s.config.tripped,token:configRevision(s.config),armedAt:s.config.updatedAt};
 }
 guardAck(ctx){
  this.check(ctx);const s=this.quota.status();
  return s.guardAcknowledged===true&&s.guardAck?.revision===ctx.guardReceipt?.token;
 }
 async restoreStart(ctx){
  this.safeForMutation(ctx,{requireGuard:true});
  const previousNative=unresolvedNativeReason(this.master.restore.state);
  if(previousNative)throw Error(previousNative);
  const {saved,revision}=this.manifest();
  if(ctx?.bootstrap?.manifestRevision&&ctx.bootstrap.manifestRevision!==revision)throw Error('The preserved fleet changed after Startup validation; no restore was submitted');
  const branchAdoptionsRevision=this.master.restore.branchApproval?.()?.revision||null;
  if(branchAdoptionsRevision!==(ctx.bootstrap?.branchAdoptionsRevision||null))throw Error('Reviewed workspace branch mapping changed after Startup validation');
  // All asynchronous preview work has already finished. Recheck safety and
  // start synchronously so Cancel/Emergency cannot interleave before submit.
  if(this.master.restore.state.active||this.master.restore.running)throw Error('A restore operation is already running');
  this.safeForMutation(ctx,{requireGuard:true});
  this.master.disableAutomaticDispatch();
  return this.master.restore.start({autoDispatchAfter:false,manifest:saved,fromEmergency:true,startupRunId:ctx.runId,branchAdoptionsRevision});
 }
 restoreStatus(ctx){this.check(ctx);return this.master.restore.status();}
 restorePause(ctx){this.check(ctx);return this.master.restore.pause();}
 beginInspection(){
  const reading={serial:++this.inspectionSerial,startedAt:this.now(),settled:false,value:null,waiters:new Set()};
  this.reading=reading;
  const finish=value=>{
   reading.value={...value,inspectionPending:false,inspectionStartedAt:new Date(reading.startedAt).toISOString()};
   reading.settled=true;this.live=reading.value;this.liveAt=this.now();
   if(this.reading===reading)this.reading=null;
   for(const waiter of [...reading.waiters])waiter(reading.value);
   reading.waiters.clear();
  };
  // Keep one real inspector alive after a UI wait expires. Clearing its slot
  // on the response deadline would allow every refresh to spawn another probe.
  Promise.resolve().then(()=>this.readRuntimes()).then(finish,()=>finish({
   at:new Date(this.now()).toISOString(),daemonOnline:false,runtimesReady:false,healthyIds:[],verified:0,conflicts:[],
   processCountsComplete:false,runtimeAbsenceComplete:false,reason:'Current Orchestrator runtime health is unavailable'}));
  return reading;
 }
 waitForInspection(reading,ctx,waitMs){
  this.check(ctx);
  if(reading.settled)return Promise.resolve(reading.value);
  return new Promise((resolve,reject)=>{
   let timer,done=false;
   const finish=(value,error)=>{
    if(done)return;done=true;clearTimeout(timer);reading.waiters.delete(onValue);
    ctx?.signal?.removeEventListener('abort',onAbort);
    if(error)reject(error);else resolve(value);
   };
   const onValue=value=>finish(value);
   const onAbort=()=>finish(null,Error('Startup operation was cancelled before the next action'));
   reading.waiters.add(onValue);ctx?.signal?.addEventListener('abort',onAbort,{once:true});
   if(ctx?.signal?.aborted){onAbort();return;}
   if(waitMs!==null)timer=setTimeout(()=>finish(null),waitMs);
  });
 }
 pendingReadiness(reading){
  return {at:new Date(this.now()).toISOString(),lastObservedAt:this.live?.at||null,
   inspectionStartedAt:new Date(reading.startedAt).toISOString(),inspectionPending:true,stale:true,
   daemonOnline:null,runtimesReady:false,healthyIds:[],nativeAbsentIds:[],verified:0,conflicts:[],
   runningExpectedAgents:null,processCountsComplete:false,runtimeAbsenceComplete:false,
   reason:'Current runtime inspection is still running; previous readiness is not current. No startup or recovery outcome was inferred.'};
 }
 async readiness(ctx,{force=false}={}){
  this.check(ctx);
  const age=this.now()-this.liveAt;
  let sameManifest=false;
  if(this.live){try{sameManifest=this.live.manifestRevision===this.manifest().revision}catch{}}
  if(!force&&this.live&&age>=0&&age<3000&&sameManifest)return this.decorate(this.live);
  const requiredSerial=force?this.inspectionSerial+1:0;
  for(;;){
   this.check(ctx);
   const reading=this.reading||this.beginInspection();
   // UI GETs have a short response budget. Forced bootstrap/reconciliation
   // retain their caller's cancellation/deadline and may not substitute pending
   // or old observations for the mutation safety checks they previously awaited.
   const value=await this.waitForInspection(reading,ctx,force?null:this.readinessWaitMs);
   this.check(ctx);
   if(value===null)return this.decorate(this.pendingReadiness(reading));
   if(force&&reading.serial<requiredSerial)continue;
   return this.decorate(value);
  }
 }
 async readRuntimes(){
  const at=new Date(this.now()).toISOString();
  try{
   const {saved,targets,revision}=this.manifest();
   const restorationScope=createRestorationScope(saved,targets);
   const counts=await this.master.restore.runtimeInspector([...new Set(targets.map(s=>s.projectId))],{restorationScope});
   // Controller state is read after the potentially slow native inspection so
   // a completed background result does not revive an earlier ready snapshot.
   const sessions=await this.getSessions();
   const conflicts=[],missing=[],nativeAbsentIds=[],stoppedIds=[],recoveryIdentityMismatches=[],branchAdoptionIds=[],postReviewIds=[],postReviewRevisions=new Set();
   const bindings=()=>stateFingerprint({source:this.master.restore.state.source,startedAt:this.master.restore.state.startedAt,inflight:this.master.restore.state.inflight,
    branchAdoptionReceipts:this.master.restore.state.branchAdoptionReceipts,postRestoreReviewReceipts:this.master.restore.state.postRestoreReviewReceipts});
   const beforeBindings=bindings();
   const observations=await Promise.all(targets.map(async target=>{
    const matches=sessions.filter(s=>s.id===target.id);
    const candidate=matches.length===1?matches[0]:null;
    const match=this.master.restore.runtimeTarget?await this.master.restore.runtimeTarget(target,candidate,counts.get(target.id)):{matches:matchesRestoreTarget(target,candidate),adoption:null};
    return {target,candidate,match};
   }));
   for(const {target,candidate,match} of observations){
    const live=match.matches?candidate:null;
    if(match.adoption)branchAdoptionIds.push(target.id);
    if(match.postReview){postReviewIds.push(target.id);postReviewRevisions.add(match.postReview.reviewRevision);}
    if(!live||(!match.adoption&&!match.postReview&&['model','branch'].some(key=>(live[key]??null)!==(target[key]??null))))recoveryIdentityMismatches.push(target.id);
    if(target.kind==='orchestrator'){
     const other=sessions.filter(s=>s.projectId===target.projectId&&s.kind==='orchestrator'&&s.id!==target.id&&!s.isTerminated&&s.activity?.state!=='exited');
     conflicts.push(...other.map(s=>s.id));
    }
    if(!live||!this.runtimeHealth(live,counts.get(target.id)))missing.push(target.id);
    if(live&&runtimeAbsentEvidence(live,counts,this.now(),{restorationScope}))nativeAbsentIds.push(target.id);
    if(live&&verifiedStopped(live,counts,this.now(),restorationScope))stoppedIds.push(target.id);
   }
   const postReviewRevision=postReviewIds.length?this.master.restore.postReview?.()?.revision||null:null;
   const stable=this.manifest().revision===revision&&bindings()===beforeBindings&&(!postReviewIds.length||(postReviewRevisions.size===1&&postReviewRevisions.has(postReviewRevision)));
   const runtimesReady=stable&&missing.length===0&&conflicts.length===0;
   return {at:new Date(this.now()).toISOString(),manifestRevision:revision,targetIds:targets.map(s=>s.id),healthyIds:stable?targets.filter(s=>!missing.includes(s.id)).map(s=>s.id):[],daemonOnline:true,runtimesReady,total:targets.length,verified:stable?targets.length-missing.length:0,
    runningExpectedAgents:stable?targets.filter(s=>Number.isInteger(counts.get(s.id))&&counts.get(s.id)>0).length:null,
    processCountsComplete:stable&&targets.every(s=>Number.isInteger(counts.get(s.id))&&counts.get(s.id)>=0),
    nativeAbsentIds:stable?nativeAbsentIds:[],
    stoppedIds:stable?stoppedIds:[],
    branchAdoptionIds:stable?branchAdoptionIds:[],branchAdoptionsRevision:branchAdoptionIds.length?this.master.restore.state.source?.branchAdoptions?.revision||null:null,
    postReviewIds:stable?postReviewIds:[],postReviewRevision:stable?postReviewRevision:null,
    recoveryIdentitiesMatch:stable&&recoveryIdentityMismatches.length===0,
    runtimeAbsenceComplete:stable&&conflicts.length===0&&targets.every(s=>counts.get(s.id)===0||nativeAbsentIds.includes(s.id)),missing,conflicts,
    reason:!stable?'The preserved manifest or restore bindings changed during inspection; a new observation is required':runtimesReady?'Preserved runtimes and primary controllers are currently healthy':conflicts.length?'Conflicting primary controllers require attention':missing.length+' preserved runtimes are not currently verified healthy'};
  }catch(error){return {at,daemonOnline:false,runtimesReady:false,reason:'Current Orchestrator runtime health is unavailable',verified:0,conflicts:[]}}
 }
 decorate(value){
  const healthyIds=new Set(value.healthyIds||[]),unresolvedIds=unresolvedNativeOperations(this.master.restore.state).filter(id=>!healthyIds.has(id));
  const q=this.quota.status(),reason=this.master.locked()?'Emergency stop is active':this.quota.dispatchBlockReason()||(unresolvedIds.length?'A previous native restore is still unverified for '+unresolvedIds.join(', ')+'. No replay is permitted.':null);
  const paused=!this.master.sustain.config.enabled&&!this.master.turbo.config.enabled;
  return {...value,ready:value.runtimesReady===true&&!reason&&!this.master.drainState.enabled,
   reason:reason||value.reason,dispatchPaused:paused,unresolvedNativeIds:unresolvedIds,quota:{fresh:q.fresh,remainingPercent:q.selectedRemainingPercent,armed:q.config.autoKillArmed,guardAcknowledged:q.guardAcknowledged}};
 }
 async openUi(ctx){this.safeForMutation(ctx,{requireGuard:true});return this.launcher.launch('ui',{signal:ctx.signal,beforeTrigger:()=>this.safeForMutation(ctx,{requireGuard:true})});}
 durableRestoreState(){
  try{
   const bytes=fs.readFileSync(this.master.restore.statePath||path.join(this.root,'restore-state.json'));
   const value=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));
   if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.active!=='boolean'||typeof value.phase!=='string'||
    !Object.hasOwn(value,'source')||!Object.hasOwn(value,'startedAt')||!Array.isArray(value.desiredIds)||
    !value.inflight||typeof value.inflight!=='object'||Array.isArray(value.inflight)||!value.errors||typeof value.errors!=='object'||Array.isArray(value.errors))return null;
   // This collection was introduced after the preserved legacy operation.
   // Only its absent/empty default may be normalized; ledgers never default.
   if(!Object.hasOwn(value,'branchAdoptionReceipts')&&this.master.restore.state.branchAdoptionReceipts&&
    !Array.isArray(this.master.restore.state.branchAdoptionReceipts)&&Object.keys(this.master.restore.state.branchAdoptionReceipts).length===0)value.branchAdoptionReceipts={};
   if(!isDeepStrictEqual(value,this.master.restore.state))return null;
   return {stateFingerprint:stateFingerprint(this.master.restore.state),bytesFingerprint:createHash('sha256').update(bytes).digest('hex')};
  }catch{return null}
 }
 recoveryBinding(previous,manifest,{uncommitted=false,durableBefore=null}={}){
  const state=this.master.restore.state,ids=manifest.targets.map(s=>s.id),source=state.source;
  const events=Array.isArray(previous.events)?previous.events:[];
  const stage=events.filter(event=>event?.phase==='starting_restore');
  const waiting=events.filter(event=>event?.phase==='restoring');
  const times=(uncommitted?[state.startedAt,previous.startedAt,stage[0]?.at,previous.actionPending?.startedAt,previous.finishedAt]:[previous.startedAt,stage[0]?.at,state.startedAt,waiting[0]?.at,previous.finishedAt]).map(value=>Date.parse(value||''));
  const windowMatches=stage.length===1&&waiting.length===(uncommitted?0:1)&&times.every(Number.isFinite)&&times.every((time,index)=>index===0||time>=times[index-1])&&times[4]<=this.now()+1000&&(!uncommitted||times[0]<times[1]&&events.at(-1)?.phase==='starting_restore');
  const sourceMatches=Array.isArray(source?.targets)&&exactIds(source.targets.map(s=>s?.id),ids)&&manifest.targets.every(target=>{
   const saved=source.targets.find(s=>s.id===target.id);
   return ['id','projectId','kind','harness','model','mode'].every(key=>(saved[key]??null)===(target[key]??null))&&
    ['branch','createdAt'].every(key=>!Object.hasOwn(saved,key)||(saved[key]??null)===(target[key]??null));
  })&&typeof manifest.saved.at==='string'&&Number.isFinite(Date.parse(manifest.saved.at))&&source.snapshotAt===manifest.saved.at;
  const receiptMatches=!previous.restoreReceipt||(previous.restoreReceipt.startedAt===state.startedAt&&previous.restoreReceipt.total===ids.length);
  const ledgerReadable=state.inflight&&typeof state.inflight==='object'&&!Array.isArray(state.inflight)&&state.errors&&typeof state.errors==='object'&&!Array.isArray(state.errors);
  const legacyPriorOwner=!!source&&!Object.hasOwn(source,'startupRunId'),priorRunId=legacyPriorOwner?null:source?.startupRunId;
  const priorOwner=legacyPriorOwner||(typeof priorRunId==='string'&&!!priorRunId&&priorRunId!==previous.runId);
  const durableAfter=uncommitted?this.durableRestoreState():null;
  const durableStateVerified=!!durableBefore&&!!durableAfter&&durableBefore.stateFingerprint===durableAfter.stateFingerprint&&durableBefore.bytesFingerprint===durableAfter.bytesFingerprint;
  const startNotCommitted=uncommitted&&previous.restoreStarted===false&&previous.restoreReceipt===null&&previous.actionPending?.name==='start existing RestoreFleet'&&windowMatches&&priorOwner&&durableStateVerified;
  const operationMatches=uncommitted?startNotCommitted:previous.restoreStarted===true&&previous.actionPending===null&&receiptMatches&&(!source?.startupRunId||source.startupRunId===previous.runId)&&(!source?.branchAdoptions||source.branchAdoptions.revision===previous.bootstrap?.branchAdoptionsRevision);
  return {kind:uncommitted?'uncommitted-start':'committed-restore',runId:previous.runId,phase:state.phase,startedAt:state.startedAt||null,manifestRevision:manifest.revision,
   ...(uncommitted?{priorRunId,legacyPriorOwner,startNotCommitted,durableStateVerified,durableStateFingerprint:durableAfter?.stateFingerprint||null,durableBytesFingerprint:durableAfter?.bytesFingerprint||null}:{}),
   targetIds:ids,sourceTargetIds:Array.isArray(source?.targets)?source.targets.map(s=>s?.id):[],
   desiredIds:Array.isArray(state.desiredIds)?[...state.desiredIds]:[],stateFingerprint:stateFingerprint(state),
   verified:previous.restoreStartAttempted===true&&operationMatches&&state.active===false&&!this.master.restore.running&&
    ['paused','partial','failed'].includes(state.phase)&&windowMatches&&sourceMatches&&ledgerReadable&&exactIds(state.desiredIds,ids)};
 }
 async reconcile(ctx){
  this.check(ctx);
  const restoreBefore=stateFingerprint(this.master.restore.state);
  const durableBefore=this.durableRestoreState();
  const restoreWasInactive=this.master.restore.state.active===false&&!this.master.restore.running;
  const current=await this.readiness(ctx,{force:true});
  this.check(ctx);
  // Only the controller's explicit RECHECK STARTUP path calls this method.
  // The broker may retire a matching verified terminal cancellation, never an
  // unknown native outcome, and never submits another launch while doing so.
  const l=this.launcher.reconcile({acceptCompletedCancellation:true});
  const q=this.quota.status(),e=this.emergency(),r=this.master.restore;
  const completedLaunch=l.request&&l.result&&l.request.nonce===l.result.nonce&&l.request.mode===l.result.mode&&['ok','error','rejected'].includes(l.result.status)&&!l.requiresReconciliation;
  const launchActive=l.requiresReconciliation===true||!!l.pending&&!completedLaunch;
  const submittedIds=unresolvedNativeOperations(r.state),healthyIds=new Set(current.healthyIds||[]);
  const unresolvedSubmittedIds=submittedIds.filter(id=>!healthyIds.has(id));
  const operationInFlight=!!r.running||!!r.state.active||launchActive||unresolvedSubmittedIds.length>0;
  const dispatchPaused=!this.master.sustain.config.enabled&&!this.master.turbo.config.enabled;
  const previous=ctx.previousRun||{},manifest=this.manifest();
  const reviewedAdoptionsMatch=!current.branchAdoptionIds?.length||(previous.bootstrap?.branchAdoptionsRevision===current.branchAdoptionsRevision&&r.state.source?.startupRunId===previous.runId);
  const postReviewsMatch=!current.postReviewIds?.length||(r.state.source?.startupRunId===previous.runId&&current.postReviewIds.every(id=>r.state.postRestoreReviewReceipts?.[id]?.reviewRevision===current.postReviewRevision));
  const identitiesMatch=!!previous.bootstrap?.manifestRevision&&previous.bootstrap.manifestRevision===manifest.revision&&current.manifestRevision===manifest.revision&&reviewedAdoptionsMatch&&postReviewsMatch;
  const proof={runId:ctx.runId,stateRevision:ctx.expectedStateRevision,observedAt:new Date(this.now()).toISOString(),
   outcome:'unknown',noOperationInFlight:!operationInFlight,dispatchPaused,restoreActive:!!r.state.active,launchActive,
   durableSubmittedIds:submittedIds,unresolvedSubmittedIds,reason:unresolvedSubmittedIds.length?'A previous native restore outcome remains unverified; its durable ledger was preserved and no replay is allowed.':null,
   emergencyLatched:e.latched,expectedTargetsMatch:identitiesMatch,expectedTargetCount:manifest.targets.length,
   verifiedTargetCount:current.verified||0,allExpectedControllersHealthy:current.runtimesReady===true,
   allExpectedRuntimesHealthy:current.runtimesReady===true,
   guard:{fresh:q.fresh,armed:q.config.autoKillArmed,tripped:q.config.tripped,monitorEnabled:q.config.enabled,remainingPercent:q.selectedRemainingPercent,
    ordinaryUsageAllowed:q.ordinaryUsageAllowed,thresholdPercent:q.config.thresholdPercent,pollSeconds:q.config.pollSeconds,
    window:q.config.window,revision:configRevision(q.config),acknowledgedRevision:q.guardAcknowledged?q.guardAck?.revision:null}};
  if(!operationInFlight&&dispatchPaused&&current.ready&&identitiesMatch)proof.outcome='ready_paused';
  // Only the complete local audit of a run that never attempted restore can
  // prove no resume was submitted. Absence of OS processes alone is not proof.
  const knownAbsent=(current.processCountsComplete&&current.runningExpectedAgents===0)||current.runtimeAbsenceComplete===true;
  if(!operationInFlight&&submittedIds.length===0&&dispatchPaused&&identitiesMatch&&previous.restoreStartAttempted===false&&previous.restoreStarted===false&&previous.actionPending===null&&current.daemonOnline&&current.conflicts?.length===0&&knownAbsent&&previous.error?.code!=='STATE_UNREADABLE'){
   proof.outcome='idle';proof.submissionAuditComplete=true;proof.submittedResume=false;proof.runningExpectedAgents=0;
   proof.nativeRuntimeAbsenceVerified=current.runtimeAbsenceComplete===true;
  }
  // An interrupted partial restore is neither a completed fleet nor a run that
  // never submitted work. Account for every exact target without clearing any
  // native submission ledger. Recheck only permits a new explicit Startup.
  if(proof.outcome==='unknown'&&previous.restoreStartAttempted===true){
   // start() commits the new source/run identity before scheduling native work.
   // An unchanged older durable operation can therefore prove that a rejected
   // start never committed, without discarding that earlier operation's ledger.
   const binding=this.recoveryBinding(previous,manifest,{uncommitted:previous.restoreStarted===false,durableBefore}),healthy=current.healthyIds||[],stopped=current.stoppedIds||[];
   const accounted=exactIds([...healthy,...stopped],manifest.targets.map(s=>s.id));
   const protectedNow=!e.latched&&!this.quota.dispatchBlockReason()&&q.config.enabled===true&&q.config.autoKillArmed===true&&q.config.tripped===false&&q.config.thresholdPercent===7&&q.config.pollSeconds===5&&q.config.window==='any'&&q.guardAcknowledged===true&&q.guardAck?.revision===configRevision(q.config);
   if(!operationInFlight&&restoreWasInactive&&restoreBefore===binding.stateFingerprint&&submittedIds.length===0&&dispatchPaused&&identitiesMatch&&current.recoveryIdentitiesMatch===true&&current.daemonOnline===true&&current.conflicts?.length===0&&!this.master.drainState.enabled&&binding.runId===ctx.runId&&binding.verified&&accounted&&stopped.length>0&&protectedNow){
    Object.assign(proof,{outcome:'resumable',resumeAuditComplete:true,manifestRevision:manifest.revision,
     expectedTargetIds:manifest.targets.map(s=>s.id),healthyTargetIds:[...healthy],stoppedTargetIds:[...stopped],
     verifiedStoppedCount:stopped.length,allExpectedTargetsAccounted:true,restoreBinding:binding,
     reason:'Every preserved target is verified healthy or stopped. No unresolved native submission remains; a new explicit Startup can continue restoration.'});
   }
  }
  this.check(ctx);return proof;
 }
 deps(){
  return Object.fromEntries(['safety','bootstrap','holdDispatch','unlock','watchdogDisarmed','arm','guardAck','readiness','openUi','reconcile'].map(k=>[k,this[k].bind(this)]).concat([['restore',{start:this.restoreStart.bind(this),status:this.restoreStatus.bind(this),pause:this.restorePause.bind(this)}]]));
 }
}

module.exports={StartupAdapter};
