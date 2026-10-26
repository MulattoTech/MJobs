'use strict';

const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {RestoreFleet,INITIAL_STATE}=require('./restore-fleet');
const {StartupAdapter}=require('./startup-adapter');
const {StartupController}=require('./startup-control');
const {MasterControls}=require('./master-controls');
const {configRevision}=require('./quota-guard');
const {digest,readBranchAdoptions,makeBranchReceipt}=require('./branch-adoptions');
const {readPostSubmissionReview}=require('./post-submission-review');
const {completeReviewedRestore}=require('./post-review-control');
const clone=value=>structuredClone(value);

function fixture(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-post-review-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const s={now:Date.now(),safe:true,locked:false,commands:[],events:[],workspaceCalls:0,nativeCalls:0,onWorkspace:null,onNative:null,onSessions:null};
 const at=delta=>new Date(s.now+delta).toISOString();
 const make=(id,kind)=>({id,projectId:'mjobs-luna',kind,harness:'codex',mode:'tui',model:'preserved-model',createdAt:at(-86400000),
  branch:'preserved/'+id,activity:'idle',isTerminated:false,displayName:id});
 const primary=make('mjobs-primary','orchestrator'),worker=make('mjobs-worker','worker');
 const snapshot={at:at(-600000),sessions:[primary,worker]};
 s.live=snapshot.sessions.map(value=>({...clone(value),activity:{state:'idle'},controllerRunning:true,terminalGeneration:'generation-one'}));
 s.live[1].branch='existing/dirty-worker';s.counts=new Map(s.live.map(value=>[value.id,3]));
 const health=(session,count)=>session?.isTerminated===false&&session.activity?.state==='idle'&&Number.isInteger(count)&&count>0;
 fs.writeFileSync(path.join(root,'restore-snapshot.json'),JSON.stringify(snapshot));
 fs.writeFileSync(path.join(root,'emergency-latch.json'),JSON.stringify({enabled:false}));
 const runId='post-review-startup-001',fence={at:at(-120000),command:'restore'};
 const ledger={...clone(INITIAL_STATE),active:false,phase:'paused',startedAt:at(-300000),desiredIds:snapshot.sessions.map(value=>value.id),
  source:{startupRunId:runId,snapshotAt:snapshot.at,targets:clone(snapshot.sessions)},
  inflight:{[worker.id]:fence},errors:{[worker.id]:{uncertain:true,message:'Earlier completion was not yet verified',at:at(-110000)}}};
 delete ledger.postRestoreReviewReceipts;
 fs.writeFileSync(path.join(root,'restore-state.json'),JSON.stringify(ledger));
 const event={time:at(-90000),event:'restore_resume_submitted',id:worker.id,project:worker.projectId,action:'restore'};
 const auditLine=JSON.stringify(event);fs.writeFileSync(path.join(root,'audit.jsonl'),auditLine+'\n');
 const nativeRuntime={nativeSessionId:'11111111-2222-4333-8444-555555555555',providerPid:2002,providerCreatedAt:at(-119000),
  providerExecutable:'C:\\Users\\Fixture\\Codex\\codex.exe',hostPid:2001,hostCreatedAt:at(-119900),terminalGeneration:'generation-one'};
 const entry={id:worker.id,projectId:worker.projectId,kind:worker.kind,harness:worker.harness,mode:worker.mode,model:worker.model,
  createdAt:worker.createdAt,branch:worker.branch,expectedBranch:s.live[1].branch,workspacePath:'C:\\Worktrees\\mjobs-worker',workspaceRepoPath:'C:\\Repos\\MJobs',
  inflight:clone(fence),completionAudit:{time:event.time,lineSha256:digest(auditLine)},nativeRuntime};
 fs.mkdirSync(path.join(root,'updates'));
 const original=[{id:worker.id,agent_session_id:nativeRuntime.nativeSessionId,workspace_path:entry.workspacePath,model:worker.model,branch:worker.branch,
  created_at:worker.createdAt.replace('T',' ').replace('Z',' +0000 UTC')}];
 const originalFile=path.join(root,'updates','preserved-before.json');fs.writeFileSync(originalFile,JSON.stringify(original));
 fs.utimesSync(originalFile,new Date(at(-420000)),new Date(at(-420000)));
 const beforeEvidenceAt=fs.statSync(originalFile).mtime.toISOString();
 const history={schemaVersion:1,kind:'post-submission-native-history-evidence',sessions:[{...clone(entry),beforeNativeSessionId:nativeRuntime.nativeSessionId,
  resumedNativeSessionId:nativeRuntime.nativeSessionId,activeRolloutPresent:true,registeredWorkspacePresent:true,beforeEvidenceAt,afterObservedAt:at(-15000),
  beforeProvenance:{timestampSource:'filesystem-last-write-time',file:'updates/preserved-before.json',sha256:digest(fs.readFileSync(originalFile)),
   lastWriteTimeUtc:beforeEvidenceAt,reviewReference:'Fixture preserved bytes and file timestamp reviewed against capture sequence'}}]};
 const historyFile=path.join(root,'post-restore-history-evidence.json');fs.writeFileSync(historyFile,JSON.stringify(history));
 const review={schemaVersion:1,kind:'post-submission-workspace-review',reviewedAt:at(-10000),reviewReference:'Explicit review after completed native command',
  startupRunId:runId,restoreStartedAt:ledger.startedAt,snapshotAt:snapshot.at,snapshotSha256:digest(fs.readFileSync(path.join(root,'restore-snapshot.json'))),
  sourceSha256:digest(ledger.source),desiredIdsSha256:digest(ledger.desiredIds),restoreStateSha256:digest(fs.readFileSync(path.join(root,'restore-state.json'))),
  historyEvidence:{file:path.basename(historyFile),sha256:digest(fs.readFileSync(historyFile))},entries:[entry]};
 const reviewFile=path.join(root,'restore-post-submission-review.json');
 const writeReview=()=>fs.writeFileSync(reviewFile,JSON.stringify(review));writeReview();
 const nativeProof=entry=>({verified:true,observedAt:new Date(s.now).toISOString(),...Object.fromEntries(Object.entries(entry.nativeRuntime).filter(([key])=>key!=='terminalGeneration'))});
 const getSessions=async()=>{if(s.onSessions)await s.onSessions();return clone(s.live);};
 const restore=new RestoreFleet({root,autoSchedule:false,now:()=>s.now,getGlobalSessions:getSessions,runtimeHealth:health,locked:()=>s.locked,
  command:async args=>{s.commands.push(args);throw Error('No native command is authorized in this test');},audit:(event,value)=>s.events.push({event,...value}),
  runtimeInspector:async()=>new Map(s.counts),workspaceVerifier:async value=>{s.workspaceCalls++;if(s.onWorkspace)await s.onWorkspace();
   return {verified:true,observedAt:new Date(s.now).toISOString(),workspacePath:value.workspacePath,workspaceRepoPath:value.workspaceRepoPath,branch:value.expectedBranch};},
  reviewedRuntimeVerifier:async value=>{s.nativeCalls++;if(s.onNative)await s.onNative();return nativeProof(value);}});
 const assertSafe=()=>{if(!s.safe||s.locked)throw Error('Protection changed');};
 const complete=()=>restore.completeReviewed({confirm:'COMPLETE REVIEWED RESTORE',runId,reviewRevision:digest(fs.readFileSync(reviewFile))},{assertSafe});
 const config={enabled:true,autoKillArmed:true,tripped:false,pollSeconds:5,thresholdPercent:7,window:'any',updatedAt:at(-1000)};
 const quota={config:()=>clone(config),dispatchBlockReason:()=>s.safe?null:'Guard unavailable',status:()=>({fresh:true,selectedRemainingPercent:100,
  ordinaryUsageAllowed:true,config:clone(config),guardAcknowledged:true,guardAck:{revision:configRevision(config)}})};
 const master={restore,sustain:{config:{enabled:false}},turbo:{config:{enabled:false}},drainState:{enabled:false},locked:()=>s.locked};
 const launcher={status:()=>({pending:false,requiresReconciliation:false}),reconcile:()=>({pending:false,requiresReconciliation:false})};
 const adapter=new StartupAdapter({root,master,quota,launcher,getSessions,runtimeHealth:health,now:()=>s.now});
 return {root,s,restore,entry,review,reviewFile,history,historyFile,originalFile,snapshot,ledger,runId,writeReview,complete,assertSafe,master,quota,launcher,adapter,config,at};
}

