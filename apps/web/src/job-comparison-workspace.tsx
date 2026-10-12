import React, { useEffect, useRef, useState } from 'react';

export type JobComparisonChoice = {
  job_id:string;
  title:string;
  company:string;
};

export type JobComparisonRequest = {
  profile_id:string;
  search_profile_id:string;
  job_ids:string[];
};

export type JobComparisonProvenance = {
  source:string;
  evidence_ref?:string;
};

export type JobComparisonContribution = {
  dimension:string;
  weight:number;
  raw_score:number|null;
  weighted_contribution:number|null;
  explanation:string;
  provenance:JobComparisonProvenance;
};

export type ComparedJob = {
  job_id:string;
  title:string|null;
  company:string|null;
  match_score:number|null;
  scoring_contributions:JobComparisonContribution[];
};

export type SharedJobRequirement = {
  term:string;
  job_ids:string[];
  provenance:Array<JobComparisonProvenance&{job_id:string}>;
};

export type JobSpecificRequirement = {
  term:string;
  job_id:string;
  provenance:JobComparisonProvenance;
};

export type JobComparisonUnknown = {
  category:'required_terms'|'match_score'|'contributions';
  job_ids:string[];
  reason:'REQUIREMENTS_MISSING'|'REQUIREMENTS_INVALID'|'REQUIREMENT_TERM_UNKNOWN'
    |'REQUIREMENT_PROVENANCE_UNKNOWN'|'MATCH_SCORE_MISSING'|'MATCH_SCORE_INVALID'
    |'SCORING_CONTRIBUTIONS_MISSING'|'SCORING_CONTRIBUTIONS_INVALID';
};

export type JobComparisonResponse = {
  profile_id:string;
  search_profile_id:string;
  job_ids:string[];
  jobs:ComparedJob[];
  shared_requirements:SharedJobRequirement[];
  job_specific_requirements:JobSpecificRequirement[];
  unknowns:JobComparisonUnknown[];
};

export type JobComparisonCallback = (request:JobComparisonRequest)=>Promise<unknown>;
export type JobComparisonState = 'idle'|'loading'|'ready'|'error'|'stale'|'scope-mismatch';

export type JobComparisonLoadResult = {
  state:JobComparisonState;
  response:JobComparisonResponse|null;
  error:string|null;
};

export class JobComparisonWorkspaceError extends Error {
  constructor(readonly kind:'invalid_request'|'invalid_response'|'scope_mismatch') {
    super(kind);
    this.name='JobComparisonWorkspaceError';
  }
}

function isRecord(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}

function isText(value:unknown,maxLength=500):value is string {
  return typeof value==='string'&&!!value.trim()&&value.length<=maxLength;
}

function isIdentifier(value:unknown):value is string {
  return typeof value==='string'&&!!value.trim()&&value===value.trim()&&value.length<=128
    &&value!=='.'&&value!=='..'&&!/[\\/\u0000-\u001f\u007f]/.test(value);
}

function isScore(value:unknown,nullable:true):value is number|null;
function isScore(value:unknown,nullable?:false):value is number;
function isScore(value:unknown,nullable=false):value is number|null {
  return (nullable&&value===null)
    ||(typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100);
}

function invalidRequest():JobComparisonWorkspaceError {
  return new JobComparisonWorkspaceError('invalid_request');
}

function invalidResponse():JobComparisonWorkspaceError {
  return new JobComparisonWorkspaceError('invalid_response');
}

function scopeMismatch():JobComparisonWorkspaceError {
  return new JobComparisonWorkspaceError('scope_mismatch');
}

function validateRequest(request:JobComparisonRequest):string|null {
  if(!isIdentifier(request.profile_id)||!isIdentifier(request.search_profile_id)) {
    return 'Select a profile and saved search before comparing jobs.';
  }
  if(!Array.isArray(request.job_ids)||request.job_ids.length<2||request.job_ids.length>10
    ||!request.job_ids.every(isIdentifier)||new Set(request.job_ids).size!==request.job_ids.length) {
    return 'Select between 2 and 10 unique jobs to compare.';
  }
  return null;
}

