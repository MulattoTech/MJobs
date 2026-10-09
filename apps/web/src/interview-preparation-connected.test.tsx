import {describe, expect, it} from 'vitest';
import {selectInterviewPrepStories} from './interview-preparation-connected';

describe('selectInterviewPrepStories', () => {
  it('offers only current reusable revisions approved for interview preparation', () => {
    const result = selectInterviewPrepStories('profile-1', {
      profile_id: 'profile-1',
      stories: [
        {story_id: 'approved', profile_id: 'profile-1', revision: 2, title: 'Current story',
          fact_ids: ['fact-1'], allowed_uses: ['INTERVIEW_PREPARATION'], reusable: true, is_latest: true},
        {story_id: 'old', profile_id: 'profile-1', revision: 1, title: 'Old revision',
          fact_ids: ['fact-2'], allowed_uses: ['INTERVIEW_PREPARATION'], reusable: false, is_latest: false},
        {story_id: 'resume-only', profile_id: 'profile-1', revision: 1, title: 'Resume only',
          fact_ids: ['fact-3'], allowed_uses: ['RESUME'], reusable: true, is_latest: true},
        {story_id: 'other-profile', profile_id: 'profile-2', revision: 1, title: 'Other profile',
          fact_ids: ['fact-4'], allowed_uses: ['INTERVIEW_PREPARATION'], reusable: true, is_latest: true},
      ],
    });

    expect(result).toEqual([{
      revision_id: 'approved',
      title: 'Current story',
      revision: 2,
      source_refs: ['career_story_revision:approved', 'career_fact:fact-1'],
    }]);
  });

  it('rejects a story-list response for another profile', () => {
    expect(() => selectInterviewPrepStories('profile-1', {profile_id: 'profile-2', stories: []}))
      .toThrow('Career story response belongs to a different profile.');
  });
});
