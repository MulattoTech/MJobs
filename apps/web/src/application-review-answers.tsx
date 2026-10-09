import React, { useCallback, useEffect, useRef, useState } from 'react';

export type ApplicationAnswerReview = {
  profile_version:string;
  company?:string;
  job_title?:string;
  question:{question_id:string;text:string;reason:string;sensitive?:boolean;supported?:boolean;unsupported_reason?:string};
  answer_bank:ReviewedAnswer[];
};

export type ReviewedAnswer = {
  id:string;
  question:string;
  text:string;
  user_approved:boolean;
  verified:boolean;
  confidence:number;
  source?:string;
};

export type AnswerSaveReadback = {
  answer:ReviewedAnswer;
  profile_version:string;
  external_submission:'OFF';
};

export type ApplicationAnswerReviewClient = {
  load:(profileId:string,applicationId:string,questionId:string)=>Promise<ApplicationAnswerReview>;
  saveToAnswerBank:(profileId:string,applicationId:string,input:{question_id:string;question:string;text:string;save_to_answer_bank:true;reviewed:true;expected_profile_version:string},operationId:string)=>Promise<AnswerSaveReadback>;
};

function normalize(value:string):string {
  return value.trim().replace(/\s+/g,' ');
}

function errorCode(error:unknown):string {
  if(error&&typeof error==='object'&&'code' in error)return String((error as {code:unknown}).code).toUpperCase();
  if(error&&typeof error==='object'&&'status' in error)return String((error as {status:unknown}).status);
  return '';
}

function errorMessage(error:unknown):string {
  if(error instanceof Error)return error.message;
  return 'The answer could not be saved. Your text is still here; retry when ready.';
}

export function hasExactReviewedAnswerReadback(review:ApplicationAnswerReview,readback:AnswerSaveReadback,text:string):boolean {
  const answer=readback.answer;
  return readback.external_submission==='OFF'
    &&typeof readback.profile_version==='string'&&readback.profile_version.length>0
    &&answer.user_approved===true&&!!answer.id
    &&normalize(answer.question)===normalize(review.question.text)
    &&normalize(answer.text)===normalize(text);
}

export async function saveAndReadBackAnswer(
  client:ApplicationAnswerReviewClient,
  profileId:string,
  applicationId:string,
  review:ApplicationAnswerReview,
  text:string,
  operationId:string,
  reviewed=true,
):Promise<ReviewedAnswer> {
  if(!normalize(text))throw new Error('Enter an answer before saving it to the Answer Bank.');
  if(!reviewed)throw new Error('Review this answer before saving it to the Answer Bank.');
  if(review.question.sensitive)throw new Error('This sensitive question needs a separate review and cannot be saved to the Answer Bank here.');
  if(review.question.supported!==true)throw new Error('This question type is unsupported by this review path.');
  const current=await client.load(profileId,applicationId,review.question.question_id);
  if(current.question.question_id!==review.question.question_id)
    throw Object.assign(new Error('The selected required question changed while this answer was being reviewed.'),{code:'STALE_VERSION'});
  const existing=current.answer_bank.find(answer=>!!answer.id&&answer.user_approved===true
    &&normalize(answer.question)===normalize(review.question.text)&&normalize(answer.text)===normalize(text));
  if(existing)return existing;
  if(current.profile_version!==review.profile_version)
    throw Object.assign(new Error('The profile changed while this answer was being reviewed.'),{code:'STALE_VERSION'});
  const result=await client.saveToAnswerBank(profileId,applicationId,{
    question_id:review.question.question_id,question:review.question.text,text:normalize(text),
    save_to_answer_bank:true,reviewed:true,expected_profile_version:review.profile_version,
  },operationId);
  if(!hasExactReviewedAnswerReadback(review,result,text))
    throw new Error('The save completed without a matching Answer Bank readback. Retry to check the saved version.');
  const persisted=await client.load(profileId,applicationId,review.question.question_id);
  const exact=persisted.answer_bank.find(answer=>!!answer.id&&answer.id===result.answer.id&&answer.user_approved===true
    &&normalize(answer.question)===normalize(review.question.text)&&normalize(answer.text)===normalize(text));
  if(persisted.profile_version!==result.profile_version||!exact)
    throw new Error('The saved answer was not confirmed by the durable Answer Bank readback. Retry to check the saved version.');
  return exact;
}

