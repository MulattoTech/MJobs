'use strict';
const assert=require('node:assert/strict'),{preflight}=require('./preflight');
const fixture=()=>({quota:{fresh:true,selectedRemainingPercent:90,ordinaryUsageAllowed:true,config:{enabled:true,autoKillArmed:true,tripped:false,thresholdPercent:7}},master:{emergency:{latched:false,remainingTotal:0},drain:{enabled:false},restore:{active:false}},snapshot:{offline:false}});
assert.equal(preflight(fixture()).ready,true);
for(const mutate of [
 x=>x.quota.fresh=false,x=>x.quota.selectedRemainingPercent=15,
 x=>x.quota.config.enabled=false,x=>x.quota.config.autoKillArmed=false,
 x=>x.quota.config.tripped=true,x=>x.quota.ordinaryUsageAllowed=false,
 x=>x.master.emergency.latched=true,x=>x.master.emergency.remainingTotal=1,
 x=>x.master.drain.enabled=true,x=>x.master.restore.active=true,
 x=>x.snapshot.offline=true
]){
 const x=fixture();mutate(x);const result=preflight(x);
 assert.equal(result.ready,false);assert(result.issues.length>0);
 assert.equal(result.canSaveAgentPlans,true,'Configuration may remain staged while execution is blocked');
}
console.log('PASS preflight: fresh healthy state, quota/guard/usage blocks, emergency, drain, restore and offline gates');
