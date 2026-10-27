'use strict';
const fs = require('node:fs');
const {randomUUID} = require('node:crypto');
const path = require('node:path');
const os = require('node:os');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const execAsync = promisify(execFile);

const DEFAULTS = Object.freeze({
  enabled:false,
  profile:'aggressive',
  targetWorking:12,
  maxActionsPerCycle:3,
  maxActionsPerHour:18,
  perAgentCooldownMinutes:18,
  orchestratorCooldownMinutes:10,
  pollingSeconds:60,
  minFreeRamGiB:9,
  minDiskFreeGiB:25,
  maxCpuPercent:92,
  maxReviewAssignmentsPerCycle:1,
  maxLocalHeavyTests:3
});

const giB=1024*1024*1024;
const clone=x=>JSON.parse(JSON.stringify(x));
function atomic(file,record){
  const tmp=file+'.tmp';
  fs.writeFileSync(tmp,JSON.stringify(record,null,2),'utf8');
  fs.renameSync(tmp,file);
}
function load(file,fallback){
  try{return JSON.parse(fs.readFileSync(file,'utf8'))}catch{return clone(fallback)}
}
const {publicError,publicState,privateDiagnostic}=require('./control-errors');
const {canReceiveInstruction}=require('./runtime-health');
const {checks,prSignal,graphSignal,DispatchSignals,REPORT_POLICY}=require('./dispatch-policy');
function errorText(e){return publicError('turbo',e)}
function parseTime(value){const x=Date.parse(value||'');return Number.isFinite(x)?x:0}
function recent(time,withinMs,now){return now-parseTime(time)<withinMs}
function asNumber(value,min,max,name){
  if(!Number.isInteger(value)||value<min||value>max)throw Error(name+' must be between '+min+' and '+max);
  return value;
}
function configWithChanges(current,changes){
  const keys=['profile','targetWorking','maxActionsPerCycle','maxActionsPerHour','perAgentCooldownMinutes','orchestratorCooldownMinutes','pollingSeconds','minFreeRamGiB','minDiskFreeGiB','maxCpuPercent','maxReviewAssignmentsPerCycle','maxLocalHeavyTests'];
  for(const key of Object.keys(changes||{}))if(!keys.includes(key))throw Error('Unrecognized configuration setting: '+key);
  const n={...current,...changes};
  if(!['balanced','aggressive'].includes(n.profile))throw Error('Unsupported Turbo profile');
  asNumber(n.targetWorking,1,25,'targetWorking');
  asNumber(n.maxActionsPerCycle,1,6,'maxActionsPerCycle');
  asNumber(n.maxActionsPerHour,1,60,'maxActionsPerHour');
  asNumber(n.perAgentCooldownMinutes,5,120,'perAgentCooldownMinutes');
  asNumber(n.orchestratorCooldownMinutes,5,120,'orchestratorCooldownMinutes');
  asNumber(n.pollingSeconds,30,300,'pollingSeconds');
  asNumber(n.minFreeRamGiB,6,32,'minFreeRamGiB');
  asNumber(n.minDiskFreeGiB,10,200,'minDiskFreeGiB');
  asNumber(n.maxCpuPercent,65,100,'maxCpuPercent');
  asNumber(n.maxReviewAssignmentsPerCycle,0,3,'maxReviewAssignmentsPerCycle');
  asNumber(n.maxLocalHeavyTests,1,5,'maxLocalHeavyTests');
  if(n.maxReviewAssignmentsPerCycle>n.maxActionsPerCycle)throw Error('Review assignments cannot exceed cycle action limit');
  if(n.orchestratorCooldownMinutes>n.perAgentCooldownMinutes*4)throw Error('Orchestrator cooldown too long for worker cadence');
  return n;
}
function extractRefs(snapshot){
  const byPR=new Map();
  for(const s of snapshot.sessions||[]){
    for(const p of s.prs||[]){
      if(!['open','draft'].includes(p.state))continue;
      const val=Number(p.number);
      if(Number.isFinite(val)&&!byPR.has(val))byPR.set(val,{number:val,owner:s.id,ownerName:s.name,ci:p.ci,review:p.review,status:p.state,url:p.url});
    }
  }
  return [...byPR.values()];
}
class Turbo {
 constructor({root,getState,command,audit,isManualBusy,projectId='mjobs-luna',githubRunner=null,clock=()=>Date.now(),autoSchedule=true}){
   this.getState=getState;this.command=command;this.audit=audit;
   this.isManualBusy=isManualBusy||(()=>false);
   this.projectId=projectId;this.dispatchSignals=new DispatchSignals(root);
   this.clock=clock;
   this.configPath=path.join(root,'turbo-config.json');
   this.statePath=path.join(root,'turbo-state.json');
   this.eventsPath=path.join(root,'turbo-events.jsonl');
   this.config={...DEFAULTS,...load(this.configPath,{})};
   this.config=configWithChanges(this.config,{});
   this.session={...load(this.statePath,{lastCycle:null,lastSuccess:null,lastError:null,actions:[],reviewClaims:{}})};
   this.session.actions=Array.isArray(this.session.actions)?this.session.actions:[];
   this.session.reviewClaims=this.session.reviewClaims||{};
   this.latest={ready:false,pausedReason:'Not evaluated',planned:[],actionsThisCycle:0,resources:null,rootId:null,githubFetchedAt:null,health:null,intervalSeconds:this.config.pollingSeconds};
   this.running=false;
   this.githubRunner=githubRunner||this.runGitHub.bind(this);
   this.githubCache=null;
   this.githubAt=0;this.githubErrorAt=0;
   this.timer=null;
   if(autoSchedule)this.start();
 }
 start(){
   if(this.timer)return;
   this.timer=setInterval(()=>this.cycle().catch(e=>this.error(e)),this.config.pollingSeconds*1000);
   this.timer.unref?.();
 }
 restartTimer(){
   if(this.timer){clearInterval(this.timer);this.timer=null}
   this.start();
 }
 log(event,details={}){
   const rec={at:new Date(this.clock()).toISOString(),event,...details};
   fs.appendFileSync(this.eventsPath,JSON.stringify(rec)+'\n','utf8');
   this.audit('turbo_'+event,details);
 }
 save(){
   const cutoff=this.clock()-3*24*60*60*1000;
   this.session.actions=this.session.actions.filter(a=>parseTime(a.at)>cutoff).slice(-600);
   this.session.reviewClaims=Object.fromEntries(Object.entries(this.session.reviewClaims).filter(([,x])=>parseTime(x.at)>cutoff));
   atomic(this.statePath,this.session);
 }
 configuration(){return clone(this.config)}
 summary(){
   const now=this.clock(),hours=this.session.actions.filter(a=>recent(a.at,60*60*1000,now));
   const today=this.session.actions.filter(a=>recent(a.at,24*60*60*1000,now));
   return {
    config:clone(this.config),
    running:this.running,
    lastCycle:this.session.lastCycle||null,
    lastSuccess:this.session.lastSuccess||null,
    lastError:this.session.lastError?errorText({message:this.session.lastError}):null,
    lastPlan:publicState(this.latest,'turbo'),
    sentThisHour:hours.length,
    sent24Hours:today.length,
    activity:publicState(this.session.actions.slice(-15).reverse(),'turbo'),
    reviewsClaimed:Object.keys(this.session.reviewClaims).length
   };
 }
 update(changes){
   const value=configWithChanges(this.config,changes);
   this.config=value;atomic(this.configPath,value);this.restartTimer();
   this.log('settings',{settings:changes});
   return this.summary();
 }
 enable(on){
   if(typeof on!=='boolean')throw Error('enabled must be boolean');
   this.config.enabled=on;atomic(this.configPath,this.config);
   this.log(on?'enabled':'disabled',{});
   return this.summary();
 }
 async runGitHub(){
   const args=['pr','list','--repo','MulattoTech/MJobs','--state','open','--limit','50','--json','number,headRefOid,title,isDraft,reviewDecision,statusCheckRollup'];
   const merged=['pr','list','--repo','MulattoTech/MJobs','--state','merged','--limit','100','--json','number,mergedAt'];
   const [a,b]=await Promise.all([
    execAsync('gh.exe',args,{windowsHide:true,timeout:20000,maxBuffer:2000000}),
    execAsync('gh.exe',merged,{windowsHide:true,timeout:20000,maxBuffer:1000000})
   ]);
   return {open:JSON.parse(a.stdout),merged:JSON.parse(b.stdout),at:new Date(this.clock()).toISOString()};
 }
 async github(force=false){
   if(!force&&this.githubErrorAt&&this.clock()-this.githubErrorAt<30000)return this.githubCache;
   if(!force&&this.githubCache?.fresh!==false&&this.githubCache&&this.clock()-this.githubAt<180000)return this.githubCache;
   try{
    const raw=await this.githubRunner();
    if(!raw||!Array.isArray(raw.open))throw Error('Invalid GitHub snapshot');
    const x={...raw,at:new Date(this.clock()).toISOString(),fresh:true,error:null};
    this.githubCache=x;this.githubAt=this.clock();this.githubErrorAt=0;this.latest.githubError=null;return x;
   }catch(e){
    this.log('github_error',{diagnostic:privateDiagnostic(e)});
    this.latest.githubError=errorText(e);this.githubErrorAt=this.clock();
    if(this.githubCache){this.githubCache={...this.githubCache,fresh:false,error:errorText(e)};return this.githubCache;}
    return null;
   }
 }
 async signals(snap){
   let diskFree=null;
   try{
    const stat=fs.statfsSync(this.configPath?path.dirname(this.configPath):process.cwd());
    diskFree=Number(stat.bavail)*Number(stat.bsize)/giB;
   }catch{}
   const freeMem=(snap.metrics?.ramFree??os.freemem())/giB;
   const cpu=snap.metrics?.cpu;
   let pause=null;
   if(!snap.sessions?.length)pause='Orchestrator session data unavailable';
   else if(freeMem<this.config.minFreeRamGiB)pause='Low available RAM ('+freeMem.toFixed(1)+' GiB)';
   else if(diskFree!==null&&diskFree<this.config.minDiskFreeGiB)pause='Low disk space ('+diskFree.toFixed(1)+' GiB)';
   else if(Number.isFinite(cpu)&&cpu>=this.config.maxCpuPercent)pause='CPU pressure ('+cpu+'%)';
   return {freeRamGiB:+freeMem.toFixed(1),freeDiskGiB:diskFree===null?null:+diskFree.toFixed(1),cpuPercent:cpu??null,pausedReason:pause};
 }
 validRoot(s){
   return s&&s.kind==='orchestrator'&&!s.isTerminated&&!s.replacedBy&&s.activity!=='exited'&&s.status!=='emergency_stopped';
 }
 withinCooldown(target,type,now){
   const minutes=(type==='primary'?this.config.orchestratorCooldownMinutes:this.config.perAgentCooldownMinutes);
   return this.session.actions.some(a=>a.id===target&&recent(a.at,minutes*60000,now));
 }
 hourlyCap(now){
   return this.session.actions.filter(a=>recent(a.at,3600000,now)).length>=this.config.maxActionsPerHour;
 }
 previewTextPlan(snapshot,gh,now){
   const sessions=snapshot.sessions||[];
   const root=sessions.find(x=>this.validRoot(x));
   const plan=[];
   const graph=graphSignal(sessions,gh?.open||[]);
   const actions=[];
   const fromGH=new Map((gh?.open||[]).map(p=>[p.number,p]));
   const refs=extractRefs(snapshot);
   const openPRs=gh?refs.filter(p=>fromGH.has(p.number)):refs;
   const candidate=(priority,kind,session,message,reason,key)=>{
    if(!canReceiveInstruction(session))return;
    if(this.withinCooldown(session.id,kind==='primary'?'primary':kind,now))return;
    if(!session.id||!message||!key||this.dispatchSignals.seen(key))return;
    actions.push({priority,kind,id:session.id,name:session.name,mode:session.mode,message:(message+REPORT_POLICY).replaceAll('mjobs-luna-34',root?.id||'mjobs-luna-35'),reason,key});
   };
   if(!this.validRoot(root)){return {plan:[],blocked:'No active primary orchestrator',rootId:root?.id||null,openPRs:openPRs.length}}
   const available=sessions.filter(s=>canReceiveInstruction(s)&&!s.openPRs&&!s.blocked&&s.kind==='worker');
   const fail=openPRs.filter(p=>{
    const full=fromGH.get(p.number);
    if(gh&&full)return (full.statusCheckRollup||[]).some(x=>x.conclusion==='FAILURE'||x.conclusion==='TIMED_OUT');
    return p.ci==='failing'||p.ci==='failed';
   });
   const pending=openPRs.filter(p=>p.ci==='pending'||p.ci==='queued');
   const idleNames=available.filter(s=>s.role!=='Orchestration'&&!s.role.toLowerCase().includes('review')).slice(0,12).map(s=>s.id);
   const rootMessage='TURBO DISPATCH CHECKPOINT: You are the only MJobs primary integration owner. Live fleet '+sessions.length+' sessions; '+snapshot.summary.working+' working, '+snapshot.summary.idle+' idle. Existing PRs '+openPRs.map(p=>'#'+p.number+'('+p.ci+', owner '+p.owner+')').slice(0,14).join(', ')+'. '+(fail.length?'Urgent failing CI: '+fail.map(p=>'#'+p.number).join(', ')+'. ':'')+'Available implementation agents: '+idleNames.join(', ')+'. Assign exclusive P0/P1 GitHub issues and file scopes to existing idle implementation workers, prioritizing accepted deliverables. Review, refresh, and integrate eligible passing PRs with exact-head CI, required review and branch protection. Never edit dirty main, force-push, duplicate file ownership or submit real applications. Restrict simultaneous heavy local tests to '+this.config.maxLocalHeavyTests+'. Keep no more than '+this.config.targetWorking+' executing turns. Delegate with plain ao send --session ID --message TEXT (no --steer for TUI). Do not spawn new sessions. Reply with a short dispatch ledger: ownership by issue/file, reviewer assignments and CI blockers.';
   if(canReceiveInstruction(root)&&(snapshot.summary.working<this.config.targetWorking||fail.length||openPRs.length))
    candidate(0,'primary',root,rootMessage,'Wake primary to allocate idle fleet and clear PR queue','root:'+root.id+':'+graph);
   for(const p of fail){
    const owner=sessions.find(s=>s.id===p.owner);
    if(!canReceiveInstruction(owner)||owner.kind==='orchestrator')continue;
    const msg='TURBO CI RECOVERY for your existing PR #'+p.number+'. Diagnose current exact-head failing checks without blind reruns. Use the existing PR and branch you own. Only modify your already-authorized files; coordinate any shared test or new file ownership with primary mjobs-luna-34. Fix proven failures, run focused checks, and publish an updated exact-head PR only when appropriate. Do not force-push, change dirty main, merge, or bypass CI. Report the root cause and CI evidence to the primary.';
    candidate(1,'ci',owner,msg,'Repair failing CI on owned PR #'+p.number,prSignal(fromGH.get(p.number))?.key);
   }
   const reviewers=sessions.filter(s=>canReceiveInstruction(s)&&s.role.toLowerCase().includes('review'));
   for(const p of openPRs){
    const full=fromGH.get(p.number);
    if(!full?.headRefOid||full.headRefOid.length<35)continue;
    const checkFail=(full.statusCheckRollup||[]).some(x=>x.conclusion==='FAILURE');
    const ready=!(full.statusCheckRollup||[]).some(x=>['PENDING','IN_PROGRESS','QUEUED'].includes(x.status));
    if(checkFail||!ready||!(full.statusCheckRollup||[]).length)continue;
    const hash=String(full.headRefOid).slice(0,12);
    const claimKey=p.number+':'+full.headRefOid;
    if(this.session.reviewClaims[claimKey])continue;
    if(full.reviewDecision==='APPROVED')continue;
    const reviewer=reviewers.find(r=>r.id!==p.owner&&!actions.some(a=>a.id===r.id));
    if(!reviewer)break;
    const msg='TURBO READ-ONLY REVIEW: Inspect MJobs PR #'+p.number+' at exact GitHub head '+full.headRefOid+' independently. Review the diff, negative tests, boundaries and acceptance evidence. Do not edit files, trigger additional agents, merge, or claim ownership. Send actionable blocker/high-confidence findings and current-head verification to primary mjobs-luna-34, and report any outdated head. No duplicate review of unchanged head.';
    candidate(2,'review',reviewer,msg,'Review PR #'+p.number+' @ '+hash,'review:'+claimKey);
   }
   const coordinators=sessions.filter(s=>canReceiveInstruction(s)&&s.role==='Orchestration'&&s.kind==='worker');
   for(const coordinator of coordinators){
    const msg='TURBO COORDINATION CHECKPOINT. Report a concise list of concrete unclaimed P0/P1 issues or failing PRs to primary mjobs-luna-34 in your assigned lane. Avoid duplicating coordinator reports. You may assign work only if the primary has explicitly delegated exclusive ownership. Do not independently spawn agents or merge.';
    candidate(3,'coord',coordinator,msg,'Refresh '+coordinator.name+' lane plan','coord:'+coordinator.id+':'+graph);
   }
   const workAvailable=available.filter(s=>s.role!=='Orchestration'&&!s.role.toLowerCase().includes('review'));
   for(const worker of workAvailable){
    const msg='TURBO TASK REFILL: Your agent turn is idle. Check prior assignments and owned branches first. If you have no unfinished task or uncommitted work, request a new bounded P0/P1 issue with acceptance criteria and exclusive file ownership from primary orchestrator mjobs-luna-34. Do not self-assign, duplicate existing file scope, run heavy tests or modify dirty main until delegated. When assigned, focus on completing a tested PR with exact-head CI evidence.';
    candidate(4,'refill',worker,msg,'Ask for scoped implementation work','refill:'+worker.id);
   }
   actions.sort((a,b)=>a.priority-b.priority||a.id.localeCompare(b.id));
   const unique=[],seen=new Set();
   for(const x of actions){if(seen.has(x.id))continue;seen.add(x.id);unique.push(x)}
   return {plan:unique,blocked:null,rootId:root.id,openPRs:openPRs.length,pendingPRs:pending.length,failedPRs:fail.length};
 }
 async evaluate(){
   const snapshot=await this.getState(true);
   const [gh,resource]=await Promise.all([this.github(),this.signals(snapshot)]);
   const plan=this.previewTextPlan(snapshot,gh,this.clock());
   if(resource.pausedReason)plan.blocked=resource.pausedReason;
   if(snapshot.offline)plan.blocked='Orchestrator roster is an offline snapshot';
   if(!gh||gh.fresh===false)plan.blocked='GitHub snapshot unavailable or stale; no automatic assignments';
   if(this.isManualBusy())plan.blocked='Another manual dashboard operation is in progress';
   if(this.hourlyCap(this.clock()))plan.blocked='Hourly action budget reached';
   if(snapshot.summary.working>=this.config.targetWorking)plan.blocked='Productive concurrency target reached';
   const now=this.clock();
   const hour=this.session.actions.filter(a=>recent(a.at,3600000,now)).length;
   const remaining=Math.max(0,this.config.maxActionsPerHour-hour);
   const selected=[];
   let reviews=0;
   const freeSlots=Math.max(0,this.config.targetWorking-snapshot.summary.working);
   for(const action of plan.plan){
    if(selected.length>=Math.min(this.config.maxActionsPerCycle,remaining,freeSlots))break;
    if(action.kind==='review' && reviews>=this.config.maxReviewAssignmentsPerCycle)continue;
    if(action.kind==='review')reviews++;
    selected.push(action);
   }
   const recentMerged=(gh?.merged||[]).filter(x=>recent(x.mergedAt,3600000,now)).length;
   const merged24h=(gh?.merged||[]).filter(x=>recent(x.mergedAt,24*3600000,now)).length;
   const detail={
    ready:!plan.blocked,pausedReason:plan.blocked,
    snapshotAt:snapshot.timestamp,rootId:plan.rootId,resources:resource,
    planned:plan.plan.slice(0,15).map(({priority,kind,id,name,reason,key})=>({priority,kind,id,name,reason,key})),
    nextActions:selected.map(({kind,id,name,reason})=>({kind,id,name,reason})),
    counts:{openPRs:plan.openPRs||0,failedPRs:plan.failedPRs||0,pendingPRs:plan.pendingPRs||0,waitingInput:snapshot.sessions.filter(s=>s.activity==='waiting_input').length,
      idleFree:snapshot.summary.idleFree,working:snapshot.summary.working,
      mergedLastHour:recentMerged,mergedLast24Hours:merged24h,
      sampledMerged:(gh?.merged||[]).length},
    githubFetchedAt:gh?.at||null,
    estimatedNextTick:new Date(now+this.config.pollingSeconds*1000).toISOString()
   };
   this.latest=detail;
   return {detail,selected};
 }
 async preview(){
   if(this.running)throw Error('Turbo cycle is already in progress');
   const {detail}=await this.evaluate();
   return {enabled:this.config.enabled,preview:detail};
 }
 async cycle({manual=false,dryRun=false}={}){
   if(this.running)throw Error('Turbo cycle is already in progress');
   if(!manual&&this.sustain?.config.enabled)return {skipped:'Sustained dispatch owns automatic scheduling'};
   if(!manual&&!this.config.enabled)return {skipped:'disabled'};
   if(this.isManualBusy())return {skipped:'manual operation in progress'};
   this.running=true;
   const timestamp=new Date(this.clock()).toISOString();
   try{
    const {detail,selected}=await this.evaluate();
    this.session.lastCycle=timestamp;
    if(!detail.ready||dryRun){
      this.save();
      return {executed:[],preview:detail,reason:detail.pausedReason,simulation:dryRun};
    }
    const sent=[];
    for(const action of selected){
      if(!this.config.enabled&&!manual)break;
      if(this.isManualBusy())break;
      const current=await this.getState(true);
      const live=current.sessions.find(s=>s.id===action.id);
       if(current.offline||!canReceiveInstruction(live))continue;
      const resource=await this.signals(current);
      if(resource.pausedReason||current.summary.working>=this.config.targetWorking)break;
      // Persist reservation before sending. If the CLI times out, do not retry blindly.
      if(!this.dispatchSignals.reserve(action.key,action.id,this.clock()))continue;
      const stamp=new Date(this.clock()).toISOString();
      this.session.actions.push({at:stamp,id:action.id,kind:action.kind,key:action.key,status:'reserved',reason:action.reason});
      this.save();
      const record=this.session.actions[this.session.actions.length-1];
      try{
       const args=['send','--session',action.id,'--message',action.message];
       if(action.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
       await this.command(args,25000);
       record.status='submitted';this.dispatchSignals.finish(action.key,'submitted');
       sent.push({id:action.id,kind:action.kind,reason:action.reason});
       if(action.kind==='review')this.session.reviewClaims[action.key.slice(7)]={at:stamp,reviewer:action.id};
       this.log('sent',{id:action.id,kind:action.kind,reason:action.reason});
      }catch(e){
       record.status='uncertain';this.dispatchSignals.finish(action.key,'uncertain');
       record.error=errorText(e);
       this.log('send_uncertain',{id:action.id,kind:action.kind,error:errorText(e),diagnostic:privateDiagnostic(e)});
       // Stop this cycle; a timeout may mean the message was accepted.
       this.session.lastError=errorText(e);
       this.save();
       break;
      }
      this.save();
    }
    this.session.lastSuccess=new Date(this.clock()).toISOString();
    this.latest.actionsThisCycle=sent.length;
    this.save();
    return {executed:sent,preview:this.latest,reason:null};
   }catch(e){
    this.error(e);
    if(manual)throw new Error(errorText(e));
    return {error:errorText(e)};
   }finally{this.running=false}
 }
 error(e){
   this.session.lastError=errorText(e);
   this.log('error',{message:this.session.lastError,diagnostic:privateDiagnostic(e)});
   this.save();
 }
}

const allHandsHoldMs=10*60*1000;
function compact(s){return String(s||'').replace(/\s+/g,' ').trim().slice(0,250);}
Turbo.prototype.makeAllHandsPlan=async function(){
 const snap=await this.getState(true);
 const resource=await this.signals(snap);
 const gh=await this.github(true);
 const open=Array.isArray(gh?.open)?gh.open:[];
 const ghMap=new Map(open.map(p=>[Number(p.number),p]));
 if(snap.offline||!gh||gh.fresh===false)throw Error('Fresh Orchestrator and GitHub snapshots are required');
 const root=snap.sessions.find(s=>this.validRoot(s));
 if(!root)throw Error('Primary orchestrator missing. No All Hands dispatch permitted.');
 const now=this.clock();
 const people=(snap.sessions||[]).filter(canReceiveInstruction);
 const working=(snap.sessions||[]).filter(s=>s.busy);
 const plans=[],skips=[],assignedPR=new Set();
 const sessionSent=(s)=>this.session.actions.some(a=>a.id===s.id&&a.kind==='allhands'&&recent(a.at,allHandsHoldMs,now));
 const lastAll=this.session.lastAllHands?parseTime(this.session.lastAllHands):0;
 const globalCooldown=Math.max(0,Math.ceil((allHandsHoldMs-(now-lastAll))/60000));
 const push=(s,reason,message,signalKey=null)=>{
  const deliveryHold=this.deliveryLedger?.holdFor(s.id,now);if(deliveryHold){skips.push({id:s.id,reason:deliveryHold.reason});return;}
  if(signalKey&&this.dispatchSignals.seen(signalKey)){skips.push({id:s.id,reason:'Exact-head action already delivered'});return;}
  if(sessionSent(s)){skips.push({id:s.id,reason:'All Hands already sent within 10 minutes'});return}
  plans.push({id:s.id,name:s.name,model:s.model,kind:s.kind,mode:s.mode,role:s.role,reason,signalKey,message:(message+REPORT_POLICY).replaceAll('mjobs-luna-34',root?.id||'mjobs-luna-35')});
 };
 for(const s of people){
  if(s.id===root.id){
   push(s,'Primary fleet-wide delegation',
    'ALL HANDS: You are the sole integration owner. Coordinate your 29 existing agents into bounded, nonoverlapping P0/P1 work. Immediately reconcile issues, assignments, existing PRs, and failing CI; give idle workers specific owned tasks and file boundaries. Let reviewers inspect distinct PR heads and only merge after the required exact-head CI/review rules pass. Avoid new agents, force pushes, dirty-main edits, credential disclosure or real application submissions. Preserve current work. Ask idle workers for status only when you have concrete work to give them. Report assignments and blockers succinctly.');
   continue;
  }
  const prs=(s.prs||[]).filter(p=>['open','draft'].includes(p.state)&&(!gh||ghMap.has(Number(p.number))));
  const failing=prs.filter(p=>p.ci==='failing'||p.ci==='failed'||(ghMap.get(Number(p.number))?.statusCheckRollup||[]).some(c=>c.conclusion==='FAILURE'));
  if(failing.length){
   const nums=failing.map(x=>'#'+x.number).join(', ');
   push(s,'Repair CI on '+nums,
    'ALL HANDS—existing PR ownership: investigate and fix the current-head failing checks for your PR(s) '+nums+'. Work only inside your already-owned branch/files, root-cause the failure, run focused tests, publish a normal non-force update only when verified, and notify mjobs-luna-34. Do not touch modified local main or merge yourself.');
   continue;
  }
  if(prs.length&&prs.every(p=>!prSignal(ghMap.get(Number(p.number))))){skips.push({id:s.id,reason:'Hosted checks still running; no repeat audit'});continue;}
  if(prs.length){
   const nums=prs.map(x=>'#'+x.number).join(', ');
   push(s,'Advance existing PR(s) '+nums,
    'ALL HANDS—PR integration: check your existing '+nums+' against current base and exact-head CI. Resolve concrete review feedback, validate disjoint scope, and provide the Sol primary mjobs-luna-34 the exact next action to get these merged. If refresh or fixes are within your approved file ownership, implement, test and update without force pushing. Do not create a new overlapping PR or merge.');
   continue;
  }
  if(s.role==='Orchestration' && s.kind==='worker'){
   const area=s.name.toLowerCase().includes('quality')?'quality, failing CI, and acceptance verification':s.name.toLowerCase().includes('p0')?'remaining original P0 implementation':'P1 features and end-to-end delivery';
   push(s,'Coordinate '+area,
    'ALL HANDS—lane coordination: immediately inventory '+area+' in existing MJobs issues and PRs. Send mjobs-luna-34 a short prioritized ledger of 3-5 disjoint, specifically identified GitHub issues, acceptance tests and file scopes. Do not independently assign files or merge; once delegated, direct the existing workers on your assigned lane. Avoid duplicate investigations.');
   continue;
  }
  if(s.role.toLowerCase().includes('review')){
   let candidates=open.filter(p=>p.headRefOid&&p.headRefOid.length>=35&&!assignedPR.has(p.number)&&!this.dispatchSignals.seen('review:'+p.number+':'+p.headRefOid)&&!this.session.reviewClaims[p.number+':'+p.headRefOid]&&!this.sustain?.status.reviewHeads[p.number+':'+p.headRefOid]);
   candidates.sort((a,b)=>{
    const fa=(a.statusCheckRollup||[]).some(c=>c.conclusion==='FAILURE')?0:1;
    const fb=(b.statusCheckRollup||[]).some(c=>c.conclusion==='FAILURE')?0:1;
    return fa-fb || Number(b.number)-Number(a.number);
   });
   const p=candidates[0];
   if(p){
    assignedPR.add(p.number);
    push(s,'Independent PR #'+p.number+' review',
      'ALL HANDS—read-only review: independently inspect MJobs PR #'+p.number+' at exact commit '+p.headRefOid+'. Focus on behavioral correctness, negative testing, security regressions and acceptance proof. Summarize blockers to mjobs-luna-34. Never merge, force-push, claim the author branch, edit source or rerun tests repeatedly. If head moved, validate the new head first.','review:'+p.number+':'+p.headRefOid);
   }else{
    push(s,'Independent acceptance audit',
      'ALL HANDS—read-only verification: inspect the currently unaccepted P0 requirements and propose concrete negative tests or missing evidence without editing any shared files. Send exact issue IDs and observations to mjobs-luna-34. Avoid duplicating a review already assigned elsewhere.');
   }
   continue;
  }
  const name=s.name.toLowerCase();
  const specialty=s.role==='Architecture'?'architecture / integration boundaries':s.role==='Deep implementation'?'difficult P0/P1 implementation and acceptance gaps':s.role==='Complex implementation'?'complex feature implementation':'your established feature domain';
  push(s,'Obtain a scoped '+specialty+' task',
   'ALL HANDS—implementation: resume your existing assignment if unfinished. Otherwise inspect open unclaimed original P0/P1 requirements relevant to '+specialty+' ('+compact(s.name)+'). Immediately request a SPECIFIC GitHub issue, acceptance tests and EXCLUSIVE file ownership from mjobs-luna-34, attaching your proposed bounded slice. Begin implementation and validation once ownership is confirmed; finish a focused PR. Do not merely write a status report, duplicate another agent or modify dirty local main.');
 }
 return {
  snapshotAt:snap.timestamp,
  root:{id:root.id,mode:root.mode,status:root.status},
  working:working.map(s=>s.id),
  idle:people.length,
  plan:plans,
  skipped:skips,
  globalCooldownMinutes:globalCooldown,
  availableRamGiB:resource.freeRamGiB,
  availableDiskGiB:resource.freeDiskGiB,
  cpuPercent:resource.cpuPercent,
  pausedReason:resource.pausedReason||null,
  limitsOverridden:['hourly dispatch action budget','automatic dispatch per-cycle budget','automatic productive concurrency target'],
  cautions:['Codex subscription limits still apply','messages cannot guarantee sustained parallel work','existing task and ownership guards remain']
 };
};
Turbo.prototype.previewAllHands=async function(){
 if(this.running)throw Error('Scheduler is already running');
 const plan=await this.makeAllHandsPlan();
 return {plan:plan.plan.map(({id,name,role,reason})=>({id,name,role,reason})),skipped:plan.skipped,
   root:plan.root,snapshotAt:plan.snapshotAt,working:plan.working,resourceGuard:plan.pausedReason,
   globalCooldownMinutes:plan.globalCooldownMinutes,quotaOverride:true};
};
Turbo.prototype.allHands=async function(){
 if(this.running)throw Error('Scheduler is already running');
 if(this.isManualBusy())throw Error('Another dashboard control action is in progress');
 this.running=true;
 const sent=[],skipped=[],uncertain=[];
 try{
  const preview=await this.makeAllHandsPlan();
  if(preview.pausedReason)throw Error('All Hands paused by resource safety: '+preview.pausedReason);
  if(preview.globalCooldownMinutes>0)throw Error('All Hands already ran recently. Retry in '+preview.globalCooldownMinutes+' minute(s) to avoid duplicating work.');
  this.session.lastAllHands=new Date(this.clock()).toISOString();
  this.save();
  this.log('allhands_start',{idle:preview.idle,eligible:preview.plan.length,working:preview.working.length,overrides:preview.limitsOverridden});
  let stop=false;
  // Small, paced batches rather than 29 concurrent CLI subprocesses.
  for(let offset=0;offset<preview.plan.length&&!stop;offset+=3){
   if(this.isManualBusy())break;
   const batch=preview.plan.slice(offset,offset+3);
   const current=await this.getState(true);
   if(current.offline||(await this.signals(current)).pausedReason)break;
   const results=await Promise.all(batch.map(async agent=>{
    const live=current.sessions.find(s=>s.id===agent.id);
    if(!canReceiveInstruction(live))return {id:agent.id,skipped:true,reason:'Agent began work, needs a decision, or is no longer available'};
    if(this.session.actions.some(a=>a.id===agent.id&&a.kind==='allhands'&&recent(a.at,allHandsHoldMs,this.clock())))
     return {id:agent.id,skipped:true,reason:'Already contacted in this burst'};
    if(agent.signalKey&&!this.dispatchSignals.reserve(agent.signalKey,agent.id,this.clock()))return {id:agent.id,skipped:true,reason:'Exact-head action already reserved'};
    const rec={at:new Date(this.clock()).toISOString(),id:agent.id,kind:'allhands',key:'allhands:'+agent.id,status:'reserved',reason:agent.reason};
    this.session.actions.push(rec);this.save();
    try{
     const args=['send','--session',agent.id,'--message',agent.message];
     if(agent.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
     await this.command(args,22000);
     rec.status='submitted';if(agent.signalKey)this.dispatchSignals.finish(agent.signalKey,'submitted');this.log('allhands_sent',{id:agent.id,reason:agent.reason});
     return {id:agent.id,submitted:true,reason:agent.reason};
    }catch(e){
     rec.status='uncertain';if(agent.signalKey)this.dispatchSignals.finish(agent.signalKey,'uncertain');rec.error=errorText(e);
     this.log('allhands_uncertain',{id:agent.id,error:rec.error,diagnostic:privateDiagnostic(e)});
     return {id:agent.id,uncertain:true,reason:rec.error};
    }finally{this.save()}
   }));
   for(const r of results){
    if(r.submitted)sent.push(r);
    else if(r.skipped)skipped.push(r);
    else{uncertain.push(r);stop=true}
   }
   if(!stop&&offset+3<preview.plan.length)await new Promise(r=>setTimeout(r,300));
  }
  const result={
   at:new Date(this.clock()).toISOString(),attempted:sent.length+uncertain.length,submitted:sent.length,
   skipped:skipped.length+preview.skipped.length,uncertain:uncertain.length,workingInitially:preview.working.length,
   totalSessions:preview.working.length+preview.idle,sent,skippedDetails:[...skipped,...preview.skipped],uncertainDetails:uncertain,
   note:'Manual action overrides only Turbo internal dispatch budgets, not Codex service quota or project permission gates.'
  };
  this.session.lastAllHandsResult={at:result.at,attempted:result.attempted,submitted:result.submitted,skipped:result.skipped,uncertain:result.uncertain};
  this.session.lastSuccess=result.at;this.save();
  this.log('allhands_finished',this.session.lastAllHandsResult);
  return result;
 }catch(e){this.error(e);throw new Error(errorText(e))}
 finally{this.running=false}
};

module.exports={Turbo,DEFAULTS,configWithChanges};