test('a review file alone cannot adopt an outcome; explicit completion atomically preserves provenance and clears exact fences',async t=>{
 const f=fixture(t),before=fs.readFileSync(f.restore.snapshotPath),source=clone(f.restore.state.source),fence=clone(f.restore.state.inflight[f.entry.id]),error=clone(f.restore.state.errors[f.entry.id]);
 const candidate=f.s.live[1],target=f.snapshot.sessions[1];
 assert.equal((await f.restore.runtimeTarget(target,candidate,3)).matches,false);
 assert.equal(f.s.nativeCalls,0);assert.equal((await f.adapter.readRuntimes()).verified,1);
 const result=await f.complete();assert.equal(result.phase,'complete');assert.equal(result.restoredCount,2);assert.equal(result.active,false);
 assert.deepEqual(f.restore.state.inflight,{});assert.deepEqual(f.restore.state.errors,{});assert.deepEqual(f.restore.state.source,source);
 const receipt=f.restore.state.postRestoreReviewReceipts[f.entry.id];
 assert.equal(receipt.reviewTiming,'after-native-command-succeeded');assert.deepEqual(receipt.originalFence,fence);assert.deepEqual(receipt.resolvedError,error);
 assert.equal(receipt.reviewedHistory.beforeProvenance.timestampSource,'filesystem-last-write-time');assert.deepEqual(fs.readFileSync(f.restore.snapshotPath),before);
 assert.equal(receipt.completionAudit.lineSha256,f.entry.completionAudit.lineSha256);assert.equal(f.restore.state.allowDispatchAfter,false);
 assert.equal((await f.restore.runtimeTarget(target,candidate,3)).matches,true);
 const live=await f.adapter.readRuntimes();assert.equal(live.runtimesReady,true);assert.deepEqual(live.postReviewIds,[f.entry.id]);assert.deepEqual(live.branchAdoptionIds,[]);
 const bytes=fs.readFileSync(f.restore.statePath);assert.equal((await f.complete()).idempotent,true);assert.deepEqual(fs.readFileSync(f.restore.statePath),bytes);
 assert.deepEqual(f.s.commands,[]);
});

