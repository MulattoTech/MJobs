import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  SourceCandidateReviewQueue,
  buildSourceCandidateReviewPayload,
  requestSourceCandidateReview,
  type SourceCandidate,
  type SourceCandidateReviewDraft,
} from './source-candidate-review';

const candidate: SourceCandidate = {
  id: 'candidate-1', profile_id: 'profile-1', domain: 'talenthop.com', display_name: 'TalentHop',
  site_type: 'UNKNOWN', state: 'NEEDS_REVIEW', first_seen_at: '2026-10-07T10:00:00Z',
  last_seen_at: '2026-10-08T11:30:00Z', collection_enabled: false,
  review_reason: 'Application destination; source support unknown.', integration_issue: null,
  integration_issue_status: 'NOT_LINKED', blockers: ['Public feed not verified.', '403 evidence is not a feed test.'],
  observations: [{
    id: 'obs-1', job_id: 'job-3', application_id: 'app-8', discovery_origin: 'LinkedIn',
    origin_attribution: 'USER_REPORTED', source_url: null,
    application_url: 'https://talenthop.com/apply/3', observed_hops: [{ state: 'UNKNOWN' }],
    capture_method: 'MANUAL_CAPTURE', evidence_status: 'PARTIAL', observed_at: '2026-10-07T10:00:00Z',
  }],
  review_history: [],
};

const draft: SourceCandidateReviewDraft = {
  site_type: 'ATS', reason: 'Destination only; public feed remains unknown.',
  api_feed_evidence_status: 'UNKNOWN', api_feed_evidence_reference: '',
  api_feed_evidence_note: 'No public feed was verified.', access_terms_note: 'Terms remain unreviewed.',
  capabilities_limits_note: 'No collection capability is confirmed.',
  expected_value_maintenance_note: 'Value and maintenance are not yet established.', integration_issue: '401',
};

function render(overrides: Partial<React.ComponentProps<typeof SourceCandidateReviewQueue>> = {}) {
  return renderToStaticMarkup(<SourceCandidateReviewQueue
    profileId="profile-1"
    candidates={[candidate]}
    api={{ review: vi.fn() }}
    onRetry={vi.fn()}
    {...overrides}
  />);
}

describe('source candidate review queue', () => {
  it('shows domain, user-reported origin, destination, evidence, unknowns and review status', () => {
    const html = render();
    expect(html).toContain('TalentHop');
    expect(html).toContain('talenthop.com');
    expect(html).toContain('First seen');
    expect(html).toContain('2026-10-07T10:00:00.000Z');
    expect(html).toContain('Last seen');
    expect(html).toContain('2026-10-08T11:30:00.000Z');
    expect(html).toContain('Discovery origin (user reported)');
    expect(html).toContain('LinkedIn');
    expect(html).toContain('Source URL: <code>Unknown</code>');
    expect(html).toContain('Application destination');
    expect(html).toContain('https://talenthop.com/apply/3');
    expect(html).toContain('Linked job: job-3');
    expect(html).toContain('Linked application: app-8');
    expect(html).toContain('Unknown hop');
    expect(html).toContain('Evidence status: PARTIAL');
    expect(html).toContain('Blockers and unknowns');
    expect(html).toContain('403 evidence is not a feed test.');
    expect(html).toContain('Review state: NEEDS REVIEW');
  });

  it('shows accessible loading, empty, no-profile, and retryable error states', () => {
    expect(render({ loading: true, candidates: [] })).toContain('Loading source candidates');
    expect(render({ candidates: [] })).toContain('No source candidates need review for this profile.');
    expect(render({ profileId: null, candidates: [] })).toContain('Select a profile to review its source candidates.');
    const failed = render({ error: 'Source candidates could not be loaded.', candidates: [] });
    expect(failed).toContain('role="alert"');
    expect(failed).toContain('Retry loading candidates');
  });

  it('keeps review separate from collection and exposes explicit review outcomes', () => {
    const html = render();
    expect(html).toContain('Collection is disabled. Review does not enable collection.');
    expect(html).not.toContain('Enable collection');
    expect(html).toContain('Queue integration review');
    expect(html).toContain('Mark blocked');
    expect(html).toContain('Reject candidate');
    expect(html).toContain('Keep needs review');
  });

  it('shows the sanitized public API or feed reference in durable review history', () => {
    const reviewed = {
      ...candidate,
      review_history: [{
        id: 'review-1', profile_id: 'profile-1', state: 'BLOCKED' as const,
        site_type: 'ATS' as const, reason: 'Access was denied.',
        api_feed_evidence_status: 'BLOCKED' as const,
        api_feed_evidence_reference: 'https://talenthop.com/api/jobs',
        api_feed_evidence_note: 'Prior endpoint request returned 403.',
        access_terms_note: 'Terms remain unverified.',
        capabilities_limits_note: 'No feed has been confirmed.',
        expected_value_maintenance_note: 'Value remains unknown.',
        integration_issue: null, integration_issue_status: 'NOT_LINKED' as const,
        reviewed_at: '2026-10-09T00:00:00Z',
      }],
    };
    const html = render({ candidates: [reviewed] });
    expect(html).toContain('Public API or feed reference: <code>https://talenthop.com/api/jobs</code>');
  });

  it('does not invoke the review callback just by rendering', () => {
    const review = vi.fn();
    renderToStaticMarkup(<SourceCandidateReviewQueue
      profileId="profile-1"
      candidates={[candidate]}
      api={{ review }}
      onRetry={vi.fn()}
    />);
    expect(review).not.toHaveBeenCalled();
  });

  it('validates evidence and only carries an issue reference into integration review', () => {
    expect(() => buildSourceCandidateReviewPayload(
      { ...draft, api_feed_evidence_note: '' }, 'NEEDS_REVIEW', 'request-1',
    )).toThrow(/complete/i);
    expect(() => buildSourceCandidateReviewPayload(
      { ...draft, api_feed_evidence_status: 'PUBLIC_DOCUMENTED', api_feed_evidence_reference: 'javascript:alert(1)' },
      'NEEDS_REVIEW', 'request-1',
    )).toThrow(/HTTP or HTTPS/i);
    const blocked = buildSourceCandidateReviewPayload(draft, 'BLOCKED', 'request-1');
    expect(blocked.integration_issue).toBeNull();
    expect(Object.keys(blocked)).not.toContain('collection_enabled');
    expect(buildSourceCandidateReviewPayload(draft, 'INTEGRATION_QUEUED', 'request-1'))
      .toMatchObject({ state: 'INTEGRATION_QUEUED', integration_issue: 401 });
  });

  it('retries the explicit profile-scoped callback with the same criteria and request ID', async () => {
    const api = { review: vi.fn().mockRejectedValueOnce(new Error('Temporary review error.'))
      .mockResolvedValueOnce(candidate) };
    await expect(requestSourceCandidateReview(api, 'profile-1', candidate, draft, 'INTEGRATION_QUEUED', 'request-7'))
      .rejects.toThrow('Temporary review error.');
    await requestSourceCandidateReview(api, 'profile-1', candidate, draft, 'INTEGRATION_QUEUED', 'request-7');
    expect(api.review).toHaveBeenCalledTimes(2);
    expect(api.review.mock.calls[0]).toEqual(api.review.mock.calls[1]);
    expect(draft.reason).toBe('Destination only; public feed remains unknown.');
    await expect(requestSourceCandidateReview(api, 'other-profile', candidate, draft, 'NEEDS_REVIEW', 'request-8'))
      .rejects.toThrow(/does not belong to the selected profile/i);
  });
});
