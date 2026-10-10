import {useEffect,useRef,useState} from 'react';
import {cancelMockRun,forgetTerminalMockRun,loadMockRunIntent,lookupMockRun,mockRunTerminal,
  postMockRun,saveMockRunIntent,type MockRunIntent,type MockRunReceipt,type MockRunScope} from './mock-run-client';

const editable=new Set(['PREPARED','DRY_RUN_COMPLETE','REVIEW_REQUIRED','AUTH_REQUIRED']);
type Props=MockRunScope&{applicationState:string;disabled?:boolean;onRefresh:()=>void|Promise<void>};
type ViewProps={intent:MockRunIntent|null;receipt:MockRunReceipt|null;busy:boolean;disabled:boolean;
  canStart:boolean;canCancel:boolean;consent:boolean;error:string;notice:string;storageBlocked:boolean;
  onConsent:(value:boolean)=>void;onBegin:(submit:boolean)=>void;onLookup:()=>void;
  onResend:()=>void;onNew:()=>void;onCancel:()=>void};

export function MockRunControlsView(props:ViewProps) {
  const locked=props.busy||props.disabled||props.storageBlocked;
  const active=!!props.receipt&&['QUEUED','RUNNING'].includes(props.receipt.state);
  const terminal=!!props.receipt&&mockRunTerminal.has(props.receipt.state);
  return <section className="mock-box" aria-label="Recoverable mock run">
    <h3>Controlled fictional test</h3>
    <p>A dry-run fills and reviews the local mock form, then stops before submission. No real employer application is sent.</p>
    {props.intent&&<><p>Saved request: <code>{props.intent.requestId}</code> · {props.intent.submit?'One fictional submission':'Dry-run only'}</p>
      <p role="status">{props.receipt?<>Operation <code>{props.receipt.operation_id}</code>: <strong>{props.receipt.state}</strong></>:'No acknowledgement yet. Request identity is saved.'}</p>
      {props.receipt?.error_code&&<p className="safety">{props.receipt.error_code}</p>}
      <p>Reloading only checks this request. It does not start another browser or submit another application.</p>
      <button disabled={locked} onClick={props.onLookup}>Refresh run status</button>
      {!props.receipt&&<button disabled={locked||!props.canStart||(props.intent.submit&&!props.consent)} onClick={props.onResend}>Resend same request</button>}
      {active&&<button disabled={locked||!props.canCancel} onClick={props.onCancel}>Cancel queued or running test</button>}
      {terminal&&<button disabled={locked||!props.canStart||!!props.error} onClick={props.onNew}>Start a new reviewed run</button>}
    </>}
    {!props.intent&&<button disabled={locked||!props.canStart} onClick={()=>props.onBegin(false)}>Run dry-run</button>}
    {(!props.intent||(props.intent.submit&&!props.receipt))&&<label className="consent"><input type="checkbox" checked={props.consent} disabled={locked||!props.canStart}
      onChange={event=>props.onConsent(event.target.checked)}/> I approve one fictional mock submission</label>}
    {!props.intent&&<button disabled={locked||!props.canStart||!props.consent} onClick={()=>props.onBegin(true)}>Submit to local mock</button>}
    {!props.canStart&&<p className="safety">This application cannot start another test in its current state. Resolve review or check the existing receipt first.</p>}
    {props.busy&&<p>Checking the durable request…</p>}
    {props.error&&<p role="alert">{props.error} The saved request has not been replaced.</p>}
    {props.notice&&<p role="status">{props.notice}</p>}
  </section>;
}

export function MockRunControls(props:Props) {
  // A profile/application switch destroys the old controller and its late responses.
  return <MockRunController key={`${props.profileId}\u0000${props.applicationId}`} {...props}/>;
}

