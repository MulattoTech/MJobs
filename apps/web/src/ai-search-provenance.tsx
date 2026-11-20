export function AIEvaluationFreshness({status}:{status?:'CURRENT'|'STALE'|'UNKNOWN'}) {
  if(status==='CURRENT')return null;
  return <p role="status">{status==='STALE'
    ? 'Job or profile changed. This saved result is historical; evaluate and review again before preparing documents.'
    : 'Input freshness cannot be verified for this saved result. Evaluate and review again before preparing documents.'}</p>;
}

export type AISearchScope = {search_profile_id:string;name:string;criteria_digest:string;updated_at:string};
export type AIJobEvidence = {status:'VERIFIED_QUOTES'|'UNKNOWN';quotes:{field:'title'|'description';quote:string}[]};
export function AIJobSourceEvidence({value}:{value?:AIJobEvidence|null}) {
  if(value?.status!=='VERIFIED_QUOTES'||!value.quotes.length)return <p className="muted">No verified posting quotes were recorded. The AI rationale is unverified advisory text.</p>;
  return <section aria-label="Checked posting quotes"><p>These quotes match the posting text supplied for this evaluation. Only the quotations are verified; the AI rationale and applicant qualifications are not.</p>
    {value.quotes.map((item,index)=><blockquote key={index}><p>{item.quote}</p><footer>Source: captured job {item.field}</footer></blockquote>)}</section>;
}
export type AIJobScoringDataFlow =
  | {status:'UNKNOWN'}
  | {status:'DISABLED';contract:'JOB_SCORING_V1'}
  | {status:'RECORDED';contract:'JOB_SCORING_V1';
      prompt_fields:['job_title','company','job_description','deterministic_match_score'];
      text_limits:{job_title:512;company:512;job_description:8000;prompt:12000};
      local_records:['evaluation_input_snapshot','advisory_results'];provider_retention:'UNKNOWN'};
export type AIProviderAccessInfo = {provider:string;credential_source:'SAVED_PROFILE'|'RUNTIME_CONFIGURATION'|'DISABLED';destination:string|null;data_flow?:AIJobScoringDataFlow};

function hasExactKeys(value:unknown,keys:string[]):value is Record<string,unknown> {
  return !!value&&typeof value==='object'&&!Array.isArray(value)&&
    Object.keys(value).length===keys.length&&keys.every(key=>Object.hasOwn(value,key));
}

function hasExactItems(value:unknown,items:string[]):boolean {
  return Array.isArray(value)&&value.length===items.length&&items.every((item,index)=>value[index]===item);
}

function isKnownRuntimeProvider(value:unknown):value is string {
  return typeof value==='string'&&['openai','ollama','openai-compatible','local','azure-openai','azure'].includes(value);
}

function jobScoringDisclosureStatus(value?:AIProviderAccessInfo|null):'UNKNOWN'|'DISABLED'|'RECORDED' {
  const flow:unknown=value?.data_flow;
  if(hasExactKeys(flow,['status','contract'])&&flow.status==='DISABLED'&&flow.contract==='JOB_SCORING_V1'&&
      value?.provider==='disabled'&&value.credential_source==='DISABLED'&&value.destination===null)return 'DISABLED';
  if(!hasExactKeys(flow,['status','contract','prompt_fields','text_limits','local_records','provider_retention'])||
      flow.status!=='RECORDED'||flow.contract!=='JOB_SCORING_V1'||flow.provider_retention!=='UNKNOWN'||
      !hasExactItems(flow.prompt_fields,['job_title','company','job_description','deterministic_match_score'])||
      !hasExactItems(flow.local_records,['evaluation_input_snapshot','advisory_results'])||
      !hasExactKeys(flow.text_limits,['job_title','company','job_description','prompt'])||
      flow.text_limits.job_title!==512||flow.text_limits.company!==512||
      flow.text_limits.job_description!==8000||flow.text_limits.prompt!==12000)return 'UNKNOWN';
  if(value?.credential_source==='SAVED_PROFILE'&&value.provider==='openai'&&value.destination==='https://api.openai.com')return 'RECORDED';
  if(value?.credential_source==='RUNTIME_CONFIGURATION'&&isKnownRuntimeProvider(value.provider)&&value.destination===null)return 'RECORDED';
  return 'UNKNOWN';
}

