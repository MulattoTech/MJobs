'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {createRequire}=require('node:module'),serverPath=path.join(__dirname,'server.js'),nativeRequire=createRequire(serverPath);
const source=fs.readFileSync(serverPath,'utf8'),marker="if(!fs.existsSync(AO)){";
const calls=[],fixtureWrites=[];
const safeFs={...fs,mkdirSync(){},writeFileSync(file,data){fixtureWrites.push({file,data:String(data)})},renameSync(){},appendFileSync(){}};
const ctx=vm.createContext({require(id){if(id==='node:child_process')return {execFile(file,args,opts,cb){calls.push(args);cb(null,{stdout:'ok'});}};if(id==='node:fs')return safeFs;return nativeRequire(id);},__dirname,process,Buffer,URL,console,fetch,setTimeout,clearTimeout,structuredClone,AbortSignal});
vm.runInContext(source.slice(0,source.indexOf(marker))+'\nglobalThis.f={main,command,replace,retire,stateImpl:state};',ctx);
vm.runInContext("master={locked:()=>false,drainState:{enabled:false}};quota={dispatchBlockReason:()=>null};sustain={running:false};turbo={running:false};findWork={running:false};startup={status:()=>({phase:'idle',active:false,reconciliationRequired:false})};audit=()=>{};",ctx);
async function post(url,data,headers={}){let status,body;await ctx.f.main({url,method:'POST',headers:{host:'127.0.0.1:8787','x-ao-fleet':'1','content-type':'application/json',...headers},async *[Symbol.asyncIterator](){yield Buffer.from(JSON.stringify(data))}},{writeHead(n){status=n},end(x){body=JSON.parse(x)}});return {status,body};}
const worker=patch=>({id:'worker-race',name:'Race worker',kind:'worker',role:'Implementation',model:'gpt-6-luna',harness:'codex',mode:'tui',createdAt:'2026-10-26T20:00:00Z',branch:'ao/race-worker',activity:'idle',isIdle:true,busy:false,isTerminated:false,openPRs:0,prs:[],blocked:false,replacedBy:null,branchState:{commits:0,unpushed:0},...patch});
function lifecycleHarness(states,records={pairs:[]}){
 ctx.lifecycle={states:structuredClone(states),last:null,records:structuredClone(records),commands:[],saved:[]};
 vm.runInContext(`
  globalThis.fixtureState=async()=>{const next=lifecycle.states.length?lifecycle.states.shift():lifecycle.last;lifecycle.last=next;return structuredClone(next)};
  state=globalThis.fixtureState;loadState=globalThis.fixtureState;
  verifyCodexExecutable=async()=>{};assertWorktreeClean=async()=>{};
  readReplacements=()=>structuredClone(lifecycle.records);
  saveReplacements=value=>lifecycle.saved.push(structuredClone(value));
  command=async args=>{lifecycle.commands.push(Array.from(args));if(args[0]==='spawn')throw Error('UNEXPECTED_SPAWN');return args[0]==='session'&&args[1]==='history'?'fixture history':'fixture command accepted'};
 `,ctx);
 return ctx.lifecycle;
}
function staleSharedReadHarness(stale,fresh,records={pairs:[]}){
 ctx.sharedRace={stale:structuredClone(stale),fresh:structuredClone(fresh),records:structuredClone(records),commands:[],saved:[],shared:null};
 vm.runInContext(`
  state=async()=>structuredClone(sharedRace.stale);
  verifyCodexExecutable=async()=>{};
  readReplacements=()=>structuredClone(sharedRace.records);
  saveReplacements=value=>sharedRace.saved.push(structuredClone(value));
  command=async args=>{sharedRace.commands.push(Array.from(args));if(args[0]==='spawn')throw Error('UNEXPECTED_SPAWN');return args[0]==='session'&&args[1]==='history'?'fixture history':'fixture command accepted'};
  assertWorktreeClean=async()=>{
   state=globalThis.f.stateImpl;stateInFlight=null;
   let release;const staleResult=new Promise(resolve=>{release=resolve});let reads=0;
   loadState=async()=>{reads++;if(reads===1)return staleResult;return structuredClone(sharedRace.fresh)};
   sharedRace.shared=state(false);setTimeout(()=>release(structuredClone(sharedRace.stale)),0);
  };
 `,ctx);
 return ctx.sharedRace;
}
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
 const initiallyIdle=worker(),becameActive=worker({activity:'working',isIdle:false,busy:true});
 let race=lifecycleHarness([{offline:false,sessions:[initiallyIdle]},{offline:false,sessions:[becameActive]}]);
 await assert.rejects(()=>ctx.f.replace({id:initiallyIdle.id,model:'gpt-6-luna',effort:'high'}),/state changed/i);
 assert.equal(race.commands.filter(args=>args[0]==='spawn').length,0,'replacement must revalidate before spawn');
 assert.equal(race.saved.length,0,'failed replacement revalidation must not write a replacement record');
 const replacement=worker({id:'replacement-race',name:'Replacement',createdAt:'2026-10-26T20:01:00Z',replacedBy:null});
 const original=worker({replacedBy:replacement.id});
 const acquiredWork=worker({replacedBy:replacement.id,activity:'working',isIdle:false,busy:true,openPRs:1,prs:[{number:825,state:'open'}]});
 race=lifecycleHarness([{offline:false,sessions:[original,replacement]},{offline:false,sessions:[acquiredWork,replacement]}],{pairs:[{oldId:original.id,newId:replacement.id,model:replacement.model,effort:'high',retired:false}]});
 await assert.rejects(()=>ctx.f.retire({id:original.id,confirm:original.id}),/state changed/i);
 assert.equal(race.commands.filter(args=>args[0]==='session'&&args[1]==='kill').length,0,'retirement must revalidate before kill');
 assert.equal(race.saved.length,0,'failed retirement revalidation must not mark the record retired');
 const changedReplacement=worker({...replacement,model:'gpt-6-sol'});
 race=lifecycleHarness([{offline:false,sessions:[original,replacement]},{offline:false,sessions:[original,changedReplacement]}],{pairs:[{oldId:original.id,newId:replacement.id,model:replacement.model,effort:'high',retired:false}]});
 await assert.rejects(()=>ctx.f.retire({id:original.id,confirm:original.id}),/replacement state changed/i);
 assert.equal(race.commands.filter(args=>args[0]==='session'&&args[1]==='kill').length,0,'retirement must revalidate the replacement before kill');
 assert.equal(race.saved.length,0,'changed replacement must not mark the original retired');
 race=staleSharedReadHarness({offline:false,sessions:[initiallyIdle]},{offline:false,sessions:[becameActive]});
 await assert.rejects(()=>ctx.f.replace({id:initiallyIdle.id,model:'gpt-6-luna',effort:'high'}),/state changed/i);
 await race.shared;
 assert.equal(race.commands.filter(args=>args[0]==='spawn').length,0,'fresh replacement validation must bypass a stale shared read');
 assert.equal(race.saved.length,0,'stale shared reads must not write replacement records');
 race=staleSharedReadHarness({offline:false,sessions:[original,replacement]},{offline:false,sessions:[acquiredWork,replacement]},{pairs:[{oldId:original.id,newId:replacement.id,model:replacement.model,effort:'high',retired:false}]});
 await assert.rejects(()=>ctx.f.retire({id:original.id,confirm:original.id}),/state changed/i);
 await race.shared;
 assert.equal(race.commands.filter(args=>args[0]==='session'&&args[1]==='kill').length,0,'fresh retirement validation must bypass a stale shared read');
 assert.equal(race.saved.length,0,'stale shared reads must not mark retirement records');
 race=staleSharedReadHarness({offline:false,sessions:[initiallyIdle]},{offline:true,sessions:[initiallyIdle]});
 await assert.rejects(()=>ctx.f.replace({id:initiallyIdle.id,model:'gpt-6-luna',effort:'high'}),/fresh live session state unavailable/i);
 await race.shared;
 assert.equal(race.commands.filter(args=>args[0]==='spawn').length,0,'offline mutation validation must fail before spawn');
 console.log('PASS: rejected concurrent requests cannot release owned lock; only stop notice can pass graceful-drain guard; emergency lock wins.');
})().catch(e=>{console.error(e);process.exitCode=1});
