'use strict';
const fs=require('node:fs'),path=require('node:path');
const CACHE=path.join(__dirname,'agy-usage-snapshot.json'),PATTERN=/^[a-z0-9][a-z0-9_.:-]{0,127}$/i;
const finite=x=>typeof x==='number'&&Number.isFinite(x);
const reset=x=>typeof x==='string'&&Number.isFinite(Date.parse(x))?new Date(Date.parse(x)).toISOString():null;
function normalizeAgyStatusline(payload,now=Date.now()){
 if(!payload||payload.product!=='antigravity'||!payload.quota||typeof payload.quota!=='object'||Array.isArray(payload.quota))throw Error('No native AGY quota payload');
 const windows=[];
 for(const [bucket,q] of Object.entries(payload.quota).slice(0,64)){
  if(!PATTERN.test(bucket)||!q||typeof q!=='object'||Array.isArray(q))continue;
  const disabled=q.disabled===true,f=finite(q.remaining_fraction)&&q.remaining_fraction>=0&&q.remaining_fraction<=1?q.remaining_fraction:null;
  windows.push({bucket,label:bucket,disabled,remainingPercent:disabled||f===null?null:Math.round(f*10000)/100,remainingAmount:!disabled&&finite(q.remaining_amount)&&q.remaining_amount>=0?q.remaining_amount:null,resetsAt:reset(q.reset_time)});
 }
 // Whitelist only quota. Never retain email, paths, IDs, prompts or credentials.
 return {schemaVersion:1,provider:'agy',observedAt:new Date(now).toISOString(),providerSampledAt:null,windows};
}
function saveAgyObservation(payload,{cachePath=CACHE,now=Date.now()}={}){
 const value=normalizeAgyStatusline(payload,now);let previous=null;
 try{previous=JSON.parse(fs.readFileSync(cachePath,'utf8'))}catch{}
 const prior=Date.parse(previous?.observedAt);
 if(previous?.schemaVersion===1&&previous.provider==='agy'&&Number.isFinite(prior)&&prior<=now&&now-prior<5000&&JSON.stringify(previous.windows)===JSON.stringify(value.windows))return previous;
 const tmp=cachePath+'.'+process.pid+'.tmp';
 try{fs.writeFileSync(tmp,JSON.stringify(value));fs.renameSync(tmp,cachePath)}finally{try{fs.unlinkSync(tmp)}catch{}}
 return value;
}
function sanitizeWindows(windows){
 return windows.filter(w=>w&&typeof w.bucket==='string'&&PATTERN.test(w.bucket)).slice(0,64).map(w=>({bucket:w.bucket,label:w.bucket,disabled:w.disabled===true,remainingPercent:w.disabled!==true&&finite(w.remainingPercent)&&w.remainingPercent>=0&&w.remainingPercent<=100?w.remainingPercent:null,remainingAmount:w.disabled!==true&&finite(w.remainingAmount)&&w.remainingAmount>=0?w.remainingAmount:null,resetsAt:reset(w.resetsAt)}));
}
function readGeminiUsage(capabilities={},options={}){
 const now=options.now??Date.now();let snapshot=null;
 try{snapshot=JSON.parse(fs.readFileSync(options.cachePath||CACHE,'utf8'))}catch{}
 let agy={id:'agy',name:'Antigravity CLI',status:capabilities.agyAvailable?'unavailable':'not_detected',fresh:false,sampledAt:null,observedAt:null,ageSeconds:null,remainingPercent:null,windows:[],source:'AGY native statusline snapshot',freshnessBasis:'client observation; provider sample time unavailable',reason:capabilities.agyAvailable?'Waiting for a native AGY quota observation. Open /usage in AGY to refresh its backend quota.':'Antigravity CLI not detected.',nativeCommand:'/usage',docsUrl:'https://antigravity.google/docs/cli/commands/usage',automaticStopSupported:false};
 const observed=Date.parse(snapshot?.observedAt);
 if(capabilities.agyAvailable&&snapshot?.schemaVersion===1&&snapshot.provider==='agy'&&Number.isFinite(observed)&&observed<=now+5000&&Array.isArray(snapshot.windows)){
  const ageSeconds=Math.max(0,Math.floor((now-observed)/1000)),windows=sanitizeWindows(snapshot.windows),active=windows.filter(w=>!w.disabled),percentages=active.filter(w=>w.remainingPercent!==null).map(w=>w.remainingPercent),hasData=active.some(w=>w.remainingPercent!==null||w.remainingAmount!==null),fresh=hasData&&ageSeconds<=90;
  agy={...agy,status:hasData?(fresh?'observed':'stale'):'unavailable',fresh,observedAt:snapshot.observedAt,ageSeconds,remainingPercent:fresh&&percentages.length?Math.min(...percentages):null,windows,reason:hasData?(fresh?'Native CLI observation; provider sampling time is not supplied.':'Last CLI observation is stale. Open /usage in AGY to refresh.'):'AGY supplied no active numerical quota buckets.'};
 }
 const gemini={id:'gemini',name:'Gemini CLI (separate client)',status:capabilities.geminiAvailable?'unavailable':'not_detected',fresh:false,sampledAt:null,observedAt:null,remainingPercent:null,windows:[],source:'Gemini CLI /stats model',reason:capabilities.geminiAvailable?'No verified standalone quota telemetry adapter. Use /stats model in Gemini CLI.':'Gemini CLI not found on PATH. Antigravity quota is displayed separately.',nativeCommand:'/stats model',automaticStopSupported:false};
 return {at:new Date(now).toISOString(),providers:[agy,gemini],warning:'AGY data is a native client observation, not an independently polled billing sample. Google automatic percentage cutoff is not verified. Unknown quota is not unused quota.'};
}
async function statuslineMain(){
 let raw='';for await(const chunk of process.stdin){raw+=chunk;if(raw.length>262144)throw Error('Payload too large')}
 const value=saveAgyObservation(JSON.parse(raw)),parts=value.windows.filter(w=>!w.disabled).flatMap(w=>w.remainingPercent!==null?[w.bucket+': '+w.remainingPercent+'%']:w.remainingAmount!==null?[w.bucket+': '+w.remainingAmount+' remaining']:[]);
 process.stdout.write(parts.length?'Fleet quota | '+parts.join(' | '):'Fleet quota | awaiting native usage');
}
if(require.main===module&&process.argv[2]==='--statusline')statuslineMain().catch(()=>process.stdout.write('Fleet quota | unavailable'));
module.exports={normalizeAgyStatusline,saveAgyObservation,readGeminiUsage};
