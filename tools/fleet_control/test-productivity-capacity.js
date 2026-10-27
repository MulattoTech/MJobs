'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os');
const {Sustain}=require('./sustain');const {classify}=require('./work-state');
function fixture(t,count=7){const root=fs.mkdtempSync(path.join(os.tmpdir(),'productive-capacity-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));let now=Date.parse('2026-10-27T00:00:00Z'),cpu=10,hold=false,delay=null;
 const sessions=Array.from({length:count},(_,i)=>({id:i?'mjobs-worker-'+i:'mjobs-root',kind:i?'worker':'orchestrator',role:i?'Implementation':'Orchestration',name:'Fixture '+i,mode:'tui',isIdle:true,busy:false,activity:'idle',lastActivity:new Date(now-600000).toISOString(),prs:i?[{number:50+i,state:'draft'}]:[],openPRs:i?1:0}));
 const gh={fresh:true,open:sessions.slice(1).map((s,i)=>({number:51+i,headRefOid:String(i+1).repeat(40).slice(0,40),isDraft:true,statusCheckRollup:[{name:'CI',status:'IN_PROGRESS',conclusion:''}]}))};
 fs.writeFileSync(path.join(root,'productive-catalog.json'),JSON.stringify({version:1,baseCommit:'f'.repeat(40),entries:Array.from({length:20},(_,i)=>({path:'services/doc/module'+i+'.py',blob:(i+1).toString(16).padStart(40,'0'),missingDocstrings:2}))}));
 const sent=[];const engine=new Sustain({root,autoSchedule:false,clock:()=>now,getState:async()=>{if(delay)await delay;return {sessions,summary:{total:count,working:sessions.filter(s=>s.busy).length,idle:count},metrics:{}};},turbo:{signals:async()=>({freeRamGiB:17,freeDiskGiB:200,cpuPercent:cpu}),github:async()=>gh,running:false},command:async args=>{sent.push(args);return 'accepted';},isManualBusy:()=>hold,audit:()=>{}});
 engine.config={...engine.config,enabled:true,productiveWork:true,targetWorking:count,maxDispatchPerTick:4,intervalSeconds:30,minQuietSeconds:60};engine.nextTick=new Date(now+30000).toISOString();
 return {engine,sessions,sent,root,now:()=>now,advance:n=>{now+=n;},cpu:n=>{cpu=n;},hold:v=>{hold=v;},delay:v=>{delay=v;}};
}
test('CI-waiting workers receive distinct real maintenance; delivery pending consumes capacity',async t=>{
 const f=fixture(t);f.engine.config.targetWorking=3;let plan=await f.engine.preview();assert.equal(plan.proposed.length,7);assert.equal(plan.selected.length,3);assert.equal(plan.held.length,6);
 const result=await f.engine.tick();assert.equal(result.sent.length,3);assert.equal(new Set(f.sent.map(x=>x[2])).size,3);
 f.advance(30000);plan=await f.engine.preview();assert.equal(plan.pending,3);assert.equal(plan.selected.length,0);assert.equal(plan.proposed.every(x=>x.etaSeconds===null),true,'No invented ETA when all slots await activity confirmation');
 assert.equal(f.engine.productivity.summary().assigned,2);assert.equal(f.sent.filter(x=>x.at(-1).includes('DOCUMENTED')).length,0);
});
test('effective CPU pacing is reflected in the queue ETA rather than promising full-rate dispatch',async t=>{
 const f=fixture(t);f.cpu(99);const p=await f.engine.preview();assert.equal(p.selected.length,1);assert.equal(p.proposed[1].etaSeconds-p.proposed[0].etaSeconds,30);
 assert.equal(p.proposed[2].etaSeconds-p.proposed[0].etaSeconds,60);
});
test('a pause during the live delivery read wins before any native submission',async t=>{
 const f=fixture(t);let reads=0,release;const wait=new Promise(ok=>{release=ok;});f.engine.getState=async()=>{if(++reads===2){f.hold(true);await wait;}return {sessions:f.sessions,summary:{total:7,working:0,idle:7},metrics:{}};};
 const task=f.engine.tick();for(let i=0;i<30&&reads<2;i++)await new Promise(ok=>setImmediate(ok));assert.equal(reads,2);release();await task;assert.equal(f.sent.length,0);assert.equal(f.engine.productivity.summary().assigned,0);
});
test('future plans and ambiguous maintenance delivery never masquerade as busy',t=>{
 const f=fixture(t),s=f.sessions[1],c={config:f.engine.config,status:{agents:{}},latest:{at:new Date(f.now()+5000).toISOString(),proposed:[{id:s.id,etaSeconds:5}]}};
 assert.equal(classify(s,c,f.now()).state,'unknown');c.latest.at=new Date(f.now()).toISOString();c.productivity={summary:()=>({tasks:[{agentId:s.id,state:'uncertain',sourcePath:'services/doc/module.py'}]})};
 const result=classify(s,c,f.now());assert.equal(result.state,'queued');delete c.latest.proposed;const held=classify(s,c,f.now());assert.equal(held.state,'delivery_review');assert.equal(held.executing,false);assert.equal(held.estimatedStartAt,null);
});
test('productive policy attaches to a real existing UI region and stages all-worker settings without launching',()=>{
 const html=fs.readFileSync(path.join(__dirname,'public/index.html'),'utf8'),ui=fs.readFileSync(path.join(__dirname,'public/sustain-ui.js'),'utf8');assert(html.includes('id="turboSection"'));assert(ui.includes("getElementById('turboSection')"));assert(!ui.includes("getElementById('sustainSection')"));
 assert(ui.includes('productiveAll'));assert(ui.includes('Nothing has started yet.'));
});
