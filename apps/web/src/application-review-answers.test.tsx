import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationReviewAnswersView,
  ApplicationReviewAnswers,
  hasExactReviewedAnswerReadback,
  saveAndReadBackAnswer,
  type ApplicationAnswerReview,
  type ApplicationAnswerReviewClient,
  type AnswerSaveReadback,
} from './application-review-answers';

const review:ApplicationAnswerReview = {
  profile_version:'profile-v1', answer_bank:[],
  company:'Acme', job_title:'Systems Administrator',
  question:{question_id:'q-azure', text:'Describe your experience managing Azure subscriptions.', reason:'No answer is saved for this required question.',supported:true},
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

  it('saves with the loaded version and permits readback recovery without fabricating state',async()=>{
    const readbackReview={...review,profile_version:'profile-v2',answer_bank:[saved.answer]};
    const client:ApplicationAnswerReviewClient={
      load:vi.fn().mockResolvedValueOnce(review).mockResolvedValueOnce(readbackReview),
      saveToAnswerBank:vi.fn().mockResolvedValue(saved),
    };
    const result=await saveAndReadBackAnswer(client,'profile-a','application-a',review,'I managed test subscriptions.','stable-operation');
    expect(client.saveToAnswerBank).toHaveBeenCalledWith('profile-a','application-a',{
      question_id:'q-azure',question:review.question.text,text:'I managed test subscriptions.',
      save_to_answer_bank:true,reviewed:true,expected_profile_version:'profile-v1',
    },'stable-operation');
    expect(client.load).toHaveBeenCalledTimes(2);
    expect(result).toEqual(saved.answer);
  });

  it('rejects empty text, missing deliberate review, or a save without exact readback',async()=>{
    const readbackReview={...review,profile_version:'profile-v2',answer_bank:[]};
    const client:ApplicationAnswerReviewClient={
      load:vi.fn().mockResolvedValueOnce(review).mockResolvedValueOnce(readbackReview),
      saveToAnswerBank:vi.fn().mockResolvedValue(saved),
    };
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'  ','req-1')).rejects.toThrow('Enter an answer');
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'Some answer','req-1',false)).rejects.toThrow('Review this answer');
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',review,'Some answer','req-1',true)).rejects.toThrow('matching Answer Bank readback');
    expect(client.saveToAnswerBank).toHaveBeenCalledTimes(1);
  });

  it('does not write again when retry discovers the exact answer already persisted',async()=>{
    const retriedReview={...review,profile_version:'profile-v2',answer_bank:[saved.answer]};
    const client:ApplicationAnswerReviewClient={load:vi.fn().mockResolvedValue(retriedReview),saveToAnswerBank:vi.fn()};
    await expect(saveAndReadBackAnswer(client,'profile-a','application-a',retriedReview,'I managed test subscriptions.','retry-stable')).resolves.toEqual(saved.answer);
    expect(client.saveToAnswerBank).not.toHaveBeenCalled();
  });

  it('surfaces stale profile versions before writing and preserves the user draft in the recoverable view',async()=>{
    const changed={...review,profile_version:'profile-v2'};
    const client:ApplicationAnswerReviewClient={load:vi.fn().mockResolvedValue(changed),saveToAnswerBank:vi.fn()};
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
    const client:ApplicationAnswerReviewClient={load:vi.fn(),saveToAnswerBank:vi.fn()};
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
    await expect(saveAndReadBackAnswer({load:vi.fn(),saveToAnswerBank:vi.fn()},'profile-a','application-a',unsupported,
      'I managed test subscriptions.','unsupported-retry'))
      .rejects.toThrow('unsupported by this review path');
  });
});
