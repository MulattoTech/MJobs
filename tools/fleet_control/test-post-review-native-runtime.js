'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const path=require('node:path');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const {verifyReviewedNativeRuntime}=require('./post-review-native-runtime');
const stamp=Date.parse('2026-10-26T02:00:00.000Z');
const native={nativeSessionId:'12345678-abcd-4abc-8abc-123456789abc',providerPid:303,providerCreatedAt:'2026-10-26T01:59:00.000Z',
 providerExecutable:'C:\\fixture\\codex.exe',hostPid:101,hostCreatedAt:'2026-10-26T01:58:59.000Z',terminalGeneration:null};
const fixture=()=>({id:'mjobs-review-10',nativeRuntime:{...native}});
function options(change=()=>{}){
 return {platform:'win32',aoExecutable:'C:\\fixture\\ao.exe',codexExecutable:'C:\\fixture\\codex.exe',now:()=>stamp,
  execFile:async(executable,args,settings)=>{
   assert.equal(executable,'C:\\Program Files\\PowerShell\\7\\pwsh.exe');
   assert.equal(args[0],'-NoLogo');assert.equal(args[4],path.join(__dirname,'Get-ReviewedNativeRuntime.ps1'));
   assert.equal(args[5],'-RequestBase64');assert.equal(settings.shell,false);assert.equal(settings.windowsHide,true);
   const request=JSON.parse(Buffer.from(args[6],'base64').toString('utf8'));
   const result={version:1,type:'reviewed-native-runtime',probeId:request.probeId,id:request.id,verified:true,
    observedAt:new Date(stamp).toISOString(),...request.nativeRuntime,providerExecutable:'c:\\fixture\\codex.exe',
    proof:{sameProvider:true,sameHost:true,exactResume:true,ancestryVerified:true,stableObservations:true}};
   change(result,request,settings);return {stdout:JSON.stringify(result)};
  }};
}
test('reader verifies the exact reviewed identity and returns only the safe projection',async()=>{
 const result=await verifyReviewedNativeRuntime(fixture(),options(r=>{r.privateArgv='do not return';}));
 assert.equal(result.verified,true);assert.equal(result.nativeSessionId,native.nativeSessionId);
 assert.equal(result.providerExecutable,'c:\\fixture\\codex.exe');assert.equal(Object.hasOwn(result,'privateArgv'),false);
 assert.equal(Object.hasOwn(result,'terminalGeneration'),false,'DTO generation is checked separately by the caller');
});
test('malformed or redirected input fails before spawning the read-only helper',async t=>{
 const cases={
  'invalid session ID':e=>{e.id='bad/id';},'invalid conversation':e=>{e.nativeRuntime.nativeSessionId='--last';},
  'zero conversation':e=>{e.nativeRuntime.nativeSessionId='00000000-0000-0000-0000-000000000000';},
  'PID is string':e=>{e.nativeRuntime.providerPid='303';},'PID reused as host':e=>{e.nativeRuntime.hostPid=303;},
  'future birth':e=>{e.nativeRuntime.providerCreatedAt='2030-01-01T00:00:00Z';},
  'parent born after provider':e=>{e.nativeRuntime.hostCreatedAt='2026-10-26T01:59:01Z';},
  'different executable':e=>{e.nativeRuntime.providerExecutable='C:\\other\\codex.exe';},
  'relative executable':e=>{e.nativeRuntime.providerExecutable='codex.exe';},
  'invalid generation':e=>{e.nativeRuntime.terminalGeneration={};}
 };
 for(const [name,change]of Object.entries(cases))await t.test(name,async()=>{
  const entry=fixture();change(entry);let calls=0;const opt=options();opt.execFile=async()=>{calls++;throw Error('must not run');};
  await assert.rejects(verifyReviewedNativeRuntime(entry,opt),/REVIEWED_NATIVE_/);assert.equal(calls,0);
 });
});
test('wrong or incomplete helper proof never validates the native submission',async t=>{
 const cases={
  'nonce':r=>{r.probeId='0'.repeat(32);},'target':r=>{r.id='mjobs-other';},'provider PID':r=>{r.providerPid++;},
  'provider birth':r=>{r.providerCreatedAt='2026-10-26T01:59:01Z';},'host PID':r=>{r.hostPid++;},
  'host birth':r=>{r.hostCreatedAt='2026-10-26T01:58:58Z';},'native conversation':r=>{r.nativeSessionId='ffffffff-abcd-4abc-8abc-123456789abc';},
  'image':r=>{r.providerExecutable='C:\\other\\codex.exe';},'old observation':r=>{r.observedAt='2026-10-26T01:59:00Z';},
  'future observation':r=>{r.observedAt='2026-10-26T02:00:10Z';},'ancestry':r=>{r.proof.ancestryVerified=false;},
  'unstable observations':r=>{r.proof.stableObservations=false;},'missing proof':r=>{delete r.proof;}
 };
 for(const [name,change]of Object.entries(cases))await t.test(name,async()=>{
  await assert.rejects(verifyReviewedNativeRuntime(fixture(),options(change)),/REVIEWED_NATIVE_PROCESS_CHANGED/);
 });
});
test('entry is snapshotted before asynchronous work',async()=>{
 const entry=fixture(),opt=options();const reader=opt.execFile;
 opt.execFile=async(...args)=>{entry.id='mjobs-other';entry.nativeRuntime.providerPid=999;return reader(...args);};
 const result=await verifyReviewedNativeRuntime(entry,opt);assert.equal(result.providerPid,303);
});
test('native failure and thrown command output are sanitized',async()=>{
 await assert.rejects(verifyReviewedNativeRuntime(fixture(),options(r=>{r.verified=false;r.reason='REVIEWED_NATIVE_ANCESTRY_UNVERIFIED';})),/^Error: REVIEWED_NATIVE_ANCESTRY_UNVERIFIED$/);
 const opt=options();opt.execFile=async()=>{throw Error('private prompt C:\\secret token');};
 await assert.rejects(verifyReviewedNativeRuntime(fixture(),opt),/^Error: REVIEWED_NATIVE_INSPECTION_UNAVAILABLE$/);
});
test('outer deadline bounds a reader that ignores cancellation',async()=>{
 const opt=options();opt.timeoutMs=10;opt.execFile=async()=>new Promise(()=>{});
 await assert.rejects(verifyReviewedNativeRuntime(fixture(),opt),/^Error: REVIEWED_NATIVE_INSPECTION_TIMEOUT$/);
});
test('external cancellation is propagated and cannot return success',async()=>{
 const controller=new AbortController(),opt=options();opt.signal=controller.signal;
 opt.execFile=async()=>{controller.abort();return new Promise(()=>{});};
 await assert.rejects(verifyReviewedNativeRuntime(fixture(),opt),/^Error: REVIEWED_NATIVE_INSPECTION_CANCELLED$/);
});
test('Windows executes the real PowerShell parser and ancestry fixtures without native mutations',{skip:process.platform!=='win32'},async()=>{
 const result=await promisify(execFile)('C:\\Program Files\\PowerShell\\7\\pwsh.exe',
  ['-NoLogo','-NoProfile','-NonInteractive','-File',path.join(__dirname,'Get-ReviewedNativeRuntime.ps1'),'-SelfTest'],
  {windowsHide:true,shell:false,timeout:12000,maxBuffer:65536});
 const value=JSON.parse(result.stdout.trim());assert.equal(value.ok,true);assert.equal(value.checks,30);
 assert.equal(value.processMutations,0);assert.equal(value.ownershipWrites,0);assert.equal(value.socketConnections,0);
});
