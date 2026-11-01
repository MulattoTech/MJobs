// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { fireEvent, getByLabelText, getByRole, queryByRole } from '@testing-library/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApplicationAttemptHistory } from './application-attempt-history';
import { EmailStatusReview } from './email-status-review';
import { ResumeComparison } from './resume-comparison';
import type { GeneratedDocument } from './document-history';

const phases = ['fetch', 'body'] as const;
const privateDetail = 'SYNTHETIC_PRIVATE_EMAIL_SERVER_DETAIL';
const preview = { preview_id: 'reviewed-preview', content_sha256: 'a'.repeat(64),
  category: 'INTERVIEW_INVITATION', suggested_status: 'INTERVIEW',
  reason_codes: ['EXPLICIT_INTERVIEW_INVITATION'], expected_tracking_sequence: 4,
  attribution: 'USER_REPORTED_EMAIL' };

function stalled(phase: typeof phases[number]) {
  let headers!: (value: Response) => void;
  let writer!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({
    start(controller) { writer = controller; },
  }));
  return {
    response,
    promise: phase === 'fetch' ? new Promise<Response>(resolve => { headers = resolve; }) : Promise.resolve(response),
    finish(value: unknown) {
      if (phase === 'fetch') headers(Response.json(value));
      else { writer.enqueue(new TextEncoder().encode(JSON.stringify(value))); writer.close(); }
    },
  };
}

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
async function render(value: React.ReactNode) { await act(async () => root.render(value)); }
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function click(name: string | RegExp) {
  await act(async () => { fireEvent.click(getByRole(container, 'button', { name })); });
}
async function reviewEmail() {
  await act(async () => { fireEvent.change(getByLabelText(container, 'Paste email text'), {
    target: { value: 'Synthetic invitation to interview.' },
  }); });
  await click('Preview message');
  await act(async () => { fireEvent.click(getByLabelText(container,
    'I reviewed this email and want to record the suggested status.')); });
}

