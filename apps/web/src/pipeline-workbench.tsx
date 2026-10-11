import React, { useEffect, useState } from 'react';
import { api } from './api';

export const PIPELINE_STAGES = [
  'DISCOVERED','NEEDS_SCORING','NEEDS_REVIEW','READY_TO_PREPARE','PREPARING','PREPARED','TRACKING',
] as const;
export type PipelineStage = typeof PIPELINE_STAGES[number];
export type PipelineNextAction = 'REVIEW_MATCH'|'PREPARE'|'WAIT_FOR_OPERATION'|'OPEN_APPLICATION'|'REVIEW_ROUTE'|'NONE';
export const PIPELINE_OPERATION_STATES = ['QUEUED','RUNNING','COMPLETED','FAILED','BLOCKED','CANCELLED','DEAD_LETTERED','DISCARDED'] as const;
export type PipelineOperationState = typeof PIPELINE_OPERATION_STATES[number];
export type PipelineWorkbenchItem = {
  job_id:string;
  title:string;
  company:string;
  score:number|null;
  application_id:string|null;
  stage:PipelineStage;
  blockers:Array<{code:string;message:string}>;
  can_prepare:boolean;
  fingerprint:string|null;
  route_id:string|null;
  next_action:PipelineNextAction;
  operation_id?:string|null;
};
export type PipelineWorkbenchResponse = {
  profile_id:string;
  profile_version:string;
  items:PipelineWorkbenchItem[];
  counts:{total:number;by_stage:Partial<Record<PipelineStage,number>>};
};
export type PipelinePrepareRequest = {request_id:string;expected_profile_version:string;fingerprint:string};
export type PipelinePrepareAcknowledgement = {request_id:string;job_id:string;operation_id:string;state:PipelineOperationState;next_action:'REFRESH_WORKBENCH'};
export type PipelineWorkbenchClient = {
  prepare:(profileId:string,jobId:string,body:PipelinePrepareRequest)=>Promise<unknown>;
};

const STAGE_LABELS:Record<PipelineStage,string> = {
  DISCOVERED:'Newly discovered',
  NEEDS_SCORING:'Needs scoring',
  NEEDS_REVIEW:'Needs review',
  READY_TO_PREPARE:'Ready to prepare',
  PREPARING:'Preparation in progress',
  PREPARED:'Prepared',
  TRACKING:'Tracking',
};
const NEXT_ACTION_LABELS:Record<PipelineNextAction,string> = {
  REVIEW_MATCH:'Review match and requirements',
  PREPARE:'Prepare application documents',
  WAIT_FOR_OPERATION:'Wait for preparation to finish',
  OPEN_APPLICATION:'Open existing application',
  REVIEW_ROUTE:'Review application route',
  NONE:'No next action recorded',
};
const SAFE_BLOCKERS:Record<string,string> = {
  SCORE_MISSING:'This job needs a current profile score.',
  SCORE_STALE:'The profile changed since this job was scored.',
  SCORE_INVALID:'The stored profile score is not usable.',
  DOCUMENTS_MISSING:'Resume and cover letter have not been prepared.',
  DOCUMENTS_STALE:'Prepared documents use an older profile version.',
  DOCUMENTS_INCOMPLETE:'Resume and cover letter are not both available.',
  FACTS_UNVERIFIED:'At least one verified, approved profile fact is required.',
  ROUTE_UNAVAILABLE:'No usable application route is available.',
  ROUTE_REVIEW_REQUIRED:'Choose the application route that matches this posting.',
  ROUTE_SELECTION_REQUIRED:'Choose an application route before preparing.',
  MOCK_ROUTE_IN_PERSONAL_MODE:'Fixture routes are unavailable in personal mode.',
  APPLICATION_STATE_LOCKED:'The existing application state needs review.',
  PREPARATION_FAILED:'The last preparation attempt failed and needs review.',
  DISCOVERY_RUNNING:'A current profile scoring operation is still running.',
  WORKSPACE_MODE_UNAVAILABLE:'Bind this workspace to an operation mode before preparing.',
};
const STAGE_FILTERS = ['ALL',...PIPELINE_STAGES] as const;
type StageFilter = typeof STAGE_FILTERS[number];

