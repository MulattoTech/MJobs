'use strict';
// Query-only native receipts. Exact message + session identity + time/cursor;
// never uses an activity LED as delivery evidence and never returns prompt text.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
function epoch(value){
 if(typeof value!=='string')return NaN;
 const normalized=value.split(' m=')[0].replace(/\s([+-]\d{2})(\d{2})\s+[A-Za-z][A-Za-z0-9_+/-]*$/,'$1:$2').replace(/(\.\d{3})\d+/, '$1');
 return Date.parse(normalized);
}
const identity=s=>hash(JSON.stringify([s.id,s.project_id,s.created_at,s.agent_session_id,s.runtime_launch_id]));
const fileIdentity=file=>{const s=fs.statSync(file,{bigint:true});return [String(s.dev),String(s.ino),String(s.birthtimeNs)].join(':');};
class NativeDeliveryReceipts{
 constructor({projectId='mjobs-luna',database=path.join(os.homedir(),'.ao','data','ao.db'),now=Date.now}={}){this.projectId=projectId;this.database=database;this.now=now;}
 query(fn){
  const fileBefore=fileIdentity(this.database);const {DatabaseSync}=require('node:sqlite');
  const db=new DatabaseSync(this.database,{readOnly:true});
  try{db.exec('PRAGMA query_only=ON; PRAGMA busy_timeout=1000; PRAGMA cache_size=-1024; BEGIN');const result=fn(db,fileBefore);if(fileIdentity(this.database)!==fileBefore)throw Error('Native database identity changed');return result;}finally{db.close();}
 }
 session(db,id){
  if(typeof id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$/.test(id))throw Error('Invalid native session');
  const s=db.prepare('SELECT id,project_id,created_at,agent_session_id,runtime_launch_id,session_mode,harness,is_terminated FROM sessions WHERE id=?').get(id);
  if(!s||s.project_id!==this.projectId||s.is_terminated||s.session_mode!=='tui'||s.harness!=='codex'||!s.agent_session_id||!s.runtime_launch_id)throw Error('Native receipt target identity unavailable');return s;
 }
 capture({id,generation,createdAt}){
  return this.query((db,databaseIdentity)=>{const s=this.session(db,id);if(s.runtime_launch_id!==generation||epoch(s.created_at)!==epoch(createdAt))throw Error('Native receipt target changed');
   const cursor=db.prepare('SELECT COALESCE(MAX(id),0) AS cursor FROM session_terminal_turns WHERE session_id=?').get(id).cursor;
   return {version:1,id,projectId:this.projectId,generation,createdAt,identity:identity(s),databaseIdentity,cursor,capturedAt:this.now()};});
 }
 inspect({id,messageHash,normalizedHash,sentAt,baseline=null}){
  if(!/^[a-f0-9]{64}$/.test(messageHash||'')||!Number.isFinite(sentAt)||sentAt>this.now()+1000)return null;
  return this.query((db,databaseIdentity)=>{const s=this.session(db,id);
   if(!Number.isFinite(epoch(s.created_at))||epoch(s.created_at)>sentAt||baseline&&(baseline.id!==id||baseline.projectId!==this.projectId||baseline.identity!==identity(s)||baseline.databaseIdentity!==databaseIdentity||!Number.isFinite(baseline.capturedAt)||baseline.capturedAt>sentAt+1000||sentAt-baseline.capturedAt>30000||(!Number.isSafeInteger(baseline.cursor)||baseline.cursor<0)))return null;
   const rows=db.prepare('SELECT id,harness,native_session_id,user_request,requested_at,responded_at FROM session_terminal_turns WHERE session_id=? AND id>? ORDER BY id DESC LIMIT 64').all(id,baseline?.cursor||0);
   const matches=rows.filter(r=>{const at=epoch(r.requested_at);if(r.harness!=='codex'||r.native_session_id!==s.agent_session_id||!Number.isFinite(at)||at<sentAt-1000||at>sentAt+120000||at>this.now()+1000||typeof r.user_request!=='string'||r.user_request.length>250000)return false;
    return hash(r.user_request)===messageHash||normalizedHash&&hash(r.user_request.replace(/\r\n/g,'\n').trimEnd())===normalizedHash;});
   if(matches.length!==1)return null;const row=matches[0],requested=epoch(row.requested_at),responded=epoch(row.responded_at),complete=Number.isFinite(responded)&&responded>=requested&&responded<=this.now()+1000;
   return {version:1,verified:true,sessionId:id,projectId:this.projectId,messageHash,turnId:row.id,requestedAt:new Date(requested).toISOString(),completedAt:complete?new Date(responded).toISOString():null,state:complete?'completed':'received',observedAt:new Date(this.now()).toISOString(),identity:identity(s),databaseIdentity};
  });
 }
}
module.exports={NativeDeliveryReceipts,epoch,hash};
