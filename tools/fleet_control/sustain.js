'use strict';
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {randomUUID}=require('node:crypto');
const execAsync=promisify(execFile);
const GiB=1024**3;
const DEF=Object.freeze({
 enabled:false,
 intervalSeconds:60,
 minFreeRamGiB:9,
 minFreeDiskGiB:25,
 maxCpuPercent:92,
 targetWorking:12,
 maxDispatchPerTick:2,
 minQuietSeconds:180,
 repeatNoProgressBackoffMinutes:20,
 rootCooldownMinutes:10,
 resourceHeavyTestLanes:2
});
const parse=x=>{const v=Date.parse(x||'');return Number.isFinite(v)?v:0};
const clone=x=>JSON.parse(JSON.stringify(x));
const short=e=>String(e?.message||e).slice(0,550);
function atomic(p,x){const t=p+'.tmp';fs.writeFileSync(t,JSON.stringify(x,null,2),'utf8');fs.renameSync(t,p)}
function read(p,x){try{return JSON.parse(fs.readFileSync(p,'utf8'))}catch{return clone(x)}}
function ensureInt(v,min,max,k){if(!Number.isInteger(v)||v<min||v>max)throw Error(k+' must be an integer from '+min+' to '+max)}
function verifySettings(x){
 ensureInt(x.intervalSeconds,30,180,'intervalSeconds');
 ensureInt(x.minFreeRamGiB,6,32,'minFreeRamGiB');
 ensureInt(x.minFreeDiskGiB,10,120,'minFreeDiskGiB');
 ensureInt(x.maxCpuPercent,70,100,'maxCpuPercent');
 ensureInt(x.targetWorking,3,29,'targetWorking');
 ensureInt(x.maxDispatchPerTick,1,12,'maxDispatchPerTick');
 ensureInt(x.minQuietSeconds,60,1200,'minQuietSeconds');
 ensureInt(x.repeatNoProgressBackoffMinutes,5,120,'repeatNoProgressBackoffMinutes');
 ensureInt(x.rootCooldownMinutes,2,60,'rootCooldownMinutes');
 ensureInt(x.resourceHeavyTestLanes,1,5,'resourceHeavyTestLanes');
 return x;
}
const denyTaskWords=/\b(apply to actual jobs|real employer submission|force push|force-push|delete production|credentials|password)\b/i;
class Sustain {
 constructor({root,getState,turbo,command,audit,isManualBusy=()=>false,clock=()=>Date.now(),
  issueRunner=null,autoSchedule=true}){
  this.root=root;this.getState=getState;this.turbo=turbo;
  this.command=command;this.audit=audit;this.isManualBusy=isManualBusy;this.clock=clock;
  this.configPath=path.join(root,'sustain-config.json');
  this.statePath=path.join(root,'sustain-state.json');
  this.eventsPath=path.join(root,'sustain-events.jsonl');
  this.taskPath=path.join(root,'sustain-task-bank.json');
  this.config=verifySettings({...DEF,...read(this.configPath,{})});
  this.status=read(this.statePath,{agents:{},leases:{},reviewHeads:{},ticks:0,lastTick:null,lastSuccess:null,lastError:null,quotaPausedUntil:null,sentTotal:0,dispatches:[]});
  this.status.agents||={};this.status.leases||={};this.status.reviewHeads||={};this.status.dispatches||=[];
  this.taskBank=read(this.taskPath,[]);
  this.issueRunner=issueRunner||this.defaultIssueRunner.bind(this);
  this.issueCache=new Map();this.running=false;
  this.timer=null;this.nextTick=null;this.latest=null;
  if(autoSchedule)this.schedule();
 }
 save(){
  this.status.dispatches=this.status.dispatches.slice(-350);
  atomic(this.statePath,this.status);
 }
 log(event,record={}){
  const row={at:new Date(this.clock()).toISOString(),event,...record};
  fs.appendFileSync(this.eventsPath,JSON.stringify(row)+'\n');
  this.audit('sustain_'+event,record);
 }
 schedule(){
  if(this.timer)clearInterval(this.timer);
  if(!this.config.enabled){this.timer=null;this.nextTick=null;return}
  this.nextTick=new Date(this.clock()+this.config.intervalSeconds*1000).toISOString();
  this.timer=setInterval(()=>{
   this.tick().catch(e=>this.failure(e));
   this.nextTick=new Date(this.clock()+this.config.intervalSeconds*1000).toISOString();
  },this.config.intervalSeconds*1000);
  this.timer.unref?.();
 }
 enable(enabled){
  if(typeof enabled!=='boolean')throw Error('enabled must be boolean');
  this.config.enabled=enabled;atomic(this.configPath,this.config);
  this.log(enabled?'enabled':'disabled',{});
  this.schedule();
  if(enabled&&this.timer){setTimeout(()=>this.tick().catch(e=>this.failure(e)),200).unref?.()}
  return this.summary();
 }
 update(changes){
  const allowed=new Set(Object.keys(DEF).filter(k=>k!=='enabled'));
  for(const k of Object.keys(changes||{}))if(!allowed.has(k))throw Error('Unknown setting '+k);
  this.config=verifySettings({...this.config,...changes});
  atomic(this.configPath,this.config);this.schedule();
  this.log('settings',{changes});
  return this.summary();
 }
 summary(){
  const recent=this.status.dispatches.filter(x=>this.clock()-parse(x.at)<3600000);
  return {
   enabled:this.config.enabled,running:this.running,config:clone(this.config),nextTick:this.nextTick,
   lastTick:this.status.lastTick,lastSuccess:this.status.lastSuccess,lastError:this.status.lastError,
   quotaPausedUntil:this.status.quotaPausedUntil,
   sentTotal:this.status.sentTotal||0,sentThisHour:recent.length,
   leases:Object.values(this.status.leases).map(l=>({issue:l.issue,session:l.session,scope:l.scope,state:l.state,at:l.at})),
   activity:this.status.dispatches.slice(-12).reverse(),latest:this.latest
  };
 }
 async defaultIssueRunner(n){
  const x=await execAsync('gh.exe',['issue','view',String(n),'--repo','MulattoTech/MJobs','--json','number,title,state,labels,assignees'],{windowsHide:true,timeout:17000,maxBuffer:160000});
  return JSON.parse(x.stdout);
 }
 async issue(n){
  const cached=this.issueCache.get(n);
  if(cached&&this.clock()-cached.at<600000)return cached.value;
  const value=await this.issueRunner(n);this.issueCache.set(n,{value,at:this.clock()});
  return value;
 }
 fingerprint(session,gh){
  const ps=(session.prs||[]).filter(x=>['open','draft'].includes(x.state)).map(p=>{
   const live=(gh?.open||[]).find(x=>Number(x.number)===Number(p.number));
   const failures=(live?.statusCheckRollup||[]).filter(x=>x.conclusion==='FAILURE'||x.conclusion==='TIMED_OUT').length;
   return String(p.number)+':'+(live?.headRefOid||'')+':'+(live?.isDraft?'draft':'open')+':'+failures+':'+(live?.reviewDecision||'');
  }).sort();
  return [session.status||'',session.branchState?.commits||0,session.branchState?.unpushed||0,session.openPRs||0,...ps].join('|');
 }
 observe(sessions,gh){
  const now=this.clock();
  for(const s of sessions){
   const a=this.status.agents[s.id] ||= {};
   const fingerprint=this.fingerprint(s,gh);
   if(a.lastSent&&(s.busy||s.activity==='active'||s.activity==='working')){
    // A running turn confirms delivery even when AO's activity timestamp is
    // the turn-start time. Never time out a still-running turn as undelivered.
    a.awaiting=true;a.observedActiveAt=new Date(now).toISOString();
    if(a.backoffReason==='unverified_delivery'||(!a.backoffReason&&(a.unverified||0)>0)){
     delete a.backoffUntil;delete a.backoffReason;
    }
    a.unverified=0;
    continue;
   }
   if(a.lastSent && a.awaiting){
    const sent=parse(a.lastSent);
    const activity=parse(s.lastActivity);
    if(parse(a.observedActiveAt)>=sent||activity>sent+300){
     a.awaiting=false;a.completedAt=s.lastActivity;
     a.noProgress=(a.lastFingerprint===fingerprint)?(a.noProgress||0)+1:0;
     a.lastFingerprint=fingerprint;
     a.lastObservedActivity=activity;
    }else if(now-sent>180000){
     a.awaiting=false;a.unverified=(a.unverified||0)+1;
     a.backoffUntil=new Date(now+Math.min(45,a.unverified*12)*60000).toISOString();
     a.backoffReason='unverified_delivery';
     this.log('unverified_delivery',{id:s.id,backoffUntil:a.backoffUntil});
    }
   }
   if(!a.lastFingerprint)a.lastFingerprint=fingerprint;
  }
 }
 possibleDispatch(session,type,key,message,reason,now,extra={}){
  if(!session?.isIdle||session.replacedBy||session.activity==='waiting_input')return null;
  const data=this.status.agents[session.id]||{};
  if(data.awaiting)return null;
  if(parse(data.backoffUntil)>now)return null;
  const quiet=this.config.minQuietSeconds*1000;
  if(now-parse(session.lastActivity)<quiet)return null;
  if(now-parse(data.lastSent)<quiet)return null;
  const same=data.lastKey===key;
  if(same){
   if(type==='root'&&now-parse(data.lastSent)<this.config.rootCooldownMinutes*60000)return null;
   if(data.noProgress>=2&&now-parse(data.lastSent)<this.config.repeatNoProgressBackoffMinutes*60000)return null;
   if(data.noProgress>=4&&now-parse(data.lastSent)<45*60000)return null;
  }
  return {id:session.id,mode:session.mode,name:session.name,type,key,message,reason,priority:extra.priority??5,issue:extra.issue||null};
 }
 async taskFor(s,openPRs){
  const entries=this.taskBank.filter(e=>e.session===s.id);
  for(const task of entries){
   if(openPRs.some(p=>RegExp('(?:#|mj.req.)'+task.issue+'(?:\\D|$)','i').test(p.title||'')))continue;
   const otherLease=Object.values(this.status.leases).find(l=>l.issue===task.issue&&l.session!==s.id&&l.state!=='complete');
   if(otherLease)continue;
   try{
    const issue=await this.issue(task.issue);
    if(issue.state!=='OPEN')continue;
    const assignees=issue.assignees||[];
    if(assignees.length>0)continue;
    if(denyTaskWords.test(task.focus||''))continue;
    const active=this.status.leases[task.issue];
    if(active&&active.state==='complete')continue;
    const paths=task.scope||[];
    if(!paths.length||paths.some(p=>!/^((tests|docs)\/)[a-z0-9_./-]+$/.test(p)||p.includes('..')))continue;
    const collision=Object.values(this.status.leases).some(l=>l.issue!==task.issue&&l.state!=='complete'&&(l.scope||[]).some(p=>paths.includes(p)));
    if(collision)continue;
    return {task,issue};
   }catch(e){this.log('issue_unavailable',{id:s.id,issue:task.issue,error:short(e)})}
  }
  return null;
 }
 async buildPlan(){
  const snap=await this.getState(true);
  const resources=await this.turbo.signals(snap);
  // Honor Sustained Mode's separate resource envelope, not the old Turbo budget.
  resources.pausedReason=null;
  if(resources.freeRamGiB<this.config.minFreeRamGiB)
   resources.pausedReason='Low available RAM ('+resources.freeRamGiB+' GiB)';
  else if(resources.freeDiskGiB!==null&&resources.freeDiskGiB<this.config.minFreeDiskGiB)
   resources.pausedReason='Low disk space ('+resources.freeDiskGiB+' GiB)';
  else if(Number.isFinite(resources.cpuPercent)&&resources.cpuPercent>=this.config.maxCpuPercent)
   resources.throttledReason='CPU pressure ('+resources.cpuPercent+'%): using one lightweight dispatch per cycle while RAM is safe';
  const gh=await this.turbo.github();
  const now=this.clock();
  this.observe(snap.sessions,gh);
  const prs=(gh?.open||[]).filter(p=>p.number>0);
  const liveRoot=snap.sessions.find(s=>s.kind==='orchestrator'&&!s.isTerminated&&!s.replacedBy&&s.activity!=='exited');
  const working=snap.sessions.filter(s=>s.busy).length;
  const waiting=snap.sessions.filter(s=>s.activity==='waiting_input').length;
  const overdueLeases=[];
  const actions=[],seenPR=new Set();
  let blocked=resources.pausedReason||null;
  if(this.status.quotaPausedUntil&&parse(this.status.quotaPausedUntil)>now)blocked='Provider usage/rate limit pause until '+this.status.quotaPausedUntil;
  if(this.isManualBusy()||this.turbo.running)blocked='Another control operation is running';
  const root=liveRoot;
  if(!root)blocked='No live primary orchestrator';
  if(snap.offline)blocked='Orchestrator roster is an offline snapshot';
  if(!gh||gh.fresh===false)blocked='GitHub snapshot unavailable or stale; no automatic assignments';
  for(const s of snap.sessions){
   if(!s.isIdle||s.replacedBy)continue;
   const owned=(s.prs||[]).filter(p=>['open','draft'].includes(p.state)&&prs.some(g=>g.number===p.number));
   let message=null,reason='',type='',key='',priority=5,issueId=null;
   if(s.kind==='orchestrator'){
    type='root';priority=0;key='root:'+prs.map(p=>p.number+':'+p.headRefOid).join(',');
    reason='Coordinate issue ownership and PR integration';
    message='SUSTAINED FLEET CHECKPOINT: The user-authorized Fleet Control scheduler reserves issues and files; treat these leases as exclusive: '+Object.values(this.status.leases).map(l=>'#'+l.issue+' to '+l.session+' files '+(l.scope||[]).join('+')).slice(0,18).join('; ')+'. You are the primary integration owner. Current live sessions: '+snap.summary.total+', actively working: '+working+', idle: '+snap.summary.idle+', open PRs: '+prs.map(p=>'#'+p.number).join(', ')+'. Reconcile the live ledger and assign actual exclusive P0/P1 issues and acceptance tests to idle agents. Workers should not simply request assignments repeatedly. Enforce CI and review before merges, keep no more than '+this.config.resourceHeavyTestLanes+' heavy local tests, preserve six modified main files and all isolated worktrees. Report bounded issue/file reservations and actual merged PRs; no new agents, force pushes or real application submissions.';
   }else if(owned.length){
    const failing=owned.filter(p=>{
     const g=prs.find(x=>x.number===p.number);
     return p.ci==='failing'||(g?.statusCheckRollup||[]).some(c=>c.conclusion==='FAILURE'||c.conclusion==='TIMED_OUT');
    });
    const list=(failing.length?failing:owned);
    const tags=list.map(p=>{
     const g=prs.find(x=>x.number===p.number);
     return '#'+p.number+'@'+String(g?.headRefOid||'').slice(0,12);
    });
    key=(failing.length?'ci:':'pr:')+tags.join(',');
    type=failing.length?'ci':'pr';priority=failing.length?1:2;
    reason=(failing.length?'Repair failing CI':'Advance existing PR')+' '+tags.join(', ');
    message='SUSTAINED MJobs '+(failing.length?'CI REMEDIATION':'PR INTEGRATION')+': You own existing '+tags.join(', ')+'. Re-check its exact head and current checks. Fix specific proven defects within your owned file paths, run focused tests then publish normally without force-push; if passing, seek required review and merge via primary coordinator. Do not create duplicate PRs or touch dirty local main. Continue to actionable completion rather than sending status alone; report the blocker only if truly external.';
   }else if(s.role?.toLowerCase().includes('review')){
    const candidates=prs.filter(p=>p.headRefOid&&!seenPR.has(p.number)&&!this.status.reviewHeads[p.number+':'+p.headRefOid]);
    candidates.sort((a,b)=>{
     const score=x=>(x.statusCheckRollup||[]).some(c=>c.conclusion==='FAILURE')?0:1;
     return score(a)-score(b)||b.number-a.number;
    });
    if(candidates.length){
     const p=candidates[0];seenPR.add(p.number);
     type='review';priority=3;issueId=null;key='review:'+p.number+':'+p.headRefOid;
     reason='Independent review of PR #'+p.number;
     message='SUSTAINED INDEPENDENT REVIEW: Inspect MJobs PR #'+p.number+' at exact current SHA '+p.headRefOid+'. Analyze acceptance criteria, security, edge cases and negative tests. Record high-confidence blockers to primary '+(root?.id||'mjobs-luna-35')+'. Do not change files, approve/merge on behalf of user, or redo the same unchanged head. If PR has a failure, identify a concrete root cause and owner action.';
    }
   }else if(s.role==='Orchestration'){
    type='coord';priority=4;key='coord:'+s.id;
    reason='Delegate disjoint P0/P1 workstream';
    message='SUSTAINED COORDINATION: In your '+s.name+' domain, propose 3 concrete open, unclaimed P0/P1 issues with exclusive files/acceptance to primary '+(root?.id||'mjobs-luna-35')+'. You may deliver explicit assignments only for already reserved issue/file scopes in the Fleet Control task ledger. Avoid duplicate claims; no new agents, no unreviewed merges.';
   }else {
    const found=await this.taskFor(s,prs);
    if(found){
     const {task,issue}=found;issueId=task.issue;
     const paths=task.scope.join(', ');
     const lease=this.status.leases[task.issue];
     type='issue';priority=4;key='issue:'+task.issue+':'+(lease?.state||'unassigned');
     reason='Execute P1/P0 issue #'+task.issue+' '+issue.title;
     message='USER-AUTHORIZED SUSTAINED TASK ASSIGNMENT: MJobs GitHub issue #'+task.issue+' ('+issue.title+'). This Fleet Control dispatcher reserves EXCLUSIVE WRITE OWNERSHIP of only '+paths+' to you, '+s.id+'. Goal: '+task.focus+'. Read the full issue and relevant existing implementation, add meaningful regression evidence and implement a tested focused PR within ONLY those files. You may inspect production paths read-only; if a production change or another shared file is required, report that exact ownership escalation to primary '+(root?.id||'mjobs-luna-35')+' and DO NOT modify the unreserved path. All other workers must not write these paths. Work in your isolated AO worktree, never dirty local main. Include test results, exact-head CI evidence, PR and commit provenance; do not merge, force push, change protected settings or make real applications. Continue implementation directly instead of asking for the same issue assignment again.';
    }else if(!this.status.agents[s.id]?.askedForTask){
     type='refill';priority=7;key='refill:'+s.id;reason='Request a new approved issue';
     message='SUSTAINED TASK REFILL: you have no currently assigned PR or reserved workstream. Propose exactly one concrete open P0/P1 issue with its acceptance and nonoverlapping file paths to primary '+(root?.id||'mjobs-luna-35')+'. Do not modify files until your issue and files are expressly reserved. Avoid repeated research or status-only output.';
    }
   }
   if(message){
    const x=this.possibleDispatch(s,type,key,message,reason,now,{priority,issue:issueId});
    if(x)actions.push(x);
   }
  }
  const fairScore=a=>{
   const prior=this.status.agents[a.id]||{};
   const age=(now-parse(prior.lastSent))/60000;
   let extra=0;
   if(prior.lastSent&&age<12)extra+=6;
   else if(prior.lastSent&&age<30)extra+=2;
   if((prior.noProgress||0)>=2)extra+=4;
   return a.priority+extra;
  };
  actions.sort((a,b)=>fairScore(a)-fairScore(b)||a.id.localeCompare(b.id,undefined,{numeric:true}));
  const capacity=Math.max(0,this.config.targetWorking-working);
  const selected=actions.slice(0,Math.min(capacity,resources.throttledReason?1:this.config.maxDispatchPerTick));
  return {
   snapshot:snap,gh,resources,blocked,working,waiting,rootId:root?.id||null,
   proposed:actions.map(({id,type,priority,reason,issue})=>({id,type,priority,reason,issue})),
   selected,eligible:actions.length,leases:Object.keys(this.status.leases).length
  };
 }
 async preview(){
  if(this.running)throw Error('Sustained dispatch already running');
  const p=await this.buildPlan();
  this.latest={at:new Date(this.clock()).toISOString(),enabled:this.config.enabled,blocked:p.blocked,working:p.working,waiting:p.waiting,rootId:p.rootId,resources:p.resources,eligible:p.eligible,proposed:p.proposed.slice(0,25),selected:p.selected.map(x=>({id:x.id,type:x.type,reason:x.reason,issue:x.issue})),leases:p.leases};
  return clone(this.latest);
 }
 async tick({forced=false,dryRun=false}={}){
  if(this.running)return {skipped:'already running'};
  if(!this.config.enabled&&!forced)return {skipped:'disabled'};
  this.running=true;
  const now=this.clock();
  const selected=[];
  try{
   const p=await this.buildPlan();
   this.latest={at:new Date(now).toISOString(),enabled:this.config.enabled,blocked:p.blocked,working:p.working,waiting:p.waiting,rootId:p.rootId,resources:p.resources,eligible:p.eligible,proposed:p.proposed.slice(0,25),selected:p.selected.map(x=>({id:x.id,type:x.type,reason:x.reason,issue:x.issue})),leases:p.leases};
   this.status.lastTick=new Date(now).toISOString();this.status.ticks=(this.status.ticks||0)+1;
   if(p.blocked||dryRun){this.save();return {sent:[],blocked:p.blocked,preview:this.latest,dryRun}}
   for(const action of p.selected){
    if((!this.config.enabled&&!forced)||this.isManualBusy())break;
    const live=await this.getState(true);
    if(live.offline||live.sessions.filter(s=>s.busy).length>=this.config.targetWorking)break;
    const resources=await this.turbo.signals(live);
    if(resources.freeRamGiB<this.config.minFreeRamGiB||(resources.freeDiskGiB!==null&&resources.freeDiskGiB<this.config.minFreeDiskGiB))break;
    const current=live.sessions.find(s=>s.id===action.id);
    if(!current?.isIdle||current.replacedBy)continue;
    const a=this.status.agents[action.id]||{};
    if(a.awaiting&&parse(a.lastSent)>0)continue;
    if(action.type==='review'){
     const k=action.key.slice('review:'.length);
     if(this.status.reviewHeads[k])continue;
    }
    if(action.type==='issue'){
     const task=this.taskBank.find(t=>t.issue===action.issue&&t.session===action.id);
     if(!task)continue;
     const previous=this.status.leases[task.issue];
     if(previous&&previous.session!==action.id)continue;
     this.status.leases[task.issue]={issue:task.issue,session:action.id,scope:task.scope,state:'assigned',at:new Date(this.clock()).toISOString()};
    }
    const stamp=new Date(this.clock()).toISOString();
    this.status.agents[action.id]={...a,lastSent:stamp,lastKey:action.key,lastFingerprint:this.fingerprint(current,p.gh),awaiting:true,observedActiveAt:null,backoffReason:null,askedForTask:action.type==='refill'||a.askedForTask||false};
    this.save();
    const args=['send','--session',action.id,'--message',action.message];
    if(current.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
    try{
     await this.command(args,25000);
     if(action.type==='review')this.status.reviewHeads[action.key.slice('review:'.length)]={at:stamp,reviewer:action.id};
     const row={at:stamp,id:action.id,type:action.type,issue:action.issue,status:'submitted',reason:action.reason};
     this.status.dispatches.push(row);this.status.sentTotal=(this.status.sentTotal||0)+1;
     this.log('sent',row);this.status.lastSuccess=stamp;
     selected.push({id:action.id,type:action.type,reason:action.reason});
    }catch(e){
     const message=short(e);
     this.status.agents[action.id].awaiting=false;
     if(action.issue&&this.status.leases[action.issue])this.status.leases[action.issue].state='delivery_uncertain';
     this.status.agents[action.id].backoffUntil=new Date(this.clock()+20*60000).toISOString();
     this.status.agents[action.id].backoffReason='dispatch_error';
     if(/(?:rate.limit|usage.limit|quota|too many requests|429|exhausted)/i.test(message)){
      this.status.quotaPausedUntil=new Date(this.clock()+20*60000).toISOString();
      this.log('provider_pause',{message,until:this.status.quotaPausedUntil});
     }else this.log('uncertain',{id:action.id,message});
     this.status.lastError=message;
     this.save();
     break;
    }
    this.save();
   }
   this.save();
   return {sent:selected,blocked:null,preview:this.latest};
  }catch(e){
   this.failure(e);
   if(forced)throw e;
   return {sent:selected,error:short(e)};
  }finally{this.running=false}
 }
 failure(e){this.status.lastError=short(e);this.log('error',{message:this.status.lastError});this.save()}
}
module.exports={Sustain,DEF,verifySettings};
