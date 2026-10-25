'use strict';
const messages=Object.freeze({
 turbo:'Turbo operation failed. Inspect the local Turbo event log before retrying.',
 sustain:'Sustained dispatch failed. Inspect the local Sustain event log before retrying.',
 provider:'Provider quota or rate limit reported. Automatic dispatch is paused.',
 cooldown:'All Hands already ran recently. Wait for the cooldown before another burst.',
 resources:'All Hands paused by resource safety. Check current RAM, disk and CPU readings.',
 fresh:'Fresh Orchestrator and GitHub snapshots are required',
 primary:'Primary orchestrator missing. No All Hands dispatch permitted.'
});
const publicMessages=new Set(Object.values(messages));
function privateDiagnostic(error){
 if(typeof error?.message==='string')return error.message.slice(0,4000);
 if(typeof error==='string')return error.slice(0,4000);
 return 'Unstructured operation failure';
}
function isUsageLimit(error){return /(?:rate.limit|usage.limit|quota|too many requests|429|exhausted)/i.test(privateDiagnostic(error))}
function publicError(kind,error){
 const text=privateDiagnostic(error);
 if(text.startsWith('All Hands already ran recently.'))return messages.cooldown;
 if(text.startsWith('All Hands paused by resource safety'))return messages.resources;
 if(text===messages.fresh)return messages.fresh;
 if(text===messages.primary)return messages.primary;
 return messages[kind]||messages.turbo;
}
function publicState(value,kind){
 if(Array.isArray(value))return value.map(row=>publicState(row,kind));
 if(value===null||typeof value!=='object')return value;
 const out={};
 for(const [key,item] of Object.entries(value)){
  if(key==='stack'||key==='diagnostic')continue;
  if(['error','lastError','githubError'].includes(key)||(key==='reason'&&value.uncertain===true)){
   out[key]=item==null||item===''?item:publicMessages.has(item)?item:publicError(kind);
  }else out[key]=publicState(item,kind);
 }
 return out;
}
module.exports={publicError,publicState,privateDiagnostic,isUsageLimit};
