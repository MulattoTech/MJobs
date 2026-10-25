'use strict';

// AO v0.14.6 deliberately reports unknown process counts as null. This reader
// adds separate, short-lived native-runtime-absence preflight evidence; it never
// manufactures a zero, proves all provider processes dead, or releases a native
// submission ledger. AO's own operation/host locks remain the final authority.
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const crypto=require('node:crypto');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execAsync=promisify(execFile);
const MAX_AGE_MS=10000;
const PROBE_TIMEOUT_MS=7500;
const ID_RE=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const PROOF_KEYS=['completeInventory','stableInventory','stableRegistry','noMatchingHost',
 'ownershipClear','noSessionOwnedProvider','registryAbsent','chatOwnershipClear','instanceVerified','stableInstance'];

function nativeRuntimePaths(options={}){
 // Official config.go has independent defaults: ~/.ao/running.json versus
 // ~/.ao/data. PTY registry follows the run-file directory, NOT DataDir.
 const stateDir=options.aoStateDir||path.win32.join(options.homeDirectory||os.homedir(),'.ao');
 const runFilePath=options.runFilePath||path.win32.join(stateDir,'running.json');
 return {aoDataDir:options.aoDataDir||path.win32.join(stateDir,'data'),runFilePath,
  registryPath:options.registryPath||path.win32.join(path.win32.dirname(runFilePath),'windows-pty-hosts.json')};
}

