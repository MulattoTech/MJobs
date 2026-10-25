'use strict';
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {spawn}=require('node:child_process');
const {randomUUID}=require('node:crypto');
const {RestoreFleet,unresolvedNativeReason}=require('./restore-fleet');

const ROOT=__dirname;
const INIT_DRAIN={enabled:false,phase:'idle',startedAt:null,completedAt:null,processed:{},errors:[],counts:null,lastCheck:null};
function read(file,fallback){try{return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch{return structuredClone(fallback)}}
function write(file,value){
 const tmp=file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(value,null,2),'utf8');fs.renameSync(tmp,file);
}
function error(e){return String(e?.message||e).slice(0,450)}
class MasterControls{
 constructor({root=ROOT,sustain,turbo,command,audit,projectId='mjobs-luna',getState,getAllSessions=null,autoschedule=true,restoreOptions={},restoreCommand=null,startBlockReason=()=>null}){
  this.root=root;this.sustain=sustain;this.turbo=turbo;this.command=command;
  this.audit=audit;this.projectId=projectId;this.getState=getState;
  this.startBlockReason=startBlockReason;
  this.latchPath=path.join(root,'emergency-latch.json');
  this.statusPath=path.join(root,'emergency-status.json');
  this.drainPath=path.join(root,'drain-state.json');
  this.latch=read(this.latchPath,{enabled:false});
  this.drainState=read(this.drainPath,INIT_DRAIN);
  this.draining=false;this.drainTimer=null;this.watchdogTimer=null;
  this.lastWatchdogLaunch=0;
  this.restore=new RestoreFleet({root,snapshotFrozen:()=>this.drainState.enabled||['completed','paused','interrupted_by_emergency'].includes(this.drainState.phase),getGlobalSessions:getAllSessions|| (async()=>{
   const response=await fetch('http://127.0.0.1:3001/api/v1/sessions',{signal:AbortSignal.timeout(2000)});
   if(!response.ok)throw Error('AO HTTP '+response.status);
   const data=await response.json();return data.sessions||[];
  }),...restoreOptions,locked:()=>this.locked(),startBlockReason:()=>startBlockReason()||this.quota?.dispatchBlockReason?.()||(!this.quota?'Quota guard has not initialized':null),dispatchOff:()=>this.disableAutomaticDispatch(),command:restoreCommand||command,audit,autoSchedule:autoschedule,onComplete:async({autoDispatchAfter})=>{
    if(autoDispatchAfter&&!startBlockReason()&&!this.locked()&&!this.drainState.enabled&&!this.quota?.dispatchBlockReason?.()){this.sustain.enable(true);this.audit('master_restore_dispatch_resumed',{})}
   }});
  if(this.locked()){
   this.disableAutomaticDispatch();
   if(autoschedule)this.ensureWatchdog();
  }
  if(this.drainState.enabled&&!this.latch.enabled&&autoschedule)this.startDrainTimer();
  if(autoschedule){
   this.watchdogTimer=setInterval(()=>{
    if(this.locked())this.ensureWatchdog();
   },12000);
   this.watchdogTimer.unref?.();
  }
 }
 disableAutomaticDispatch(){
  if(this.sustain?.config.enabled)this.sustain.enable(false);
  if(this.turbo?.config.enabled)this.turbo.enable(false);
 }
 locked(){
  // The offline emergency launcher may update the latch while this server remains alive.
  try{
   const disk=JSON.parse(fs.readFileSync(this.latchPath,'utf8').replace(/^\uFEFF/,''));
   if(typeof disk.enabled!=='boolean')return true;
   this.latch=disk;return disk.enabled;
  }catch(e){return e.code==='ENOENT'?this.latch.enabled===true:true}
 }
 status(){
  const emergency=read(this.statusPath,{phase:this.latch.enabled?'pending':'inactive'});
  return {
   timestamp:new Date().toISOString(),
   dispatch:{sustainedEnabled:!!this.sustain?.config.enabled,turboEnabled:!!this.turbo?.config.enabled},
   emergency:{latched:this.locked(),...emergency},
   restore:this.restore.status(),
   drain:{
    enabled:!!this.drainState.enabled,phase:this.drainState.phase,
    startedAt:this.drainState.startedAt,completedAt:this.drainState.completedAt,
    lastCheck:this.drainState.lastCheck,
    counts:this.drainState.counts,
    exitedCount:Object.keys(this.drainState.processed||{}).length,
    recentErrors:(this.drainState.errors||[]).slice(-4)
   }
  };
 }
 pause(){
  if(this.locked())throw Error('Emergency lockout is active; dispatch is already disabled');
  this.disableAutomaticDispatch();
  if(this.restore.state.active)this.restore.pause();
  this.audit('master_pause',{});
  return this.status();
 }
 async drain(){
  const wasRestoring=this.restore.state.active;
  if(wasRestoring)this.restore.pause();
  if(!wasRestoring)await this.restore.captureSnapshot({force:true}).catch(()=>{});
  if(this.locked())throw Error('Emergency lockout active. There are no agent turns to drain.');
  this.disableAutomaticDispatch();
  this.drainState={
   enabled:true,phase:'draining',
   startedAt:new Date().toISOString(),completedAt:null,
   processed:this.drainState.processed||{},errors:[],counts:null,lastCheck:null
  };
  write(this.drainPath,this.drainState);
  this.startDrainTimer();
  this.audit('master_drain_started',{projectId:this.projectId});
  // Ask an active Chat primary to stop delegating while finishing its current turn.
  // A busy primary is never killed by graceful shutdown.
  try{
   const snapshot=await this.getState(true);
   const primary=(snapshot.sessions||[]).find(s=>s.kind==='orchestrator'&&s.busy);
   if(primary){
    const message='GRACEFUL WIND-DOWN ORDER FROM USER: stop assigning new tasks, avoid starting additional test jobs or PRs, finish or checkpoint only your current work, preserve all branches and uncommitted files, then exit your agent turn. Fleet Control is stopping idle runtimes as they finish. Do not restart agents or resume autonomous orchestration until manually requested.';
    const args=['send','--session',primary.id,'--message',message];
    if(primary.mode==='chat')args.push('--steer','--client-message-id',randomUUID());
    await this.command(args,12000,{windDown:true});
    this.audit('master_drain_root_notified',{sessionId:primary.id});
   }
  }catch(e){
   this.drainState.errors.push({id:'primary',message:'Wind-down notice uncertain: '+error(e),at:new Date().toISOString()});
  }
  this.drainTick().catch(e=>this.drainFailure(e));
  return this.status();
 }
 startDrainTimer(){
  if(this.drainTimer)return;
  this.drainTimer=setInterval(()=>this.drainTick().catch(e=>this.drainFailure(e)),12000);
  this.drainTimer.unref?.();
 }
 stopDrainTimer(){
  if(this.drainTimer){clearInterval(this.drainTimer);this.drainTimer=null}
 }
 async drainTick(){
  if(this.draining||!this.drainState.enabled||this.locked())return;
  this.draining=true;
  try{
   // Once drain has begun, no new work is permitted from either local scheduler.
   this.disableAutomaticDispatch();
   const snapshot=await this.getState(true);
   const candidates=(snapshot.sessions||[]).filter(s=>!s.isTerminated);
   const working=candidates.filter(s=>s.busy||s.activity==='active'||s.activity==='working');
   const idle=candidates.filter(s=>s.isIdle||s.activity==='idle');
   const exited=candidates.filter(s=>s.activity==='exited');
   const transient=candidates.filter(s=>!s.busy&&!s.isIdle&&s.activity!=='exited');
   this.drainState.lastCheck=new Date().toISOString();
   this.drainState.counts={total:candidates.length,working:working.length,idle:idle.length,exited:exited.length,other:transient.length};
   const batch=idle.filter(s=>!this.drainState.processed[s.id]||this.drainState.processed[s.id].result==='failed').sort((a,b)=>{
    if(a.kind==='orchestrator'&&b.kind!=='orchestrator')return -1;
    if(b.kind==='orchestrator'&&a.kind!=='orchestrator')return 1;
    return a.id.localeCompare(b.id,undefined,{numeric:true});
   }).slice(0,4);
   const done=await Promise.all(batch.map(async s=>{
    try{
     await this.command(['session','exit-agent',s.id,'--project',this.projectId,'--json'],17000);
     const rec={at:new Date().toISOString(),result:'exited'};
     this.drainState.processed[s.id]=rec;
     this.audit('master_drain_exit',{sessionId:s.id});
     return true;
    }catch(e){
     const rec={at:new Date().toISOString(),result:'failed',error:error(e)};
     this.drainState.processed[s.id]=rec;
     this.drainState.errors.push({id:s.id,message:error(e),at:rec.at});
     return false;
    }
   }));
   if(working.length===0&&idle.length===0&&transient.length===0){
    this.drainState.enabled=false;this.drainState.phase='completed';
    this.drainState.completedAt=new Date().toISOString();
    this.stopDrainTimer();
    this.audit('master_drain_finished',{exitedCount:Object.keys(this.drainState.processed).length});
   }
   write(this.drainPath,this.drainState);
  }finally{this.draining=false}
 }
 drainFailure(e){
  this.drainState.phase='awaiting_retry';
  this.drainState.errors.push({at:new Date().toISOString(),message:error(e)});
  write(this.drainPath,this.drainState);
 }
 cancelDrain(){
  if(this.locked())throw Error('Cannot cancel while emergency stop is latched');
  this.drainState.enabled=false;this.drainState.phase='paused';
  write(this.drainPath,this.drainState);
  this.stopDrainTimer();
  this.audit('master_drain_cancelled',{});
  return this.status();
 }
 spawnWatchdog(){
  const script=path.join(this.root,'Emergency-Stop-Codex.ps1');
  if(!fs.existsSync(script))throw Error('Emergency kill script unavailable');
  const pwsh='C:\\Program Files\\PowerShell\\7\\pwsh.exe';
  if(!fs.existsSync(pwsh))throw Error('PowerShell 7 is required for the emergency watchdog');
  // Log startup/exit diagnostics. PowerShell must not run with detached:true
  // on this Windows host: that exits successfully without executing -File.
  const logFile=path.join(this.root,'watchdog-runtime.log');
  const logFd=fs.openSync(logFile,'a');
  let child;
  try{
   child=spawn(pwsh,['-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',script,'-Root',this.root],{
    // Windows detached:true causes pwsh to exit before -File executes on this host.
    // Keep it attached to the long-running dashboard; unref still avoids waiting.
    cwd:this.root,detached:false,windowsHide:true,stdio:['ignore',logFd,logFd]
   });
  }finally{fs.closeSync(logFd)}
  child.on('error',e=>{
   this.audit('master_watchdog_launch_error',{pid:child.pid||null,message:error(e)});
   const current=read(this.statusPath,{});
   if(this.locked()&&current.phase==='starting'&&current.watchdogPid===child.pid)
    write(this.statusPath,{...current,phase:'watchdog_start_error',checkedAt:new Date().toISOString(),error:error(e)});
  });
  child.on('exit',(code,signal)=>{
   this.audit('master_watchdog_exited',{pid:child.pid,code,signal});
   const current=read(this.statusPath,{});
   if(this.locked()&&current.phase==='starting'&&current.watchdogPid===child.pid)
    write(this.statusPath,{...current,phase:'watchdog_exited_without_sweep',checkedAt:new Date().toISOString(),error:'Watchdog exited before its first verified shutdown sweep: '+code});
  });
  child.unref();
  if(!child.pid)throw Error('Unable to start emergency watchdog process');
  this.lastWatchdogLaunch=Date.now();
  this.audit('master_watchdog_spawned',{pid:child.pid});
  return child.pid;
 }
 ensureWatchdog(){
  if(!this.locked())return false;
  const stat=read(this.statusPath,{});
  const age=Date.now()-Date.parse(stat.checkedAt||stat.startedAt||0);
  const fresh=Number.isFinite(age)&&age>=0&&age<35000;
  if(fresh&&stat.active)return true;
  if(Date.now()-this.lastWatchdogLaunch<14000)return true;
  // Publish starting state before launching; never overwrite the child's newer heartbeat.
  write(this.statusPath,{active:true,phase:'starting',startedAt:new Date().toISOString(),watchdogPid:null,remainingTotal:null,dashboardPreserved:true});
  const pid=this.spawnWatchdog();
  const observed=read(this.statusPath,{});
  if(observed.phase==='starting'&&!observed.checkedAt)write(this.statusPath,{...observed,watchdogPid:pid});
  return true;
 }
 async emergency(){
  if(this.locked()){
   this.ensureWatchdog();
   return this.status();
  }
  // Capture all AO-managed session IDs before killing the AO daemon.
  const wasRestoring=this.restore.state.active;
  if(wasRestoring)this.restore.pause();
  if(!wasRestoring)await this.restore.captureSnapshot({force:true}).catch(()=>{});
  // Latch MUST precede process termination; the latch persists on disk.
  this.quota?.disarm();
  this.disableAutomaticDispatch();
  this.drainState.enabled=false;this.drainState.phase='interrupted_by_emergency';
  write(this.drainPath,this.drainState);
  this.stopDrainTimer();
  this.latch={enabled:true,triggeredAt:new Date().toISOString(),scope:'all Codex processes across the host',explicitlyRequested:true};
  write(this.latchPath,this.latch);
  this.audit('master_emergency_armed',{});
  try{this.ensureWatchdog()}
  catch(e){
   const status={active:true,phase:'watchdog_start_error',checkedAt:new Date().toISOString(),error:error(e),remainingTotal:null};
   write(this.statusPath,status);
   this.audit('master_emergency_error',{message:error(e)});
   throw e;
  }
  return this.status();
 }
 async restorePreview(){
  return await this.restore.preview();
 }
 async restoreStart(options={}){
  if(this.locked())throw Error('Emergency stop is latched. Explicitly release the lockout after quota resets, then re-arm the 7% quota guard before restoring any agent.');
  if(this.restore.state.active)return this.status();
  if(this.restore.running)throw Error('A previous restore tick is still running');
  const previousNative=unresolvedNativeReason(this.restore.state);
  if(previousNative)throw Error(previousNative);
  // A prior emergency trip sets autoKillArmed=false. Treat that as STOP,
  // never as permission to restore without monitoring.
  const quota=this.quota?.status();
  if(!quota?.config?.autoKillArmed || quota.config.tripped)
   throw Error('Codex quota protection must be explicitly re-armed before restoring agents.');
  if(!quota.fresh || quota.selectedRemainingPercent===null)
   throw Error('Codex usage is stale. Wait for fresh quota telemetry before restoring agents.');
  if(quota.selectedRemainingPercent<=quota.config.thresholdPercent)
   throw Error('Usage is at the automatic-stop threshold. Restore is blocked to preserve the cutoff.');
  if(this.drainState.enabled)this.cancelDrain();
  const saved=this.restore.getSnapshot();
  if(!saved.sessions?.length||!saved.restorableCount)await this.restore.captureSnapshot().catch(()=>{});
  const before=await this.restore.preview();
  if(before.totalTargets===0)throw Error('No AO-managed Codex sessions are available in the saved recovery manifest. Restore canceled safely.');
  if(before.alreadyOnline===before.totalTargets&&before.willResume===0)throw Error('All preserved session runtimes are already online. Nothing needs restoring. No dispatch settings changed.');
  const stopReason=unresolvedNativeReason(this.restore.state)||this.startBlockReason()||this.quota?.dispatchBlockReason?.();
  if(this.locked()||stopReason)throw Error('Restore blocked by emergency/quota safety: '+(stopReason||'Emergency latch was reactivated'));
  this.disableAutomaticDispatch();
  const out=this.restore.start({autoDispatchAfter:options.autoDispatchAfter===true,fromEmergency:before.emergencyLatched});
  this.drainState.enabled=false;this.drainState.phase='idle';this.drainState.processed={};write(this.drainPath,this.drainState);
  this.audit('master_restore_requested',{count:out.total,autoDispatchAfter:out.autoDispatchAfter,fromEmergency:before.emergencyLatched});
  return this.status();
 }
 restorePause(){
  const out=this.restore.pause();
  this.audit('master_restore_paused',{});
  return this.status();
 }
 rearm({expectedRevision=null}={}){
  // Read the on-disk latch again immediately before a synchronous release.
  // Startup may only release the exact stop that its user request inspected.
  if(expectedRevision!==null){
   const crypto=require('node:crypto');
   let bytes;try{bytes=fs.readFileSync(this.latchPath)}catch{throw Error('Emergency latch changed or is unreadable; no release performed')}
   if(crypto.createHash('sha256').update(bytes).digest('hex')!==expectedRevision)throw Error('A newer emergency stop superseded this release request');
  }
  if(!this.locked())return this.status();
  this.latch={enabled:false,rearmedAt:new Date().toISOString()};
  write(this.latchPath,this.latch);
  this.disableAutomaticDispatch();
  // Re-arming the emergency latch must not silently disable an armed quota guard.
  this.audit('master_emergency_rearmed',{});
  return this.status();
 }
}
module.exports={MasterControls,INIT_DRAIN};
