// @vitest-environment jsdom
import React, { act, type ReactElement, type ReactNode } from 'react';
import type { Root } from 'react-dom/client';
import { fireEvent, getByLabelText, getByRole, getAllByRole, queryByRole, within } from '@testing-library/dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApplicationReviewQueue, type ReviewQueueApplication } from './application-review-queue';

// Capture the entrypoint only; workspace cases mount the real App and children.
const entrypoint = vi.hoisted(() => ({ element: undefined as ReactElement<{ children: ReactNode }> | undefined }));
vi.mock('react-dom/client', async original => ({
  ...await original<typeof import('react-dom/client')>(),
  createRoot: () => ({ render(element: ReactElement<{ children: ReactNode }>) { entrypoint.element = element; } }),
}));

type SavedApplication = ReviewQueueApplication & {
  state: string; trace: string[]; job_id: string; target: string;
  fact_ids: string[]; document_version: number; profile_version: string;
};
const pass = { result: 'PASS' as const, reason: null, is_current: true, missing: [] };
const application = (overrides: Partial<SavedApplication> = {}): SavedApplication => ({
  id: 'app-a', profile_id: 'profile-a', title: 'Synthetic role', company: 'Example company',
  state: 'PREPARED', trace: [], job_id: '', target: 'UNKNOWN', fact_ids: [],
  document_version: 1, profile_version: 'v1', question_preflight: pass, ...overrides,
});
const states = [
  { state: 'AUTH_REQUIRED', trace: ['AUTH_REQUIRED'], label: 'Login required', next: 'Sign in yourself' },
  { state: 'USER_INTERVENTION_REQUIRED', trace: ['CHALLENGE_DETECTED', 'CAPTCHA_REQUIRED'],
    label: 'CAPTCHA needs your attention', next: 'Complete the CAPTCHA yourself' },
  { state: 'SUBMISSION_UNCONFIRMED', trace: ['SUBMITTING', 'PRIVATE_RECEIPT_DETAIL'],
    label: 'Submission result is unconfirmed', next: 'Check the provider’s receipt or application history before another attempt' },
];

