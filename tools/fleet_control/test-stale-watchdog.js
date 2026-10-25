'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {RestoreFleet}=require('./restore-fleet');
const {MasterControls}=require('./master-controls');
const root=path.join(__dirname,'test-stale-watchdog-state');
fs.rmSync(root,{recursive:true,force:true});fs.mkdirSync(root,{recursive:true});
const old=new Date(Date.now()-60*60*1000).toISOString();
const status=path.join(root,'emergency-status.json');
const starting=()=>fs.writeFileSync(status,JSON.stringify({active:true,phase:'starting',watchdogPid:12345,startedAt:old}));
let latched=false;
let alive=false;
let checks=0;
const r=new RestoreFleet({root,autoSchedule:false,locked:()=>latched,
 watchdogInspector:async()=>{checks++;return {active:alive,watchdogPids:alive?[12345]:[]}},
 audit:()=>{},getGlobalSessions:async()=>[],resourceReader:async()=>({ramGiB:8,diskGiB:50,cpuPercent:12})});
async function main(){
 starting();alive=true;
 assert.equal(await r.watchdogDisarmed(true),false);
 assert.equal(JSON.parse(fs.readFileSync(status)).phase,'starting');
 starting();alive=false;latched=true;
 assert.equal(await r.watchdogDisarmed(true),false);
 assert.equal(checks,1,'do not inspect under an active emergency latch');
 latched=false;
 assert.equal(await r.watchdogDisarmed(true),true);
 const read=JSON.parse(fs.readFileSync(status));
 assert.equal(read.phase,'disarmed');assert.equal(read.active,false);
 assert.equal(read.watchdogPid,null);
 assert.equal(await r.watchdogDisarmed(true),true);
 fs.writeFileSync(status,JSON.stringify({active:true,phase:'starting',startedAt:new Date().toISOString(),watchdogPid:444}));
 assert.equal(await r.watchdogDisarmed(true),false,'brand-new workers must not be presumed dead');
 starting();
 r.watchdogInspector=async()=>{throw Error('Process listing unavailable')};
 assert.equal(await r.watchdogDisarmed(true),false,'process query failure must fail closed');
 // Real non-detached PowerShell launch against a DISARMED fixture:
 // verifies -File actually executed and no production process is terminated.
 fs.copyFileSync(path.join(__dirname,'Emergency-Stop-Codex.ps1'),path.join(root,'Emergency-Stop-Codex.ps1'));
 fs.writeFileSync(path.join(root,'emergency-latch.json'),JSON.stringify({enabled:false}));
 starting();
 const events=[];
 const ctl=new MasterControls({root,autoschedule:false,sustain:{config:{enabled:false},enable(){}},
  turbo:{config:{enabled:false},enable(){}},command:async()=>{},audit:(event,data)=>events.push({event,data}),
  getState:async()=>({sessions:[]}),getAllSessions:async()=>[]});
 const childPid=ctl.spawnWatchdog();
 assert.ok(childPid>0,'PowerShell child PID must be recorded');
 for(let i=0;i<80&&!events.some(e=>e.event==='master_watchdog_exited');i++)
  await new Promise(resolve=>setTimeout(resolve,100));
 assert.ok(events.some(e=>e.event==='master_watchdog_exited'&&e.data.code===0),
  'disarmed fixture watchdog must exit cleanly and report its status');
 assert.equal(JSON.parse(fs.readFileSync(status,'utf8')).phase,'disarmed',
  'child exit without status: '+fs.readFileSync(path.join(root,'watchdog-runtime.log'),'utf8').slice(-600));
 assert.ok(fs.existsSync(path.join(root,'watchdog-runtime.log')),'child log must exist for startup diagnostics');
 fs.rmSync(root,{recursive:true,force:true});
 console.log('PASS watchdog recovery: stale state fails closed; PowerShell -File executed and exit logged; disarmed fixture safe');
}
main().catch(e=>{console.error(e);process.exitCode=1;fs.rmSync(root,{recursive:true,force:true})});
