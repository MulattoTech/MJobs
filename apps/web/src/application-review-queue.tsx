import React from 'react';

type MissingQuestion = {
  question_id:string;
  question:string;
  fact_ids?:string[];
  answer_ids?:string[];
  profile_paths?:string[];
  document_kinds?:string[];
  application_context?:string[];
};

export type ReviewQueueApplication = {
  id:string;
  profile_id:string;
  title:string;
  company:string;
  question_preflight?:{
    result:'PASS'|'FAIL'|'UNKNOWN';
    reason:string|null;
    missing:MissingQuestion[];
  }|null;
};

function unresolvedDetails(question:MissingQuestion):string {
  const missing=Object.entries({facts:question.fact_ids,answers:question.answer_ids,profile:question.profile_paths,documents:question.document_kinds,application:question.application_context})
    .filter(([,values])=>values?.length)
    .map(([kind,values])=>`${kind}: ${values!.join(', ')}`)
    .join('; ');
  return missing?`Persisted preflight is missing ${missing}.`:'The persisted preflight did not identify a resolvable mapping.';
}

export function ApplicationReviewQueue({applications,profileId,onReview,loading=false,loadError=false}:{applications:ReviewQueueApplication[];profileId:string;onReview:(applicationId:string)=>void;loading?:boolean;loadError?:boolean}) {
  const pending=profileId?applications.filter(application=>application.profile_id===profileId&&(application.question_preflight?.result==='UNKNOWN'||application.question_preflight?.result==='FAIL')):[];
  return <section className="panel" aria-label="Application review queue">
    <div className="toolbar"><div><h2>Applications needing question review</h2><p>Persisted question preflight is UNKNOWN or FAIL. Select an item to inspect its existing application review.</p></div><span className="tag">{pending.length} {loadError?'known':'pending'}</span></div>
    <p className="muted">This queue only opens saved application details. It does not answer questions, change application state, start browser automation, or submit anything. CAPTCHA, login, sensitive questions, unsupported ATS, document validation, and ambiguous submissions are not yet listed here.</p>
    {!profileId?<p>Select a profile to view its applications needing review.</p>:loading?<p role="status">Loading saved application preflight details…</p>:<>
      {loadError&&<p role="alert">Some saved application details could not be read. Only successfully read blockers are shown and counted below; refresh the workspace and reopen this queue to check for the unread applications.</p>}
      {pending.length===0?<p>No saved applications currently need question review.</p>:<ul className="review-queue-items">{pending.map(application=>{
      const preflight=application.question_preflight;
      if(!preflight||preflight.result==='PASS')return null;
      const result=preflight.result;
      const reason=preflight.reason||'No reason was saved for this result.';
      return <li key={application.id} className="fact">
        <h3>{application.company||'Company UNKNOWN'} — {application.title||'Job title UNKNOWN'}</h3>
        <p><strong>Reason:</strong> {reason}</p>
        <p><strong>Question preflight:</strong> {result}</p>
        {preflight?.missing?.length?<ul aria-label={`Question details for ${application.title}`}>{preflight.missing.map(question=><li key={question.question_id}>
          <p><strong>Question:</strong> {question.question}</p>
          <p><strong>Question ID:</strong> {question.question_id}</p>
          <p><strong>What needs review:</strong> {unresolvedDetails(question)}</p>
        </li>)}</ul>:<p><strong>What needs review:</strong> Inspect the saved application’s question-preflight details; no unresolved question mapping was recorded.</p>}
        <button type="button" aria-label={`Review application: ${application.title} at ${application.company}`} onClick={()=>onReview(application.id)}>Review application</button>
      </li>;
      })}</ul>}
    </>}
    <p className="muted">External submission remains OFF.</p>
  </section>;
}