function isRecord(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}
function boundedText(value:unknown,max=400):string|null {
  if(typeof value!=='string'||value.length>max)return null;
  return value.replace(/[\u0000-\u001f\u007f]/g,' ').replace(/\s+/g,' ').trim();
}
function isStage(value:unknown):value is PipelineStage {
  return typeof value==='string'&&(PIPELINE_STAGES as readonly string[]).includes(value);
}
function isNextAction(value:unknown):value is PipelineNextAction {
  return value==='REVIEW_MATCH'||value==='PREPARE'||value==='WAIT_FOR_OPERATION'||
    value==='OPEN_APPLICATION'||value==='REVIEW_ROUTE'||value==='NONE';
}
function isPipelineOperationState(value:unknown):value is PipelineOperationState {
  return typeof value==='string'&&(PIPELINE_OPERATION_STATES as readonly string[]).includes(value);
}
function validOptionalId(value:unknown):value is string|null {
  return value===null||(typeof value==='string'&&value.length>0&&value.length<=200);
}
function validCount(value:unknown):value is number {
  return Number.isInteger(value)&&typeof value==='number'&&value>=0&&value<=1_000_000;
}

export function parsePipelineWorkbench(value:unknown,profileId:string):PipelineWorkbenchResponse {
  if(!isRecord(value)||value.profile_id!==profileId)throw new Error('PIPELINE_SCOPE_MISMATCH');
  if(typeof value.profile_version!=='string'||!value.profile_version||value.profile_version.length>128||
     !Array.isArray(value.items)||value.items.length>500||!isRecord(value.counts)||
     !validCount(value.counts.total)||!isRecord(value.counts.by_stage))throw new Error('PIPELINE_RESPONSE_INVALID');
  const byStage:Partial<Record<PipelineStage,number>>={};
  for(const [key,count] of Object.entries(value.counts.by_stage)) {
    if(!isStage(key)||!validCount(count))throw new Error('PIPELINE_RESPONSE_INVALID');
    byStage[key]=count;
  }
  const items:PipelineWorkbenchItem[]=[];
  const jobIds=new Set<string>();
  for(const row of value.items) {
    if(!isRecord(row)||typeof row.job_id!=='string'||!row.job_id||row.job_id.length>200||jobIds.has(row.job_id)||
       typeof row.title!=='string'||row.title.length>300||typeof row.company!=='string'||row.company.length>300||
       !(row.score===null||(typeof row.score==='number'&&Number.isFinite(row.score)&&row.score>=0&&row.score<=100))||
       !validOptionalId(row.application_id)||!isStage(row.stage)||!Array.isArray(row.blockers)||row.blockers.length>20||
       typeof row.can_prepare!=='boolean'||!validOptionalId(row.fingerprint)||!validOptionalId(row.route_id)||
       !isNextAction(row.next_action)||!(row.operation_id===undefined||validOptionalId(row.operation_id))) {
      throw new Error('PIPELINE_RESPONSE_INVALID');
    }
    const blockers:Array<{code:string;message:string}>=[];
    for(const blocker of row.blockers) {
      if(!isRecord(blocker))throw new Error('PIPELINE_RESPONSE_INVALID');
      const code=boundedText(blocker.code,100);
      const message=boundedText(blocker.message,400);
      if(!code||!message||SAFE_BLOCKERS[code]!==message)throw new Error('PIPELINE_RESPONSE_INVALID');
      blockers.push({code,message});
    }
    jobIds.add(row.job_id);
    items.push({
      job_id:row.job_id,
      title:boundedText(row.title,300)||'Job title unavailable',
      company:boundedText(row.company,300)||'Company unavailable',
      score:row.score,
      application_id:row.application_id,
      stage:row.stage,
      blockers,
      can_prepare:row.can_prepare&&row.stage==='READY_TO_PREPARE'&&typeof row.fingerprint==='string'&&!!row.fingerprint,
      fingerprint:row.fingerprint,
      route_id:row.route_id,
      next_action:row.next_action,
      ...(row.operation_id!==undefined?{operation_id:row.operation_id}:{}),
    });
  }
  return {profile_id:profileId,profile_version:value.profile_version,items,counts:{total:value.counts.total,by_stage:byStage}};
}

