import {useEffect, useRef, useState} from 'react';
import {api} from './api';

type Snapshot={ready:boolean;fingerprint:string;fields:{question_id:string;label:string;value:string|null}[];
  blockers:string[];latest_review:{fingerprint:string}|null};
type Props={profileId:string;applicationId:string;applicationState:string;disabled?:boolean};
type ViewProps={snapshot:Snapshot|null;checked:boolean;busy:boolean;disabled:boolean;error:string;notice:string;
  onChecked:(value:boolean)=>void;onSave:()=>void;onRefresh:()=>void};

export function MockDynamicReviewView(props:ViewProps) {
  const snapshot=props.snapshot;
  if(snapshot?.blockers.includes('NO_OBSERVED_CONDITIONAL_FIELDS'))return null;
  const locked=props.busy||props.disabled;
  return <section className="panel" aria-label="Conditional dry-run review">
    <h3>Review conditional answers</h3>
    <p>The local form revealed additional required fields. Review these approved answers before a dry-run only. This does not permit submission.</p>
    {snapshot&&<>
      <dl>{snapshot.fields.map(field=><div key={field.question_id}><dt>{field.label}</dt><dd>{field.value??'Needs a verified answer'}</dd></div>)}</dl>
      {!snapshot.ready&&<p role="status">The current form or inputs need a fresh dry-run inspection, or a verified matching answer. Earlier review cannot be reused.</p>}
      {snapshot.ready&&snapshot.latest_review?.fingerprint===snapshot.fingerprint&&<p role="status">Conditional review saved. Choose a new reviewed dry-run in the controls below to continue.</p>}
      <label><input type="checkbox" checked={props.checked} disabled={locked||!snapshot.ready}
        onChange={event=>props.onChecked(event.currentTarget.checked)}/>I reviewed these conditional answers for a dry-run only</label>
      <button type="button" disabled={locked||!snapshot.ready||!props.checked} onClick={props.onSave}>Save conditional-field review</button>
    </>}
    <button type="button" disabled={locked} onClick={props.onRefresh}>Refresh conditional review</button>
    {props.error&&<p role="alert">{props.error}</p>}
    {props.notice&&<p role="status">{props.notice}</p>}
  </section>;
}

export function MockDynamicReview(props:Props) {
  return <Controller key={`${props.profileId}\u0000${props.applicationId}`} {...props}/>;
}

function Controller(props:Props) {
  const path=`/profiles/${encodeURIComponent(props.profileId)}/applications/${encodeURIComponent(props.applicationId)}/mock-dynamic-review`;
  const [snapshot,setSnapshot]=useState<Snapshot|null>(null),[checked,setChecked]=useState(false);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[notice,setNotice]=useState('');
  const alive=useRef(false),generation=useRef(0),working=useRef(false);
  const pending=useRef<{request_id:string;fingerprint:string}|null>(null);

  async function refresh() {
    const current=++generation.current;
    setChecked(false);setSnapshot(null);setError('');
    try {
      const result=await api<Snapshot>(path);
      if(alive.current&&generation.current===current)setSnapshot(result);
    } catch {
      if(alive.current&&generation.current===current)setError('Could not verify the current conditional fields. Refresh before reviewing.');
    }
  }
  useEffect(()=>{
    alive.current=true;void refresh();
    return ()=>{alive.current=false;generation.current++;};
  },[path,props.applicationState]);

  async function save() {
    if(working.current||props.disabled||!snapshot?.ready||!checked)return;
    working.current=true;setBusy(true);setChecked(false);setError('');setNotice('');
    if(pending.current?.fingerprint!==snapshot.fingerprint)
      pending.current={request_id:crypto.randomUUID(),fingerprint:snapshot.fingerprint};
    try {
      await api(path,{method:'POST',body:JSON.stringify({...pending.current,acknowledged:true})});
      if(!alive.current)return;
      pending.current=null;
      await refresh();
    } catch(failure) {
      if(!alive.current)return;
      await refresh();
      if(alive.current)setNotice((failure as {status?:number}).status===409
        ?'The form or inputs changed. Review the refreshed values before continuing.'
        :'The acknowledgement could not be confirmed. Refresh the saved review; retrying retains the same request.');
    } finally {
      working.current=false;
      if(alive.current)setBusy(false);
    }
  }
  return <MockDynamicReviewView snapshot={snapshot} checked={checked} busy={busy} disabled={!!props.disabled}
    error={error} notice={notice} onChecked={setChecked} onSave={()=>void save()} onRefresh={()=>void refresh()}/>;
}
