'use strict';
// An exited child can leave its AO terminal host alive. Keep its process count
// intact; a separate generation-bound native status proof authorizes resume.
const path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const {execFile}=require('node:child_process'),{promisify}=require('node:util');
const {nativeSessionIdentity,nativeRuntimePaths}=require('./windows-runtime-evidence');
const exec=promisify(execFile),AGE=10000,TIMEOUT=12000;
const keys=['stableInventory','stableRegistry','stableInstance','listenerOwned','exactGeneration','noProviderChildren','nativeExitConfirmed'];
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
function eligible(i){return !!i&&i.mode==='tui'&&!i.isTerminated&&!i.hibernatedAt&&i.activity==='exited'&&i.statusReadiness==='ready'&&i.provisionState==='ready'&&/^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(i.terminalGeneration||'');}
function runtimeExitedHostEvidence(session,counts,now=Date.now()){
 const i=nativeSessionIdentity(session),e=counts?.exitedHostEvidence?.get(session?.id);
 if(!(counts instanceof Map)||counts.get(session?.id)!==1||!eligible(i)||!e||e.type!=='native-provider-exited'||e.version!==1||
  !same(i,e.identity)||!/^[a-f0-9]{32}$/.test(e.probeId||'')||e.launchId!==i.terminalGeneration||
  !Number.isSafeInteger(e.hostPid)||e.hostPid<=0||!Number.isInteger(e.exitCode)||!keys.every(k=>e.proof?.[k]===true))return null;
 const at=Date.parse(e.observedAt||''),birth=Date.parse(e.hostCreatedAt||'');
 if(!Number.isFinite(now)||!Number.isFinite(at)||!Number.isFinite(birth)||birth>at||now<at||now-at>AGE)return null;
 return e;
}
async function attachExitedHostEvidence(counts,sessions,options={}){
 counts.exitedHostEvidence=new Map();counts.exitedHostDiagnostics=[];
 const now=options.now||Date.now,report=reason=>counts.exitedHostDiagnostics.push(reason);
 if((options.platform||process.platform)!=='win32')return counts;
 if(!Array.isArray(sessions)||new Set(sessions.map(s=>s?.id)).size!==sessions.length||typeof options.readSessions!=='function'){report('INVALID_SESSION_INVENTORY');return counts;}
 const identities=sessions.map(nativeSessionIdentity).filter(i=>eligible(i)&&counts.get(i.id)===1);
 if(!identities.length)return counts;if(identities.length>32){report('TOO_MANY_EXITED_HOSTS');return counts;}
 const request={version:1,probeId:crypto.randomBytes(16).toString('hex'),targets:identities,
  aoExecutable:options.aoExecutable||path.win32.join(os.homedir(),'AppData','Local','Programs','agent-orchestrator','resources','daemon','ao.exe'),
  ...nativeRuntimePaths(options),expectedPort:3001};
 const controller=new AbortController();let timer,abort;
 try{
  if(options.signal?.aborted)throw Error('cancelled');
  const stop=new Promise((_,reject)=>{abort=()=>{controller.abort();reject(Error('cancelled'));};options.signal?.addEventListener('abort',abort,{once:true});timer=setTimeout(abort,TIMEOUT);});
  const probe=async()=>{
   if(options.runProbe)return options.runProbe(request);
   const result=await exec('C:\\Program Files\\PowerShell\\7\\pwsh.exe',['-NoLogo','-NoProfile','-NonInteractive','-File',path.join(options.root||__dirname,'Get-AoExitedHostEvidence.ps1'),'-RequestBase64',Buffer.from(JSON.stringify(request)).toString('base64')],{windowsHide:true,shell:false,timeout:TIMEOUT,maxBuffer:262144,signal:controller.signal});
   return JSON.parse(result.stdout.replace(/^\uFEFF/,''));
  };
  const value=await Promise.race([probe(),stop]);
  if(value?.version!==1||value.probeId!==request.probeId||value.complete!==true||!Array.isArray(value.results)||new Set(value.results.map(x=>x?.id)).size!==value.results.length)throw Error('invalid proof');
  const after=await Promise.race([Promise.resolve().then(options.readSessions),stop]);
  if(!Array.isArray(after)||new Set(after.map(s=>s?.id)).size!==after.length||controller.signal.aborted)throw Error('invalid recheck');
  for(const identity of identities){
   const row=value.results.find(x=>x.id===identity.id),current=after.find(s=>s.id===identity.id);
   if(row?.exited!==true){report(/^[A-Z_]{3,80}$/.test(row?.reason||'')?row.reason:'EXACT_HOST_EXIT_UNVERIFIED');continue;}
   if(!same(identity,nativeSessionIdentity(current))){report('SESSION_CHANGED');continue;}
   const evidence={version:1,type:'native-provider-exited',probeId:request.probeId,observedAt:value.observedAt,identity,
    hostPid:row.hostPid,hostCreatedAt:row.hostCreatedAt,launchId:row.launchId,exitCode:row.exitCode,proof:Object.fromEntries(keys.map(k=>[k,row.proof?.[k]===true]))};
   counts.exitedHostEvidence.set(identity.id,evidence);
   if(!runtimeExitedHostEvidence(current,counts,now()))counts.exitedHostEvidence.delete(identity.id);
  }
 }catch{counts.exitedHostEvidence.clear();report('EXACT_EXITED_HOST_UNVERIFIED');}
 finally{clearTimeout(timer);options.signal?.removeEventListener('abort',abort);}
 return counts;
}
module.exports={attachExitedHostEvidence,runtimeExitedHostEvidence};
