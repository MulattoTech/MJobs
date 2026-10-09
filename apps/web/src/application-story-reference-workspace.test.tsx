import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {afterEach,describe,expect,it,vi} from 'vitest';
import {
  ApplicationStoryReferenceWorkspace,
  createReviewedStoryCitation,
  requestApplicationStoryReferences,
  validateApplicationStoryReferenceResponse,
  type ApplicationStoryReferenceClient,
  type ApplicationStoryReferenceResponse,
  type ApplicationStoryReferenceScope,
  type CurrentApplicationStoryReference,
} from './application-story-reference-workspace';

const scope:ApplicationStoryReferenceScope={
  profile_id:'profile-a',
  application_id:'application-a',
  question:'Describe a time you solved a difficult problem.',
};

const reference:CurrentApplicationStoryReference={
  story_id:'story-revision-9',
  revision:9,
  current_revision:9,
  is_current:true,
  title:'Synthetic service recovery',
  text:'This source text remains separate from the answer body.',
  allowed_use:'APPLICATION_ANSWER',
  review_status:'APPROVED',
  fact_provenance:[
    {fact_id:'fact-linux-support',fingerprint:'sha256:approved-1'},
    {fact_id:'fact-monitoring',fingerprint:'sha256:approved-2'},
  ],
};

const response:ApplicationStoryReferenceResponse={
  ...scope,
  story_references:[reference],
};

afterEach(()=>vi.restoreAllMocks());

describe('application story reference workspace',()=>{
  it('validates exact application scope and current approved revision provenance',()=>{
    expect(validateApplicationStoryReferenceResponse(scope,response)).toEqual({ok:true,response});
    expect(validateApplicationStoryReferenceResponse(
      {...scope,profile_id:'profile-other'},response,
    )).toEqual({ok:false,code:'STALE_SCOPE'});
    expect(validateApplicationStoryReferenceResponse(
      {...scope,application_id:'application-other'},response,
    )).toEqual({ok:false,code:'STALE_SCOPE'});
    expect(validateApplicationStoryReferenceResponse(
      {...scope,question:'A different question.'},response,
    )).toEqual({ok:false,code:'STALE_SCOPE'});
    expect(validateApplicationStoryReferenceResponse(
      scope,{...response,story_references:[{...reference,is_current:false}]},
    )).toEqual({ok:false,code:'INVALID_CURRENTNESS'});
    expect(validateApplicationStoryReferenceResponse(
      scope,{...response,story_references:[{...reference,current_revision:10}]},
    )).toEqual({ok:false,code:'INVALID_CURRENTNESS'});
    expect(validateApplicationStoryReferenceResponse(
      scope,{...response,story_references:[{...reference,fact_provenance:[]}]},
    )).toEqual({ok:false,code:'INVALID_PROVENANCE'});
    expect(validateApplicationStoryReferenceResponse(
      scope,{...response,story_references:[{...reference,allowed_use:'RESUME'}]},
    )).toEqual({ok:false,code:'INVALID_REFERENCE'});
  });

  it('loads only the captured profile/application/question and drops a late response after scope changes',async()=>{
    const client:ApplicationStoryReferenceClient={load:vi.fn(async requestedScope=>({
      ...requestedScope,story_references:[reference],
    }))};
    expect(await requestApplicationStoryReferences(scope,client,()=>scope)).toEqual({
      kind:'ready',response,
    });
    expect(client.load).toHaveBeenCalledExactlyOnceWith(scope);

    let finish:(value:unknown)=>void=()=>undefined;
    const pending=requestApplicationStoryReferences(
      scope,
      {load:()=>new Promise(resolve=>{finish=resolve;})},
      ()=>activeScope,
    );
    let activeScope=scope;
    activeScope={...scope,application_id:'application-b'};
    finish(response);
    expect(await pending).toEqual({kind:'stale'});
  });

  it('surfaces loader failure as retryable and never calls the client for sensitive questions',async()=>{
    const load=vi.fn<ApplicationStoryReferenceClient['load']>()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(response);
    const client={load};
    expect(await requestApplicationStoryReferences(scope,client,()=>scope)).toEqual({kind:'error'});
    expect(await requestApplicationStoryReferences(scope,client,()=>scope)).toEqual({
      kind:'ready',response,
    });
    expect(load).toHaveBeenCalledTimes(2);

    const sensitiveClient:ApplicationStoryReferenceClient={load:vi.fn(async()=>response)};
    expect(await requestApplicationStoryReferences(
      {...scope,question:'What is your ethnic origin?'},sensitiveClient,()=>scope,
    )).toEqual({kind:'sensitive'});
    expect(sensitiveClient.load).not.toHaveBeenCalled();
  });

  it('requires explicit review and emits citation identity and provenance only',()=>{
    expect(createReviewedStoryCitation(scope,reference,false)).toBeNull();
    expect(createReviewedStoryCitation(scope,{...reference,is_current:false},true)).toBeNull();
    const citation=createReviewedStoryCitation(scope,reference,true);
    expect(citation).toEqual({
      profile_id:'profile-a',
      application_id:'application-a',
      question:scope.question,
      story_id:'story-revision-9',
      revision:9,
      allowed_use:'APPLICATION_ANSWER',
      fact_provenance:[
        {fact_id:'fact-linux-support',fingerprint:'sha256:approved-1'},
        {fact_id:'fact-monitoring',fingerprint:'sha256:approved-2'},
      ],
    });
    expect(JSON.stringify(citation)).not.toContain(reference.text);
    expect(JSON.stringify(citation)).not.toContain(reference.title);
  });

  it('renders a scoped load action without exposing answer text fields or selecting a citation',()=>{
    const html=renderToStaticMarkup(<ApplicationStoryReferenceWorkspace
      {...scope}
      client={{load:vi.fn(async()=>response)}}
      onCitationChange={vi.fn()}
    />);
    expect(html).toContain('Application story reference workspace');
    expect(html).toContain('profile-a');
    expect(html).toContain('application-a');
    expect(html).toContain('Load approved story references');
    expect(html).toContain('will not fill, generate, save, or submit answer text');
    expect(html).not.toContain('Synthetic service recovery');
    expect(html).not.toMatch(/<(textarea|input)[^>]*type="text"/);
    expect(html).not.toContain('checked=""');
  });
});
