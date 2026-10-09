import { describe, expect, it, vi } from 'vitest';
import {
  createJobComparisonClient,
  JobComparisonClientError,
} from './job-comparison-client';

const profileId = 'profile 7';
const searchProfileId = 'search #2';
const jobIds = ['job 2', 'job 1'];

const responseBody = {
  profile_id: profileId,
  search_profile_id: searchProfileId,
  job_ids: [...jobIds],
  jobs: [
    {
      job_id: 'job 2',
      title: 'Cloud Administrator',
      company: 'Company Two',
      match_score: 82.5,
      scoring_contributions: [{
        dimension: 'skills',
        weight: 70,
        raw_score: 90,
        weighted_contribution: 63,
        explanation: 'Stored deterministic score contribution.',
        provenance: { source: 'legacy_match', evidence_ref: 'match:job-2' },
      }],
    },
    {
      job_id: 'job 1',
      title: null,
      company: null,
      match_score: null,
      scoring_contributions: [],
    },
  ],
  shared_requirements: [{
    term: 'kubernetes',
    job_ids: ['job 2', 'job 1'],
    provenance: [
      { job_id: 'job 2', source: 'structured_feed', evidence_ref: 'req-2' },
      { job_id: 'job 1', source: 'structured_feed', evidence_ref: 'req-1' },
    ],
  }],
  job_specific_requirements: [{
    term: 'ansible',
    job_id: 'job 2',
    provenance: { source: 'structured_feed', evidence_ref: 'req-2-ansible' },
  }],
  unknowns: [
    { category: 'required_terms', job_ids: ['job 1'], reason: 'REQUIREMENTS_MISSING' },
    { category: 'match_score', job_ids: ['job 1'], reason: 'MATCH_SCORE_MISSING' },
    { category: 'contributions', job_ids: ['job 1'], reason: 'SCORING_CONTRIBUTIONS_MISSING' },
  ],
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('job comparison client', () => {
  it('posts only the selected scope and preserves stored scores, contributions, provenance, and unknowns', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(responseBody));
    const client = createJobComparisonClient(fetchImpl);

    const result = await client.compare(profileId, searchProfileId, jobIds);

    expect(result).toEqual(responseBody);
    expect(result.jobs[0].scoring_contributions[0].provenance).toEqual({
      source: 'legacy_match', evidence_ref: 'match:job-2',
    });
    expect(result.jobs[1].match_score).toBeNull();
    expect(result.unknowns).toContainEqual({
      category: 'match_score', job_ids: ['job 1'], reason: 'MATCH_SCORE_MISSING',
    });
    expect(fetchImpl).toHaveBeenCalledExactlyOnceWith(
      '/profiles/profile%207/search-profiles/search%20%232/jobs/compare',
      {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ job_ids: jobIds }),
      },
    );
  });

  it('rejects mismatched profile or saved-search echoes without exposing the body', async () => {
    for (const override of [
      { profile_id: 'foreign-profile' },
      { search_profile_id: 'foreign-search' },
    ]) {
      const client = createJobComparisonClient(vi.fn().mockResolvedValue(jsonResponse({
        ...responseBody, ...override, private: 'do-not-leak',
      })));
      await expect(client.compare(profileId, searchProfileId, jobIds)).rejects.toMatchObject({
        kind: 'SCOPE_MISMATCH', retryable: false,
      });
    }
  });

  it('rejects reordered or foreign selected-job IDs', async () => {
    const cases = [
      { ...responseBody, job_ids: ['job 1', 'job 2'] },
      { ...responseBody, jobs: [responseBody.jobs[1], responseBody.jobs[0]] },
      { ...responseBody, jobs: [{ ...responseBody.jobs[0], job_id: 'foreign-job' }, responseBody.jobs[1]] },
    ];
    for (const body of cases) {
      const client = createJobComparisonClient(vi.fn().mockResolvedValue(jsonResponse(body)));
      await expect(client.compare(profileId, searchProfileId, jobIds)).rejects.toMatchObject({
        kind: 'SCOPE_MISMATCH',
      });
    }
  });

  it('fails closed on missing or malformed unknown fields and requirement rows', async () => {
    const cases = [
      (() => { const { unknowns: _unknowns, ...missing } = responseBody; return missing; })(),
      { ...responseBody, unknowns: [{ category: 'candidate_qualification', job_ids: ['job 2'], reason: 'INFERRED' }] },
      { ...responseBody, unknowns: [{ category: 'required_terms', job_ids: ['foreign-job'], reason: 'REQUIREMENTS_MISSING' }] },
      { ...responseBody, shared_requirements: [{ ...responseBody.shared_requirements[0], provenance: [] }] },
      { ...responseBody, job_specific_requirements: [{ ...responseBody.job_specific_requirements[0], provenance: { source: '' } }] },
      { ...responseBody, jobs: [{ ...responseBody.jobs[0], scoring_contributions: [{ dimension: 'skills' }] }, responseBody.jobs[1]] },
      { ...responseBody, jobs: [responseBody.jobs[0], { ...responseBody.jobs[1], match_score: 90 }] },
    ];
    for (const body of cases) {
      const client = createJobComparisonClient(vi.fn().mockResolvedValue(jsonResponse(body)));
      await expect(client.compare(profileId, searchProfileId, jobIds)).rejects.toBeInstanceOf(
        JobComparisonClientError,
      );
    }
  });

  it('preserves separate unknown reasons for one job and category', async () => {
    const body = {
      ...responseBody,
      unknowns: [
        ...responseBody.unknowns,
        { category: 'required_terms' as const, job_ids: ['job 1'], reason: 'REQUIREMENT_TERM_UNKNOWN' as const },
        { category: 'required_terms' as const, job_ids: ['job 1'], reason: 'REQUIREMENT_PROVENANCE_UNKNOWN' as const },
      ],
    };
    const client = createJobComparisonClient(vi.fn().mockResolvedValue(jsonResponse(body)));

    const result = await client.compare(profileId, searchProfileId, jobIds);

    expect(result.unknowns.slice(-2)).toEqual(body.unknowns.slice(-2));
  });

  it('maps stale 409 to a safe refresh error without leaking response details', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      detail: { code: 'JOB_COMPARISON_STALE_CONTEXT', secret: 'private-server-detail' },
    }, 409));
    const client = createJobComparisonClient(fetchImpl);

    await expect(client.compare(profileId, searchProfileId, jobIds)).rejects.toMatchObject({
      kind: 'STALE_CONTEXT', state: 'STALE', status: 409, recovery: 'REFRESH', retryable: false,
    });
    try {
      await client.compare(profileId, searchProfileId, jobIds);
    } catch (error) {
      expect(error).toBeInstanceOf(JobComparisonClientError);
      expect((error as Error).message).toMatch(/refresh/i);
      expect((error as Error).message).not.toMatch(/private-server-detail|JOB_COMPARISON/);
      expect(error).not.toHaveProperty('response');
      expect(error).not.toHaveProperty('body');
    }
  });

  it('validates request IDs and the 2-to-10 unique selection before fetching', async () => {
    const fetchImpl = vi.fn();
    const client = createJobComparisonClient(fetchImpl);

    await expect(client.compare(profileId, searchProfileId, ['job-1'])).rejects.toMatchObject({
      kind: 'INVALID_REQUEST',
    });
    await expect(client.compare(profileId, searchProfileId, ['job-1', 'job-1'])).rejects.toMatchObject({
      kind: 'INVALID_REQUEST',
    });
    await expect(client.compare('profile/other', searchProfileId, jobIds)).rejects.toMatchObject({
      kind: 'INVALID_REQUEST',
    });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
