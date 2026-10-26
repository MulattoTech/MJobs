'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {Turbo}=require('./turbo');
const loc=path.join(__dirname,'test-allhands-state');
fs.rmSync(loc,{recursive:true,force:true});fs.mkdirSync(loc,{recursive:true});
let timestamp=Date.parse('2026-10-23T03:00:00Z');
const clock=()=>timestamp;
const idle=(id,role,prs=[],kind='worker')=>({
 id,name:id,model:'gpt-6-luna',role,kind,mode:'tui',busy:false,isIdle:true,activity:'idle',prs,
 openPRs:prs.length,blocked:prs.some(p=>p.ci==='failing'),replacedBy:null
});
const base={
 timestamp:new Date(timestamp).toISOString(),
 metrics:{ramFree:20*1024**3,cpu:15},
 sessions:[
 idle('mjobs-luna-34','Orchestration',[],'orchestrator'),
 idle('mjobs-luna-2','Fast implementation',[{number:772,state:'draft',ci:'failing'}]),
 idle('mjobs-luna-5','Fast implementation',[{number:761,state:'open',ci:'passing'}]),
 idle('mjobs-luna-17','Orchestration'),
 idle('mjobs-luna-31','Critical review'),
 idle('mjobs-luna-25','Deep implementation'),
 idle('mjobs-luna-9','Fast implementation')
 ],
 summary:{working:0,idle:7,idleFree:5}
};
const github={open:[
 {number:772,headRefOid:'b'.repeat(40),statusCheckRollup:[{conclusion:'FAILURE'}]},
 {number:761,headRefOid:'c'.repeat(40),statusCheckRollup:[{conclusion:'SUCCESS'}]}
],merged:[],at:new Date(timestamp).toISOString()};
base.sessions[0].activity='waiting_input';
base.sessions[6].activity='waiting_input';
base.sessions.push({...idle('mjobs-blocked','Fast implementation'),activity:'blocked'});
async function test(){
 const sent=[],logs=[];
 const turbo=new Turbo({root:loc,getState:async()=>base,command:async a=>{sent.push(a);return'ok'},
  audit:(e,d)=>logs.push({e,d}),clock,githubRunner:async()=>github,autoSchedule:false});
 // Fill the automatic hourly budget; explicit All Hands bypasses dashboard-only limit.
 turbo.session.actions=Array.from({length:18},(_,i)=>({at:new Date(timestamp-1000).toISOString(),id:'old-'+i,kind:'refill'}));
 turbo.save();
 assert.equal(turbo.hourlyCap(timestamp),true);
 let p=await turbo.previewAllHands();
 assert.equal(p.plan.length,7);
 assert.equal(p.working.length,0);
 assert.equal(p.resourceGuard,null);
 assert.equal(sent.length,0);
 assert.equal(p.plan.find(x=>x.id==='mjobs-luna-2').reason,'Repair CI on #772');
 assert.equal(p.plan.find(x=>x.id==='mjobs-luna-31').reason,'Independent PR #772 review');
 assert.equal(p.plan.find(x=>x.id==='mjobs-luna-5').reason,'Advance existing PR(s) #761');
 const result=await turbo.allHands();
 assert.equal(result.submitted,7);
 assert.equal(result.uncertain,0);
 assert.equal(result.skipped,0);
 assert.equal(sent.length,7);
 assert.ok(sent.some(args=>args.includes('mjobs-luna-34'))&&sent.some(args=>args.includes('mjobs-luna-9')),'All Hands includes instruction-waiting agents');
 assert.ok(!sent.some(args=>args.includes('mjobs-blocked')),'All Hands never answers a pending decision');
 assert.equal(new Set(sent.map(x=>x[2])).size,7);
 assert.ok(sent.every(a=>!a.includes('--steer')),'TUI messages must omit --steer');
 const again=await turbo.previewAllHands();
 assert.equal(again.plan.length,0);
 assert.equal(again.skipped.length,7);
 await assert.rejects(()=>turbo.allHands(),/already ran recently/);
 let reboot=new Turbo({root:loc,getState:async()=>base,command:async a=>sent.push(a),
  audit:()=>{},clock,githubRunner:async()=>github,autoSchedule:false});
 assert.ok(reboot.session.lastAllHands);
 assert.equal(reboot.summary().sentThisHour,25);
 // Already working agents must not be woken again.
 timestamp+=11*60*1000;
 base.sessions[0]={...base.sessions[0],isIdle:false,busy:true,activity:'active'};
 const fresh=await reboot.previewAllHands();
 assert.equal(fresh.plan.length,6);
 assert.equal(fresh.working.length,1);
 // Low memory is always a hard stop even for manual activation.
 base.metrics.ramFree=2*1024**3;
 await assert.rejects(()=>reboot.allHands(),/resource safety/);
 assert.equal(sent.length,7);
 base.metrics.ramFree=20*1024**3;
 const raceRoot=path.join(loc,'decision-race');fs.mkdirSync(raceRoot);
 const raceSent=[];let raceReads=0;
 const raceAgent={...idle('race-primary','Orchestration',[],'orchestrator'),activity:'waiting_input'};
 const race=new Turbo({root:raceRoot,autoSchedule:false,clock,githubRunner:async()=>github,
  getState:async()=>({...base,sessions:[{...raceAgent,activity:++raceReads===1?'waiting_input':'blocked'}]}),
  command:async args=>raceSent.push(args),audit:()=>{}});
 const raceResult=await race.allHands();
 assert.ok(raceReads>=2);assert.equal(raceResult.submitted,0);assert.equal(raceResult.skipped,1);
 assert.deepEqual(raceSent,[],'A fresh blocked activity prevents an All Hands send after selection');
 console.log('PASS All Hands: 7 unique recipients including instruction-waiting agents, decision-race rejection, budgets, cooldown, ownership, resource gates and persistence');
 fs.rmSync(loc,{recursive:true,force:true});
}
test().catch(e=>{console.error(e.stack);process.exitCode=1});
