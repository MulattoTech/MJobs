'use strict';

const fs=require('node:fs');
const path=require('node:path');
const {randomUUID}=require('node:crypto');
const {digest,matchesEntry,workspaceProofMatches}=require('./branch-adoptions');
const {createRestorationScope}=require('./windows-runtime-evidence');

const REVIEW_FILE='restore-post-submission-review.json';
const ID=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SHA=/^[a-f0-9]{64}$/;
const UUID=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const identityKeys=['id','projectId','kind','harness','model','mode'];
const time=value=>typeof value==='string'&&Number.isFinite(Date.parse(value));
const absolute=value=>typeof value==='string'&&path.win32.isAbsolute(value)&&!/[\x00-\x1f\x7f]/.test(value);
const branch=value=>typeof value==='string'&&value.length>0&&value.length<=300&&!/[\x00-\x20\x7f]/.test(value);
const pathKey=value=>typeof value==='string'?path.win32.normalize(value.replace(/^\\\\\?\\/,'' )).replace(/\\+$/,'').toLowerCase():null;
function instantKey(value){
 if(typeof value!=='string')return null;
 const text=value.replace(' ','T').replace(/ \+0000 UTC$/,'Z').replace(/\+00:00$/,'Z');
 const match=/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(text);
 return match&&time(text)?match[1]+'.'+(match[2]||'').padEnd(9,'0')+'Z':null;
}
const equal=(left,right)=>digest(left)===digest(right);
const exactIds=(left,right)=>Array.isArray(left)&&Array.isArray(right)&&new Set(left).size===left.length&&new Set(right).size===right.length&&left.length===right.length&&left.every(id=>typeof id==='string'&&right.includes(id));
function fail(){throw Error('Post-submission review is invalid or no longer bound to the preserved operation');}
function freeze(value){if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;}
function readSmall(file,max=2*1024*1024){const bytes=fs.readFileSync(file);if(bytes.length>max)fail();return bytes;}
function parse(bytes){try{return JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,''));}catch{fail();}}
function runtimeShape(value){
 return value&&Object.keys(value).sort().join(',')===['nativeSessionId','providerPid','providerCreatedAt','providerExecutable','hostPid','hostCreatedAt','terminalGeneration'].sort().join(',')&&
  UUID.test(value.nativeSessionId||'')&&absolute(value.providerExecutable)&&['providerPid','hostPid'].every(key=>Number.isInteger(value[key])&&value[key]>0&&value[key]<=4294967295)&&
  value.providerPid!==value.hostPid&&time(value.providerCreatedAt)&&time(value.hostCreatedAt)&&
  Date.parse(value.providerCreatedAt)>=Date.parse(value.hostCreatedAt)&&Object.hasOwn(value,'terminalGeneration')&&
  (value.terminalGeneration===null||typeof value.terminalGeneration==='string');
}
function readPostSubmissionReview(root,snapshot,targets,now=Date.now()){
 let bytes;try{bytes=readSmall(path.join(root,REVIEW_FILE));}catch(error){if(error.code==='ENOENT')return null;throw error;}
 const value=parse(bytes);
 if(value?.schemaVersion!==1||value.kind!=='post-submission-workspace-review'||!time(value.reviewedAt)||
  !time(value.snapshotAt)||!time(value.restoreStartedAt)||Date.parse(value.snapshotAt)>Date.parse(value.restoreStartedAt)||
  Date.parse(value.restoreStartedAt)>Date.parse(value.reviewedAt)||Date.parse(value.reviewedAt)>now+1000||
  !ID.test(value.startupRunId||'')||typeof value.reviewReference!=='string'||!value.reviewReference.trim()||value.reviewReference.length>1000||
  !['snapshotSha256','sourceSha256','desiredIdsSha256','restoreStateSha256'].every(key=>SHA.test(value[key]||''))||
  !value.historyEvidence||typeof value.historyEvidence.file!=='string'||!ID.test(value.historyEvidence.file)||!value.historyEvidence.file.endsWith('.json')||
  !SHA.test(value.historyEvidence.sha256||'')||!Array.isArray(value.entries)||!value.entries.length||value.entries.length>200||
  new Set(value.entries.map(entry=>entry?.id)).size!==value.entries.length)fail();
 for(const entry of value.entries){
  if(!entry||!identityKeys.every(key=>typeof entry[key]==='string'&&!!entry[key])||!ID.test(entry.id)||!ID.test(entry.projectId)||
   entry.harness!=='codex'||entry.mode!=='tui'||!['worker','orchestrator'].includes(entry.kind)||
   !branch(entry.branch)||!branch(entry.expectedBranch)||entry.branch===entry.expectedBranch||!time(entry.createdAt)||
   Date.parse(entry.createdAt)>Date.parse(value.snapshotAt)||!absolute(entry.workspacePath)||!absolute(entry.workspaceRepoPath)||
   !entry.inflight||Object.keys(entry.inflight).sort().join(',')!=='at,command'||entry.inflight.command!=='restore'||!time(entry.inflight.at)||
   Date.parse(entry.inflight.at)<Date.parse(value.restoreStartedAt)||Date.parse(entry.inflight.at)>Date.parse(value.reviewedAt)||
   !entry.completionAudit||!SHA.test(entry.completionAudit.lineSha256||'')||!time(entry.completionAudit.time)||
   Date.parse(entry.completionAudit.time)<Date.parse(entry.inflight.at)||Date.parse(entry.completionAudit.time)>Date.parse(value.reviewedAt)||
   !runtimeShape(entry.nativeRuntime)||Date.parse(entry.nativeRuntime.hostCreatedAt)<Date.parse(entry.inflight.at)-1000||
   Date.parse(entry.nativeRuntime.providerCreatedAt)>Date.parse(value.reviewedAt)+1000)fail();
 }
 // A normal later capture ends this one-time review's generation. Malformed,
 // future and same-generation byte mismatches never receive this treatment.
 const scope=createRestorationScope(snapshot,targets);
 if(scope&&time(snapshot?.at)&&Date.parse(snapshot.at)>Date.parse(value.snapshotAt)&&Date.parse(snapshot.at)<=now)return null;
 if(!scope||snapshot?.at!==value.snapshotAt||value.entries.length>targets.length||digest(readSmall(path.join(root,'restore-snapshot.json')))!==value.snapshotSha256)fail();
 for(const entry of value.entries){
  const target=targets.find(item=>item.id===entry.id);
  if(!target||!identityKeys.every(key=>entry[key]===target[key])||target.branch!==entry.branch||
   (Object.hasOwn(target,'createdAt')&&target.createdAt!==entry.createdAt))fail();
 }
 return freeze({...value,revision:digest(bytes)});
}
function verifyHistoryEvidence(root,review){
 const bytes=readSmall(path.join(root,review.historyEvidence.file));if(digest(bytes)!==review.historyEvidence.sha256)fail();
 const value=parse(bytes);
 if(value?.schemaVersion!==1||value.kind!=='post-submission-native-history-evidence'||!Array.isArray(value.sessions)||
  new Set(value.sessions.map(session=>session?.id)).size!==value.sessions.length)fail();
 const reviewed={};
 for(const entry of review.entries){
  const proof=value.sessions.find(session=>session?.id===entry.id);
  if(!proof||!identityKeys.every(key=>proof[key]===entry[key])||proof.createdAt!==entry.createdAt||proof.branch!==entry.branch||
   proof.expectedBranch!==entry.expectedBranch||proof.workspacePath!==entry.workspacePath||proof.workspaceRepoPath!==entry.workspaceRepoPath||
   proof.beforeNativeSessionId!==entry.nativeRuntime.nativeSessionId||proof.resumedNativeSessionId!==entry.nativeRuntime.nativeSessionId||
   proof.activeRolloutPresent!==true||proof.registeredWorkspacePresent!==true||!equal(proof.nativeRuntime,entry.nativeRuntime)||
   !time(proof.beforeEvidenceAt)||!time(proof.afterObservedAt)||Date.parse(proof.beforeEvidenceAt)<Date.parse(review.snapshotAt)||
   Date.parse(proof.beforeEvidenceAt)>=Date.parse(entry.inflight.at)||Date.parse(proof.afterObservedAt)<Date.parse(entry.completionAudit.time)||
   Date.parse(proof.afterObservedAt)>Date.parse(review.reviewedAt))fail();
  const provenance=proof.beforeProvenance;
  if(provenance?.timestampSource!=='filesystem-last-write-time'||typeof provenance.file!=='string'||
   !/^(?:updates[\\/])?[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.json$/.test(provenance.file)||!SHA.test(provenance.sha256||'')||
   provenance.lastWriteTimeUtc!==proof.beforeEvidenceAt||typeof provenance.reviewReference!=='string'||!provenance.reviewReference.trim()||provenance.reviewReference.length>1000)fail();
  const realRoot=fs.realpathSync(root),file=path.resolve(root,provenance.file),realFile=fs.realpathSync(file),relative=path.relative(realRoot,realFile);
  if(!relative||relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative))fail();
  let component=root;for(const part of provenance.file.split(/[\\/]/)){component=path.join(component,part);if(fs.lstatSync(component).isSymbolicLink())fail();}
  const originalBytes=readSmall(realFile,32*1024*1024);
  if(
   digest(originalBytes)!==provenance.sha256||Math.abs(fs.statSync(realFile).mtimeMs-Date.parse(provenance.lastWriteTimeUtc))>=1)fail();
  const original=parse(originalBytes),before=Array.isArray(original)?original.filter(row=>row?.id===entry.id):[];
  if(before.length!==1||before[0].agent_session_id!==entry.nativeRuntime.nativeSessionId||before[0].model!==entry.model||
   before[0].branch!==entry.branch||pathKey(before[0].workspace_path)!==pathKey(entry.workspacePath)||
   !instantKey(before[0].created_at)||instantKey(before[0].created_at)!==instantKey(entry.createdAt))fail();
  reviewed[entry.id]={beforeEvidenceAt:proof.beforeEvidenceAt,beforeProvenance:structuredClone(provenance),afterObservedAt:proof.afterObservedAt,
   beforeNativeSessionId:proof.beforeNativeSessionId,resumedNativeSessionId:proof.resumedNativeSessionId};
 }
 return reviewed;
}
function verifyCompletionAudits(root,review){
 const bytes=readSmall(path.join(root,'audit.jsonl'),64*1024*1024),lines=bytes.toString('utf8').split('\n');
 const output={};
 for(const entry of review.entries){
  const matched=[];
  for(const raw of lines){
   const line=raw.endsWith('\r')?raw.slice(0,-1):raw;if(!line||digest(line)!==entry.completionAudit.lineSha256)continue;
   let value;try{value=JSON.parse(line);}catch{fail();}
   if(value.event!=='restore_resume_submitted'||value.id!==entry.id||value.project!==entry.projectId||value.action!=='restore'||value.time!==entry.completionAudit.time)fail();
   matched.push(value);
  }
  if(matched.length!==1)fail();
  // A second successful restore for this identity after the preserved fence
  // makes its generation ambiguous even if the selected line still exists.
  const candidates=lines.flatMap(raw=>{try{return [JSON.parse(raw)];}catch{return [];}}).filter(value=>
   value.event==='restore_resume_submitted'&&value.id===entry.id&&value.project===entry.projectId&&value.action==='restore'&&
   time(value.time)&&Date.parse(value.time)>=Date.parse(entry.inflight.at));
  if(candidates.length!==1)fail();
  output[entry.id]={...matched[0],lineSha256:entry.completionAudit.lineSha256};
 }
 return output;
}
function pendingReviewMatches(review,state,stateBytes){
 return !!review&&state?.active===false&&['paused','partial','failed','cancelled'].includes(state.phase)&&
  review.startupRunId===state.source?.startupRunId&&review.restoreStartedAt===state.startedAt&&review.snapshotAt===state.source?.snapshotAt&&
  review.sourceSha256===digest(state.source)&&review.desiredIdsSha256===digest(state.desiredIds)&&review.restoreStateSha256===digest(stateBytes)&&
  exactIds(review.entries.map(entry=>entry.id),Object.keys(state.inflight||{}))&&review.entries.every(entry=>equal(state.inflight[entry.id],entry.inflight))&&
  Object.entries(state.errors||{}).every(([id,error])=>error?.uncertain!==true||review.entries.some(entry=>entry.id===id))&&
  review.entries.every(entry=>!state.postRestoreReviewReceipts?.[entry.id]&&!state.branchAdoptionReceipts?.[entry.id]);
}
function nativeProofMatches(proof,entry,after,now){
 return proof?.verified===true&&time(proof.observedAt)&&Date.parse(proof.observedAt)>=Date.parse(after)&&Date.parse(proof.observedAt)<=now+1000&&
  now-Date.parse(proof.observedAt)<=30000&&['nativeSessionId','providerPid','hostPid'].every(key=>proof[key]===entry.nativeRuntime[key])&&
  pathKey(proof.providerExecutable)===pathKey(entry.nativeRuntime.providerExecutable)&&
  ['providerCreatedAt','hostCreatedAt'].every(key=>instantKey(proof[key])&&instantKey(proof[key])===instantKey(entry.nativeRuntime[key]));
}
function matchesPostReviewEntry(entry,session){
 return matchesEntry(entry,session,{expectedBranch:true})&&session.isTerminated===false&&
  (session.terminalGeneration??null)===entry.nativeRuntime.terminalGeneration;
}
function makePostReviewReceipt(review,entry,state,audit,workspace,nativeRuntime,history,now){
 if(!workspaceProofMatches(workspace,entry,review.reviewedAt,now)||!nativeProofMatches(nativeRuntime,entry,review.reviewedAt,now))fail();
 return {schemaVersion:1,type:'post-submission-restore-review-receipt',receiptId:randomUUID(),status:'verified',
  reviewTiming:'after-native-command-succeeded',reviewRevision:review.revision,reviewedAt:review.reviewedAt,completedAt:new Date(now).toISOString(),
  startupRunId:review.startupRunId,restoreStartedAt:review.restoreStartedAt,snapshotAt:review.snapshotAt,snapshotSha256:review.snapshotSha256,
  sourceSha256:review.sourceSha256,desiredIdsSha256:review.desiredIdsSha256,consumedStateSha256:review.restoreStateSha256,
  historyEvidence:{...review.historyEvidence},reviewedHistory:structuredClone(history),entry:structuredClone(entry),originalFence:structuredClone(state.inflight[entry.id]),
  resolvedError:structuredClone(state.errors?.[entry.id]||null),completionAudit:structuredClone(audit),workspaceAfter:structuredClone(workspace),nativeAfter:structuredClone(nativeRuntime)};
}
function postReviewReceiptForRuntime(review,state,target,session,now){
 const entry=review?.entries.find(value=>value.id===target.id),receipt=state?.postRestoreReviewReceipts?.[target.id];
 if(!entry||!receipt||receipt.schemaVersion!==1||receipt.type!=='post-submission-restore-review-receipt'||receipt.status!=='verified'||
  receipt.reviewTiming!=='after-native-command-succeeded'||typeof receipt.receiptId!=='string'||!receipt.receiptId||
  receipt.reviewRevision!==review.revision||receipt.reviewedAt!==review.reviewedAt||!time(receipt.completedAt)||
  Date.parse(receipt.completedAt)<Date.parse(review.reviewedAt)||Date.parse(receipt.completedAt)>now+1000||
  receipt.startupRunId!==review.startupRunId||receipt.restoreStartedAt!==review.restoreStartedAt||receipt.snapshotAt!==review.snapshotAt||
  receipt.snapshotSha256!==review.snapshotSha256||receipt.sourceSha256!==review.sourceSha256||receipt.desiredIdsSha256!==review.desiredIdsSha256||
  receipt.consumedStateSha256!==review.restoreStateSha256||!equal(receipt.historyEvidence,review.historyEvidence)||!equal(receipt.entry,entry)||
  receipt.reviewedHistory?.beforeProvenance?.timestampSource!=='filesystem-last-write-time'||
  receipt.reviewedHistory.beforeNativeSessionId!==entry.nativeRuntime.nativeSessionId||receipt.reviewedHistory.resumedNativeSessionId!==entry.nativeRuntime.nativeSessionId||
  !equal(receipt.originalFence,entry.inflight)||receipt.completionAudit?.lineSha256!==entry.completionAudit.lineSha256||
  receipt.completionAudit.time!==entry.completionAudit.time||receipt.completionAudit.event!=='restore_resume_submitted'||
  receipt.completionAudit.id!==entry.id||receipt.completionAudit.project!==entry.projectId||receipt.completionAudit.action!=='restore'||
  !workspaceProofMatches(receipt.workspaceAfter,entry,review.reviewedAt,now)||!nativeProofMatches(receipt.nativeAfter,entry,review.reviewedAt,Date.parse(receipt.completedAt))||
  !identityKeys.every(key=>target[key]===entry[key])||target.branch!==entry.branch||(target.createdAt&&target.createdAt!==entry.createdAt)||
  !matchesPostReviewEntry(entry,session)||Object.hasOwn(state.inflight||{},entry.id)||state.errors?.[entry.id]?.uncertain===true) return null;
 const originalOperation=state.source?.startupRunId===receipt.startupRunId&&state.startedAt===receipt.restoreStartedAt&&
  digest(state.source)===receipt.sourceSha256&&digest(state.desiredIds)===receipt.desiredIdsSha256;
 const carried=state.source?.snapshotAt===receipt.snapshotAt&&state.source?.carriedPostRestoreReviews?.[target.id]===digest(receipt)&&digest(state.desiredIds)===receipt.desiredIdsSha256&&
  time(state.startedAt)&&Date.parse(state.startedAt)>=Date.parse(receipt.completedAt);
 if(!originalOperation&&!carried)return null;
 return {entry,receipt};
}

module.exports={REVIEW_FILE,readPostSubmissionReview,verifyHistoryEvidence,verifyCompletionAudits,pendingReviewMatches,nativeProofMatches,
 matchesPostReviewEntry,makePostReviewReceipt,postReviewReceiptForRuntime,exactIds};
