'use strict';

const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');

const WAIT_MS = 120000;
const BULK_WAIT_MS = 120000;
const BATCH_SIZE = 3;

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch { return JSON.parse(JSON.stringify(fallback)); }
}
function saveJson(file, data) {
  const temp=file+'.tmp';
  fs.writeFileSync(temp,JSON.stringify(data,null,2),'utf8');
  fs.renameSync(temp,file);
}
function ageMs(value,now) {
  const t=Date.parse(value||'');
  return Number.isFinite(t)?Math.max(0,now-t):Infinity;
}
function cleanText(value){return String(value||'').slice(0,200);}
function openPrs(session){
  return (session.prs||[]).filter(p=>(p.state==='open'||p.state==='draft')&&Number.isFinite(Number(p.number)));
}
function classify(session){
  if(session.kind==='orchestrator')return 'primary';
  const prs=openPrs(session);
  if(prs.some(p=>p.ci==='failing'||p.ci==='failed')||session.blocked)return 'ci';
  if(prs.some(p=>p.state==='draft'))return 'draft';
  if(prs.length)return 'pr';
  if((session.role||'').toLowerCase().includes('review'))return 'review';
  if(session.role==='Orchestration')return 'coordinator';
  return 'implementation';
}
function instruction(session,primaryId){
  const prs=openPrs(session);
  const numbers=prs.slice(0,8).map(p=>'#'+p.number).join(', ');
  const base='USER-REQUESTED FIND WORK: Take an actionable, bounded step rather than merely reporting that you are idle. Work in your assigned isolated worktree; do not edit dirty local main, force-push, bypass required CI/reviews, spawn extra agents, access credentials, or make real job submissions. Coordinate exact issue and file ownership with primary '+primaryId+'. Do not duplicate another agent or claim overlapping files. ';
  const kind=classify(session);
  if(kind==='primary')return {kind,message:base+
    'Act as the assignment authority. Reconcile the current open and draft PR backlog, active assignments and current-head CI. PRIORITY: clear failing CI, finish draft PRs, obtain required reviews, and merge only exact-head green approved PRs; assign idle agents concrete exclusive issues and acceptance criteria when they have no existing work. Send precise bounded instructions to existing agents now. Keep your ownership ledger current, and do not hold ready PRs indefinitely without an explicit blocker.'};
  if(kind==='ci')return {kind,message:base+
    'You own PR(s) '+numbers+' with failed checks or CI attention. FIRST diagnose exact-head failures from GitHub Actions, distinguishing flaky shared tests from defects in your change. Fix within your exclusively owned scope, run focused evidence, and update the PR normally when warranted. Seek a primary-approved path to current-head passing CI, review, and merge. Only after these PRs are resolved should you request a fresh unclaimed P0/P1 issue.'};
  if(kind==='draft')return {kind,message:base+
    'You have unfinished DRAFT PR(s) '+numbers+'. Finish the existing PR(s) FIRST: identify what prevents Ready for Review, implement missing acceptance/test coverage within your owned scope, refresh against current main when released by primary, and obtain exact-head CI/reviews. Mark ready only when genuinely complete and permitted. Do not open a redundant new PR; ask for new P0/P1 work only after those PRs are resolved or explicitly transferred.'};
  if(kind==='pr')return {kind,message:base+
    'You own open PR(s) '+numbers+'. FIRST determine concrete integration blockers: latest-base drift, exact-head CI, required human/provider review, or unresolved comments. Repair owned defects and publish a tested non-force update when needed; request a primary merge/review decision when clean. Do not open a duplicate PR. Once all owned PRs are merged, closed, or reassigned, find the next unclaimed P0/P1 issue and obtain an exclusive file reservation.'};
  if(kind==='review')return {kind,message:base+
    'Find a useful independent READ-ONLY review. Check current MJobs open PRs and the primary review ledger; propose one unreviewed PR head with a unique assignment and request coordination before proceeding. Evaluate acceptance evidence, negative cases, current-head CI and high-confidence blockers; relay actionable findings to the primary. Avoid duplicate review, write operations, approvals/merges or needless repeated checks. If nothing needs review, identify missing P0 acceptance evidence.'};
  if(kind==='coordinator')return {kind,message:base+
    'Coordinate your existing lane (P0, P1, or quality). Reconcile at least one unassigned bounded requirement or waiting CI/review decision with the primary and propose exact issue IDs, acceptance checks and exclusive files. If the primary has delegated ownership, actually assign next steps to eligible workers. If no work is unclaimed, help clear blockers rather than sending another status-only report; do not independently merge or create duplicate claims.'};
  return {kind,message:base+
    'Check your current task, uncommitted work, and existing GitHub assignments first. If unfinished, IMPLEMENT OR VALIDATE THE NEXT CONCRETE STEP toward a passing PR. If your prior task is complete, search current original MJobs P0/P1 issues for an unclaimed requirement suitable for your skills and propose one exact issue ID, acceptance tests, and nonoverlapping file scope to primary. Begin work only after exclusive ownership is confirmed, and aim for a tested, reviewable PR rather than a status report.'};
}

