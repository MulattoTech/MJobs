'use strict';
// Evidence-backed, bounded maintenance work. No provider call, Git write or
// invented defect is performed by this planner. Its artifacts require review.
const fs=require('node:fs'),path=require('node:path'),{randomUUID}=require('node:crypto');
const ID=/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/,SHA=/^[a-f0-9]{40}$/;
const safePath=v=>typeof v==='string'&&/^(services|apps|tools|tests)\/[A-Za-z0-9_./-]+\.(py|js|ts|tsx)$/.test(v)&&!v.split('/').includes('..');
function read(file,fallback){try{const stat=fs.statSync(file);if(stat.size>4*1024*1024)throw Error('oversized');return JSON.parse(fs.readFileSync(file,'utf8').replace(/^\uFEFF/,''));}catch(e){if(e.code==='ENOENT')return fallback;throw Error('Productive-work evidence is unreadable');}}
class ProductiveWork{
 constructor(root){this.root=root;this.file=path.join(root,'productive-work-state.json');this.catalogFile=path.join(root,'productive-catalog.json');this.error=null;}
 load(){const value=read(this.file,{version:1,tasks:{}});if(value.version!==1||!value.tasks||Array.isArray(value.tasks)||Object.keys(value.tasks).length>2000)throw Error('Invalid productive-work ledger');
  for(const [key,t]of Object.entries(value.tasks)){
   if(!t||!ID.test(t.agentId||'')||!safePath(t.sourcePath)||!SHA.test(t.blob||'')||!SHA.test(t.ref||'')||key!=='productive:reference-v1:'+t.blob||!['reserved','submitted','uncertain','artifact_ready','no_new_gap'].includes(t.state))throw Error('Invalid productive task record');
   const directory=path.join(this.root,'artifacts','productive-work',t.agentId),stem=t.blob.slice(0,16);
   if(t.artifactPath!==path.join(directory,stem+'.md')||t.resultPath!==path.join(directory,stem+'.result.json'))throw Error('Invalid artifact scope');
   for(const file of [t.artifactPath,t.resultPath]){try{if(fs.lstatSync(file).isSymbolicLink()||!fs.realpathSync(file).startsWith(fs.realpathSync(this.root)+path.sep))throw Error('Artifact redirected outside its scope');}catch(e){if(e.code!=='ENOENT')throw e;}}
  }return value;}
 save(value){const temp=this.file+'.'+randomUUID()+'.tmp';fs.writeFileSync(temp,JSON.stringify(value,null,2),{flag:'wx'});fs.renameSync(temp,this.file);}
 catalog(){const value=read(this.catalogFile,null);if(!value||value.version!==1||!SHA.test(value.baseCommit||'')||!Array.isArray(value.entries)||value.entries.length>1000)return [];
  const seen=new Set();return value.entries.filter(e=>safePath(e.path)&&SHA.test(e.blob||'')&&Number.isInteger(e.missingDocstrings)&&e.missingDocstrings>0&&!seen.has(e.blob)&&seen.add(e.blob)).map(e=>({...e,ref:value.baseCommit,key:'productive:reference-v1:'+e.blob}));}
 observe(){const data=this.load();let changed=false;
  for(const task of Object.values(data.tasks)){
   if(!['submitted','reserved'].includes(task.state))continue;
   const result=read(task.resultPath,null);if(!result)continue;
   if(result.sourceBlob!==task.blob||result.sourcePath!==task.sourcePath||!['documented','no_new_gap'].includes(result.outcome)||!Array.isArray(result.references)||result.references.length<1||result.references.length>30)continue;
   if(result.outcome==='documented'){
    let stat;try{stat=fs.statSync(task.artifactPath);}catch{continue;}if(!stat.isFile()||stat.size<300||stat.size>200000)continue;
    const text=fs.readFileSync(task.artifactPath,'utf8');if(!text.includes(task.sourcePath)||!text.includes(task.blob))continue;
   }
   task.state=result.outcome==='documented'?'artifact_ready':'no_new_gap';task.completedAt=new Date().toISOString();task.acceptance='unreviewed';changed=true;
  }if(changed)this.save(data);return data;
 }
 candidates(sessions,{signals,now=Date.now(),reserveWorkers=0}={}){
  try{
   const data=this.observe(),catalog=this.catalog(),reserved=new Set(sessions.filter(s=>s.kind==='worker'&&/review/i.test(s.role||'')&&!s.isTerminated&&!s.replacedBy).sort((a,b)=>a.id.localeCompare(b.id)).slice(0,reserveWorkers).map(s=>s.id));
   const used=new Set(Object.keys(data.tasks)),result=[];
   for(const session of sessions){
    if(!ID.test(session.id||'')||session.kind!=='worker'||session.busy||session.isTerminated||session.replacedBy||!session.isIdle||!['idle','waiting_input'].includes(session.activity)||reserved.has(session.id))continue;
    if(Object.values(data.tasks).some(t=>t.agentId===session.id&&['reserved','submitted','uncertain'].includes(t.state)))continue;
    const entry=catalog.find(e=>!used.has(e.key)&&!signals?.seen(e.key));if(!entry)continue;used.add(entry.key);
    const directory=path.join(this.root,'artifacts','productive-work',session.id),stem=entry.blob.slice(0,16),artifactPath=path.join(directory,stem+'.md'),resultPath=path.join(directory,stem+'.result.json');
    const task={key:entry.key,agentId:session.id,sourcePath:entry.path,blob:entry.blob,ref:entry.ref,artifactPath,resultPath,createdAt:new Date(now).toISOString(),state:'proposed',missingDocstrings:entry.missingDocstrings};
    task.message='USER-AUTHORIZED PRODUCTIVE MAINTENANCE: Produce a useful maintainer reference for '+entry.path+' at source commit '+entry.ref+' (blob '+entry.blob+'). A deterministic AST inventory found '+entry.missingDocstrings+' public definitions without docstrings; this is a documentation opportunity, NOT proof of a bug. Read the exact source with git show '+entry.ref+':'+entry.path+' and its nearby tests/readme. Do not rerun CI or rewrite production code. Explain actual entry points, inputs/outputs, dependencies, error behavior, and safe examples, with exact source-line references. Only document what the source proves. Check whether existing documentation already covers it; when it does, record no_new_gap rather than duplicate prose. You have EXCLUSIVE WRITE ownership only of '+artifactPath+' and '+resultPath+'. Create their parent directory as needed. These are private draft artifacts outside the Git worktree; leave any existing PR head, tests and files untouched. Do not create a new PR, commit, merge, launch agents or change your current issue ownership. Work once, bounded to approximately 10 minutes; deliver a useful draft, not filler or repeated status. Include the exact source path and full blob SHA in the draft. Finish by writing the result JSON with sourceBlob, sourcePath, outcome (documented or no_new_gap), and references (actual source/test paths and line ranges). Never claim accepted or tested unless independently verified. Then send primary the artifact location and at most six lines of changed findings; do not ask for the same assignment again.';
    result.push(task);
   }
   this.error=null;return {tasks:result,reserveIds:[...reserved],catalogCount:catalog.length};
  }catch(e){this.error=e.message;return {tasks:[],reserveIds:[],catalogCount:0,error:e.message};}
 }
 reserve(task,now=Date.now()){
  if(!ID.test(task.agentId||'')||!safePath(task.sourcePath)||!SHA.test(task.blob)||task.key!=='productive:reference-v1:'+task.blob)throw Error('Invalid productive task');
  const data=this.load();if(data.tasks[task.key])return false;
  if(Object.values(data.tasks).some(x=>x.agentId===task.agentId&&['reserved','submitted','uncertain'].includes(x.state)))return false;
  const base=path.join(this.root,'artifacts','productive-work',task.agentId),stem=task.blob.slice(0,16);
  if(task.artifactPath!==path.join(base,stem+'.md')||task.resultPath!==path.join(base,stem+'.result.json'))throw Error('Artifact scope mismatch');
  fs.mkdirSync(base,{recursive:true});const {message,...record}=task;data.tasks[task.key]={...record,state:'reserved',assignedAt:new Date(now).toISOString()};this.save(data);return true;
 }
 finish(key,state){if(!['submitted','uncertain'].includes(state))throw Error('Invalid task result');const data=this.load();if(!data.tasks[key])throw Error('Unreserved task');data.tasks[key].state=state;this.save(data);}
 summary(){try{const data=this.load(),tasks=Object.values(data.tasks);return {available:!this.error,error:this.error,catalogCount:this.catalog().length,assigned:tasks.filter(t=>['reserved','submitted'].includes(t.state)).length,artifactsReady:tasks.filter(t=>t.state==='artifact_ready').length,noNewGap:tasks.filter(t=>t.state==='no_new_gap').length,tasks:tasks.map(t=>({id:t.key,agentId:t.agentId,sourcePath:t.sourcePath,sourceCommit:t.ref,sourceBlob:t.blob,state:t.state,assignedAt:t.assignedAt,completedAt:t.completedAt||null,acceptance:t.acceptance||'not_reviewed'}))};}catch{return {available:false,error:'Productive-work ledger unavailable',tasks:[]};}}
}
module.exports={ProductiveWork,safePath};
