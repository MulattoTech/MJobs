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

/** Uses the already profile-scoped operation readback; never sends externally. */
export function OperationNotifications({enabled,operations,loadError=false}:{
  enabled:boolean;operations:NotificationOperation[];loadError?:boolean;
}) {
  const [dismissed,setDismissed] = useState<string[]>([]);
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
        <button type="button" onClick={()=>setDismissed(previous=>[
          ...previous.slice(-99),notificationKey(op),
        ])}>Dismiss notification</button>
      </li>)}</ul>:<p>No new activity notifications.</p>}
    </div>}
  </section>;
}