function normalizeProvenance(value:unknown):JobComparisonProvenance {
  if(!isRecord(value)||!isText(value.source,128)
    ||(value.evidence_ref!==undefined&&!isText(value.evidence_ref,2000))) throw invalidResponse();
  return {
    source:value.source.trim(),
    ...(value.evidence_ref===undefined?{}:{evidence_ref:(value.evidence_ref as string).trim()}),
  };
}

function normalizeContribution(value:unknown):JobComparisonContribution {
  if(!isRecord(value)||!isText(value.dimension,100)||!isScore(value.weight)
    ||!isScore(value.raw_score,true)||!isScore(value.weighted_contribution,true)
    ||(value.raw_score===null)!==(value.weighted_contribution===null)
    ||!isText(value.explanation,2000)) throw invalidResponse();
  const provenance=normalizeProvenance(value.provenance);
  const unknown=value.raw_score===null;
  if((provenance.source==='UNKNOWN')!==unknown) throw invalidResponse();
  return {
    dimension:value.dimension.trim(),weight:value.weight,
    raw_score:value.raw_score,weighted_contribution:value.weighted_contribution,
    explanation:value.explanation.trim(),provenance,
  };
}

function normalizeJob(value:unknown):ComparedJob {
  if(!isRecord(value)||!isIdentifier(value.job_id)
    ||(value.title!==null&&!isText(value.title,300))
    ||(value.company!==null&&!isText(value.company,300))) throw invalidResponse();
  if(value.match_score!==undefined&&!isScore(value.match_score,true)) throw invalidResponse();
  const rawContributions=value.scoring_contributions;
  if(rawContributions!==undefined&&rawContributions!==null
    &&(!Array.isArray(rawContributions)||rawContributions.length>64)) throw invalidResponse();
  const contributions=(rawContributions??[]).map(normalizeContribution);
  if(new Set(contributions.map(row=>row.dimension)).size!==contributions.length) throw invalidResponse();
  return {
    job_id:value.job_id,
    title:typeof value.title==='string'?value.title.trim():null,
    company:typeof value.company==='string'?value.company.trim():null,
    match_score:value.match_score===undefined?null:value.match_score as number|null,
    scoring_contributions:contributions,
  };
}

function normalizeSharedRequirement(value:unknown,selected:Set<string>):SharedJobRequirement {
  if(!isRecord(value)||!isText(value.term,300)||!Array.isArray(value.job_ids)
    ||value.job_ids.length<2||!value.job_ids.every(isIdentifier)
    ||new Set(value.job_ids).size!==value.job_ids.length
    ||!Array.isArray(value.provenance)||value.provenance.length>10) throw invalidResponse();
  const jobIds=value.job_ids as string[];
  if(jobIds.some(jobId=>!selected.has(jobId))) throw scopeMismatch();
  const provenance=value.provenance.map(item=>{
    if(!isRecord(item)||!isIdentifier(item.job_id)) throw invalidResponse();
    const row=normalizeProvenance(item);
    if(!jobIds.includes(item.job_id)) throw scopeMismatch();
    return {...row,job_id:item.job_id};
  });
  if(new Set(provenance.map(row=>row.job_id)).size!==provenance.length) throw invalidResponse();
  return {term:value.term.trim(),job_ids:[...jobIds],provenance};
}

function normalizeSpecificRequirement(value:unknown,selected:Set<string>):JobSpecificRequirement {
  if(!isRecord(value)||!isText(value.term,300)||!isIdentifier(value.job_id)) throw invalidResponse();
  if(!selected.has(value.job_id)) throw scopeMismatch();
  return {term:value.term.trim(),job_id:value.job_id,provenance:normalizeProvenance(value.provenance)};
}

function normalizeUnknown(value:unknown,selected:Set<string>):JobComparisonUnknown {
  if(!isRecord(value)||!['required_terms','match_score','contributions'].includes(String(value.category))
    ||!['REQUIREMENTS_MISSING','REQUIREMENTS_INVALID','REQUIREMENT_TERM_UNKNOWN',
      'REQUIREMENT_PROVENANCE_UNKNOWN','MATCH_SCORE_MISSING','MATCH_SCORE_INVALID',
      'SCORING_CONTRIBUTIONS_MISSING','SCORING_CONTRIBUTIONS_INVALID'].includes(String(value.reason))
    ||!Array.isArray(value.job_ids)||value.job_ids.length<1||!value.job_ids.every(isIdentifier)
    ||new Set(value.job_ids).size!==value.job_ids.length) throw invalidResponse();
  const jobIds=value.job_ids as string[];
  if(jobIds.some(jobId=>!selected.has(jobId))) throw scopeMismatch();
  return {
    category:value.category as JobComparisonUnknown['category'],
    job_ids:[...jobIds],reason:value.reason as JobComparisonUnknown['reason'],
  };
}

