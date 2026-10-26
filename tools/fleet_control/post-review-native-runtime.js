'use strict';

// Read-only proof for a reviewed, already submitted Codex restore. This reader
// neither resumes a conversation nor clears a native submission fence.
const path=require('node:path');
const os=require('node:os');
const {randomBytes}=require('node:crypto');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execAsync=promisify(execFile);
const ID_RE=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const UUID_RE=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
const PROOF_KEYS=['sameProvider','sameHost','exactResume','ancestryVerified','stableObservations'];
const SAFE_CODES=new Set(['REVIEWED_NATIVE_IDENTITY_INVALID','REVIEWED_NATIVE_PLATFORM_UNAVAILABLE',
 'REVIEWED_NATIVE_PROCESS_UNAVAILABLE','REVIEWED_NATIVE_PROCESS_CHANGED',
 'REVIEWED_NATIVE_EXECUTABLE_MISMATCH','REVIEWED_NATIVE_RESUME_UNVERIFIED',
 'REVIEWED_NATIVE_ANCESTRY_UNVERIFIED','REVIEWED_NATIVE_HOST_UNVERIFIED',
 'REVIEWED_NATIVE_INSPECTION_CANCELLED','REVIEWED_NATIVE_INSPECTION_TIMEOUT',
 'REVIEWED_NATIVE_INSPECTION_UNAVAILABLE']);
