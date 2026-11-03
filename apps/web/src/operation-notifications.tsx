import React, {useState} from 'react';

export type NotificationOperation = {id:string;kind:string;state:string;updated_at:string};
const outcomes:Record<string,string> = {
  COMPLETED:'completed', FAILED:'needs attention', DEAD_LETTERED:'needs review',
  CANCELLED:'was cancelled', BLOCKED:'needs review',
};
const names:Record<string,string> = {
  DISCOVER:'Job discovery', SOURCE_COLLECT:'Source collection',
  COMPANY_DISCOVER:'Company discovery', PREPARE:'Document preparation',
  APPLY_MOCK:'Fixture application', AI_EVALUATE:'AI evaluation', AI_EVALUATE_QUEUED:'AI evaluation',
  RESUME_EXTRACT_QUEUED:'Resume extraction', UPLOADED_RESUME_COMPARE_QUEUED:'Resume comparison',
  COMPANY_SOURCE_REFRESH:'Company source refresh', APPLICATION_DRY_RUN:'Application dry-run',
  APPLICATION_FORM_REVIEW:'Application form review',
};
export function notificationKey(operation:NotificationOperation):string {
  return JSON.stringify([operation.id,operation.state,operation.updated_at]);
}

const DISMISSAL_PREFIX='mjobs-notification-dismissals:v1:';
const MAX_DISMISSALS=100;
const MAX_DISMISSAL_KEY_LENGTH=512;
const MAX_DISMISSAL_PAYLOAD_LENGTH=65536;

function dismissalStorageKey(profileId:string):string|null {
  if(!profileId||profileId.length>128)return null;
  try {return DISMISSAL_PREFIX+encodeURIComponent(profileId);}catch{return null;}
}
function boundedDismissals(value:unknown):string[] {
  if(!Array.isArray(value))return [];
  return value.filter((key):key is string=>typeof key==='string'
    &&key.length>0&&key.length<=MAX_DISMISSAL_KEY_LENGTH).slice(-MAX_DISMISSALS);
}
function readDismissals(profileId:string):string[] {
  const key=dismissalStorageKey(profileId);
  if(!key||typeof window==='undefined')return [];
  try {
    const raw=window.localStorage.getItem(key);
    if(!raw||raw.length>MAX_DISMISSAL_PAYLOAD_LENGTH)return [];
    return boundedDismissals(JSON.parse(raw) as unknown);
  }catch{return [];} // Storage may be disabled, corrupt, or unavailable.
}
function persistDismissals(profileId:string,dismissed:string[]):void {
  const key=dismissalStorageKey(profileId);
  if(!key||typeof window==='undefined')return;
  try {window.localStorage.setItem(key,JSON.stringify(boundedDismissals(dismissed)));}
  catch{/* Continue with in-memory dismissal if browser storage is unavailable. */}
}

/** Uses the already profile-scoped operation readback; never sends externally. */
export function OperationNotifications({profileId,enabled,operations,loadError=false}:{
  profileId:string;enabled:boolean;operations:NotificationOperation[];loadError?:boolean;
}) {
  const [dismissed,setDismissed] = useState<string[]>(()=>readDismissals(profileId));
  const [muted,setMuted] = useState(false);
  if(!enabled)return null;
  const visible=operations.filter(op=>Object.hasOwn(outcomes,op.state)
    &&!dismissed.includes(notificationKey(op)))
    .sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,5);
  return <section className="panel" aria-label="Activity notifications">
    <div className="toolbar"><h2>Activity notifications</h2>
      <button type="button" className="quiet" onClick={()=>setMuted(value=>!value)}>
        {muted?'Show notifications':'Mute for this visit'}
      </button></div>
    {!muted&&<div aria-live="polite" aria-atomic="false">
      {loadError&&<p>Activity updates are temporarily unavailable. Retrying shortly.</p>}
      {visible.length?<ul>{visible.map(op=><li key={notificationKey(op)}>
        <span>{Object.hasOwn(names,op.kind)?names[op.kind]:'Background task'} {outcomes[op.state]}.</span>{' '}
        <button type="button" onClick={()=>{
          const key=notificationKey(op);
          const next=[...dismissed.filter(value=>value!==key),key].slice(-MAX_DISMISSALS);
          setDismissed(next);
          persistDismissals(profileId,next);
        }}>Dismiss notification</button>
      </li>)}</ul>:<p>No new activity notifications.</p>}
    </div>}
  </section>;
}