export async function queuePipelinePreparation(
  client:PipelineWorkbenchClient,
  profileId:string,
  profileVersion:string,
  item:PipelineWorkbenchItem,
  requestId:string,
):Promise<PipelinePrepareAcknowledgement> {
  if(!profileId||!profileVersion||!requestId||!item.can_prepare||item.stage!=='READY_TO_PREPARE'||!item.fingerprint) {
    throw new Error('PIPELINE_ITEM_NOT_READY');
  }
  const body={request_id:requestId,expected_profile_version:profileVersion,fingerprint:item.fingerprint};
  const response=await client.prepare(profileId,item.job_id,body);
  if(!isRecord(response)||response.request_id!==requestId||response.job_id!==item.job_id||
     typeof response.operation_id!=='string'||!response.operation_id||response.operation_id.length>200||
     !isPipelineOperationState(response.state)||response.next_action!=='REFRESH_WORKBENCH') {
    throw new Error('PIPELINE_QUEUE_ACK_INVALID');
  }
  return {request_id:requestId,job_id:item.job_id,operation_id:response.operation_id,state:response.state,next_action:'REFRESH_WORKBENCH'};
}

export function requestIdForPipelinePreparation(
  requestIds:Map<string,string>,profileId:string,jobId:string,createId:()=>string=()=>crypto.randomUUID(),
):string {
  const key=`${profileId}\u0000${jobId}`;
  const existing=requestIds.get(key);
  if(existing)return existing;
  const requestId=createId();
  if(!requestId||requestId.length>200)throw new Error('PIPELINE_REQUEST_ID_INVALID');
  requestIds.set(key,requestId);
  return requestId;
}

export function pipelinePreparationNotice(state:PipelineOperationState):string {
  switch(state) {
    case 'QUEUED':return 'Preparation queued. It has not been submitted.';
    case 'RUNNING':return 'Preparation is in progress. It has not been submitted.';
    case 'COMPLETED':return 'Preparation completed. No employer submission was made.';
    case 'FAILED':return 'Preparation failed. Refresh readiness to review safe reasons. No employer submission was made.';
    case 'BLOCKED':return 'Preparation is blocked. Refresh readiness to review safe reasons. No employer submission was made.';
    case 'CANCELLED':return 'Preparation was cancelled. No employer submission was made.';
    case 'DEAD_LETTERED':return 'Preparation stopped after repeated failures. Refresh readiness to review safe reasons. No employer submission was made.';
    case 'DISCARDED':return 'Preparation was discarded. No employer submission was made.';
  }
}

const apiClient:PipelineWorkbenchClient&{load:(profileId:string)=>Promise<unknown>}={
  load:(profileId)=>api<unknown>(`/profiles/${encodeURIComponent(profileId)}/pipeline-workbench`),
  prepare:(profileId,jobId,body)=>api<unknown>(
    `/profiles/${encodeURIComponent(profileId)}/pipeline-workbench/${encodeURIComponent(jobId)}/prepare`,
    {method:'POST',body:JSON.stringify(body)},
  ),
};

type ViewProps={
  profileId:string|null;
  workbench:PipelineWorkbenchResponse|null;
  loading:boolean;
  error:boolean;
  notice?:string|null;
  actionError?:string|null;
  stageFilter?:StageFilter;
  onStageFilter?:(value:StageFilter)=>void;
  onRetry:()=>void;
  onPrepare:(item:PipelineWorkbenchItem)=>void;
  onOpenApplication?:(applicationId:string)=>void;
  busyJobId?:string|null;
};

function selectedItems(items:PipelineWorkbenchItem[],filter:StageFilter):PipelineWorkbenchItem[] {
  return filter==='ALL'?items:items.filter(item=>item.stage===filter);
}

