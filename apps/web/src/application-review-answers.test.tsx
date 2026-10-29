import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationReviewAnswersView,
  ApplicationReviewAnswers,
  continueAfterFreshValidation,
  createApplicationAnswerResumeClient,
  hasExactReviewedAnswerReadback,
  hasBoundReviewScope,
  resumeAndRevalidateAnswer,
  saveAndReadBackAnswer,
  type ApplicationAnswerReview,
  type ApplicationAnswerReviewClient,
  type AnswerSaveReadback,
} from './application-review-answers';

const review:ApplicationAnswerReview = {
  profile_id:'profile-a',application_id:'application-a',profile_version:'profile-v1', form_fingerprint:'form-v1', answer_bank:[],
  company:'Acme', job_title:'Systems Administrator',
  question:{question_id:'q-azure', fingerprint:'question-v1', text:'Describe your experience managing Azure subscriptions.', reason:'No answer is saved for this required question.',supported:true},
};

const saved:AnswerSaveReadback = {
  profile_version:'profile-v2', external_submission:'OFF',
  answer:{id:'answer-1',question:review.question.text,text:'I managed test subscriptions.',user_approved:true,verified:false,confidence:0,source:'User-authored and reviewed'},
};

describe('application review answer editor',()=>{
  it('shows the exact unresolved question and truthful controls without inventing a suggested answer',()=>{
    const html=renderToStaticMarkup(<ApplicationReviewAnswersView
      review={review} draftText="" reviewed={false} busy={false} savedAnswer={null} error=""
      onDraftChange={vi.fn()} onReviewedChange={vi.fn()} onSave={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()}
    />);
    expect(html).toContain('Acme — Systems Administrator');
    expect(html).toContain('No answer is saved for this required question.');
    expect(html).toContain(review.question.text);
    expect(html).toContain('User-authored; reviewed by you');
    expect(html).toContain('Save to Answer Bank');
    expect(html).not.toContain('Suggested answer');
    expect(html).not.toMatch(/\bverified\b/i);
    expect(html).not.toMatch(/<button[^>]*>[^<]*submit[^<]*<\/button>/i);
    expect(html).toContain('does not fill an employer form or submit an application');
    expect(html).toMatch(/<button type="button" disabled="">Continue<\/button>/);
  });

  it('requires an exact reviewed answer readback for the same profile and question',()=>{
    expect(hasExactReviewedAnswerReadback(review,saved,'I managed test subscriptions.')).toBe(true);
    expect(hasExactReviewedAnswerReadback(review,{...saved,external_submission:'ON' as 'OFF'},'I managed test subscriptions.')).toBe(false);
    expect(hasExactReviewedAnswerReadback(review,{...saved,answer:{...saved.answer,user_approved:false}},'I managed test subscriptions.')).toBe(false);
    expect(hasExactReviewedAnswerReadback(review,{...saved,answer:{...saved.answer,text:'different'}},'I managed test subscriptions.')).toBe(false);
    expect(hasExactReviewedAnswerReadback(review,{...saved,answer:{...saved.answer,id:''}},'I managed test subscriptions.')).toBe(false);
  });

  it('requires nonempty form and question fingerprints before save or resume actions',()=>{
    expect(hasBoundReviewScope(review,'profile-a','application-a','q-azure')).toBe(true);
    expect(hasBoundReviewScope({...review,profile_id:'profile-b'},'profile-a','application-a','q-azure')).toBe(false);
    expect(hasBoundReviewScope({...review,application_id:'application-b'},'profile-a','application-a','q-azure')).toBe(false);
    expect(hasBoundReviewScope({...review,form_fingerprint:''},'profile-a','application-a','q-azure')).toBe(false);
    expect(hasBoundReviewScope({...review,question:{...review.question,fingerprint:''}},'profile-a','application-a','q-azure')).toBe(false);
    expect(hasBoundReviewScope({...review,question:{...review.question,question_id:'q-other'}},'profile-a','application-a','q-azure')).toBe(false);
  });

  it('fails before client I/O when the loaded review has no exact fingerprint binding',async()=>{
    const client:ApplicationAnswerReviewClient={load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    const unbound={...review,form_fingerprint:''};
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',unbound,
      'I managed test subscriptions.','unbound-save')).rejects.toThrow('review binding');
    await expect(resumeAndRevalidateAnswer(client,'profile-a','application-a',unbound,saved,'unbound-resume'))
      .rejects.toThrow('review binding');
    expect(client.load).not.toHaveBeenCalled();
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();
    expect(client.resumeAndValidate).not.toHaveBeenCalled();
  });

  it('rejects a loaded review from another profile or application before client I/O',async()=>{
    const client:ApplicationAnswerReviewClient={load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    for(const foreign of [{...review,profile_id:'profile-b'},{...review,application_id:'application-b'}]){
      await expect(saveAndReadBackAnswer(client,'profile-a','application-a',foreign,
        'I managed test subscriptions.','foreign-save')).rejects.toThrow('review binding');
      await expect(resumeAndRevalidateAnswer(client,'profile-a','application-a',foreign,saved,'foreign-resume'))
        .rejects.toThrow('review binding');
    }
    expect(client.load).not.toHaveBeenCalled();
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();
    expect(client.resumeAndValidate).not.toHaveBeenCalled();
  });

  it('saves with the loaded version and permits readback recovery without fabricating state',async()=>{
    const readbackReview={...review,profile_version:'profile-v2',answer_bank:[saved.answer]};
    const client:ApplicationAnswerReviewClient={
      load:vi.fn().mockResolvedValueOnce(review).mockResolvedValueOnce(readbackReview),
      saveToAnswerBank:vi.fn().mockResolvedValue(saved),
      resumeAndValidate:vi.fn(),
    };
    const result=await saveAndReadBackAnswer(client,'profile-a','application-a',review,'I managed test subscriptions.','stable-operation');
    expect(client.saveToAnswerBank).toHaveBeenCalledWith('profile-a','application-a',{
      question_id:'q-azure',question:review.question.text,text:'I managed test subscriptions.',
      save_to_answer_bank:true,reviewed:true,expected_profile_version:'profile-v1',
    },'stable-operation');
    expect(client.load).toHaveBeenCalledTimes(2);
    expect(result).toEqual(saved);
  });

  it('resumes only through a fresh exact-scope current PASS validation and answer readback',async()=>{
    const resumeResult={
      request_id:'resume-1',profile_id:'profile-a',application_id:'application-a',
      question_id:'q-azure',question_fingerprint:'question-v1',form_fingerprint:'form-v1',
      profile_version:'profile-v2',external_submission:'OFF' as const,answer:saved.answer,
      preflight:{result:'PASS' as const,is_current:true},
    };
    const client:ApplicationAnswerReviewClient={
      load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn().mockResolvedValue(resumeResult),
    };
    await expect(resumeAndRevalidateAnswer(
      client,'profile-a','application-a',review,saved,'resume-1',
    )).resolves.toEqual(saved.answer);
    expect(client.resumeAndValidate).toHaveBeenCalledWith('profile-a','application-a',{
      request_id:'resume-1',question_id:'q-azure',question_fingerprint:'question-v1',
      form_fingerprint:'form-v1',answer_id:'answer-1',expected_profile_version:'profile-v2',
    });
  });

  it.each([
    ['profile scope',{profile_id:'profile-b'}],
    ['application scope',{application_id:'application-b'}],
    ['question scope',{question_id:'q-other'}],
    ['question fingerprint',{question_fingerprint:'question-v2'}],
    ['form fingerprint',{form_fingerprint:'form-v2'}],
    ['profile version',{profile_version:'profile-v3'}],
    ['request identity',{request_id:'resume-other'}],
    ['external submission',{external_submission:'ON'}],
    ['answer identity',{answer:{...saved.answer,id:'answer-other'}}],
    ['answer question',{answer:{...saved.answer,question:'Different question'}}],
    ['answer text',{answer:{...saved.answer,text:'Different answer'}}],
    ['answer approval',{answer:{...saved.answer,user_approved:false}}],
    ['failed preflight',{preflight:{result:'FAIL',is_current:true}}],
    ['unknown preflight',{preflight:{result:'UNKNOWN',is_current:true}}],
    ['stale preflight',{preflight:{result:'PASS',is_current:false}}],
  ])('fails closed when resumed validation changes %s',async(_label,change)=>{
    const exact={
      request_id:'resume-1',profile_id:'profile-a',application_id:'application-a',
      question_id:'q-azure',question_fingerprint:'question-v1',form_fingerprint:'form-v1',
      profile_version:'profile-v2',external_submission:'OFF',answer:saved.answer,
      preflight:{result:'PASS',is_current:true},
    };
    const client={load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn().mockResolvedValue({...exact,...change})} as unknown as ApplicationAnswerReviewClient;
    await expect(resumeAndRevalidateAnswer(client,'profile-a','application-a',review,saved,'resume-1'))
      .rejects.toThrow('fresh application validation');
  });

  it('does not request resumed preflight without an explicitly approved exact saved answer',async()=>{
    const client={load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()} as unknown as ApplicationAnswerReviewClient;
    await expect(resumeAndRevalidateAnswer(client,'profile-a','application-a',review,{
      ...saved,answer:{...saved.answer,user_approved:false},
    },'resume-1')).rejects.toThrow('approved saved answer');
    expect(client.resumeAndValidate).not.toHaveBeenCalled();
  });

  it('calls the continuation only after fresh resumed preflight passes',async()=>{
    const client:ApplicationAnswerReviewClient={
      load:vi.fn(),saveToAnswerBank:vi.fn(),
      resumeAndValidate:vi.fn().mockResolvedValue({
        request_id:'resume-component',profile_id:'profile-a',application_id:'application-a',
        question_id:'q-azure',question_fingerprint:'question-v1',form_fingerprint:'form-v1',
        profile_version:'profile-v2',external_submission:'OFF',answer:saved.answer,
        preflight:{result:'PASS',is_current:true},
      }),
    };
    const continued=vi.fn();
    await continueAfterFreshValidation(client,'profile-a','application-a',review,saved,'resume-component',continued);
    expect(client.resumeAndValidate).toHaveBeenCalledTimes(1);
    expect(continued).toHaveBeenCalledWith(saved.answer);
  });

  it('does not call the continuation when fresh resumed preflight fails',async()=>{
    const client:ApplicationAnswerReviewClient={
      load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn().mockResolvedValue({
        request_id:'resume-failed',profile_id:'profile-a',application_id:'application-a',
        question_id:'q-azure',question_fingerprint:'question-v1',form_fingerprint:'form-v1',
        profile_version:'profile-v2',external_submission:'OFF',answer:saved.answer,
        preflight:{result:'FAIL',is_current:true},
      }),
    };
    const continued=vi.fn();
    await expect(continueAfterFreshValidation(client,'profile-a','application-a',review,saved,'resume-failed',continued))
      .rejects.toThrow('fresh application validation');
    expect(continued).not.toHaveBeenCalled();
  });

  it('uses the bound resume API and polls to an exact completed validation',async()=>{
    const queued={operation_id:'operation-1',state:'QUEUED',error_code:null,replayed:false,
      input_fingerprint:'a'.repeat(64),external_submission_enabled:false};
    const completed={
      request_id:'resume-http',profile_id:'profile/a',application_id:'application?b',
      question_id:'q-azure',question_fingerprint:'question-v1',form_fingerprint:'form-v1',
      profile_version:'profile-v2',external_submission:'OFF' as const,answer:saved.answer,
      preflight:{result:'PASS' as const,is_current:true},
    };
    const request=vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(queued),{status:202,headers:{'Content-Type':'application/json'}}))
      .mockResolvedValueOnce(new Response(JSON.stringify(queued),{status:202,headers:{'Content-Type':'application/json'}}))
      .mockResolvedValueOnce(new Response(JSON.stringify(completed),{status:200,headers:{'Content-Type':'application/json'}}));
    const wait=vi.fn().mockResolvedValue(undefined);
    const client=createApplicationAnswerResumeClient(request as typeof fetch,wait);
    await expect(client.resumeAndValidate('profile/a','application?b',{
      request_id:'resume-http',question_id:'q-azure',question_fingerprint:'question-v1',
      form_fingerprint:'form-v1',answer_id:'answer-1',expected_profile_version:'profile-v2',
    })).resolves.toEqual(completed);
    const base='/profiles/profile%2Fa/applications/application%3Fb/answer-review-resumes';
    expect(request).toHaveBeenNthCalledWith(1,base,expect.objectContaining({method:'POST'}));
    expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({
      request_id:'resume-http',question_id:'q-azure',question_fingerprint:'question-v1',
      form_fingerprint:'form-v1',answer_id:'answer-1',expected_profile_version:'profile-v2',
    });
    expect(request).toHaveBeenNthCalledWith(2,base+'/resume-http',{method:'GET'});
    expect(request).toHaveBeenNthCalledWith(3,base+'/resume-http',{method:'GET'});
    expect(wait).toHaveBeenCalledTimes(2);
  });

  it('fails closed when the bound resume API rejects or never completes',async()=>{
    const rejected=vi.fn().mockResolvedValue(new Response(
      JSON.stringify({detail:{code:'APPLICATION_RESUME_PREFLIGHT_STALE'}}),
      {status:409,headers:{'Content-Type':'application/json'}},
    ));
    const rejectedClient=createApplicationAnswerResumeClient(rejected as typeof fetch,vi.fn());
    await expect(rejectedClient.resumeAndValidate('profile-a','application-a',{
      request_id:'resume-rejected',question_id:'q-azure',question_fingerprint:'question-v1',
      form_fingerprint:'form-v1',answer_id:'answer-1',expected_profile_version:'profile-v2',
    })).rejects.toThrow('APPLICATION_RESUME_PREFLIGHT_STALE');

    const queued={operation_id:'operation-1',state:'QUEUED',error_code:null,replayed:false,
      input_fingerprint:'a'.repeat(64),external_submission_enabled:false};
    const pending=vi.fn().mockResolvedValue(new Response(JSON.stringify(queued),{
      status:202,headers:{'Content-Type':'application/json'},
    }));
    const pendingClient=createApplicationAnswerResumeClient(pending as typeof fetch,vi.fn(),2);
    await expect(pendingClient.resumeAndValidate('profile-a','application-a',{
      request_id:'resume-timeout',question_id:'q-azure',question_fingerprint:'question-v1',
      form_fingerprint:'form-v1',answer_id:'answer-1',expected_profile_version:'profile-v2',
    })).rejects.toThrow('did not complete');
  });

  it('rejects empty text, missing deliberate review, or a save without exact readback',async()=>{
    const readbackReview={...review,profile_version:'profile-v2',answer_bank:[]};
    const client:ApplicationAnswerReviewClient={
      load:vi.fn().mockResolvedValueOnce(review).mockResolvedValueOnce(readbackReview),
      saveToAnswerBank:vi.fn().mockResolvedValue(saved),
      resumeAndValidate:vi.fn(),
    };
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'  ','req-1')).rejects.toThrow('Enter an answer');
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'Some answer','req-1',false)).rejects.toThrow('Review this answer');
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'Some answer','req-1',true)).rejects.toThrow('matching Answer Bank readback');
    expect(client.saveToAnswerBank).toHaveBeenCalledTimes(1);
  });

  it('does not write again when retry discovers the exact answer already persisted',async()=>{
    const retriedReview={...review,profile_version:'profile-v2',answer_bank:[saved.answer]};
    const client:ApplicationAnswerReviewClient={load:vi.fn().mockResolvedValue(retriedReview),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',retriedReview,'I managed test subscriptions.','retry-stable')).resolves.toEqual({
      answer:saved.answer,profile_version:'profile-v2',external_submission:'OFF',
    });
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();
  });

  it.each([
    ['profile scope',{profile_id:'profile-b'}],
    ['application scope',{application_id:'application-b'}],
    ['form fingerprint',{form_fingerprint:'form-v2'}],
    ['question fingerprint',{question:{...review.question,fingerprint:'question-v2'}}],
    ['question text',{question:{...review.question,text:'Changed required question'}}],
  ])('rejects %s drift found by the pre-save review re-read',async(_label,change)=>{
    const changed={...review,profile_version:'profile-v2',answer_bank:[saved.answer],...change};
    const client:ApplicationAnswerReviewClient={load:vi.fn().mockResolvedValue(changed),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,
      'I managed test subscriptions.','stale-binding')).rejects.toMatchObject({code:'STALE_VERSION'});
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();
  });

  it('surfaces stale profile versions before writing and preserves the user draft in the recoverable view',async()=>{
    const changed={...review,profile_version:'profile-v2'};
    const client:ApplicationAnswerReviewClient={load:vi.fn().mockResolvedValue(changed),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'I managed test subscriptions.','req-stale'))
      .rejects.toMatchObject({code:'STALE_VERSION'});
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();

    const html=renderToStaticMarkup(<ApplicationReviewAnswersView review={review} draftText="I managed test subscriptions."
      reviewed={true} busy={false} savedAnswer={null} error="The profile changed before this answer was saved."
      onDraftChange={vi.fn()} onReviewedChange={vi.fn()} onSave={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()}/>);
    expect(html).toContain('I managed test subscriptions.');
    expect(html).toContain('The profile changed before this answer was saved.');
    expect(html).toContain('Reload review state');
    expect(html).toMatch(/<button type="button" disabled="">Continue<\/button>/);
  });

  it('shows a recoverable load state before calling the injected client',()=>{
    const client:ApplicationAnswerReviewClient={load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()};
    const html=renderToStaticMarkup(<ApplicationReviewAnswers profileId="profile-a" applicationId="application-a"
      questionId="q-azure" client={client} onContinue={vi.fn()}/>);
    expect(html).toContain('Loading the selected required question');
    expect(client.load).not.toHaveBeenCalled();
  });

  it('blocks sensitive questions from saving or continuing',()=>{
    const html=renderToStaticMarkup(<ApplicationReviewAnswersView review={{...review,question:{...review.question,sensitive:true}}}
      draftText="I managed test subscriptions." reviewed={true} busy={false} savedAnswer={saved.answer} error=""
      onDraftChange={vi.fn()} onReviewedChange={vi.fn()} onSave={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()}/>);
    expect(html).toContain('sensitive question');
    expect(html).toMatch(/<button type="button" class="primary" disabled="">Save to Answer Bank<\/button>/);
    expect(html).toMatch(/<button type="button" disabled="">Continue<\/button>/);
  });

  it('fails closed for unsupported question types',async()=>{
    const unsupported={...review,question:{...review.question,supported:false,unsupported_reason:'Unrecognized response control'}};
    const html=renderToStaticMarkup(<ApplicationReviewAnswersView review={unsupported}
      draftText="I managed test subscriptions." reviewed={true} busy={false} savedAnswer={saved.answer} error=""
      onDraftChange={vi.fn()} onReviewedChange={vi.fn()} onSave={vi.fn()} onContinue={vi.fn()} onRetry={vi.fn()}/>);
    expect(html).toContain('unsupported question type');
    expect(html).toContain('Unrecognized response control');
    expect(html).toMatch(/<button type="button" class="primary" disabled="">Save to Answer Bank<\/button>/);
    expect(html).toMatch(/<button type="button" disabled="">Continue<\/button>/);
    await expect(saveAndReadBackAnswer({load:vi.fn(),saveToAnswerBank:vi.fn(),resumeAndValidate:vi.fn()},'profile-a','application-a',unsupported,
      'I managed test subscriptions.','unsupported-retry'))
      .rejects.toThrow('unsupported by this review path');
  });
});
