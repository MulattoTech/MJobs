// @vitest-environment jsdom
import React, { act, useLayoutEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api } from './api';
import { type FollowUpDraftCandidate } from './application-follow-up-drafts';
import {
  ApplicationFollowUpReadback, type ApplicationFollowUpReadbackProps,
} from './application-follow-up-readback';

vi.mock('./api', () => ({ api: vi.fn() }));

type Reply = { quiet_days: number; candidates: FollowUpDraftCandidate[] };

const candidate: FollowUpDraftCandidate = {
  application_id: 'application-a', job_id: 'job-a', job_title: 'Synthetic Administrator',
  company: 'Synthetic Employer', days_quiet: 20, reported_at: '2026-10-01T12:00:00Z',
  attribution: 'USER_REPORTED', verified: false,
  email_subject: 'Following up on my application',
  email_body: 'Synthetic reviewed follow-up wording for application A.',
  dm_body: 'Synthetic status check for application A.',
  character_limits: { subject: 120, email_body: 500, dm_body: 280 },
};

const application = {
  id: candidate.application_id, profile_id: 'profile-a', job_id: candidate.job_id,
  title: candidate.job_title, company: candidate.company,
  pipeline_status: 'APPLIED', pipeline_attribution: 'USER_REPORTED',
};
const applied = {
  type: 'status', sequence: 1, timestamp: candidate.reported_at,
  status: 'APPLIED', attribution: 'USER_REPORTED',
};

