import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { withRequestDeadline } from './api';
import { matchApplicationAnswers } from './application-answer-matching-client';
import { loadLatestApplicationConfirmation, checkApplicationConfirmation } from './application-confirmation-recovery';
import { createApplicationFormReviewCapabilityClient, createApplicationFormReviewClient } from './application-form-review-connected';
import { createApplicationMultipageClient } from './application-multipage-workspace';
import { createApplicationPriorityClient } from './application-priority-client';
import { createApplicationRehearsalClient } from './application-rehearsal';
import { createApplicationAnswerResumeClient } from './application-review-answers';
import { fetchDataExport } from './data-export';
import { createDynamicApplicationFieldsClient } from './dynamic-application-form-client';
import { loadInterviewPrepPacks, createInterviewPrepPack } from './interview-preparation';
import { loadInterviewPreparation } from './interview-preparation-client';
import { createJobComparisonClient } from './job-comparison-client';
import { createMatchFeedbackClient } from './match-feedback-client';
import { createMatchingExplanationClient } from './matching-explanation-client';
import { postMockRun, lookupMockRun, cancelMockRun } from './mock-run-client';
import { createResumeWorkspaceClient } from './resume-workspace-client';
import { loadRetentionPolicy, saveRetentionPolicy } from './retention-policy';
import { createScoringPreviewClient } from './role-scoring-preview-client';
import { createRoleScoringProfileClient, type RoleScoringProfileDraft } from './role-scoring-profile-client';
import { createSearchHistoryClient } from './search-history-client';
import { createSourceCandidateReviewClient } from './source-candidate-review-client';

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks(); });

const scope = { profileId: 'profile', applicationId: 'application' };
const intent = { version: 1 as const, requestId: 'reviewed-request', submit: false };
const answer = { request_id: 'reviewed-request', question_id: 'question', question_fingerprint: 'q-v1',
  form_fingerprint: 'f-v1', answer_id: 'answer', expected_profile_version: 'p-v1' };
const draft: RoleScoringProfileDraft = { schema_version: 1, role_family: 'engineering', weights: { skills: 100 }, thresholds: {} };
const workspace = { profileId: 'profile', jobId: 'job', templateId: 'classic', profileVersion: 'p-v1',
  jobFingerprint: 'a'.repeat(64), sections: [] };
const pages = [{ id: 'contact', title: 'Contact', fields: [] }];
type Case = { name: string; run: (fetcher: typeof fetch) => Promise<unknown>; timeout?: number;
  sentinel?: boolean; status?: number; headersOnly?: boolean; first?: unknown };
