'use strict';

const fs=require('node:fs');
const path=require('node:path');
const {createHash,randomUUID}=require('node:crypto');
const {createRestorationScope}=require('./windows-runtime-evidence');
const digest=value=>createHash('sha256').update(typeof value==='string'||Buffer.isBuffer(value)?value:JSON.stringify(value)).digest('hex');
const validTime=value=>typeof value==='string'&&Number.isFinite(Date.parse(value));
const exact=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const branch=value=>typeof value==='string'&&value.length>0&&value.length<=300&&!/[\x00-\x20\x7f]/.test(value);
const absolute=value=>typeof value==='string'&&path.win32.isAbsolute(value)&&!/[\x00-\x1f\x7f]/.test(value);
const identityKeys=['id','projectId','kind','harness','model','mode'];
const ID_RE=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

function readBranchAdoptions(root,snapshot,targets,now=Date.now(),onDiagnostic=()=>{}){
 onDiagnostic(null);
 const file=path.join(root,'restore-branch-adoptions.json');
 let bytes;try{bytes=fs.readFileSync(file)}catch(error){if(error.code==='ENOENT')return null;throw Error('Reviewed branch adoption record is unreadable')}
 let value;try{if(bytes.length>1048576)throw Error();value=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''))}catch{throw Error('Reviewed branch adoption record is invalid')}
 const scope=createRestorationScope(snapshot,targets);
 if(value?.schemaVersion!==1||value.kind!=='preserved-worktree-branch-adoptions'||!validTime(value.snapshotAt)||
  typeof value.snapshotSha256!=='string'||!/^[a-f0-9]{64}$/.test(value.snapshotSha256)||
  !validTime(value.reviewedAt)||Date.parse(value.reviewedAt)<Date.parse(value.snapshotAt)||Date.parse(value.reviewedAt)>now+1000||
  typeof value.reviewReference!=='string'||!value.reviewReference.trim()||value.reviewReference.length>500||
  !Array.isArray(value.entries)||!value.entries.length||value.entries.length>200||new Set(value.entries.map(x=>x?.id)).size!==value.entries.length||
  !value.entries.every(entry=>entry&&identityKeys.every(key=>typeof entry[key]==='string'&&!!entry[key])&&entry.mode==='tui'&&
   ID_RE.test(entry.id)&&ID_RE.test(entry.projectId)&&
   ['worker','orchestrator'].includes(entry.kind)&&['codex','agy','gemini'].includes(entry.harness)&&
   branch(entry.branch)&&branch(entry.expectedBranch)&&entry.branch!==entry.expectedBranch&&validTime(entry.createdAt)&&
   Date.parse(entry.createdAt)<=Date.parse(value.snapshotAt)&&absolute(entry.workspacePath)&&absolute(entry.workspaceRepoPath)))
  throw Error('Reviewed branch adoption record is not bound to the preserved manifest');
 // A correctly formed one-time record becomes inert when the normal capture
 // path preserves a newer fleet generation. It cannot authorize that generation.
 // Same-generation byte changes, future records and malformed files still fail.
 if(scope&&Date.parse(value.snapshotAt)<Date.parse(snapshot.at)&&Date.parse(snapshot.at)<=now){
  onDiagnostic({code:'HISTORICAL_BRANCH_ADOPTION_RECORD_IGNORED',snapshotAt:value.snapshotAt});return null;
 }
 if(!scope||value.snapshotAt!==snapshot.at||value.entries.length>targets.length)throw Error('Reviewed branch adoption record is not bound to the preserved manifest');
 let snapshotBytes;try{snapshotBytes=fs.readFileSync(path.join(root,'restore-snapshot.json'))}catch{throw Error('Preserved manifest bytes are unavailable for branch adoption')}
 if(digest(snapshotBytes)!==value.snapshotSha256)throw Error('Preserved manifest bytes changed after branch review');
 const entries=value.entries.map(entry=>{
  const target=targets.find(s=>s.id===entry?.id);
  if(!target||target.mode!=='tui'||!identityKeys.every(key=>entry[key]===target[key])||entry.branch!==target.branch||
   !branch(entry.branch)||!branch(entry.expectedBranch)||entry.branch===entry.expectedBranch||
   !validTime(entry.createdAt)||Date.parse(entry.createdAt)>Date.parse(snapshot.at)||
   (target.createdAt&&entry.createdAt!==target.createdAt)||!absolute(entry.workspacePath)||!absolute(entry.workspaceRepoPath))
   throw Error('Reviewed branch adoption entry does not match its exact preserved TUI identity');
  return Object.freeze(Object.fromEntries([...identityKeys,'branch','createdAt','expectedBranch','workspacePath','workspaceRepoPath'].map(key=>[key,entry[key]])));
 });
 return Object.freeze({revision:digest(bytes),snapshotAt:value.snapshotAt,snapshotSha256:value.snapshotSha256,
  reviewedAt:value.reviewedAt,reviewReference:value.reviewReference,entries:Object.freeze(entries)});
}
function matchesEntry(entry,session,{expectedBranch=false}={}){
 return !!entry&&!!session&&identityKeys.every(key=>entry[key]===session[key])&&entry.createdAt===session.createdAt&&
  session.branch===(expectedBranch?entry.expectedBranch:entry.branch);
}
function workspaceProofMatches(proof,entry,after,now){
 return proof?.verified===true&&proof.workspacePath===entry.workspacePath&&proof.workspaceRepoPath===entry.workspaceRepoPath&&
  proof.branch===entry.expectedBranch&&validTime(proof.observedAt)&&Date.parse(proof.observedAt)>=Date.parse(after)&&Date.parse(proof.observedAt)<=now+1000;
}
function makeBranchReceipt(approval,entry,current,source,startedAt,workspaceBefore,now){
 if(!approval||!matchesEntry(entry,current)||current.isTerminated!==true||current.mode!=='tui'||
  !source?.startupRunId||source.branchAdoptions?.revision!==approval.revision||!validTime(startedAt)||
  !workspaceProofMatches(workspaceBefore,entry,approval.reviewedAt,now)||now-Date.parse(workspaceBefore.observedAt)>30000)
  throw Error('Branch adoption requires a fresh reviewed workspace and exact archived native identity');
 return {schemaVersion:1,type:'preserved-branch-adoption-receipt',receiptId:randomUUID(),nativeCommand:'restore',status:'submitted',
  approvalRevision:approval.revision,snapshotAt:approval.snapshotAt,snapshotSha256:approval.snapshotSha256,
  startupRunId:source.startupRunId,restoreStartedAt:startedAt,submittedAt:new Date(now).toISOString(),
  entry:{...entry},nativeBefore:{...Object.fromEntries([...identityKeys,'branch','createdAt'].map(key=>[key,current[key]])),isTerminated:true},
  workspaceBefore:{...workspaceBefore}};
}
function receiptForRuntime(approval,state,target,session,now){
 const receipt=state?.branchAdoptionReceipts?.[target.id],entry=approval?.entries.find(x=>x.id===target.id),source=state?.source;
 if(!receipt||!entry||receipt.schemaVersion!==1||receipt.type!=='preserved-branch-adoption-receipt'||receipt.nativeCommand!=='restore'||
  typeof receipt.receiptId!=='string'||!receipt.receiptId||!['submitted','command_succeeded','verified'].includes(receipt.status)||
  !validTime(receipt.submittedAt)||Date.parse(receipt.submittedAt)>now+1000||!validTime(receipt.restoreStartedAt)||
  Date.parse(receipt.restoreStartedAt)>Date.parse(receipt.submittedAt)||typeof receipt.startupRunId!=='string'||!receipt.startupRunId||
  receipt.approvalRevision!==approval.revision||source?.branchAdoptions?.revision!==approval.revision||
  receipt.snapshotAt!==approval.snapshotAt||receipt.snapshotSha256!==approval.snapshotSha256||source.snapshotAt!==approval.snapshotAt||
  !exact(receipt.entry,entry)||!identityKeys.every(key=>target[key]===entry[key])||target.branch!==entry.branch||
  (target.createdAt&&target.createdAt!==entry.createdAt)||!matchesEntry(entry,receipt.nativeBefore)||receipt.nativeBefore.isTerminated!==true||
  !matchesEntry(entry,session,{expectedBranch:true})||session.isTerminated!==false||
  !workspaceProofMatches(receipt.workspaceBefore,entry,approval.reviewedAt,now))return null;
 const currentOperation=receipt.restoreStartedAt===state.startedAt&&receipt.startupRunId===source.startupRunId;
 if(receipt.status==='verified'){
  if(!validTime(receipt.verifiedAt)||Date.parse(receipt.verifiedAt)<Date.parse(receipt.submittedAt)||Date.parse(receipt.verifiedAt)>now+1000||
   (!currentOperation&&source.carriedBranchReceipts?.[target.id]!==digest(receipt))||
   !workspaceProofMatches(receipt.workspaceAfter,entry,receipt.submittedAt,now))return null;
 }else if(!currentOperation||state.inflight?.[target.id]?.command!=='restore'||state.inflight[target.id].branchAdoptionReceiptId!==receipt.receiptId)return null;
 return {entry,receipt};
}
module.exports={readBranchAdoptions,matchesEntry,workspaceProofMatches,makeBranchReceipt,receiptForRuntime,digest};
