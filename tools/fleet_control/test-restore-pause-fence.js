'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {RestoreFleet}=require('./restore-fleet');
function deferred(){let resolve;return {promise:new Promise(ok=>{resolve=ok;}),resolve:value=>resolve(value)};}
function fixture(t,stage){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'restore-pause-fence-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const target={id:'mjobs-primary',projectId:'mjobs',kind:'orchestrator',harness:'codex',mode:'tui',model:'fixture',branch:'ao/fixture',createdAt:'2026-10-20T00:00:00Z'};
 const live={...target,isTerminated:false,activity:{state:'exited'},provisionState:'ready',statusReadiness:'ready'};
 const entered=deferred(),release=deferred(),commands=[],launches=[];
 async function checkpoint(name,value){if(stage===name||stage===name+'-error'){entered.resolve();await release.promise;if(stage.endsWith('-error'))throw Error('Delayed read failed');}return value;}
 const restore=new RestoreFleet({root,autoSchedule:false,audit:()=>{},command:async args=>{commands.push(args);},launchAO:async()=>launches.push('launch'),
  getGlobalSessions:()=>checkpoint('sessions',[live]),runtimeInspector:()=>checkpoint('inventory',new Map([[target.id,0]])),resourceReader:()=>checkpoint('resources',{ramGiB:20,diskGiB:100,cpuPercent:10})});
 restore.watchdogDisarmed=()=>checkpoint('watchdog',true);
 restore.runtimeTarget=()=>checkpoint('identity',{matches:true,adoption:null});
 restore.state={...restore.state,active:true,phase:'preparing',startedAt:new Date().toISOString(),source:{targets:[target]},desiredIds:[target.id]};restore.save();
 return {restore,entered,release,commands,launches};
}
for(const stage of ['watchdog','sessions','sessions-error','inventory','inventory-error','identity','resources']){
 test('pause wins over delayed '+stage+' read without stranding reconciliation',async t=>{
  const f=fixture(t,stage),task=f.restore.tick();await f.entered.promise;f.restore.pause();
  const snapshot=structuredClone(f.restore.state),disk=fs.readFileSync(f.restore.statePath);
  f.release.resolve();await task;
  assert.deepEqual(f.restore.state,snapshot,'Late poll may not change paused in-memory state');
  assert.deepEqual(fs.readFileSync(f.restore.statePath),disk,'Late poll may not overwrite paused durable state');
  assert.equal(f.restore.state.phase,'paused');assert.equal(f.restore.running,false);
  assert.equal(f.commands.length,0);assert.equal(f.launches.length,0);
 });
}
