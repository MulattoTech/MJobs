'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {RestoreFleet}=require('./restore-fleet');
const root=path.join(__dirname,'restore-test-state');
fs.rmSync(root,{recursive:true,force:true});fs.mkdirSync(root,{recursive:true});
let clock=Date.now(),launches=0,commands=[],doneCalled=0,offline=false,latched=false;
const records=[
 {id:'mjobs-luna-35',projectId:'mjobs-luna',kind:'orchestrator',harness:'codex',displayName:'Sol primary',model:'gpt-5.6-sol',mode:'chat',activity:{state:'active'},isTerminated:false},
 {id:'mjobs-luna-5',projectId:'mjobs-luna',kind:'worker',harness:'codex',displayName:'Luna worker',model:'gpt-6-luna',mode:'tui',activity:{state:'active'},isTerminated:false},
 {id:'mjobs-luna-6',projectId:'mjobs-luna',kind:'worker',harness:'codex',displayName:'Luna worker 2',model:'gpt-6-luna',mode:'tui',activity:{state:'exited'},isTerminated:false},
 {id:'mterm-cli-1',projectId:'mterm-cli',kind:'orchestrator',harness:'codex',displayName:'Other primary',model:'gpt-6-luna',mode:'chat',activity:{state:'active'},isTerminated:false}
];
const alive=new Map(records.map(s=>[s.id,s.activity.state==='active'?6:0]));
const logs=[];
const getGlobalSessions=async()=>{if(offline)throw Error('daemon unavailable');return records.map(x=>({...x,activity:{...x.activity}}))};
const restore=new RestoreFleet({root,autoSchedule:false,
 getGlobalSessions,now:()=>clock,locked:()=>latched,
 onComplete:async()=>{doneCalled++},
 launchAO:async()=>{launches++;offline=false},
 resourceReader:async()=>({ramGiB:16,diskGiB:150,cpuPercent:24}),
 runtimeInspector:async()=>new Map(alive),
 command:async args=>{
  commands.push(args);
  const type=args[1],id=args[2];
  const target=records.find(x=>x.id===id);
  if(type==='resume-agent'||type==='restore'){
   target.isTerminated=false;target.activity.state='idle';alive.set(id,5);
  }else if(type==='exit-agent'){
   target.activity.state='exited';alive.set(id,0);
  }else throw Error('unexpected AO command '+args.join(' '));
  return '{}';
 },
 audit:(e,data)=>logs.push({e,data})
});
async function main(){
 const snap=await restore.captureSnapshot();
 assert.equal(snap.restorableCount,3,'Exclude exited session from pre-stop snapshot');
 assert.ok(restore.getSnapshot().sessions.length===4);
 let p=await restore.preview();
 assert.equal(p.totalTargets,3);
 assert.equal(p.alreadyOnline,3);
 assert.equal(p.willResume,0);
 assert.equal(restore.status().active,false);
 // Simulate emergency kill: runtime process count zero although AO DB remembers active.
 for(const id of alive.keys())alive.set(id,0);
 p=await restore.preview();
 assert.equal(p.willResume,3,'Stale AO activity status must not be trusted if no OS processes');
 assert.ok(p.working.some(x=>x.action==='restart-runtime'));
 const state=restore.start({autoDispatchAfter:true});
 assert.equal(state.active,true);
 assert.equal(state.total,3);
 // AO temporarily offline after kill; must start only once (with cooldown).
 offline=true;
 const t0=await restore.tick();
 assert.equal(t0.waiting,'Starting Orchestrator.inc desktop app');
 assert.equal(launches,1);
 // Start primary roots first, one current no process => native exit+resume.
 const t1=await restore.tick();
 assert.equal(t1.phase,'restoring_primary');
 assert.equal(commands.length,4,'Two roots, exit plus resume per stale session');
 assert.deepEqual(commands.map(x=>x[2]),['mjobs-luna-35','mjobs-luna-35','mterm-cli-1','mterm-cli-1']);
 assert.ok(commands.every(x=>x[0]==='session'));
 const t2=await restore.tick();
 assert.ok(t2.submitted.includes('mjobs-luna-5'));
 assert.equal(commands.length,6);
 const t3=await restore.tick();
 assert.equal(t3.complete,true);
 assert.equal(restore.status().phase,'complete');
 assert.equal(doneCalled,1);
 assert.equal(restore.status().restoredCount,3);
 assert.equal(restore.status().active,false);
 assert.equal(records.find(x=>x.id==='mjobs-luna-6').activity.state,'exited','Originally exited worker stays exited');
 const state2=new RestoreFleet({root,autoSchedule:false,locked:()=>latched,getGlobalSessions,runtimeInspector:async()=>new Map(alive),
  command:async()=>{throw Error('should not spawn on restart')},audit:()=>{},resourceReader:async()=>({ramGiB:12,diskGiB:90,cpuPercent:10})});
 assert.equal(state2.status().phase,'complete');
 // Latch prevents restarting until explicit rearm.
 latched=true;
 assert.throws(()=>state2.start(),/emergency/i);
 const restorePrev=await state2.preview();
 assert.equal(restorePrev.emergencyLatched,true);
 restore.pause();
 fs.rmSync(root,{recursive:true,force:true});
 console.log('PASS restore controller: active snapshot, exclude exited agents, emergency stale-status recovery, root-first start, original IDs, no duplicate worktrees, dispatch-after-health and persisted state');
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;fs.rmSync(root,{recursive:true,force:true})});
