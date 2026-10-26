'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {readBranchAdoptions,makeBranchReceipt,receiptForRuntime,digest}=require('./branch-adoptions');
const NOW=Date.parse('2026-10-26T02:00:00.000Z');
const at=offset=>new Date(NOW+offset).toISOString();
const clone=value=>structuredClone(value);
function fixture(t){
 const root=fs.mkdtempSync(path.join(os.tmpdir(),'fleet-branch-adoptions-'));
 t.after(()=>fs.rmSync(root,{recursive:true,force:true}));
 const target={id:'mjobs-luna-20',projectId:'mjobs-luna',kind:'worker',harness:'codex',model:'preserved-model',mode:'tui',
  branch:'saved/branch',createdAt:at(-7200000),isTerminated:false,activity:'waiting_input'};
 const snapshot={at:at(-3600000),sessions:[target]};
 const entry={...Object.fromEntries(['id','projectId','kind','harness','model','mode','branch','createdAt'].map(key=>[key,target[key]])),
  expectedBranch:'actual/retained',workspacePath:'C:\\AO Worktrees\\mjobs-luna-20',workspaceRepoPath:'C:\\Tools\\Dev\\MJobs'};
 const snapshotPath=path.join(root,'restore-snapshot.json'),approvalPath=path.join(root,'restore-branch-adoptions.json');
 const snapshotBytes=Buffer.from(JSON.stringify(snapshot));fs.writeFileSync(snapshotPath,snapshotBytes);
 const record={schemaVersion:1,kind:'preserved-worktree-branch-adoptions',snapshotAt:snapshot.at,snapshotSha256:digest(snapshotBytes),
  reviewedAt:at(-60000),reviewReference:'fixture reviewed existing registered worktree',entries:[entry]};
 const save=()=>fs.writeFileSync(approvalPath,JSON.stringify(record));save();
 const read=(next=snapshot,targets=next.sessions,onDiagnostic)=>readBranchAdoptions(root,next,targets,NOW,onDiagnostic);
 return {root,target,snapshot,entry,record,snapshotPath,approvalPath,save,read};
}
function receiptFixture(t){
 const f=fixture(t),approval=f.read(),entry=approval.entries[0];
 const state={startedAt:at(-30000),source:{snapshotAt:approval.snapshotAt,startupRunId:'startup-fixture-one',
  branchAdoptions:{revision:approval.revision,snapshotSha256:approval.snapshotSha256}},inflight:{},branchAdoptionReceipts:{}};
 const before={...entry,isTerminated:true};
 const workspace={verified:true,observedAt:at(-10000),workspacePath:entry.workspacePath,workspaceRepoPath:entry.workspaceRepoPath,branch:entry.expectedBranch};
 const receipt=makeBranchReceipt(approval,entry,before,state.source,state.startedAt,workspace,NOW-5000);
 state.branchAdoptionReceipts[entry.id]=receipt;
 state.inflight[entry.id]={command:'restore',branchAdoptionReceiptId:receipt.receiptId};
 const session={...before,isTerminated:false,branch:entry.expectedBranch};
 const verify=()=>receiptForRuntime(approval,state,f.target,session,NOW);
 const markVerified=()=>Object.assign(receipt,{status:'verified',verifiedAt:at(-1000),workspaceAfter:{...workspace,observedAt:at(-2000)}});
 return {...f,approval,entry,state,before,workspace,receipt,session,verify,markVerified};
}

