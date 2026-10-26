'use strict';

// A reviewed branch transition must refer to the same existing native AO
// workspace and Git worktree. This reader never checks out, recreates, cleans,
// resets, or edits a worktree, and it never returns native command output.
const fs=require('node:fs');
const path=require('node:path');
const {execFile}=require('node:child_process');
const {promisify}=require('node:util');
const execAsync=promisify(execFile);
const ID_RE=/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SAFE_CODES=new Set(['BRANCH_WORKSPACE_IDENTITY_INVALID','BRANCH_WORKSPACE_LOCATION_UNVERIFIED',
 'BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED','BRANCH_WORKSPACE_HEAD_UNVERIFIED',
 'BRANCH_WORKSPACE_REPOSITORY_UNVERIFIED','BRANCH_WORKSPACE_CHANGED',
 'BRANCH_WORKSPACE_INSPECTION_CANCELLED','BRANCH_WORKSPACE_INSPECTION_TIMEOUT',
 'BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE']);
function fail(code){throw Error(code)}
function windowsPathKey(value){
 if(typeof value!=='string'||!value||/[\x00-\x1f\x7f]/.test(value))fail('BRANCH_WORKSPACE_IDENTITY_INVALID');
 let normalized=value.replaceAll('/','\\');
 if(normalized.startsWith('\\\\?\\UNC\\'))normalized='\\\\'+normalized.slice(8);
 else if(normalized.startsWith('\\\\?\\'))normalized=normalized.slice(4);
 if(!path.win32.isAbsolute(normalized)||(!/^[A-Za-z]:\\/.test(normalized)&&!/^\\\\[^\\]+\\[^\\]+(?:\\|$)/.test(normalized)))fail('BRANCH_WORKSPACE_IDENTITY_INVALID');
 normalized=path.win32.normalize(normalized);
 const root=path.win32.parse(normalized).root;
 while(normalized.length>root.length&&normalized.endsWith('\\'))normalized=normalized.slice(0,-1);
 return normalized.toLowerCase();
}
async function canonicalDirectory(directory){
 if(process.platform!=='win32')fail('BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE');
 const resolved=await fs.promises.realpath(directory);
 if(!(await fs.promises.stat(resolved)).isDirectory())fail('BRANCH_WORKSPACE_LOCATION_UNVERIFIED');
 return resolved;
}
function parseWorktrees(value){
 if(typeof value!=='string'||!value.endsWith('\0\0'))fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
 const records=[];let current=null;
 for(const field of value.split('\0')){
  if(field===''){if(current){records.push(current);current=null;}continue;}
  const space=field.indexOf(' '),key=space<0?field:field.slice(0,space),value=space<0?'':field.slice(space+1);
  if(key==='worktree'){
   if(current||!value)fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
   current=Object.create(null);current.worktree=value;continue;
  }
  if(!current||!['HEAD','branch','detached','bare','locked','prunable'].includes(key)||Object.hasOwn(current,key))fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
  current[key]=value;
 }
 if(current||!records.length)fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
 return records;
}
function singleLine(value){
 if(typeof value!=='string'||!value||/[\0\r\n]/.test(value.replace(/\r?\n$/,'')))fail('BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE');
 return value.replace(/\r?\n$/,'');
}

