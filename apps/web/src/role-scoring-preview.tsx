import React,{useEffect,useRef,useState} from 'react';
import type {RoleScoringProfileDraft} from './role-scoring-profile-client';
import {createScoringPreviewClient,type PreviewClient,type ScoringPreview} from './role-scoring-preview-client';

const defaultClient=createScoringPreviewClient();
export type PreviewProps={profileId:string;searchProfileId:string;draft:RoleScoringProfileDraft;
  expectedVersion:string|null;disabled?:boolean;client?:PreviewClient};

export function ScoringPreviewResults({value}:{value:ScoringPreview}) {
  return <div aria-label="Draft ranking preview results">
    <p role="status">Preview only ? not saved. {value.eligible_count} ranked out of {value.scanned_count} inspected stored matches.</p>
    <p>{value.limitation}</p>
    {value.truncated&&<p role="note">The {value.candidate_limit}-job inspection limit was reached. These are sample ranks, not global ranks.</p>}
    {value.skipped_count>0&&<p role="note">{value.skipped_count} malformed records were left unscored.</p>}
    <p>Career profile version: <code>{value.profile_version}</code>. Current scoring version: <code>{value.current_version??'Legacy default'}</code>.</p>
    {value.jobs.length===0?<p>No supported current-profile matches remain in this sample. Discover and score jobs before previewing.</p>:
      <table aria-label="Draft weight ranking comparison"><thead><tr>
        <th scope="col">Job</th><th scope="col">Current score</th><th scope="col">Draft score</th>
        <th scope="col">Change</th><th scope="col">Sample rank</th><th scope="col">Draft evidence</th>
      </tr></thead><tbody>{value.jobs.map(row=><tr key={row.job_id}>
        <th scope="row">{row.title}</th><td>{row.current_score}</td><td>{row.draft_score}</td>
        <td>{row.delta>0?'+':''}{row.delta}</td><td>{row.current_rank} ? {row.draft_rank}</td>
        <td><details><summary>Score contributions</summary>{row.draft_contributions.map(item=><p key={item.dimension}>
          <strong>{item.dimension.replaceAll('_',' ')}</strong>: raw {item.raw_score??'UNKNOWN'}; weight {item.weight}; contribution {item.weighted_contribution??'UNKNOWN'}. {item.explanation} Source: {item.provenance.source}.
        </p>)}</details></td>
      </tr>)}</tbody></table>}
  </div>;
}

export function RoleScoringPreview({profileId,searchProfileId,draft,expectedVersion,disabled=false,client=defaultClient}:PreviewProps) {
  const key=JSON.stringify({profileId,searchProfileId,draft,expectedVersion});
  const currentKey=useRef(key);currentKey.current=key;
  const sequence=useRef(0);
  const active=useRef<AbortController|null>(null);
  const [state,setState]=useState<{key:string;busy:boolean;value:ScoringPreview|null;error:string|null}>({key,busy:false,value:null,error:null});
  useEffect(()=>{sequence.current++;active.current?.abort();return ()=>{sequence.current++;active.current?.abort();};},[key]);
  const visible=state.key===key?state:null;
  const preview=async()=>{
    active.current?.abort();
    const controller=new AbortController();active.current=controller;
    const requestId=++sequence.current;
    setState({key,busy:true,value:null,error:null});
    try {
      const value=await client({profileId,searchProfileId,draft,expectedVersion,signal:controller.signal});
      if(!controller.signal.aborted&&sequence.current===requestId&&currentKey.current===key) setState({key,busy:false,value,error:null});
    } catch(error) {
      if(!controller.signal.aborted&&sequence.current===requestId&&currentKey.current===key) setState({key,busy:false,value:null,
        error:error instanceof Error?error.message:'The preview failed. Nothing was saved.'});
    }
  };
  return <div className="scoring-preview" aria-label="Read-only scoring preview">
    <h3>Try weights before saving</h3>
    <p>Compare this unsaved draft with current weights on a bounded sample of your stored matches. Previewing does not save settings, change application rules or contact an employer.</p>
    <button type="button" disabled={disabled||!!visible?.busy} onClick={()=>void preview()}>{visible?.busy?'Calculating preview?':'Preview ranking'}</button>
    {visible?.error&&<p role="alert">{visible.error}</p>}
    {visible?.value&&<ScoringPreviewResults value={visible.value}/>}
  </div>;
}