export function PipelineWorkbenchView({profileId,workbench,loading,error,notice,actionError,stageFilter='ALL',onStageFilter,
  onRetry,onPrepare,onOpenApplication,busyJobId}:ViewProps) {
  if(!profileId)return <section className="panel" aria-label="Pipeline workbench"><h2>Pipeline workbench</h2>
    <p>Select a profile to view its pipeline readiness.</p></section>;
  if(loading)return <section className="panel" aria-label="Pipeline workbench" aria-busy="true"><h2>Pipeline workbench</h2>
    {notice&&<p className="notice" role="status">{notice}</p>}<p role="status">Loading pipeline readiness…</p></section>;
  if(error)return <section className="panel" aria-label="Pipeline workbench"><h2>Pipeline workbench</h2>
    <p role="alert">Pipeline readiness could not be loaded for this profile.</p><button type="button" onClick={onRetry}>Retry</button></section>;
  if(!workbench)return <section className="panel" aria-label="Pipeline workbench"><h2>Pipeline workbench</h2>
    <p role="alert">Pipeline readiness is unavailable.</p><button type="button" onClick={onRetry}>Retry</button></section>;
  if(workbench.profile_id!==profileId)return <section className="panel" aria-label="Pipeline workbench">
    <h2>Pipeline workbench</h2><p role="alert">This workbench does not match the selected profile. Reload before viewing.</p>
    <button type="button" onClick={onRetry}>Reload pipeline</button></section>;

  const visible=selectedItems(workbench.items,stageFilter);
  const groups=stageFilter==='ALL'?PIPELINE_STAGES:[stageFilter];
  return <section className="panel pipeline-workbench" aria-label="Pipeline workbench">
    <div className="toolbar"><div><h2>Pipeline workbench</h2>
      <p>{workbench.counts.total} jobs in this profile. Readiness is profile-specific and may change when facts, routes, or documents change.</p></div>
      <button type="button" onClick={onRetry}>Refresh readiness</button></div>
    {notice&&<p className="notice" role="status">{notice}</p>}
    {actionError&&<p className="alert" role="alert">{actionError}</p>}
    <label>Group by readiness
      <select aria-label="Filter pipeline readiness stage" value={stageFilter} onChange={event=>onStageFilter?.(event.target.value as StageFilter)}>
        <option value="ALL">All readiness groups</option>
        {PIPELINE_STAGES.map(stage=><option key={stage} value={stage}>{STAGE_LABELS[stage]}</option>)}
      </select>
    </label>
    <div className="pipeline-stage-counts" aria-label="Readiness counts" style={{display:'flex',flexWrap:'wrap',gap:8,margin:'14px 0'}}>
      {PIPELINE_STAGES.filter(stage=>(workbench.counts.by_stage[stage]||0)>0).map(stage=><span className="tag" key={stage}>
        {STAGE_LABELS[stage]}: {workbench.counts.by_stage[stage]}
      </span>)}
    </div>
    {!visible.length?<p className="empty">{workbench.items.length===0
      ? 'No jobs are available in this profile’s pipeline yet.' : 'No jobs match this readiness group.'}</p>
      : <div className="pipeline-stage-groups">{groups.map(stage=>{
        const rows=visible.filter(item=>item.stage===stage);
        if(!rows.length)return null;
        return <section key={stage} className="pipeline-stage-group" aria-label={STAGE_LABELS[stage]} style={{margin:'18px 0'}}>
          <h3>{STAGE_LABELS[stage]} <span className="tag">{workbench.counts.by_stage[stage]||rows.length}</span></h3>
          <ul className="pipeline-workbench-items" style={{listStyle:'none',padding:0,display:'grid',gap:12}}>{rows.map(item=>{
            const busy=busyJobId===item.job_id;
            return <li key={item.job_id} className="fact" style={{margin:0}}>
              <div className="header-row"><div><h3 style={{marginTop:0}}>{item.title}</h3><p>{item.company}</p></div>
                <span className="tag">{item.score===null?'Unknown score — not treated as zero':`Match score ${Math.round(item.score)}`}</span>
              </div>
              <p><strong>Next action:</strong> {NEXT_ACTION_LABELS[item.next_action]}</p>
              {item.blockers.length?<ul aria-label={`Readiness reasons for ${item.title}`}>{item.blockers.map((blocker,index)=><li key={`${blocker.code}-${index}`}>{blocker.message}</li>)}</ul>
                : item.can_prepare?<p className="muted">Readiness checks passed for the current profile version.</p>
                  : <p className="muted">No additional readiness reason was recorded.</p>}
              <div className="pipeline-workbench-actions" style={{display:'flex',flexWrap:'wrap',gap:8,marginTop:12}}>
                {item.can_prepare&&item.stage==='READY_TO_PREPARE'&&<button type="button" className="primary"
                  disabled={busy||!!busyJobId} onClick={()=>onPrepare(item)}>{busy?'Queueing preparation…':'Prepare application documents'}</button>}
                {item.application_id&&onOpenApplication&&<button type="button" onClick={()=>onOpenApplication(item.application_id!)}>
                  Open existing application</button>}
              </div>
            </li>;
          })}</ul>
        </section>;
      })}</div>}
    <p className="muted">Preparing documents only creates local application materials. Queue acceptance is not employer submission or confirmation.</p>
  </section>;
}