test('reviewed map binds the exact preserved bytes and immutable explicit branch mapping',t=>{
 const f=fixture(t),approval=f.read();
 assert.equal(approval.revision,digest(fs.readFileSync(f.approvalPath)));
 assert.equal(approval.snapshotSha256,digest(fs.readFileSync(f.snapshotPath)));
 assert.equal(approval.entries[0].branch,'saved/branch');assert.equal(approval.entries[0].expectedBranch,'actual/retained');
 assert.ok(Object.isFrozen(approval));assert.ok(Object.isFrozen(approval.entries));assert.ok(Object.isFrozen(approval.entries[0]));
 fs.unlinkSync(f.approvalPath);assert.equal(f.read(),null);
});
test('malformed or same-generation changed manifest bytes never authorize adoption',async t=>{
 for(const change of ['malformed-json','wrong-snapshot-hash','changed-snapshot-whitespace','unknown-schema','future-review','future-generation','duplicate-entry','wrong-id','wrong-model','wrong-original-branch','same-new-branch','wrong-mode','new-birth','relative-workspace','invalid-time'])await t.test(change,st=>{
  const f=fixture(st);
  if(change==='malformed-json'){fs.writeFileSync(f.approvalPath,'{bad json');assert.throws(()=>f.read());return;}
  if(change==='wrong-snapshot-hash')f.record.snapshotSha256='0'.repeat(64);
  if(change==='changed-snapshot-whitespace')fs.appendFileSync(f.snapshotPath,'\n');
  if(change==='unknown-schema')f.record.schemaVersion=2;
  if(change==='future-review')f.record.reviewedAt=at(1001);
  if(change==='future-generation'){f.record.snapshotAt=at(1001);f.record.reviewedAt=at(1001);}
  if(change==='duplicate-entry')f.record.entries.push(clone(f.entry));
  if(change==='wrong-id')f.entry.id='other-session';
  if(change==='wrong-model')f.entry.model='different-model';
  if(change==='wrong-original-branch')f.entry.branch='unreviewed/original';
  if(change==='same-new-branch')f.entry.expectedBranch=f.entry.branch;
  if(change==='wrong-mode')f.entry.mode='chat';
  if(change==='new-birth')f.entry.createdAt=at(-1000);
  if(change==='relative-workspace')f.entry.workspacePath='relative/path';
  if(change==='invalid-time')f.record.reviewedAt='not-a-time';
  f.save();assert.throws(()=>f.read());
 });
});
test('a valid older map becomes inert only for a valid strictly newer nonfuture snapshot',t=>{
 const f=fixture(t),next={at:at(-30000),sessions:[{...f.target,branch:f.entry.expectedBranch}]};
 fs.writeFileSync(f.snapshotPath,JSON.stringify(next));const diagnostics=[];
 assert.equal(f.read(next,next.sessions,value=>diagnostics.push(value)),null);
 assert.equal(diagnostics.at(-1).code,'HISTORICAL_BRANCH_ADOPTION_RECORD_IGNORED');
 assert.ok(fs.existsSync(f.approvalPath),'The historical audit record is preserved');
 const future={...next,at:at(1)};assert.throws(()=>f.read(future));
 const invalid={...next,sessions:[{...next.sessions[0],isTerminated:true}]};assert.throws(()=>f.read(invalid));
 f.record.entries[0].workspacePath='relative';f.save();assert.throws(()=>f.read(next),'Malformed historical records are not silently ignored');
});
test('legacy saved creation omission still requires the reviewed exact native creation predating preservation',t=>{
 const f=fixture(t);delete f.target.createdAt;
 fs.writeFileSync(f.snapshotPath,JSON.stringify(f.snapshot));f.record.snapshotSha256=digest(fs.readFileSync(f.snapshotPath));f.save();
 assert.ok(f.read());f.entry.createdAt=at(-1000);f.save();assert.throws(()=>f.read());
});
test('receipt creation requires the exact archived identity, protected run binding and fresh workspace proof',async t=>{
 for(const change of ['live-session','chat-session','wrong-branch','wrong-birth','missing-run','wrong-approval','wrong-workspace','wrong-workspace-branch','stale-workspace','future-workspace','workspace-before-review'])await t.test(change,st=>{
  const f=receiptFixture(st),current=clone(f.before),source=clone(f.state.source),workspace=clone(f.workspace);
  if(change==='live-session')current.isTerminated=false;
  if(change==='chat-session')current.mode='chat';
  if(change==='wrong-branch')current.branch='other';
  if(change==='wrong-birth')current.createdAt=at(-7100000);
  if(change==='missing-run')delete source.startupRunId;
  if(change==='wrong-approval')source.branchAdoptions.revision='0'.repeat(64);
  if(change==='wrong-workspace')workspace.workspacePath='C:\\Other';
  if(change==='wrong-workspace-branch')workspace.branch='other';
  if(change==='stale-workspace')workspace.observedAt=at(-40000);
  if(change==='future-workspace')workspace.observedAt=at(1001);
  if(change==='workspace-before-review')workspace.observedAt=at(-60001);
  assert.throws(()=>makeBranchReceipt(f.approval,f.entry,current,source,f.state.startedAt,workspace,NOW));
 });
});
test('submitted or successful command receipts require the exact durable native restore ledger',t=>{
 const f=receiptFixture(t);assert.ok(f.verify());f.receipt.status='command_succeeded';assert.ok(f.verify());
 delete f.state.inflight[f.entry.id];assert.equal(f.verify(),null);
});
test('receipt cannot authorize changed identity, approval, workspace, run or unrelated native submission',async t=>{
 const changes={
  approval:f=>{f.approval={...f.approval,revision:'0'.repeat(64)};},
  receiptApproval:f=>{f.receipt.approvalRevision='0'.repeat(64);},
  sourceApproval:f=>{f.state.source.branchAdoptions.revision='0'.repeat(64);},
  sourceGeneration:f=>{f.state.source.snapshotAt=at(-3500000);},
  receiptSnapshot:f=>{f.receipt.snapshotSha256='0'.repeat(64);},
  targetBranch:f=>{f.target.branch='other';},
  targetModel:f=>{f.target.model='other';},
  nativeID:f=>{f.session.id='other';},
  nativeProject:f=>{f.session.projectId='other';},
  nativeModel:f=>{f.session.model='other';},
  nativeMode:f=>{f.session.mode='chat';},
  nativeBirth:f=>{f.session.createdAt=at(-7100000);},
  nativeBranch:f=>{f.session.branch='unreviewed/branch';},
  stillArchived:f=>{f.session.isTerminated=true;},
  beforeWasLive:f=>{f.receipt.nativeBefore.isTerminated=false;},
  beforeBranch:f=>{f.receipt.nativeBefore.branch='other';},
  receiptEntry:f=>{f.receipt.entry.workspacePath='C:\\Other';},
  workspaceBefore:f=>{f.receipt.workspaceBefore.workspacePath='C:\\Other';},
  workspaceFalse:f=>{f.receipt.workspaceBefore.verified=false;},
  wrongCommand:f=>{f.receipt.nativeCommand='resume-agent';},
  wrongLedgerCommand:f=>{f.state.inflight[f.entry.id].command='resume-agent';},
  wrongLedgerID:f=>{f.state.inflight[f.entry.id].branchAdoptionReceiptId='other';},
  missingLedger:f=>{delete f.state.inflight[f.entry.id];},
  differentRun:f=>{f.state.source.startupRunId='other';},
  differentStart:f=>{f.state.startedAt=at(-20000);},
  futureSubmission:f=>{f.receipt.submittedAt=at(1001);},
  submissionBeforeStart:f=>{f.receipt.submittedAt=at(-40000);},
  malformedSubmission:f=>{f.receipt.submittedAt='invalid';},
  unknownStatus:f=>{f.receipt.status='unknown';},
 };
 for(const [name,change]of Object.entries(changes))await t.test(name,st=>{
  const f=receiptFixture(st);change(f);assert.equal(receiptForRuntime(f.approval,f.state,f.target,f.session,NOW),null);
 });
});
test('verified receipts require post-submission workspace proof and exact carry hash across runs',t=>{
 const f=receiptFixture(t);f.markVerified();delete f.state.inflight[f.entry.id];assert.ok(f.verify());
 const good=clone(f.receipt.workspaceAfter);f.receipt.workspaceAfter.observedAt=at(-5001);assert.equal(f.verify(),null);
 f.receipt.workspaceAfter=good;f.receipt.verifiedAt=at(-6000);assert.equal(f.verify(),null);f.receipt.verifiedAt=at(-1000);
 f.state.source.startupRunId='startup-fixture-two';f.state.startedAt=at(-500);
 assert.equal(f.verify(),null,'An old receipt does not automatically authorize a new run');
 f.state.source.carriedBranchReceipts={[f.entry.id]:digest(f.receipt)};assert.ok(f.verify());
 f.receipt.workspaceAfter.branch='other';assert.equal(f.verify(),null,'Receipt mutation invalidates its explicit carry hash');
});
