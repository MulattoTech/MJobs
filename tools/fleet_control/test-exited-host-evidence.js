'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {attachExitedHostEvidence,runtimeExitedHostEvidence}=require('./exited-host-evidence');
const {runtimeAbsentEvidence}=require('./windows-runtime-evidence');
const {RestoreFleet}=require('./restore-fleet');
const now=Date.parse('2026-10-26T22:00:00Z');
const session=()=>({id:'test-primary',projectId:'mjobs',kind:'orchestrator',harness:'codex',mode:'tui',model:'test-model',branch:'ao/test',createdAt:'2026-10-24T00:00:00Z',updatedAt:'2026-10-26T06:00:00Z',isTerminated:false,activity:{state:'exited'},statusReadiness:'ready',provisionState:'ready',terminalGeneration:'11111111-2222-3333-4444-555555555555'});
function fixture(change=()=>{}){
 const current=session(),counts=new Map([[current.id,1]]),options={platform:'win32',now:()=>now,readSessions:async()=>[current],runProbe:async request=>{
  const value={version:1,probeId:request.probeId,complete:true,observedAt:new Date(now).toISOString(),results:[{id:current.id,exited:true,hostPid:123,hostCreatedAt:'2026-10-26T03:00:00Z',launchId:current.terminalGeneration,exitCode:0,proof:{stableInventory:true,stableRegistry:true,stableInstance:true,listenerOwned:true,exactGeneration:true,noProviderChildren:true,nativeExitConfirmed:true}}]};change(value,current,counts);return value;}};
 return {current,counts,options};
}
test('terminal host existence is not confused with provider liveness or host absence',async()=>{
 const f=fixture();await attachExitedHostEvidence(f.counts,[f.current],f.options);
 assert(runtimeExitedHostEvidence(f.current,f.counts,now));assert.equal(f.counts.get(f.current.id),1);assert.equal(runtimeAbsentEvidence(f.current,f.counts,now),null);
});
test('incomplete, stale, changed and still-running evidence fail closed',async t=>{
 const cases={nonce:v=>{v.probeId='0'.repeat(32);},incomplete:v=>{v.complete=false;},old:v=>{v.observedAt=new Date(now-10001).toISOString();},future:v=>{v.observedAt=new Date(now+1).toISOString();},duplicate:v=>v.results.push(v.results[0]),alive:v=>{v.results[0].exited=false;},exitMissing:v=>{delete v.results[0].exitCode;},generation:v=>{v.results[0].launchId='ffffffff-2222-3333-4444-555555555555';},children:v=>{v.results[0].proof.noProviderChildren=false;},listener:v=>{v.results[0].proof.listenerOwned=false;},registry:v=>{v.results[0].proof.stableRegistry=false;},instance:v=>{v.results[0].proof.stableInstance=false;},inventory:v=>{v.results[0].proof.stableInventory=false;},session:(v,s)=>{s.updatedAt='2026-10-26T21:00:00Z';},hostBirth:v=>{v.results[0].hostCreatedAt='2027-01-01T00:00:00Z';}};
 for(const [name,change]of Object.entries(cases))await t.test(name,async()=>{const f=fixture(change);await attachExitedHostEvidence(f.counts,[f.current],f.options);assert.equal(runtimeExitedHostEvidence(f.current,f.counts,now),null);assert.equal(f.counts.get(f.current.id),1);});
});
test('archived, chat, blocked, active, unknown-count and multi-process targets are never probed',async t=>{
 for(const kind of ['archive','chat','blocked','active','unknown','multiple'])await t.test(kind,async()=>{
  const f=fixture();let calls=0;f.options.runProbe=async()=>{calls++;throw Error('not allowed');};
  if(kind==='archive')f.current.isTerminated=true;if(kind==='chat')f.current.mode='chat';if(['blocked','active'].includes(kind))f.current.activity.state=kind;
  if(kind==='unknown')f.counts.set(f.current.id,null);if(kind==='multiple')f.counts.set(f.current.id,2);
  await attachExitedHostEvidence(f.counts,[f.current],f.options);assert.equal(calls,0);assert.equal(f.counts.exitedHostEvidence.size,0);
 });
});
test('proof expires and changing native generation cannot reuse it',async()=>{
 const f=fixture();await attachExitedHostEvidence(f.counts,[f.current],f.options);assert.equal(runtimeExitedHostEvidence(f.current,f.counts,now+10001),null);
 f.current.terminalGeneration='ffffffff-2222-3333-4444-555555555555';assert.equal(runtimeExitedHostEvidence(f.current,f.counts,now),null);
});
test('real mutation boundary permits exact stopped-child proof and rejects its removal',async t=>{
 const f=fixture();await attachExitedHostEvidence(f.counts,[f.current],f.options);
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-exited-host-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const restore=new RestoreFleet({root,autoSchedule:false,now:()=>now,runtimeInspector:async()=>f.counts,getGlobalSessions:async()=>[f.current],runtimeHealth:()=>false,audit:()=>{},command:async()=>{throw Error('No native command expected');}});
 const result=await restore.inspectMutationTarget(f.current);assert.equal(result.needsExit,false);
 f.counts.exitedHostEvidence.clear();await assert.rejects(restore.inspectMutationTarget(f.current),/unverified/);
});
test('cancelled and throwing probes cannot produce usable proof',async()=>{
 const f=fixture();f.options.runProbe=async()=>{throw Error('private failure');};await attachExitedHostEvidence(f.counts,[f.current],f.options);assert.equal(f.counts.exitedHostEvidence.size,0);
 const g=fixture(),abort=new AbortController();abort.abort();g.options.signal=abort.signal;await attachExitedHostEvidence(g.counts,[g.current],g.options);assert.equal(g.counts.exitedHostEvidence.size,0);
});
