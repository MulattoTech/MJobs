'use strict';
// Stable event identities, not idle timers, authorize automatic repeat work.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const digest=value=>crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex');
function checks(pr){
 const rows=Array.isArray(pr?.statusCheckRollup)?pr.statusCheckRollup:[];
 const normalized=rows.map(c=>({name:String(c.name||c.context||''),state:String(c.conclusion||c.state||c.status||'UNKNOWN').toUpperCase()})).sort((a,b)=>(a.name+':'+a.state).localeCompare(b.name+':'+b.state));
 const failures=normalized.filter(c=>['FAILURE','TIMED_OUT','CANCELLED','ACTION_REQUIRED','ERROR','STARTUP_FAILURE'].includes(c.state));
 const settled=normalized.length>0&&normalized.every(c=>['SUCCESS','NEUTRAL','SKIPPED','FAILURE','TIMED_OUT','CANCELLED','ACTION_REQUIRED','ERROR','STARTUP_FAILURE'].includes(c.state));
 return {pending:!settled,failed:failures.length>0,signature:digest(normalized),failureSignature:digest(failures)};
}
function prSignal(pr){
 if(!pr||!Number.isSafeInteger(pr.number)||!/^[0-9a-f]{40}$/i.test(pr.headRefOid||''))return null;
 const state=checks(pr);
 if(state.failed)return {kind:'ci',key:`ci:${pr.number}:${pr.headRefOid}:${state.failureSignature}`,pending:false};
 if(state.pending)return null;
 return {kind:'pr',key:`pr:${pr.number}:${pr.headRefOid}:${state.signature}:${pr.isDraft===true}:${pr.reviewDecision||''}`,pending:false};
}
function graphSignal(sessions,prs){
 return digest({roster:sessions.filter(s=>!s.isTerminated&&!s.replacedBy).map(s=>[s.id,s.kind]).sort(),prs:prs.map(p=>[p.number,p.headRefOid,p.isDraft,p.reviewDecision||'',checks(p).signature]).sort((a,b)=>a[0]-b[0])});
}
class DispatchSignals{
 constructor(root){this.file=path.join(root,'dispatch-signals.json');}
 read(){
  try{const data=JSON.parse(fs.readFileSync(this.file,'utf8').replace(/^\uFEFF/,''));if(data.version!==1||!data.entries||typeof data.entries!=='object'||Array.isArray(data.entries)||Object.keys(data.entries).length>20000)throw Error('Invalid dispatch event ledger');return data;}
  catch(e){if(e.code==='ENOENT')return {version:1,entries:{}};throw Error('Dispatch event ledger unavailable; automatic duplicate protection is holding work');}
 }
 seen(key){return Object.hasOwn(this.read().entries,digest(key));}
 update(fn){
  let lock;try{lock=fs.openSync(this.file+'.lock','wx');const data=this.read(),result=fn(data);const temp=this.file+'.'+crypto.randomUUID()+'.tmp';try{fs.writeFileSync(temp,JSON.stringify(data),{flag:'wx'});fs.renameSync(temp,this.file);}finally{try{fs.unlinkSync(temp);}catch(e){if(e.code!=='ENOENT')throw e;}}return result;}
  finally{if(lock!==undefined){fs.closeSync(lock);fs.unlinkSync(this.file+'.lock');}}
 }
 reserve(key,id,now=Date.now()){
  if(typeof key!=='string'||!key||key.length>8192||typeof id!=='string'||id.length>128)throw Error('Invalid dispatch event');
  return this.update(data=>{const hash=digest(key);if(Object.hasOwn(data.entries,hash))return false;if(Object.keys(data.entries).length>=20000)throw Error('Dispatch event ledger requires archival review');data.entries[hash]={id,at:new Date(now).toISOString(),status:'reserved'};return true;});
 }
 finish(key,status){if(!['submitted','uncertain'].includes(status))throw Error('Invalid dispatch result');this.update(data=>{const entry=data.entries[digest(key)];if(!entry)throw Error('Missing dispatch reservation');entry.status=status;});}
}
const REPORT_POLICY=' Report only changed actionable facts in at most six lines. If the same head is only waiting for CI or review, wait without rerunning tests or repeating status. If current code already satisfies the proposed slice, report NO_NEW_GAP once and park it; do not invent a defect or add duplicate tests for activity.';
module.exports={checks,prSignal,graphSignal,DispatchSignals,REPORT_POLICY};