let container: HTMLDivElement;
let root: Root;
let activeFetch: typeof fetch = async () => { throw new Error('Test transport not configured.'); };
const dispatchFetch: typeof fetch = (input, init) => activeFetch(input, init);
beforeAll(async () => {
  vi.stubGlobal('fetch', dispatchFetch);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  await import('./main');
}, 30_000);
beforeEach(async () => {
  vi.useFakeTimers();
  activeFetch = vi.fn(async () => { throw new Error('Unexpected transport from a component-only case.'); });
  vi.stubGlobal('fetch', dispatchFetch);
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  localStorage.clear();
  container = document.createElement('div');
  document.body.append(container);
  const actual = await vi.importActual<typeof import('react-dom/client')>('react-dom/client');
  root = actual.createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
async function render(node: ReactNode) { await act(async () => root.render(node)); }
async function click(element: HTMLElement) { await act(async () => fireEvent.click(element)); }
const queue = () => getByRole(container, 'region', { name: 'Application review queue' });
const queueNavigation = () => getByRole(getByRole(container, 'navigation', { name: 'Main navigation' }), 'button', { name: /^Review Queue/ });

describe('persisted application blockers', () => {
  it.each(states)('explains $state and only navigates when its review button is clicked', async ({ state, trace, label, next }) => {
    const saved = application({ state, trace });
    const before = JSON.stringify(saved);
    const onReview = vi.fn();
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[saved]} onReview={onReview} />);

    expect(queue().textContent).toContain(label);
    expect(queue().textContent).toContain(next);
    expect(queue().textContent).toContain('1 known blocker');
    expect(queue().textContent).toContain('External submission remains OFF');
    expect(queue().textContent).not.toContain('PRIVATE_RECEIPT_DETAIL');
    expect(onReview).not.toHaveBeenCalled();
    const buttons = getAllByRole(queue(), 'button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].getAttribute('type')).toBe('button');
    await click(buttons[0]);
    expect(onReview).toHaveBeenCalledExactlyOnceWith('app-a');
    expect(activeFetch).not.toHaveBeenCalled();
    expect(JSON.stringify(saved)).toBe(before);
  });

  it.each([
    ['CAPTCHA_REQUIRED', 'CHALLENGE_DETECTED'],
    ['CAPTCHA_REQUIRED', 'PRIVATE_CHALLENGE_DETAIL'],
    ['CAPTCHA_REQUIRED:PRIVATE_CHALLENGE_DETAIL'],
    [],
  ])('uses a generic current intervention label unless the last marker is exactly CAPTCHA_REQUIRED (%j)', async (...trace) => {
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      application({ state: 'USER_INTERVENTION_REQUIRED', trace }),
    ]} onReview={vi.fn()} />);
    const item = getByRole(queue(), 'heading', { name: 'Example company — Synthetic role' }).closest('li')!;
    expect(item.textContent).toContain('Application needs your attention');
    expect(item.textContent).toContain('Inspect the current challenge');
    expect(item.textContent).not.toContain('CAPTCHA');
    expect(item.textContent).not.toContain('PRIVATE_CHALLENGE_DETAIL');
  });

  it.each(['CONFIRMED', 'COMPLETED', 'CANCELLED', 'EXTERNALLY_APPLIED'])('does not turn %s history or missing preflight into a current blocker', async state => {
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      application({ state, question_preflight: null, trace: ['AUTH_REQUIRED', 'CAPTCHA_REQUIRED'] }),
    ]} onReview={vi.fn()} />);
    expect(queue().textContent).toContain('0 known blockers');
    expect(queryByRole(queue(), 'button')).toBeNull();
    expect(queue().textContent).not.toContain('Example company');
    expect(queue().textContent).not.toContain('Question preflight: UNKNOWN');
  });

  describe.each(['CONFIRMED', 'CANCELLED', 'EXTERNALLY_APPLIED'])('%s with retained question preflight', state => {
    it.each([
      { result: 'FAIL' as const, is_current: true },
      { result: 'FAIL' as const, is_current: undefined },
      { result: 'UNKNOWN' as const, is_current: true },
      { result: 'UNKNOWN' as const, is_current: undefined },
    ])('does not reopen a blocker for $result when is_current is $is_current', async ({ result, is_current }) => {
      await render(<ApplicationReviewQueue profileId="profile-a" applications={[
        application({ state, trace: ['AUTH_REQUIRED', 'CAPTCHA_REQUIRED'], question_preflight: {
          result, reason: 'OLD_PREFLIGHT_BEFORE_APPLICATION_FINISHED', missing: [],
          ...(is_current === undefined ? {} : { is_current }),
        } }),
      ]} onReview={vi.fn()} />);
      expect(queue().textContent).toContain('0 known blockers');
      expect(queryByRole(queue(), 'button')).toBeNull();
      expect(queue().textContent).not.toContain('Example company');
      expect(queue().textContent).not.toContain('OLD_PREFLIGHT_BEFORE_APPLICATION_FINISHED');
    });
  });

  it.each(['FAILED', 'BLOCKED', 'REVIEW_REQUIRED', undefined])('retains an actionable failed preflight for active or legacy state %s', async state => {
    const onReview = vi.fn();
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      { ...application(), state, question_preflight: {
        result: 'FAIL', reason: 'Current answer still needs review.', is_current: true, missing: [],
      } },
    ]} onReview={onReview} />);
    expect(queue().textContent).toContain('1 known blocker');
    expect(queue().textContent).toContain('Current answer still needs review.');
    await click(getByRole(queue(), 'button', { name: /Review application/ }));
    expect(onReview).toHaveBeenCalledExactlyOnceWith('app-a');
    expect(activeFetch).not.toHaveBeenCalled();
  });

  it('keeps current preflight reasons, excludes stale and foreign rows, and counts each application once', async () => {
    const failed = { result: 'FAIL' as const, reason: 'UNRESOLVED_REQUIRED_QUESTIONS', is_current: true,
      missing: [{ question_id: 'q1', question: 'Describe your lab work.', answer_ids: ['lab-answer'] }] };
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      application({ id: 'both', state: 'AUTH_REQUIRED', question_preflight: failed }),
      application({ id: 'question-only', title: 'Question-only role', question_preflight: failed }),
      application({ id: 'unknown', title: 'Unknown metadata role', question_preflight: { result: 'UNKNOWN', reason: 'Metadata not supported.', missing: [] } }),
      application({ id: 'stale', title: 'Stale-only role', question_preflight: { ...failed, is_current: false } }),
      application({ id: 'other', profile_id: 'profile-b', title: 'PRIVATE_OTHER_PROFILE', state: 'AUTH_REQUIRED', question_preflight: failed }),
      application({ id: 'historical-challenge', title: 'Resolved challenge role', trace: ['CAPTCHA_REQUIRED', 'DRY_RUN_COMPLETE'], state: 'DRY_RUN_COMPLETE' }),
    ]} onReview={vi.fn()} />);
    expect(queue().textContent).toContain('3 known blockers');
    expect(getAllByRole(queue(), 'button')).toHaveLength(3);
    expect(queue().textContent).toContain('Login required');
    expect(queue().textContent).toContain('Persisted preflight is missing answers: lab-answer.');
    expect(queue().textContent).toContain('Metadata not supported.');
    expect(queue().textContent).not.toContain('Stale-only role');
    expect(queue().textContent).not.toContain('PRIVATE_OTHER_PROFILE');
    expect(queue().textContent).not.toContain('Resolved challenge role');
  });

  it.each([
    { target: 'UNKNOWN', state: 'PREPARED' },
    { target: 'UNKNOWN', state: 'REVIEW_REQUIRED' },
    { target: 'UNKNOWN', state: 'DRY_RUN_COMPLETE' },
    { target: 'REAL', state: 'PREPARED' },
    { target: 'REAL', state: 'REVIEW_REQUIRED' },
    { target: 'REAL', state: 'DRY_RUN_COMPLETE' },
  ])('describes unavailable preflight for $target / $state without promising a local dry run', async ({ target, state }) => {
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      application({ target, state, question_preflight: null }),
    ]} onReview={vi.fn()} />);
    expect(queue().textContent).toContain('Question preflight unavailable');
    expect(queue().textContent).toContain('No supported question preflight result is saved');
    expect(queue().textContent).not.toContain('Question preflight: UNKNOWN');
    expect(queue().textContent).not.toContain('Question preflight: FAIL');
    expect(queue().textContent).not.toMatch(/\brun a local dry run\b/i);
    expect(queue().textContent).toContain('available question checks and manual review options');
  });

  it('retains incomplete-read warnings while a loaded login blocker stays actionable', async () => {
    await render(<ApplicationReviewQueue profileId="profile-a" applications={[
      application({ state: 'AUTH_REQUIRED' }),
    ]} onReview={vi.fn()} loadError />);
    expect(getByRole(queue(), 'alert').textContent).toContain('Some saved application details could not be read');
    expect(queue().textContent).toContain('1 known blocker');
    expect(getByRole(queue(), 'button', { name: /Review application/ })).toBeTruthy();
    await render(<ApplicationReviewQueue profileId="" applications={[application({ state: 'AUTH_REQUIRED' })]} onReview={vi.fn()} />);
    expect(queue().textContent).toContain('Select a profile');
    expect(queryByRole(queue(), 'button')).toBeNull();
  });
});

