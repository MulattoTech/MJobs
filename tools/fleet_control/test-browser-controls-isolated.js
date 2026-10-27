'use strict';
// Opt-in Windows browser contract: real UI assets, synthetic state, inert POSTs.
// It never imports server.js, executes AO/Codex, or calls a production service.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),http=require('node:http'),assert=require('node:assert/strict');
const {spawn}=require('node:child_process');
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const assets=path.join(__dirname,'public'),rows=[],posts=[],exceptions=[],blockedRequests=[];
let scenario='healthy',server,child,ws,profile;
const iso=()=>new Date().toISOString();
function quota(){return {fresh:true,ageSeconds:0,sampledAt:iso(),monitorOnline:true,precision:'fixture',selectedRemainingPercent:97,guardAcknowledged:true,monitoredWindow:'Weekly',config:{enabled:true,autoKillArmed:scenario!=='unarmed',tripped:false,thresholdPercent:7,window:'any',pollSeconds:5},trigger:{tripped:false},windows:[{label:'Weekly',remainingPercent:97,resetsAt:'2030-01-01T00:00:00Z'}]};}
function roster(){return {timestamp:iso(),offline:scenario==='offline',snapshotAt:iso(),summary:{total:2,working:0,idle:2,idleFree:2,idlePending:0,ciAttention:0,other:0},metrics:{cpu:10,ramTotal:64*1024**3,ramFree:32*1024**3,logicalCores:16},sessions:[{id:'mjobs-primary',name:'Fixture primary',kind:'orchestrator',model:'fixture-model',harness:'codex',mode:'tui',role:'Orchestration',activity:scenario==='offline'?'exited':'idle',isIdle:scenario!=='offline',busy:false,prs:[],openPRs:0,stateLabel:'Idle',branch:'ao/fixture',createdAt:iso(),lastActivity:iso(),branchState:{}},{id:'mjobs-worker',name:'Fixture worker',kind:'worker',model:'fixture-model',harness:'codex',mode:'tui',role:'Implementation',activity:scenario==='waiting'?'waiting_input':scenario==='blocked'?'blocked':scenario==='offline'?'exited':'idle',isIdle:!['blocked','offline'].includes(scenario),busy:false,prs:[],openPRs:0,stateLabel:'Fixture state',branch:'ao/fixture-worker',createdAt:iso(),lastActivity:iso(),branchState:{},replacedBy:scenario==='replaced'?'mjobs-replacement':null}],replacements:[],models:[],efforts:['medium']};}
function readiness(){return {at:iso(),ready:!['offline','uncertain','restoring','latched','stopped'].includes(scenario),runtimesReady:!['offline','stopped'].includes(scenario),total:2,verified:scenario==='stopped'?1:2,dispatchPaused:true,reason:scenario==='stopped'?'Primary stopped':'Fixture ready',quota:{fresh:true,remainingPercent:97}};}
function startup(){return {run:{phase:scenario==='resumable'?'resumable':scenario==='uncertain'?'uncertain':scenario==='restoring'?'restoring':'ready_paused',runId:'fixture-run-0001',active:scenario==='restoring',reconciliationRequired:scenario==='uncertain',finishedAt:iso()},readiness:readiness(),preview:{targetCount:2}};}
function master(){return {dispatch:{sustainedEnabled:scenario==='sustain-on',turboEnabled:scenario==='turbo-on'},emergency:{latched:scenario==='latched',remainingTotal:0,phase:'holding_zero',checkedAt:iso()},restore:{phase:scenario==='restoring'?'restoring_workers':'complete',active:scenario==='restoring',total:2,restoredCount:2,nextCheckSeconds:12},drain:{enabled:scenario==='draining',phase:scenario==='draining'?'draining':'idle',counts:{working:0,idle:2,exited:0}},currentReadiness:readiness(),startup:startup().run};}
function turbo(){return {config:{enabled:scenario==='turbo-on',targetWorking:12,maxActionsPerCycle:2,minFreeRamGiB:7,perAgentCooldownMinutes:10,pollingSeconds:45,maxActionsPerHour:18},sentThisHour:0,running:false,lastPlan:{ready:true,pausedReason:null,counts:{working:0,openPRs:0,failedPRs:0},nextActions:[]}};}
function sustain(){return {enabled:scenario==='sustain-on',running:false,config:{enabled:scenario==='sustain-on',intervalSeconds:60,targetWorking:12},latest:{blocked:null},leases:[],lastTick:iso(),nextTick:iso()};}
function fixture(route){
 if(route==='/api/state')return roster();if(route==='/api/startup')return startup();if(route==='/api/master')return master();if(route==='/api/quota')return quota();
 if(route==='/api/providers')return {providers:[{id:'codex',name:'Codex',available:true,models:[{id:'fixture-model',name:'Fixture model',efforts:['medium'],defaultEffort:'medium'}]}]};
 if(route==='/api/agent-plan')return {plans:scenario==='planned'?[{id:'mjobs-worker',status:'pending',harness:'codex',model:'fixture-model',effort:'medium',mode:'tui',updatedAt:iso()}]:[]};
 if(route==='/api/preflight')return {ready:readiness().ready,issues:[],canSaveAgentPlans:true};if(route==='/api/gemini-usage')return {providers:[]};
 if(route==='/api/turbo')return turbo();if(route==='/api/turbo/preview')return {preview:turbo().lastPlan};if(route==='/api/sustain')return sustain();if(route==='/api/sustain/preview')return {blocked:null};
 if(route==='/api/find-work/preview')return {eligibleCount:2,eligible:[{id:'mjobs-primary',category:'primary'},{id:'mjobs-worker',category:'implementation'}],working:0,skippedCount:0};
 if(route==='/api/turbo/all-hands/preview')return {plan:[{id:'mjobs-worker'}],working:[],skipped:[],globalCooldownMinutes:0,resourceGuard:null};
 if(route==='/api/master/restore/preview')return {totalTargets:2,alreadyOnline:1,willResume:1,rootConflicts:[],emergencyLatched:scenario==='latched'};
 return {};
}
function responseFor(route,body){
 if(route==='/api/startup')return {...startup(),run:{...startup().run,phase:'validating',active:true,requestId:body.requestId},statusCode:202};
 if(route==='/api/startup/reconcile')return {...startup(),run:{...startup().run,phase:'resumable',reconciliationRequired:false},reconciled:true};
 if(route==='/api/startup/cancel')return {...startup(),run:{...startup().run,phase:'cancelled',active:false}};
 if(route.startsWith('/api/master/'))return master();if(route.startsWith('/api/quota/'))return quota();
 if(route==='/api/turbo/all-hands')return {submitted:1,skipped:0,uncertain:0};if(route.startsWith('/api/turbo/'))return turbo();
 if(route.startsWith('/api/sustain/'))return sustain();
 if(route==='/api/agent-plan')return {plan:{...body,status:'pending',updatedAt:iso()}};
 return {ok:true,category:'implementation',submittedCount:2,skippedCount:0,uncertainCount:0,note:'Inert fixture acknowledgment'};
}
async function main(){
 if(process.platform!=='win32'){console.log('NOT_RUN: Windows Edge is required');process.exitCode=2;return;}
 const edge=['C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe','C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe'].find(fs.existsSync);assert(edge,'Edge unavailable');
 server=http.createServer(async(req,res)=>{try{
  const url=new URL(req.url,'http://fixture');
  if(req.method==='POST'){
   let text='';for await(const chunk of req){text+=chunk;if(text.length>20000)throw Error('Oversized fixture request');}
   assert.equal(req.headers['x-ao-fleet'],'1');const body=JSON.parse(text||'{}');posts.push({scenario,route:url.pathname,body});
   res.writeHead(200,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify(responseFor(url.pathname,body)));return;
  }
  if(url.pathname.startsWith('/api/')){res.writeHead(200,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify(fixture(url.pathname)));return;}
  const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
  if(!/^[A-Za-z0-9_.-]+$/.test(name)||!fs.existsSync(path.join(assets,name))){res.writeHead(404);res.end();return;}
  res.writeHead(200,{'content-type':name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':'text/html','cache-control':'no-store'});res.end(fs.readFileSync(path.join(assets,name)));
 }catch{res.writeHead(500,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify({error:'FIXTURE_REQUEST_FAILED'}));}});
 await new Promise(ok=>server.listen(0,'127.0.0.1',ok));const port=server.address().port;
 profile=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-isolated-browser-'));
 child=spawn(edge,['--headless=new','--disable-gpu','--no-first-run','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
 let debug;for(let i=0;i<60;i++){try{debug=Number(fs.readFileSync(path.join(profile,'DevToolsActivePort'),'utf8').split('\n')[0]);if(debug)break;}catch{}await sleep(100);}assert(debug);
 const tabs=await(await fetch('http://127.0.0.1:'+debug+'/json/list')).json(),page=tabs.find(x=>x.type==='page');assert(page);
 ws=new WebSocket(page.webSocketDebuggerUrl);await new Promise((ok,no)=>{ws.onopen=ok;ws.onerror=no;});const pending=new Map();let seq=0;
 const call=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{pending.delete(id);reject(Error('CDP timeout '+method));},10000);pending.set(id,{resolve,reject,timer});ws.send(JSON.stringify({id,method,params}));});
 ws.onmessage=event=>{const m=JSON.parse(event.data);if(m.method==='Runtime.exceptionThrown')exceptions.push(m.params.exceptionDetails.exception?.description||m.params.exceptionDetails.text);
  if(m.method==='Fetch.requestPaused'){
   const request=m.params,allowed=request.request.url.startsWith('http://127.0.0.1:'+port+'/');
   if(!allowed)blockedRequests.push(request.request.url);
   void call(allowed?'Fetch.continueRequest':'Fetch.failRequest',{requestId:request.requestId,...(allowed?{}:{errorReason:'BlockedByClient'})}).catch(()=>{});
  }
  const job=pending.get(m.id);if(job){pending.delete(m.id);clearTimeout(job.timer);m.error?job.reject(Error(m.error.message)):job.resolve(m.result);}
 };
 await call('Runtime.enable');await call('Page.enable');await call('Fetch.enable',{patterns:[{urlPattern:'*'}]});
 const evaluate=async(expression)=>{const r=await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
 const pageFunctions=Object.freeze({
  selectable:'function(selector,enabled){const element=document.querySelector(selector);return !!element&&(!enabled||!element.disabled)}',
  click:'function(selector){document.querySelector(selector).click()}',
  input:"function(selector,value){const element=document.querySelector(selector);element.value=value;element.dispatchEvent(new Event('input',{bubbles:true}))}",
  sentinel:'function(){return globalThis.__fleetHarnessInjected===true}',
 });
 const invoke=async(operation,...values)=>{const functionDeclaration=pageFunctions[operation];assert(functionDeclaration);const globalResult=await call('Runtime.evaluate',{expression:'globalThis'}),objectId=globalResult.result.objectId;assert(objectId);const r=await call('Runtime.callFunctionOn',{objectId,functionDeclaration,arguments:values.map(value=>({value})),awaitPromise:true,returnByValue:true});if(r.exceptionDetails)throw Error('Browser fixture function failed');return r.result.value;};
 const wait=async(expression)=>{for(let i=0;i<100;i++){if(await evaluate(expression))return;await sleep(40);}throw Error('Browser condition failed: '+expression);};
 const waitForSelector=async(selector,enabled=false)=>{for(let i=0;i<100;i++){if(await invoke('selectable',selector,enabled))return;await sleep(40);}throw Error('Browser fixture selector unavailable');};
 const click=async(selector)=>{await waitForSelector(selector,true);await invoke('click',selector);};
 const input=async(selector,value)=>{await waitForSelector(selector);await invoke('input',selector,value);};
 const load=async(value='healthy')=>{scenario=value;await call('Page.navigate',{url:'http://127.0.0.1:'+port+'/?scenario='+value});await wait('document.querySelectorAll("#tableRows tr").length===2&&document.querySelector("#quotaLive")?.textContent.startsWith("LIVE")&&typeof FleetStartupUI!=="undefined"&&FleetStartupUI.status().fresh');await sleep(100);};
 async function check(name,{state='healthy',button,confirm,phrase,route,body,prepare,verify,afterOpen}={}){
  await load(state);if(prepare)await prepare();const count=posts.length;
  await click(button);if(confirm){await wait('!!document.querySelector('+JSON.stringify(confirm)+')');assert.equal(posts.length,count,name+' must not act before confirmation');if(afterOpen)await afterOpen();if(phrase){assert(await evaluate('document.querySelector('+JSON.stringify(confirm)+').disabled'),name+' missing phrase gate');await input(phrase[0],phrase[1]);}await click(confirm);}
  for(let i=0;i<100&&posts.length===count;i++)await sleep(30);
  if(name==='All Hands burst')await wait('document.querySelector("#modalBody").textContent.includes("All Hands dispatched")');
  assert.equal(posts.length,count+1,name+' must send one request');const sent=posts.at(-1);assert.equal(sent.route,route,name);
  for(const [key,value] of Object.entries(body||{}))assert.deepEqual(sent.body[key],value,name+' '+key);
  if(verify)await verify();
  rows.push({name,status:'PASS',route,scenario:state});
 }
 const inertPayload="\"'`<script>globalThis.__fleetHarnessInjected=true</script>";
 const cases=[
  ['Startup',{button:'#startupStart',confirm:'#startupConfirmButton',phrase:['#startupConfirmText','STARTUP'],route:'/api/startup',body:{confirm:'STARTUP',startWorkAfter:true}}],
  ['Continue Startup',{state:'resumable',button:'#startupStart',confirm:'#startupConfirmButton',phrase:['#startupConfirmText','STARTUP'],route:'/api/startup',body:{confirm:'STARTUP',startWorkAfter:true}}],
  ['Restore only',{button:'#startupStart',afterOpen:()=>click('#startupWorkOption'),confirm:'#startupConfirmButton',phrase:['#startupConfirmText','STARTUP'],route:'/api/startup',body:{confirm:'STARTUP',startWorkAfter:false}}],
  ['Recheck safely',{state:'uncertain',button:'#startupRecheck',route:'/api/startup/reconcile',body:{confirm:'RECHECK STARTUP'}}],
  ['Cancel Startup',{state:'restoring',button:'#startupCancel',route:'/api/startup/cancel',body:{runId:'fixture-run-0001'}}],
  ['Open Orchestrator',{button:'#startupOpenUi',route:'/api/orchestrator/open'}],
  ['Pause assignments',{button:'#masterPause',route:'/api/master/pause'}],
  ['Graceful drain',{button:'#masterDrain',confirm:'#confirmGraceful',route:'/api/master/drain',body:{confirm:'GRACEFUL STOP'}}],
  ['Cancel drain',{state:'draining',button:'#masterCancelDrain',route:'/api/master/cancel-drain'}],
  ['Emergency stop',{button:'#masterKill',confirm:'#confirmEmergency',phrase:['#emergencyPhrase','KILL ALL CODEX'],route:'/api/master/emergency',body:{confirm:'KILL ALL CODEX'}}],
  ['Topbar emergency stop',{button:'#topbarKill',confirm:'#confirmEmergency',phrase:['#emergencyPhrase','KILL ALL CODEX'],route:'/api/master/emergency',body:{confirm:'KILL ALL CODEX'}}],
  ['Emergency recheck',{state:'latched',button:'#masterKill',confirm:'#retryEmergency',route:'/api/master/emergency',body:{confirm:'KILL ALL CODEX'}}],
  ['Release stop latch',{state:'latched',button:'#masterRearm',confirm:'#confirmRearm',phrase:['#rearmPhrase','REARM'],route:'/api/master/rearm',body:{confirm:'REARM'}}],
  ['Restore fleet',{button:'#masterRestore',confirm:'#confirmRestore',phrase:['#restorePhrase','RESTORE FLEET'],route:'/api/master/restore',body:{confirm:'RESTORE FLEET',autoDispatchAfter:false}}],
  ['Topbar restore',{button:'#topbarRestore',confirm:'#confirmRestore',phrase:['#restorePhrase','RESTORE FLEET'],route:'/api/master/restore'}],
  ['Pause restoration',{state:'restoring',button:'#masterCancelRestore',route:'/api/master/restore/pause'}],
  ['Enable Turbo',{button:'#turboToggle',confirm:'#enableTurboConfirmed',route:'/api/turbo/toggle',body:{enabled:true}}],
  ['Pause Turbo',{state:'turbo-on',button:'#turboToggle',route:'/api/turbo/toggle',body:{enabled:false}}],
  ['Run Turbo cycle',{state:'turbo-on',button:'#turboRun',confirm:'#runTurboConfirmed',route:'/api/turbo/run',body:{dryRun:false}}],
  ['All Hands burst',{button:'#turboAllHands',confirm:'#confirmAllHands',route:'/api/turbo/all-hands',body:{confirm:'ALL HANDS'}}],
  ['Save Turbo settings',{button:'#turboSave',route:'/api/turbo/config',body:{targetWorking:12}}],
  ['Enable Sustained',{button:'#sustainToggle',confirm:'#confirmSustain',route:'/api/sustain/toggle',body:{enabled:true}}],
  ['Pause Sustained',{state:'sustain-on',button:'#sustainToggle',route:'/api/sustain/toggle',body:{enabled:false}}],
  ['Save productive capacity policy',{prepare:async()=>{await click('#productiveEnabled');await input('#productiveTarget','31');await input('#productiveReserves','0');},button:'#productiveSave',route:'/api/sustain/config',body:{productiveWork:true,targetWorking:31,reserveWorkers:0}}],
  ['Save quota settings',{button:'#quotaSave',route:'/api/quota/config',body:{thresholdPercent:7}}],
  ['Arm quota protection',{state:'unarmed',button:'#quotaArm',confirm:'#quotaArmConfirm',phrase:['#quotaArmPhrase','ARM AUTO STOP'],route:'/api/quota/arm',body:{confirm:'ARM AUTO STOP'}}],
  ['Disarm quota protection',{button:'#quotaArm',route:'/api/quota/disarm'}],
  ['Find Work for worker',{button:'[data-action="find-work"][data-id="mjobs-worker"]',route:'/api/find-work',body:{id:'mjobs-worker'}}],
  ['Find Work instruction-waiting worker',{state:'waiting',button:'[data-action="find-work"][data-id="mjobs-worker"]',route:'/api/find-work',body:{id:'mjobs-worker'}}],
  ['Find Work All',{button:'#findWorkAll',confirm:'#confirmFindWorkAll',route:'/api/find-work/all',body:{confirm:'FIND WORK ALL'}}],
  ['Ask agent status',{prepare:()=>click('[data-action="view"][data-id="mjobs-worker"]'),button:'[data-action="nudge"][data-type="progress"]',route:'/api/nudge',body:{id:'mjobs-worker',type:'progress'}}],
  ['Send custom instruction',{prepare:async()=>{await click('[data-action="view"][data-id="mjobs-worker"]');await input('#customMessage',inertPayload);},button:'[data-action="nudge"][data-type="custom"]',route:'/api/nudge',body:{message:inertPayload},verify:async()=>{assert.equal(await invoke('sentinel'),false);}}],
  ['Save worker plan',{prepare:()=>click('[data-action="view"][data-id="mjobs-worker"]'),button:'[data-action="save-plan"]',route:'/api/agent-plan',body:{id:'mjobs-worker',harness:'codex',model:'fixture-model',effort:'medium'}}],
  ['Apply saved worker plan',{state:'planned',prepare:()=>click('[data-action="view"][data-id="mjobs-worker"]'),button:'[data-action="apply-plan"]',confirm:'#confirmApplyPlan',route:'/api/agent-plan/apply',body:{confirm:'APPLY AGENT PLAN'}}],
  ['Archive replaced original',{state:'replaced',prepare:()=>click('[data-action="view"][data-id="mjobs-worker"]'),button:'[data-action="retire"]',confirm:'#confirmRetire',phrase:['#retireConfirm','mjobs-worker'],route:'/api/retire',body:{id:'mjobs-worker',confirm:'mjobs-worker'}}],
 ];
 for(const [name,options]of cases){try{await check(name,options);console.log('PASS '+name);}catch(e){rows.push({name,status:'FAIL',error:e.message});console.error('FAIL '+name+': '+e.message);}}
 await load('offline');assert(await evaluate('document.querySelector("#findWorkAll").disabled'));await click('[data-action="view"][data-id="mjobs-worker"]');assert(await evaluate('document.querySelector("[data-action=save-plan]").disabled===false&&document.querySelector("[data-action=apply-plan]").disabled'));rows.push({name:'Offline configuration remains usable, execution blocked',status:'PASS'});
 await load('blocked');assert(await evaluate('document.querySelector("[data-action=find-work][data-id=mjobs-worker]").disabled'));rows.push({name:'Approval decision cannot be nudged as an idle prompt',status:'PASS'});
 await load();const count=posts.length;await click('#masterKill');await click('[data-modal="cancel"]');assert.equal(posts.length,count);rows.push({name:'Cancel destructive confirmation performs no request',status:'PASS'});
 assert.equal(exceptions.length,0,'JavaScript exceptions: '+exceptions.join('\n'));assert.equal(blockedRequests.length,0,'Unexpected non-fixture network request');
 const report={at:iso(),ok:rows.every(x=>x.status==='PASS'),passed:rows.filter(x=>x.status==='PASS').length,failed:rows.filter(x=>x.status==='FAIL').length,rows,fixturePosts:posts.length,productionRequests:0,nativeAgentCommands:0,javascriptErrors:exceptions};
 const destination=process.argv[2];if(destination)fs.writeFileSync(destination,JSON.stringify(report,null,2));console.log(JSON.stringify({...report,rows:undefined}));if(!report.ok)process.exitCode=1;try{await call('Browser.close');}catch{}
}
main().catch(e=>{console.error(e.stack);process.exitCode=1;}).finally(()=>{try{ws?.close();child?.kill();server?.close();}catch{}setTimeout(()=>process.exit(process.exitCode||0),300);});
