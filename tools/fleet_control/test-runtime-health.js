'use strict';
const assert=require('node:assert/strict');
const {addControllerHealth,runtimeHealthy,controllerStopped}=require('./runtime-health');
(async()=>{
 const saved={id:'primary-1',kind:'orchestrator',mode:'chat',activity:{state:'idle'},isTerminated:false};
 const [stopped]=await addControllerHealth([saved],async()=>({controller:'stopped',messages:['must not retain']}));
 assert.equal(runtimeHealthy(stopped,4),false,'wrapper processes do not prove a chat controller is ready');
 assert.equal(controllerStopped(stopped),true);
 assert.equal(Object.hasOwn(stopped,'messages'),false);
 for(const state of ['ready','busy']){
  const [s]=await addControllerHealth([saved],async()=>({controller:state}));
  assert.equal(runtimeHealthy(s,2),true);assert.equal(runtimeHealthy(s,0),false);
 }
 for(const state of ['connecting','recovering','hibernated','unknown']){
  const [s]=await addControllerHealth([saved],async()=>({controller:state}));assert.equal(runtimeHealthy(s,4),false);
 }
 const [unavailable]=await addControllerHealth([saved],async()=>{throw Error('offline')});
 assert.equal(runtimeHealthy(unavailable,4),false);assert.equal(controllerStopped(unavailable),false);
 const [malformed]=await addControllerHealth([saved],async()=>({controller:{state:'ready'}}));assert.equal(runtimeHealthy(malformed,4),false);
 const tui={...saved,mode:'tui'};assert.equal(runtimeHealthy(tui,3),true);assert.equal(runtimeHealthy(tui,-1),false);
 assert.equal(runtimeHealthy({...tui,activity:{state:'exited'}},3),false);
 assert.equal(runtimeHealthy({...tui,isTerminated:true},3),false);
 console.log('PASS current chat controller health, stale wrapper rejection, safe unavailable state and TUI runtime checks');
})().catch(e=>{console.error(e);process.exitCode=1});
