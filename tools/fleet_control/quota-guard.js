'use strict';
const fs=require('node:fs');
const path=require('node:path');
const readline=require('node:readline');
const {spawn,execFile}=require('node:child_process');
const ROOT=__dirname;
const CONFIG=path.join(ROOT,'quota-guard-config.json');
const STATUS=path.join(ROOT,'quota-guard-status.json');
const LATCH=path.join(ROOT,'emergency-latch.json');
const CODEX=path.join(ROOT,'codex-monitor','node_modules','@openai','codex-win32-x64','vendor','x86_64-pc-windows-msvc','bin','codex.exe');
const POWERSHELL='C:\\Program Files\\PowerShell\\7\\pwsh.exe';
const FALLBACK=path.join(ROOT,'Invoke-Emergency-Kill-Codex.ps1');
const WATCHDOG=path.join(ROOT,'Emergency-Stop-Codex.ps1');
const EMERGENCY_STATUS=path.join(ROOT,'emergency-status.json');
const WATCHDOG_LOG=path.join(ROOT,'watchdog-runtime.log');
const DEFAULT={enabled:true,pollSeconds:5,thresholdPercent:7,window:'any',autoKillArmed:false,tripped:false};
const short=x=>String(x||'').slice(0,260);
const clone=x=>JSON.parse(JSON.stringify(x));
function read(file,fallback){try{return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch{return clone(fallback)}}
function save(file,obj){const tmp=file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(obj,null,2));fs.renameSync(tmp,file)}
function sane(raw){
 const c={...DEFAULT,...raw};
 c.enabled=c.enabled===true;
 c.autoKillArmed=c.autoKillArmed===true;
 c.tripped=c.tripped===true;
 if(!Number.isInteger(c.pollSeconds)||c.pollSeconds<5||c.pollSeconds>60)c.pollSeconds=5;
 if(!Number.isInteger(c.thresholdPercent)||c.thresholdPercent<0||c.thresholdPercent>50)c.thresholdPercent=7;
 if(!['any','five-hour','weekly'].includes(c.window))c.window='any';
 return c;
}
function inspectLimits(value){
 const set=value?.rateLimitsByLimitId||null;
 const buckets=set&&Object.keys(set).length?set:value?.rateLimits?{codex:value.rateLimits}:{};
 const output=[];
 for(const [key,bucket] of Object.entries(buckets)){
  if(!(key==='codex'||bucket?.limitId==='codex'))continue;
  for(const kind of ['primary','secondary']){
   const w=bucket?.[kind];
   if(!w||!Number.isFinite(w.usedPercent)||w.usedPercent<0||w.usedPercent>100)continue;
   const mins=Number.isFinite(w.windowDurationMins)?w.windowDurationMins:null;
   const label=mins>=10000?'Weekly':mins>=240&&mins<=360?'5-hour':(String(mins||'?')+'-minute');
   const reset=Number.isFinite(w.resetsAt)&&w.resetsAt>0?new Date(w.resetsAt*1000).toISOString():null;
   output.push({bucket:'codex',kind,label,windowMinutes:mins,usedPercent:w.usedPercent,remainingPercent:100-w.usedPercent,resetsAt:reset});
  }
 }
 return output;
}
function shouldKill(config,remaining){return config.enabled===true&&config.autoKillArmed===true&&config.tripped!==true&&Number.isFinite(remaining)&&remaining<=config.thresholdPercent}
function selectedWindows(rows,mode){
 if(mode==='weekly')return rows.filter(r=>r.label==='Weekly');
 if(mode==='five-hour')return rows.filter(r=>r.label==='5-hour');
 return rows;
}
let proc=null,connecting=null,initialized=false,operating=false,backoffUntil=0,lastHeartbeat=0,lastSample=0,lastTrigger=0,rpcId=0;
const pending=new Map();
let state=read(STATUS,{status:'starting',windows:[],polledAt:null,lastSuccessfulPoll:null,heartbeatAt:null,error:null,source:'Codex App Server'});
state.monitorIdentity=null;
let lastSafetyLaunch=0;
function watchdogVerified(status,now=Date.now()){
 const heartbeat=Date.parse(status?.checkedAt||'');
 return status?.active===true&&['holding_zero','partial_stop'].includes(status.phase)&&
  Number.isFinite(heartbeat)&&now-heartbeat>=0&&now-heartbeat<30000;
}
function launchPowerShell(script,extra=[]){
 // On this Windows host detached:true exits pwsh without ever executing -File.
 const logFd=fs.openSync(WATCHDOG_LOG,'a');
 let child;
 try{
  child=spawn(POWERSHELL,['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',script,...extra],
   {cwd:ROOT,detached:false,windowsHide:true,stdio:['ignore',logFd,logFd]});
 }finally{fs.closeSync(logFd)}
 child.on('error',e=>{try{fs.appendFileSync(WATCHDOG_LOG,'PowerShell launch error: '+short(e.message)+'\n')}catch{}});
 child.unref();
 return child;
}
function ensureEmergencyWatchdog(){
 if(!fs.existsSync(POWERSHELL)||!fs.existsSync(WATCHDOG))return;
 if(watchdogVerified(read(EMERGENCY_STATUS,{})))return;
 if(Date.now()-lastSafetyLaunch<20000)return;
 lastSafetyLaunch=Date.now();
 try{launchPowerShell(WATCHDOG,['-Root',ROOT])}
 catch(e){try{fs.appendFileSync(WATCHDOG_LOG,'Quota emergency watchdog launch failed: '+short(e.message)+'\n')}catch{}}
}
function publish(patch={}){
 state={...state,...patch,heartbeatAt:new Date().toISOString(),pid:process.pid,config:sane(read(CONFIG,DEFAULT))};
 save(STATUS,state);lastHeartbeat=Date.now();
}
function stopRPC(reason){
 state.monitorIdentity=null;
 for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error(reason))}
 pending.clear();
 initialized=false;connecting=null;
 const old=proc;proc=null;
 if(old){try{old.kill()}catch{}}
}
function assertReadOnlyRPC(message){
 if(!message||!['initialize','initialized','account/rateLimits/read'].includes(message.method))throw Error('Quota monitor refuses non-metering RPC');
 return true;
}
async function registerMonitorIdentity(child){
 const code="$ErrorActionPreference='Stop';$g=Get-CimInstance Win32_Process -Filter 'ProcessId="+process.pid+"';$m=Get-CimInstance Win32_Process -Filter 'ProcessId="+child.pid+"';if(!$g -or !$m -or $m.ParentProcessId -ne $g.ProcessId){throw 'Monitor parent identity unavailable'};@{guardPid=$g.ProcessId;guardCreationKey=([DateTimeOffset]$g.CreationDate).ToUniversalTime().Ticks.ToString();guardExecutablePath=$g.ExecutablePath;monitorPid=$m.ProcessId;monitorCreationKey=([DateTimeOffset]$m.CreationDate).ToUniversalTime().Ticks.ToString()}|ConvertTo-Json -Compress";
 const output=await new Promise((resolve,reject)=>execFile(POWERSHELL,['-NoProfile','-NonInteractive','-Command',code],{windowsHide:true,timeout:6500,maxBuffer:12000},(error,stdout)=>error?reject(Error('Monitor process identity unavailable')):resolve(stdout)));
 const identity=JSON.parse(output.trim());
 if(identity.guardPid!==process.pid||identity.monitorPid!==child.pid||!identity.guardExecutablePath||!(/^\d+$/.test(identity.guardCreationKey))||!(/^\d+$/.test(identity.monitorCreationKey)))throw Error('Invalid monitor process identity');
 if(proc!==child||child.exitCode!==null)throw Error('Monitor exited before registration');
 publish({monitorIdentity:identity});
}
function send(data){
 assertReadOnlyRPC(data);
 if(!proc||!proc.stdin||proc.stdin.destroyed)throw Error('App-server not available');
 proc.stdin.write(JSON.stringify(data)+'\n');
}
function request(method,params=undefined,timeoutMs=7500){
 const id=++rpcId;
 return new Promise((resolve,reject)=>{
  const timer=setTimeout(()=>{pending.delete(id);reject(Error('RPC timeout: '+method))},timeoutMs);
  pending.set(id,{resolve,reject,timer});
  try{send({id,method,...(params===undefined?{}:{params})})}
  catch(e){clearTimeout(timer);pending.delete(id);reject(e)}
 });
}
async function connect(){
 if(initialized)return;
 if(connecting)return connecting;
 if(Date.now()<backoffUntil)throw Error('Codex telemetry reconnect cooldown');
 connecting=(async()=>{
  if(!fs.existsSync(CODEX))throw Error('Isolated Codex monitoring binary is not installed');
  if(proc)stopRPC('reconnect');
  proc=spawn(CODEX,['app-server','--listen','stdio://'],{windowsHide:true,stdio:['pipe','pipe','pipe'],cwd:ROOT});
  const child=proc;
  child.on('error',()=>{initialized=false});
  child.on('exit',()=>{
   if(proc!==child)return;
   proc=null;initialized=false;
   for(const p of pending.values()){clearTimeout(p.timer);p.reject(Error('Codex app-server exited'))}
   pending.clear();
  });
  readline.createInterface({input:child.stdout}).on('line',line=>{
   let obj;try{obj=JSON.parse(line)}catch{return}
   if(obj.method==='account/rateLimits/updated'){
    setTimeout(()=>poll().catch(()=>{}),300).unref?.();
    return;
   }
   if(typeof obj.id!=='number')return;
   const match=pending.get(obj.id);if(!match)return;
   pending.delete(obj.id);clearTimeout(match.timer);
   obj.error?match.reject(Error(short(obj.error.message||JSON.stringify(obj.error)))):match.resolve(obj.result);
  });
  child.stderr.on('data',()=>{}); // never log raw stderr or account metadata
  await registerMonitorIdentity(child);
  await request('initialize',{clientInfo:{name:'mjobs_fleet_control_quota_guard',title:'MJobs Fleet Control Usage Guard',version:'1.0.0'},capabilities:{experimentalApi:true}});
  send({method:'initialized',params:{}});
  initialized=true;
 })();
 try{await connecting}
 catch(e){backoffUntil=Date.now()+9000;stopRPC('initialization failed');throw e}
 finally{connecting=null}
}
async function triggerKill(config,pct,label){
 // Persistent armed/tripped state governs each authorization, not a process-lifetime flag.
 // A previous stop must not disable a later explicitly re-armed 7% campaign.
 const authorized=sane(read(CONFIG,DEFAULT));
 if(!authorized.autoKillArmed||authorized.tripped||authorized.window!==config.window||authorized.thresholdPercent!==config.thresholdPercent)return;
 lastTrigger=Date.now();
 const next={...config,autoKillArmed:false,tripped:true,triggeredAt:new Date().toISOString(),triggerRemainingPercent:pct,triggerWindow:label};
 save(CONFIG,next);
 publish({status:'triggering_emergency',triggeredAt:next.triggeredAt,triggerRemainingPercent:pct,triggerWindow:label});
 stopRPC('Quota threshold reached');
 let accepted=false;
 try{
  const resp=await fetch('http://127.0.0.1:8787/api/master/emergency',{
   method:'POST',headers:{'Content-Type':'application/json','X-AO-Fleet':'1'},
   body:JSON.stringify({confirm:'KILL ALL CODEX'}),signal:AbortSignal.timeout(3000)
  });
  accepted=resp.ok;
 }catch{}
 if(!accepted&&fs.existsSync(POWERSHELL)&&fs.existsSync(FALLBACK)){
  // The dashboard could not acknowledge the stop: use the offline latch
  // launcher, with verified PowerShell execution and persistent stderr.
  launchPowerShell(FALLBACK,['-Confirm','KILL ALL CODEX']);
 }
 publish({status:accepted?'emergency_submitted':'emergency_fallback_requested',triggeredAt:next.triggeredAt});
}
async function poll(){
 if(operating)return;
 operating=true;
 try{
  const c=sane(read(CONFIG,DEFAULT));
  const emergency=read(LATCH,{enabled:false}).enabled===true;
  if(emergency)ensureEmergencyWatchdog(); // Safety stays latched; read-only rate-limit RPC is not an agent turn.
  if(!c.enabled){stopRPC('Monitoring disabled');publish({status:emergency?'emergency_locked':'disabled',error:null});return}
  await connect();
  const result=await request('account/rateLimits/read',undefined,7000);
  const windows=inspectLimits(result);
  if(!windows.length)throw Error('Codex returned no valid quota window');
  const now=Date.now();
  const selected=selectedWindows(windows,c.window).filter(w=>!w.resetsAt||Date.parse(w.resetsAt)>now-15000);
  const remain=selected.length?Math.min(...selected.map(w=>w.remainingPercent)):null;
  const label=selected.find(w=>w.remainingPercent===remain)?.label||null;
  lastSample=now;
  publish({status:emergency?'emergency_locked':selected.length?'fresh':'window_unavailable',error:null,
   windows,polledAt:new Date().toISOString(),lastSuccessfulPoll:new Date().toISOString(),
   selectedRemainingPercent:remain,selectedWindow:label,
   source:'Codex App Server — account/rateLimits/read',precision:'whole percent',ordinaryUsageAllowed:result?.ordinaryUsageAllowed??null});
  if(!emergency&&shouldKill(c,remain))await triggerKill(c,remain,label);
 }catch(e){
  const message=short(e.message);
  publish({status:'unavailable',error:message,selectedRemainingPercent:null,selectedWindow:null,lastSuccessfulPoll:state.lastSuccessfulPoll||null});
  if(/timeout|exited|initializ|closed|broken pipe/i.test(message))stopRPC(message);
 }finally{operating=false}
}
if(require.main===module){
 if(!fs.existsSync(CONFIG))save(CONFIG,DEFAULT);
 poll().catch(()=>{});
 let previous=Date.now();
 const timer=setInterval(()=>{
  const c=sane(read(CONFIG,DEFAULT));
  if(Date.now()-previous>=c.pollSeconds*1000){previous=Date.now();poll().catch(()=>{})}
  if(Date.now()-lastHeartbeat>5000)publish({});
 },1000);
 process.on('SIGINT',()=>{clearInterval(timer);stopRPC('stop');process.exit(0)});
 process.on('SIGTERM',()=>{clearInterval(timer);stopRPC('stop');process.exit(0)});
}
module.exports={inspectLimits,selectedWindows,sane,shouldKill,watchdogVerified,assertReadOnlyRPC,DEFAULT};
