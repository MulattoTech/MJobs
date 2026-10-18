import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationReviewQueue, type ReviewQueueApplication } from './application-review-queue';

const application = (overrides:Partial<ReviewQueueApplication> = {}):ReviewQueueApplication => ({
  id:'app-unknown',profile_id:'profile-a',title:'Systems Administrator',company:'Acme',
  question_preflight:{result:'UNKNOWN',reason:'No supported question metadata was found.',missing:[]},
  ...overrides,
});

describe('application review queue',()=>{
  it('shows only this profile’s persisted UNKNOWN or FAIL preflight blockers and exact question reasons',()=>{
    const html=renderToStaticMarkup(<ApplicationReviewQueue profileId="profile-a" applications={[
      application(),
      application({id:'app-fail',title:'Cloud Administrator',company:'Northwind',question_preflight:{
        result:'FAIL',reason:'UNRESOLVED_REQUIRED_QUESTIONS',missing:[{
          question_id:'azure-tenants',question:'Describe your experience managing Azure subscriptions.',
          answer_ids:['azure-proof'],profile_paths:['facts.azure'],
        }],
      }}),
      application({id:'app-pass',question_preflight:{result:'PASS',reason:null,missing:[]}}),
      application({id:'app-stale',title:'Historical result',question_preflight:{result:'FAIL',reason:'OLD_REVISION',is_current:false,missing:[]}}),
      application({id:'other-profile',profile_id:'profile-b'}),
    ]} onReview={vi.fn()}/>);
    expect(html).toContain('Acme');
    expect(html).toContain('No supported question metadata was found.');
    expect(html).toContain('Northwind');
    expect(html).toContain('Describe your experience managing Azure subscriptions.');
    expect(html).toContain('Persisted preflight is missing answers: azure-proof; profile: facts.azure.');
    expect(html).not.toContain('app-pass');
    expect(html).not.toContain('Historical result');
    expect(html).not.toContain('app-stale');
    expect(html).not.toContain('other-profile');
    expect(html).not.toContain('suggested answer');
  });

  it('has navigation-only review actions for each queued application',()=>{
    const onReview=vi.fn();
    const html=renderToStaticMarkup(<ApplicationReviewQueue profileId="profile-a" applications={[application()]} onReview={onReview}/>);
    expect(html).toContain('Review application');
    expect(html).toContain('External submission remains OFF');
    expect(html).not.toMatch(/\b(Submit|Continue|Save answer)\b/);
    expect(onReview).not.toHaveBeenCalled();
  });
});
