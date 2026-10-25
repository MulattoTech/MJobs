'use strict';
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {createRequire}=require('node:module'),{AgentPlans}=require('./agent-plans');
const serverPath=path.join(__dirname,'server.js'),nativeRequire=createRequire(serverPath),source=fs.readFileSync(serverPath,'utf8');
const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-apply-test-'));
const plans=new AgentPlans({root,validate:async x=>({harness:'agy',model:'test-gemini',effort:'high',mode:'tui'})});
const old={id:'worker-1',name:'Worker',kind:'worker',activity:'idle',isIdle:true,busy:false,harness:'codex',branch:'old-work',prs:[{number:17}],branchState:{commits:3,unpushed:1}};
let snapshot={sessions:[old]},fail=false;const calls=[];
const c=vm.createContext({require:nativeRequire,__dirname:root,process,Buffer,URL,console,fetch,setTimeout,clearTimeout,structuredClone,AbortSignal,plans,snapshot,calls,async commandMock(args){
 calls.push(args);
 if(args[0]==='session')old.activity='exited';
 if(args[0]==='spawn'){
  if(fail)throw Error('Delivery timed out');
  snapshot.sessions.push({id:'replacement-1',kind:'worker',name:args[args.indexOf('--name')+1],harness:'agy',model:'test-gemini',isTerminated:false});
 }
 return 'ok';
}});
vm.runInContext(source.slice(0,source.indexOf("if(!fs.existsSync(AO)){"))+'\nglobalThis.apply=applyAgentPlan;startup={status:()=>({active:false,uncertain:false,phase:"idle"})};master={locked:()=>false,drainState:{enabled:false}};quota={dispatchBlockReason:()=>null};agentPlans=plans;state=async()=>snapshot;command=commandMock;audit=()=>{};',c);
(async()=>{
 await plans.save(old,{model:'test-gemini'});
 old.busy=true;await assert.rejects(()=>c.apply({id:old.id,confirm:'APPLY AGENT PLAN'}),/idle or stopped/);assert.equal(calls.length,0);
 old.busy=false;const result=await c.apply({id:old.id,confirm:'APPLY AGENT PLAN'});
 assert.equal(result.newId,'replacement-1');assert.equal(result.originalParked,true);
 assert.equal(calls[0][1],'exit-agent');assert.equal(calls[1][0],'spawn');
 assert.ok(calls[1].includes('agy'));assert.ok(!calls[1].includes('--effort'),'AGY reasoning is encoded by model variant, not an unverified effort override');
 assert.equal(old.branch,'old-work');assert.equal(old.prs[0].number,17);
 assert.equal(plans.all().plans[0].status,'applied');
 old.id='worker-2';old.activity='exited';snapshot={sessions:[old]};c.snapshot=snapshot;fail=true;
 await plans.save(old,{model:'test-gemini'});
 await assert.rejects(()=>c.apply({id:old.id,confirm:'APPLY AGENT PLAN'}),/timed out/);
 assert.equal(plans.all().plans.find(x=>x.id===old.id).status,'unverified');
 const count=calls.length;await assert.rejects(()=>c.apply({id:old.id,confirm:'APPLY AGENT PLAN'}),/Save a new/);assert.equal(calls.length,count);
 console.log('PASS: active worker rejected; idle worker parked before isolated AGY launch; branch/PRs preserved; selected effort sent; uncertain spawn cannot duplicate.');
 fs.rmSync(root,{recursive:true,force:true});
})().catch(e=>{console.error(e);process.exitCode=1});