function fail(code){throw Error(code)}
function windowsPathKey(value){
 if(typeof value!=='string'||!value||/[\x00-\x1f\x7f]/.test(value))fail('REVIEWED_NATIVE_IDENTITY_INVALID');
 let text=value.replaceAll('/','\\');
 if(text.startsWith('\\\\?\\UNC\\'))text='\\\\'+text.slice(8);
 else if(text.startsWith('\\\\?\\'))text=text.slice(4);
 if(!path.win32.isAbsolute(text)||!(/^[A-Za-z]:\\/.test(text)||/^\\\\[^\\]+\\[^\\]+(?:\\|$)/.test(text)))fail('REVIEWED_NATIVE_IDENTITY_INVALID');
 return path.win32.normalize(text).replace(/\\+$/,'').toLowerCase();
}
const validPid=value=>Number.isInteger(value)&&value>0&&value<=0xffffffff;
const validTime=value=>typeof value==='string'&&Number.isFinite(Date.parse(value));
function nativePaths(options={}){
 const home=options.homeDirectory||os.homedir();
 return {
  aoExecutable:options.aoExecutable||path.win32.join(home,'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe'),
  codexExecutable:options.codexExecutable||path.win32.join(home,'AppData','Roaming','npm','node_modules','@openai','codex','node_modules','@openai','codex-win32-x64','vendor','x86_64-pc-windows-msvc','bin','codex.exe')
 };
}
async function verifyReviewedNativeRuntime(entry,options={}){
 let timer,abort;
 const controller=new AbortController();
 try{
  const source=entry?.nativeRuntime;
  const expected=source&&Object.freeze(Object.fromEntries(['nativeSessionId','providerPid','providerCreatedAt','providerExecutable','hostPid','hostCreatedAt','terminalGeneration'].map(key=>[key,source[key]])));
  const id=entry?.id;
  const now=options.now||Date.now,start=now(),timeoutMs=options.timeoutMs??12000;
  if(typeof id!=='string'||!ID_RE.test(id)||!expected||!UUID_RE.test(expected.nativeSessionId||'')||/^0{8}-0{4}-0{4}-0{4}-0{12}$/.test(expected.nativeSessionId)||
   !validPid(expected.providerPid)||!validPid(expected.hostPid)||expected.providerPid===expected.hostPid||
   !validTime(expected.providerCreatedAt)||!validTime(expected.hostCreatedAt)||Date.parse(expected.hostCreatedAt)>Date.parse(expected.providerCreatedAt)||
   !Number.isFinite(start)||Date.parse(expected.providerCreatedAt)>start+1000||
   !(expected.terminalGeneration===null||typeof expected.terminalGeneration==='string'&&expected.terminalGeneration.length<=300&&!/[\x00-\x1f\x7f]/.test(expected.terminalGeneration))||
   !Number.isInteger(timeoutMs)||timeoutMs<1||timeoutMs>15000)fail('REVIEWED_NATIVE_IDENTITY_INVALID');
  const paths=nativePaths(options),approvedProvider=windowsPathKey(paths.codexExecutable);
  windowsPathKey(paths.aoExecutable);
  if(windowsPathKey(expected.providerExecutable)!==approvedProvider)fail('REVIEWED_NATIVE_EXECUTABLE_MISMATCH');
  if((options.platform||process.platform)!=='win32'&&!options.execFile)fail('REVIEWED_NATIVE_PLATFORM_UNAVAILABLE');
  if(options.signal?.aborted)fail('REVIEWED_NATIVE_INSPECTION_CANCELLED');
  let rejectStop;
  const stopped=new Promise((_,reject)=>{rejectStop=reject;});
  abort=()=>{controller.abort();rejectStop(Error(options.signal?.aborted?'REVIEWED_NATIVE_INSPECTION_CANCELLED':'REVIEWED_NATIVE_INSPECTION_TIMEOUT'));};
  options.signal?.addEventListener('abort',abort,{once:true});
  timer=setTimeout(abort,timeoutMs);
  const request=Object.freeze({version:1,probeId:randomBytes(16).toString('hex'),id,
   aoExecutable:paths.aoExecutable,codexExecutable:paths.codexExecutable,nativeRuntime:expected});
  const reader=options.execFile||execAsync;
  const result=await Promise.race([
   Promise.resolve().then(()=>reader(options.powershell||'C:\\Program Files\\PowerShell\\7\\pwsh.exe',
    ['-NoLogo','-NoProfile','-NonInteractive','-File',path.join(options.root||__dirname,'Get-ReviewedNativeRuntime.ps1'),
     '-RequestBase64',Buffer.from(JSON.stringify(request),'utf8').toString('base64')],
    {windowsHide:true,shell:false,timeout:timeoutMs,maxBuffer:65536,encoding:'utf8',signal:controller.signal})),
   stopped
  ]);
  if(controller.signal.aborted)fail(options.signal?.aborted?'REVIEWED_NATIVE_INSPECTION_CANCELLED':'REVIEWED_NATIVE_INSPECTION_TIMEOUT');
  let proof;try{proof=JSON.parse(String(result?.stdout||'').replace(/^\uFEFF/,'').trim())}catch{fail('REVIEWED_NATIVE_INSPECTION_UNAVAILABLE')}
  if(proof?.verified!==true){if(SAFE_CODES.has(proof?.reason))fail(proof.reason);fail('REVIEWED_NATIVE_INSPECTION_UNAVAILABLE')}
  const observed=Date.parse(proof.observedAt||''),finished=now();
  if(!Number.isFinite(finished)||finished<start||finished-start>timeoutMs)fail('REVIEWED_NATIVE_INSPECTION_TIMEOUT');
  if(proof.version!==1||proof.type!=='reviewed-native-runtime'||proof.probeId!==request.probeId||proof.id!==id||
   !Number.isFinite(observed)||observed<start-1000||observed>finished+1000||
   !PROOF_KEYS.every(key=>proof.proof?.[key]===true)||
   ['nativeSessionId','providerPid','providerCreatedAt','hostPid','hostCreatedAt'].some(key=>proof[key]!==expected[key])||
   windowsPathKey(proof.providerExecutable)!==approvedProvider)fail('REVIEWED_NATIVE_PROCESS_CHANGED');
  return {verified:true,observedAt:proof.observedAt,nativeSessionId:expected.nativeSessionId,
   providerPid:expected.providerPid,providerCreatedAt:expected.providerCreatedAt,providerExecutable:proof.providerExecutable,
   hostPid:expected.hostPid,hostCreatedAt:expected.hostCreatedAt};
 }catch(error){
  if(options.signal?.aborted)throw Error('REVIEWED_NATIVE_INSPECTION_CANCELLED');
  if(SAFE_CODES.has(error?.message))throw Error(error.message);
  throw Error('REVIEWED_NATIVE_INSPECTION_UNAVAILABLE');
 }finally{clearTimeout(timer);options.signal?.removeEventListener('abort',abort);}
}
module.exports={verifyReviewedNativeRuntime};
