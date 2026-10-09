import React from 'react';

export type MatchExplanationResolution = 'CURRENT'|'LEGACY_DEFAULT';
export type MatchProfileSelection = 'ROLE_FAMILY'|'DEFAULT_PROFILE'|'LEGACY_DEFAULT';

export type MatchExplanationContribution = {
  dimension:string;
  weight:number;
  raw_score:number|null;
  weighted_contribution:number|null;
  explanation:string;
  provenance:{source:string;evidence_ref?:string};
};

export type MatchExplanationResponse = {
  profile_id:string;
  search_profile_id:string;
  job_id:string;
  score:number;
  resolution:MatchExplanationResolution;
  profile_resolution:{
    requested_role_family:string|null;
    selected_role_family:string|null;
    profile_id:string|null;
    profile_version:string|null;
    selection:MatchProfileSelection;
  };
  scoring_profile:{id:string;version:string;role_family:string}|null;
  scoring_contributions:MatchExplanationContribution[];
};

export type MatchingExplanationPanelProps = {
  profileId:string|null;
  searchProfileId:string|null;
  jobId:string|null;
  state:'loading'|'error'|'ready'|'empty';
  explanation:MatchExplanationResponse|null;
  error:string|null;
  onRetry?:()=>void;
};

function isRecord(value:unknown):value is Record<string,unknown> {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}

function isText(value:unknown):value is string {
  return typeof value==='string'&&value.trim().length>0;
}

function isNullableText(value:unknown):value is string|null {
  return value===null||isText(value);
}

function isScore(value:unknown):value is number {
  return typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100;
}

function isNullableScore(value:unknown):value is number|null {
  return value===null||isScore(value);
}

function isContribution(value:unknown):value is MatchExplanationContribution {
  if(!isRecord(value)||!isText(value.dimension)||!isScore(value.weight)
    ||!isNullableScore(value.raw_score)||!isNullableScore(value.weighted_contribution)
    ||!isText(value.explanation)||!isRecord(value.provenance)
    ||!isText(value.provenance.source)) return false;
  if(value.provenance.evidence_ref!==undefined&&!isText(value.provenance.evidence_ref)) return false;
  const unknown=value.raw_score===null&&value.weighted_contribution===null;
  return (value.raw_score===null)===(value.weighted_contribution===null)
    &&(unknown?value.provenance.source==='UNKNOWN':value.provenance.source!=='UNKNOWN');
}

function isValidExplanation(value:unknown):value is MatchExplanationResponse {
  if(!isRecord(value)||!isText(value.profile_id)||!isText(value.search_profile_id)
    ||!isText(value.job_id)||!isScore(value.score)
    ||(value.resolution!=='CURRENT'&&value.resolution!=='LEGACY_DEFAULT')
    ||!isRecord(value.profile_resolution)||!Array.isArray(value.scoring_contributions)
    ||!value.scoring_contributions.every(isContribution)) return false;
  const dimensions=value.scoring_contributions.map(row=>(row as MatchExplanationContribution).dimension);
  if(new Set(dimensions).size!==dimensions.length) return false;

  const resolution=value.profile_resolution;
  if(!isNullableText(resolution.requested_role_family)
    ||!isNullableText(resolution.selected_role_family)
    ||!isNullableText(resolution.profile_id)
    ||!isNullableText(resolution.profile_version)
    ||!['ROLE_FAMILY','DEFAULT_PROFILE','LEGACY_DEFAULT'].includes(String(resolution.selection))) {
    return false;
  }
  const selected=value.scoring_profile;
  if(value.resolution==='LEGACY_DEFAULT') {
    return selected===null&&resolution.selection==='LEGACY_DEFAULT'
      &&resolution.selected_role_family===null&&resolution.profile_id===null
      &&resolution.profile_version===null;
  }
  return isRecord(selected)&&isText(selected.id)&&isText(selected.version)
    &&isText(selected.role_family)&&resolution.selection!=='LEGACY_DEFAULT'
    &&resolution.profile_id===selected.id&&resolution.profile_version===selected.version
    &&resolution.selected_role_family===selected.role_family;
}

