'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {retainStoppedTargets}=require('./snapshot-retention');
const {RestoreFleet}=require('./restore-fleet');
const {StartupAdapter}=require('./startup-adapter');
const at='2026-10-26T10:00:00.000Z';
const rootRow={id:'mjobs-primary',projectId:'mjobs',kind:'orchestrator',harness:'codex',model:'model-a',mode:'tui',branch:'ao/root',createdAt:'2026-10-20T10:00:00Z',isTerminated:false,activity:'idle',observedAt:at};
const worker={...rootRow,id:'mjobs-worker',kind:'worker',branch:'ao/worker'};
function fixture(t,entries=[rootRow,worker]){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-snapshot-continuity-'));t.after(()=>fs.rmSync(root,{force:true,recursive:true}));
 let rows=structuredClone(entries),blocked=false,frozen=false,gate=null;
 fs.writeFileSync(path.join(root,'restore-snapshot.json'),JSON.stringify({at,sessions:entries,restorableCount:entries.length}));
 const restore=new RestoreFleet({root,autoSchedule:false,locked:()=>blocked,snapshotFrozen:()=>frozen,audit:()=>{},command:async()=>{throw Error('No native commands allowed');},getGlobalSessions:async()=>{if(gate)await gate;return rows.map(s=>({...s,activity:{state:s.activity}}));}});
 return {root,restore,setRows:x=>{rows=x;},setLocked:x=>{blocked=x;},setFrozen:x=>{frozen=x;},setGate:x=>{gate=x;}};
}
test('stopped exact target stays recoverable without claiming its historical observation is live',()=>{
 const live={...rootRow,activity:'exited',observedAt:'2026-10-26T11:00:00Z'};
 const result=retainStoppedTargets([rootRow],[live],live.observedAt)[0];
 assert.equal(result.activity,'idle');assert.equal(result.observedAt,at);assert.equal(result.recoveryRetention.lastObservedActivity,'exited');
 assert.equal(result.recoveryRetention.lastObservedAt,live.observedAt);assert.equal(live.activity,'exited');
 assert.deepEqual(retainStoppedTargets([result],[live],live.observedAt)[0],result);
});
test('archived, originally stopped, replaced-generation or changed-identity rows are never adopted',async t=>{
 const changes={archived:{isTerminated:true},model:{model:'other'},branch:{branch:'other'},creation:{createdAt:'2026-10-21T00:00:00Z'},project:{projectId:'other'},mode:{mode:'chat'},harness:{harness:'agy'},kind:{kind:'worker'}};
 for(const [name,fields]of Object.entries(changes))await t.test(name,()=>{const live={...rootRow,activity:'exited',...fields};assert.equal(retainStoppedTargets([rootRow],[live],at)[0],live);});
 const live={...rootRow,activity:'exited'};
 assert.equal(retainStoppedTargets([{...rootRow,activity:'exited'}],[live],at)[0],live);
 assert.equal(retainStoppedTargets([{...rootRow,createdAt:undefined}],[live],at)[0],live);
 assert.throws(()=>retainStoppedTargets([rootRow],[live,live],at),/Ambiguous/);
});
test('real periodic capture retains stopped primary and workers through restart and all-stopped state',async t=>{
 const f=fixture(t);f.setRows([{...rootRow,activity:'exited'},worker]);
 assert.equal((await f.restore.captureSnapshot()).restorableCount,2);
 const saved=f.restore.getSnapshot();assert.equal(saved.sessions[0].recoveryRetention.lastObservedActivity,'exited');
 const replacement=new RestoreFleet({root:f.root,autoSchedule:false,audit:()=>{},getGlobalSessions:async()=>[rootRow,worker].map(s=>({...s,activity:{state:'exited'}}))});
 assert.equal((await replacement.captureSnapshot()).restorableCount,2);
 assert.equal(replacement.targets(replacement.getSnapshot()).length,2);
});
test('an originally exited unrelated project is not enrolled by capture',async t=>{
 const f=fixture(t);f.setRows([rootRow,worker,{...rootRow,id:'other-root',projectId:'other',activity:'exited'}]);
 assert.equal((await f.restore.captureSnapshot()).restorableCount,2);
 assert.deepEqual(f.restore.targets(f.restore.getSnapshot()).map(s=>s.id),[rootRow.id,worker.id]);
});
test('a root identity mismatch cannot overwrite a good roster with orphaned workers',async t=>{
 const f=fixture(t),before=fs.readFileSync(f.restore.snapshotPath);f.setRows([{...rootRow,activity:'exited',branch:'unexpected'},worker]);
 assert.equal(await f.restore.captureSnapshot(),null);assert.deepEqual(fs.readFileSync(f.restore.snapshotPath),before);
});
test('stop, freeze, new restoration and another snapshot win over a pending capture',async t=>{
 for(const mode of ['stop','freeze','restore','ledger','snapshot'])await t.test(mode,async t=>{
  const f=fixture(t);let release;f.setGate(new Promise(ok=>{release=ok;}));const before=fs.readFileSync(f.restore.snapshotPath);const work=f.restore.captureSnapshot();
  if(mode==='stop')f.setLocked(true);if(mode==='freeze')f.setFrozen(true);if(mode==='restore')f.restore.state.active=true;
  if(mode==='ledger')f.restore.state.phase='complete';
  if(mode==='snapshot')fs.writeFileSync(f.restore.snapshotPath,JSON.stringify({at,sessions:[rootRow],restorableCount:1}));
  const expected=mode==='snapshot'?fs.readFileSync(f.restore.snapshotPath):before;release();assert.equal(await work,null);assert.deepEqual(fs.readFileSync(f.restore.snapshotPath),expected);
 });
});
test('invalid manifest is not misreported as a dead daemon',async()=>{
 const a=new StartupAdapter({root:'.',master:{restore:{getSnapshot:()=>({sessions:[worker]}),targets:x=>x.sessions}},quota:{},launcher:{},providers:{},verifyCodex:async()=>{},getSessions:async()=>{throw Error('must not query');},runtimeHealth:()=>false});
 const result=await a.readRuntimes();assert.equal(result.errorCode,'RECOVERY_MANIFEST_INVALID');assert.equal(result.daemonOnline,null);assert.equal(result.runtimesReady,false);
});