test('completion followed by actual Startup Recheck reaches ready_paused without native replay or manifest edits',async t=>{
 const f=fixture(t),manifest=f.adapter.manifest(),before=fs.readFileSync(f.restore.snapshotPath);
 fs.writeFileSync(path.join(f.root,'startup-state.json'),JSON.stringify({schemaVersion:1,revision:'paused-startup-revision',runId:f.runId,phase:'uncertain',active:false,
  reconciliationRequired:true,requestId:'reviewed-startup-request',recentRequests:[],restoreStartAttempted:true,restoreStarted:true,
  bootstrap:{manifestRevision:manifest.revision,targetCount:2,targetIds:manifest.targets.map(value=>value.id),branchAdoptionsRevision:null}}));
 const startup=new StartupController({root:f.root,deps:f.adapter.deps(),now:()=>f.s.now});
 await completeReviewedRestore({confirm:'COMPLETE REVIEWED RESTORE',runId:f.runId,reviewRevision:digest(fs.readFileSync(f.reviewFile))},
  {master:f.master,startup,quota:f.quota,launcher:f.launcher,adapter:f.adapter,sustain:f.master.sustain,turbo:f.master.turbo,findWork:{running:false}});
 assert.equal(startup.status().phase,'uncertain');
 const result=await startup.reconcile({confirm:'RECHECK STARTUP',runId:f.runId});
 assert.equal(result.run.phase,'ready_paused');assert.equal(result.run.reconciliationRequired,false);assert.equal(result.run.restore.restoredCount,2);
 assert.deepEqual(fs.readFileSync(f.restore.snapshotPath),before);assert.deepEqual(f.s.commands,[]);
});

