import React,{useCallback,useEffect,useRef,useState} from 'react';
import {api} from './api';
import {OperationProgress,type OperationProgressRecord} from './operation-progress';

type ProgressResponse={operations:OperationProgressRecord[]};
type ProgressRequest=(path:string)=>Promise<ProgressResponse>;
const defaultProgressRequest:ProgressRequest=path=>api<ProgressResponse>(path);
const POLL_INTERVAL_MS=2000;
const MAX_ERROR_BACKOFF_MS=30000;
const ACTIVE_STATES=new Set(['QUEUED','RUNNING','PENDING','RETRY_PENDING','RETRYING','SCHEDULED','WAITING','CANCEL_REQUESTED']);
export function OperationProgressConnected({profileId,request=defaultProgressRequest}:{profileId:string|null;request?:ProgressRequest}){
  const [generation,setGeneration]=useState(0);
  const [state,setState]=useState<{loading:boolean;error:boolean;operations:OperationProgressRecord[]}>({loading:false,error:false,operations:[]});
  const requestInFlight=useRef(false);
  const lastProfileId=useRef(profileId);
  const refresh=useCallback(()=>setGeneration(value=>value+1),[]);
  useEffect(()=>{
    let active=true;
    let timer:ReturnType<typeof setTimeout>|undefined;
    let errorBackoff=POLL_INTERVAL_MS;
    const schedule=(delay:number)=>{timer=setTimeout(load,delay);};
    const load=()=>{
      if(!active)return;
      if(requestInFlight.current){schedule(100);return;}
      requestInFlight.current=true;
      setState(current=>({loading:current.operations.length===0,error:false,operations:current.operations}));
      void Promise.resolve().then(()=>request(`/profiles/${encodeURIComponent(profileId!)}/operations/progress`)).then(value=>{
        if(!value||!Array.isArray(value.operations))throw new Error('OPERATIONS_RESPONSE_INVALID');
        if(!active)return;
        errorBackoff=POLL_INTERVAL_MS;
        setState({loading:false,error:false,operations:value.operations});
        if(value.operations.some(operation=>ACTIVE_STATES.has(operation.state)))schedule(POLL_INTERVAL_MS);
      }).catch(()=>{
        if(!active)return;
        setState(current=>({loading:false,error:true,operations:current.operations}));
        schedule(errorBackoff);
        errorBackoff=Math.min(errorBackoff*2,MAX_ERROR_BACKOFF_MS);
      }).finally(()=>{requestInFlight.current=false;});
    };
    const profileChanged=lastProfileId.current!==profileId;
    lastProfileId.current=profileId;
    if(!profileId){setState({loading:false,error:false,operations:[]});return()=>{active=false;};}
    setState(current=>({loading:profileChanged||current.operations.length===0,error:false,operations:profileChanged?[]:current.operations}));
    load();
    return()=>{active=false;if(timer!==undefined)clearTimeout(timer);};
  },[profileId,generation,request]);
  return <section className="panel" aria-label="Saved operation progress">
    <div className="toolbar"><h2>Saved operation progress</h2><button type="button" className="quiet" onClick={refresh} disabled={!profileId}>Refresh progress</button></div>
    {!profileId?<p>Select a profile to view saved operations.</p>:<><OperationProgress operations={state.operations} loading={state.loading} error={state.error&&state.operations.length===0} recovered/>{state.error&&state.operations.length>0&&<p role="alert">The latest progress could not be refreshed. Showing saved progress and retrying automatically.</p>}</>}
  </section>;
}
