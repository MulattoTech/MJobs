import React, { useEffect, useState } from 'react';
import {
  ApplicationPriority,
  type ApplicationPriorityExplanation,
  type ApplicationPriorityState,
} from './application-priority';

export type ApplicationPriorityApiResponse = ApplicationPriorityExplanation & {
  unknowns:string[];
};

export type GetApplicationPriority = (
  profileId:string,
  jobId:string,
) => Promise<ApplicationPriorityApiResponse>;

export type ApplicationPriorityPanelState = {
  state:ApplicationPriorityState;
  explanation:ApplicationPriorityExplanation|null;
};

type ApplicationPriorityPanelProps = {
  profileId:string;
  jobId:string;
  getApplicationPriority:GetApplicationPriority;
};

function record(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}

function score(value:unknown,nullable=false):boolean {
  return (nullable&&value===null)
    ||(typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100);
}

function responseHasValidShape(value:unknown):value is ApplicationPriorityApiResponse {
  if(!record(value)
    ||typeof value.profile_id!=='string'||typeof value.job_id!=='string'
    ||!score(value.priority_score,true)||!score(value.match_score,true)
    ||!['UNKNOWN','LOW','NORMAL','HIGH'].includes(String(value.priority_band))
    ||!score(value.coverage_percent)
    ||value.advisory!==true||value.affects_match_score!==false
    ||!Array.isArray(value.unknowns)||!value.unknowns.every(item=>typeof item==='string')
    ||!Array.isArray(value.factors)) return false;

  const keys:string[]=[];
  for(const factor of value.factors) {
    if(!record(factor)
      ||typeof factor.key!=='string'||!factor.key.trim()
      ||typeof factor.label!=='string'||!factor.label.trim()
      ||!['KNOWN','UNKNOWN'].includes(String(factor.status))
      ||typeof factor.explanation!=='string'||!factor.explanation.trim()
      ||typeof factor.source!=='string'||!factor.source.trim()
      ||!score(factor.score,true)||!score(factor.contribution,true)) return false;
    if(factor.status==='UNKNOWN'&&(factor.score!==null||factor.contribution!==null)) return false;
    if(factor.status==='KNOWN'&&(!score(factor.score)||!score(factor.contribution))) return false;
    keys.push(factor.key);
  }
  return new Set(keys).size===keys.length
    &&JSON.stringify(value.unknowns)===JSON.stringify(value.factors
      .filter((factor:Record<string,unknown>)=>factor.status==='UNKNOWN')
      .map((factor:Record<string,unknown>)=>factor.key));
}

export async function loadApplicationPriority(
  getApplicationPriority:GetApplicationPriority,
  profileId:string,
  jobId:string,
):Promise<ApplicationPriorityPanelState> {
  try {
    const response:unknown=await getApplicationPriority(profileId,jobId);
    if(!responseHasValidShape(response)) return {state:'error',explanation:null};
    return {state:'ready',explanation:response};
  } catch {
    return {state:'error',explanation:null};
  }
}

export function mapPriorityPanelState(
  profileId:string,
  jobId:string,
  loaded:ApplicationPriorityPanelState,
) {
  return {profileId,jobId,state:loaded.state,explanation:loaded.explanation};
}

export function ApplicationPriorityPanel({profileId,jobId,getApplicationPriority}:ApplicationPriorityPanelProps) {
  const [attempt,setAttempt]=useState(0);
  const [loaded,setLoaded]=useState<ApplicationPriorityPanelState>({state:'loading',explanation:null});

  useEffect(()=>{
    let current=true;
    setLoaded({state:'loading',explanation:null});
    void loadApplicationPriority(getApplicationPriority,profileId,jobId).then(result=>{
      if(current) setLoaded(result);
    });
    return ()=>{ current=false; };
  },[getApplicationPriority,profileId,jobId,attempt]);

  const retry=()=>{
    setLoaded({state:'loading',explanation:null});
    setAttempt(previous=>previous+1);
  };

  return <ApplicationPriority {...mapPriorityPanelState(profileId,jobId,loaded)} onRetry={retry}/>;
}