export function normalizeJobComparisonResponse(
  value:unknown,
  request:JobComparisonRequest,
):JobComparisonResponse {
  const problem=validateRequest(request);
  if(problem) throw invalidRequest();
  if(!isRecord(value)) throw invalidResponse();
  if(value.profile_id!==request.profile_id||value.search_profile_id!==request.search_profile_id) {
    throw scopeMismatch();
  }
  if(!Array.isArray(value.job_ids)||!Array.isArray(value.jobs)
    ||!Array.isArray(value.shared_requirements)||!Array.isArray(value.job_specific_requirements)
    ||!Array.isArray(value.unknowns)) throw invalidResponse();
  const ids=value.job_ids;
  if(!ids.every(isIdentifier)||ids.length!==request.job_ids.length
    ||ids.some((id,index)=>id!==request.job_ids[index])) throw scopeMismatch();

  const jobs=value.jobs.map(normalizeJob);
  if(jobs.length!==request.job_ids.length
    ||jobs.some((job,index)=>job.job_id!==request.job_ids[index])) throw scopeMismatch();
  const selected=new Set(request.job_ids);
  const shared=value.shared_requirements.map(row=>normalizeSharedRequirement(row,selected));
  const specific=value.job_specific_requirements.map(row=>normalizeSpecificRequirement(row,selected));
  const unknowns=value.unknowns.map(row=>normalizeUnknown(row,selected));
  if(shared.length>256||specific.length>1024||unknowns.length>128) throw invalidResponse();
  return {
    profile_id:request.profile_id,search_profile_id:request.search_profile_id,
    job_ids:[...request.job_ids],jobs,
    shared_requirements:shared,job_specific_requirements:specific,unknowns,
  };
}

function responseErrorKind(value:unknown):'scope-mismatch'|'stale'|'error' {
  if(!isRecord(value)) return 'error';
  const detail=isRecord(value.detail)?value.detail:{};
  const code=value.kind??value.code??detail.code;
  if(code==='JOB_COMPARISON_SCOPE_MISMATCH') return 'scope-mismatch';
  if(code==='JOB_COMPARISON_STALE_CONTEXT') return 'stale';
  if(value.status===409) return 'stale';
  return 'error';
}

function stateError(state:'error'|'stale'|'scope-mismatch'):string {
  if(state==='scope-mismatch') return 'This comparison does not match the selected profile, saved search, and jobs. Its details are hidden.';
  if(state==='stale') return 'Saved job or profile context changed. Refresh or change the selected jobs before comparing again.';
  return 'Job comparison could not be loaded. Check the selected jobs and retry.';
}

export async function loadJobComparison(
  onCompare:JobComparisonCallback,
  request:JobComparisonRequest,
):Promise<JobComparisonLoadResult> {
  const problem=validateRequest(request);
  if(problem) return {state:'error',response:null,error:problem};
  try {
    const response=await onCompare({
      profile_id:request.profile_id,search_profile_id:request.search_profile_id,
      job_ids:[...request.job_ids],
    });
    return {
      state:'ready',response:normalizeJobComparisonResponse(response,request),error:null,
    };
  } catch(error) {
    if(error instanceof JobComparisonWorkspaceError) {
      const state=error.kind==='scope_mismatch'?'scope-mismatch':'error';
      return {state,response:null,error:stateError(state)};
    }
    const state=responseErrorKind(error);
    return {state,response:null,error:stateError(state)};
  }
}

