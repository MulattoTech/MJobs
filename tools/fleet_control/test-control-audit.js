'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),os=require('node:os'),vm=require('node:vm');
const {FindWork}=require('./find-work'),{preflight}=require('./preflight');
function findFixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-buttons-'));t.after(()=>fs.rmSync(root,{recursive:true,force:true}));let hold=null,rows=[{id:'mjobs-worker',kind:'worker',mode:'tui',activity:'waiting_input',isIdle:true,isTerminated:false,prs:[]}],delay=null;const sent=[];const work=new FindWork({root,getState:async()=>{if(delay)await delay;return {sessions:rows};},guard:()=>hold,command:async args=>{sent.push(args);return 'accepted';},audit:()=>{}});return {work,sent,setHold:x=>{hold=x;},setRows:x=>{rows=x;},setDelay:x=>{delay=x;}};}
test('Find Work accepts a genuine instruction prompt but never a pending decision',async t=>{
 const f=findFixture(t);assert.equal((await f.work.preview()).eligibleCount,1);await f.work.sendOne('mjobs-worker');assert.equal(f.sent.length,1);
 assert.ok(f.work.eligibility({id:'other-worker',activity:'blocked',isIdle:true}));assert.ok(f.work.eligibility({id:'other-worker',activity:'exited',isIdle:true}));
 await assert.rejects(f.work.sendOne('mjobs-worker'),/already requested/);assert.equal(f.sent.length,1);
});
test('a stop during a pending roster read prevents even a delivery reservation',async t=>{
 const f=findFixture(t);let release;f.setDelay(new Promise(ok=>{release=ok;}));const task=f.work.sendOne('mjobs-worker');f.setHold('Emergency stop engaged');release();await assert.rejects(task,/Emergency/);assert.equal(f.sent.length,0);assert.equal(f.work.state.sent['mjobs-worker'],undefined);
});
test('empty Find Work bulk response has usable numeric counts',async t=>{const f=findFixture(t);f.setRows([]);const r=await f.work.sendAll();assert.deepEqual([r.submittedCount,r.skippedCount,r.uncertainCount],[0,0,0]);assert.equal(f.sent.length,0);});
test('offline fallback can never authorize a Find Work instruction',async t=>{const f=findFixture(t);f.work.getState=async()=>({offline:true,sessions:[]});await assert.rejects(f.work.preview(),/offline/);await assert.rejects(f.work.sendOne('mjobs-worker'),/offline/);assert.equal(f.sent.length,0);});
function sourcePart(file,start,end){const s=fs.readFileSync(path.join(__dirname,'public',file),'utf8'),a=s.indexOf(start),b=s.indexOf(end,a+start.length);assert(a>=0&&b>a);return s.slice(a,b);}
function element(){return {value:'',textContent:'',disabled:false,hidden:false,className:'',classList:{add(){},remove(){},toggle(){}},addEventListener(){},focus(){}};}
function startupUi(){
 const elements=new Map(),calls=[],modals=[];const byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
 const c=vm.createContext({run:{phase:'resumable',active:false},readiness:{quota:{fresh:true,remainingPercent:97}},preview:{targetCount:3},actionBusy:false,pendingRequest:null,modalOpen:false,mutationEpoch:0,byId,fresh:()=>true,uncertain:()=>false,alreadyStarted:()=>false,escape:String,openModal:html=>modals.push(html),closeModal(){},render(){},savePending(){},setText(){},notify(){},refresh(){},requestId:()=> 'test-startup-id-001',api:async(route,data)=>{calls.push({route,data});return {run:{phase:'validating',active:true}};},accept(){}});
 vm.runInContext(sourcePart('startup-ui.js','  function updateModal()','  async function refresh(')+sourcePart('startup-ui.js','  function previewStartup()','  async function openOrchestrator()'),c);
 return {c,calls,modals,byId};
}
test('Continue Startup opens confirmation, requires the phrase, and sends exactly one request',async()=>{
 const f=startupUi();f.c.previewStartup();assert.equal(f.modals.length,1);assert.equal(f.byId('startupConfirmButton').disabled,true);
 await f.c.submitStartup();assert.equal(f.calls.length,0);f.byId('startupConfirmText').value='STARTUP';f.c.updateModal();assert.equal(f.byId('startupConfirmButton').disabled,false);
 let release;f.c.api=async(route,data)=>{f.calls.push({route,data});await new Promise(ok=>{release=ok;});return {run:{phase:'validating'}};};
 const first=f.c.submitStartup();const second=f.c.submitStartup();assert.equal(f.calls.length,1);assert.equal(f.calls[0].data.confirm,'STARTUP');release();await Promise.all([first,second]);
});
test('stale, active, and unresolved Startup states cannot open launch confirmation',()=>{
 for(const state of ['stale','active','uncertain']){const f=startupUi();if(state==='stale')f.c.fresh=()=>false;if(state==='active')f.c.run.active=true;if(state==='uncertain')f.c.uncertain=()=>true;f.c.previewStartup();assert.equal(f.modals.length,0);assert.equal(f.calls.length,0);}
});
test('a timed out Startup retries its original request identity rather than launching a new request',async()=>{
 const f=startupUi();f.byId('startupConfirmText').value='STARTUP';let count=0;f.c.api=async(route,data)=>{f.calls.push({route,data});if(++count===1)throw Error('timeout');return {run:{phase:'validating'}};};await f.c.submitStartup();assert.equal(f.c.pendingRequest.uncertain,true);await f.c.submitStartup();assert.equal(f.calls[0].data.requestId,f.calls[1].data.requestId);
});
test('preflight distinguishes configuration saves from unsafe execution and bootstrap recovery',()=>{
 const base={quota:{fresh:true,guardAcknowledged:true,selectedRemainingPercent:97,config:{enabled:true,autoKillArmed:true,tripped:false,thresholdPercent:7}},master:{emergency:{latched:false}},snapshot:{offline:false}};
 assert.equal(preflight({...base,runtimeReadiness:{ready:true}}).ready,true);
 const stopped=preflight({...base,runtimeReadiness:{ready:false}});assert.equal(stopped.ready,false);assert.equal(stopped.canSaveAgentPlans,true);assert.equal(stopped.bootstrapEligible,true);
 assert.equal(preflight({...base,runtimeReadiness:{ready:true},controlBlockReason:'Recheck required'}).ready,false);
});
test('master badge never calls a held scheduler production active',()=>{
 const elements=new Map(),byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};const c=vm.createContext({byId,busy:false,Date,dateTime:()=>''});
 vm.runInContext(sourcePart('master-ui.js',' function masterState(',' async function refresh('),c);
 c.masterState({dispatch:{sustainedEnabled:true,blockedReason:'No live primary orchestrator'},emergency:{latched:false},restore:{phase:'complete'},currentReadiness:{ready:false,reason:'Primary stopped'}});
 assert.equal(byId('masterState').textContent,'DISPATCH HELD');assert.match(byId('masterText').textContent,/primary/);
 c.masterState({dispatch:{sustainedEnabled:false,turboEnabled:false},emergency:{latched:false}});assert.equal(byId('masterState').textContent,'DISPATCH PAUSED');
});

