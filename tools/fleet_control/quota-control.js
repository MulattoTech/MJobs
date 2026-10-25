'use strict';
const fs=require('node:fs');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {DEFAULT,sane,selectedWindows}=require('./quota-guard');
function load(file,fallback){try{return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''))}catch{return structuredClone(fallback)}}
function persist(file,data){const tmp=file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(data,null,2));fs.renameSync(tmp,file)}
class QuotaControl{
 constructor({root,audit=()=>{},locked=()=>false,autoSchedule=true,spawnMonitor=null}){
  this.root=root;this.audit=audit;this.locked=locked;
  this.configPath=path.join(root,'quota-guard-config.json');
  this.statusPath=path.join(root,'quota-guard-status.json');
  this.script=path.join(root,'quota-guard.js');
  this.launcher=spawnMonitor||this.launchMonitor.bind(this);
  this.lastLaunch=0;this.timer=null;
  if(!fs.existsSync(this.configPath))persist(this.configPath,DEFAULT);
  if(autoSchedule){this.ensure();this.timer=setInterval(()=>this.ensure(),10000);this.timer.unref?.()}
 }
 config(){return sane(load(this.configPath,DEFAULT))}
 status(){
  const config=this.config();
  const info=load(this.statusPath,{status:'starting',windows:[],lastSuccessfulPoll:null,heartbeatAt:null});
  const now=Date.now();
  const heartbeat=Date.parse(info.heartbeatAt||'');
  const success=Date.parse(info.lastSuccessfulPoll||'');
  const sampleAge=now-success,heartbeatAge=now-heartbeat;
  const age=Number.isFinite(success)&&sampleAge>=0?Math.floor(sampleAge/1000):null;
  const alive=Number.isFinite(heartbeat)&&heartbeatAge>=0&&heartbeatAge<22000;
  const fresh=config.enabled&&alive&&['fresh','emergency_locked'].includes(info.status)&&!info.error&&age!==null&&sampleAge<=Math.max(25,config.pollSeconds*3)*1000;
  const windows=Array.isArray(info.windows)?info.windows.filter(x=>Number.isFinite(x.remainingPercent)&&x.remainingPercent>=0&&x.remainingPercent<=100):[],
   selected=selectedWindows(windows,config.window).filter(x=>!x.resetsAt||(Number.isFinite(Date.parse(x.resetsAt))&&Date.parse(x.resetsAt)>now));
  const selectedRemaining=fresh&&selected.length?Math.min(...selected.map(x=>x.remainingPercent)):null;
  return {
   config,
   monitorOnline:alive,
   fresh:fresh&&selected.length>0,
   sampledAt:info.lastSuccessfulPoll||null,
   ageSeconds:age,
   source:'Codex App Server — account/rateLimits/read',
   precision:'whole percentage points',
   selectedRemainingPercent:selectedRemaining,
   monitoredWindow:selected.find(x=>x.remainingPercent===selectedRemaining)?.label||null,
   windows,
   status:info.status||'starting',
   error:info.error||null,
   ordinaryUsageAllowed:info.ordinaryUsageAllowed??null,
   trigger:{
    tripped:config.tripped,triggeredAt:config.triggeredAt||null,
    remainingAtTrigger:config.triggerRemainingPercent??null,
    window:config.triggerWindow||null
   },
   warning:!alive?'Quota monitor is offline':!fresh?'Usage snapshot unavailable or stale':selected.length===0?'Selected quota window unavailable':null
  };
 }
 dispatchBlockReason(){
  const s=this.status();
  if(s.config.tripped)return 'Automatic quota stop has tripped; explicitly re-arm protection before starting more work';
  if(!s.config.autoKillArmed)return 'Codex '+s.config.thresholdPercent+'% emergency quota guard is not armed; start/dispatch is disabled until you explicitly arm it';
  if(!s.config.enabled)return 'Quota protection is armed but monitoring is disabled';
  if(!s.monitorOnline||!s.fresh||s.selectedRemainingPercent===null)return 'Quota protection is armed but the usage reading is unavailable or stale; new work is paused';
  if(s.ordinaryUsageAllowed===false)return 'Codex reports that ordinary usage is unavailable; new work is paused';
  if(s.selectedRemainingPercent<=s.config.thresholdPercent)return 'Quota safety threshold reached at '+s.selectedRemainingPercent+'% remaining; new work is blocked';
  // New assignments stop 8 percentage points early. Already-running agent turns
  // can consume quota after the last provider sample; the hard kill stays at 7%.
  const brake=Math.min(100,s.config.thresholdPercent+8);
  if(s.selectedRemainingPercent<=brake)return 'Quota safety brake: '+s.selectedRemainingPercent+'% remaining (new assignments paused at '+brake+'%, emergency at '+s.config.thresholdPercent+'%)';
  return null;
 }
 validateUpdate(x){
  const allowed=['thresholdPercent','window','pollSeconds','enabled'];
  for(const key of Object.keys(x||{}))if(!allowed.includes(key))throw Error('Unsupported guard option: '+key);
  if(x.thresholdPercent!==undefined&&(!Number.isInteger(x.thresholdPercent)||x.thresholdPercent<0||x.thresholdPercent>50))
   throw Error('Stop threshold must be an integer percentage from 0 to 50');
  if(x.pollSeconds!==undefined&&(!Number.isInteger(x.pollSeconds)||x.pollSeconds<5||x.pollSeconds>60))
   throw Error('Poll interval must be 5–60 seconds');
  if(x.window!==undefined&&!['any','five-hour','weekly'].includes(x.window))throw Error('Invalid quota window');
  if(x.enabled!==undefined&&typeof x.enabled!=='boolean')throw Error('enabled must be boolean');
  return {...this.config(),...x,updatedAt:new Date().toISOString()};
 }
 update(x){
  const config=this.validateUpdate(x);
  if(this.locked())throw Error('Emergency lockout is active');
  if(config.autoKillArmed){
   const before=this.config();
   if(before.thresholdPercent!==config.thresholdPercent||before.window!==config.window)config.autoKillArmed=false;
  }
  persist(this.configPath,config);this.audit('quota_settings',{thresholdPercent:config.thresholdPercent,window:config.window,pollSeconds:config.pollSeconds});
  this.ensure();return this.status();
 }
 arm(){
  if(this.locked())throw Error('Emergency lockout is active; re-arm Codex first');
  const s=this.status();
  if(!s.monitorOnline||!s.fresh||s.selectedRemainingPercent===null)throw Error('Wait for a fresh Codex quota sample before arming auto-kill');
  if(s.ordinaryUsageAllowed===false||s.selectedRemainingPercent<=Math.min(100,s.config.thresholdPercent+8))throw Error('Usage must be above the safety brake ('+(s.config.thresholdPercent+8)+'% remaining) before arming a new sprint.');
  const config={...this.config(),enabled:true,autoKillArmed:true,tripped:false,updatedAt:new Date().toISOString()};
  persist(this.configPath,config);
  this.audit('quota_auto_stop_armed',{threshold:config.thresholdPercent,window:config.window,remaining:s.selectedRemainingPercent});
  this.ensure();return this.status();
 }
 disarm(){
  const config={...this.config(),autoKillArmed:false,updatedAt:new Date().toISOString()};
  persist(this.configPath,config);
  this.audit('quota_auto_stop_disarmed',{});
  return this.status();
 }
 launchMonitor(){
  const node='C:\\Program Files\\nodejs\\node.exe';
  if(!fs.existsSync(node))throw Error('Node.js binary missing');
  const script=this.script;
  if(!fs.existsSync(script))throw Error('Codex quota monitor script missing');
  const child=spawn(node,[script],{cwd:this.root,windowsHide:true,detached:true,stdio:'ignore'});
  child.unref();
  if(!child.pid)throw Error('Could not launch Codex quota guard');
  return child.pid;
 }
 ensure(){
  const s=this.status();
  if(s.monitorOnline)return true;
  if(Date.now()-this.lastLaunch<20000)return false;
  this.lastLaunch=Date.now();
  try{const pid=this.launcher();this.audit('quota_guard_started',{pid});return true}
  catch(e){this.audit('quota_guard_start_failed',{message:String(e.message).slice(0,160)});return false}
 }
}
module.exports={QuotaControl};
