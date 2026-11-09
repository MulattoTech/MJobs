import { useEffect, useRef, useState, type ReactNode } from 'react';
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

export function followUpDraftsPath(profileId:string,quietDays:number) {
  return `/profiles/${encodeURIComponent(profileId)}/application-follow-up-drafts?quiet_days=${quietDays}`;
}

function reportedAt(value:string) {
  const parsed=new Date(value);
  return Number.isNaN(parsed.getTime())?value:parsed.toLocaleString();
}

type FollowUpDraftsViewProps = {
  quietDays:number;
  candidates:FollowUpDraftCandidate[];
  loading:boolean;
  error:string|null;
  copied:string|null;
  onQuietDaysChange:(days:number)=>void;
  onCopy:(kind:CopyKind,candidate:FollowUpDraftCandidate)=>void;
};

function FollowUpDraftsShell({quietDays,onQuietDaysChange,children}:{
  quietDays:number;
  onQuietDaysChange:(days:number)=>void;
  children:ReactNode;
}) {
  return <section className="panel" aria-label="Follow-up drafts">
    <h3>Follow-up drafts</h3>
    <p>Drafts are based on your reported application history. They do not confirm employer receipt and are never sent by MJobs.</p>
    <label htmlFor="follow-up-quiet-days">Quiet days before follow-up</label>
    <select id="follow-up-quiet-days" aria-label="Quiet days before follow-up" value={quietDays}
      onChange={event=>onQuietDaysChange(Number(event.currentTarget.value))}>
      {Array.from({length:90},(_,index)=>index+1).map(days=><option key={days} value={days}>{days} {days===1?'day':'days'}</option>)}
    </select>
    {children}
  </section>;
}

function FollowUpDraftResults({quietDays,candidates,loading,error,copied,onCopy}:
  Omit<FollowUpDraftsViewProps,'onQuietDaysChange'>) {
  const count=(value:string,limit:number)=>`${Array.from(value).length} / ${limit} characters`;
  const eligibleCandidates=candidates.filter(candidate=>candidate.verified===false&&
    (candidate.attribution==='USER_REPORTED'||candidate.attribution==='EXTERNAL_USER_REPORTED'));
  return <>
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
  </>;
}

export function FollowUpDraftsView({quietDays,onQuietDaysChange,...results}:FollowUpDraftsViewProps) {
  return <FollowUpDraftsShell quietDays={quietDays} onQuietDaysChange={onQuietDaysChange}>
    <FollowUpDraftResults quietDays={quietDays} {...results}/>
  </FollowUpDraftsShell>;
}

export function ApplicationFollowUpDrafts({profileId}:{profileId:string}) {
  const [quietDays,setQuietDays]=useState(7);
  // A new query context gets fresh state before its first commit. Old requests
  // and clipboard promises retain only the discarded instance's state setters.
  // Keep the selector outside that boundary so changing it preserves focus.
  return <FollowUpDraftsShell quietDays={quietDays} onQuietDaysChange={setQuietDays}>
    <FollowUpDraftContext key={JSON.stringify([profileId,quietDays])} profileId={profileId} quietDays={quietDays}/>
  </FollowUpDraftsShell>;
}

function FollowUpDraftContext({profileId,quietDays}:{
  profileId:string;
  quietDays:number;
}) {
  const [candidates,setCandidates]=useState<FollowUpDraftCandidate[]>([]);
  const [loading,setLoading]=useState(true);
  const [error,setError]=useState<string|null>(null);
  const [copied,setCopied]=useState<string|null>(null);
  const [copyError,setCopyError]=useState<string|null>(null);
  const copySequence=useRef(0);

  useEffect(()=>{
    let active=true;
    const dispose=()=>{active=false;copySequence.current++;};
    if(!profileId){setLoading(false);return dispose;}
    void api<FollowUpDraftResponse>(followUpDraftsPath(profileId,quietDays)).then(response=>{
      if(active)setCandidates(response.candidates);
    }).catch(reason=>{
      if(active){setCandidates([]);setError(reason instanceof Error?reason.message:'The local API returned an error.');}
    }).finally(()=>{if(active)setLoading(false);});
    return dispose;
  },[profileId,quietDays]);

  async function copy(kind:CopyKind,candidate:FollowUpDraftCandidate) {
    const text=kind==='subject'?candidate.email_subject:kind==='email'?candidate.email_body:candidate.dm_body;
    if(text===null)return;
    const sequence=++copySequence.current;
    setCopied(null);setCopyError(null);
    try {
      await navigator.clipboard.writeText(text);
      if(sequence!==copySequence.current)return;
      setCopied(`${candidate.application_id}:${kind}`);setCopyError(null);
    } catch {
      if(sequence!==copySequence.current)return;
      setCopyError('Clipboard access is unavailable. Select and copy the draft text manually.');
    }
  }

  return <>
    <FollowUpDraftResults quietDays={quietDays} candidates={candidates} loading={loading} error={error} copied={copied}
      onCopy={(kind,candidate)=>void copy(kind,candidate)}/>
    {copyError&&<p role="alert">{copyError}</p>}
  </>;
}