export type JobComparisonWorkspaceViewProps = {
  profileId:string|null;
  searchProfileId:string|null;
  availableJobs:readonly JobComparisonChoice[];
  selectedJobIds:readonly string[];
  state:JobComparisonState;
  response:JobComparisonResponse|null;
  error:string|null;
  selectionError?:string|null;
  onSelectionChange:(jobIds:string[])=>void;
  onCompare:()=>void;
  onRetry:()=>void;
};

function eligibleChoices(availableJobs:readonly JobComparisonChoice[]):boolean {
  return Array.isArray(availableJobs)&&availableJobs.length>=2
    &&availableJobs.every(job=>isRecord(job)&&isIdentifier(job.job_id)
      &&isText(job.title,300)&&isText(job.company,300))
    &&new Set(availableJobs.map(job=>job.job_id)).size===availableJobs.length;
}

function scoreLabel(value:number|null):string {
  return value===null?'UNKNOWN':`${value}/100`;
}

function categoryLabel(category:JobComparisonUnknown['category']):string {
  if(category==='required_terms') return 'required terms';
  if(category==='match_score') return 'match score';
  return 'score contributions';
}

function jobLabel(job:ComparedJob):string {
  return `${job.title??job.job_id} — ${job.company??'Employer unavailable'}`;
}

function ContributionRows({job}: {job:ComparedJob}) {
  if(job.scoring_contributions.length===0) {
    return <p>Contribution evidence: UNKNOWN for this job.</p>;
  }
  return <table aria-label={`Saved score contributions for ${job.title??job.job_id}`}>
    <caption>Saved score contributions</caption>
    <thead><tr><th scope="col">Dimension</th><th scope="col">Weight</th>
      <th scope="col">Raw score</th><th scope="col">Contribution</th>
      <th scope="col">Explanation</th><th scope="col">Provenance</th></tr></thead>
    <tbody>{job.scoring_contributions.map(row=><tr key={row.dimension}>
      <th scope="row">{row.dimension}</th><td>{scoreLabel(row.weight)}</td>
      <td>{scoreLabel(row.raw_score)}</td><td>{scoreLabel(row.weighted_contribution)}</td>
      <td>{row.explanation}</td><td><span>{row.provenance.source}</span>
        {row.provenance.evidence_ref&&<> · evidence <code>{row.provenance.evidence_ref}</code></>}
      </td>
    </tr>)}</tbody>
  </table>;
}

function SharedRequirements({requirements,jobs}: {
  requirements:SharedJobRequirement[];
  jobs:ComparedJob[];
}) {
  return <section aria-labelledby="comparison-shared-requirements">
    <h3 id="comparison-shared-requirements">Shared observed requirements</h3>
    {requirements.length===0
      ?<p>No shared observed requirements are available (UNKNOWN evidence).</p>
      :<ul>{requirements.map((requirement,index)=><li key={`${requirement.term}-${index}`}>
        <strong>{requirement.term}</strong>
        <p>Observed in: {requirement.job_ids.map(id=>jobs.find(job=>job.job_id===id)?.title??id).join(', ')}</p>
        {requirement.provenance.length===0
          ?<p>Provenance: UNKNOWN</p>
          :<ul>{requirement.provenance.map(row=><li key={row.job_id}>
            {jobs.find(job=>job.job_id===row.job_id)?.title??row.job_id}: {row.source}
            {row.evidence_ref&&<> · evidence <code>{row.evidence_ref}</code></>}
          </li>)}</ul>}
      </li>)}</ul>}
  </section>;
}

function JobSpecificRequirements({requirements,jobs}: {
  requirements:JobSpecificRequirement[];
  jobs:ComparedJob[];
}) {
  return <section aria-labelledby="comparison-specific-requirements">
    <h3 id="comparison-specific-requirements">Job-specific observed requirements</h3>
    {requirements.length===0
      ?<p>No job-specific observed requirements are available (UNKNOWN evidence).</p>
      :<ul>{requirements.map((requirement,index)=><li key={`${requirement.job_id}-${requirement.term}-${index}`}>
        <strong>{requirement.term}</strong> — {jobs.find(job=>job.job_id===requirement.job_id)?.title??requirement.job_id}
        <span> · {requirement.provenance.source}</span>
        {requirement.provenance.evidence_ref&&<> · evidence <code>{requirement.provenance.evidence_ref}</code></>}
      </li>)}</ul>}
  </section>;
}

