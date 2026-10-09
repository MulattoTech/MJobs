import React from 'react';

export type ApplicationPriorityFactor = {
  key:string;
  label:string;
  score:number|null;
  contribution:number|null;
  status:'KNOWN'|'UNKNOWN';
  explanation:string;
  source:string;
};

export type ApplicationPriorityExplanation = {
  profile_id:string;
  job_id:string;
  priority_score:number|null;
  match_score:number|null;
  priority_band:string;
  coverage_percent:number;
  factors:ApplicationPriorityFactor[];
  advisory:true;
  affects_match_score:false;
};

export type ApplicationPriorityState = 'loading'|'error'|'ready';

type Props = {
  profileId:string;
  jobId:string;
  state:ApplicationPriorityState;
  explanation:ApplicationPriorityExplanation|null;
  onRetry:()=>void;
};

function validScore(value:number|null):value is number {
  return typeof value==='number'&&Number.isFinite(value)&&value>=0&&value<=100;
}

function validResponse(value:ApplicationPriorityExplanation):boolean {
  return value.advisory===true&&value.affects_match_score===false
    &&(value.priority_score===null||validScore(value.priority_score))
    &&(value.match_score===null||validScore(value.match_score))
    &&Number.isFinite(value.coverage_percent)&&value.coverage_percent>=0&&value.coverage_percent<=100
    &&Array.isArray(value.factors)
    &&value.factors.every(factor=>typeof factor.key==='string'&&typeof factor.label==='string'
      &&(factor.status==='KNOWN'||factor.status==='UNKNOWN')
      &&typeof factor.explanation==='string'&&typeof factor.source==='string');
}

function shownValue(value:number|null):string {
  return validScore(value)?`${value}/100`:'UNKNOWN';
}

export function ApplicationPriority({profileId,jobId,state,explanation,onRetry}:Props) {
  const scopeMatches=!!explanation&&explanation.profile_id===profileId&&explanation.job_id===jobId;
  const responseValid=!!explanation&&validResponse(explanation);
  const safeExplanation=scopeMatches&&responseValid?explanation:null;

  return <section className="panel" aria-label="Application priority explanation">
    <div className="toolbar"><div><h2>Application priority</h2><p>Advisory ordering context for this job.</p></div></div>
    <p className="muted"><strong>Application priority is advisory only.</strong> Priority is separate from match score and does not change match score or job eligibility. It never rejects or hides a job.</p>
    {state==='loading'?<p role="status" aria-live="polite">Loading application priority…</p>
      :state==='error'?<div role="alert"><p>Application priority could not be loaded.</p><button type="button" onClick={onRetry}>Retry</button></div>
        :explanation&&!scopeMatches?<p role="alert">This priority explanation does not match the selected profile and job. Reload it before viewing.</p>
          :explanation&&!responseValid?<p role="alert">This priority explanation could not be verified as advisory. No score is shown.</p>
            :!safeExplanation?<p>No application priority explanation is available for this job.</p>
              :<>
                <dl>
                  <dt>Application priority</dt><dd>{safeExplanation.priority_score===null?'UNKNOWN':`${safeExplanation.priority_score}/100`}</dd>
                  <dt>Priority band</dt><dd>{safeExplanation.priority_band||'UNKNOWN'}</dd>
                  <dt>Match score</dt><dd>{safeExplanation.match_score===null?'UNKNOWN':`${safeExplanation.match_score}/100`}</dd>
                  <dt>Factor coverage</dt><dd>{safeExplanation.coverage_percent}%</dd>
                </dl>
                {safeExplanation.factors.length===0?<p>No priority factors were returned.</p>:<ul aria-label="Priority factors">
                  {safeExplanation.factors.map(factor=>{
                    const score=factor.status==='KNOWN'&&validScore(factor.score)?factor.score:null;
                    const contribution=factor.status==='KNOWN'&&validScore(factor.contribution)?factor.contribution:null;
                    const known=score!==null&&contribution!==null;
                    return <li className="fact" key={factor.key}>
                      <h3>{factor.label}</h3>
                      <p><strong>Status:</strong> {known?'KNOWN':'UNKNOWN'}</p>
                      <p><strong>Factor score:</strong> {known?shownValue(score):'UNKNOWN'}</p>
                      <p><strong>Priority contribution:</strong> {known?`${contribution>0?'+':''}${contribution}`:'UNKNOWN'}</p>
                      <p>{factor.explanation||'No explanation was provided.'}</p>
                      <p><strong>Source:</strong> {factor.source||'UNKNOWN'}</p>
                    </li>;
                  })}
                </ul>}
              </>}
  </section>;
}
