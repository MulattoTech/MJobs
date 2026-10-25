'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync(path.join(__dirname,'public','app.js'),'utf8');
function section(start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert(a>=0&&b>a,'Missing section '+start);return source.slice(a,b);}
function context(extra={}){return vm.createContext({console,AbortController,Date,setTimeout,clearTimeout,...extra});}
function load(c,code,names){vm.runInContext(code+'\n'+names.map(n=>'globalThis.'+n+'='+n+';').join('\n'),c);}
function elem(){return {hidden:false,value:'',innerHTML:'',textContent:'',style:{},options:[],disabled:false,querySelectorAll:()=>[],contains:()=>false};}
function esc(x){return String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
async function testApi(){
 const calls=[],timers=[],c=context({fleetPrefix:'/fleet',setTimeout:(fn,ms)=>{const t={fn,ms};timers.push(t);return t},clearTimeout:()=>{},fetch:(url,options)=>{calls.push({url,options});return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{const e=Error('abort');e.name='AbortError';reject(e)}));}});
 load(c,section('async function api(','function updateCounts('),['api']);
 for(const [route,data,ms] of [['/api/state',undefined,10000],['/api/nudge',{id:'worker'},45000],['/api/agent-plan/apply',{id:'worker'},90000]]){
  const before=calls.length,result=c.api(route,data).catch(e=>e);assert.equal(timers.at(-1).ms,ms);timers.at(-1).fn();
  assert.match((await result).message,/timed out/i);assert.equal(calls.length,before+1);assert.equal(calls.at(-1).url,'/fleet'+route);assert.equal(calls.at(-1).options.cache,'no-store');
 }
 c.fetch=async()=>({ok:true,json:async()=>{throw Error('bad json')}});await assert.rejects(c.api('/api/agent-plan',{}),/outcome is unknown/i);
}
function testDraft(){
 const elements={'#overlay':{hidden:false},'#changeProvider':{value:'agy'},'#changeModel':{value:'gemini-3.8-flash-high'},'#changeEffort':{value:'high'},'#changeMode':{value:'tui'},'#customMessage':{value:'Preserve unsent instruction'}},state={drawerId:'worker-1',drafts:{}},c=context({state,$:s=>elements[s]});
 load(c,section('function captureDrawerDraft(','function planChoices('),['captureDrawerDraft']);c.captureDrawerDraft();
 assert.equal(state.drafts['worker-1'].harness,'agy');assert.equal(state.drafts['worker-1'].mode,'tui');assert.equal(state.drafts['worker-1'].message,'Preserve unsent instruction');
}
function testPlans(){
 const elements={},get=s=>elements[s]||(elements[s]=elem());get('#overlay').hidden=true;
 const worker={id:'worker-1',name:'Worker',kind:'worker',activity:'exited',isIdle:false,busy:false,replacedBy:null,model:'gpt-6-luna',harness:'codex',mode:'tui',role:'Implementation',stateLabel:'Stopped',branch:'work/a',branchState:{commits:4,unpushed:2},openPRs:2,blocked:true,prs:[]};
 const state={data:{offline:true,sessions:[worker]},pending:false,drawerId:null,agentPlansFresh:true,providerCatalogFresh:true,drafts:{'worker-1':{harness:'agy',model:'gemini-3.8-flash-high',effort:'high',mode:'tui',message:'Keep my instruction'}},agentPlans:[{id:'worker-1',status:'pending',harness:'agy',model:'gemini-3.8-flash-high',effort:'high',mode:'tui'}],providerCatalog:{providers:[{id:'agy',name:'AGY',available:true,models:[{id:'gemini-3.8-flash-high',name:'Gemini High',efforts:['high'],defaultEffort:'high',effortMode:'model-id'}]}]}};
 let unavailable=true;const c=context({state,$:get,esc,modelName:x=>x,relativeTime:()=>'',statusClass:()=>'',captureDrawerDraft:()=>{},executionUnavailable:()=>unavailable,markFreshness:()=>{},toast:()=>{},document:{body:{style:{}}}});
 load(c,section('function canSavePlan(','function markFreshness('),['canSavePlan','canApplyPlan']);load(c,section('function planChoices(','function showDrawer('),['planChoices']);load(c,section('function showDrawer(','function closeDrawer('),['showDrawer']);
 const button=a=>get('#drawerBody').innerHTML.match(new RegExp('<button[^>]*data-action="'+a+'"[^>]*>'))?.[0]||'';
 c.showDrawer(worker.id,false);assert(button('save-plan'));assert(!/disabled/.test(button('save-plan')));assert(/disabled/.test(button('apply-plan')));assert(get('#drawerBody').innerHTML.includes('Keep my instruction'));
 state.data.offline=false;unavailable=false;worker.activity='idle';worker.isIdle=true;c.showDrawer(worker.id,false);assert(!/disabled/.test(button('apply-plan')),'PR ownership must not block preserve-and-park');
 assert.equal(c.planChoices(worker).model.id,'gemini-3.8-flash-high');
 state.agentPlansFresh=false;c.showDrawer(worker.id,false);assert(/disabled/.test(button('apply-plan')));
 state.agentPlansFresh=true;state.providerCatalog.providers[0].models=[];c.showDrawer(worker.id,false);assert.equal(c.planChoices(worker).model.id,'gemini-3.8-flash-high');assert.equal(c.planChoices(worker).valid,false);assert(/disabled/.test(button('save-plan')));assert(/disabled/.test(button('apply-plan')));worker.busy=true;c.showDrawer(worker.id,false);assert(/disabled/.test(button('apply-plan')));
}
async function testRefresh(){
 let resolveRoster,reads=0,renders=0;const roster={sessions:[],summary:{},metrics:{},timestamp:new Date().toISOString()},state={pending:true,data:null,refreshPromise:null,receivedAt:0,refreshError:null,firstLoad:true,drawerId:null},elements={},get=s=>elements[s]||(elements[s]=elem());
 const c=context({state,$:get,esc,count:()=>{},toast:()=>{},renderReadiness:()=>{},updateCounts:()=>{},updateFilters:()=>{},renderTable:()=>renders++,showDrawer:()=>{},markFreshness:()=>{},api:route=>{
  if(route==='/api/state'){reads++;return new Promise(r=>{resolveRoster=r})}if(route==='/api/preflight')return Promise.resolve({ready:false,issues:[]});if(route==='/api/agent-plan')return Promise.resolve({plans:[]});return Promise.resolve({providers:[]});
 }});
 load(c,section('async function refresh(','document.addEventListener('),['refresh']);const a=c.refresh(),b=c.refresh(true);assert.equal(reads,1);resolveRoster(roster);await Promise.all([a,b]);assert.equal(renders,1);assert.equal(state.data,roster);assert(state.receivedAt>0);assert.equal(state.refreshPromise,null);assert.equal(state.agentPlansFresh,true);
 c.api=async route=>{if(route==='/api/state')throw Error('daemon down');if(route==='/api/agent-plan')throw Error('plan unavailable');return {providers:[]}};
 await c.refresh();assert.equal(state.data,roster);assert.equal(state.refreshError,'daemon down');assert.equal(state.agentPlansFresh,false);assert.equal(state.refreshPromise,null);
}
function testRealFreshnessDomContract(){
 const buttons=[{dataset:{id:'worker',action:'nudge'},disabled:false},{dataset:{id:'worker',action:'find-work'},disabled:false}];
 const state={data:{sessions:[{id:'worker',isIdle:true}]},pending:false,receivedAt:Date.now()},elements={'#connectionText':elem()};
 let blocked=true;
 const document={body:{classList:{toggle(){}}},querySelector:selector=>elements[selector]||buttons[0],querySelectorAll:()=>buttons};
 const c=context({state,document,$:selector=>document.querySelector(selector),$$:selector=>Array.from(document.querySelectorAll(selector)),rosterIsFresh:()=>true,executionUnavailable:()=>blocked,count:()=>{},relativeTime:()=>''});
 load(c,section('function markFreshness(){','const limit='),['markFreshness']);
 c.markFreshness();assert(buttons.every(b=>b.disabled),'all matching buttons disabled');
 blocked=false;c.markFreshness();assert(buttons.every(b=>!b.disabled),'controls recover after fresh eligibility');
}
(async()=>{testRealFreshnessDomContract();await testApi();testDraft();testPlans();await testRefresh();console.log('PASS dashboard deadlines/no-retry, shared-port URLs, offline staging, draft preservation, replacement gates, refresh coalescing and stale-plan handling');})().catch(e=>{console.error(e);process.exitCode=1});