export function JobComparisonWorkspaceView({
  profileId,searchProfileId,availableJobs,selectedJobIds,state,response,error,selectionError,
  onSelectionChange,onCompare,onRetry,
}:JobComparisonWorkspaceViewProps) {
  let safeResponse:JobComparisonResponse|null=null;
  let displayState=state;
  let displayError=error;
  if(state==='ready'&&response&&profileId&&searchProfileId) {
    try {
      safeResponse=normalizeJobComparisonResponse(response,{
        profile_id:profileId,search_profile_id:searchProfileId,job_ids:[...selectedJobIds],
      });
    } catch(failure) {
      displayState=failure instanceof JobComparisonWorkspaceError&&failure.kind==='scope_mismatch'
        ?'scope-mismatch':'error';
      displayError=stateError(displayState);
    }
  } else if(state==='ready') {
    displayState='error';
    displayError=stateError('error');
  }
  const validScope=isIdentifier(profileId)&&isIdentifier(searchProfileId);
  const choicesValid=eligibleChoices(availableJobs);
  const validSelection=selectedJobIds.length>=2&&selectedJobIds.length<=10
    &&selectedJobIds.every(id=>availableJobs.some(job=>job.job_id===id))
    &&new Set(selectedJobIds).size===selectedJobIds.length;
  const canCompare=validScope&&choicesValid&&validSelection
    &&displayState!=='loading'&&displayState!=='stale'&&displayState!=='scope-mismatch';

  return <section className="panel" aria-labelledby="job-comparison-title">
    <h2 id="job-comparison-title">Compare saved job evidence</h2>
    <p role="note"><strong>Read-only comparison.</strong> Scores are saved per-job scores, not recalculated here. Observed requirements come from job descriptions and are not verified candidate qualifications. This view does not change hard eligibility or application decisions.</p>
    {!validScope
      ?<p role="status">Select a profile and saved search before comparing jobs.</p>
      :!choicesValid
        ?<p role="alert">Job choices are unavailable or have invalid scope data.</p>
        :<fieldset>
          <legend>Select between 2 and 10 jobs from the active profile and saved search</legend>
          {availableJobs.map(job=><label key={job.job_id}>
            <input type="checkbox" aria-label={`Compare ${job.title} at ${job.company}`}
              checked={selectedJobIds.includes(job.job_id)} disabled={state==='loading'}
              onChange={event=>{
                const next=event.currentTarget.checked
                  ?[...selectedJobIds,job.job_id]
                  :selectedJobIds.filter(id=>id!==job.job_id);
                onSelectionChange(next);
              }}/>
            {job.title} — {job.company}
          </label>)}
          <p aria-live="polite">{selectedJobIds.length} selected. Choose 2–10 unique jobs.</p>
          {selectionError&&<p role="alert">{selectionError}</p>}
          <button type="button" aria-label="Compare selected jobs" disabled={!canCompare||state==='loading'}
            onClick={onCompare}>{state==='loading'?'Comparing jobs…':'Compare selected jobs'}</button>
        </fieldset>}
    {displayState==='loading'&&<p role="status" aria-busy="true">Comparing selected jobs…</p>}
    {displayState==='scope-mismatch'&&<p role="alert">{displayError||stateError('scope-mismatch')}</p>}
    {displayState==='stale'&&<p role="alert">{displayError||stateError('stale')}</p>}
    {displayState==='error'&&<>
      <p role="alert">{displayError||stateError('error')}</p>
      <button type="button" onClick={onRetry}>Retry comparison</button>
    </>}
    {displayState==='ready'&&safeResponse&&<div aria-label="Job comparison results">
      <p>Profile {safeResponse.profile_id}; saved search {safeResponse.search_profile_id}.</p>
      <div className="job-comparison-results">
        {safeResponse.jobs.map(job=><section key={job.job_id} aria-labelledby={`comparison-job-${job.job_id}`}>
          <h3 id={`comparison-job-${job.job_id}`}>{jobLabel(job)}</h3>
          <p aria-label={`Saved match score for ${job.title??job.job_id}`}><strong>Saved match score:</strong> {scoreLabel(job.match_score)}</p>
          <ContributionRows job={job}/>
        </section>)}
      </div>
      <p>Observed in job descriptions; not verified candidate qualifications.</p>
      <SharedRequirements requirements={safeResponse.shared_requirements} jobs={safeResponse.jobs}/>
      <JobSpecificRequirements requirements={safeResponse.job_specific_requirements} jobs={safeResponse.jobs}/>
      {safeResponse.unknowns.length>0&&<section aria-label="Unknown comparison evidence">
        <h3>UNKNOWN evidence</h3>
        <ul>{safeResponse.unknowns.map((row,index)=><li key={`${row.category}-${row.reason}-${index}`}>
          UNKNOWN {categoryLabel(row.category)} for {row.job_ids.map(id=>safeResponse?.jobs.find(job=>job.job_id===id)?.title??id).join(', ')}: {row.reason}
        </li>)}</ul>
      </section>}
    </div>}
  </section>;
}