const cases: Case[] = [
  { name: 'answer suggestions', run: f => { const s = { ...scope, question: 'Describe your technical skills.' }; return matchApplicationAnswers(s, () => s, f); } },
  { name: 'saved confirmation', run: f => loadLatestApplicationConfirmation('profile', 'application', f) },
  { name: 'receipt check write', run: f => checkApplicationConfirmation('profile', 'application', 'reviewed-request', f) },
  { name: 'review capability sentinel', run: f => createApplicationFormReviewCapabilityClient('profile', 'application', f).load(), sentinel: true },
  { name: 'saved review sentinel', run: f => createApplicationFormReviewClient('profile', 'application', f).load(), sentinel: true },
  { name: 'multipage load', run: f => createApplicationMultipageClient('profile', 'application', pages, f).load('profile', 'application') },
  { name: 'multipage save', run: f => createApplicationMultipageClient('profile', 'application', pages, f).transition('profile', 'application', { request_id: 'reviewed-request', action: 'SAVE_PROGRESS', expected_version: 1, page_id: 'contact' }) },
  { name: 'application priority', run: f => createApplicationPriorityClient({ fetchImpl: f }).load('profile', 'job') },
  { name: 'rehearsal body', run: f => createApplicationRehearsalClient('profile', 'application', f).load() },
  { name: 'rehearsal acknowledgement', run: f => createApplicationRehearsalClient('profile', 'application', f).recordReview({ request_id: 'reviewed-request', fingerprint: 'a'.repeat(64), acknowledged: true }), headersOnly: true },
  { name: 'answer resume admission', run: f => createApplicationAnswerResumeClient(f).resumeAndValidate('profile', 'application', answer), timeout: 60_000 },
  { name: 'private blob export', run: f => fetchDataExport('profile', 'backup-json', f) },
  { name: 'dynamic fields', run: f => createDynamicApplicationFieldsClient(f).load('profile', 'application') },
  { name: 'interview history', run: f => loadInterviewPrepPacks('profile', 'job', f) },
  { name: 'interview creation', run: f => createInterviewPrepPack('profile', 'job', [], f) },
  { name: 'typed interview client', run: f => loadInterviewPreparation('profile', 'job', f), sentinel: true },
  { name: 'job comparison', run: f => createJobComparisonClient(f).compare('profile', 'search', ['job-1', 'job-2']) },
  { name: 'feedback history', run: f => createMatchFeedbackClient(f).load!('profile', 'job') },
  { name: 'feedback write', run: f => createMatchFeedbackClient(f).submit('profile', 'job', 'GOOD_MATCH', 'reviewed-request') },
  { name: 'preference history', run: f => createMatchFeedbackClient(f).loadPreferences!('profile') },
  { name: 'preference write', run: f => createMatchFeedbackClient(f).applyPreference!('profile', 'feedback', 'salary', 'increase', 'reviewed-request') },
  { name: 'matching explanation', run: f => createMatchingExplanationClient({ fetchImpl: f }).load('profile', 'search', 'job') },
  { name: 'mock run admission', run: f => postMockRun(scope, intent, f) },
  { name: 'mock run lookup', run: f => lookupMockRun(scope, intent, f) },
  { name: 'mock cancel error body', run: f => cancelMockRun('operation', f), status: 409 },
  { name: 'resume workspace load', run: f => createResumeWorkspaceClient({ fetchImpl: f }).load('profile', 'job') },
  { name: 'resume workspace preview', run: f => createResumeWorkspaceClient({ fetchImpl: f }).preview({ ...workspace, templateVersion: 'v1' }) },
  { name: 'resume workspace save', run: f => createResumeWorkspaceClient({ fetchImpl: f }).save({ ...workspace, expectedVersion: null, previewFingerprint: 'b'.repeat(64), reviewed: true, requestId: 'reviewed-request' }) },
  { name: 'retention load', run: f => loadRetentionPolicy(f) },
  { name: 'retention save', run: f => saveRetentionPolicy(f, { expected_revision: 1, expected_config_fingerprint: 'a'.repeat(64), enabled: false, max_age_days_by_rule: {}, confirmed: true }) },
  { name: 'ranking preview', run: f => createScoringPreviewClient(f)({ profileId: 'profile', searchProfileId: 'search', draft, expectedVersion: null }) },
  { name: 'scoring profile load', run: f => createRoleScoringProfileClient({ fetchImpl: f }).load('profile', 'search') },
  { name: 'scoring profile save', run: f => createRoleScoringProfileClient({ fetchImpl: f }).save('profile', 'search', draft, null) },
  { name: 'search history', run: f => createSearchHistoryClient({ fetchImpl: f }).loadHistory('profile') },
  { name: 'candidate list', run: f => createSourceCandidateReviewClient({ fetchImpl: f }).list('profile') },
  { name: 'candidate review', run: f => createSourceCandidateReviewClient({ fetchImpl: f }).review('profile', 'candidate', {
    state: 'NEEDS_REVIEW', site_type: 'UNKNOWN', reason: 'Review', api_feed_evidence_status: 'UNKNOWN',
    api_feed_evidence_reference: null, api_feed_evidence_note: '', access_terms_note: '', capabilities_limits_note: '',
    expected_value_maintenance_note: '', integration_issue: null, request_id: 'reviewed-request',
  }) },
  { name: 'feedback durable readback', run: f => createMatchFeedbackClient(f).submit('profile', 'job', 'GOOD_MATCH', 'reviewed-request'),
    first: { profile_id: 'profile', job_id: 'job', feedback: { id: 'feedback', category: 'GOOD_MATCH', created_at: '2026-11-01T00:00:00Z' }, replayed: false, preference_proposal: null } },
  { name: 'preference durable readback', run: f => createMatchFeedbackClient(f).applyPreference!('profile', 'feedback', 'salary', 'increase', 'reviewed-request'),
    first: { profile_id: 'profile', replayed: false, preference: { id: 'pref', job_id: 'job', company_id: null, feedback_id: 'feedback', category: 'GOOD_MATCH', seniority_level: null, dimension: 'salary', direction: 'INCREASE', created_at: '2026-11-01T00:00:00Z' } } },
];

