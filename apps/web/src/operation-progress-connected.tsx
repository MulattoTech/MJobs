import React,{useCallback,useEffect,useRef,useState} from 'react';
import {api} from './api';
import {OperationProgress,type OperationProgressRecord} from './operation-progress';

type ProgressResponse={operations:OperationProgressRecord[]};
type ProgressRequest=(path:string)=>Promise<ProgressResponse>;
const defaultProgressRequest:ProgressRequest=path=>api<ProgressResponse>(path);
const POLL_INTERVAL_MS=2000;
const MAX_ERROR_BACKOFF_MS=30000;
const ACTIVE_STATES=new Set(['QUEUED','RUNNING','PENDING','RETRY_PENDING','RETRYING','SCHEDULED','WAITING','CANCEL_REQUESTED']);
type ProgressProps={profileId:string|null;request?:ProgressRequest};
export function OperationProgressConnected({profileId,request=defaultProgressRequest}:ProgressProps){
  // A profile switch immediately unmounts its previous view. Track outstanding
  // reads by scope so a delayed old response cannot block a different profile.
  const requestsInFlight=useRef(new Set<string>());
  return <ProfileProgress key={profileId===null?'none':JSON.stringify(profileId)} profileId={profileId}
    request={request} requestsInFlight={requestsInFlight.current}/>;
}
function ProfileProgress({profileId,request,requestsInFlight}:{profileId:string|null;request:ProgressRequest;requestsInFlight:Set<string>}){
  const [generation,setGeneration]=useState(0);
  const [state,setState]=useState<{loading:boolean;error:boolean;operations:OperationProgressRecord[]}>({loading:false,error:false,operations:[]});
  const refresh=useCallback(()=>setGeneration(value=>value+1),[]);
  useEffect(()=>{
    let active=true;
    let timer:ReturnType<typeof setTimeout>|undefined;
    let errorBackoff=POLL_INTERVAL_MS;
    const schedule=(delay:number)=>{timer=setTimeout(load,delay);};
    const load=()=>{
      if(!active||!profileId)return;
      if(requestsInFlight.has(profileId)){schedule(100);return;}
      requestsInFlight.add(profileId);
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
      }).finally(()=>{requestsInFlight.delete(profileId);});
    };
    if(!profileId){setState({loading:false,error:false,operations:[]});return()=>{active=false;};}
    setState(current=>({loading:current.operations.length===0,error:false,operations:current.operations}));
    load();
    return()=>{active=false;if(timer!==undefined)clearTimeout(timer);};
  },[profileId,generation,request,requestsInFlight]);
  return <section className="panel" aria-label="Saved operation progress">
    <div className="toolbar"><h2>Saved operation progress</h2><button type="button" className="quiet" onClick={refresh} disabled={!profileId}>Refresh progress</button></div>
    {!profileId?<p>Select a profile to view saved operations.</p>:<><OperationProgress operations={state.operations} loading={state.loading} error={state.error&&state.operations.length===0} recovered/>{state.error&&state.operations.length>0&&<p role="alert">The latest progress could not be refreshed. Showing saved progress and retrying automatically.</p>}</>}
  </section>;
}
