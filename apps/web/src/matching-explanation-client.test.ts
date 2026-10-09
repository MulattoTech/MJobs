import { describe, expect, it, vi } from 'vitest';
import {
  createMatchingExplanationClient,
  MatchingExplanationClientError,
} from './matching-explanation-client';

const profileId = 'profile 7';
const searchProfileId = 'search #2';
const jobId = 'job 3';

const current = {
  profile_id: profileId,
  search_profile_id: searchProfileId,
  job_id: jobId,
  score: 82.5,
  resolution: 'CURRENT',
  profile_resolution: {
    requested_role_family: 'Cloud Operations',
    selected_role_family: 'Cloud Operations',
    profile_id: 'scoring-profile-4',
    profile_version: 'version-9',
    selection: 'ROLE_FAMILY',
  },
  scoring_profile: { id: 'scoring-profile-4', version: 'version-9', role_family: 'Cloud Operations' },
  scoring_contributions: [
    {
      dimension: 'skills', weight: 70, raw_score: 90, weighted_contribution: 63,
      explanation: 'Strong skills overlap.', provenance: { source: 'deterministic_matcher', evidence_ref: 'job:job 3' },
    },
    {
      dimension: 'ai_intensity', weight: 30, raw_score: null, weighted_contribution: null,
      explanation: 'AI intensity is UNKNOWN because the job has no attributed source signal.',
      provenance: { source: 'UNKNOWN' },
    },
  ],
};

function legacy(overrides: Record<string, unknown> = {}) {
  return {
    profile_id: profileId,
    search_profile_id: searchProfileId,
    job_id: jobId,
    score: 61,
    resolution: 'LEGACY_DEFAULT',
    profile_resolution: {
      requested_role_family: null, selected_role_family: null,
      profile_id: null, profile_version: null, selection: 'LEGACY_DEFAULT',
    },
    scoring_profile: null,
    scoring_contributions: [{
      dimension: 'skills', weight: 100, raw_score: 61, weighted_contribution: 61,
      explanation: 'Legacy match score is preserved.', provenance: { source: 'LEGACY_MATCH_BASELINE' },
    }],
    ...overrides,
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

describe('saved match explanation client', () => {
  it('loads a current scoped explanation and preserves known provenance and UNKNOWN rows', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(current));
    const client = createMatchingExplanationClient({ fetchImpl });

    const result = await client.load(profileId, searchProfileId, jobId);

    expect(result).toMatchObject({
      profile_id: profileId,
      search_profile_id: searchProfileId,
      job_id: jobId,
      resolution: 'CURRENT',
      score: 82.5,
      scoring_profile: { id: 'scoring-profile-4', version: 'version-9', role_family: 'Cloud Operations' },
      scoring_contributions: [
        { dimension: 'skills', status: 'KNOWN', provenance: { source: 'deterministic_matcher', evidence_ref: 'job:job 3' } },
        { dimension: 'ai_intensity', status: 'UNKNOWN', raw_score: null, weighted_contribution: null,
          provenance: { source: 'UNKNOWN' } },
      ],
      state: 'UNKNOWN',
    });
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/profiles/profile%207/search-profiles/search%20%232/jobs/job%203/match-explanation');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('returns LEGACY_DEFAULT attribution without inventing a scoring profile', async () => {
    const client = createMatchingExplanationClient({
      fetchImpl: vi.fn().mockResolvedValue(jsonResponse(legacy())),
    });

    const result = await client.load(profileId, searchProfileId, jobId);

    expect(result.resolution).toBe('LEGACY_DEFAULT');
    expect(result.state).toBe('READY');
    expect(result.scoring_profile).toBeNull();
    expect(result.profile_resolution.selection).toBe('LEGACY_DEFAULT');
  });

  it('rejects any profile, saved-search, or job scope echo mismatch', async () => {
    const cases = [
      { profile_id: 'foreign-profile' },
      { search_profile_id: 'foreign-search' },
      { job_id: 'foreign-job' },
    ];
    for (const override of cases) {
      const client = createMatchingExplanationClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse({ ...current, ...override })),
      });
      await expect(client.load(profileId, searchProfileId, jobId)).rejects.toMatchObject({
        kind: 'SCOPE_MISMATCH', retryable: false,
      });
    }
  });

  it('maps stale-context 409 to a safe refresh state without leaking response details', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      detail: { code: 'MATCH_EXPLANATION_STALE_CONTEXT', secret: 'private-server-detail' },
    }, 409));
    const client = createMatchingExplanationClient({ fetchImpl });

    await expect(client.load(profileId, searchProfileId, jobId)).rejects.toMatchObject({
      kind: 'STALE_CONTEXT', state: 'STALE', status: 409, recovery: 'REFRESH', retryable: false,
    });
    try {
      await client.load(profileId, searchProfileId, jobId);
    } catch (error) {
      expect(error).toBeInstanceOf(MatchingExplanationClientError);
      expect((error as Error).message).toMatch(/refresh/i);
      expect((error as Error).message).not.toMatch(/private-server-detail|MATCH_EXPLANATION/);
      expect(error).not.toHaveProperty('response');
      expect(error).not.toHaveProperty('body');
    }
  });

  it('rejects malformed values, attribution mismatch, and inconsistent UNKNOWN rows', async () => {
    const malformed = [
      { ...current, score: Number.NaN },
      { ...current, profile_resolution: { ...current.profile_resolution, profile_version: 'older' } },
      { ...current, scoring_contributions: [{ ...current.scoring_contributions[0], weight: Infinity }] },
      { ...current, scoring_contributions: [{ ...current.scoring_contributions[0], provenance: { source: '' } }] },
      { ...current, scoring_contributions: [{ ...current.scoring_contributions[1], weighted_contribution: 0 }] },
      { ...current, scoring_contributions: [{ ...current.scoring_contributions[0] }, { ...current.scoring_contributions[0] }] },
    ];
    for (const body of malformed) {
      const client = createMatchingExplanationClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      await expect(client.load(profileId, searchProfileId, jobId)).rejects.toBeInstanceOf(MatchingExplanationClientError);
    }
  });

  it('rejects path-changing identifiers without issuing a request', async () => {
    const fetchImpl = vi.fn();
    const client = createMatchingExplanationClient({ fetchImpl });

    await expect(client.load('profile/other', searchProfileId, jobId)).rejects.toMatchObject({ kind: 'INVALID_REQUEST' });
    await expect(client.load(profileId, '..', jobId)).rejects.toMatchObject({ kind: 'INVALID_REQUEST' });
    await expect(client.load(profileId, searchProfileId, 'job\u0000id')).rejects.toMatchObject({ kind: 'INVALID_REQUEST' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('uses only the read-only explanation GET path and never calls generation or submission routes', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(current));
    const client = createMatchingExplanationClient({ fetchImpl });

    await client.load(profileId, searchProfileId, jobId);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toContain('/match-explanation');
    expect(url).not.toMatch(/generate|submit|application|ats/i);
    expect(init.method).toBe('GET');
  });
});