function MockRunController(props:Props) {
  const scope:MockRunScope={profileId:props.profileId,applicationId:props.applicationId};
  const [intent,setIntent]=useState<MockRunIntent|null>(null);
  const [run,setRun]=useState<MockRunReceipt|null>(null);
  const [busy,setBusy]=useState(true);
  const [consent,setConsent]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [storageBlocked,setStorageBlocked]=useState(false);
  const alive=useRef(false),working=useRef(false),currentIntent=useRef<MockRunIntent|null>(null);
  const currentRun=useRef<MockRunReceipt|null>(null),onRefresh=useRef(props.onRefresh);
  const pollDeadline=useRef(Date.now()+210000);
  onRefresh.current=props.onRefresh;

  async function accept(result:MockRunReceipt|null) {
    if(!alive.current)return;
    const previous=currentRun.current;
    currentRun.current=result;setRun(result);
    if(result&&mockRunTerminal.has(result.state)) {
      setConsent(false);
      if(previous?.operation_id!==result.operation_id||previous.state!==result.state) {
        try {await onRefresh.current();} catch {
          if(alive.current)setNotice('The run outcome is recorded. Refresh the application to update its other panels.');
        }
      }
    }
  }
  async function perform(task:()=>Promise<void>) {
    if(working.current||!alive.current)return;
    working.current=true;setBusy(true);setError('');setNotice('');
    try {await task();} catch(failure) {
      if(alive.current)setError(failure instanceof Error?failure.message:'Run status could not be verified.');
    } finally {
      working.current=false;
      if(alive.current)setBusy(false);
    }
  }
  async function lookup(saved:MockRunIntent) {await accept(await lookupMockRun(scope,saved));}

  useEffect(()=>{
    alive.current=true;
    let cancelled=false;
    async function restore() {
      working.current=true;
      try {
        let saved:MockRunIntent|null;
        try {saved=loadMockRunIntent(localStorage,scope);} catch(failure) {
          if(!cancelled)setStorageBlocked(true);
          throw failure;
        }
        if(cancelled)return;
        currentIntent.current=saved;setIntent(saved);
        if(saved)await lookup(saved); // Read-only: restoration never POSTs.
      } catch(failure) {
        if(!cancelled)setError(failure instanceof Error?failure.message:'Saved request could not be checked.');
      } finally {
        working.current=false;
        if(!cancelled)setBusy(false);
      }
    }
    void restore();
    return ()=>{cancelled=true;alive.current=false;};
  },[]);

  useEffect(()=>{
    if(!intent||!run||!['QUEUED','RUNNING'].includes(run.state)||busy||error)return;
    if(Date.now()>=pollDeadline.current)return;
    const timer=setTimeout(()=>void perform(()=>lookup(intent)),1500);
    return ()=>clearTimeout(timer);
  },[intent,run,busy,error]);

  async function begin(submit:boolean) {
    if(currentIntent.current||props.disabled||!editable.has(props.applicationState)||(submit&&!consent))return;
    await perform(async()=>{
      const next:MockRunIntent={version:1,requestId:crypto.randomUUID(),submit};
      try {saveMockRunIntent(localStorage,scope,next);} catch {
        setStorageBlocked(true);
        throw new Error('The request could not be saved locally, so nothing was queued. Review browser storage before continuing.');
      }
      currentIntent.current=next;setIntent(next);setConsent(false);
      pollDeadline.current=Date.now()+210000;
      await accept(await postMockRun(scope,next));
    });
  }
  async function resend() {
    const saved=currentIntent.current;
    if(!saved||props.disabled||!editable.has(props.applicationState)||(saved.submit&&!consent))return;
    await perform(async()=>{setConsent(false);await accept(await postMockRun(scope,saved));});
  }
  function newReviewedRun() {
    if(working.current||props.disabled||!editable.has(props.applicationState)||error)return;
    try {
      forgetTerminalMockRun(localStorage,scope,currentRun.current);
      currentIntent.current=null;currentRun.current=null;
      setIntent(null);setRun(null);setConsent(false);setNotice('Review the current documents and answers before choosing the next test.');
    } catch(failure) {setError(failure instanceof Error?failure.message:'Current request must be resolved first.');}
  }
  async function cancel() {
    const saved=currentIntent.current,result=currentRun.current;
    if(!saved||!result||props.disabled)return;
    await perform(async()=>{await cancelMockRun(result.operation_id);await lookup(saved);
      if(alive.current)setNotice('Cancellation requested. A submit reservation requires receipt reconciliation, not another submission.');});
  }
  return <MockRunControlsView intent={intent} receipt={run} busy={busy} disabled={!!props.disabled}
    canStart={editable.has(props.applicationState)} canCancel={!['SUBMITTING','SUBMISSION_UNCONFIRMED','CONFIRMED'].includes(props.applicationState)} consent={consent} error={error} notice={notice} storageBlocked={storageBlocked}
    onConsent={setConsent} onBegin={value=>void begin(value)} onLookup={()=>{const saved=currentIntent.current;if(saved)void perform(()=>lookup(saved));}}
    onResend={()=>void resend()} onNew={newReviewedRun} onCancel={()=>void cancel()}/>;
}
