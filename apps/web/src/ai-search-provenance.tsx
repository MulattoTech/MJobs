export function AIEvaluationFreshness({status}:{status?:'CURRENT'|'STALE'|'UNKNOWN'}) {
  if(status==='CURRENT')return null;
  return <p role="status">{status==='STALE'
    ? 'Job or profile changed. This saved result is historical; evaluate and review again before preparing documents.'
    : 'Input freshness cannot be verified for this saved result. Evaluate and review again before preparing documents.'}</p>;
}

export type AISearchScope = {search_profile_id:string;name:string;criteria_digest:string;updated_at:string};
export type AIProviderAccessInfo = {provider:string;credential_source:'SAVED_PROFILE'|'RUNTIME_CONFIGURATION'|'DISABLED';destination:string|null};

export function AIProviderAccess({value}:{value?:AIProviderAccessInfo|null}) {
  if(!value)return <p className="muted">Credential source was not recorded for this saved plan.</p>;
  if(value.credential_source==='DISABLED')return <p className="muted">AI provider disabled. This local preview makes no model calls.</p>;
  if(value.credential_source==='SAVED_PROFILE')return <p className="muted">Uses this profile's saved OpenAI credential. Requests go only to {value.destination}. A key change requires a new plan.</p>;
  return <p className="muted">Provider access: runtime configuration ({value.provider}). Review the configured service before requesting evaluation.</p>;
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
