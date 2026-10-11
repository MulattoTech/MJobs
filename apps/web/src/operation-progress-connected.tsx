import React,{useCallback,useEffect,useState} from 'react';
import {api} from './api';
import {OperationProgress,type OperationProgressRecord} from './operation-progress';

type ProgressResponse={operations:OperationProgressRecord[]};
type ProgressRequest=(path:string)=>Promise<ProgressResponse>;
const defaultProgressRequest:ProgressRequest=path=>api<ProgressResponse>(path);
export function OperationProgressConnected({profileId,request=defaultProgressRequest}:{profileId:string|null;request?:ProgressRequest}){
  const [generation,setGeneration]=useState(0);
  const [state,setState]=useState<{loading:boolean;error:boolean;operations:OperationProgressRecord[]}>({loading:false,error:false,operations:[]});
  const refresh=useCallback(()=>setGeneration(value=>value+1),[]);
  useEffect(()=>{
    let active=true;
    if(!profileId){setState({loading:false,error:false,operations:[]});return()=>{active=false;};}
    setState({loading:true,error:false,operations:[]});
    void request(`/profiles/${encodeURIComponent(profileId)}/operations/progress`).then(value=>{
      if(!value||!Array.isArray(value.operations))throw new Error('OPERATIONS_RESPONSE_INVALID');
      if(active)setState({loading:false,error:false,operations:value.operations});
    }).catch(()=>{if(active)setState({loading:false,error:true,operations:[]});});
    return()=>{active=false;};
  },[profileId,generation,request]);
  return <section className="panel" aria-label="Saved operation progress">
    <div className="toolbar"><h2>Saved operation progress</h2><button type="button" className="quiet" onClick={refresh} disabled={!profileId}>Refresh progress</button></div>
    {!profileId?<p>Select a profile to view saved operations.</p>:<OperationProgress operations={state.operations} loading={state.loading} error={state.error} recovered/>}
  </section>;
}
