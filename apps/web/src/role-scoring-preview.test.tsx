import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {describe,expect,it,vi} from 'vitest';
import {createScoringPreviewClient,validatePreview,type ScoringPreview,type PreviewRequest} from './role-scoring-preview-client';
import {ScoringPreviewResults} from './role-scoring-preview';
import {createRoleScoringProfileDraft} from './role-scoring-profile-editor';

const request:PreviewRequest={profileId:'fixture',searchProfileId:'search',expectedVersion:'v1',
  draft:{schema_version:1,role_family:'Operations',weights:{skills:0,title:100},thresholds:{}}};
const result:ScoringPreview={schema_version:1,read_only:true,profile_id:'fixture',search_profile_id:'search',
  profile_version:'career-v1',current_version:'v1',draft_id:'a'.repeat(64),snapshot_id:'b'.repeat(64),
  candidate_limit:100,scanned_count:1,eligible_count:1,skipped_count:0,truncated:false,
  limitation:'Bounded diagnostic sample; not a submission queue. Nothing is saved.',
  jobs:[{job_id:'job-1',title:'Synthetic job',current_score:60,draft_score:100,delta:40,current_rank:1,draft_rank:1,
    draft_contributions:[{dimension:'title',weight:100,raw_score:100,weighted_contribution:100,
      explanation:'Synthetic fit contribution.',provenance:{source:'DETERMINISTIC_PROFILE_JOB_FIT'}}]}]};
const clone=()=>JSON.parse(JSON.stringify(result)) as ScoringPreview;

describe('read-only scoring preview',()=>{
  it('makes only an explicit POST and verifies its profile/search/version envelope',async()=>{
    const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify(result),{status:200}));
    const client=createScoringPreviewClient(fetcher);
    expect(fetcher).not.toHaveBeenCalled();
    expect(await client(request)).toEqual(result);
    expect(fetcher).toHaveBeenCalledTimes(1);
    const [url,init]=fetcher.mock.calls[0];
    expect(url).toBe('/profiles/fixture/search-profiles/search/role-scoring-profile/preview');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual({expected_version:'v1',profile:request.draft});
  });
  it.each(['profile_id','search_profile_id','current_version'] as const)('rejects the wrong %s',key=>{
    const bad=clone();bad[key]='other';expect(()=>validatePreview(bad,request)).toThrow();
  });
  it.each(['current_score','draft_score','delta','draft_rank','current_rank'] as const)('rejects invalid %s',key=>{
    const bad=clone();bad.jobs[0][key]=Number.NaN;expect(()=>validatePreview(bad,request)).toThrow();
  });
  it('rejects duplicate jobs, wrong contribution math and inconsistent UNKNOWN evidence',()=>{
    const duplicate=clone();duplicate.jobs.push({...duplicate.jobs[0],draft_rank:2});
    duplicate.eligible_count=2;duplicate.scanned_count=2;expect(()=>validatePreview(duplicate,request)).toThrow();
    const wrong=clone();wrong.jobs[0].draft_contributions[0].weighted_contribution=17;
    expect(()=>validatePreview(wrong,request)).toThrow();
    const unknown=clone();unknown.jobs[0].draft_contributions[0].raw_score=null;
    expect(()=>validatePreview(unknown,request)).toThrow();
  });
  it('rejects a mathematically plausible response produced for different draft weights',()=>{
    const wrong=clone();wrong.jobs[0].draft_contributions[0].weight=50;
    expect(()=>validatePreview(wrong,request)).toThrow();
    const disabled={...request,draft:{...request.draft,weights:{skills:100,title:0}}};
    expect(()=>validatePreview(result,disabled)).toThrow();
  });
  it('reports a stale server version without leaking raw error details',async()=>{
    const fetcher=vi.fn().mockResolvedValue(new Response(JSON.stringify({detail:{
      code:'SCORING_PREVIEW_VERSION_CONFLICT',private:'never expose private server details',
    }}),{status:409}));
    await expect(createScoringPreviewClient(fetcher)(request)).rejects.toThrow('Reload the saved profile');
  });
  it('renders bounded ranks, sample limits, unknowns and no-save notice accessibly',()=>{
    const html=renderToStaticMarkup(<ScoringPreviewResults value={{...result,truncated:true}}/>);
    expect(html).toContain('Preview only ? not saved');
    expect(html).toContain('sample ranks, not global ranks');
    expect(html).toContain('Draft weight ranking comparison');
    expect(html).toContain('DETERMINISTIC_PROFILE_JOB_FIT');
    expect(html).toContain('+40');
  });
  it('makes the legacy implicit Skills weight explicit in new and previously sparse drafts',()=>{
    expect(createRoleScoringProfileDraft().weights.skills).toBe(100);
    const sparse={...request.draft,weights:{title:40},id:'old-config',version:'v1'};
    expect(createRoleScoringProfileDraft(sparse).weights).toEqual({title:40,skills:100,ai_intensity:0});
    expect(sparse.weights).toEqual({title:40});
    expect(createRoleScoringProfileDraft({...sparse,weights:{skills:0,title:100}}).weights.skills).toBe(0);
  });
});