export function ApplicationReviewAnswersView({
  review,draftText,reviewed,busy,savedAnswer,error,onDraftChange,onReviewedChange,onSave,onContinue,onRetry,
}: {
  review:ApplicationAnswerReview;
  draftText:string;
  reviewed:boolean;
  busy:boolean;
  savedAnswer:ReviewedAnswer|null;
  error:string;
  onDraftChange:(value:string)=>void;
  onReviewedChange:(value:boolean)=>void;
  onSave:()=>void;
  onContinue:()=>void;
  onRetry:()=>void;
}) {
  const canContinue=!!savedAnswer?.id&&savedAnswer.user_approved&&normalize(savedAnswer.question)===normalize(review.question.text)
    &&normalize(savedAnswer.text)===normalize(draftText)&&review.question.supported===true&&!review.question.sensitive;
  return <section className="panel" aria-label="Application review answer editor">
    <h2>{review.company||'Selected company'} — {review.job_title||'Selected job'}</h2>
    <p><strong>Reason:</strong> {review.question.reason}</p>
    <p><strong>Required question:</strong> {review.question.text}</p>
    {review.question.sensitive&&<p role="alert">This is a sensitive question. It needs a separate review; Save to Answer Bank and Continue are unavailable here.</p>}
    {review.question.supported!==true&&<p role="alert">{review.question.supported===false?'This is an unsupported question type.':'Support for this question type could not be confirmed.'} Save to Answer Bank and Continue are unavailable here. {review.question.unsupported_reason||'The response control could not be safely reviewed.'}</p>}
    <label htmlFor="application-review-user-answer">Your answer</label>
    <textarea id="application-review-user-answer" aria-label="Your answer" maxLength={12000} value={draftText} disabled={busy}
      onChange={event=>onDraftChange(event.target.value)} />
    <p className="muted">Write or edit your own response. No answer is generated or sent to an employer.</p>
    <label className="consent"><input type="checkbox" aria-label="I reviewed this answer and confirm it is my own wording" checked={reviewed} disabled={busy||!normalize(draftText)}
      onChange={event=>onReviewedChange(event.target.checked)} /> I reviewed this answer and confirm it is my own wording.</label>
    <p><strong>Answer provenance:</strong> User-authored; reviewed by you for this question.</p>
    {error&&<p role="alert">{error}</p>}
    {savedAnswer&&canContinue&&<p role="status">Saved answer read back from the Answer Bank. Continue is available when you choose it.</p>}
    <div className="toolbar">
      <button type="button" className="primary" disabled={busy||review.question.sensitive===true||review.question.supported!==true||!normalize(draftText)||!reviewed}
        onClick={onSave}>{busy?'Saving and checking readback…':error?'Retry save and readback':'Save to Answer Bank'}</button>
      <button type="button" disabled={busy||review.question.sensitive===true||review.question.supported!==true||!canContinue} onClick={onContinue}>Continue</button>
    </div>
    <p className="muted">Continue only advances this local review after the saved answer is read back. It does not fill an employer form or submit an application.</p>
    {error&&<button type="button" disabled={busy} onClick={onRetry}>Reload review state</button>}
  </section>;
}

export function ApplicationReviewAnswers({profileId,applicationId,questionId,client,onContinue}: {
  profileId:string;
  applicationId:string;
  questionId:string;
  client:ApplicationAnswerReviewClient;
  onContinue:(answer:ReviewedAnswer)=>void;
}) {
  const [review,setReview]=useState<ApplicationAnswerReview|null>(null);
  const [draftText,setDraftText]=useState('');
  const [reviewed,setReviewed]=useState(false);
  const [savedAnswer,setSavedAnswer]=useState<ReviewedAnswer|null>(null);
  const [loading,setLoading]=useState(true);
  const [busy,setBusy]=useState(false);
  const [loadError,setLoadError]=useState('');
  const [saveError,setSaveError]=useState('');
  const sequence=useRef(0);
  const operationId=useRef('');
  const clientRef=useRef(client);
  clientRef.current=client;

  const loadReview=useCallback(async(preserveDraft=false)=>{
    const current=++sequence.current;
    setLoading(true);setBusy(false);setLoadError('');setSaveError('');setReview(null);setSavedAnswer(null);if(!preserveDraft){setDraftText('');operationId.current='';}setReviewed(false);
    try{
      const result=await clientRef.current.load(profileId,applicationId,questionId);
      if(current!==sequence.current)return;
      if(result.question.question_id!==questionId||!result.profile_version||!Array.isArray(result.answer_bank))
        throw new Error('The saved review does not match the selected profile, application, question, or version. Reload before continuing.');
      setReview(result);
    }catch(error){if(current===sequence.current)setLoadError(errorMessage(error));}
    finally{if(current===sequence.current)setLoading(false);}
  },[applicationId,profileId,questionId]);

  useEffect(()=>{void loadReview();return()=>{sequence.current+=1;};},[loadReview]);

  function updateDraft(value:string) {
    setDraftText(value);setReviewed(false);setSavedAnswer(null);setSaveError('');operationId.current='';
  }

  async function save() {
    if(!review||!normalize(draftText)||!reviewed||busy)return;
    const saveSequence=sequence.current;
    if(!operationId.current)operationId.current=globalThis.crypto?.randomUUID?.()||`answer-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setBusy(true);setSaveError('');setSavedAnswer(null);
    try{
      const result=await saveAndReadBackAnswer(clientRef.current,profileId,applicationId,review,draftText,operationId.current,reviewed);
      if(saveSequence===sequence.current)setSavedAnswer(result);
    }catch(error){
      if(saveSequence!==sequence.current)return;
      const code=errorCode(error);
      setSaveError(code==='409'||code==='STALE_VERSION'||/stale|version conflict/i.test(errorMessage(error))
        ?'The profile changed before this answer was saved. Your text is preserved; reload the latest version, review it again, and retry.'
        :errorMessage(error));
    }finally{if(saveSequence===sequence.current)setBusy(false);}
  }

  if(loading)return <section className="panel" aria-label="Application review answer editor"><p role="status">Loading the selected required question…</p></section>;
  if(loadError||!review)return <section className="panel" aria-label="Application review answer editor"><h2>Application question review</h2><p role="alert">{loadError||'The selected question is unavailable.'}</p><button type="button" onClick={()=>void loadReview()}>Retry loading question</button></section>;
  return <ApplicationReviewAnswersView review={review} draftText={draftText} reviewed={reviewed} busy={busy} savedAnswer={savedAnswer}
    error={saveError} onDraftChange={updateDraft} onReviewedChange={setReviewed} onSave={()=>void save()}
    onContinue={()=>{if(!savedAnswer?.id||review.question.sensitive||!savedAnswer.user_approved||normalize(savedAnswer.question)!==normalize(review.question.text)||normalize(savedAnswer.text)!==normalize(draftText))return;
      onContinue(savedAnswer);}}
    onRetry={()=>void loadReview(true)} />;
}