describe.each(phases)('mounted request deadline during %s', phase => {
  it('releases attempt-history loading and ignores a late response', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetcher);
    await render(<ApplicationAttemptHistory profileId="profile-a" applicationId="app-a"/>);
    expect(getByRole(container, 'button', { name: 'Refresh attempt history' }).hasAttribute('disabled')).toBe(true);
    await advance(29_999);
    expect(queryByRole(container, 'alert')).toBeNull();
    await advance(1);
    expect(getByRole(container, 'alert').textContent).toBe('Attempt history could not be loaded. No application action was performed.');
    expect(queryByRole(container, 'status')).toBeNull();
    expect(getByRole(container, 'button', { name: 'Refresh attempt history' }).hasAttribute('disabled')).toBe(false);
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    pending.finish({ profile_id: 'profile-a', application_id: 'app-a', attempts: [], has_more: false });
    await advance(0);
    expect(container.textContent).not.toContain('No browser attempts have been recorded');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancels attempt-history readback when scope changes without publishing old records', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(pending.promise).mockResolvedValue(
      Response.json({ profile_id: 'profile-b', application_id: 'app-b', attempts: [], has_more: false }));
    vi.stubGlobal('fetch', fetcher);
    await render(<ApplicationAttemptHistory profileId="profile-a" applicationId="app-a"/>);
    await render(<ApplicationAttemptHistory profileId="profile-b" applicationId="app-b"/>);
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    pending.finish({ profile_id: 'profile-a', application_id: 'app-a', attempts: [{ attempt_id: privateDetail }] });
    await advance(30_000);
    expect(container.textContent).toContain('No browser attempts have been recorded');
    expect(container.textContent).not.toContain(privateDetail);
    expect(queryByRole(container, 'alert')).toBeNull();
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('releases email history loading using only the fixed privacy-safe error', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetcher);
    await render(<EmailStatusReview profileId="profile-a" applicationId="app-a"/>);
    await advance(30_000);
    expect(getByRole(container, 'alert').textContent).toBe('Reported status history could not be loaded. Refresh this view to try again.');
    expect(getByRole(container, 'button', { name: 'Refresh history' }).hasAttribute('disabled')).toBe(false);
    pending.finish({ detail: privateDetail });
    await advance(0);
    expect(container.textContent).not.toContain(privateDetail);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('releases a stalled email preview without retrying or echoing response content', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>((url) => String(url).endsWith('/preview')
      ? pending.promise : Promise.resolve(Response.json({ items: [] })));
    vi.stubGlobal('fetch', fetcher);
    await render(<EmailStatusReview profileId="profile-a" applicationId="app-a"/>);
    await act(async () => { fireEvent.change(getByLabelText(container, 'Paste email text'), {
      target: { value: 'Synthetic email draft.' },
    }); });
    await click('Preview message');
    expect(getByRole(container, 'button', { name: 'Working…' }).hasAttribute('disabled')).toBe(true);
    await advance(30_000);
    expect(getByRole(container, 'button', { name: 'Preview message' }).hasAttribute('disabled')).toBe(false);
    expect(getByRole(container, 'alert').textContent).toBe('Email status could not be processed. Check the message and try again.');
    pending.finish({ detail: privateDetail });
    await advance(0);
    expect(container.textContent).not.toContain(privateDetail);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('releases email confirmation and reuses its request identity only after explicit retry', async () => {
    const pending = stalled(phase);
    let applications = 0;
    const fetcher = vi.fn<typeof fetch>((url) => {
      if (String(url).endsWith('/preview')) return Promise.resolve(Response.json(preview));
      if (String(url).endsWith('/apply')) {
        applications += 1;
        return applications === 1 ? pending.promise : Promise.resolve(Response.json({
          preview_id: preview.preview_id, applied: true, status: 'INTERVIEW', attribution: 'USER_REPORTED_EMAIL',
        }));
      }
      return Promise.resolve(Response.json({ items: [] }));
    });
    vi.stubGlobal('fetch', fetcher);
    await render(<EmailStatusReview profileId="profile-a" applicationId="app-a"/>);
    await reviewEmail();
    await click('Confirm reported status');
    await advance(30_000);
    expect(getByRole(container, 'alert').textContent).toBe('Status update could not be confirmed. Retry to safely check the same reviewed request.');
    expect(applications).toBe(1);
    expect(getByRole(container, 'button', { name: 'Confirm reported status' }).hasAttribute('disabled')).toBe(false);
    pending.finish({ detail: privateDetail });
    await advance(0);
    await click('Confirm reported status');
    const writes = fetcher.mock.calls.filter(([url]) => String(url).endsWith('/apply'));
    expect(writes).toHaveLength(2);
    expect(JSON.parse(String(writes[0][1]?.body))).toEqual(JSON.parse(String(writes[1][1]?.body)));
    expect(container.textContent).toContain('Reported status saved.');
    expect(container.textContent).not.toContain(privateDetail);
  });

  it('drops pending email readback and private drafts when the application changes', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>((url) => String(url).endsWith('/preview')
      ? pending.promise : Promise.resolve(Response.json({ items: [] })));
    vi.stubGlobal('fetch', fetcher);
    await render(<EmailStatusReview profileId="profile-a" applicationId="app-a"/>);
    await act(async () => { fireEvent.change(getByLabelText(container, 'Paste email text'), {
      target: { value: privateDetail },
    }); });
    await click('Preview message');
    await render(<EmailStatusReview profileId="profile-b" applicationId="app-b"/>);
    const old = fetcher.mock.calls.find(([url]) => String(url).endsWith('/preview'));
    expect(old?.[1]?.signal?.aborted).toBe(true);
    pending.finish(preview);
    await advance(30_000);
    expect((getByLabelText(container, 'Paste email text') as HTMLTextAreaElement).value).toBe('');
    expect(container.innerHTML).not.toContain(privateDetail);
    expect(queryByRole(container, 'region', { name: 'Email status preview' })).toBeNull();
    expect(queryByRole(container, 'alert')).toBeNull();
  });

  it('releases comparison controls when saved-operation readback stalls without repeating admission', async () => {
    const pending = stalled(phase);
    let admitted = false;
    const fetcher = vi.fn<typeof fetch>((url, init) => {
      if (String(url).endsWith('/resumes')) return Promise.resolve(Response.json({
        resumes: [{ revision_id: 'original', version: 1, label: 'Original', filename: 'resume.txt' }],
      }));
      if (init?.method === 'POST') {
        admitted = true;
        return Promise.resolve(Response.json({ operation_id: 'comparison-1', state: 'QUEUED', current_step: null }));
      }
      return admitted ? pending.promise : Promise.resolve(new Response('not JSON', { status: 404 }));
    });
    vi.stubGlobal('fetch', fetcher);
    const target: GeneratedDocument = { id: 'tailored', profile_id: 'profile-a', application_id: 'app-a',
      kind: 'resume', version: 2, profile_version: 'version', fact_ids: [], files: {}, sha256: {},
      template: 'approved-skill-order-headings-v1', generation_model: 'deterministic-skill-order-v2',
      generation_context: { mode: 'APPROVED_SKILL_ORDER' },
      created_at: null, facts_used: [], missing_fact_ids: [] };
    await render(<ResumeComparison documents={[target]}/>);
    expect(queryByRole(container, 'alert')).toBeNull(); // A 404 is absence, not a parse failure.
    await click('Compare selected resumes');
    await advance(500);
    await advance(30_000);
    expect(getByRole(container, 'button', { name: 'Compare selected resumes' }).hasAttribute('disabled')).toBe(false);
    expect(getByRole(container, 'button', { name: 'Recover saved comparison' }).hasAttribute('disabled')).toBe(false);
    expect(getByRole(container, 'alert').textContent).toContain('Check the saved status before trying again');
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
    pending.finish({ operation_id: 'comparison-1', state: 'COMPLETED', result: { scope: privateDetail } });
    await advance(0);
    expect(container.textContent).not.toContain(privateDetail);
  });
});

it('preserves email conflict status/code while suppressing untrusted server details', async () => {
  vi.stubGlobal('fetch', vi.fn<typeof fetch>((url) => {
    if (String(url).endsWith('/preview')) return Promise.resolve(Response.json(preview));
    if (String(url).endsWith('/apply')) return Promise.resolve(Response.json({
      detail: { code: 'EMAIL_STATUS_STALE_PREVIEW', message: privateDetail },
    }, { status: 409 }));
    return Promise.resolve(Response.json({ items: [] }));
  }));
  await render(<EmailStatusReview profileId="profile-a" applicationId="app-a"/>);
  await reviewEmail();
  await click('Confirm reported status');
  expect(getByRole(container, 'alert').textContent).toBe('Application status changed since this preview. Refresh history and review the email again.');
  expect(container.textContent).not.toContain(privateDetail);
  expect(queryByRole(container, 'button', { name: 'Confirm reported status' })).toBeNull();
});
