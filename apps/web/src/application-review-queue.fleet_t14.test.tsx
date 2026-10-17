import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationReviewQueue, type ReviewQueueApplication } from './application-review-queue';

const blocker = (overrides: Partial<ReviewQueueApplication> = {}): ReviewQueueApplication => ({
  id: 'app-known', profile_id: 'profile-a', title: 'Systems Administrator', company: 'Acme',
  question_preflight: { result: 'FAIL', reason: 'A required answer is unresolved.', missing: [] },
  ...overrides,
});

describe('fleet T14 partial application review queue recovery', () => {
  it('keeps loaded same-profile blockers actionable while reporting incomplete saved reads', () => {
    const onReview = vi.fn();
    const html = renderToStaticMarkup(<ApplicationReviewQueue
      profileId="profile-a"
      applications={[
        blocker(),
        blocker({ id: 'other-profile', profile_id: 'profile-b', title: 'Private profile question' }),
      ]}
      onReview={onReview}
      loadError
    />);

    expect(html).toContain('Some saved application details could not be read');
    expect(html).toContain('Acme — Systems Administrator');
    expect(html).toContain('Review application');
    expect(html).toContain('1 known');
    expect(html).not.toContain('Private profile question');
    expect(onReview).not.toHaveBeenCalled();
  });
});
