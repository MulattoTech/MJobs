'use strict';
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {Sustain}=require('./sustain');
const root=path.join(__dirname,'sustain-test-data');
fs.rmSync(root,{recursive:true,force:true});fs.mkdirSync(root,{recursive:true});
fs.writeFileSync(path.join(root,'sustain-task-bank.json'),JSON.stringify([
 {session:'mjobs-luna-9',issue:173,scope:['tests/unit/test_structured_text_web_security_sustain_173.py'],focus:'security negative test'},
 {session:'mjobs-luna-10',issue:217,scope:['tests/unit/test_company_resolution_sustain_217.py'],focus:'company matching regression'}
]));
let clockTime=Date.parse('2026-10-23T04:00:00Z');
const iso=x=>new Date(x).toISOString();
const ago=x=>iso(clockTime-x*1000);
const model='gpt-6-luna';
const agent=(id,role,prs=[],kind='worker')=>({id,name:id,role,kind,mode:kind==='orchestrator'?'chat':'tui',model,isIdle:true,busy:false,activity:'idle',lastActivity:ago(500),status:'idle',branchState:{commits:0,unpushed:0},openPRs:prs.length,prs});
const sessions=[
 agent('mjobs-luna-35','Orchestration',[],'orchestrator'),
 agent('mjobs-luna-2','Fast implementation',[{number:772,state:'draft',ci:'failing'}]),
 agent('mjobs-luna-31','Critical review'),
 agent('mjobs-luna-9','Fast implementation'),
 agent('mjobs-luna-10','Fast implementation')
];
const snap={timestamp:iso(clockTime),metrics:{ramFree:18*1024**3,cpu:15},summary:{total:5,working:0,idle:5,idleFree:4},sessions};
const gh={open:[
 {number:772,headRefOid:'1'.repeat(40),statusCheckRollup:[{conclusion:'FAILURE'}],isDraft:true,reviewDecision:''},
 {number:761,headRefOid:'2'.repeat(40),statusCheckRollup:[{conclusion:'SUCCESS'}],isDraft:false,reviewDecision:''}
],at:iso(clockTime)};
const sent=[],log=[];
const turbo={signals:async s=>({freeRamGiB:s.metrics.ramFree/1024**3,cpuPercent:s.metrics.cpu,freeDiskGiB:300,pausedReason:s.metrics.ramFree<9*1024**3?'Low RAM':null}),
 github:async()=>gh,running:false};
const sustain=new Sustain({root,getState:async()=>snap,turbo,command:async args=>{sent.push(args);return 'accepted'},audit:(event,data)=>log.push({event,data}),
 issueRunner:async issue=>({number:issue,state:'OPEN',title:'Mock GitHub issue '+issue,assignees:[]}),clock:()=>clockTime,autoSchedule:false});
async function test(){
 assert.equal(sustain.config.enabled,false);
 assert.equal(sustain.config.targetWorking,12);
 assert.equal(sustain.config.maxDispatchPerTick,2);
 assert.equal(sustain.config.resourceHeavyTestLanes,2);
 assert.ok((await sustain.preview()).selected.length<=2,'default dispatch stays bounded');
 // Explicit larger fixture exercises five distinct ownership paths in one cycle.
 sustain.update({maxDispatchPerTick:6,resourceHeavyTestLanes:3});
 const pre=await sustain.preview();
 assert.equal(pre.working,0);
 assert.equal(pre.rootId,'mjobs-luna-35');
 assert.equal(sent.length,0);
 assert.ok(pre.selected.some(x=>x.id==='mjobs-luna-35'));
 assert.ok(pre.selected.some(x=>x.id==='mjobs-luna-9'&&x.issue===173));
 assert.ok(pre.selected.some(x=>x.id==='mjobs-luna-10'&&x.issue===217));
 sustain.enable(true);
 const first=await sustain.tick();
 assert.equal(first.sent.length,5);
 assert.equal(sent.length,5);
 assert.equal(sent.find(x=>x.includes('mjobs-luna-35')).includes('--steer'),true);
 assert.equal(new Set(sent.map(x=>x[2])).size,5);
 assert.ok(sustain.status.leases[173]);
 assert.ok(sustain.status.leases[217]);
 assert.notEqual(sustain.status.leases[173].scope[0],sustain.status.leases[217].scope[0]);
 const immediate=await sustain.tick();
 assert.equal(immediate.sent.length,0,'do not duplicate unacknowledged work');
 // Agent finishes its turn; cooldown still blocks immediate resend.
 clockTime+=40000;
 sessions[3].lastActivity=iso(clockTime+1500);
 const soon=await sustain.tick();assert.equal(soon.sent.length,0);
 clockTime+=190000;
 snap.timestamp=iso(clockTime);
 // Agent has just finished; cannot be re-contacted until quiet period.
 sessions[3].lastActivity=ago(90);
 const shortRest=await sustain.preview();
 assert.ok(!shortRest.selected.some(x=>x.id==='mjobs-luna-9'));
 clockTime+=180000;
 const after=await sustain.tick();
 assert.ok(after.sent.some(x=>x.id==='mjobs-luna-9'),'should restart its reserved task after quiet period');
 assert.equal(sustain.status.leases[173].session,'mjobs-luna-9');
 assert.ok(sustain.status.dispatches.length>=6);
 snap.metrics.ramFree=4*1024**3;clockTime+=200000; const blocked=await sustain.tick();
 assert.ok(/RAM/i.test(blocked.blocked),'memory stops a cycle');
 const restart=new Sustain({root,getState:async()=>snap,turbo,command:async args=>sent.push(args),audit:()=>{},issueRunner:async()=>({state:'OPEN',title:'Mock issue',assignees:[]}),clock:()=>clockTime,autoSchedule:false});
 assert.equal(restart.config.enabled,true);
 assert.equal(restart.status.leases[173].session,'mjobs-luna-9');
 restart.enable(false);
 assert.equal((await restart.tick()).skipped,'disabled');
 snap.metrics.ramFree=18*1024**3;snap.metrics.cpu=100;
 const cpuDegraded=await restart.preview();
 assert.equal(cpuDegraded.blocked,null);
 assert.match(cpuDegraded.resources.throttledReason,/CPU pressure/);
 assert.ok(cpuDegraded.selected.length<=1);
 snap.metrics.cpu=20;
 console.log('PASS Sustained mode: 19 checks, issue leasing, per-agent completion/quiet time, no duplicate sends, Chat/TUI delivery, RAM gate, persistence');
 fs.rmSync(root,{recursive:true,force:true});
}
test().catch(e=>{console.error(e.stack);process.exitCode=1});