function AIJobScoringDisclosure({value}:{value?:AIProviderAccessInfo|null}) {
  const status=jobScoringDisclosureStatus(value);
  if(status==='UNKNOWN')return <p className="muted">Data-flow details were not recorded or cannot be verified for this saved plan.</p>;
  if(status==='DISABLED')return <p className="muted">Job-scoring AI is disabled for this plan. It authorizes no inference request.</p>;
  return <details aria-label="Job scoring data flow"><summary>What job scoring shares and keeps</summary>
    <p>Preview makes no model calls. Evaluation requires an explicit request; this disclosure does not prove a request was sent.</p>
    <p>This job-scoring prompt can include the job title and company (up to 512 characters each), job description (up to 8,000 characters), and deterministic match score. The score is derived from profile information. The complete prompt, including instructions, is limited to 12,000 characters.</p>
    <p>This job-scoring prompt does not add raw résumé text, contact details, career facts, or application answers. Job source text may itself contain sensitive information, so review it before evaluation.</p>
    <p>MJobs stores the evaluation input snapshot and advisory results in its database. Provider retention and deletion are unknown; cleaning up MJobs data does not establish deletion at the provider.</p>
    <p>This disclosure applies only to job scoring. Evaluation produces advisory results and does not submit an application; document preparation requires a separate review.</p>
  </details>;
}

export function AIProviderAccess({value}:{value?:AIProviderAccessInfo|null}) {
  return <>
    {!value?<p className="muted">Credential source was not recorded for this saved plan.</p>
      :value.credential_source==='DISABLED'?<p className="muted">AI provider disabled. This preview makes no model calls.</p>
      :value.credential_source==='SAVED_PROFILE'?<p className="muted">Uses this profile's saved OpenAI credential. Requests go only to {value.destination}. A key change requires a new plan.</p>
      :<p className="muted">Provider access: runtime configuration ({isKnownRuntimeProvider(value.provider)?value.provider:'unknown provider'}). The destination was not recorded for this plan. Review the configured service before requesting evaluation.</p>}
    <AIJobScoringDisclosure value={value}/>
  </>;
}

export type AIScoreEvidence = {basis?:string;limitation?:string;base_score:number;score:number;scoring_profile:{selection:string;profile_version:string|null};
  scoring_contributions:{dimension:string;raw_score:number|null;weighted_contribution:number|null;explanation:string}[]};
export type AICandidate = {job_id:string;title:string;company:string;deterministic_score:number;score_evidence:AIScoreEvidence|null};

export function AISearchProvenance({scope,basis}:{scope?:AISearchScope|null;basis?:string|null}) {
  return <p className="muted">{scope
    ? <>Saved search: <strong>{scope.name}</strong> · Criteria {scope.criteria_digest.slice(0,12)} · Scores captured for this evaluation; later search edits do not change this result.</>
    : basis==='CURRENT_APPROVED_SKILL_COVERAGE_V1'
      ? 'Profile-wide evaluation using current approved skill evidence captured for this plan. Later edits do not rewrite saved explanations.'
      : 'Profile-wide evaluation using stored deterministic scores.'}</p>;
}

export function AIScoreDetails({value}:{value?:AIScoreEvidence|null}) {
  if(!value)return null;
  return <details><summary>Why this candidate was selected</summary>
    {value.basis==='APPROVED_SKILL_COVERAGE'
      ? <><p>Approved skill evidence coverage: {value.score}%. The AI selection score uses the whole-number part.</p><p>{value.limitation}</p></>
      : <p>Approved skill evidence baseline: {value.base_score}. Search-weighted score: {value.score}.</p>}
    <ul>{value.scoring_contributions.map(row=><li key={row.dimension}>
      {row.dimension}: {row.raw_score===null?'UNKNOWN':row.raw_score} · contribution {row.weighted_contribution??'UNKNOWN'}. {row.explanation}
    </li>)}</ul>
  </details>;
}

export function AICandidatePreview({scope,basis,providerAccess,unscoredCount=0,candidates}:{scope?:AISearchScope|null;basis?:string|null;providerAccess?:AIProviderAccessInfo|null;unscoredCount?:number;candidates:AICandidate[]}) {
  return <section aria-label="Planned AI candidates"><AISearchProvenance scope={scope} basis={basis}/>
    <AIProviderAccess value={providerAccess}/>
    <p>Review these candidates before requesting evaluation. Changed inputs require a new preview.</p>
    {unscoredCount>0&&<p role="status">{unscoredCount} {unscoredCount===1?'job has':'jobs have'} unknown skill coverage and {unscoredCount===1?'is':'are'} excluded from AI work. Missing evidence is not a zero match or a qualification decision.</p>}
    {candidates.map(row=><div key={row.job_id}><strong>{row.title} · {row.company}</strong>
      <p>Deterministic score: {row.deterministic_score}</p><AIScoreDetails value={row.score_evidence}/></div>)}
  </section>;
}
