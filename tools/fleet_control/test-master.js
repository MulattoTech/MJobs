'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {MasterControls}=require('./master-controls');
const root=path.join(__dirname,'test-master-state');
fs.rmSync(root,{recursive:true,force:true});fs.mkdirSync(root,{recursive:true});
const calls=[];
const flags={sustain:true,turbo:true};
const sustain={config:{enabled:true},enable:x=>{sustain.config.enabled=x;flags.sustain=x}};
const turbo={config:{enabled:true},enable:x=>{turbo.config.enabled=x;flags.turbo=x}};
const clock=()=>new Date().toISOString();
const sessions=[
 {id:'mjobs-luna-35',kind:'orchestrator',isIdle:false,busy:true,activity:'active',lastActivity:clock(),isTerminated:false},
 {id:'mjobs-luna-2',kind:'worker',isIdle:true,busy:false,activity:'idle',lastActivity:clock(),isTerminated:false},
 {id:'mjobs-luna-3',kind:'worker',isIdle:true,busy:false,activity:'idle',lastActivity:clock(),isTerminated:false}
];
const source=async()=>({sessions});
const audit=[];
const m=new MasterControls({root,sustain,turbo,command:async args=>{calls.push(args);if(args[0]==='send')return JSON.stringify({ok:true});const id=args[2];const s=sessions.find(x=>x.id===id);if(s){s.activity='exited';s.busy=false;s.isIdle=false}return JSON.stringify({ok:true})},
 audit:(event,data)=>audit.push({event,data}),getState:source,autoschedule:false});
async function test(){
 assert.equal(m.status().emergency.latched,false);
 assert.equal(m.status().drain.enabled,false);
 assert.equal(flags.sustain,true);
 const p=m.pause();
 assert.equal(flags.sustain,false);
 assert.equal(flags.turbo,false);
 assert.equal(p.dispatch.sustainedEnabled,false);
 const response=await m.drain();
 assert.equal(response.drain.enabled,true);
 await new Promise(r=>setTimeout(r,80));
 assert.equal(calls.length,3);
 assert.equal(calls[0][0],'send');
 assert.ok(calls.slice(1).every(x=>x[0]==='session'&&x[1]==='exit-agent'));
 assert.ok(m.status().drain.exitedCount===2);
 assert.equal(m.status().drain.enabled,true);
 sessions[0].busy=false;sessions[0].isIdle=true;sessions[0].activity='idle';
 await m.drainTick();
 assert.equal(m.status().drain.enabled,true,'Completion requires a fresh snapshot');
 await m.drainTick();
 assert.equal(m.status().drain.enabled,false);
 assert.equal(m.status().drain.phase,'completed');
 // Fake the watchdog so this test NEVER launches real process-kill commands.
 let fakeLaunches=0;
 m.ensureWatchdog=()=>{fakeLaunches++;return true};
 const first=await m.emergency();
 assert.equal(first.emergency.latched,true);
 assert.equal(fs.existsSync(path.join(root,'emergency-latch.json')),true);
 assert.equal(fakeLaunches,1);
 assert.equal(flags.sustain,false);
 assert.throws(()=>m.pause(),/Emergency lockout/);
 await assert.rejects(()=>m.drain(),/Emergency lockout/);
 const again=await m.emergency();
 assert.equal(again.emergency.latched,true);
 assert.equal(fakeLaunches,2);
 const restart=new MasterControls({root,sustain,turbo,command:async()=>{},audit:()=>{},getState:source,autoschedule:false});
 assert.equal(restart.status().emergency.latched,true);
 const reset=m.rearm();
 assert.equal(reset.emergency.latched,false);
 assert.equal(flags.sustain,false,'Rearm must never restart dispatch automatically');
 assert.equal(flags.turbo,false,'Rearm must never restart turbo automatically');
 assert.equal(m.status().drain.enabled,false);
 fs.rmSync(root,{recursive:true,force:true});
 console.log('PASS master controls: pause, asynchronous graceful drain, persisted lockout, emergency without real execution, explicit rearm, no automatic restart');
}
test().catch(e=>{console.error(e.stack);process.exitCode=1;fs.rmSync(root,{recursive:true,force:true})});