test('the post-submission completion preserves a separate legitimate pre-approved branch receipt and approval bytes',async t=>{
 const f=fixture(t),target=f.snapshot.sessions[0],entry={...Object.fromEntries(['id','projectId','kind','harness','model','mode','createdAt','branch'].map(key=>[key,target[key]])),
  expectedBranch:'existing/primary-work',workspacePath:'C:\\Worktrees\\primary',workspaceRepoPath:'C:\\Repos\\MJobs'};
 const file=path.join(f.root,'restore-branch-adoptions.json');fs.writeFileSync(file,JSON.stringify({schemaVersion:1,kind:'preserved-worktree-branch-adoptions',
  snapshotAt:f.snapshot.at,snapshotSha256:f.review.snapshotSha256,reviewedAt:f.at(-200000),reviewReference:'Separate earlier branch approval',entries:[entry]}));
 const approval=readBranchAdoptions(f.root,f.snapshot,f.snapshot.sessions,f.s.now);
 f.restore.state.source.branchAdoptions={revision:approval.revision,snapshotSha256:approval.snapshotSha256};
 const workspace={verified:true,observedAt:f.at(-190000),workspacePath:entry.workspacePath,workspaceRepoPath:entry.workspaceRepoPath,branch:entry.expectedBranch};
 const receipt=makeBranchReceipt(approval,approval.entries[0],{...target,isTerminated:true},f.restore.state.source,f.restore.state.startedAt,workspace,f.s.now-180000);
 Object.assign(receipt,{status:'verified',verifiedAt:f.at(-170000),workspaceAfter:{...workspace,observedAt:f.at(-170000)}});
 f.restore.state.branchAdoptionReceipts[target.id]=receipt;f.s.live[0].branch=entry.expectedBranch;f.restore.save();
 f.review.sourceSha256=digest(f.restore.state.source);f.review.restoreStateSha256=digest(fs.readFileSync(f.restore.statePath));f.writeReview();
 const before=fs.readFileSync(file),existing=clone(receipt);
 await f.complete();assert.deepEqual(fs.readFileSync(file),before);assert.deepEqual(f.restore.state.branchAdoptionReceipts[target.id],existing);
 const live=await f.adapter.readRuntimes();assert.equal(live.runtimesReady,true);assert.deepEqual(live.branchAdoptionIds,[target.id]);assert.deepEqual(live.postReviewIds,[f.entry.id]);
});

test('durable Startup owner substitution during inspection cannot commit reviewed completion',async t=>{
 const f=fixture(t),manifest=f.adapter.manifest(),file=path.join(f.root,'startup-state.json');
 fs.writeFileSync(file,JSON.stringify({schemaVersion:1,revision:'stable-owner',runId:f.runId,phase:'uncertain',active:false,reconciliationRequired:true,
  recentRequests:[],bootstrap:{manifestRevision:manifest.revision,targetCount:2}}));
 const startup=new StartupController({root:f.root,deps:f.adapter.deps(),now:()=>f.s.now}),before=fs.readFileSync(f.restore.statePath);
 f.s.onWorkspace=async()=>{const changed=JSON.parse(fs.readFileSync(file));changed.revision='different-owner';fs.writeFileSync(file,JSON.stringify(changed));};
 await assert.rejects(()=>completeReviewedRestore({confirm:'COMPLETE REVIEWED RESTORE',runId:f.runId,reviewRevision:digest(fs.readFileSync(f.reviewFile))},
  {master:f.master,startup,quota:f.quota,launcher:f.launcher,adapter:f.adapter,sustain:f.master.sustain,turbo:f.master.turbo,findWork:{running:false}}));
 assert.deepEqual(fs.readFileSync(f.restore.statePath),before);assert.ok(f.restore.state.inflight[f.entry.id]);assert.deepEqual(f.s.commands,[]);
});

