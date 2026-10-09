import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import {
  SourceCandidateReviewQueue,
  buildSourceCandidateReviewPayload,
  type SourceCandidate,
  type SourceCandidateReviewDraft,
} from './source-candidate-review';

const candidateId = 'candidate-0123456789abcdef0123456789abcdef';
const draft: SourceCandidateReviewDraft = {
  site_type: 'JOB_BOARD',
  reason: 'Evaluate whether this site has a supported public feed.',
  api_feed_evidence_status: 'UNKNOWN',
  api_feed_evidence_reference: '',
  api_feed_evidence_note: 'No public feed is verified.',
  access_terms_note: 'Terms are not yet verified.',
  capabilities_limits_note: 'Collection remains disabled.',
  expected_value_maintenance_note: 'Review before implementation.',
  integration_issue: '',
};
const queuedCandidate: SourceCandidate = {
  id: candidateId,
  profile_id: 'profile-1',
  domain: 'jobs.example.invalid',
  display_name: 'jobs.example.invalid',
  site_type: 'JOB_BOARD',
  state: 'INTEGRATION_QUEUED',
  first_seen_at: '2026-10-09T00:00:00Z',
  last_seen_at: '2026-10-09T00:00:00Z',
  collection_enabled: false,
  review_reason: draft.reason,
  integration_issue: null,
  integration_issue_status: 'NOT_LINKED',
  observations: [],
  review_history: [{
    id: 'review-1',
    profile_id: 'profile-1',
    state: 'INTEGRATION_QUEUED',
    site_type: 'JOB_BOARD',
    reason: draft.reason,
    api_feed_evidence_status: 'UNKNOWN',
    api_feed_evidence_reference: null,
    api_feed_evidence_note: draft.api_feed_evidence_note,
    access_terms_note: draft.access_terms_note,
    capabilities_limits_note: draft.capabilities_limits_note,
    expected_value_maintenance_note: draft.expected_value_maintenance_note,
    integration_issue: null,
    integration_issue_status: 'NOT_LINKED',
    reviewed_at: '2026-10-09T00:00:00Z',
  }],
};

describe('source candidate local integration tasks', () => {
  it('allows a bounded local task with no external issue number', () => {
    const payload = buildSourceCandidateReviewPayload(draft, 'INTEGRATION_QUEUED', 'request-1');
    expect(payload.integration_issue).toBeNull();
    expect(payload.state).toBe('INTEGRATION_QUEUED');
  });

  it('shows the durable candidate key as the local task identity after readback', () => {
    const html = renderToStaticMarkup(<SourceCandidateReviewQueue
      profileId="profile-1"
      candidates={[queuedCandidate]}
      api={{ review: vi.fn() }}
      onRetry={vi.fn()}
    />);
    expect(html).toContain('Local integration task');
    expect(html).toContain(candidateId);
    expect(html).toContain('Collection is disabled');
  });
});
