import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  CareerStoryReusePicker,
  isCareerStoryRevisionSelectable,
  resolveCareerStoryRevisionSelection,
  type CareerStoryReuseCandidate,
} from './career-story-reuse-picker';

const approvedFacts = [{
  factId: 'fact-1', kind: 'accomplishment', text: 'Reduced service downtime',
  source: 'Acme work history', verified: true, userApproved: true,
}];

const candidate: CareerStoryReuseCandidate = {
  storyId: 'story-1', revisionId: 'story-row-2', revision: 2,
  title: 'Improved service reliability', reviewStatus: 'APPROVED', reviewedAt: '2026-10-08T14:00:00Z',
  isLatest: true, reusable: true, allowedUses: ['RESUME', 'INTERVIEW_PREPARATION'],
  factIds: ['fact-1'], factFingerprints: [{factId: 'fact-1', fingerprint: 'fingerprint-v2'}],
  staleFactIds: [], provenance: ['fact-1'],
};

describe('career story reuse picker', () => {
  it('allows only current approved revisions linked to approved facts for the requested use', () => {
    expect(isCareerStoryRevisionSelectable(candidate, 'RESUME', approvedFacts)).toBe(true);
    expect(isCareerStoryRevisionSelectable(candidate, 'COVER_LETTER', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, reviewStatus: 'PENDING'}, 'RESUME', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, reviewStatus: 'REJECTED'}, 'RESUME', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, staleFactIds: ['fact-1']}, 'RESUME', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, reusable: false}, 'RESUME', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, isLatest: false}, 'RESUME', approvedFacts)).toBe(false);
    expect(isCareerStoryRevisionSelectable(candidate, 'RESUME', [{...approvedFacts[0], verified: false}])).toBe(false);
    expect(isCareerStoryRevisionSelectable({...candidate, factIds: []}, 'RESUME', approvedFacts)).toBe(false);
  });

  it('returns exact revision identity only for explicitly selected reusable rows', () => {
    expect(resolveCareerStoryRevisionSelection(
      [candidate],
      ['story-row-2', 'unknown-row', 'story-row-2'],
      'RESUME',
      approvedFacts,
    )).toEqual([{storyId: 'story-1', revisionId: 'story-row-2', revision: 2}]);
  });

  it('shows the exact revision, review state, allowed use, approved fact and provenance', () => {
    const html = renderToStaticMarkup(<CareerStoryReusePicker
      use="RESUME"
      candidates={[candidate]}
      approvedFacts={approvedFacts}
      selectedRevisionIds={[]}
      onSelectionChange={() => undefined}
    />);

    expect(html).toContain('Improved service reliability');
    expect(html).toContain('Revision 2');
    expect(html).toContain('Review status: APPROVED');
    expect(html).toContain('Allowed uses: Resume');
    expect(html).toContain('Reduced service downtime');
    expect(html).toContain('Acme work history');
    expect(html).toContain('fingerprint-v2');
    expect(html).toMatch(/Use .* revision 2 for resume/i);
    expect(html).not.toMatch(/type="checkbox"[^>]*checked/);
  });

  it('disables rejected, stale, and wrong-use revisions with a reason', () => {
    const html = renderToStaticMarkup(<CareerStoryReusePicker
      use="COVER_LETTER"
      candidates={[
        {...candidate, revisionId: 'pending-row', revision: 1, reviewStatus: 'PENDING'},
        {...candidate, revisionId: 'rejected-row', revision: 2, reviewStatus: 'REJECTED'},
        {...candidate, revisionId: 'stale-row', revision: 3, staleFactIds: ['fact-1'], reusable: false},
        {...candidate, revisionId: 'wrong-use-row', revision: 4, allowedUses: ['RESUME']},
      ]}
      approvedFacts={approvedFacts}
      selectedRevisionIds={[]}
      onSelectionChange={() => undefined}
    />);

    expect(html).toContain('disabled=""');
    expect(html).toContain('This revision has not been approved.');
    expect(html).toContain('This revision was rejected.');
    expect(html).toContain('A linked fact is stale or unavailable.');
    expect(html).toContain('This story is not allowed for cover letter use.');
  });
});