test('malformed, unbound, ambiguous and incomplete evidence cannot complete or clear any ledger',async t=>{
 const faults={
  'wrong source':f=>{f.review.sourceSha256='0'.repeat(64);f.writeReview();},
  'wrong raw state':f=>{f.review.restoreStateSha256='0'.repeat(64);f.writeReview();},
  'wrong desired IDs':f=>{f.review.desiredIdsSha256='0'.repeat(64);f.writeReview();},
  'wrong run':f=>{f.review.startupRunId='other-run';f.writeReview();},
  'wrong original branch':f=>{f.review.entries[0].branch='other/branch';f.writeReview();},
  'same-generation snapshot bytes changed':f=>fs.appendFileSync(f.restore.snapshotPath,' '),
  'wrong native birth':f=>{f.review.entries[0].createdAt=f.at(-80000000);f.writeReview();},
  'wrong history UUID':f=>{f.history.sessions[0].beforeNativeSessionId='other';fs.writeFileSync(f.historyFile,JSON.stringify(f.history));f.review.historyEvidence.sha256=digest(fs.readFileSync(f.historyFile));f.writeReview();},
  'altered original evidence':f=>fs.appendFileSync(f.originalFile,' '),
  'altered original metadata':f=>fs.utimesSync(f.originalFile,new Date(f.at(-500000)),new Date(f.at(-500000))),
  'wrong provenance timestamp source':f=>{f.history.sessions[0].beforeProvenance.timestampSource='invented-observation';fs.writeFileSync(f.historyFile,JSON.stringify(f.history));f.review.historyEvidence.sha256=digest(fs.readFileSync(f.historyFile));f.writeReview();},
  'wrong successful audit hash':f=>{f.review.entries[0].completionAudit.lineSha256='0'.repeat(64);f.writeReview();},
  'second successful native command after review':f=>fs.appendFileSync(path.join(f.root,'audit.jsonl'),JSON.stringify({time:f.at(-1000),event:'restore_resume_submitted',id:f.entry.id,project:f.entry.projectId,action:'restore'})+'\n'),
  'missing durable state':f=>fs.unlinkSync(f.restore.statePath),
  'corrupt durable state':f=>fs.writeFileSync(f.restore.statePath,'{bad'),
  'disk-memory disagreement':f=>{const value=JSON.parse(fs.readFileSync(f.restore.statePath));value.lastError='external';fs.writeFileSync(f.restore.statePath,JSON.stringify(value));},
  'another inflight native submission':f=>{f.restore.state.inflight['mjobs-primary']={at:f.at(-60000),command:'resume-agent'};f.restore.save();},
  'native tick still running':f=>{f.restore.running=true;},
  'restore still active':f=>{f.restore.state.active=true;},
  'unknown runtime count':f=>f.s.counts.set('mjobs-primary',null),
  'missing healthy target':f=>f.s.live.shift(),
  'changed terminal generation':f=>{f.s.live[1].terminalGeneration='different-generation';},
  'fresh native proof differs':f=>{f.restore.reviewedRuntimeVerifier=async entry=>({verified:true,observedAt:f.at(0),...entry.nativeRuntime,providerPid:9999});},
  'fresh workspace proof differs':f=>{f.restore.workspaceVerifier=async entry=>({verified:true,observedAt:f.at(0),workspacePath:entry.workspacePath,workspaceRepoPath:entry.workspaceRepoPath,branch:'different/branch'});},
  'unhealthy reviewed runtime':f=>{f.s.live[1].activity.state='exited';},
  'new root conflict':f=>f.s.live.push({...clone(f.s.live[0]),id:'conflicting-primary'}),
 };
 for(const [name,change]of Object.entries(faults))await t.test(name,async st=>{
  const f=fixture(st);change(f);const before=clone(f.restore.state),exists=fs.existsSync(f.restore.statePath),bytes=exists?fs.readFileSync(f.restore.statePath):null;
  await assert.rejects(f.complete);assert.deepEqual(f.restore.state,before);assert.equal(fs.existsSync(f.restore.statePath),exists);
  if(exists)assert.deepEqual(fs.readFileSync(f.restore.statePath),bytes);assert.deepEqual(f.s.commands,[]);
 });
});

