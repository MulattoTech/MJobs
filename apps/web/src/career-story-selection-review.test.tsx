import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  CareerStorySelectionReview,
  assessCareerStorySelectionReview,
  type CareerStorySelectionReviewProps,
} from './career-story-selection-review';
import type { CareerStoryRevisionChoice, CareerStorySelectionCandidate } from './career-story-selection';

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

const selected: CareerStoryRevisionChoice[] = [
  {storyId: 'story-key-1', revisionId: 'story-record-2', revision: 2},
];

const baseProps: CareerStorySelectionReviewProps = {
  selected,
  candidates: [candidate],
  approvedFactIds: ['fact-1'],
  allowedUse: 'COVER_LETTER',
  onRemove: vi.fn(),
  onClear: vi.fn(),
  onPreparationReadinessChange: vi.fn(),
};

describe('career story selection review summary', () => {
  it('summarizes exact identity, permitted use, approval/currentness, verified fact count, and accessible remove/clear actions', () => {
    const html = renderToStaticMarkup(<CareerStorySelectionReview {...baseProps} />);

    expect(html).toContain('Improved service reliability');
    expect(html).toContain('story-key-1');
    expect(html).toContain('story-record-2');
    expect(html).toContain('Revision 2');
    expect(html).toContain('Permitted use: Cover letter');
    expect(html).toContain('Approval: APPROVED');
    expect(html).toContain('Currentness: Current');
    expect(html).toContain('Verified fact provenance: 1');
    expect(html).toContain('aria-label="Remove Improved service reliability revision 2"');
    expect(html).toContain('aria-label="Clear selected career stories"');
    expect(html).not.toContain('story body');
    expect(baseProps.onPreparationReadinessChange).not.toHaveBeenCalled();
  });

  it('fails closed on stale or revoked revisions and counts only currently approved fingerprinted facts', () => {
    const stale = assessCareerStorySelectionReview(
      selected,
      [{...candidate, isLatest: false, reusable: false, factIds: ['fact-1', 'fact-2']}],
      ['fact-1'],
      'COVER_LETTER',
    );
    expect(stale.canPrepare).toBe(false);
    expect(stale.items[0]).toMatchObject({currentness: 'STALE', verifiedFactCount: 1, linkedFactCount: 2});

    const revoked = assessCareerStorySelectionReview(
      selected,
      [{...candidate, reviewStatus: 'REJECTED', reusable: false}],
      ['fact-1'],
      'COVER_LETTER',
    );
    expect(revoked.canPrepare).toBe(false);
    expect(revoked.items[0]?.currentness).toBe('REVOKED');
  });

  it('renders stale/revoked warnings without story bodies or private fact values', () => {
    const privateCandidate = Object.assign({...candidate, isLatest: false, reusable: false}, {
      storyText: 'PRIVATE STORY BODY',
      factText: 'PRIVATE FACT VALUE',
    });
    const html = renderToStaticMarkup(<CareerStorySelectionReview
      {...baseProps}
      candidates={[privateCandidate]}
    />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('This selected story revision is stale. Remove it before preparation.');
    expect(html).toContain('Clear selected career stories');
    expect(html).not.toContain('PRIVATE STORY BODY');
    expect(html).not.toContain('PRIVATE FACT VALUE');
  });

  it('does not treat a missing, mismatched, or wrong-use revision as ready', () => {
    for (const row of [
      [],
      [{...candidate, revision: 3}],
      [{...candidate, allowedUses: ['RESUME']}],
      [{...candidate, factFingerprints: []}],
    ]) {
      const review = assessCareerStorySelectionReview(selected, row, ['fact-1'], 'COVER_LETTER');
      expect(review.canPrepare).toBe(false);
      expect(review.items[0]?.currentness).not.toBe('CURRENT');
    }
  });

  it('allows preparation when no story was selected and exposes an explicit empty state', () => {
    const html = renderToStaticMarkup(<CareerStorySelectionReview
      {...baseProps}
      selected={[]}
      onRemove={vi.fn()}
      onClear={vi.fn()}
    />);
    expect(html).toContain('No story revisions selected.');
    expect(assessCareerStorySelectionReview([], [], ['fact-1'], 'COVER_LETTER').canPrepare).toBe(true);
  });
});
