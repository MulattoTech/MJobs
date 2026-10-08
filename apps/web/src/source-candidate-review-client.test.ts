import { describe, expect, it, vi } from 'vitest';
import {
  createSourceCandidateReviewClient,
  SourceCandidateReviewClientError,
  type SourceCandidate,
  type SourceCandidateReviewInput,
} from './source-candidate-review-client';

const candidate: SourceCandidate = {
  id: 'candidate-1', profile_id: 'fixture', domain: 'talenthop.com',
  display_name: 'TalentHop', site_type: 'UNKNOWN', state: 'NEEDS_REVIEW',
  first_seen_at: '2026-10-08T10:00:00Z', last_seen_at: '2026-10-08T10:30:00Z',
  collection_enabled: false, review_reason: null, integration_issue: null,
  integration_issue_status: 'NOT_LINKED', blockers: ['Feed support is unknown.'],
  observations: [{
    id: 'obs-1', job_id: 'job-1', application_id: null,
    discovery_origin: 'LinkedIn', origin_attribution: 'USER_REPORTED',
    source_url: 'https://www.linkedin.com/jobs/view/123?tracking=private',
    application_url: 'https://talenthop.com/apply/123?session=private#form',
    observed_hops: [{ state: 'UNKNOWN' }], capture_method: 'MANUAL_CAPTURE',
    evidence_status: 'PARTIAL', observed_at: '2026-10-08T10:00:00Z',
  }],
  review_history: [],
};

const input: SourceCandidateReviewInput = {
  state: 'NEEDS_REVIEW', site_type: 'ATS', reason: 'Application destination is known.',
  api_feed_evidence_status: 'UNKNOWN', api_feed_evidence_reference: null,
  api_feed_evidence_note: 'No feed was confirmed.',
  access_terms_note: 'Terms are unknown.',
  capabilities_limits_note: 'Collection capabilities are unknown.',
  expected_value_maintenance_note: 'Value and maintenance are unknown.',
  integration_issue: null, request_id: 'review-request-1',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

describe('source candidate review API client', () => {
  it('lists only the selected profile and strips query data from display URLs', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      candidates: [{ ...candidate, profile_id: 'fixture / one' }],
    }));
    const client = createSourceCandidateReviewClient({
      baseUrl: 'http://127.0.0.1:8000/', fetchImpl,
    });

    const result = await client.list('fixture / one');

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/source-candidates?profile_id=fixture+%2F+one');
    expect(init.method).toBe('GET');
    expect(result).toHaveLength(1);
    expect(result[0].observations[0].source_url).toBe('https://www.linkedin.com/jobs/view/123');
    expect(result[0].observations[0].application_url).toBe('https://talenthop.com/apply/123');
    expect(JSON.stringify(result)).not.toContain('private');
  });

  it('rejects cross-profile list rows rather than exposing them to the selected profile', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      candidates: [candidate, { ...candidate, id: 'other-candidate', profile_id: 'other' }],
    }));
    const client = createSourceCandidateReviewClient({ fetchImpl });

    await expect(client.list('fixture')).rejects.toThrow(/profile/i);
  });

  it('uses the profile-scoped review route and preserves request ID and payload on retry', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: 'private server detail' }, 503))
      .mockResolvedValueOnce(jsonResponse({ ...candidate, id: 'candidate / one', profile_id: 'fixture / one' }));
    const client = createSourceCandidateReviewClient({ baseUrl: 'http://127.0.0.1:8000', fetchImpl });

    await expect(client.review('fixture / one', 'candidate / one', input))
      .rejects.toBeInstanceOf(SourceCandidateReviewClientError);
    const result = await client.review('fixture / one', 'candidate / one', input);

    expect(result.id).toBe('candidate / one');
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    const [firstUrl, firstInit] = fetchImpl.mock.calls[0] as [string, RequestInit];
    const [retryUrl, retryInit] = fetchImpl.mock.calls[1] as [string, RequestInit];
    expect(firstUrl).toBe('http://127.0.0.1:8000/profiles/fixture%20%2F%20one/source-candidates/candidate%20%2F%20one/review');
    expect(retryUrl).toBe(firstUrl);
    expect(firstInit.method).toBe('PUT');
    expect(firstInit.headers).toMatchObject({ 'content-type': 'application/json' });
    expect(firstInit.body).toBe(JSON.stringify(input));
    expect(retryInit.body).toBe(firstInit.body);
    expect(JSON.parse(String(retryInit.body)).request_id).toBe('review-request-1');
  });

  it('maps stale, invalid, and server failures to safe status errors without echoing response details', async () => {
    for (const status of [404, 409, 422, 500]) {
      const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ detail: 'secret=do-not-display' }, status));
      const client = createSourceCandidateReviewClient({ fetchImpl });

      try {
        await client.review('fixture', candidate.id, input);
        throw new Error('expected review request to fail');
      } catch (error) {
        expect(error).toBeInstanceOf(SourceCandidateReviewClientError);
        expect((error as Error).message).toMatch(new RegExp(String(status)));
        expect((error as Error).message).not.toContain('do-not-display');
      }
    }
  });

  it('rejects a review response for a different profile or candidate', async () => {
    const mismatches = [
      { ...candidate, profile_id: 'other' },
      { ...candidate, id: 'different-candidate' },
    ];
    for (const body of mismatches) {
      const client = createSourceCandidateReviewClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      await expect(client.review('fixture', candidate.id, input)).rejects.toThrow(/response/i);
    }
  });

  it('never returns credential-bearing display URLs from an untrusted response', async () => {
    const unsafe = {
      ...candidate,
      observations: [{
        ...candidate.observations[0], application_url: 'https://user:pass@talenthop.com/apply',
        observed_hops: ['https://user:pass@private.example/hop?token=secret'],
      }],
    };
    const client = createSourceCandidateReviewClient({
      fetchImpl: vi.fn().mockResolvedValue(jsonResponse({ candidates: [unsafe] })),
    });

    const [result] = await client.list('fixture');

    expect(result.observations[0].application_url).toBeNull();
    expect(result.observations[0].observed_hops).toEqual(['Unknown']);
    expect(JSON.stringify(result)).not.toMatch(/user:pass|secret|private\.example/);
  });
});