describe.each(['headers', 'body'] as const)('injected transport stalled at %s', phase => {
  for (const entry of cases.filter(item => phase !== 'body' || !item.headersOnly)) {
    it(entry.name, async () => {
      let finishHeaders!: (response: Response) => void;
      let stream!: ReadableStreamDefaultController<Uint8Array>;
      const response = new Response(new ReadableStream<Uint8Array>({ start(value) { stream = value; } }), { status: entry.status ?? 200 });
      const pending = phase === 'headers' ? new Promise<Response>(resolve => { finishHeaders = resolve; }) : Promise.resolve(response);
      const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending);
      if (entry.first) fetcher.mockResolvedValueOnce(Response.json(entry.first));
      let settled = false;
      const outcome = entry.run(fetcher).then(value => { settled = true; return { rejected: false, value }; },
        error => { settled = true; return { rejected: true, value: error }; });
      await vi.advanceTimersByTimeAsync(0);
      const dispatches = entry.first ? 2 : 1;
      expect(fetcher).toHaveBeenCalledTimes(dispatches);
      await vi.advanceTimersByTimeAsync((entry.timeout ?? 30_000) - 1);
      expect(settled).toBe(false);
      await vi.advanceTimersByTimeAsync(1);
      const result = await outcome;
      expect(settled).toBe(true);
      expect(result.rejected).toBe(!entry.sentinel);
      if (entry.sentinel) expect(result.value).not.toMatchObject({ status: 'success' });
      expect(fetcher.mock.calls.at(-1)?.[1]?.signal?.aborted).toBe(true);
      if (phase === 'headers') finishHeaders(Response.json({}));
      else { stream.enqueue(new TextEncoder().encode('{}')); stream.close(); }
      await vi.advanceTimersByTimeAsync(0);
      expect(fetcher).toHaveBeenCalledTimes(dispatches);
      expect(vi.getTimerCount()).toBe(0);
    });
  }
});

describe('complete operation budgets', () => {
  it('allows nested successful reads without cancelling the parent or retaining timers', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({ ok: true }));
    const read = (request: typeof fetch) => withRequestDeadline(async client => (await client('/fixture')).json(), request);
    await expect(withRequestDeadline(async request => [await read(request), await read(request)], fetcher))
      .resolves.toEqual([{ ok: true }, { ok: true }]);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('honors null overriding an aborted input Request signal', async () => {
    const controller = new AbortController(); controller.abort();
    const input = new Request('https://fixture.invalid/', { signal: controller.signal });
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ok: true }));
    await expect(withRequestDeadline(async request => (await request(input, { signal: null })).json(), fetcher))
      .resolves.toEqual({ ok: true });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each(['headers', 'body'] as const)('cancels during %s even when the transport ignores abort', async phase => {
    const controller = new AbortController();
    const add = vi.spyOn(controller.signal, 'addEventListener');
    const remove = vi.spyOn(controller.signal, 'removeEventListener');
    const response = new Response(new ReadableStream());
    const fetcher = vi.fn<typeof fetch>().mockImplementation(() => phase === 'headers' ? new Promise(() => {}) : Promise.resolve(response));
    const result = withRequestDeadline(async request => (await request('/fixture', { signal: controller.signal })).json(), fetcher)
      .catch(error => error);
    await vi.advanceTimersByTimeAsync(0);
    controller.abort(new DOMException('Cancelled', 'AbortError'));
    expect(await result).toMatchObject({ name: 'AbortError' });
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(add).toHaveBeenCalledTimes(1); expect(remove).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not dispatch a pre-cancelled operation or retain a synchronous callback failure', async () => {
    const controller = new AbortController(); controller.abort();
    const operation = vi.fn(async () => true);
    await expect(withRequestDeadline(operation, fetch, { signal: controller.signal })).rejects.toMatchObject({ name: 'AbortError' });
    expect(operation).not.toHaveBeenCalled();
    await expect(withRequestDeadline(() => { throw new Error('fixture failure'); })).rejects.toThrow('fixture failure');
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops the whole resume polling workflow after 60 seconds with only one admission', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}, { status: 202 }));
    const result = createApplicationAnswerResumeClient(fetcher).resumeAndValidate('profile', 'application', answer).catch(error => error);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(await result).toMatchObject({ code: 'REQUEST_TIMEOUT' });
    const calls = fetcher.mock.calls.length;
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(calls);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels an active resume poll delay immediately without another readback', async () => {
    const controller = new AbortController();
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({}, { status: 202 }));
    const result = createApplicationAnswerResumeClient(fetcher).resumeAndValidate('profile', 'application', answer, controller.signal).catch(error => error);
    await vi.advanceTimersByTimeAsync(0); controller.abort();
    expect(await result).toMatchObject({ name: 'AbortError' });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});
