'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),os=require('node:os'),path=require('node:path');
const {DatabaseSync}=require('node:sqlite');const {NativeDeliveryReceipts,epoch,hash}=require('./native-delivery-receipts');
const {DeliveryReconciliation}=require('./delivery-reconciliation');
function fixture(t){const root=fs.mkdtempSync(path.join(os.tmpdir(),'native-receipt-'));const file=path.join(root,'ao.db');const db=new DatabaseSync(file);t.after(()=>{db.close();fs.rmSync(root,{recursive:true,force:true});});
 db.exec('CREATE TABLE sessions(id TEXT PRIMARY KEY,project_id TEXT,created_at TEXT,agent_session_id TEXT,runtime_launch_id TEXT,session_mode TEXT,harness TEXT,is_terminated INTEGER); CREATE TABLE session_terminal_turns(id INTEGER PRIMARY KEY,session_id TEXT,harness TEXT,native_session_id TEXT,user_request TEXT,requested_at TEXT,responded_at TEXT)');
 const now=Date.parse('2026-10-27T12:00:10Z'),sentAt=now-5000,createdAt='2026-10-26T00:00:00Z',message='Test only: inspect exact module and give bounded evidence.';
 db.prepare('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)').run('mjobs-worker','mjobs-luna',createdAt,'native-private','generation','tui','codex',0);
 const reader=new NativeDeliveryReceipts({database:file,now:()=>now}),baseline=reader.capture({id:'mjobs-worker',generation:'generation',createdAt});
 baseline.capturedAt=sentAt-100;
 const request={id:'mjobs-worker',sentAt,messageHash:hash(message),normalizedHash:hash(message),baseline};
 const add=(changes={})=>{const row={id:1,session_id:'mjobs-worker',harness:'codex',native_session_id:'native-private',user_request:message,requested_at:'2026-10-27 07:00:06.0000000 -0500 CDT',responded_at:'2026-10-27T12:00:08Z',...changes};db.prepare('INSERT INTO session_terminal_turns VALUES(?,?,?,?,?,?,?)').run(...Object.values(row));};
 return {root,db,reader,request,baseline,add,message,now};}
test('exact native message confirms fast completed turn without exporting prompt text',t=>{const f=fixture(t);f.add();const r=f.reader.inspect(f.request);assert(r.verified);assert.equal(r.state,'completed');assert.equal(r.completedAt,'2026-10-27T12:00:08.000Z');assert(!JSON.stringify(r).includes(f.message));assert(!JSON.stringify(r).includes('native-private'));});
test('foreign identity, wrong message, stale/future time and ambiguous same-message turns do not confirm',async t=>{
 for(const [name,change]of Object.entries({wrongMessage:{user_request:'Unrelated turn'},wrongNative:{native_session_id:'other'},wrongSession:{session_id:'other'},wrongHarness:{harness:'agy'},beforeSend:{requested_at:'2026-10-27T11:59:00Z'},future:{requested_at:'2027-01-01T00:00:00Z'}}))await t.test(name,t=>{const f=fixture(t);f.add(change);assert.equal(f.reader.inspect(f.request),null);});
 const f=fixture(t);f.add();f.add({id:2});assert.equal(f.reader.inspect(f.request),null);
});
test('cursor/generation/archive/project changes cannot reuse old receipts',async t=>{
 for(const mutation of ["UPDATE sessions SET runtime_launch_id='changed'","UPDATE sessions SET project_id='babytracker'","UPDATE sessions SET is_terminated=1","UPDATE sessions SET agent_session_id='other'"])await t.test(mutation,t=>{const f=fixture(t);f.add();f.db.exec(mutation);let proof=null;try{proof=f.reader.inspect(f.request);}catch{}assert.equal(proof,null);});
 const f=fixture(t);f.add();const b=f.reader.capture({id:'mjobs-worker',generation:'generation',createdAt:'2026-10-26T00:00:00Z'});assert.equal(f.reader.inspect({...f.request,baseline:b}),null);
});
test('timestamps support exact local offset and late receipt remains receipt-only, never acceptance',t=>{assert.equal(epoch('2026-10-27 07:00:06.0000000 -0500 CDT'),Date.parse('2026-10-27T12:00:06Z'));const f=fixture(t);f.add({responded_at:null});assert.equal(f.reader.inspect(f.request).state,'received');});
test('reconciliation retains dedupe history, clears only matching delivery hold, and never launches work',t=>{
 const f=fixture(t);f.add();const entry={id:'reserved-one',at:f.request.sentAt,messageHash:f.request.messageHash,baseline:f.baseline,status:'uncertain'};let ledgerSaves=0,saves=0,signalUpdates=0;
 const ledger={entries:{'mjobs-worker':entry},save(){ledgerSaves++;}},agent={lastSent:new Date(entry.at).toISOString(),lastKey:'unique-task',lastType:'review',backoffReason:'dispatch_error',backoffUntil:'future',awaiting:false};
 const sustain={running:false,status:{agents:{'mjobs-worker':agent},lastError:'bad'},signals:{read:()=>({entries:{[hash(JSON.stringify('unique-task'))]:{id:'mjobs-worker'}}}),finish(key,status){assert.equal(key,'unique-task');assert.equal(status,'submitted');signalUpdates++;}},save(){saves++;}};
 const r=new DeliveryReconciliation(f.root,{reader:f.reader,now:()=>f.now});assert.equal(r.run(ledger,sustain,{force:true}).confirmed,1);assert.equal(entry.status,'submitted');assert.equal(agent.backoffUntil,undefined);assert.equal(agent.awaiting,false);assert.equal(signalUpdates,1);assert.equal(saves,1);assert.equal(ledgerSaves,1);assert.equal(r.run(ledger,sustain,{force:true}).confirmed,0);
});

test('unknown reservations survive expiry and cannot be blindly reused',t=>{const {DispatchLedger}=require('./dispatch-ledger');const f=fixture(t);let now=f.now;const d=new DispatchLedger({root:f.root,now:()=>now});const record=d.reserve('mjobs-worker','one instruction');d.finish('mjobs-worker',record.id,'uncertain');now+=3*86400000;d.save();assert(d.entries['mjobs-worker']);assert.throws(()=>d.reserve('mjobs-worker','different instruction'),/unverified/);d.finish('mjobs-worker',record.id,'submitted');assert(d.reserve('mjobs-worker','new verified-scope instruction'));});