type WorkspaceModel=JobComparisonLoadResult&{scopeKey:string;requestKey:string};
type WorkspaceSelection={scopeKey:string;jobIds:string[]};

function emptyModel(scopeKey:string,jobIds:string[],state:JobComparisonState='idle'):WorkspaceModel {
  return {scopeKey,requestKey:JSON.stringify(jobIds),state,response:null,error:null};
}

function makeScopeKey(profileId:string|null,searchProfileId:string|null,jobs:readonly JobComparisonChoice[]):string {
  return JSON.stringify([profileId,searchProfileId,jobs.map(job=>[job.job_id,job.title,job.company])]);
}

export function JobComparisonWorkspace({
  profileId,searchProfileId,availableJobs,onCompare,
}: {
  profileId:string|null;
  searchProfileId:string|null;
  availableJobs:readonly JobComparisonChoice[];
  onCompare:JobComparisonCallback;
}) {
  const scopeKey=makeScopeKey(profileId,searchProfileId,availableJobs);
  const [selection,setSelection]=useState<WorkspaceSelection>({scopeKey,jobIds:[]});
  const [model,setModel]=useState<WorkspaceModel>(()=>emptyModel(scopeKey,[]));
  const [selectionError,setSelectionError]=useState<string|null>(null);
  const requestVersion=useRef(0);
  const selectedJobIds=selection.scopeKey===scopeKey?selection.jobIds:[];
  const visibleModel=model.scopeKey===scopeKey&&model.requestKey===JSON.stringify(selectedJobIds)
    ?model:emptyModel(scopeKey,selectedJobIds);

  useEffect(()=>{
    requestVersion.current+=1;
    setSelection({scopeKey,jobIds:[]});
    setModel(emptyModel(scopeKey,[]));
    setSelectionError(null);
  },[scopeKey]);

  const updateSelection=(jobIds:string[])=>{
    requestVersion.current+=1;
    if(jobIds.length>10) {
      setSelectionError('Select no more than 10 jobs at a time.');
      return;
    }
    setSelection({scopeKey,jobIds});
    setSelectionError(null);
    setModel(emptyModel(scopeKey,jobIds));
  };

  const compare=async()=>{
    if(!profileId||!searchProfileId||selection.scopeKey!==scopeKey) return;
    const request:JobComparisonRequest={
      profile_id:profileId,search_profile_id:searchProfileId,job_ids:[...selectedJobIds],
    };
    const problem=validateRequest(request);
    if(problem) {
      setSelectionError(problem);
      return;
    }
    const version=++requestVersion.current;
    const requestKey=JSON.stringify(selectedJobIds);
    setSelectionError(null);
    setModel({scopeKey,requestKey,state:'loading',response:null,error:null});
    const result=await loadJobComparison(onCompare,request);
    if(version===requestVersion.current) setModel({...result,scopeKey,requestKey});
  };

  const retry=()=>{ void compare(); };
  return <JobComparisonWorkspaceView
    profileId={profileId}
    searchProfileId={searchProfileId}
    availableJobs={availableJobs}
    selectedJobIds={selectedJobIds}
    state={visibleModel.state}
    response={visibleModel.response}
    error={visibleModel.error}
    selectionError={selectionError}
    onSelectionChange={updateSelection}
    onCompare={()=>{void compare();}}
    onRetry={retry}
  />;
}
