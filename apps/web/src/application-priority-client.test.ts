import { describe, expect, it, vi } from 'vitest';
import {
  createApplicationPriorityClient,
  ApplicationPriorityClientError,
} from './application-priority-client';

const result = {
  profile_id: 'profile 7', job_id: 'job #2', match_score: 83,
  priority_score: null, priority_band: 'UNKNOWN', coverage_percent: 25,
  factors: [
    { key: 'match', label: 'Match score', score: 83, contribution: 25,
      status: 'KNOWN', explanation: 'Stored match score.', source: 'match.score' },
    { key: 'salary', label: 'Salary', score: null, contribution: null,
      status: 'UNKNOWN', explanation: 'No salary comparison is available.', source: 'UNKNOWN' },
  ],
  unknowns: ['salary'], advisory: true, affects_match_score: false,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status, headers: { 'content-type': 'application/json' },
  });
}

describe('application priority API client', () => {
  it('loads an advisory projection for the exact selected profile and job', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(result));
    const client = createApplicationPriorityClient({
      baseUrl: 'http://127.0.0.1:8000/', fetchImpl,
    });

    const loaded = await client.load(result.profile_id, result.job_id);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/profiles/profile%207/jobs/job%20%232/application-priority');
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(loaded.profile_id).toBe(result.profile_id);
    expect(loaded.job_id).toBe(result.job_id);
    expect(loaded.match_score).toBe(83);
    expect(loaded.priority_band).toBe('UNKNOWN');
    expect(loaded.priority_score).toBeNull();
    expect(loaded.factors[1].status).toBe('UNKNOWN');
    expect(loaded.unknowns).toEqual(['salary']);
    expect(loaded.affects_match_score).toBe(false);
    expect(loaded.state).toBe('UNKNOWN');
  });

  it('classifies profile and job scope mismatches without returning response data or retry hints', async () => {
    for (const body of [
      { ...result, profile_id: 'another-profile' },
      { ...result, job_id: 'another-job' },
    ]) {
      const client = createApplicationPriorityClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      try {
        await client.load(result.profile_id, result.job_id);
        throw new Error('expected mismatched scope to be rejected');
      } catch (error) {
        expect(error).toBeInstanceOf(ApplicationPriorityClientError);
        expect(error).toMatchObject({
          kind: 'SCOPE_MISMATCH', status: null, recovery: 'NONE', retryable: false,
        });
        expect((error as Error).message).toMatch(/does not match the selected profile and job/i);
        expect((error as Error).message).not.toMatch(/retry/i);
        expect((error as Error).message).not.toMatch(/another-profile|another-job/);
        expect(error).not.toHaveProperty('response');
        expect(error).not.toHaveProperty('body');
      }
    }
  });

  it('rejects malformed priority data instead of coercing unknowns or scores', async () => {
    const malformed = [
      { ...result, priority_score: 80, priority_band: 'UNKNOWN' },
      { ...result, unknowns: [] },
      { ...result, match_score: Number.POSITIVE_INFINITY },
      { ...result, factors: [{ ...result.factors[1], score: 0 }] },
    ];
    for (const body of malformed) {
      const client = createApplicationPriorityClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse(body)),
      });
      await expect(client.load(result.profile_id, result.job_id))
        .rejects.toBeInstanceOf(ApplicationPriorityClientError);
    }
  });

  it('maps stale, missing, and transient server responses to safe recovery states', async () => {
    const responses = [
      { status: 404, kind: 'NOT_FOUND', recovery: 'NONE', retryable: false },
      { status: 409, kind: 'STALE', recovery: 'REFRESH', retryable: false },
      { status: 503, kind: 'HTTP_ERROR', recovery: 'RETRY', retryable: true },
    ] as const;
    for (const expected of responses) {
      const client = createApplicationPriorityClient({
        fetchImpl: vi.fn().mockResolvedValue(jsonResponse({ detail: 'private failure detail' }, expected.status)),
      });
      try {
        await client.load(result.profile_id, result.job_id);
        throw new Error('expected application priority request to fail');
      } catch (error) {
        expect(error).toBeInstanceOf(ApplicationPriorityClientError);
        expect(error).toMatchObject(expected);
        expect((error as Error).message).not.toContain('private failure detail');
      }
    }
  });

  it('marks network failures retryable without retrying or sending a write', async () => {
    const fetchImpl = vi.fn().mockRejectedValue(new Error('offline'));
    const client = createApplicationPriorityClient({ fetchImpl });

    await expect(client.load(result.profile_id, result.job_id))
      .rejects.toMatchObject({ kind: 'NETWORK', recovery: 'RETRY', retryable: true });

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect((fetchImpl.mock.calls[0] as [string, RequestInit])[1].method).toBe('GET');
  });

  it('rejects path-changing identifiers before making a request', async () => {
    const fetchImpl = vi.fn();
    const client = createApplicationPriorityClient({ fetchImpl });

    await expect(client.load('profile/other', result.job_id))
      .rejects.toMatchObject({ kind: 'INVALID_REQUEST', retryable: false });
    await expect(client.load(result.profile_id, '..'))
      .rejects.toMatchObject({ kind: 'INVALID_REQUEST', retryable: false });
    expect(fetchImpl).not.toHaveBeenCalled();
  });
});