export function PipelineWorkbench({profileId,onOpenApplication}:{profileId:string|null;onOpenApplication?:(applicationId:string)=>void}) {
  const [attempt,setAttempt]=useState(0);
  const [loaded,setLoaded]=useState<PipelineWorkbenchResponse|null>(null);
  const [loadedProfileId,setLoadedProfileId]=useState<string|null>(null);
  const [loading,setLoading]=useState(!!profileId);
  const [errorProfileId,setErrorProfileId]=useState<string|null>(null);
  const [actionError,setActionError]=useState<{profileId:string;text:string}|null>(null);
  const [notice,setNotice]=useState<{profileId:string;text:string}|null>(null);
  const [busyJobId,setBusyJobId]=useState<string|null>(null);
  const [stageFilter,setStageFilter]=useState<StageFilter>('ALL');
  const requestIds=React.useRef(new Map<string,string>());
  const scope=React.useRef(profileId);
  scope.current=profileId;

  useEffect(()=>{
    let current=true;
    setErrorProfileId(null);
    setActionError(null);
    if(!profileId){setLoaded(null);setLoadedProfileId(null);setLoading(false);setBusyJobId(null);setStageFilter('ALL');return ()=>{current=false;};}
    setLoading(true);
    setLoaded(null);
    setLoadedProfileId(null);
    setBusyJobId(null);
    setStageFilter('ALL');
    void apiClient.load(profileId).then(value=>{
      const result=parsePipelineWorkbench(value,profileId);
      if(current){setLoaded(result);setLoadedProfileId(profileId);}
    }).catch(()=>{if(current)setErrorProfileId(profileId);})
      .finally(()=>{if(current)setLoading(false);});
    return ()=>{current=false;};
  },[profileId,attempt]);

  async function prepare(item:PipelineWorkbenchItem) {
    if(!profileId||!loaded||loadedProfileId!==profileId||busyJobId)return;
    const activeProfile=profileId;
    const requestId=requestIdForPipelinePreparation(requestIds.current,activeProfile,item.job_id);
    setBusyJobId(item.job_id);setActionError(null);
    try {
      const acknowledgement=await queuePipelinePreparation(apiClient,activeProfile,loaded.profile_version,item,requestId);
      if(scope.current!==activeProfile)return;
      requestIds.current.delete(`${activeProfile}\u0000${item.job_id}`);
      setNotice({profileId:activeProfile,text:pipelinePreparationNotice(acknowledgement.state)});
      setAttempt(value=>value+1);
    } catch {
      if(scope.current===activeProfile)setActionError({profileId:activeProfile,text:'Readiness may have changed or preparation could not be queued. Refresh to review the latest reasons.'});
    } finally {
      if(scope.current===activeProfile)setBusyJobId(null);
    }
  }

  const activeNotice=notice?.profileId===profileId?notice.text:null;
  const activeActionError=actionError?.profileId===profileId?actionError.text:null;
  const activeError=errorProfileId===profileId;
  const visibleWorkbench=loadedProfileId===profileId?loaded:null;
  return <PipelineWorkbenchView profileId={profileId} workbench={visibleWorkbench} loading={loading||!!profileId&&!visibleWorkbench&&!activeError}
    error={activeError} notice={activeNotice} actionError={activeActionError} stageFilter={stageFilter} onStageFilter={setStageFilter}
    onRetry={()=>setAttempt(value=>value+1)} onPrepare={item=>void prepare(item)} onOpenApplication={onOpenApplication} busyJobId={busyJobId}/>;
}
