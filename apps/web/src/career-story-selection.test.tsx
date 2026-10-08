import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  CareerStorySelection,
  isCareerStorySelectionCandidateEligible,
  toggleCareerStorySelection,
  type CareerStorySelectionCandidate,
} from './career-story-selection';

const candidate: CareerStorySelectionCandidate = {
  storyId: 'story-key-1',
  revisionId: 'story-record-2',
  revision: 2,
  title: 'Improved service reliability',
  reviewStatus: 'APPROVED',
  reviewedAt: '2026-10-08T14:00:00Z',
  isLatest: true,
  reusable: true,
  allowedUses: ['COVER_LETTER'],
  factIds: ['fact-1'],
  factFingerprints: [{factId: 'fact-1', fingerprint: 'fingerprint-1'}],
  staleFactIds: [],
};

const baseProps = {
  profileId: 'profile-1',
  jobId: 'job-1',
  jobTitle: 'Systems Administrator',
  allowedUse: 'COVER_LETTER' as const,
  candidates: [candidate],
  approvedFactIds: ['fact-1'],
  onSelectionChange: vi.fn(),
};

describe('career story selection', () => {
  it('starts empty and shows revision identity, approved fact provenance, and allowed use', () => {
    const html = renderToStaticMarkup(<CareerStorySelection {...baseProps} />);

    expect(html).toContain('Story revisions for Systems Administrator');
    expect(html).toContain('Improved service reliability');
    expect(html).toContain('Revision 2');
    expect(html).toContain('Review status: APPROVED');
    expect(html).toContain('Allowed uses: Cover letter');
    expect(html).toContain('fact-1');
    expect(html).toContain('fingerprint-1');
    expect(html).toContain('type="checkbox"');
    expect(html).not.toMatch(/type="checkbox"[^>]*checked/);
    expect(baseProps.onSelectionChange).not.toHaveBeenCalled();
  });

  it('disables pending, rejected, nonlatest, stale, unapproved-fact, and wrong-use revisions', () => {
    const candidates = [
      {...candidate, revisionId: 'pending', revision: 3, reviewStatus: 'PENDING' as const},
      {...candidate, revisionId: 'rejected', revision: 4, reviewStatus: 'REJECTED' as const},
      {...candidate, revisionId: 'old', revision: 1, isLatest: false},
      {...candidate, revisionId: 'stale', revision: 5, staleFactIds: ['fact-1'], reusable: false},
      {...candidate, revisionId: 'unapproved-fact', revision: 6, factIds: ['fact-2']},
      {...candidate, revisionId: 'wrong-use', revision: 7, allowedUses: ['RESUME']},
    ];
    const html = renderToStaticMarkup(<CareerStorySelection
      {...baseProps}
      candidates={candidates}
      approvedFactIds={['fact-1']}
    />);

    expect((html.match(/disabled=""/g) || []).length).toBe(candidates.length);
    expect(html).toContain('This revision has not been approved.');
    expect(html).toContain('This revision was rejected.');
    expect(html).toContain('Only the current story revision can be reused.');
    expect(html).toContain('A linked fact is stale or unavailable.');
    expect(html).toContain('A linked fact is not currently approved.');
    expect(html).toContain('This story is not allowed for cover letter use.');
    expect(html).not.toContain('fingerprint-1');
  });

  it('returns only the explicitly chosen exact revision identity', () => {
    expect(toggleCareerStorySelection([candidate], [], 'story-record-2', true, 'COVER_LETTER', ['fact-1']))
      .toEqual({selectedRevisionIds: ['story-record-2'], selection: [{
        storyId: 'story-key-1', revisionId: 'story-record-2', revision: 2,
      }]});
    expect(toggleCareerStorySelection([candidate], ['story-record-2'], 'story-record-2', false, 'COVER_LETTER', ['fact-1']))
      .toEqual({selectedRevisionIds: [], selection: []});
  });

  it('does not return an unsupported, duplicate, or currently ineligible revision', () => {
    expect(toggleCareerStorySelection([], [], 'missing', true, 'COVER_LETTER', ['fact-1']).selection).toEqual([]);
    expect(toggleCareerStorySelection([candidate, candidate], [], 'story-record-2', true, 'COVER_LETTER', ['fact-1']).selection).toEqual([]);
    expect(toggleCareerStorySelection([candidate], [], 'story-record-2', true, 'COVER_LETTER', []).selection).toEqual([]);
    expect(isCareerStorySelectionCandidateEligible(candidate, 'COVER_LETTER', ['fact-1'])).toBe(true);
    expect(isCareerStorySelectionCandidateEligible(candidate, 'COVER_LETTER', [])).toBe(false);
  });

  it('disables duplicate revision identities until the story list is reloaded', () => {
    const html = renderToStaticMarkup(<CareerStorySelection
      {...baseProps}
      candidates={[candidate, {...candidate, title: 'Conflicting duplicate'}]}
    />);
    expect((html.match(/disabled=""/g) || []).length).toBe(2);
    expect(html).toContain('This revision identity is missing or ambiguous. Reload career stories.');
  });
});