const profiles = ['profile-a', 'profile-b'].map(id => ({ id, name: `Synthetic ${id}`,
  email: `${id}@example.invalid`, version: 'v1', facts: [], answers: [], contact: {}, preferences: {} }));
function fixtureResponse(input: RequestInfo | URL, summaries: SavedApplication[], details: Record<string, SavedApplication>): Response {
  const url = new URL(String(input), 'http://localhost');
  const bodies: Record<string, unknown> = {
    '/profiles': { profiles }, '/applications': { applications: summaries },
    '/sources': { sources: [] }, '/operations': { operations: [] },
    '/company-sources/view': { company_sources: [] }, '/company-discovery/requests': { requests: [] },
    '/search-skills': { skills: [] }, '/settings': { operation_mode: 'PERSONAL' },
    '/jobs': { jobs: [] }, '/search-profiles': { search_profiles: [] }, '/source-candidates': { candidates: [] },
    '/readiness': { status: 'ready', checks: { api: true } },
  };
  for (const profile of profiles) {
    const path = `/profiles/${profile.id}`;
    bodies[path] = profile;
    bodies[`${path}/resumes`] = { resumes: [] };
    bodies[`${path}/ai-task-settings`] = { tasks: {}, fallback_capabilities: {} };
    bodies[`${path}/ai-cost-plan/evaluations`] = { runs: [] };
  }
  for (const [path, detail] of Object.entries(details)) {
    bodies[path] = detail;
    bodies[`${path}/documents`] = { documents: [] };
    bodies[`${path}/answers`] = { answers: [] };
    bodies[`${path}/tracking`] = { status: null, history: [], notes: [], external_reports: [] };
    bodies[`${path}/attempt-history`] = { profile_id: detail.profile_id, application_id: detail.id, attempts: [], has_more: false };
    bodies[`${path}/email-status`] = { items: [] };
  }
  return Object.hasOwn(bodies, url.pathname) ? Response.json(bodies[url.pathname])
    : Response.json({ detail: 'Synthetic optional data unavailable.' }, { status: 404 });
}
const detailPath = (saved: SavedApplication) => `/profiles/${saved.profile_id}/applications/${saved.id}`;
async function mountWorkspace() {
  localStorage.setItem('mjobs-profile', 'profile-a');
  await render(entrypoint.element!.props.children);
  await click(queueNavigation());
}

