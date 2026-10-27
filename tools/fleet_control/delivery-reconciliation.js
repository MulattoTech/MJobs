'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {NativeDeliveryReceipts}=require('./native-delivery-receipts');
// Read-only evidence establishes delivery, never acceptance. Keep reservations
// as tombstones; only remove the matching delivery-error cooldown.
class DeliveryReconciliation{
 constructor(root,{reader=new NativeDeliveryReceipts(),now=Date.now}={}){this.root=root;this.reader=reader;this.now=now;this.lastCheck=0;this.status={checkedAt:null,confirmed:0,unresolved:0};}
 run(ledger,sustain,{force=false}={}){
  if(!ledger||!sustain||sustain.running||(!force&&this.now()-this.lastCheck<60000))return this.status;
  this.lastCheck=this.now();let confirmed=0,unresolved=0;
  for(const [id,entry]of Object.entries(ledger.entries)){
   if(entry.status!=='uncertain'||!id.startsWith('mjobs-'))continue;
   let proof;try{proof=this.reader.inspect({id,messageHash:entry.messageHash,normalizedHash:entry.normalizedHash,sentAt:entry.at,baseline:entry.baseline||null});}catch{unresolved++;continue;}
   if(!proof?.verified||proof.sessionId!==id||proof.messageHash!==entry.messageHash||entry!==ledger.entries[id]){unresolved++;continue;}
   const prior=entry.status;entry.receipt=proof;entry.status='submitted';entry.reconciledAt=this.now();
   const agent=sustain.status.agents[id],same=agent&&Math.abs(Date.parse(agent.lastSent||'')-entry.at)<100;
   if(same){
    if(['dispatch_error','unverified_delivery'].includes(agent.backoffReason)){delete agent.backoffUntil;delete agent.backoffReason;}
    agent.unverified=0;agent.deliveryReceipt=proof;agent.awaiting=!proof.completedAt;agent.completedAt=proof.completedAt||agent.completedAt;
    if(agent.lastKey){const signalKey=crypto.createHash('sha256').update(JSON.stringify(agent.lastKey)).digest('hex'),event=sustain.signals.read?.().entries?.[signalKey];if(event?.id===id)sustain.signals.finish(agent.lastKey,'submitted');}
    if(agent.lastType==='productive'&&agent.lastKey)sustain.productivity.finish(agent.lastKey,'submitted');
    if(agent.lastType==='artifact_review'&&agent.lastKey)sustain.artifactReviews.finish(agent.lastKey,'submitted');
    if(Date.parse(proof.requestedAt)>(Date.parse(sustain.status.lastSuccess||'')||0))sustain.status.lastSuccess=proof.requestedAt;
    // A historical receipt must not hide a newer failure from another agent.
   }
   fs.appendFileSync(path.join(this.root,'delivery-reconciliation.jsonl'),JSON.stringify({at:new Date(this.now()).toISOString(),id,reservation:entry.id,previousStatus:prior,proof,matchingSchedulerAssignment:!!same})+'\n');confirmed++;
  }
  if(confirmed){ledger.save();sustain.save();}
  this.status={checkedAt:new Date(this.now()).toISOString(),confirmed,unresolved};sustain.deliveryReconciliation=this.status;return this.status;
 }
}
module.exports={DeliveryReconciliation};