function nativeSessionIdentity(s){
 if(!s||!ID_RE.test(s.id||'')||typeof s.projectId!=='string'||!s.projectId||
    !['chat','tui'].includes(s.mode)||!['codex','agy','gemini'].includes(s.harness)||
    typeof s.createdAt!=='string'||!Number.isFinite(Date.parse(s.createdAt))||
    typeof s.updatedAt!=='string'||!Number.isFinite(Date.parse(s.updatedAt))||
    typeof s.isTerminated!=='boolean')return null;
 const activity=s.activity?.state||s.status;
 if(typeof activity!=='string')return null;
 return {id:s.id,projectId:s.projectId,mode:s.mode,harness:s.harness,
  createdAt:s.createdAt,updatedAt:s.updatedAt,
  terminalGeneration:typeof s.terminalGeneration==='string'?s.terminalGeneration:'',
  activity,isTerminated:s.isTerminated,statusReadiness:s.statusReadiness||'unknown',
  provisionState:s.provisionState||'ready',hibernatedAt:s.hibernatedAt||null,
  controllerState:s.mode==='chat'?(s.controllerState||'unknown'):'not-applicable',
  controllerVerified:s.mode==='chat'?s.controllerVerified===true:false};
}
function sameIdentity(a,b){return !!a&&!!b&&JSON.stringify(a)===JSON.stringify(b)}
function eligibleIdentity(identity){
 return !!identity&&!identity.isTerminated&&!identity.hibernatedAt&&identity.provisionState==='ready'&&identity.activity==='exited'&&identity.statusReadiness==='ready'&&
  (identity.mode==='tui'||(identity.controllerVerified&&identity.controllerState==='stopped'));
}
function countAllowsAbsence(counts,id){
 const value=counts.get(id);
 return value===undefined||value===null||value===0;
}
function completeProof(proof,mode){
 if(!proof||!['completeInventory','stableInventory','stableRegistry','noMatchingHost',
    'ownershipClear','noSessionOwnedProvider','instanceVerified','stableInstance'].every(k=>proof[k]===true))return false;
 return mode==='tui'?proof.registryAbsent===true:proof.chatOwnershipClear===true;
}
function runtimeAbsentEvidence(session,counts,now=Date.now()){
 if(!(counts instanceof Map)||!(counts.stoppedEvidence instanceof Map))return null;
 const identity=nativeSessionIdentity(session),evidence=counts.stoppedEvidence.get(session?.id);
 if(!eligibleIdentity(identity)||!countAllowsAbsence(counts,session.id)||!evidence||
    evidence.version!==1||evidence.type!=='native-runtime-absent'||
    !/^[a-f0-9]{32}$/.test(evidence.probeId||'')||!sameIdentity(identity,evidence.identity)||
    !completeProof(evidence.proof,identity.mode))return null;
 const observed=Date.parse(evidence.observedAt||'');
 if(!Number.isFinite(now)||!Number.isFinite(observed)||now<observed||now-observed>MAX_AGE_MS)return null;
 return evidence;
}
function diagnostic(id,reason){return {id:id||null,reason}}
function bounded(promise,ms,signal){
 return new Promise((resolve,reject)=>{
  let done=false;
  const finish=(fn,value)=>{if(done)return;done=true;clearTimeout(timer);signal?.removeEventListener('abort',abort);fn(value)};
  const abort=()=>finish(reject,Error('PROBE_CANCELLED'));
  const timer=setTimeout(()=>finish(reject,Error('NATIVE_SESSION_RECHECK_TIMEOUT')),ms);
  if(signal?.aborted){abort();return}
  signal?.addEventListener('abort',abort,{once:true});
  Promise.resolve(promise).then(v=>finish(resolve,v),e=>finish(reject,e));
 });
}
async function runNativeProbe(request,options){
 const script=path.join(options.root||__dirname,'Get-AoRuntimeEvidence.ps1');
 if(!fs.existsSync(script))throw Error('RUNTIME_EVIDENCE_HELPER_MISSING');
 const powershell=options.powershell||'C:\\Program Files\\PowerShell\\7\\pwsh.exe';
 const result=await execAsync(powershell,['-NoLogo','-NoProfile','-NonInteractive','-File',script,
  '-RequestBase64',Buffer.from(JSON.stringify(request),'utf8').toString('base64')],{
   windowsHide:true,timeout:PROBE_TIMEOUT_MS,maxBuffer:1024*1024,signal:options.signal
  });
 return JSON.parse(String(result.stdout||'').replace(/^\uFEFF/,'').trim());
}
async function attachWindowsStoppedEvidence(counts,sessions,options={}){
 if(!(counts instanceof Map))throw TypeError('Native process counts must be a Map');
 counts.stoppedEvidence=new Map();counts.evidenceDiagnostics=[];
 const report=(id,reason)=>counts.evidenceDiagnostics.push(diagnostic(id,reason));
 const now=typeof options.now==='function'?options.now:()=>Date.now();
 if((options.platform||process.platform)!=='win32'){report(null,'WINDOWS_PROBE_UNAVAILABLE');return counts}
 if(!Array.isArray(sessions)||typeof options.readSessions!=='function'){
  report(null,'FRESH_NATIVE_SESSION_RECHECK_REQUIRED');return counts;
 }
 const seen=new Set(),identities=[];
 for(const session of sessions){
  if(seen.has(session?.id)){report(null,'DUPLICATE_NATIVE_SESSION_ID');return counts}
  seen.add(session?.id);
  const identity=nativeSessionIdentity(session);
  if(!identity){report(session?.id,'INCOMPLETE_NATIVE_SESSION_IDENTITY');continue}
  if(!eligibleIdentity(identity)){report(identity.id,'NATIVE_SESSION_NOT_VERIFIED_STOPPED');continue}
  if(!countAllowsAbsence(counts,identity.id)){report(identity.id,'NATIVE_COUNT_CONTRADICTS_ABSENCE');continue}
  identities.push(identity);
 }
 if(!identities.length)return counts;
 if(identities.length>200){report(null,'TOO_MANY_RUNTIME_TARGETS');return counts}
 if(options.signal?.aborted){report(null,'PROBE_CANCELLED');return counts}
 const request={version:1,probeId:crypto.randomBytes(16).toString('hex'),
  aoExecutable:options.aoExecutable||path.win32.join(os.homedir(),'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe'),
  ...nativeRuntimePaths(options),expectedPort:options.expectedPort||3001,
  targets:identities};
 let value;
 try{value=await bounded(Promise.resolve().then(()=>options.runProbe?options.runProbe(request):runNativeProbe(request,options)),PROBE_TIMEOUT_MS+200,options.signal)}
 catch{report(null,options.signal?.aborted?'PROBE_CANCELLED':'WINDOWS_RUNTIME_PROBE_FAILED');return counts}
 if(!value||value.version!==1||value.probeId!==request.probeId||!Array.isArray(value.results)||
    value.complete!==true){report(null,typeof value?.reason==='string'&&/^[A-Z0-9_]{1,80}$/.test(value.reason)?value.reason:'WINDOWS_RUNTIME_PROBE_INCOMPLETE');return counts}
 const observed=Date.parse(value.observedAt||'');
 if(!Number.isFinite(observed)||now()<observed||now()-observed>MAX_AGE_MS){report(null,'WINDOWS_RUNTIME_PROBE_STALE');return counts}
 let after;
 try{after=await bounded(Promise.resolve().then(()=>options.readSessions()),3000,options.signal)}
 catch{report(null,'NATIVE_SESSION_RECHECK_FAILED');return counts}
 if(!Array.isArray(after)||new Set(after.map(s=>s?.id)).size!==after.length){report(null,'NATIVE_SESSION_RECHECK_MALFORMED');return counts}
 const finalSessions=new Map(after.map(s=>[s.id,s]));
 const resultIds=new Set();
 for(const row of value.results){
  if(!row||typeof row.id!=='string'||resultIds.has(row.id)){report(null,'DUPLICATE_OR_MALFORMED_PROBE_RESULT');counts.stoppedEvidence.clear();return counts}
  resultIds.add(row.id);
 }
 for(const identity of identities){
  const row=value.results.find(r=>r.id===identity.id);
  if(!row||row.absent!==true||!completeProof(row.proof,identity.mode)){
   report(identity.id,typeof row?.reason==='string'&&/^[A-Z0-9_]{1,80}$/.test(row.reason)?row.reason:'NATIVE_RUNTIME_ABSENCE_UNPROVEN');continue;
  }
  const current=finalSessions.get(identity.id);
  if(!sameIdentity(identity,nativeSessionIdentity(current))){report(identity.id,'NATIVE_SESSION_CHANGED_DURING_PROBE');continue}
  const evidence={version:1,type:'native-runtime-absent',probeId:request.probeId,
   observedAt:value.observedAt,identity,proof:Object.fromEntries(PROOF_KEYS.map(k=>[k,row.proof[k]===true]))};
  counts.stoppedEvidence.set(identity.id,evidence);
  if(options.signal?.aborted||!runtimeAbsentEvidence(current,counts,now())){
   counts.stoppedEvidence.delete(identity.id);report(identity.id,'RUNTIME_EVIDENCE_EXPIRED_OR_CANCELLED');
  }
 }
 return counts;
}
module.exports={MAX_AGE_MS,PROBE_TIMEOUT_MS,nativeRuntimePaths,nativeSessionIdentity,runtimeAbsentEvidence,attachWindowsStoppedEvidence};