test('inspection races reject the final commit without clearing original fences',async t=>{
 const faults={
  'review byte substitution':f=>fs.appendFileSync(f.reviewFile,' '),
  'native fence changed':f=>{f.restore.state.inflight[f.entry.id].at=f.at(-115000);f.restore.save();},
  'restore source changed':f=>{f.restore.state.source.startupRunId='different-run';f.restore.save();},
  'guard withdrawn':f=>{f.s.safe=false;},
  'emergency engaged':f=>{f.s.locked=true;},
  'native generation changed':f=>{f.s.live[1].terminalGeneration='different-generation';},
  'workspace branch changed':f=>{f.s.live[1].branch='unexpected/branch';},
 };
 for(const [name,change]of Object.entries(faults))await t.test(name,async st=>{
  const f=fixture(st);let once=false;f.s.onWorkspace=async()=>{if(!once){once=true;change(f);}};
  await assert.rejects(f.complete);assert.ok(f.restore.state.inflight[f.entry.id]);assert.deepEqual(f.restore.state.postRestoreReviewReceipts,{});assert.deepEqual(f.s.commands,[]);
 });
});

test('an atomic persistence failure leaves the live owner uncommitted',async t=>{
 const f=fixture(t),before=clone(f.restore.state),bytes=fs.readFileSync(f.restore.statePath);
 fs.mkdirSync(f.restore.statePath+'.tmp');
 await assert.rejects(f.complete);assert.deepEqual(f.restore.state,before);assert.deepEqual(fs.readFileSync(f.restore.statePath),bytes);assert.deepEqual(f.s.commands,[]);
});

test('a changed binding during parallel readiness cannot publish the previous verified count',async t=>{
 const f=fixture(t);await f.complete();f.s.onNative=async()=>{f.restore.state.source.startupRunId='different-owner';};
 const readiness=await f.adapter.readRuntimes();
 assert.equal(readiness.runtimesReady,false);assert.equal(readiness.verified,0);assert.equal(readiness.runningExpectedAgents,null);
 assert.deepEqual(readiness.healthyIds,[]);assert.match(readiness.reason,/changed during inspection/);assert.deepEqual(f.s.commands,[]);
});

test('snapshot stays frozen across completion and Recheck, preserves drain holds, then renders the historical review inert',async t=>{
 const f=fixture(t);let startupUncertain=true;const before=fs.readFileSync(f.restore.snapshotPath);
 await f.complete();
 const master=new MasterControls({root:f.root,autoschedule:false,sustain:{config:{enabled:false}},turbo:{config:{enabled:false}},command:async()=>{throw Error('No native command');},
  audit:()=>{},getState:async()=>({sessions:f.s.live}),getAllSessions:async()=>clone(f.s.live),restoreOptions:{snapshotFrozen:()=>startupUncertain}});
 assert.equal(await master.restore.captureSnapshot(),null);assert.deepEqual(fs.readFileSync(f.restore.snapshotPath),before);
 startupUncertain=false;master.drainState.phase='paused';assert.equal(await master.restore.captureSnapshot(),null);
 master.drainState.phase='idle';assert.ok(await master.restore.captureSnapshot());
 const current=master.restore.getSnapshot();f.s.now=Date.now()+1000;
 assert.equal(readPostSubmissionReview(f.root,current,master.restore.targets(current),f.s.now),null);
 assert.equal(current.sessions.find(value=>value.id===f.entry.id).branch,f.entry.expectedBranch);
 assert.ok(master.restore.state.postRestoreReviewReceipts[f.entry.id]);
 const malformed={at:new Date(f.s.now).toISOString(),sessions:[]};
 assert.throws(()=>readPostSubmissionReview(f.root,malformed,[],f.s.now));
});

test('verified post-review receipts are carried immutably when the same frozen snapshot is used for another run',async t=>{
 const f=fixture(t);await f.complete();const receipt=clone(f.restore.state.postRestoreReviewReceipts[f.entry.id]);
 f.restore.schedule=()=>{};f.restore.tick=async()=>({});
 f.restore.start({startupRunId:'next-startup-run',autoDispatchAfter:false});
 assert.deepEqual(f.restore.state.postRestoreReviewReceipts[f.entry.id],receipt);
 assert.equal(f.restore.state.source.carriedPostRestoreReviews[f.entry.id],digest(receipt));
 assert.equal((await f.restore.runtimeTarget(f.snapshot.sessions[1],f.s.live[1],3)).matches,true);assert.deepEqual(f.s.commands,[]);
});
