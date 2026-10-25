'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const serverPath=path.join(__dirname,'server.js');
const nativeRequire=createRequire(serverPath);
const source=fs.readFileSync(serverPath,'utf8');
const marker="if(!fs.existsSync(AO)){";
assert.ok(source.includes(marker),'server bootstrap marker exists');
const calls=[];
const context=vm.createContext({
 require(id){
  if(id==='node:child_process')return {execFile(file,args,options,callback){calls.push(args);callback(null,{stdout:'ok'});}};
  return nativeRequire(id);
 },
 __dirname,process,Buffer,URL,console,fetch,setTimeout,clearTimeout,structuredClone,AbortSignal
});
const definitions=source.slice(0,source.indexOf(marker));
vm.runInContext(definitions+'\nglobalThis.fixture={command,main};',context,{filename:serverPath});
const f=context.fixture;
function set(reason,lock=false,drain=false){
 context.fakeQuota={dispatchBlockReason:()=>reason};
 context.fakeMaster={locked:()=>lock,drainState:{enabled:drain}};
 context.fakeSustain={running:false,config:{enabled:false},enable(x){this.config.enabled=x;return {enabled:x}}};
 context.fakeTurbo={running:false,config:{enabled:false},enable(x){this.config.enabled=x;return {enabled:x}}};
 vm.runInContext('quota=globalThis.fakeQuota;master=globalThis.fakeMaster;sustain=globalThis.fakeSustain;turbo=globalThis.fakeTurbo;audit=()=>{};',context);
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
 assert.equal((await post('/api/sustain/toggle',{enabled:true})).status,200);
 console.log('PASS: stale/7%-threshold/tripped state blocks send, spawn, resume, restore, review, and dispatch enable; pause, stop, and reads remain available. No real process launched.');
})().catch(e=>{console.error(e);process.exitCode=1;});
