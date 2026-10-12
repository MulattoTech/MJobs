import React, {useEffect, useRef, useState} from 'react';
import {api} from './api';

type EvaluationOperation = {
  operation_id:string;request_id:string;state:string;current_step:string|null;
  progress:number;error:string|null;updated_at:string;cancel_requested:boolean;
};
const active = (row:EvaluationOperation) => ['QUEUED','RUNNING'].includes(row.state);

export function DurableAIEvaluation({profileId,canEvaluate,onCompleted}:{
  profileId:string;canEvaluate:boolean;onCompleted:()=>void;
}) {
  const [operations,setOperations]=useState<EvaluationOperation[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState('');
  const [submitting,setSubmitting]=useState(false);
  const [cancelling,setCancelling]=useState<string|null>(null);
  const [refresh,setRefresh]=useState(0);
  const requestId=useRef<string|null>(null);
  const seen=useRef(new Set<string>());
  const completed=useRef(onCompleted);
  completed.current=onCompleted;
  const base=`/profiles/${encodeURIComponent(profileId)}/ai-cost-plan`;
  useEffect(()=>{
    if(!profileId){setLoading(false);return;}
    let stopped=false;
    let timer:ReturnType<typeof setTimeout>|undefined;
    async function poll() {
      try {
        const response=await api<{profile_id:string;operations:EvaluationOperation[]}>(`${base}/operations`);
        if(stopped)return;
        if(response.profile_id!==profileId)throw new Error('AI evaluation profile mismatch.');
        setOperations(response.operations);setError('');setLoading(false);
        if(response.operations.some(row=>row.request_id===requestId.current))requestId.current=null;
        for(const row of response.operations)if(row.state==='COMPLETED'&&!seen.current.has(row.operation_id)) {
          seen.current.add(row.operation_id);completed.current();
        }
      } catch {
        if(!stopped){setError('Cannot verify AI evaluation status. Retrying; new evaluation is paused.');setLoading(false);}
      } finally {
        if(!stopped)timer=setTimeout(()=>void poll(),1000);
      }
    }
    void poll();
    return ()=>{stopped=true;if(timer)clearTimeout(timer);};
  },[base,profileId,refresh]);
  async function queue() {
    setSubmitting(true);setError('');
    requestId.current ||= crypto.randomUUID();
    try {
      const row=await api<EvaluationOperation>(`${base}/evaluate/queued`,{
        method:'POST',body:JSON.stringify({request_id:requestId.current}),
      });
      setOperations(current=>[row,...current.filter(item=>item.operation_id!==row.operation_id)]);
      requestId.current=null;setRefresh(value=>value+1);
    } catch {
      setError('Admission was not confirmed. Check status before retrying; retry uses the same request identity.');
    } finally {setSubmitting(false);}
  }
  async function cancel(operationId:string) {
    setCancelling(operationId);
    try {
      const row=await api<EvaluationOperation>(`${base}/operations/${encodeURIComponent(operationId)}/cancel`,{method:'POST'});
      setOperations(current=>current.map(item=>item.operation_id===operationId?row:item));
      setRefresh(value=>value+1);
    } catch {setError('Cancellation was not confirmed. Check the saved operation status.');}
    finally {setCancelling(null);}
  }
  return <section aria-label="Durable AI evaluations">
    <p>Admitted evaluations continue after you close this page. Reload to check progress. Partial results stay private until the complete batch is saved.</p>
    {loading&&<p role="status">Loading saved AI operations…</p>}
    {error&&<p role="alert">{error}</p>}
    <button type="button" disabled={!canEvaluate||loading||submitting||!!error||operations.some(active)}
      onClick={()=>void queue()}>Evaluate planned AI candidates</button>
    {error&&<button type="button" onClick={()=>setRefresh(value=>value+1)}>Check AI evaluation status</button>}
    {operations.slice(0,10).map(row=><article key={row.operation_id} aria-label="AI evaluation operation">
      <p role="status">{row.state} · {row.current_step||'Waiting for worker'} · {row.progress||0}%</p>
      {row.cancel_requested&&active(row)&&<p role="status">Cancellation requested. Waiting for the bounded provider call; partial results will not be published.</p>}
      {row.error&&<p>{row.error==='AI_PROVIDER_OUTCOME_UNKNOWN'
        ? 'Provider outcome is unknown. Automatic replay is paused to avoid a duplicate paid call. Review provider history before starting another evaluation.'
        : `Evaluation stopped: ${row.error}. No partial advisory batch was published.`}</p>}
      {active(row)&&<button type="button" disabled={cancelling===row.operation_id||row.cancel_requested}
        onClick={()=>void cancel(row.operation_id)}>{cancelling===row.operation_id?'Cancellation requested':'Cancel AI evaluation'}</button>}
    </article>)}
  </section>;
}
