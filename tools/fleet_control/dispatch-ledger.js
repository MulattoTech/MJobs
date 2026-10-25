'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
class DispatchLedger {
 constructor({root,now=()=>Date.now(),cooldownMs=120000}) {
  this.file=path.join(root,'dispatch-ledger.json');this.now=now;this.cooldownMs=cooldownMs;
  try{this.entries=JSON.parse(fs.readFileSync(this.file,'utf8'));}catch(e){if(e.code!=='ENOENT')throw Error('Dispatch ledger unreadable; refusing duplicate-prone dispatch');this.entries={};}
 }
 save(){
  const cutoff=this.now()-86400000;
  this.entries=Object.fromEntries(Object.entries(this.entries).filter(([,v])=>v.at>=cutoff));
  const tmp=this.file+'.tmp';fs.writeFileSync(tmp,JSON.stringify(this.entries,null,2));fs.renameSync(tmp,this.file);
 }
 reserve(sessionId,message){
  const previous=this.entries[sessionId],now=this.now();
  if(previous&&now-previous.at<this.cooldownMs)
   throw Error('A message was already submitted or its delivery is uncertain for '+sessionId+'. Wait '+Math.ceil((this.cooldownMs-(now-previous.at))/1000)+' seconds and inspect activity before retrying.');
  const record={at:now,id:crypto.randomUUID(),messageHash:crypto.createHash('sha256').update(message).digest('hex'),status:'reserved'};
  this.entries[sessionId]=record;this.save();return record;
 }
 finish(sessionId,id,status){
  const current=this.entries[sessionId];if(current?.id!==id)return;
  current.status=status;current.finishedAt=this.now();this.save();
 }
}
module.exports={DispatchLedger};
