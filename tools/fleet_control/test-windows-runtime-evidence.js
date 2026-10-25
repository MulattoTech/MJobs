'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {execFileSync}=require('node:child_process');
const {attachWindowsStoppedEvidence,runtimeAbsentEvidence,nativeRuntimePaths,nativeSessionIdentity,MAX_AGE_MS}=require('./windows-runtime-evidence');
const NOW=Date.parse('2026-10-25T20:00:00.000Z');
const clone=value=>JSON.parse(JSON.stringify(value));
function session(patch={}){
 return {id:'mjobs-primary',projectId:'mjobs',mode:'chat',harness:'codex',
  createdAt:'2026-10-22T10:00:00Z',updatedAt:'2026-10-25T19:00:00Z',
  terminalGeneration:'generation-one',activity:{state:'exited'},isTerminated:false,
  statusReadiness:'ready',controllerVerified:true,controllerState:'stopped',...patch};
}
function proof(patch={}){
 return {completeInventory:true,stableInventory:true,stableRegistry:true,noMatchingHost:true,
  ownershipClear:true,noSessionOwnedProvider:true,registryAbsent:true,chatOwnershipClear:true,
  instanceVerified:true,stableInstance:true,...patch};
}
function response(request,patch={}){
 return {version:1,probeId:request.probeId,observedAt:new Date(NOW).toISOString(),complete:true,
  results:request.targets.map(target=>({id:target.id,absent:true,reason:null,proof:proof()})),...patch};
}
function fixture(patch={}){
 const current=session(patch),counts=new Map([[current.id,null],['unrelated',7]]);
 const state={now:NOW,requests:[],reads:0,after:clone(current)};
 const options={platform:'win32',now:()=>state.now,readSessions:async()=>{state.reads++;return [clone(state.after)]},
  runProbe:async request=>{state.requests.push(request);return response(request)}};
 return {current,counts,state,options,run:()=>attachWindowsStoppedEvidence(counts,[current],options)};
}
test('verified chat absence is a typed sidecar, never a fabricated zero or global provider-death claim',async()=>{
 const f=fixture();const original=f.counts;await f.run();
 assert.equal(f.counts,original);assert.equal(f.counts.get(f.current.id),null);assert.equal(f.counts.get('unrelated'),7);
 const evidence=runtimeAbsentEvidence(f.current,f.counts,NOW);
 assert.equal(evidence.type,'native-runtime-absent');assert.equal(evidence.identity.mode,'chat');
 assert.equal(f.state.reads,1);assert.equal(f.state.requests.length,1);
 assert.equal(Object.hasOwn(evidence,'allProviderProcessesDead'),false);
});
test('default Windows native layout keeps registry root separate from chat data root',()=>{
 assert.deepEqual(nativeRuntimePaths({homeDirectory:'C:\\Users\\Dylan'}),{
  aoDataDir:'C:\\Users\\Dylan\\.ao\\data',runFilePath:'C:\\Users\\Dylan\\.ao\\running.json',
  registryPath:'C:\\Users\\Dylan\\.ao\\windows-pty-hosts.json'});
});
test('an explicit data directory does not relocate the independent run file or registry',()=>{
 const result=nativeRuntimePaths({homeDirectory:'C:\\Users\\Dylan',aoDataDir:'D:\\AO-Data'});
 assert.equal(result.aoDataDir,'D:\\AO-Data');assert.equal(result.runFilePath,'C:\\Users\\Dylan\\.ao\\running.json');
 assert.equal(result.registryPath,'C:\\Users\\Dylan\\.ao\\windows-pty-hosts.json');
});
test('custom run-file directory moves only the registry, not default chat ownership',()=>{
 const result=nativeRuntimePaths({homeDirectory:'C:\\Users\\Dylan',runFilePath:'D:\\AO-Instance\\running.json'});
 assert.equal(result.aoDataDir,'C:\\Users\\Dylan\\.ao\\data');
 assert.equal(result.registryPath,'D:\\AO-Instance\\windows-pty-hosts.json');
});
test('explicit isolated native roots remain independent in the actual probe request',async()=>{
 const f=fixture();Object.assign(f.options,{aoDataDir:'D:\\Durable AO',runFilePath:'E:\\Run AO\\running.json'});
 await f.run();assert.equal(f.state.requests[0].aoDataDir,'D:\\Durable AO');
 assert.equal(f.state.requests[0].registryPath,'E:\\Run AO\\windows-pty-hosts.json');
 assert.equal(f.state.requests[0].runFilePath,'E:\\Run AO\\running.json');
});
test('missing native count remains missing while independent evidence can qualify',async()=>{
 const f=fixture();f.counts.delete(f.current.id);await f.run();
 assert.equal(f.counts.has(f.current.id),false);assert.ok(runtimeAbsentEvidence(f.current,f.counts,NOW));
});
test('an actual measured zero is preserved',async()=>{
 const f=fixture();f.counts.set(f.current.id,0);await f.run();
 assert.equal(f.counts.get(f.current.id),0);assert.ok(runtimeAbsentEvidence(f.current,f.counts,NOW));
});
test('TUI requires complete exact registry absence and ignores unrelated chat-controller annotations',async()=>{
 const f=fixture({mode:'tui',controllerVerified:false,controllerState:'unknown'});await f.run();
 assert.ok(runtimeAbsentEvidence(f.current,f.counts,NOW));
 const g=fixture({mode:'tui'});g.options.runProbe=async request=>response(request,{results:[{id:g.current.id,absent:true,proof:proof({registryAbsent:false})}]});
 await g.run();assert.equal(g.counts.stoppedEvidence.size,0);
});
test('non-Windows is explicit unavailable and does not invoke any probe',async()=>{
 const f=fixture();f.options.platform='linux';await f.run();
 assert.equal(f.state.requests.length,0);assert.equal(f.state.reads,0);
 assert.equal(f.counts.evidenceDiagnostics[0].reason,'WINDOWS_PROBE_UNAVAILABLE');
});
test('a fresh native post-probe reader is mandatory',async()=>{
 const f=fixture();delete f.options.readSessions;await f.run();
 assert.equal(f.state.requests.length,0);assert.equal(f.counts.stoppedEvidence.size,0);
});
test('native count contradictions cannot be overwritten by absence evidence',async t=>{
 for(const value of [1,2,-1,0.5,NaN,Infinity,'0',false])await t.test(String(value),async()=>{
  const f=fixture();f.counts.set(f.current.id,value);await f.run();
  assert.equal(f.state.requests.length,0);assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('chat must be natively stopped, not merely idle or a stale wrapper',async t=>{
 for(const patch of [{controllerVerified:false},{controllerState:'ready'},{controllerState:'busy'},
  {controllerState:'recovering'},{controllerState:'hibernated'},{statusReadiness:'checking'},
  {statusReadiness:'unavailable'},{activity:{state:'idle'}},{isTerminated:true},
  {provisionState:'provisioning'},{hibernatedAt:'2026-10-25T19:30:00Z'}])await t.test(JSON.stringify(patch),async()=>{
  const f=fixture(patch);await f.run();assert.equal(f.state.requests.length,0);assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('incomplete native session identity fails closed',async t=>{
 for(const key of ['id','projectId','mode','harness','createdAt','updatedAt','isTerminated'])await t.test(key,async()=>{
  const f=fixture();delete f.current[key];await f.run();assert.equal(f.state.requests.length,0);assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('unsafe session identifiers are not passed to ownership path readers',async()=>{
 const f=fixture({id:'../host'});await f.run();assert.equal(f.state.requests.length,0);
});
test('duplicate native session IDs reject the entire preflight',async()=>{
 const f=fixture();await attachWindowsStoppedEvidence(f.counts,[f.current,clone(f.current)],f.options);
 assert.equal(f.state.requests.length,0);assert.equal(f.counts.stoppedEvidence.size,0);
});
test('native helper errors and unavailable inspection expose no raw error text',async()=>{
 const f=fixture();f.options.runProbe=async()=>{throw Error('SECRET_RAW_COMMANDLINE')};await f.run();
 assert.equal(f.counts.stoppedEvidence.size,0);assert.doesNotMatch(JSON.stringify(f.counts.evidenceDiagnostics),/SECRET/);
 const g=fixture();g.options.runProbe=async request=>response(request,{complete:false,reason:'REGISTRY_PID_IDENTITY_MISMATCH'});await g.run();
 assert.equal(g.counts.evidenceDiagnostics[0].reason,'REGISTRY_PID_IDENTITY_MISMATCH');
});
test('unknown, stale, reused-PID and changed-registry outcomes cannot certify absence',async t=>{
 for(const reason of ['REGISTRY_PID_IDENTITY_MISMATCH','REGISTRY_UNRESOLVED_OWNERSHIP',
  'REGISTRY_CREATION_IDENTITY_UNVERIFIED','PROCESS_IDENTITY_UNREADABLE','REGISTRY_CHANGED',
  'MATCHING_NATIVE_HOST_PRESENT','CHAT_LOCK_OWNER_LIVE_OR_REUSED','SESSION_OWNED_PROVIDER_PRESENT'])await t.test(reason,async()=>{
  const f=fixture();f.options.runProbe=async request=>response(request,{results:[{id:f.current.id,absent:false,reason,proof:proof()}]});await f.run();
  assert.equal(f.counts.stoppedEvidence.size,0);assert.equal(f.counts.evidenceDiagnostics[0].reason,reason);
 });
});
test('every mandatory proof field must be a literal true',async t=>{
 for(const field of ['completeInventory','stableInventory','stableRegistry','noMatchingHost','ownershipClear','noSessionOwnedProvider','chatOwnershipClear','instanceVerified','stableInstance'])await t.test(field,async()=>{
  const f=fixture();f.options.runProbe=async request=>response(request,{results:[{id:f.current.id,absent:true,proof:proof({[field]:'true'})}]});await f.run();
  assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('wrong probe nonce, malformed response, absent result and duplicate result fail closed',async t=>{
 for(const kind of ['nonce','version','no-results','missing','duplicate'])await t.test(kind,async()=>{
  const f=fixture();f.options.runProbe=async request=>{
   const value=response(request);
   if(kind==='nonce')value.probeId='0'.repeat(32);
   if(kind==='version')value.version=2;
   if(kind==='no-results')delete value.results;
   if(kind==='missing')value.results=[];
   if(kind==='duplicate')value.results.push(clone(value.results[0]));
   return value;
  };await f.run();assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('stale, future and malformed native timestamps fail closed',async t=>{
 for(const timestamp of [new Date(NOW-MAX_AGE_MS-1).toISOString(),new Date(NOW+1).toISOString(),'not-a-date'])await t.test(timestamp,async()=>{
  const f=fixture();f.options.runProbe=async request=>response(request,{observedAt:timestamp});await f.run();
  assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('identity changes during inspection invalidate the result',async t=>{
 for(const patch of [{id:'replacement'},{projectId:'different'},{mode:'tui'},{harness:'agy'},
  {terminalGeneration:'new-generation'},{updatedAt:'2026-10-25T19:59:59Z'},
  {createdAt:'2026-10-25T19:59:58Z'},{activity:{state:'active'}},{controllerState:'ready'},
  {isTerminated:true},{statusReadiness:'checking'}])await t.test(JSON.stringify(patch),async()=>{
  const f=fixture();Object.assign(f.state.after,patch);await f.run();assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('post-probe session lookup failure or duplicate rows leaves native state unknown',async t=>{
 for(const mode of ['throw','malformed','duplicate'])await t.test(mode,async()=>{
  const f=fixture();f.options.readSessions=async()=>{
   if(mode==='throw')throw Error('unavailable');
   if(mode==='malformed')return {};
   return [f.current,clone(f.current)];
  };await f.run();assert.equal(f.counts.stoppedEvidence.size,0);
 });
});
test('evidence is revalidated at point of use, including time, identity and new numeric counts',async()=>{
 const f=fixture();await f.run();assert.ok(runtimeAbsentEvidence(f.current,f.counts,NOW));
 assert.equal(runtimeAbsentEvidence(f.current,f.counts,NOW+MAX_AGE_MS+1),null);
 assert.equal(runtimeAbsentEvidence(f.current,f.counts,NOW-1),null);
 assert.equal(runtimeAbsentEvidence({...f.current,terminalGeneration:'new'},f.counts,NOW),null);
 f.counts.set(f.current.id,2);assert.equal(runtimeAbsentEvidence(f.current,f.counts,NOW),null);
});
test('a changing numeric count during the probe cannot acquire stopped evidence',async()=>{
 const f=fixture();f.options.runProbe=async request=>{f.counts.set(f.current.id,3);return response(request)};
 await f.run();assert.equal(f.counts.stoppedEvidence.size,0);assert.equal(f.counts.get(f.current.id),3);
});
test('failed refresh clears previous sidecar rather than serving stale permission',async()=>{
 const f=fixture();await f.run();assert.equal(f.counts.stoppedEvidence.size,1);
 f.options.runProbe=async()=>{throw Error('offline')};await f.run();assert.equal(f.counts.stoppedEvidence.size,0);
});
test('cancellation before probe and during native recheck cannot authorize resume',async()=>{
 const f=fixture(),before=new AbortController();before.abort();f.options.signal=before.signal;await f.run();assert.equal(f.state.requests.length,0);
 const g=fixture(),during=new AbortController();g.options.signal=during.signal;
 g.options.readSessions=async()=>{during.abort();return [g.current]};await g.run();assert.equal(g.counts.stoppedEvidence.size,0);
});
test('a slow recheck cannot extend a native absence observation indefinitely',async()=>{
 const f=fixture();f.options.readSessions=async()=>{f.state.now=NOW+MAX_AGE_MS+1;return [f.current]};await f.run();
 assert.equal(f.counts.stoppedEvidence.size,0);
});
test('only whitelisted native metadata and boolean proof fields are retained',async()=>{
 const f=fixture();f.current.messages=['PRIVATE_TRANSCRIPT'];f.current.account='PRIVATE_ACCOUNT';
 f.options.runProbe=async request=>{
  assert.doesNotMatch(JSON.stringify(request),/PRIVATE_TRANSCRIPT|PRIVATE_ACCOUNT/);
  return response(request,{results:[{id:f.current.id,absent:true,proof:proof({rawCommandLine:'PRIVATE_ARGV',token:'PRIVATE_TOKEN'})}]});
 };await f.run();const evidence=runtimeAbsentEvidence(f.current,f.counts,NOW);
 assert.ok(evidence);assert.doesNotMatch(JSON.stringify(evidence),/PRIVATE/);
 assert.deepEqual(evidence.identity,nativeSessionIdentity(f.current));
});
test('Windows native argument parser and native policy fixture', {skip:process.platform!=='win32'?'Windows native fixture requires PowerShell and Win32 APIs; not claimed tested on Linux':false},()=>{
 const text=execFileSync('C:\\Program Files\\PowerShell\\7\\pwsh.exe',[
  '-NoLogo','-NoProfile','-NonInteractive','-File',path.join(__dirname,'Get-AoRuntimeEvidence.ps1'),'-SelfTest'
 ],{encoding:'utf8',windowsHide:true,timeout:12000,maxBuffer:1024*1024});
 const result=JSON.parse(text.replace(/^\uFEFF/,''));
 assert.equal(result.ok,true,JSON.stringify(result));assert.ok(result.checks>=22);
 assert.equal(result.processMutations,0);assert.equal(result.ownershipWrites,0);
});
