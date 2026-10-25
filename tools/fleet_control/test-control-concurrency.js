'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {createRequire}=require('node:module'),serverPath=path.join(__dirname,'server.js'),nativeRequire=createRequire(serverPath);
const source=fs.readFileSync(serverPath,'utf8'),marker="if(!fs.existsSync(AO)){";
const calls=[],ctx=vm.createContext({require(id){if(id==='node:child_process')return {execFile(file,args,opts,cb){calls.push(args);cb(null,{stdout:'ok'});}};return nativeRequire(id);},__dirname,process,Buffer,URL,console,fetch,setTimeout,clearTimeout,structuredClone,AbortSignal});
vm.runInContext(source.slice(0,source.indexOf(marker))+'\nglobalThis.f={main,command};',ctx);
vm.runInContext("master={locked:()=>false,drainState:{enabled:false}};quota={dispatchBlockReason:()=>null};sustain={running:false};turbo={running:false};findWork={running:false};audit=()=>{};",ctx);
async function post(url,data,headers={}){let status,body;await ctx.f.main({url,method:'POST',headers:{host:'127.0.0.1:8787','x-ao-fleet':'1','content-type':'application/json',...headers},async *[Symbol.asyncIterator](){yield Buffer.from(JSON.stringify(data))}},{writeHead(n){status=n},end(x){body=JSON.parse(x)}});return {status,body};}
(async()=>{
 for(const route of ['/api/nudge','/api/find-work']){
  vm.runInContext('mutationLock=true',ctx);
  assert.equal((await post(route,{id:'worker-1'})).status,409);
  assert.equal(vm.runInContext('mutationLock',ctx),true,'rejected request must not release another request lock');
  assert.equal((await post(route,{id:'worker-1'},{'x-ao-fleet':'0'})).status,400);
  assert.equal(vm.runInContext('mutationLock',ctx),true,'bad header cannot unlock another operation');
 }
 vm.runInContext('mutationLock=false;master.drainState.enabled=true',ctx);
 await assert.rejects(()=>ctx.f.command(['send','--session','worker-1','--message','Implement new task']),/wind-down/);
 await ctx.f.command(['send','--session','primary','--message','GRACEFUL WIND-DOWN ORDER FROM USER: stop assigning work'],200,{windDown:true});
 assert.equal(calls.length,1,'only deliberate stop notice can pass draining guard');
 vm.runInContext('master.locked=()=>true',ctx);
 await assert.rejects(()=>ctx.f.command(['send','--session','primary','--message','GRACEFUL WIND-DOWN ORDER FROM USER: stop assigning work'],200,{windDown:true}),/Emergency/);
 console.log('PASS: rejected concurrent requests cannot release owned lock; only stop notice can pass graceful-drain guard; emergency lock wins.');
})().catch(e=>{console.error(e);process.exitCode=1});
