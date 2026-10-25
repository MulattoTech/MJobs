'use strict';
function preflight({quota,master,snapshot=null,providers=null,now=Date.now()}){
 const e=master.emergency||{},issues=[];
 const stamp=Date.parse(e.checkedAt||''),watchdogFresh=Number.isFinite(stamp)&&now-stamp>=0&&now-stamp<35000;
 if(e.latched)issues.push({code:'EMERGENCY_LOCKED',message:'Emergency lock is still engaged. Release it explicitly after verifying the allowance reset.'});
 if(quota.config.enabled===false)issues.push({code:'MONITOR_DISABLED',message:'Enable quota monitoring before starting work.'});
 if(quota.ordinaryUsageAllowed===false)issues.push({code:'USAGE_UNAVAILABLE',message:'Codex reports that ordinary usage is unavailable.'});
 if(!quota.fresh)issues.push({code:'QUOTA_STALE',message:'Wait for fresh Codex quota telemetry.'});
 else if(quota.selectedRemainingPercent<=quota.config.thresholdPercent+8)issues.push({code:'QUOTA_LOW',message:'Codex allowance must exceed '+(quota.config.thresholdPercent+8)+'% before a new sprint.'});
 if(!quota.config.autoKillArmed||quota.config.tripped)issues.push({code:'GUARD_UNARMED',message:'Explicitly arm the '+quota.config.thresholdPercent+'% emergency guard after the reset.'});
 if(e.latched&&!watchdogFresh)issues.push({code:'WATCHDOG_STALE',message:'Emergency watcher heartbeat is stale; do not resume work.'});
 if((e.remainingTotal||0)>0)issues.push({code:'RESIDUAL_PROCESSES',message:String(e.remainingTotal)+' target processes remain; resolve them before restoring.'});
 if(!snapshot||snapshot.offline)issues.push({code:'AO_OFFLINE',message:'Orchestrator is offline. Restore the preserved fleet only after quota checks pass.'});
 if(master.drain?.enabled)issues.push({code:'DRAINING',message:'Graceful wind-down is in progress.'});
 if(master.restore?.active)issues.push({code:'RESTORING',message:'Fleet restoration is in progress.'});
 return {at:new Date(now).toISOString(),ready:issues.length===0,issues,canSaveAgentPlans:true,
  quota:{fresh:quota.fresh,remaining:quota.selectedRemainingPercent,stopAt:quota.config.thresholdPercent,brakeAt:quota.config.thresholdPercent+8,armed:quota.config.autoKillArmed},
  emergency:{latched:!!e.latched,watchdogFresh,remaining:e.remainingTotal??null},
  dispatch:master.dispatch,rosterSource:snapshot?.offline?'saved snapshot':snapshot?'live':'unavailable',
  providers,notes:['Saving a worker plan does not start work.','Allowance reset never auto-releases the emergency stop or starts agents.','Provider-metering delay means an exact remaining percentage cannot be guaranteed.']};
}
module.exports={preflight};