describe('workspace review queue reads and navigation', () => {
  it('uses fresh detail state and trace even when summaries include preflight, with the same sidebar and list count', async () => {
    const summaries = [
      application({ id: 'login', title: 'Login role' }),
      application({ id: 'captcha', title: 'CAPTCHA role', trace: ['PRIVATE_OLD_TRACE'] }),
      application({ id: 'uncertain', title: 'Uncertain role' }),
      application({ id: 'resolved', title: 'Resolved role', state: 'AUTH_REQUIRED' }),
      application({ id: 'cancelled', title: 'Cancelled role', state: 'USER_INTERVENTION_REQUIRED' }),
      application({ id: 'external', title: 'Externally applied role', state: 'PREPARED' }),
      application({ id: 'stale', title: 'Stale role', question_preflight: { result: 'FAIL', reason: 'Old question', is_current: false, missing: [] } }),
    ];
    const details = Object.fromEntries(summaries.map(saved => [detailPath(saved), { ...saved }]));
    details[detailPath(summaries[0])].state = 'AUTH_REQUIRED';
    details[detailPath(summaries[1])].state = 'USER_INTERVENTION_REQUIRED';
    details[detailPath(summaries[1])].trace = ['CHALLENGE_DETECTED', 'CAPTCHA_REQUIRED'];
    details[detailPath(summaries[2])].state = 'SUBMISSION_UNCONFIRMED';
    details[detailPath(summaries[3])].state = 'CONFIRMED';
    details[detailPath(summaries[3])].question_preflight = { result: 'FAIL', reason: 'Preflight before confirmation', is_current: true, missing: [] };
    details[detailPath(summaries[4])].state = 'CANCELLED';
    details[detailPath(summaries[4])].question_preflight = { result: 'UNKNOWN', reason: 'Preflight before cancellation', missing: [] };
    details[detailPath(summaries[5])].state = 'EXTERNALLY_APPLIED';
    details[detailPath(summaries[5])].question_preflight = { result: 'FAIL', reason: 'Preflight before external report', missing: [] };
    const fetcher = vi.fn<typeof fetch>(async input => fixtureResponse(input, summaries, details));
    activeFetch = fetcher;
    await mountWorkspace();

    expect(queueNavigation().textContent).toBe('Review Queue3');
    expect(queue().textContent).toContain('3 known blockers');
    expect(getAllByRole(queue(), 'button')).toHaveLength(3);
    for (const label of states.map(item => item.label)) expect(queue().textContent).toContain(label);
    expect(queue().textContent).not.toContain('Resolved role');
    expect(queue().textContent).not.toContain('Cancelled role');
    expect(queue().textContent).not.toContain('Externally applied role');
    expect(queue().textContent).not.toContain('Stale role');
    expect(queue().textContent).not.toContain('PRIVATE_OLD_TRACE');
    for (const saved of summaries) expect(fetcher.mock.calls.some(([url]) => String(url) === detailPath(saved))).toBe(true);
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
  });

  it('excludes mismatched profile, mismatched application ID, and failed detail reads from both counts', async () => {
    const summaries = ['good', 'wrong-id', 'wrong-profile', 'failed'].map(id => application({ id, title: `${id} role`, question_preflight: undefined }));
    const details = {
      [detailPath(summaries[0])]: application({ id: 'good', title: 'good role', state: 'AUTH_REQUIRED' }),
      [detailPath(summaries[1])]: application({ id: 'not-requested', title: 'PRIVATE_WRONG_ID', state: 'AUTH_REQUIRED', question_preflight: { result: 'FAIL', reason: 'PRIVATE_WRONG_ID', missing: [] } }),
      [detailPath(summaries[2])]: application({ id: 'wrong-profile', profile_id: 'profile-b', title: 'PRIVATE_WRONG_PROFILE', state: 'AUTH_REQUIRED' }),
    };
    const fetcher = vi.fn<typeof fetch>(async input => fixtureResponse(input, summaries, details));
    activeFetch = fetcher;
    await mountWorkspace();
    expect(queueNavigation().textContent).toBe('Review Queue1');
    expect(queue().textContent).toContain('1 known blocker');
    expect(getByRole(queue(), 'alert').textContent).toContain('Some saved application details could not be read');
    expect(getAllByRole(queue(), 'button')).toHaveLength(1);
    expect(queue().textContent).not.toMatch(/PRIVATE_WRONG|wrong-id role|wrong-profile role|failed role/);
  });

  it.each(states)('opens the actual $state application review without transport writes', async ({ state, trace }) => {
    const saved = application({ state, trace });
    const before = JSON.stringify(saved);
    const fetcher = vi.fn<typeof fetch>(async input => fixtureResponse(input, [saved], { [detailPath(saved)]: saved }));
    activeFetch = fetcher;
    await mountWorkspace();
    await click(getByRole(queue(), 'button', { name: 'Review application: Synthetic role at Example company' }));
    expect(getByRole(container, 'region', { name: 'Application review' }).textContent).toContain('Synthetic role');
    expect(getByRole(getByRole(container, 'navigation', { name: 'Main navigation' }), 'button', { name: 'Applications 1' }).getAttribute('aria-current')).toBe('page');
    expect(fetcher.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
    expect(JSON.stringify(saved)).toBe(before);
    expect(container.textContent).toContain('External submission OFF');
  });

  it('ignores a late profile-a detail after switching to profile-b', async () => {
    const first = application({ id: 'old', title: 'PRIVATE_LATE_PROFILE_A', state: 'AUTH_REQUIRED', question_preflight: undefined });
    const second = application({ id: 'current', profile_id: 'profile-b', title: 'Current profile role', state: 'AUTH_REQUIRED' });
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>(resolve => { finish = resolve; });
    activeFetch = vi.fn<typeof fetch>(input => String(input) === detailPath(first) ? pending
      : Promise.resolve(fixtureResponse(input, [first, second], { [detailPath(second)]: second })));
    await mountWorkspace();
    expect(getByRole(queue(), 'status').textContent).toContain('Loading saved application');
    await act(async () => fireEvent.change(getByLabelText(container, 'Active profile'), { target: { value: 'profile-b' } }));
    expect(queueNavigation().textContent).toBe('Review Queue1');
    expect(queue().textContent).toContain('Current profile role');
    await act(async () => finish(Response.json(first)));
    expect(queue().textContent).not.toContain('PRIVATE_LATE_PROFILE_A');
    expect(queueNavigation().textContent).toBe('Review Queue1');
    expect(within(queue()).getAllByRole('button')).toHaveLength(1);
  });
});
