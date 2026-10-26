'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {Turbo}=require('./turbo');
const temp=path.join(__dirname,'test-turbo-state');
fs.rmSync(temp,{recursive:true,force:true});
fs.mkdirSync(temp,{recursive:true});
const t0=Date.parse('2026-10-23T02:00:00Z');
let now=t0;
let actions=[];
const idle=(id,role,prs=[],model='gpt-6-luna')=>({
 id,name:id,kind:role==='primary'?'orchestrator':'worker',role:role==='primary'?'Orchestration':role,model,status:'idle',activity:'idle',busy:false,isIdle:true,openPRs:prs.length,blocked:prs.some(p=>p.ci==='failing'),prs
});
const snapshot={
 timestamp:new Date(t0).toISOString(),
 sessions:[
  idle('mjobs-luna-34','primary'),
  idle('mjobs-luna-21','Complex implementation',[{number:779,ci:'failing',state:'draft',url:'https://github.com/MulattoTech/MJobs/pull/779'}]),
  idle('mjobs-luna-31','Critical review'),
  idle('mjobs-luna-7','Fast implementation',[{number:761,ci:'passing',state:'open',url:'https://github.com/MulattoTech/MJobs/pull/761'}]),
  idle('mjobs-luna-9','Fast implementation'),
  idle('mjobs-luna-17','Orchestration')],
 metrics:{ramFree:17*1024**3,cpu:20},
 summary:{working:0,idle:6,idleFree:4}
};
const github={
 at:new Date(t0).toISOString(),
 open:[
  {number:779,headRefOid:'a'.repeat(40),reviewDecision:'',statusCheckRollup:[{conclusion:'FAILURE',status:'COMPLETED'}]},
  {number:761,headRefOid:'b'.repeat(40),reviewDecision:'',statusCheckRollup:[{conclusion:'SUCCESS',status:'COMPLETED'}]}
 ],
 merged:[{number:100,mergedAt:new Date(t0-100000).toISOString()}]
};
snapshot.sessions[0].activity='waiting_input';
snapshot.sessions.push({...idle('mjobs-blocked','Critical review'),activity:'blocked'});
const run=async()=>{
 const control=new Turbo({root:temp,getState:async()=>snapshot,command:async args=>{actions.push(args);return 'ok'},audit:()=>{},isManualBusy:()=>false,clock:()=>now,githubRunner:async()=>github,autoSchedule:false});
 assert.equal(control.configuration().enabled,false);
 let prev=await control.preview();
 assert.equal(prev.enabled,false);
 assert.equal(actions.length,0,'Preview must not send anything');
 assert.equal(prev.preview.nextActions[0].id,'mjobs-luna-34');
 assert.ok(prev.preview.counts.failedPRs===1);
 control.enable(true);
 const first=await control.cycle();
 assert.equal(first.executed.length,3,'Three dispatches per cycle');
 assert.deepEqual(first.executed.map(x=>x.id),['mjobs-luna-34','mjobs-luna-21','mjobs-luna-31']);
 assert.ok(actions.some(args=>args.includes('mjobs-luna-34')),'A primary at its instruction prompt can coordinate work');
 assert.ok(!actions.some(args=>args.includes('mjobs-blocked')),'A pending decision is never an eligible recipient despite isIdle');
 assert.equal(control.summary().sentThisHour,3);
 const duplicate=await control.cycle();
 assert.equal(duplicate.executed.length,2,'Remaining agents get assignments, originals are on cooldown');
 assert.equal(new Set(actions.map(a=>a[2])).size,5,'Agents receive distinct messages before cooldown ends');
 now+=60000;
 const cooled=await control.cycle();
 assert.equal(cooled.executed.length,0,'Cooldown prevents repeated nudges');
 snapshot.metrics.ramFree=5*1024**3; now+=20*60000;
 const pressured=await control.cycle();
 assert.match(pressured.reason,/RAM/);
 assert.equal(pressured.executed.length,0);
 snapshot.metrics.ramFree=17*1024**3;
 control.update({maxActionsPerCycle:2,pollingSeconds:90});
 assert.equal(control.configuration().pollingSeconds,90);
 assert.throws(()=>control.update({maxActionsPerCycle:30}),/between/);
 assert.throws(()=>control.update({enabled:true}),/Unrecognized/);
 control.enable(false);
 let off=await control.cycle();assert.equal(off.skipped,'disabled');
 const reboot=new Turbo({root:temp,getState:async()=>snapshot,command:async args=>{actions.push(args)},audit:()=>{},clock:()=>now,githubRunner:async()=>github,autoSchedule:false});
 assert.equal(reboot.configuration().enabled,false,'Toggle persists across restart');
 assert.equal(reboot.configuration().pollingSeconds,90);
 assert.equal(reboot.summary().sentThisHour,5);
 snapshot.sessions=snapshot.sessions.filter(s=>s.id!=='mjobs-luna-34');
 const noRoot=await reboot.preview();
 assert.match(noRoot.preview.pausedReason,/No active primary orchestrator/);
 const diagnostic='FIXTURE_PRIVATE_COMMAND_AND_STACK';
 reboot.session.lastError=diagnostic;
 reboot.session.actions.push({at:new Date(now).toISOString(),id:'fixture',error:diagnostic});
 reboot.latest.githubError=diagnostic;
 assert.ok(!JSON.stringify(reboot.summary()).includes(diagnostic),'Public summary sanitizes historical diagnostics');
 assert.equal(reboot.session.lastError,diagnostic,'Recovery evidence is not destroyed by rendering');
 reboot.error(new Error(diagnostic));
 assert.ok(fs.readFileSync(reboot.eventsPath,'utf8').includes(diagnostic),'Operator detail remains in local log');
 reboot.getState=async()=>{throw new Error(diagnostic)};
 await assert.rejects(()=>reboot.cycle({manual:true}),e=>e.message.includes('Turbo operation failed')&&!e.message.includes(diagnostic),'Manual failure returns safe summary');
 assert.ok(!JSON.stringify(reboot.summary()).includes(diagnostic));
 const raceRoot=path.join(temp,'decision-race');fs.mkdirSync(raceRoot);
 const raceSent=[];let raceReads=0;
 const raceAgent={...idle('race-primary','primary'),mode:'tui',activity:'waiting_input'};
 const race=new Turbo({root:raceRoot,autoSchedule:false,clock:()=>now,githubRunner:async()=>github,
  getState:async()=>({...snapshot,sessions:[{...raceAgent,activity:++raceReads===1?'waiting_input':'blocked'}]}),
  command:async args=>raceSent.push(args),audit:()=>{}});
 const raceResult=await race.cycle({manual:true});
 assert.ok(raceReads>=2,'The instruction-ready primary was selected and rechecked');
 assert.equal(raceResult.executed.length,0);assert.deepEqual(raceSent,[],'A decision appearing before delivery blocks the send');
 console.log('PASS Turbo: budgets, cooldowns, instruction-waiting primary, decision-race rejection, resource gates, persistence and private diagnostics');
 fs.rmSync(temp,{recursive:true,force:true});
};
run().catch(e=>{console.error(e.stack);process.exitCode=1});
