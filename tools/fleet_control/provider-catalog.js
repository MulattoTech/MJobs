'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),{execFile}=require('node:child_process'),{promisify}=require('node:util');
const exec=promisify(execFile);
const AGY_NAMES={'gemini-3.8-flash-low':'Gemini 3.8 Flash · Low','gemini-3.8-flash-medium':'Gemini 3.8 Flash · Medium','gemini-3.8-flash-high':'Gemini 3.8 Flash · High','claude-sonnet-4-6':'Claude Sonnet 4.6','claude-opus-4-6-thinking':'Claude Opus 4.6 Thinking','gpt-oss-120b-medium':'GPT-OSS 120B · Medium'};
function agyModel(id){const m=/^gemini-3\.8-flash-(low|medium|high)$/.exec(id),fixed=m?m[1]:id==='gpt-oss-120b-medium'?'medium':'default';return {id,name:AGY_NAMES[id],efforts:[fixed],defaultEffort:fixed,effortMode:'model-id'};}
function discoverProviderCatalog(evidence={}){
 const codexModels=(evidence.codexModels||[]).map(m=>({id:m.id,name:m.name,efforts:[...(evidence.codexEfforts||[])],defaultEffort:'medium',effortMode:'flag'}));
 const agyModels=[...new Set(evidence.agyModelIds||[])].filter(id=>Object.hasOwn(AGY_NAMES,id)).map(agyModel);
 return {at:evidence.observedAt||new Date().toISOString(),source:'Locally verified CLI capabilities',providers:[
 {id:'codex',name:'Codex',available:evidence.codexAvailable!==false&&codexModels.length>0,modes:['tui','chat'],models:codexModels,error:evidence.codexAvailable===false?'Codex executable not found':null},
 {id:'agy',name:'Antigravity CLI',available:!!evidence.agyAvailable&&agyModels.length>0,version:evidence.agyVersion||null,modes:['tui','chat'],models:agyModels,error:evidence.error||(evidence.agyAvailable?(agyModels.length?null:'No validated AGY models'):'Antigravity CLI not found')},
 {id:'gemini',name:'Gemini CLI (separate from AGY)',available:false,installed:!!evidence.geminiAvailable,modes:[],models:[],error:evidence.geminiAvailable?'CLI detected; model/effort adapter not yet verified':'Gemini CLI not found on PATH'}]};
}
function validateSelection(input,catalog){
 if(!input||typeof input!=='object')throw Error('Agent selection is required');
 const harness=input.harness||input.provider,p=catalog?.providers?.find(x=>x.id===harness);
 if(!p||!p.available)throw Error(p?.error||'Provider is not available');
 const m=p.models.find(x=>x.id===input.model);if(!m)throw Error('Model is not in the verified provider catalog');
 const effort=input.effort==null?m.defaultEffort:input.effort;
 if(typeof effort!=='string'||!m.efforts.includes(effort))throw Error('Reasoning level is not supported by this model');
 const mode=input.mode||'tui';if(!['tui','chat'].includes(mode))throw Error('Mode must be tui or chat');
 return {harness,model:m.id,effort,mode,effortMode:m.effortMode,launchEffort:m.effortMode==='flag'?effort:null};
}
function buildProviderArgs(input,catalog){const s=validateSelection(input,catalog);return ['--harness',s.harness,'--model',s.model,...(s.launchEffort?['--effort',s.launchEffort]:[])];}
function createProviderRuntime({codexModels,codexEfforts,codexExe,now=Date.now,run=exec}){
 let cache=null,inflight=null;
 const agy=path.join(os.homedir(),'AppData','Local','Microsoft','WinGet','Packages','Google.AntigravityCLI_Microsoft.Winget.Source_8wekyb3d8bbwe','agy.exe');
 async function get(){
  const cacheMs=cache?.providers?.find(p=>p.id==='agy')?.available?30*60000:30000;
  if(cache&&now()-Date.parse(cache.at)<cacheMs)return cache;
  if(inflight)return inflight;
  inflight=(async()=>{
   const evidence={codexModels,codexEfforts,codexAvailable:fs.existsSync(codexExe),agyAvailable:fs.existsSync(agy),agyModelIds:[],geminiAvailable:false,observedAt:new Date(now()).toISOString()};
   if(evidence.agyAvailable){try{
    const [models,version]=await Promise.all([run(agy,['models'],{windowsHide:true,timeout:8000,maxBuffer:100000}),run(agy,['--version'],{windowsHide:true,timeout:3000,maxBuffer:20000})]);
    evidence.agyModelIds=models.stdout.split(/\r?\n/).map(s=>s.trim().split(/\s+/)[0]).filter(id=>Object.hasOwn(AGY_NAMES,id));
    evidence.agyVersion=version.stdout.trim();
   }catch(e){evidence.error='AGY model discovery unavailable: '+String(e.code||e.message).slice(0,140);evidence.agyAvailable=false;}}
   cache=discoverProviderCatalog(evidence);return cache;
  })().finally(()=>{inflight=null});
  return inflight;
 }
 return {get,refresh(){cache=null;return get();}};
}
module.exports={discoverProviderCatalog,validateSelection,buildProviderArgs,createProviderRuntime};
