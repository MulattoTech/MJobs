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
  state?:string;
  trace?:string[];
  question_preflight?:{
    result:'PASS'|'FAIL'|'UNKNOWN';
    reason:string|null;
    is_current?:boolean|null;
    missing:MissingQuestion[];
  }|null;
};

type StateBlocker = {label:string;nextStep:string};
type ReviewQueueItem = {
  application:ReviewQueueApplication;
  stateBlocker:StateBlocker|null;
  preflight:ReviewQueueApplication['question_preflight'];
  preflightUnavailable:boolean;
};

function currentStateBlocker(application:ReviewQueueApplication):StateBlocker|null {
  if(application.state==='AUTH_REQUIRED')return {
    label:'Login required',
    nextStep:'Sign in yourself through the provider’s normal login flow. Then reopen the application review and check its saved status.',
  };
  if(application.state==='USER_INTERVENTION_REQUIRED') {
    // Only the latest marker describes this challenge; never display raw trace details.
    return application.trace?.at(-1)==='CAPTCHA_REQUIRED'?{
      label:'CAPTCHA needs your attention',
      nextStep:'Complete the CAPTCHA yourself on the provider’s page. Then reopen the application review and check its saved status.',
    }:{
      label:'Application needs your attention',
      nextStep:'Inspect the current challenge in the application review and complete any required manual steps on the provider’s site. Check the saved status before choosing a next action.',
    };
  }
  if(application.state==='SUBMISSION_UNCONFIRMED')return {
    label:'Submission result is unconfirmed',
    nextStep:'Check the provider’s receipt or application history before another attempt. Open the application review to inspect confirmation evidence; do not retry while the result is uncertain.',
  };
  return null;
}

// Both the list and sidebar count applications, even when one has several blockers.
export function selectApplicationReviewItems(applications:ReviewQueueApplication[],profileId:string):ReviewQueueItem[] {
  if(!profileId)return [];
  return applications.flatMap(application=>{
    if(application.profile_id!==profileId)return [];
    // A version-current preflight can still predate confirmation, cancellation
    // or an external report. Those persisted states take precedence over it.
    if(['CONFIRMED','CANCELLED','EXTERNALLY_APPLIED'].includes(application.state||''))return [];
    const stateBlocker=currentStateBlocker(application);
    const saved=application.question_preflight;
    const preflight=saved?.is_current!==false&&(saved?.result==='UNKNOWN'||saved?.result==='FAIL')?saved:null;
    // A missing preparation check is unavailable evidence, not a persisted failure.
    // Finished, cancelled and externally reported applications do not need a new check.
    const preflightUnavailable=!saved&&['PREPARED','REVIEW_REQUIRED','DRY_RUN_COMPLETE'].includes(application.state||'');
    return stateBlocker||preflight||preflightUnavailable?[{application,stateBlocker,preflight,preflightUnavailable}]:[];
  });
}

function unresolvedDetails(question:MissingQuestion):string {
  const missing=Object.entries({facts:question.fact_ids,answers:question.answer_ids,profile:question.profile_paths,documents:question.document_kinds,application:question.application_context})
    .filter(([,values])=>values?.length)
    .map(([kind,values])=>`${kind}: ${values!.join(', ')}`)
    .join('; ');
  return missing?`Persisted preflight is missing ${missing}.`:'The persisted preflight did not identify a resolvable mapping.';
}

export function ApplicationReviewQueue({applications,profileId,onReview,loading=false,loadError=false}:{applications:ReviewQueueApplication[];profileId:string;onReview:(applicationId:string)=>void;loading?:boolean;loadError?:boolean}) {
  const pending=selectApplicationReviewItems(applications,profileId);
  const emptyMessage=loading?null:loadError?<p>No successfully read applications currently need review; the queue is incomplete, and unread applications may still need review.</p>:<p>No saved applications currently need review.</p>;
  return <section className="panel" aria-label="Application review queue">
    <div className="toolbar"><div><h2>Applications needing review</h2><p>Saved login or intervention needs, unconfirmed submission results, and question preflight issues. Select an item to inspect its existing application review.</p></div><span className="tag">{pending.length} known blocker{pending.length===1?'':'s'}</span></div>
    <p className="muted">This queue only opens saved application details. It does not answer questions, change application state, start browser automation, or submit anything. Sensitive questions, low-confidence fields, unsupported ATS, and document validation are not yet listed as separate review categories.</p>
    {!profileId?<p>Select a profile to view its applications needing review.</p>:<>
      {loading&&<p role="status">Loading saved application review details…</p>}
      {loadError&&<p role="alert">Some saved application details could not be read. Only successfully read blockers are shown and counted below; refresh the workspace and reopen this queue to check for the unread applications.</p>}
      {pending.length===0?emptyMessage:<ul className="review-queue-items">{pending.map(({application,stateBlocker,preflight,preflightUnavailable})=>{
      return <li key={application.id} className="fact">
        <h3>{application.company||'Company UNKNOWN'} — {application.title||'Job title UNKNOWN'}</h3>
        {stateBlocker&&<><p><strong>{stateBlocker.label}</strong></p><p><strong>What to do next:</strong> {stateBlocker.nextStep}</p></>}
        {preflight&&<><p><strong>Reason:</strong> {preflight.reason||'No reason was saved for this result.'}</p>
        <p><strong>Question preflight:</strong> {preflight.result}</p>
        {preflight.missing?.length?<ul aria-label={`Question details for ${application.title}`}>{preflight.missing.map(question=><li key={question.question_id}>
          <p><strong>Question:</strong> {question.question}</p>
          <p><strong>Question ID:</strong> {question.question_id}</p>
          <p><strong>What needs review:</strong> {unresolvedDetails(question)}</p>
        </li>)}</ul>:<p><strong>What needs review:</strong> Inspect the saved application’s question-preflight details; no unresolved question mapping was recorded.</p>}
        <p><strong>What to do next:</strong> Open the application review, inspect the unresolved questions and their evidence, and explicitly review any answer before saving it.</p></>}
        {preflightUnavailable&&<><p><strong>Question preflight unavailable</strong></p><p>No supported question preflight result is saved. Open the application review to inspect available question checks and manual review options before proceeding.</p></>}
        <button type="button" aria-label={`Review application: ${application.title} at ${application.company}`} onClick={()=>onReview(application.id)}>Review application</button>
      </li>;
      })}</ul>}
    </>}
    <p className="muted">External submission remains OFF.</p>
  </section>;
}
