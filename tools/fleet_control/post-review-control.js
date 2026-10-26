'use strict';

const fs=require('node:fs');
const {digest}=require('./branch-adoptions');

// The HTTP owner holds its existing mutation lane while this runs. This path
// observes completed native work; it cannot submit a restore, prompt or launch.
async function completeReviewedRestore(data,{master,startup,quota,launcher,adapter,sustain,turbo,findWork}){
 const original=startup.status(),guardReceipt={token:quota.status().guardAck?.revision};
 const bytes=fs.readFileSync(startup.file),durable=JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/,'')),fingerprint=digest(bytes);
 if(digest(durable)!==digest(original))throw Error('Durable Startup state differs from the current owner');
 const assertSafe=()=>{
  const current=startup.status(),launch=launcher.status();
  const completed=launch.request&&launch.result&&launch.request.nonce===launch.result.nonce&&launch.request.mode===launch.result.mode&&
   ['ok','error','rejected'].includes(launch.result.status)&&!launch.requiresReconciliation;
  if(data.confirm!=='COMPLETE REVIEWED RESTORE'||!data.runId||data.runId!==original.runId||
   current.runId!==original.runId||current.revision!==original.revision||digest(fs.readFileSync(startup.file))!==fingerprint||data.runId!==master.restore.state.source?.startupRunId||
   current.active||startup.task||startup.reconcileTask||startup.pendingMutations?.size||master.restore.state.active||master.restore.running||
   sustain.running||turbo.running||findWork?.running||master.drainState.enabled||master.draining||
   sustain.config.enabled||turbo.config.enabled||launch.requiresReconciliation||launch.pending&&!completed)
   throw Error('Reviewed completion requires the same paused and settled Startup owner');
  adapter.safeForMutation({guardReceipt},{requireGuard:true});
 };
 assertSafe();
 return master.restore.completeReviewed(data,{assertSafe});
}

module.exports={completeReviewedRestore};
