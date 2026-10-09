import {describe, expect, it, vi} from 'vitest';
import {
  loadInterviewPreparation,
  saveInterviewPreparation,
  type InterviewPrepPack,
} from './interview-preparation-client';

const pack: InterviewPrepPack = {
  schema_version: 1,
  pack_id: 'prep-pack-1',
  pack_version: 1,
  profile_id: 'profile-a',
  profile_version: 'profile-v4',
  job_id: 'job-a',
  job_fingerprint: 'job-fingerprint-a',
  created_at: '2026-10-08T14:00:00Z',
  questions: [{id: 'question-1', prompt: 'Tell me about a time you solved a problem.', source_refs: ['story:story-row-1']}],
  talking_points: [{text: 'Resolved a documented service issue.', source_refs: ['fact:fact-1']}],
  star_prompts: [{
    question: 'Describe the service issue.',
    situation_prompt: 'What was the situation?',
    task_prompt: 'What was your responsibility?',
    action_prompt: 'What did you do?',
    result_prompt: 'What recorded result can you support?',
    source_refs: ['story:story-row-1'],
  }],
  missing_evidence: [{topic: 'Impact', reason: 'No approved impact measure is linked.'}],
  selected_story_revision_ids: ['story-row-1'],
  export_text: 'Deterministic prep export',
};

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function json(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json'}});
}

describe('interview preparation API client', () => {
  it('loads only the profile/job-scoped history from the collection GET', async () => {
    const fetcher = vi.fn<Fetcher>(async () => json({
      schema_version: 1, profile_id: 'profile-a', job_id: 'job-a', packs: [pack],
    }));

    const result = await loadInterviewPreparation('profile-a', 'job-a', fetcher);

    expect(result).toEqual({status: 'success', value: [pack]});
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledWith('/profiles/profile-a/jobs/job-a/interview-prep-packs', {
      headers: {'Content-Type': 'application/json'},
    });
  });

  it('reports save success only after exact detail readback matches the POST response', async () => {
    const fetcher = vi.fn<Fetcher>(async (_input, init) => init?.method === 'POST' ? json(pack) : json(pack));

    const result = await saveInterviewPreparation('profile-a', 'job-a', ['story-row-1'], fetcher);

    expect(result).toEqual({status: 'success', value: pack});
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0][0]).toBe('/profiles/profile-a/jobs/job-a/interview-prep-packs');
    expect(fetcher.mock.calls[0][1]).toMatchObject({
      method: 'POST',
      body: JSON.stringify({selected_story_revision_ids: ['story-row-1']}),
    });
    expect(fetcher.mock.calls[1][0]).toBe('/profiles/profile-a/jobs/job-a/interview-prep-packs/prep-pack-1');
  });

  it('surfaces a stale selected story without retrying or reading back', async () => {
    const fetcher = vi.fn<Fetcher>(async () => json({
      detail: {code: 'INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE'},
    }, 422));

    const result = await saveInterviewPreparation('profile-a', 'job-a', ['stale-story-row'], fetcher);

    expect(result).toMatchObject({
      status: 'stale',
      code: 'INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE',
      stage: 'save',
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('returns a transport failure for a rejected request without retrying', async () => {
    const fetcher = vi.fn<Fetcher>(async () => { throw new TypeError('network unavailable'); });

    const result = await loadInterviewPreparation('profile-a', 'job-a', fetcher);

    expect(result).toMatchObject({status: 'transport_error', stage: 'load'});
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('keeps a readback transport failure explicit and performs no hidden retry', async () => {
    const fetcher = vi.fn<Fetcher>(async (_input, init) => {
      if (init?.method === 'POST') return json(pack);
      throw new TypeError('readback unavailable');
    });

    const result = await saveInterviewPreparation('profile-a', 'job-a', ['story-row-1'], fetcher);

    expect(result).toMatchObject({status: 'transport_error', stage: 'readback'});
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('does not claim save success when durable readback differs from the POST response', async () => {
    const changed = {...pack, export_text: 'different persisted payload'};
    const fetcher = vi.fn<Fetcher>(async (_input, init) => init?.method === 'POST' ? json(pack) : json(changed));

    const result = await saveInterviewPreparation('profile-a', 'job-a', ['story-row-1'], fetcher);

    expect(result).toMatchObject({status: 'readback_error', stage: 'readback'});
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('fails closed when the API returns a pack from another profile or job', async () => {
    const fetcher = vi.fn<Fetcher>(async () => json({
      schema_version: 1, profile_id: 'profile-a', job_id: 'job-a',
      packs: [{...pack, job_id: 'job-b'}],
    }));

    const result = await loadInterviewPreparation('profile-a', 'job-a', fetcher);

    expect(result).toMatchObject({status: 'scope_error', stage: 'load'});
  });
});