function props(overrides: Partial<ApplicationFollowUpReadbackProps> = {}): ApplicationFollowUpReadbackProps {
  return {
    profileId: 'profile-a', applications: [application], selectedApplication: application,
    tracking: { status: 'APPLIED', attribution: 'USER_REPORTED', history: [applied] },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}

function CommitObservation({ observe }: { observe: () => void }) {
  useLayoutEffect(observe);
  return null;
}

describe('follow-up drafts follow durable application readback', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    vi.mocked(api).mockReset();
    vi.mocked(api).mockResolvedValue({ quiet_days: 7, candidates: [candidate] });
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });

  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  async function render(readback: ApplicationFollowUpReadbackProps, observe?: () => void) {
    await act(async () => {
      root.render(<>
        <ApplicationFollowUpReadback {...readback}/>
        {observe && <CommitObservation observe={observe}/>}
      </>);
    });
  }

  function selector() {
    return container.querySelector('select[aria-label="Quiet days before follow-up"]') as HTMLSelectElement;
  }

  async function chooseQuietDays(days: number) {
    await act(async () => {
      selector().value = String(days);
      selector().dispatchEvent(new Event('change', { bubbles: true }));
    });
  }

  function copyButtons() {
    return Array.from(container.querySelectorAll('button')).filter(button =>
      /^Copy (email|DM)/.test(button.textContent ?? ''));
  }

  it.each(['REJECTED', 'WITHDRAWN', 'INTERVIEW'])(
    'removes an old candidate on the first %s commit while replacement readback is pending',
    async status => {
      await render(props());
      expect(container.textContent).toContain(candidate.email_body);
      expect(copyButtons()).toHaveLength(3);

      const fresh = deferred<Reply>();
      vi.mocked(api).mockImplementation(() => fresh.promise as Promise<never>);
      const commitText: string[] = [];
      await render(props({
        applications: [{ ...application, pipeline_status: status }],
        selectedApplication: { ...application, pipeline_status: status },
        tracking: {
          status, attribution: 'USER_REPORTED',
          history: [applied, { ...applied, sequence: 2, timestamp: '2026-10-21T12:00:00Z', status }],
        },
      }), () => { commitText.push(container.textContent ?? ''); });

      expect(commitText[0]).toContain('Loading follow-up drafts');
      expect(commitText[0]).not.toContain(candidate.email_body);
      expect(copyButtons()).toHaveLength(0);
      expect(api).toHaveBeenCalledTimes(2);

      await act(async () => { fresh.resolve({ quiet_days: 7, candidates: [] }); });
      expect(container.textContent).toContain('No reported applications have been quiet for 7 days or more.');
      expect(copyButtons()).toHaveLength(0);
      expect(vi.mocked(api).mock.calls.every(([, init]) => init === undefined)).toBe(true);
    },
  );

  it('cannot restore an obsolete candidate from a late GET after a durable status change', async () => {
    await render(props());
    const oldRead = deferred<Reply>();
    const fresh = deferred<Reply>();
    vi.mocked(api).mockImplementationOnce(() => oldRead.promise as Promise<never>);
    await chooseQuietDays(14);
    expect(selector().value).toBe('14');

    vi.mocked(api).mockImplementationOnce(() => fresh.promise as Promise<never>);
    await render(props({
      applications: [{ ...application, pipeline_status: 'REJECTED' }],
      tracking: {
        status: 'REJECTED', attribution: 'USER_REPORTED',
        history: [applied, { ...applied, sequence: 2, status: 'REJECTED' }],
      },
    }));
    // The existing child has a local threshold; genuine revision remounts reset
    // it to seven days, while the unchanged-readback test below preserves it.
    expect(selector().value).toBe('7');
    expect(container.textContent).not.toContain(candidate.email_body);
    await act(async () => { fresh.resolve({ quiet_days: 7, candidates: [] }); });
    await act(async () => { oldRead.resolve({ quiet_days: 14, candidates: [candidate] }); });

    expect(container.textContent).not.toContain(candidate.email_body);
    expect(copyButtons()).toHaveLength(0);
    expect(container.textContent).toContain('No reported applications have been quiet for 7 days or more.');
    expect(api).toHaveBeenNthCalledWith(2, '/profiles/profile-a/application-follow-up-drafts?quiet_days=14');
    expect(api).toHaveBeenNthCalledWith(3, '/profiles/profile-a/application-follow-up-drafts?quiet_days=7');
  });

  it('refreshes after a new APPLIED event even when the summary status is unchanged', async () => {
    await render(props());
    const fresh = deferred<Reply>();
    vi.mocked(api).mockImplementation(() => fresh.promise as Promise<never>);
    const firstCommit: string[] = [];
    await render(props({
      tracking: {
        status: 'APPLIED', attribution: 'USER_REPORTED',
        history: [applied, { ...applied, sequence: 2, timestamp: '2026-10-21T12:00:00Z' }],
      },
    }), () => { firstCommit.push(container.textContent ?? ''); });

    expect(firstCommit[0]).not.toContain(candidate.email_body);
    expect(copyButtons()).toHaveLength(0);
    expect(api).toHaveBeenCalledTimes(2);
    await act(async () => { fresh.resolve({ quiet_days: 7, candidates: [] }); });
    expect(container.textContent).toContain('No reported applications have been quiet for 7 days or more.');
  });

  it('includes newly read external application events in the durable revision', async () => {
    await render(props());
    const fresh = deferred<Reply>();
    vi.mocked(api).mockImplementation(() => fresh.promise as Promise<never>);
    await render(props({
      tracking: {
        status: 'APPLIED', attribution: 'USER_REPORTED',
        history: [applied, {
          type: 'external_application', sequence: 2, timestamp: '2026-10-21T12:00:00Z',
          applied_at: '2026-10-20T09:00:00Z', source: 'MANUAL',
        }],
      },
    }));

    expect(api).toHaveBeenCalledTimes(2);
    expect(container.textContent).not.toContain(candidate.email_body);
    await act(async () => { fresh.resolve({ quiet_days: 7, candidates: [] }); });
    expect(copyButtons()).toHaveLength(0);
  });

  it('preserves the chosen threshold across equivalent cloned/reordered and note-only readbacks', async () => {
    const reports = Object.freeze([
      Object.freeze({ sequence: 2, timestamp: '2026-10-03T12:00:00Z', applied_at: '2026-10-01T12:00:00Z', source: 'MANUAL' }),
      Object.freeze({ sequence: 1, timestamp: '2026-10-02T12:00:00Z', applied_at: '2026-09-30T12:00:00Z' }),
    ]);
    const own = Object.freeze({ ...application, external_reports: reports });
    const otherApplication = Object.freeze({ ...application, id: 'application-b', pipeline_status: undefined });
    const applications = Object.freeze([own, otherApplication]);
    const history = Object.freeze([
      Object.freeze({ ...applied, sequence: 4 }),
      Object.freeze({ type: 'external_application', ...reports[0] }),
      Object.freeze({ ...applied, status: 'SAVED' }),
    ]);
    const before = JSON.stringify({ applications, history });
    await render(props({ applications, selectedApplication: own,
      tracking: { status: 'APPLIED', attribution: 'USER_REPORTED', history } }));
    vi.mocked(api).mockResolvedValue({ quiet_days: 14, candidates: [candidate] });
    await chooseQuietDays(14);
    expect(api).toHaveBeenCalledTimes(2);
    expect(selector().value).toBe('14');

    await render(props({
      applications: [
        { ...otherApplication, pipeline_status: null, external_reports: null },
        { ...own, external_reports: reports.map(report => ({ ...report, source: 'source' in report ? report.source : null })).reverse() },
      ],
      selectedApplication: { ...own },
      tracking: {
        status: 'APPLIED', attribution: 'USER_REPORTED',
        history: [...history.map(event => ({ ...event })).reverse(), {
          type: 'note', sequence: 5, timestamp: '2026-10-22T12:00:00Z',
        }],
      },
    }));

    expect(api).toHaveBeenCalledTimes(2);
    expect(selector().value).toBe('14');
    expect(container.textContent).toContain(candidate.email_body);
    expect(JSON.stringify({ applications, history })).toBe(before);
  });

  it('ignores other-profile summaries and selected tracking while preserving the chosen threshold', async () => {
    const foreign = { ...application, id: 'foreign-application', profile_id: 'profile-b' };
    await render(props({ applications: [application, foreign], selectedApplication: foreign }));
    vi.mocked(api).mockResolvedValue({ quiet_days: 14, candidates: [candidate] });
    await chooseQuietDays(14);
    await render(props({
      applications: [{ ...foreign, title: 'Changed private title', pipeline_status: 'WITHDRAWN' }, { ...application }],
      selectedApplication: { ...foreign },
      tracking: {
        status: 'WITHDRAWN', attribution: 'EXTERNAL_USER_REPORTED',
        history: [{ ...applied, sequence: 99, status: 'WITHDRAWN' }],
      },
    }));

    expect(api).toHaveBeenCalledTimes(2);
    expect(selector().value).toBe('14');
    expect(container.textContent).toContain(candidate.email_body);
    expect(container.textContent).not.toContain('Changed private title');
    expect(vi.mocked(api).mock.calls.every(([path]) => path.startsWith('/profiles/profile-a/'))).toBe(true);
  });
});
