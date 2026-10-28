'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {minimumRemaining,policyStatus}=require('./quota-policy');
const {QuotaControl}=require('./quota-control');const {configRevision}=require('./quota-guard');const {StartupController}=require('./startup-control');
function fixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-quota-policy-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));const q=new QuotaControl({root,autoSchedule:false});
 const stamp=()=>new Date().toISOString();fs.writeFileSync(q.configPath,JSON.stringify({enabled:true,pollSeconds:5,thresholdPercent:7,window:'any',autoKillArmed:true,tripped:false,updatedAt:stamp()}));
 const sample=(remaining=13)=>{const at=stamp();fs.writeFileSync(q.statusPath,JSON.stringify({status:'fresh',heartbeatAt:at,lastSuccessfulPoll:at,guardAck:{revision:configRevision(q.config()),appliedAt:at},windows:[{label:'Weekly',remainingPercent:remaining,resetsAt:new Date(Date.now()+86400000).toISOString()}]}));};sample();return {root,q,sample};}
test('legacy 15% remains the default; configured boundary is independent of the 7% emergency stop',()=>{
 assert.equal(minimumRemaining({thresholdPercent:7}),15);assert.equal(minimumRemaining({thresholdPercent:7,minimumRemainingPercent:12}),12);assert.equal(minimumRemaining({thresholdPercent:7,minimumRemainingPercent:7}),7);
 for(const x of [0,6,100,NaN,'12',null])assert.equal(minimumRemaining({thresholdPercent:7,minimumRemainingPercent:x}),100);
 assert.equal(policyStatus({thresholdPercent:7,minimumRemainingPercent:12},{fresh:true,selectedRemainingPercent:12}).blocked,true);
 assert.equal(policyStatus({thresholdPercent:7,minimumRemainingPercent:12},{fresh:true,selectedRemainingPercent:13}).blocked,false);
 assert.equal(policyStatus({thresholdPercent:7,minimumRemainingPercent:7},{fresh:false,selectedRemainingPercent:99}).blocked,true);
});
test('saved limit changes dispatch, survives recreation, and never disarms emergency protection',t=>{
 const f=fixture(t);assert.match(f.q.dispatchBlockReason(),/15%/);const before=f.q.config();f.q.update({minimumRemainingPercent:12});f.sample();
 assert.equal(f.q.config().thresholdPercent,7);assert.equal(f.q.config().autoKillArmed,true);assert.equal(f.q.dispatchBlockReason(),null);
 const again=new QuotaControl({root:f.root,autoSchedule:false});assert.equal(again.status().policy.minimumRemainingPercent,12);assert.equal(again.dispatchBlockReason(),null);
 f.sample(12);assert.match(again.dispatchBlockReason(),/paused at 12%/);
 for(const value of [6,0,100,12.5,'12',null])assert.throws(()=>again.update({minimumRemainingPercent:value}),/limit/);
 assert.equal(again.config().minimumRemainingPercent,12);assert.equal(before.thresholdPercent,again.config().thresholdPercent);
});
function startupFixture(t,minimum,remaining){const f=fixture(t);const actions=[];const deps={safety:async()=>({quota:{fresh:true,remainingPercent:remaining,minimumRemainingPercent:minimum,armed:true,tripped:false,thresholdPercent:7,pollSeconds:5,window:'any',monitorEnabled:true},emergency:{latched:false},dispatchPaused:true}),bootstrap:async()=>({ok:true,targetCount:1,alreadyOnline:true}),holdDispatch:async()=>actions.push('hold'),unlock:async()=>actions.push('unlock'),watchdogDisarmed:async()=>true,arm:async()=>({armed:true,token:'fixture'}),guardAck:async()=>true,readiness:async()=>({ready:true}),restore:{start:async()=>{throw Error('must not restore');},status:async()=>({}),pause:async()=>{}}};return {...f,actions,startup:new StartupController({root:f.root,deps,pollMs:1})};}
test('Startup at 13% accepts an explicit 12% limit, but 13/15% limits block before mutations',async t=>{
 for(const min of [7,12,13,15])await t.test('minimum '+min,async t=>{const f=startupFixture(t,min,13);f.startup.start({confirm:'STARTUP',requestId:'quota-test-'+min,openUi:false});const result=await f.startup.waitForIdle();assert.equal(result.phase,min<13?'ready_paused':'failed');if(min>=13){assert.equal(result.error.code,'QUOTA_LOW');assert.deepEqual(f.actions,[]);}else assert.equal(result.policy.brakePercent,min);});
});
test('invalid saved Startup policy fails closed without changing any runtime',async t=>{const f=startupFixture(t,6,13);f.startup.start({confirm:'STARTUP',requestId:'invalid-quota-policy'});const result=await f.startup.waitForIdle();assert.equal(result.error.code,'QUOTA_POLICY_INVALID');assert.deepEqual(f.actions,[]);});