async function verifyBranchWorkspace(entry,options={}){
 let timer,abort;
 const controller=new AbortController();
 try{
  entry=entry&&Object.freeze({id:entry.id,expectedBranch:entry.expectedBranch,workspacePath:entry.workspacePath,workspaceRepoPath:entry.workspaceRepoPath});
  if(!entry||typeof entry.id!=='string'||!ID_RE.test(entry.id)||typeof entry.expectedBranch!=='string'||!entry.expectedBranch||
   entry.expectedBranch.length>1024||entry.expectedBranch!==entry.expectedBranch.trim()||/[\x00-\x20\x7f]/.test(entry.expectedBranch))fail('BRANCH_WORKSPACE_IDENTITY_INVALID');
  const entryPathKey=windowsPathKey(entry.workspacePath);windowsPathKey(entry.workspaceRepoPath);
  const timeoutMs=options.timeoutMs??10000;
  if(!Number.isInteger(timeoutMs)||timeoutMs<1||timeoutMs>15000)fail('BRANCH_WORKSPACE_IDENTITY_INVALID');
  const now=options.now||Date.now,start=now();
  if(!Number.isFinite(start))fail('BRANCH_WORKSPACE_IDENTITY_INVALID');
  const external=options.signal;
  abort=()=>controller.abort();
  if(external?.aborted)fail('BRANCH_WORKSPACE_INSPECTION_CANCELLED');
  external?.addEventListener('abort',abort,{once:true});
  let rejectStopped;
  const stopped=new Promise((_,reject)=>{rejectStopped=reject;});
  const onAbort=()=>rejectStopped(Error(external?.aborted?'BRANCH_WORKSPACE_INSPECTION_CANCELLED':'BRANCH_WORKSPACE_INSPECTION_TIMEOUT'));
  controller.signal.addEventListener('abort',onAbort,{once:true});
  timer=setTimeout(()=>controller.abort(),timeoutMs);
  const call=fn=>{
   if(controller.signal.aborted)fail(external?.aborted?'BRANCH_WORKSPACE_INSPECTION_CANCELLED':'BRANCH_WORKSPACE_INSPECTION_TIMEOUT');
   return Promise.race([Promise.resolve().then(fn),stopped]);
  };
  const resolveDirectory=options.canonicalDirectory||canonicalDirectory;
  const canonicalKey=async value=>windowsPathKey(await call(()=>resolveDirectory(value)));
  const workspaceKey=await canonicalKey(entry.workspacePath),repoKey=await canonicalKey(entry.workspaceRepoPath);
  const fetcher=options.fetch||globalThis.fetch;
  const reader=options.execFile||execAsync;
  // Environment selectors must not redirect `git -C` to some other repository.
  const gitEnv=Object.fromEntries(Object.entries(process.env).filter(([key])=>!/^GIT_/i.test(key)));
  gitEnv.GIT_OPTIONAL_LOCKS='0';gitEnv.GIT_TERMINAL_PROMPT='0';
  const git=async(directory,args)=>{
   const result=await call(()=>reader(options.gitExecutable||'git.exe',['--no-optional-locks','-C',directory,...args],
    {windowsHide:true,shell:false,timeout:Math.min(timeoutMs,5000),maxBuffer:4*1024*1024,encoding:'utf8',env:gitEnv,signal:controller.signal}));
   if(typeof result?.stdout!=='string')fail('BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE');
   return result.stdout;
  };
  const location=async()=>{
   const response=await call(()=>fetcher('http://127.0.0.1:3001/api/v1/desktop/sessions/'+encodeURIComponent(entry.id)+'/workspace',
    {method:'GET',redirect:'error',signal:controller.signal}));
   if(!response?.ok||response.redirected)fail('BRANCH_WORKSPACE_LOCATION_UNVERIFIED');
   const body=await call(()=>response.json()),value=body?.data??body;
   if(value?.sessionId!==entry.id||typeof value.workspacePath!=='string'||await canonicalKey(value.workspacePath)!==workspaceKey)fail('BRANCH_WORKSPACE_LOCATION_UNVERIFIED');
   return windowsPathKey(value.workspacePath);
  };
  const registration=async()=>{
   const records=parseWorktrees(await git(entry.workspaceRepoPath,['worktree','list','--porcelain','-z']));
   const matches=records.filter(record=>{const key=windowsPathKey(record.worktree);return key===entryPathKey||key===workspaceKey;});
   if(matches.length!==1)fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
   const record=matches[0];
   if(record.branch!=='refs/heads/'+entry.expectedBranch||!/^([a-f0-9]{40}|[a-f0-9]{64})$/i.test(record.HEAD||'')||/^0+$/.test(record.HEAD)||
    ['detached','bare','prunable'].some(key=>Object.hasOwn(record,key))||await canonicalKey(record.worktree)!==workspaceKey)fail('BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED');
   return JSON.stringify({path:workspaceKey,head:record.HEAD,branch:record.branch,locked:record.locked??null});
  };
  const beforeLocation=await location(),beforeRegistration=await registration();
  const expectedRef='refs/heads/'+entry.expectedBranch;
  if(singleLine(await git(entry.workspacePath,['symbolic-ref','--quiet','HEAD']))!==expectedRef)fail('BRANCH_WORKSPACE_HEAD_UNVERIFIED');
  // A stale registration alone cannot prove that the .git file at that path
  // still belongs to the approved repository.
  const repoCommon=singleLine(await git(entry.workspaceRepoPath,['rev-parse','--path-format=absolute','--git-common-dir']));
  const worktreeCommon=singleLine(await git(entry.workspacePath,['rev-parse','--path-format=absolute','--git-common-dir']));
  if(await canonicalKey(repoCommon)!==await canonicalKey(worktreeCommon))fail('BRANCH_WORKSPACE_REPOSITORY_UNVERIFIED');
  const afterRegistration=await registration(),afterLocation=await location();
  if(beforeRegistration!==afterRegistration||beforeLocation!==afterLocation||
   await canonicalKey(entry.workspacePath)!==workspaceKey||await canonicalKey(entry.workspaceRepoPath)!==repoKey)fail('BRANCH_WORKSPACE_CHANGED');
  const observed=now();
  if(controller.signal.aborted)fail(external?.aborted?'BRANCH_WORKSPACE_INSPECTION_CANCELLED':'BRANCH_WORKSPACE_INSPECTION_TIMEOUT');
  if(!Number.isFinite(observed)||observed<start||observed-start>timeoutMs)fail('BRANCH_WORKSPACE_INSPECTION_TIMEOUT');
  return {verified:true,observedAt:new Date(observed).toISOString(),workspacePath:entry.workspacePath,
   workspaceRepoPath:entry.workspaceRepoPath,branch:entry.expectedBranch};
 }catch(error){
  if(SAFE_CODES.has(error?.message))throw Error(error.message);
  throw Error(options.signal?.aborted?'BRANCH_WORKSPACE_INSPECTION_CANCELLED':'BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE');
 }finally{clearTimeout(timer);options.signal?.removeEventListener('abort',abort);}
}

module.exports={verifyBranchWorkspace};
