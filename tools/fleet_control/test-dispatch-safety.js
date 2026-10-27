'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const serverPath=path.join(__dirname,'server.js');
const nativeRequire=createRequire(serverPath);
const source=fs.readFileSync(serverPath,'utf8');
const marker="if(!fs.existsSync(AO)){";
assert.ok(source.includes(marker),'server bootstrap marker exists');
const calls=[],terminalCalls=[],daemonCalls=[],ledgerFinishes=[];let terminalState='native-observed',reservation=0;
const daemonSession={id:'worker-1',mode:'tui',harness:'codex',isTerminated:false,terminalGeneration:'generation-1',activity:{state:'idle'}};
let daemonSessions=[daemonSession];
const context=vm.createContext({
 require(id){
  if(id==='./native-delivery-receipts')return {NativeDeliveryReceipts:class{capture(){return {version:1};}inspect(){return null;}}};
  if(id==='node:child_process')return {execFile(file,args,options,callback){calls.push(args);callback(null,{stdout:'ok'});}};
  if(id==='./terminal-delivery')return {completeKnownPaste:async options=>{terminalCalls.push(options);await options.guard();assert.equal((await options.getSession()).id,'worker-1');return {state:terminalState,extraEnter:false};}};
  return nativeRequire(id);
 },
 fixtureDaemon:async uri=>{daemonCalls.push(uri);return {sessions:daemonSessions.map(value=>structuredClone(value))};},
 __dirname,process,Buffer,URL,console,fetch,setTimeout,clearTimeout,structuredClone,AbortSignal
});
const definitions=source.slice(0,source.indexOf(marker));
vm.runInContext(definitions+'\ndaemon=globalThis.fixtureDaemon;globalThis.fixture={command,main};',context,{filename:serverPath});
const f=context.fixture;
function set(reason,lock=false,drain=false){
 context.fakeQuota={dispatchBlockReason:()=>reason};
 context.fakeMaster={locked:()=>lock,drainState:{enabled:drain}};
 context.fakeSustain={running:false,config:{enabled:false},enable(x){this.config.enabled=x;return {enabled:x}}};
 context.fakeTurbo={running:false,config:{enabled:false},enable(x){this.config.enabled=x;return {enabled:x}}};
 context.fakeLedger={attach:()=>{},reserve:()=>({id:'reservation-'+(++reservation)}),finish:(id,reservedId,state)=>ledgerFinishes.push({id,reservedId,state})};
 vm.runInContext("quota=globalThis.fakeQuota;master=globalThis.fakeMaster;sustain=globalThis.fakeSustain;turbo=globalThis.fakeTurbo;dispatchLedger=globalThis.fakeLedger;startup={status:()=>({phase:'idle',active:false,reconciliationRequired:false})};findWork={running:false};audit=()=>{};",context);
}
async function post(url,data){
 let status,body;
 const req={method:'POST',url,headers:{host:'127.0.0.1:8787','x-ao-fleet':'1','content-type':'application/json'},async *[Symbol.asyncIterator](){yield Buffer.from(JSON.stringify(data));}};
 const res={writeHead(code){status=code;},end(value){body=JSON.parse(value);}};
 await f.main(req,res);
 return {status,body};
}
(async()=>{
 for(const reason of ['Quota sample unavailable or stale','Quota safety threshold reached at 7%','Automatic quota stop has tripped']){
  set(reason);
  for(const args of [['send','--session','worker-1'],['spawn','--kind','worker'],['session','resume-agent','worker-1'],['session','restore','worker-1'],['review','trigger']]){
   await assert.rejects(()=>f.command(args),e=>e.message===reason);
  }
  assert.equal(calls.length,0,'No process launch while quota protection blocks work');
  assert.equal((await post('/api/sustain/toggle',{enabled:true})).status,400);
  assert.equal((await post('/api/turbo/toggle',{enabled:true})).status,400);
  assert.equal((await post('/api/turbo/all-hands',{confirm:'ALL HANDS'})).status,400);
  assert.equal((await post('/api/sustain/toggle',{enabled:false})).status,200,'Pause remains available');
 }
 set(null,true);
 await assert.rejects(()=>f.command(['send']),/Emergency/);
 await f.command(['session','exit-agent','worker-1']);
 await f.command(['session','history','worker-1']);
 assert.equal(calls.length,2,'Stop and read commands remain available');
 set(null);
 await f.command(['send','--session','worker-1']);
 assert.equal(calls.length,3,'Healthy quota permits one launch');
 assert.equal(terminalCalls.length,1,'Healthy TUI send verifies terminal completion');
 assert.deepEqual(daemonCalls,['/api/v1/sessions','/api/v1/sessions'],'Terminal verification uses only the synthetic daemon fixture');
 const submitted=ledgerFinishes.filter(entry=>entry.state==='submitted').length;
 terminalState='no-matching-pending-composer';
 await assert.rejects(()=>f.command(['send','--session','worker-1','--message','fixture task']),/unverified/);
 assert.equal(ledgerFinishes.filter(entry=>entry.state==='submitted').length,submitted,'Absent composer cannot become a submitted task');
 assert.equal(ledgerFinishes.at(-1).state,'uncertain','Unverified terminal delivery remains uncertain');
 const agyLaunches=calls.length,agyTerminalCalls=terminalCalls.length,agySubmitted=ledgerFinishes.filter(entry=>entry.state==='submitted').length;
 daemonSessions=[{...daemonSession,harness:'agy',terminalGeneration:undefined}];
 await f.command(['send','--session','worker-1','--message','agy task']);
 assert.equal(calls.length,agyLaunches+1,'Eligible Antigravity TUI target uses native AO send');
 assert.equal(terminalCalls.length,agyTerminalCalls,'Antigravity send does not use the Codex terminal verifier');
 assert.equal(ledgerFinishes.filter(entry=>entry.state==='submitted').length,agySubmitted+1,'Successful Antigravity send is submitted');
 const launches=calls.length,submittedAfterTerminal=ledgerFinishes.filter(entry=>entry.state==='submitted').length;
 for(const harness of [undefined,'unknown']){
  daemonSessions=[{...daemonSession,harness,terminalGeneration:undefined}];
  await assert.rejects(()=>f.command(['send','--session','worker-1','--message','invalid harness']),/Target changed/);
  assert.equal(calls.length,launches,'Missing or unknown harness must not launch AO');
  assert.equal(ledgerFinishes.filter(entry=>entry.state==='submitted').length,submittedAfterTerminal,'Invalid harness cannot become submitted');
  assert.equal(ledgerFinishes.at(-1).state,'uncertain','Invalid harness reservation remains uncertain');
 }
 daemonSessions=[];
 await assert.rejects(()=>f.command(['send','--session','worker-1','--message','missing target']),/Target changed/);
 assert.equal(calls.length,launches,'Missing pre-send target must not launch AO');
 assert.equal(ledgerFinishes.filter(entry=>entry.state==='submitted').length,submittedAfterTerminal,'Missing target cannot become submitted');
 assert.equal(ledgerFinishes.at(-1).state,'uncertain','Missing target reservation remains uncertain');
 daemonSessions=[daemonSession,structuredClone(daemonSession)];
 await assert.rejects(()=>f.command(['send','--session','worker-1','--message','ambiguous target']),/Target changed/);
 assert.equal(calls.length,launches,'Duplicate pre-send targets must not launch AO');
 assert.equal(ledgerFinishes.filter(entry=>entry.state==='submitted').length,submittedAfterTerminal,'Ambiguous target cannot become submitted');
 assert.equal(ledgerFinishes.at(-1).state,'uncertain','Ambiguous target reservation remains uncertain');
 assert.equal((await post('/api/sustain/toggle',{enabled:true})).status,200);
 console.log('PASS: stale/7%-threshold/tripped state blocks send, spawn, resume, restore, review, and dispatch enable; pause, stop, and reads remain available. No real process launched.');
})().catch(e=>{console.error(e);process.exitCode=1;});