function dimensionLabel(dimension:string):string {
  if(dimension==='ai_intensity') return 'AI intensity';
  return dimension.replaceAll('_',' ').replace(/\b\w/g,letter=>letter.toUpperCase());
}

function scoreLabel(value:number|null):string {
  return value===null?'UNKNOWN':`${value}`;
}

function ScopeMismatch({onRetry}:{onRetry?:()=>void}) {
  return <div role="alert">
    <p>This saved score explanation does not match the selected profile, saved search, and job. Its score details are hidden.</p>
    {onRetry&&<button type="button" onClick={onRetry}>Refresh explanation</button>}
  </div>;
}

export function MatchingExplanationPanel({
  profileId,searchProfileId,jobId,state,explanation,error,onRetry,
}:MatchingExplanationPanelProps) {
  const scoped=Boolean(profileId&&searchProfileId&&jobId);
  const inScope=scoped&&explanation?.profile_id===profileId
    &&explanation.search_profile_id===searchProfileId&&explanation.job_id===jobId;

  return <section className="panel" aria-labelledby="matching-explanation-title">
    <h2 id="matching-explanation-title">Saved match explanation</h2>
    <p role="note"><strong>Advisory score.</strong> This breakdown explains the saved ranking and does not change hard eligibility, saved-search rules, or application decisions.</p>
    {state==='loading'?<p role="status" aria-busy="true">Loading saved match explanation…</p>
      :state==='error'?<>
        <p role="alert">{error||'The saved match explanation is unavailable.'}</p>
        {onRetry&&<button type="button" onClick={onRetry}>Retry explanation</button>}
      </>
      :!scoped?<p role="status">Select a profile, saved search, and job to view a score explanation.</p>
      :!explanation||state==='empty'?<p role="status">No saved score breakdown is available for this selection.</p>
      :!inScope?<ScopeMismatch onRetry={onRetry}/>
      :!isValidExplanation(explanation)?<p role="alert">Saved score explanation is unavailable because its attribution or contribution data is invalid.</p>
      :<>
        <p aria-label="Saved match score"><strong>Saved match score:</strong> {scoreLabel(explanation.score)} / 100</p>
        {explanation.resolution==='CURRENT'&&explanation.scoring_profile
          ?<dl aria-label="Selected scoring profile">
            <div><dt>Selected role family</dt><dd>{explanation.profile_resolution.selected_role_family}</dd></div>
            <div><dt>Scoring profile</dt><dd>{explanation.scoring_profile.id}</dd></div>
            <div><dt>Profile version</dt><dd>{explanation.scoring_profile.version}</dd></div>
            <div><dt>Profile selection</dt><dd>{explanation.profile_resolution.selection}</dd></div>
          </dl>
          :<p aria-label="Selected scoring profile">Legacy default scoring; no user scoring profile is attributed to this saved result.</p>}
        <table aria-label="Scoring signal contributions">
          <caption>Scoring signal contributions</caption>
          <thead><tr>
            <th scope="col">Dimension</th><th scope="col">Weight</th>
            <th scope="col">Raw score</th><th scope="col">Weighted contribution</th>
            <th scope="col">Explanation</th><th scope="col">Provenance</th>
          </tr></thead>
          <tbody>{explanation.scoring_contributions.map(row=><tr key={row.dimension}>
            <th scope="row">{dimensionLabel(row.dimension)}</th>
            <td>{scoreLabel(row.weight)}</td>
            <td>{scoreLabel(row.raw_score)}</td>
            <td>{scoreLabel(row.weighted_contribution)}</td>
            <td>{row.explanation}</td>
            <td><span>{row.provenance.source}</span>
              {row.provenance.evidence_ref&&<> · evidence <code>{row.provenance.evidence_ref}</code></>}
            </td>
          </tr>)}</tbody>
        </table>
      </>}
  </section>;
}
