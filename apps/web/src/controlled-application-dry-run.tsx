import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';
import type {ApplicationRehearsalSnapshot} from './application-rehearsal';

type Preview = ApplicationRehearsalSnapshot & {provider:string; context:'CONTROLLED_FIXTURE'};
type Run = {request_id:string|null; state:string; fingerprint?:string;cancel_requested?:boolean;retry_of?:string|null; error_code:string|null; receipt:null|{
  fingerprint:string;
  documents:{kind:string; document_id:string; size_bytes:number; sha256:string}[];
  verification:string; provider_acceptance_claimed:false;
}};
type Selection = {available:{revision_id:string; label:string; filename:string}[];
  selected_revision_ids:string[]; unavailable_revision_ids:string[]};

export function ControlledApplicationDryRun({profileId, applicationId}:{profileId:string; applicationId:string}) {
  const base = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}`;
  const path = base+'/controlled-dry-run';
  const [preview,setPreview] = useState<Preview|null>(null);
  const [run,setRun] = useState<Run|null>(null);
  const [selection,setSelection] = useState<Selection|null>(null);
  const [ids,setIds] = useState<string[]>([]);
  const [checked,setChecked] = useState(false);
  const [retryChecked,setRetryChecked] = useState(false);
  const [busy,setBusy] = useState(false);
  const [notice,setNotice] = useState('');
  const epoch = useRef(0);
  const pendingRun = useRef<{fingerprint:string; request_id:string}|null>(null);
  const pendingReview = useRef<{fingerprint:string; request_id:string}|null>(null);
  const pendingSelection = useRef<{key:string; request_id:string}|null>(null);
  const pendingRetry = useRef<{previous:string;fingerprint:string;request_id:string}|null>(null);
  useEffect(() => {
    const current = ++epoch.current;
    setPreview(null);setRun(null);setSelection(null);setIds([]);setChecked(false);setRetryChecked(false);setBusy(false);setNotice('');
    pendingRun.current=null;pendingReview.current=null;pendingSelection.current=null;pendingRetry.current=null;
    let timer:ReturnType<typeof setTimeout>|undefined;
    async function load() {
      try {
        const [p,r,s] = await Promise.allSettled([
          api<Preview>(path), api<Run>(path+'/runs/latest'),
          api<Selection>(path+'/supporting-documents'),
        ]);
        if (current !== epoch.current) return;
        if(p.status==='fulfilled')setPreview(p.value);
        else {setPreview(null);setNotice('Current inputs cannot be previewed. You can still cancel a saved active run.');}
        if(r.status==='fulfilled')setRun(r.value);
        if(s.status==='fulfilled'){setSelection(s.value);setIds(s.value.selected_revision_ids);}
        if (r.status==='fulfilled'&&(r.value.state === 'QUEUED' || r.value.state === 'RUNNING')) timer = setTimeout(load,1000);
      } catch {if (current === epoch.current) setNotice('Controlled preparation is available for supported Greenhouse and Lever routes. Refresh after checking the route.');}
    }
    void load();
    return () => {epoch.current++;if(timer)clearTimeout(timer);};
  },[path,base]);
  useEffect(()=>setRetryChecked(false),[run?.request_id,run?.state]);
  useEffect(()=>{
    if(!run?.request_id || (run.state!=='QUEUED'&&run.state!=='RUNNING'))return;
    let live=true;
    const timer=setTimeout(()=>void api<Run>(path+'/runs/'+encodeURIComponent(run.request_id!))
      .then(result=>{if(live)setRun(result);})
      .catch(()=>{if(live)setNotice('Could not read the queued run. Refresh to recover its durable result.');}),1000);
    return ()=>{live=false;clearTimeout(timer);};
  },[path,run]);
  async function refresh() {
    const current=epoch.current;
    const [p,r,s] = await Promise.all([api<Preview>(path),api<Run>(path+'/runs/latest'),api<Selection>(path+'/supporting-documents')]);
    if(current!==epoch.current)return;
    setPreview(p);setRun(r);setSelection(s);setIds(s.selected_revision_ids);setChecked(false);
  }
  async function cancel() {
    if(!run?.request_id||busy)return;
    const current=epoch.current;
    setBusy(true);setNotice('');
    try {
      const result=await api<Run>(path+'/runs/'+encodeURIComponent(run.request_id)+'/cancel',{method:'POST'});
      if(current!==epoch.current)return;
      setRun(result);setNotice(result.state==='CANCELLED'?'Fixture run cancelled.':result.cancel_requested?'Cancellation requested. Waiting for the worker to stop.':result.state==='RUNNING'&&result.receipt?'Verification is already saved. Refresh to read operation completion.':'The run already finished; its saved result is unchanged.');
    } catch {if(current===epoch.current)setNotice('Cancellation could not be confirmed. Check the saved run before retrying.');}
    finally {if(current===epoch.current)setBusy(false);}
  }
  async function action(kind:'review'|'run'|'select'|'retry') {
    if (!preview || busy) return;
    const current=epoch.current;
    setBusy(true);setNotice('');
    try {
      if (kind === 'select') {
        const key=JSON.stringify([...ids].sort());
        if(pendingSelection.current?.key!==key)pendingSelection.current={key,request_id:crypto.randomUUID()};
        await api(path+'/supporting-documents',{method:'PUT',body:JSON.stringify({revision_ids:ids,request_id:pendingSelection.current.request_id})});
        if(current!==epoch.current)return;
        pendingSelection.current=null;await refresh();
        setNotice('Fixture document selection saved. Review the updated values before running.');
      } else if (kind === 'review') {
        if(pendingReview.current?.fingerprint!==preview.fingerprint)pendingReview.current={fingerprint:preview.fingerprint,request_id:crypto.randomUUID()};
        await api(path+'/reviews',{method:'POST',body:JSON.stringify({...pendingReview.current,acknowledged:true})});
        if(current!==epoch.current)return;
        pendingReview.current=null;await refresh();setNotice('Fixture review saved.');
      } else if(kind==='retry') {
        if(!run?.request_id||!retryChecked)return;
        if(pendingRetry.current?.previous!==run.request_id||pendingRetry.current?.fingerprint!==preview.fingerprint)
          pendingRetry.current={previous:run.request_id,fingerprint:preview.fingerprint,request_id:crypto.randomUUID()};
        const intent=pendingRetry.current;
        const queued=await api<Run>(path+'/runs/'+encodeURIComponent(intent.previous)+'/retry',{method:'POST',body:JSON.stringify({request_id:intent.request_id,fingerprint:intent.fingerprint,acknowledged:true})});
        if(current!==epoch.current)return;
        setRun(queued);setRetryChecked(false);setNotice('Reviewed fixture retry queued. The earlier attempt remains saved.');
      } else {
        if(pendingRun.current?.fingerprint!==preview.fingerprint)pendingRun.current={fingerprint:preview.fingerprint,request_id:crypto.randomUUID()};
        const queued=await api<Run>(path+'/runs',{method:'POST',body:JSON.stringify(pendingRun.current)});
        if(current!==epoch.current)return;
        setRun(queued);setNotice('Fixture run queued. Refresh to read its durable result.');
      }
    } catch(error) {
      if(current!==epoch.current)return;
      if((error as {status?:number})?.status===409) {
        try {await refresh();} catch { /* retain last visible snapshot with stale warning */ }
        if(current!==epoch.current)return;
        const code=(error as {code?:string})?.code;
        setNotice(code==='CONTROLLED_DRY_RUN_ACTIVE'?'Another fixture run is active. Check its saved status before starting another attempt.':code==='CONTROLLED_DRY_RUN_RETRY_REVIEW_REQUIRED'?'These inputs already have a saved attempt. Use the reviewed retry control to create a new one.':'The inputs changed or need review. Check the current profile, answers, documents and route, then review again.');
      } else setNotice('The request could not be confirmed. Retry to recover the same request, or refresh its saved result.');
    } finally {if(current===epoch.current)setBusy(false);}
  }
  const active=run?.state==='QUEUED'||run?.state==='RUNNING';
  const terminal=!!run&&['COMPLETED','CANCELLED','FAILED','BLOCKED','DEAD_LETTERED'].includes(run.state);
  const selectionDirty=selection!==null&&JSON.stringify([...ids].sort())!==JSON.stringify([...selection.selected_revision_ids].sort());
  return <section className="panel" aria-label="Controlled ATS preparation">
    <h2>Controlled ATS preparation</h2>
    <p>Practice on a local Greenhouse or Lever fixture with your current reviewed values and documents. This never opens an employer form or submits an application. External submission is OFF.</p>
    {preview && <>
      <p>{preview.provider} fixture · {preview.ready?'Ready for review':'Review required'}</p>
      <ul aria-label="Fixture preparation values">{preview.fields.map(field=><li key={field.key}>
        {field.label}: <output aria-label={`${field.key} fixture value`}>{field.value===null?'Needs a verified value':String(field.value)}</output>
        {' · '}{field.source==='VERIFIED_ANSWER'?'Approved saved answer':field.source==='CONTACT'?'Profile contact':field.source==='DOCUMENT'?'Owned document':'Unresolved'}
      </li>)}</ul>
      {preview.blockers.length>0&&<p role="status">Review the unresolved fields or unavailable documents before running this fixture.</p>}
      {selection&&<fieldset disabled={busy||active}><legend>Supporting documents for this fixture</legend>
        {selection.available.length===0?<p>No supporting documents are saved in this profile. Add them in the document library when needed.</p>:selection.available.map(item=><label key={item.revision_id}>
          <input type="checkbox" checked={ids.includes(item.revision_id)} onChange={event=>{setIds(event.currentTarget.checked?[...ids,item.revision_id]:ids.filter(id=>id!==item.revision_id));setChecked(false);}}/>
          {item.label||item.filename}
        </label>)}
        <button type="button" onClick={()=>void action('select')}>Save fixture document selection</button>
      </fieldset>}
      {selectionDirty&&<p role="status">Save the supporting document selection before reviewing or running.</p>}
      <label><input type="checkbox" checked={checked} disabled={busy||active||selectionDirty||!preview.ready} onChange={event=>setChecked(event.currentTarget.checked)}/>I reviewed these fixture values and selected documents</label>
      <button type="button" disabled={busy||active||selectionDirty||!checked||!preview.ready} onClick={()=>void action('review')}>Record fixture review</button>{' '}
      <button type="button" disabled={busy||active||selectionDirty||!preview.ready||preview.latest_review?.fingerprint!==preview.fingerprint||(terminal&&run?.fingerprint===preview.fingerprint)} onClick={()=>void action('run')}>Run controlled fixture</button>
      {terminal&&<div><label><input type="checkbox" checked={retryChecked} disabled={busy||selectionDirty||!preview.ready||preview.latest_review?.fingerprint!==preview.fingerprint} onChange={event=>setRetryChecked(event.currentTarget.checked)}/>I reviewed current inputs and want a new fixture attempt</label>
        <button type="button" disabled={busy||selectionDirty||!retryChecked||!preview.ready||preview.latest_review?.fingerprint!==preview.fingerprint} onClick={()=>void action('retry')}>Run reviewed retry</button></div>}
    </>}
    {run&&run.state!=='IDLE'&&<p role="status">Fixture run: {run.state}{run.error_code?' · Review required before retrying.':''}</p>}
    {active&&<button type="button" disabled={busy||run?.cancel_requested||!!run?.receipt} onClick={()=>void cancel()}>{run?.cancel_requested?'Cancellation requested':'Cancel fixture run'}</button>}
    {run?.retry_of&&<p className="muted">This is a new reviewed attempt. The earlier attempt remains in durable history.</p>}
    {run?.receipt&&<section aria-label="Saved fixture verification"><p>Selected document bytes verified locally. No employer acceptance or submission is claimed.</p>
      {preview&&run.receipt.fingerprint!==preview.fingerprint&&<p>This receipt belongs to earlier inputs. Review and run the current fixture to verify the changed inputs.</p>}
      <ul>{run.receipt.documents.map(item=><li key={item.document_id}>{item.kind} · {item.size_bytes} bytes · SHA-256 {item.sha256}</li>)}</ul>
    </section>}
    <button type="button" disabled={busy} onClick={()=>void refresh().catch(()=>setNotice('Could not refresh. Your saved operation can be read after reconnecting.'))}>Refresh fixture preparation</button>
    {notice&&<p role="status">{notice}</p>}
  </section>;
}