class FindWork {
 constructor({root,getState,command,audit,guard=()=>null,now=()=>Date.now()}){
  this.file=path.join(root,'find-work-state.json');
  this.state=readJson(this.file,{sent:{},lastBulk:null,history:[]});
  this.getState=getState;this.command=command;this.audit=audit;this.guard=guard;this.now=now;
  this.running=false;
 }
 save(){
  const cutoff=this.now()-24*3600000;
  this.state.history=(this.state.history||[]).filter(x=>ageMs(x.at,this.now())<24*3600000).slice(-350);
  this.state.sent=Object.fromEntries(Object.entries(this.state.sent||{}).filter(([,v])=>ageMs(v.at,this.now())<24*3600000));
  saveJson(this.file,this.state);
 }
 eligibility(s){
  if(!s)return 'Session not found';
  if(s.isTerminated||s.activity==='exited')return 'Agent is intentionally exited; restore it separately before assigning work';
  if(!s.isIdle||s.activity!=='idle')return 'Agent has an active turn or is not ready';
  if(s.replacedBy)return 'Agent has a replacement session; nudge the replacement instead';
  const prior=this.state.sent[s.id];
  const remaining=WAIT_MS-ageMs(prior?.at,this.now());
  if(remaining>0)return 'Find Work already requested for this agent. Retry in '+Math.ceil(remaining/1000)+' seconds';
  return null;
 }
 async preview(){
  const hold=this.guard();if(hold)throw Error(hold);
  const snapshot=await this.getState(true);
  const eligible=[],skipped=[];
  for(const session of snapshot.sessions||[]){
   const reason=this.eligibility(session);
   if(reason)skipped.push({id:session.id,reason});
   else eligible.push({id:session.id,name:session.name,role:session.role,category:classify(session),openPRs:session.openPRs});
  }
  eligible.sort((a,b)=>{
   const rank=x=>({primary:0,ci:1,draft:2,pr:3,coordinator:4,review:5,implementation:6})[x.category]??7;
   return rank(a)-rank(b)||a.id.localeCompare(b.id,undefined,{numeric:true});
  });
  const globalMs=BULK_WAIT_MS-ageMs(this.state.lastBulk,this.now());
  return {at:new Date(this.now()).toISOString(),eligible,skipped,
   eligibleCount:eligible.length,skippedCount:skipped.length,
   bulkCooldownSeconds:Math.max(0,Math.ceil(globalMs/1000)),
   working:(snapshot.sessions||[]).filter(s=>s.busy).length,
   notes:['Existing PRs take priority over new work','No additional sessions or merge bypasses','Messages being accepted is not proof of completed work']};
 }
 async sendOne(id){
  if(this.running)throw Error('Another Find Work dispatch is in progress');
  if(this.guard())throw Error(this.guard());
  this.running=true;
  try{return await this.dispatchOne(id)}
  finally{this.running=false}
 }
 async dispatchOne(id){
  if(this.guard())throw Error(this.guard());
  const snap=await this.getState(true);
  const session=(snap.sessions||[]).find(s=>s.id===id);
  const reason=this.eligibility(session);
  if(reason)throw Error(reason);
  const primary=(snap.sessions||[]).find(s=>s.kind==='orchestrator'&&!s.isTerminated&&!s.replacedBy&&s.activity!=='exited')?.id||'the project primary orchestrator';
  const task=instruction(session,primary);
  // Reserve before delivery: on timeout, don't submit the same prompt blindly again.
  const stamp=new Date(this.now()).toISOString();
  this.state.sent[id]={at:stamp,category:task.kind,status:'reserved'};
  this.save();
  const args=['send','--session',id,'--message',task.message];
  if(session.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
  try{
   const output=await this.command(args,25000);
   const rec={at:stamp,id,category:task.kind,status:'submitted'};
   this.state.sent[id].status='submitted';this.state.history.push(rec);this.save();
   this.audit('find_work_sent',rec);
   return {id,category:task.kind,status:'submitted',note:'Find Work instruction accepted by AO. Actual activity is verified separately.',output:cleanText(output)};
  }catch(e){
   const message=cleanText(e?.message||e);
   this.state.sent[id].status='uncertain';this.state.sent[id].error=message;
   this.state.history.push({at:stamp,id,category:task.kind,status:'uncertain'});
   this.save();this.audit('find_work_uncertain',{id,category:task.kind,error:message});
   throw Error('Delivery could not be confirmed for '+id+'. Check agent history/activity before retrying; original tasks are preserved.');
  }
 }
 async sendAll(){
  if(this.running)throw Error('Another Find Work dispatch is in progress');
  if(this.guard())throw Error(this.guard());
  const p=await this.preview();
  if(p.bulkCooldownSeconds>0)throw Error('A fleet-wide Find Work request ran recently; retry in '+p.bulkCooldownSeconds+' seconds.');
  if(!p.eligibleCount)return {sent:[],skipped:p.skipped,uncertain:[],note:'No eligible idle agents'};
  this.running=true;
  this.state.lastBulk=new Date(this.now()).toISOString();this.save();
  const sent=[],skipped=[...p.skipped],uncertain=[];
  try{
   for(let i=0;i<p.eligible.length;i+=BATCH_SIZE){
    if(this.guard())break;
    const group=p.eligible.slice(i,i+BATCH_SIZE);
    // Delivery is deliberately paced to avoid overwhelming the AO daemon.
    const results=await Promise.all(group.map(async s=>{
     try{return {ok:true,data:await this.dispatchOne(s.id)}}
     catch(e){return {ok:false,id:s.id,error:cleanText(e?.message||e)}}
    }));
    for(const r of results){
     if(r.ok)sent.push(r.data);
     else if(/active turn|not ready|exited|replacement|already requested/i.test(r.error))skipped.push({id:r.id,reason:r.error});
     else uncertain.push({id:r.id,reason:r.error});
    }
    if(i+BATCH_SIZE<p.eligible.length)await new Promise(resolve=>setTimeout(resolve,350));
   }
   const result={at:new Date(this.now()).toISOString(),sent,skipped,uncertain,submittedCount:sent.length,skippedCount:skipped.length,uncertainCount:uncertain.length,
    note:'All eligible idle sessions were offered concrete PR/CI/review or assignment instructions. Some may quickly return to idle.'};
   this.state.history.push({at:result.at,id:'fleet',category:'all',status:'complete',submittedCount:sent.length});
   this.save();this.audit('find_work_all_complete',{submitted:sent.length,skipped:skipped.length,uncertain:uncertain.length});
   return result;
  }finally{this.running=false}
 }
 status(){return {lastBulk:this.state.lastBulk,sentLastHour:(this.state.history||[]).filter(x=>x.id!=='fleet'&&ageMs(x.at,this.now())<3600000).length,recent:(this.state.history||[]).slice(-12)}}
}

module.exports={FindWork,classify,instruction};
