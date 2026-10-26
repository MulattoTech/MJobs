'use strict';
const assert=require('node:assert/strict');
const {addControllerHealth,runtimeHealthy,controllerStopped,canReceiveInstruction}=require('./runtime-health');
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
 const waiting={...tui,activity:{state:'waiting_input'}};
 assert.equal(runtimeHealthy(waiting,3),true,'A live TUI at its instruction prompt is restored');
 assert.equal(runtimeHealthy(waiting,null),false,'Waiting for an instruction does not prove a native runtime exists');
 assert.equal(runtimeHealthy(waiting,0),false);
 assert.equal(runtimeHealthy({...saved,activity:{state:'waiting_input'},controllerVerified:true,controllerState:'ready'},3),true);
 assert.equal(runtimeHealthy({...saved,activity:{state:'waiting_input'},controllerVerified:true,controllerState:'stopped'},3),false);
 assert.equal(runtimeHealthy({...tui,activity:{state:'blocked'}},3),false,'A pending decision is not ready for automated work');
 for(const activity of ['idle','waiting_input'])assert.equal(canReceiveInstruction({activity,isIdle:true}),true);
 for(const activity of ['blocked','active','exited','unknown'])assert.equal(canReceiveInstruction({activity,isIdle:true}),false,'Native activity overrides a stale idle flag');
 for(const flags of [{isIdle:false},{busy:true},{isTerminated:true},{replacedBy:'new-session'}])assert.equal(canReceiveInstruction({activity:'waiting_input',isIdle:true,...flags}),false);
 assert.equal(runtimeHealthy({...tui,activity:{state:'exited'}},3),false);
 assert.equal(runtimeHealthy({...tui,isTerminated:true},3),false);
 console.log('PASS current Chat/TUI health, instruction-waiting readiness, native process evidence and blocked-decision rejection');
})().catch(e=>{console.error(e);process.exitCode=1});
