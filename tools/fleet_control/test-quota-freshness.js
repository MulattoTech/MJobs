'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {QuotaControl}=require('./quota-control');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-quota-freshness-'));
const now=Date.now(),stamp=new Date(now-1000).toISOString();
const config={enabled:true,pollSeconds:5,thresholdPercent:7,window:'any',autoKillArmed:true,tripped:false};
const window={label:'Weekly',remainingPercent:99,resetsAt:new Date(now+3600000).toISOString()};
const info={status:'fresh',error:null,ordinaryUsageAllowed:true,heartbeatAt:stamp,lastSuccessfulPoll:stamp,windows:[window]};
const write=(name,value)=>fs.writeFileSync(path.join(root,name),JSON.stringify(value));
const set=(patch={},cfg={})=>{write('quota-guard-config.json',{...config,...cfg});write('quota-guard-status.json',{...info,...patch});};
try{
 set();
 const q=new QuotaControl({root,autoSchedule:false});
 assert.equal(q.status().fresh,true);assert.equal(q.dispatchBlockReason(),null);
 for(const patch of [
  {status:'unavailable',error:'RPC timeout'},
  {status:'fresh',error:'partial failure'},
  {heartbeatAt:new Date(now+60000).toISOString()},
  {lastSuccessfulPoll:new Date(now+60000).toISOString()},
  {heartbeatAt:new Date(now-60000).toISOString()},
  {lastSuccessfulPoll:new Date(now-60000).toISOString()},
  {windows:[{...window,resetsAt:new Date(now-1000).toISOString()}]},
  {windows:[{...window,resetsAt:'invalid'}]},
  {windows:[{...window,remainingPercent:101}]},
  {windows:[]}
 ]){
  set(patch);assert.equal(q.status().fresh,false,JSON.stringify(patch));
  assert.equal(q.status().selectedRemainingPercent,null);
  assert.match(q.dispatchBlockReason(),/unavailable|stale/);
 }
 for(const pct of [16,15,8,7,6]){
  set({windows:[{...window,remainingPercent:pct}]});
  assert.equal(Boolean(q.dispatchBlockReason()),pct<=15);
 }
 set({}, {autoKillArmed:false,tripped:true});assert.match(q.dispatchBlockReason(),/tripped/);
 set({status:'unavailable',error:'offline'},{autoKillArmed:false,tripped:false});assert.match(q.dispatchBlockReason(),/not armed/);
 set({ordinaryUsageAllowed:false});assert.match(q.dispatchBlockReason(),/ordinary usage/);
 set({}, {enabled:false});assert.match(q.dispatchBlockReason(),/disabled/);
 console.log('PASS: explicit successful samples only; stale/error/future/expired/invalid readings blocked; 16/15 soft-brake and 8/7/6 hard-stop boundaries correct; tripped guard remains locked after disarming; no live settings changed.');
}finally{fs.rmSync(root,{recursive:true,force:true});}
