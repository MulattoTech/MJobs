export function AIEvaluationFreshness({status}:{status?:'CURRENT'|'STALE'|'UNKNOWN'}) {
  if(status==='CURRENT')return null;
  return <p role="status">{status==='STALE'
    ? 'Job or profile changed. This saved result is historical; evaluate and review again before preparing documents.'
    : 'Input freshness cannot be verified for this saved result. Evaluate and review again before preparing documents.'}</p>;
}

export type AISearchScope = {search_profile_id:string;name:string;criteria_digest:string;updated_at:string};
export type AIScoreEvidence = {base_score:number;score:number;scoring_profile:{selection:string;profile_version:string|null};
  scoring_contributions:{dimension:string;raw_score:number|null;weighted_contribution:number|null;explanation:string}[]};
export type AICandidate = {job_id:string;title:string;company:string;deterministic_score:number;score_evidence:AIScoreEvidence|null};

export function AISearchProvenance({scope}:{scope?:AISearchScope|null}) {
  return <p className="muted">{scope
    ? <>Saved search: <strong>{scope.name}</strong> · Criteria {scope.criteria_digest.slice(0,12)} · Scores captured for this evaluation; later search edits do not change this result.</>
    : 'Profile-wide evaluation using stored deterministic scores.'}</p>;
}

export function AIScoreDetails({value}:{value?:AIScoreEvidence|null}) {
  if(!value)return null;
  return <details><summary>Why this candidate was selected</summary>
    <p>Approved skill evidence baseline: {value.base_score}. Search-weighted score: {value.score}.</p>
    <ul>{value.scoring_contributions.map(row=><li key={row.dimension}>
      {row.dimension}: {row.raw_score===null?'UNKNOWN':row.raw_score} · contribution {row.weighted_contribution??'UNKNOWN'}. {row.explanation}
    </li>)}</ul>
  </details>;
}

export function AICandidatePreview({scope,candidates}:{scope?:AISearchScope|null;candidates:AICandidate[]}) {
  return <section aria-label="Planned AI candidates"><AISearchProvenance scope={scope}/>
    <p>Review these candidates before requesting evaluation. Changed inputs require a new preview.</p>
    {candidates.map(row=><div key={row.job_id}><strong>{row.title} · {row.company}</strong>
      <p>Deterministic score: {row.deterministic_score}</p><AIScoreDetails value={row.score_evidence}/></div>)}
  </section>;
}
