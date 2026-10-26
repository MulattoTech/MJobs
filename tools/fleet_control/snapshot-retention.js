'use strict';
// A recovery roster is not a live-activity report. Retain only an earlier
// eligible, exact identity whose runtime stopped without being archived.
const IDENTITY=['id','projectId','kind','harness','model','mode','branch','createdAt'];
function retainStoppedTargets(previous,current,observedAt){
 if(!Array.isArray(previous)||!Array.isArray(current))throw Error('Invalid snapshot rows');
 const ids=new Set();
 for(const row of current){if(!row||typeof row.id!=='string'||ids.has(row.id))throw Error('Ambiguous current session identity');ids.add(row.id);}
 const prior=new Map(previous.map(row=>[row.id,row]));
 return current.map(row=>{
  const old=prior.get(row.id);
  if(row.isTerminated||row.activity!=='exited'||!old||old.isTerminated||old.activity==='exited')return row;
  // Creation pins prevent adopting a different generation with a recycled ID.
  if(typeof old.createdAt!=='string'||!Number.isFinite(Date.parse(old.createdAt))||
   !IDENTITY.every(key=>(old[key]??null)===(row[key]??null)))return row;
  return {...old,displayName:row.displayName,recoveryRetention:{reason:'previously-eligible-runtime-stopped',
   lastObservedActivity:'exited',lastObservedAt:observedAt,preservedObservedAt:old.observedAt||null}};
 });
}
module.exports={retainStoppedTargets};