test('verified current dispatch takes precedence over a historical completed restore badge',()=>{
 const elements=new Map(),byId=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};const c=vm.createContext({byId,busy:false,Date,dateTime:()=>''});
 vm.runInContext(sourcePart('master-ui.js',' function masterState(',' async function refresh('),c);
 const data={dispatch:{sustainedEnabled:true,turboEnabled:false,blockedReason:null},emergency:{latched:false},restore:{phase:'complete',active:false,restoredCount:31,total:31},currentReadiness:{ready:true}};
 c.masterState(data);assert.equal(byId('masterState').textContent,'PRODUCTION ACTIVE');assert.match(byId('masterState').className,/on/);
 c.masterState({...data,dispatch:{sustainedEnabled:false,turboEnabled:false}});assert.equal(byId('masterState').textContent,'FLEET RESTORED');assert.match(byId('masterText').textContent,/paused/);
 c.masterState({...data,currentReadiness:{ready:false,reason:'Primary stopped'}});assert.equal(byId('masterState').textContent,'DISPATCH HELD');
});

test('completed healthy Startup is disabled at preview and submission, not only visually',async()=>{const f=startupUi();f.c.alreadyStarted=()=>true;f.c.previewStartup();assert.equal(f.modals.length,0);f.byId('startupConfirmText').value='STARTUP';await f.c.submitStartup();assert.equal(f.calls.length,0);});
