'use strict';

// AO's installed conversation client maps its native string `controller`
// into controller.state. Read only that field, never retain conversation text.
async function addControllerHealth(sessions,readConversation){
 return Promise.all(sessions.map(async s=>{
  if(s.mode!=='chat'||s.isTerminated)return s;
  try{
   const value=await readConversation(s.id);
   const state=value?.controller;
   if(typeof state!=='string')return {...s,controllerVerified:false,controllerState:'unknown'};
   return {...s,controllerVerified:true,controllerState:state};
  }catch{return {...s,controllerVerified:false,controllerState:'unavailable'}}
 }));
}
function runtimeHealthy(session,count){
 if(!session||session.isTerminated||!Number.isInteger(count)||count<=0)return false;
 if(!['active','working','idle','waiting_input'].includes(session.activity?.state||session.status))return false;
 if(session.mode==='chat')return session.controllerVerified===true&&['ready','busy'].includes(session.controllerState);
 return session.mode==='tui';
}
function controllerStopped(session){
 return session?.mode==='chat'&&session.controllerVerified===true&&session.controllerState==='stopped';
}
// AO waiting_input is an empty instruction prompt. A blocked activity is a
// pending decision/permission dialog and must never receive an automatic send,
// even when an older snapshot's convenience flags still say isIdle.
function canReceiveInstruction(session){
 return !!session&&session.isIdle===true&&!session.busy&&!session.isTerminated&&!session.replacedBy&&
  ['idle','waiting_input'].includes(session.activity);
}
module.exports={addControllerHealth,runtimeHealthy,controllerStopped,canReceiveInstruction};
