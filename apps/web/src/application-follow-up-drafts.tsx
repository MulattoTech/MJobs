import { useEffect, useRef, useState } from 'react';
import { api } from './api';

export type FollowUpDraftCandidate = {
  application_id:string;
  job_id:string;
  job_title:string;
  company:string;
  days_quiet:number;
  reported_at:string;
  attribution:'USER_REPORTED'|'EXTERNAL_USER_REPORTED';
  verified:boolean;
  email_subject:string;
  email_body:string;
  dm_body:string|null;
  character_limits:{subject:number;email_body:number;dm_body:number};
};

type FollowUpDraftResponse = {quiet_days:number;candidates:FollowUpDraftCandidate[]};
type CopyKind = 'subject'|'email'|'dm';
type ClipboardError = {context:string;message:string};

export function followUpDraftsPath(profileId:string,quietDays:number) {
  return `/profiles/${encodeURIComponent(profileId)}/application-follow-up-drafts?quiet_days=${quietDays}`;
}

export function clipboardErrorAfterContextChange(error:ClipboardError|null,previousContext:string,nextContext:string) {
  return previousContext===nextContext?error:null;
}

function reportedAt(value:string) {
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString();
}

export function FollowUpDraftsView({
  quietDays,candidates,loading,error,copied,onQuietDaysChange,onCopy,
}: {
  quietDays:number;
  candidates:FollowUpDraftCandidate[];
  loading:boolean;
  error:string|null;
  copied:string|null;
  onQuietDaysChange:(days:number)=>void;
  onCopy:(kind:CopyKind,candidate:FollowUpDraftCandidate)=>void;
}) {
  const count=(value:string,limit:number)=>`${Array.from(value).length} / ${limit} characters`;
  const eligibleCandidates=candidates.filter(candidate=>candidate.verified===false&&
    (candidate.attribution==='USER_REPORTED'||candidate.attribution==='EXTERNAL_USER_REPORTED'));
  return <section className="panel" aria-label="Follow-up drafts">
    <h3>Follow-up drafts</h3>
    <p>Drafts are based on your reported application history. They do not confirm employer receipt and are never sent by MJobs.</p>
    <label htmlFor="follow-up-quiet-days">Quiet days before follow-up</label>
    <select id="follow-up-quiet-days" aria-label="Quiet days before follow-up" value={quietDays}
      onChange={event=>onQuietDaysChange(Number(event.currentTarget.value))}>
      {Array.from({length:90},(_,index)=>index+1).map(days=><option key={days} value={days}>{days} {days===1?'day':'days'}</option>)}
    </select>
    {loading?<p role="status">Loading follow-up drafts…</p>:error?<p role="alert"><strong>Could not load follow-up drafts.</strong> {error}</p>:eligibleCandidates.length===0?
      <p className="empty">No reported applications have been quiet for {quietDays} days or more.</p>:
      <ul aria-label="Reported follow-up draft candidates">{eligibleCandidates.map(candidate=><li key={candidate.application_id}>
        <article aria-label={`${candidate.company} — ${candidate.job_title}`}>
          <h4>{candidate.job_title} · {candidate.company}</h4>
          <p><strong>{candidate.days_quiet} days quiet</strong> · reported {reportedAt(candidate.reported_at)} · {candidate.attribution==='EXTERNAL_USER_REPORTED'?'Externally reported':'User reported'}</p>
          <p className="safety">Reported by you · not verified. MJobs has no evidence that the employer received your application.</p>
          <dl>
            <dt>Email subject</dt><dd>{candidate.email_subject}</dd>
            <dd className="muted">{count(candidate.email_subject,candidate.character_limits.subject)} (limit {candidate.character_limits.subject})</dd>
            <dt>Email body</dt><dd>{candidate.email_body}</dd>
            <dd className="muted">{count(candidate.email_body,candidate.character_limits.email_body)} (limit {candidate.character_limits.email_body})</dd>
            {candidate.dm_body&&<><dt>DM wording</dt><dd>{candidate.dm_body}</dd><dd className="muted">{count(candidate.dm_body,candidate.character_limits.dm_body)} (limit {candidate.character_limits.dm_body})</dd></>}
          </dl>
          <div className="toolbar" aria-label="Copy draft text">
            <button type="button" onClick={()=>onCopy('subject',candidate)}>{copied===`${candidate.application_id}:subject`?'Copied email subject':'Copy email subject'}</button>
            <button type="button" onClick={()=>onCopy('email',candidate)}>{copied===`${candidate.application_id}:email`?'Copied email body':'Copy email body'}</button>
            {candidate.dm_body&&<button type="button" onClick={()=>onCopy('dm',candidate)}>{copied===`${candidate.application_id}:dm`?'Copied DM wording':'Copy DM wording'}</button>}
          </div>
        </article>
      </li>)}</ul>}
  </section>;
}

export function ApplicationFollowUpDrafts({profileId}:{profileId:string}) {
  const [quietDays,setQuietDays]=useState(7);
  const [candidates,setCandidates]=useState<FollowUpDraftCandidate[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState<string|null>(null);
  const [copied,setCopied]=useState<string|null>(null);
  const [copyError,setCopyError]=useState<ClipboardError|null>(null);
  const draftContext=JSON.stringify([profileId,quietDays]);
  const previousDraftContext=useRef(draftContext);
  const requestSequence=useRef(0);

  useEffect(()=>{
    const previousContext=previousDraftContext.current;
    previousDraftContext.current=draftContext;
    setCopyError(error=>clipboardErrorAfterContextChange(error,previousContext,draftContext));
    const sequence=++requestSequence.current;
    let active=true;
    if(!profileId){setCandidates([]);setLoading(false);setError(null);return ()=>{active=false;};}
    setLoading(true);setError(null);setCopied(null);
    void api<FollowUpDraftResponse>(followUpDraftsPath(profileId,quietDays)).then(response=>{
      if(active&&sequence===requestSequence.current)setCandidates(response.candidates);
    }).catch(reason=>{
      if(active&&sequence===requestSequence.current){setCandidates([]);setError(reason instanceof Error?reason.message:'The local API returned an error.');}
    }).finally(()=>{if(active&&sequence===requestSequence.current)setLoading(false);});
    return ()=>{active=false;};
  },[profileId,quietDays,draftContext]);

  async function copy(kind:CopyKind,candidate:FollowUpDraftCandidate) {
    const text=kind==='subject'?candidate.email_subject:kind==='email'?candidate.email_body:candidate.dm_body;
    if(text===null)return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(`${candidate.application_id}:${kind}`);setCopyError(null);
    } catch {
      setCopyError({context:draftContext,message:'Clipboard access is unavailable. Select and copy the draft text manually.'});
    }
  }

  return <>
    <FollowUpDraftsView quietDays={quietDays} candidates={candidates} loading={loading} error={error} copied={copied}
      onQuietDaysChange={setQuietDays} onCopy={(kind,candidate)=>void copy(kind,candidate)}/>
    {copyError?.context===draftContext&&<p role="alert">{copyError.message}</p>}
  </>;
}
