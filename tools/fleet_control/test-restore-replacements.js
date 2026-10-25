'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {RestoreFleet,PHASES}=require('./restore-fleet');
(async()=>{
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-restore-replacements-'));
 try{
  const rows=[{id:'old-worker',projectId:'mjobs-luna',kind:'worker',harness:'codex',activity:{state:'idle'},isTerminated:false},{id:'new-worker',projectId:'mjobs-luna',kind:'worker',harness:'agy',activity:{state:'idle'},isTerminated:false}];
  const restore=new RestoreFleet({root,autoSchedule:false,command:async()=>{throw Error('No commands expected')},audit:()=>{},getGlobalSessions:async()=>rows,resourceReader:async()=>({ramGiB:20,diskGiB:90,cpuPercent:3}),runtimeInspector:async()=>new Map(rows.map(r=>[r.id,1]))});
  await restore.captureSnapshot();
  fs.writeFileSync(path.join(root,'replacement-state.json'),JSON.stringify({pairs:[{oldId:'old-worker',newId:'new-worker'}]}));
  const preview=await restore.preview();
  assert.equal(preview.totalTargets,1);assert.equal(preview.working[0].id,'new-worker');
  assert.ok(PHASES.includes('paused_quota'));assert.ok(PHASES.includes('paused'));
  restore.state.phase='paused_quota';restore.state.desiredIds=['new-worker'];
  assert.equal(await restore.captureSnapshot(),null,'Quota-paused recovery must not overwrite its manifest');
  fs.writeFileSync(path.join(root,'replacement-state.json'),'{invalid');
  await assert.rejects(()=>restore.preview(),/ledger unreadable/);
  console.log('PASS restore excludes superseded workers, freezes quota-paused manifest, and fails closed on corrupt replacement records');
 }finally{fs.rmSync(root,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1});
