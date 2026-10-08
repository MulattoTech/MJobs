import { describe, expect, it } from 'vitest';
import { groupCareerStories, toCareerStoryBankRows } from './career-story-bank-connected';

const row = {
  story_id: 'revision-1', story_key: 'story-1', revision: 1,
  kind: 'STAR_STORY', title: 'Reduced report latency',
  text: 'MJobs-Career-Story/1\n{"type":"mjobs.career-story","version":1,"situation":"Reports were slow.","task":"Improve them.","action":"Optimized queries.","result":"Reports ran faster."}',
  fact_ids: ['python'], fact_fingerprints: [{fact_id: 'python', fingerprint: 'fact-v1'}],
  tags: ['performance'], allowed_uses: ['RESUME'], review_status: 'PENDING',
  review_note: null, reviewed_at: null, created_at: '2026-10-08T12:00:00Z',
  is_latest: true, reusable: false, stale_fact_ids: [],
};

describe('connected career story bank mapping', () => {
  it('preserves stable story and revision identities, authored STAR text, provenance, and review state', () => {
    const stories = groupCareerStories([row]);
    expect(stories).toHaveLength(1);
    expect(stories[0].storyId).toBe('story-1');
    expect(stories[0].revisions[0]).toMatchObject({
      revisionId: 'revision-1', revision: 1, title: 'Reduced report latency',
      situation: 'Reports were slow.', task: 'Improve them.',
      action: 'Optimized queries.', result: 'Reports ran faster.',
      factIds: ['python'], factFingerprints: [{factId: 'python', fingerprint: 'fact-v1'}],
      allowedUses: ['RESUME'], reviewStatus: 'PENDING', reviewedAt: null,
      isLatest: true, staleFactIds: [],
    });
  });

  it('maps profile facts to only explicit evidence and approval fields', () => {
    expect(toCareerStoryBankRows([{id:'python',kind:'skill',text:'Python 3.12',source:'Verified work record',verified:true,user_approved:true}])).toEqual([
      {factId:'python',kind:'skill',text:'Python 3.12',source:'Verified work record',verified:true,userApproved:true},
    ]);
  });
});
