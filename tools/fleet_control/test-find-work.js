'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {FindWork,classify,instruction}=require('./find-work');
const root=path.join(__dirname,'test-find-work-state');
fs.rmSync(root,{recursive:true,force:true});fs.mkdirSync(root,{recursive:true});
let now=Date.parse('2026-10-23T07:00:00Z'),held=false;
const agent=(id,role,opts={})=>({
 id,name:id,role,kind:opts.kind||'worker',mode:opts.mode||'tui',
 isTerminated:false,replacedBy:null,activity:opts.activity||'idle',
 isIdle:(opts.activity||'idle')==='idle',
 busy:(opts.activity||'idle')==='active',
 prs:opts.prs||[],blocked:!!opts.blocked,openPRs:(opts.prs||[]).filter(x=>x.state==='draft'||x.state==='open').length
});
const sessions=[
 agent('mjobs-luna-35','Orchestration',{kind:'orchestrator',mode:'chat'}),
 agent('mjobs-luna-3','Fast implementation',{prs:[{number:775,state:'draft',ci:'failing'}],blocked:true}),
 agent('mjobs-luna-7','Fast implementation',{prs:[{number:774,state:'draft',ci:'passing'}]}),
 agent('mjobs-luna-22','Complex implementation',{prs:[{number:791,state:'open',ci:'passing'}]}),
 agent('mjobs-luna-31','Critical review'),
 agent('mjobs-luna-17','Orchestration'),
 agent('mjobs-luna-9','Fast implementation'),
 agent('mjobs-luna-2','Fast implementation',{activity:'active'}),
 agent('mjobs-luna-10','Fast implementation',{activity:'exited'})
];
const data={sessions,summary:{idle:7,working:1}};
const sent=[],events=[];
const f=new FindWork({root,getState:async()=>data,now:()=>now,
 command:async args=>{sent.push(args);return 'accepted'},
 audit:(name,params)=>events.push({name,params}),
 guard:()=>held?'Emergency locked':null});
async function test(){
 assert.equal(classify(sessions[0]),'primary');
 assert.equal(classify(sessions[1]),'ci');
 assert.equal(classify(sessions[2]),'draft');
 assert.equal(classify(sessions[3]),'pr');
 assert.equal(classify(sessions[4]),'review');
 assert.equal(classify(sessions[5]),'coordinator');
 assert.equal(classify(sessions[6]),'implementation');
 assert.match(instruction(sessions[2],'mjobs-luna-35').message,/DRAFT PR/);
 assert.match(instruction(sessions[1],'mjobs-luna-35').message,/FIRST diagnose exact-head failures/);
 assert.match(instruction(sessions[4],'mjobs-luna-35').message,/READ-ONLY review/);
 const before=await f.preview();
 assert.equal(before.eligibleCount,7);assert.equal(before.skippedCount,2);
 assert.equal(before.eligible[0].id,'mjobs-luna-35');
 assert.equal(sent.length,0);
 const one=await f.sendOne('mjobs-luna-9');
 assert.equal(one.status,'submitted');
 assert.equal(sent.length,1);
 assert.equal(sent[0].includes('--steer'),false);
 await assert.rejects(()=>f.sendOne('mjobs-luna-9'),/Retry in/);
 await assert.rejects(()=>f.sendOne('mjobs-luna-2'),/active turn/);
 await assert.rejects(()=>f.sendOne('mjobs-luna-10'),/exited/);
 const after=await f.preview();
 assert.equal(after.eligibleCount,6);
 const bulk=await f.sendAll();
 assert.equal(bulk.submittedCount,6);
 assert.equal(bulk.uncertainCount,0);
 assert.equal(sent.length,7);
 assert.equal(new Set(sent.map(s=>s[s.indexOf('--session')+1])).size,7);
 const rootCall=sent.find(s=>s.includes('mjobs-luna-35'));
 assert.ok(rootCall.includes('--steer'));
 assert.ok(rootCall.includes('--client-message-id'));
 const prCall=sent.find(s=>s.includes('mjobs-luna-7'));
 assert.match(prCall[prCall.indexOf('--message')+1],/DRAFT PR/);
 const reviewCall=sent.find(s=>s.includes('mjobs-luna-31'));
 assert.match(reviewCall[reviewCall.indexOf('--message')+1],/READ-ONLY review/);
 const state=await f.preview();
 assert.equal(state.eligibleCount,0);
 assert.ok(state.bulkCooldownSeconds>0);
 await assert.rejects(()=>f.sendAll(),/recently/);
 const recovery=new FindWork({root,getState:async()=>data,now:()=>now,command:async()=>{throw Error('not called')},audit:()=>{}});
 assert.equal((await recovery.preview()).eligibleCount,0);
 now+=121000;
 assert.equal((await recovery.preview()).eligibleCount,7);
 held=true;
 await assert.rejects(()=>f.sendAll(),/Emergency locked/);
 fs.rmSync(root,{recursive:true,force:true});
 console.log('PASS Find Work test: categories, CI/draft precedence, root coordination, 7 unique deliveries, per-session cooldown, bulk cooldown, persistence, active/exited skip, emergency guard');
}
test().catch(e=>{console.error(e.stack);process.exitCode=1;fs.rmSync(root,{recursive:true,force:true})});
