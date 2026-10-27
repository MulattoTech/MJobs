'use strict';
// Workflow state is separate from native activity. Reserved capacity is never
// included in an executing count. All job-time predictions are conditional.
const parse=x=>Date.parse(x||'');
const validTime=(x,now)=>Number.isFinite(parse(x))&&parse(x)>=now-86400000;
function classify(session,controller={},now=Date.now()){
 const activity=typeof session.activity==='object'?session.activity.state:session.activity;
 const {config={},status={},latest,nextTick}=controller,agent=status.agents?.[session.id]||{};
 const check=validTime(nextTick,now)?new Date(Math.max(now,parse(nextTick))).toISOString():null;
 const common={observedAt:new Date(now).toISOString(),executing:false,queuePosition:null,estimatedStartAt:null,etaKind:null,nextEvaluationAt:check,assignedTask:typeof agent.lastReason==='string'?agent.lastReason.slice(0,180):null};
 const result=(state,label,reason,more={})=>({...common,state,label,reason,...more});
 if(session.isTerminated||session.replacedBy||activity==='exited'||activity==='stopped'||(session.mode==='chat'&&(session.controllerVerified!==true||!['ready','busy'].includes(session.controllerState))))return result('stopped','Stopped','No verified runnable agent controller',{nextEvaluationAt:null});
 if(['active','working'].includes(activity)||session.busy===true)return result('executing','Executing','A native agent turn is active',{executing:true,estimatedStartAt:null});
 if(activity==='blocked')return result('approval','Needs a decision','A tool or permission decision is pending; automatic instructions are withheld',{nextEvaluationAt:null});
 if(!['idle','waiting_input'].includes(activity))return result('unknown','State unavailable','No current runnable activity observation',{nextEvaluationAt:null});
 if(config.enabled!==true)return result('paused','Dispatch paused','Automatic assignments are paused; this does not mark the agent as busy',{nextEvaluationAt:null});
 const blocked=latest?.blocked;
 if(blocked){
  if(/primary/i.test(blocked))return result('primary_wait','Waiting for primary','The primary orchestrator is not ready');
  if(/RAM|disk|CPU|resource/i.test(blocked))return result('resource_wait','Resource-limited','Assignments are held for workstation headroom');
  if(/quota|usage|rate.limit/i.test(blocked))return result('quota_wait','Quota protection','Allowance or rate-limit protection is holding assignments');
  return result('control_wait','Dispatch held','The dispatcher reports a prerequisite or control-operation hold');
 }
 if(!latest||!validTime(latest.at,now)||parse(latest.at)>now+1000||now-parse(latest.at)>Math.max(90000,(config.intervalSeconds||60)*3000))return result('unknown','Awaiting dispatcher check','A fresh task plan is not available');
 const deliveryHold=latest.held?.find(x=>x.id===session.id&&['delivery_cooldown','delivery_unverified'].includes(x.kind));
 if(deliveryHold)return result(deliveryHold.kind,deliveryHold.kind==='delivery_cooldown'?'Dispatch-ready soon':'Verifying delivery',deliveryHold.reason,{nextEvaluationAt:deliveryHold.readyAt||check,estimatedStartAt:null});
 if(agent.awaiting===true)return result('dispatching','Assignment submitted','Delivery was submitted; a new active turn is not yet confirmed');
 const queued=latest.proposed?.find(x=>x.id===session.id);
 if(queued){const seconds=queued.etaSeconds,at=Number.isFinite(seconds)&&seconds>=0?parse(latest.at)+seconds*1000:null;
  return result('queued','Queued for work','A concrete non-overlapping task is planned; timing assumes unchanged capacity',{queuePosition:Number.isInteger(queued.queuePosition)?queued.queuePosition:null,estimatedStartAt:at===null?null:new Date(Math.max(now,at)).toISOString(),etaKind:at===null?null:'conditional_estimate',assignedTask:String(queued.reason||'Planned task').slice(0,180)});
 }
 if(latest.reserveIds?.includes(session.id))return result('reserve','On-call reserve','Explicitly reserved for incoming review or control work; not executing');
 if(validTime(agent.backoffUntil,now)&&parse(agent.backoffUntil)>now)return result('cooldown','Delivery backoff','A prior delivery requires backoff or verification',{nextEvaluationAt:new Date(parse(agent.backoffUntil)).toISOString()});
 const quietAt=Math.max(parse(session.lastActivity||session.activity?.lastActivityAt)||0,parse(agent.lastSent)||0)+(config.minQuietSeconds||60)*1000;
 if(quietAt>now)return result('settling','Turn settling','The completed turn is settling before another assignment',{nextEvaluationAt:new Date(quietAt).toISOString()});
 const task=(controller.productiveTasks||controller.productivity?.summary?.().tasks)?.find(t=>t.agentId===session.id&&['reserved','submitted','uncertain'].includes(t.state));
 if(task)return result(task.state==='uncertain'?'delivery_review':'receipt_wait',task.state==='uncertain'?'Delivery needs review':'Awaiting task receipt',task.state==='uncertain'?'The prior send outcome is uncertain; it will not be blindly replayed.':'A maintenance draft was assigned; its result receipt has not yet been verified.',{assignedTask:'Maintainer reference: '+task.sourcePath,estimatedStartAt:null});
 const held=latest.held?.find(x=>x.id===session.id);
 if(held&&/checks are still running/i.test(held.reason||''))return result('ci_wait','Waiting for CI','Existing CI is running; unrelated maintenance can be assigned when eligible');
 if(held)return result('review_wait','Waiting for a change','The current PR outcome was already handled; no repeat audit is scheduled');
 return result('available','Awaiting useful work',config.productiveWork?'No eligible fresh task is currently queued; the next discovery check is shown':'No work is queued; productive discovery is disabled');
}
module.exports={classify};
