import { describe, expect, it, vi } from 'vitest';
import {
  createMatchFeedbackClient,
  MatchFeedbackClientError,
  type MatchFeedbackCategory,
} from './match-feedback-client';

const profileId = 'profile 1';
const jobId = 'job/2';
const category: MatchFeedbackCategory = 'GOOD_MATCH';
const feedback = { id: 'feedback-7', category, created_at: '2026-10-09T10:30:00Z' };
const proposal = {
  dimension: 'technology' as const,
  direction: 'increase' as const,
  explanation: 'Linux matches were helpful.',
  review_required: true as const,
};
const appliedPreference = {
  id: 'applied-1', job_id: jobId, company_id: 'company-1', feedback_id: feedback.id,
  category: 'GOOD_MATCH' as const, dimension: 'salary' as const,
  direction: 'INCREASE' as const, created_at: '2026-10-09T11:30:00Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe('match feedback client', () => {
  it('posts once then confirms the same scoped category through collection readback', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        profile_id: profileId,
        job_id: jobId,
        feedback,
        replayed: false,
        preference_proposal: proposal,
      }, 201))
      .mockResolvedValueOnce(jsonResponse({
        profile_id: profileId,
        job_id: jobId,
        items: [feedback],
        latest: feedback,
      }));
    const client = createMatchFeedbackClient(fetchImpl);

    const result = await client.submit(profileId, jobId, category, 'request-stable');

    expect(result).toEqual({
      profile_id: profileId,
      job_id: jobId,
      feedback,
      items: [feedback],
      latest: feedback,
      replayed: false,
      preference_proposal: proposal,
    });
    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      '/profiles/profile%201/jobs/job%2F2/match-feedback',
      {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ category, request_id: 'request-stable' }),
      },
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      '/profiles/profile%201/jobs/job%2F2/match-feedback',
      { method: 'GET', headers: { accept: 'application/json' } },
    );
    expect(JSON.stringify(result.items)).not.toContain('request-stable');
  });

  it('does not report success if readback scope or requested feedback is absent', async () => {
    const cases = [
      { profile_id: 'foreign-profile', job_id: jobId, items: [feedback], latest: feedback },
      { profile_id: profileId, job_id: 'foreign-job', items: [feedback], latest: feedback },
      { profile_id: profileId, job_id: jobId, items: [], latest: null },
      { profile_id: profileId, job_id: jobId, items: [{ ...feedback, category: 'BAD_MATCH' }], latest: null },
    ];
    for (const readback of cases) {
      const fetchImpl = vi.fn()
        .mockResolvedValueOnce(jsonResponse({
          profile_id: profileId, job_id: jobId, feedback, replayed: false,
          preference_proposal: null,
        }))
        .mockResolvedValueOnce(jsonResponse(readback));
      const client = createMatchFeedbackClient(fetchImpl);
      await expect(client.submit(profileId, jobId, category, 'request-stable'))
        .rejects.toBeInstanceOf(MatchFeedbackClientError);
    }
  });

  it('uses status-only safe errors and skips readback after a failed POST', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      detail: 'private service text that must not reach the UI',
    }, 503));
    const client = createMatchFeedbackClient(fetchImpl);
    let failure: unknown;
    try {
      await client.submit(profileId, jobId, category, 'request-stable');
    } catch (error) {
      failure = error;
    }
    expect(failure).toBeInstanceOf(MatchFeedbackClientError);
    expect(failure).toMatchObject({ status: 503, retryable: true });
    expect((failure as Error).message).not.toContain('private service text');
    expect(fetchImpl).toHaveBeenCalledOnce();
  });

  it('rejects invalid category or request identifiers before sending', async () => {
    const fetchImpl = vi.fn();
    const client = createMatchFeedbackClient(fetchImpl);
    await expect(client.submit(profileId, jobId, 'UNSUPPORTED' as MatchFeedbackCategory, 'request'))
      .rejects.toMatchObject({ kind: 'INVALID_REQUEST' });
    await expect(client.submit(profileId, jobId, category, ' '))
      .rejects.toMatchObject({ kind: 'INVALID_REQUEST' });
    expect(fetchImpl).not.toHaveBeenCalled();
  });

  it('applies only after explicit request and confirms the profile-scoped preference by readback', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        profile_id: profileId, replayed: false, preference: appliedPreference,
      }))
      .mockResolvedValueOnce(jsonResponse({
        profile_id: profileId, items: [appliedPreference], by_dimension: { salary: appliedPreference },
      }));
    const client = createMatchFeedbackClient(fetchImpl);

    const result = await client.applyPreference!(
      profileId, feedback.id, 'salary', 'increase', 'apply-stable',
    );

    expect(result).toEqual([appliedPreference]);
    expect(fetchImpl).toHaveBeenNthCalledWith(1,
      '/profiles/profile%201/match-preferences', {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({
          feedback_id: feedback.id, dimension: 'salary', direction: 'increase', request_id: 'apply-stable',
        }),
      });
    expect(fetchImpl).toHaveBeenNthCalledWith(2,
      '/profiles/profile%201/match-preferences',
      { headers: { accept: 'application/json' } });
  });

  it('does not report preference success when durable readback omits the posted event', async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        profile_id: profileId, replayed: false, preference: appliedPreference,
      }))
      .mockResolvedValueOnce(jsonResponse({ profile_id: profileId, items: [], by_dimension: {} }));
    const client = createMatchFeedbackClient(fetchImpl);
    await expect(client.applyPreference!(
      profileId, feedback.id, 'salary', 'increase', 'apply-stable',
    )).rejects.toMatchObject({ kind: 'READBACK_NOT_CONFIRMED', retryable: true });
  });
});
