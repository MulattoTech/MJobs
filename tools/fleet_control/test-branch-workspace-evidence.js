'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {verifyBranchWorkspace}=require('./branch-workspace-evidence');
const NOW=Date.parse('2026-10-26T01:00:00.000Z');
const clone=value=>structuredClone(value);
function worktree(path,branch='reviewed/actual',head='a'.repeat(40),extra=[]){
 return ['worktree '+path,'HEAD '+head,'branch refs/heads/'+branch,...extra,'',''].join('\0');
}
function fixture(){
 const entry={id:'mjobs-luna-20',expectedBranch:'reviewed/actual',workspacePath:'C:\\AO Worktrees\\mjobs-luna-20',workspaceRepoPath:'C:\\Tools\\Dev\\MJobs'};
 const state={calls:[],locations:[],paths:[],registrations:[],heads:[],commons:[],onGit:null,onPath:null,onFetch:null};
 const registered=worktree('C:/Tools/Dev/MJobs','main')+worktree('C:/AO Worktrees/mjobs-luna-20');
 const options={now:()=>NOW,canonicalDirectory:async value=>{state.paths.push(value);if(state.onPath)return state.onPath(value);return value;},
  fetch:async(url,opts)=>{
   state.locations.push({url,opts});if(state.onFetch)return state.onFetch(url,opts);
   return {ok:true,redirected:false,json:async()=>state.locationBody||{data:{sessionId:entry.id,workspacePath:entry.workspacePath}}};
  },
  execFile:async(binary,args,opts)=>{
   state.calls.push({binary,args:clone(args),opts});if(state.onGit)await state.onGit(args);
   const command=args.slice(3);
   if(command.join(' ')==='worktree list --porcelain -z')return {stdout:state.registrations.length?state.registrations.shift():registered};
   if(command.join(' ')==='symbolic-ref --quiet HEAD')return {stdout:state.heads.length?state.heads.shift():'refs/heads/reviewed/actual\r\n'};
   if(command.join(' ')==='rev-parse --path-format=absolute --git-common-dir')return {stdout:state.commons.length?state.commons.shift():'C:/Tools/Dev/MJobs/.git\n'};
   throw Error('Unexpected command');
  }};
 return {entry,state,options,registered,run:()=>verifyBranchWorkspace(entry,options)};
}
test('existing registered workspace yields exact reviewed proof with only read-only commands',async()=>{
 const f=fixture(),proof=await f.run();
 assert.deepEqual(proof,{verified:true,observedAt:new Date(NOW).toISOString(),workspacePath:f.entry.workspacePath,workspaceRepoPath:f.entry.workspaceRepoPath,branch:f.entry.expectedBranch});
 assert.equal(f.state.locations.length,2);assert.equal(f.state.calls.length,5);
 for(const call of f.state.locations){assert.equal(call.url,'http://127.0.0.1:3001/api/v1/desktop/sessions/mjobs-luna-20/workspace');assert.equal(call.opts.method,'GET');assert.equal(call.opts.redirect,'error');}
 for(const call of f.state.calls){assert.equal(call.binary,'git.exe');assert.equal(call.opts.shell,false);assert.equal(call.args[0],'--no-optional-locks');assert.equal(call.opts.env.GIT_OPTIONAL_LOCKS,'0');assert.equal(call.opts.env.GIT_TERMINAL_PROMPT,'0');
  assert.ok(['worktree list --porcelain -z','symbolic-ref --quiet HEAD','rev-parse --path-format=absolute --git-common-dir'].includes(call.args.slice(3).join(' ')));}
 assert.equal(f.state.calls.some(call=>call.args.some(arg=>['checkout','reset','clean','config','add','prune','repair'].includes(arg))),false);
});
test('case, separators, spaces and extended Windows path prefix compare by canonical directory',async()=>{
 const f=fixture();f.state.locationBody={sessionId:f.entry.id,workspacePath:'\\\\?\\C:\\ao worktrees\\mjobs-luna-20\\'};
 assert.equal((await f.run()).verified,true);
});
test('invalid identity or relative path is rejected before any native lookup',async t=>{
 for(const [field,value]of [['id','../other'],['id',123],['expectedBranch','bad\nbranch'],['workspacePath','C:relative'],['workspaceRepoPath','relative'],['workspacePath','\\Device\\other']])await t.test(field+String(value),async()=>{
  const f=fixture();f.entry[field]=value;await assert.rejects(f.run(),/BRANCH_WORKSPACE_IDENTITY_INVALID/);assert.equal(f.state.calls.length,0);assert.equal(f.state.locations.length,0);
 });
});
test('desktop route must return the exact session and canonical approved existing path',async t=>{
 for(const change of ['wrong-id','wrong-path','missing-path','http-error','redirect','unreadable-directory'])await t.test(change,async()=>{
  const f=fixture();
  if(change==='wrong-id')f.state.locationBody={sessionId:'other-id',workspacePath:f.entry.workspacePath};
  if(change==='wrong-path')f.state.locationBody={sessionId:f.entry.id,workspacePath:'C:\\Other'};
  if(change==='missing-path')f.state.locationBody={sessionId:f.entry.id};
  if(change==='http-error')f.state.onFetch=async()=>({ok:false,json:async()=>({})});
  if(change==='redirect')f.state.onFetch=async()=>({ok:true,redirected:true,json:async()=>({})});
  if(change==='unreadable-directory')f.state.onPath=async()=>{throw Error('raw secret path or native error');};
  await assert.rejects(f.run(),error=>/^BRANCH_WORKSPACE_[A-Z_]+$/.test(error.message)&&!error.message.includes('secret'));
 });
});
test('registration must be unique, attached, present and on the exact reviewed branch',async t=>{
 const cases={
  missing:worktree('C:/Other'),wrongBranch:worktree('C:/AO Worktrees/mjobs-luna-20','other'),
  duplicate:worktree('C:/AO Worktrees/mjobs-luna-20')+worktree('c:/ao worktrees/mjobs-luna-20'),
  detached:worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','a'.repeat(40),['detached']),
  bare:worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','a'.repeat(40),['bare']),
  prunable:worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','a'.repeat(40),['prunable missing gitdir']),
  malformed:'worktree C:/AO Worktrees/mjobs-luna-20\nbranch refs/heads/reviewed/actual\n',
  duplicateBranch:worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','a'.repeat(40),['branch refs/heads/reviewed/actual']),
  unborn:worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','0'.repeat(40)),
 };
 for(const [name,registration]of Object.entries(cases))await t.test(name,async()=>{const f=fixture();f.state.registrations=[registration];await assert.rejects(f.run(),/BRANCH_WORKSPACE_REGISTRATION_UNVERIFIED/);});
});
test('a current symbolic HEAD mismatch blocks adoption',async()=>{
 const f=fixture();f.state.heads=['refs/heads/different\n'];await assert.rejects(f.run(),/BRANCH_WORKSPACE_HEAD_UNVERIFIED/);
});
test('a stale registration whose local .git now names another repository is rejected',async()=>{
 const f=fixture();f.state.commons=['C:/Tools/Dev/MJobs/.git\n','C:/OtherRepo/.git\n'];await assert.rejects(f.run(),/BRANCH_WORKSPACE_REPOSITORY_UNVERIFIED/);
});
test('registration HEAD, native workspace location, or canonical directory changes during inspection reject proof',async t=>{
 for(const change of ['head','location','canonical-workspace','canonical-repository'])await t.test(change,async()=>{
  const f=fixture();
  if(change==='head')f.state.registrations=[f.registered,worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','b'.repeat(40))];
  if(change==='location')f.state.onFetch=async()=>({ok:true,json:async()=>({sessionId:f.entry.id,workspacePath:f.state.locations.length===1?f.entry.workspacePath:'C:\\Other'})});
  if(change.startsWith('canonical-')){const watched=change==='canonical-workspace'?f.entry.workspacePath:f.entry.workspaceRepoPath;let count=0;f.state.onPath=value=>{if(value===watched&&++count>1)return 'C:\\Swapped';return value;};}
  await assert.rejects(f.run(),error=>/^BRANCH_WORKSPACE_[A-Z_]+$/.test(error.message));
 });
});
test('locked existing worktree and SHA-256 HEAD remain valid without any lock mutation',async()=>{
 const f=fixture();const data=worktree('C:/AO Worktrees/mjobs-luna-20','reviewed/actual','a'.repeat(64),['locked owner reason']);f.state.registrations=[data,data];assert.equal((await f.run()).verified,true);
});
test('native errors never expose their raw command output',async()=>{
 const f=fixture();f.state.onGit=()=>{const error=Error('secret native stderr');error.stdout='sensitive output';throw error;};
 await assert.rejects(f.run(),error=>error.message==='BRANCH_WORKSPACE_INSPECTION_UNAVAILABLE'&&!Object.hasOwn(error,'stdout'));
});
test('caller cancellation prevents work and timeout bounds a nonresponsive dependency',async()=>{
 const f=fixture(),controller=new AbortController();controller.abort();f.options.signal=controller.signal;
 await assert.rejects(f.run(),/BRANCH_WORKSPACE_INSPECTION_CANCELLED/);assert.equal(f.state.calls.length,0);
 const g=fixture();g.options.timeoutMs=20;g.state.onFetch=()=>new Promise(()=>{});
 await assert.rejects(g.run(),/BRANCH_WORKSPACE_INSPECTION_TIMEOUT/);
});
test('an invalid or backwards clock cannot produce a fresh proof',async()=>{
 const f=fixture();let count=0;f.options.now=()=>count++?NOW-1:NOW;await assert.rejects(f.run(),/BRANCH_WORKSPACE_INSPECTION_TIMEOUT/);
});
