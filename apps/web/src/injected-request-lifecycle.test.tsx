// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { fireEvent, getByLabelText, getByRole, queryByRole } from '@testing-library/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DataExport, type DataExportFetch } from './data-export';
import { RetentionPolicyControls } from './retention-policy';
import { InterviewPreparation } from './interview-preparation';
import { InterviewPreparationConnected } from './interview-preparation-connected';
import { ApplicationFormReviewConnected, ApplicationFormReviewCapabilityConnected } from './application-form-review-connected';
import { ApplicationConfirmationRecovery } from './application-confirmation-recovery';
import { createMatchingExplanationClient } from './matching-explanation-client';
import { MatchingExplanationWorkflow } from './matching-explanation-workflow';

let container: HTMLDivElement;
let root: Root;
beforeEach(() => {
  vi.useFakeTimers(); vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove();
  vi.clearAllTimers(); vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks();
});
async function render(node: React.ReactNode) { await act(async () => root.render(node)); }
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function click(name: string | RegExp) { await act(async () => { fireEvent.click(getByRole(container, 'button', { name })); }); }
function stall(phase: 'headers' | 'body') {
  let headers!: (response: Response) => void;
  let writer!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({ start(controller) { writer = controller; } }));
  return { promise: phase === 'headers' ? new Promise<Response>(resolve => { headers = resolve; }) : Promise.resolve(response),
    finish(value: unknown) { if (phase === 'headers') headers(Response.json(value)); else { writer.enqueue(new TextEncoder().encode(JSON.stringify(value))); writer.close(); } } };
}

describe.each(['headers', 'body'] as const)('mounted recovery during stalled %s', phase => {
  it('releases both form-review loading states through their resolved error contracts', async () => {
    const pending = stall(phase); const capabilityPending = stall(phase);
    const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(pending.promise).mockReturnValueOnce(capabilityPending.promise);
    await render(<ApplicationFormReviewConnected profileId="profile" applicationId="application" fetcher={fetcher}/>);
    expect(container.textContent).toContain('Loading saved form review');
    await advance(30_000);
    expect(container.textContent).not.toContain('Loading saved form review');
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await render(<ApplicationFormReviewCapabilityConnected profileId="profile" applicationId="other" fetcher={fetcher}/>);
    expect(container.textContent).toContain('Checking read-only form review');
    await advance(30_000);
    expect(container.textContent).not.toContain('Checking read-only form review');
    expect(fetcher.mock.calls[1][1]?.signal?.aborted).toBe(true);
    pending.finish({ private: 'SYNTHETIC_PRIVATE_LATE_VALUE' }); await advance(0);
    capabilityPending.finish({ private: 'SYNTHETIC_PRIVATE_LATE_VALUE' }); await advance(0);
    expect(container.textContent).not.toContain('SYNTHETIC_PRIVATE_LATE_VALUE');
  });

  it('releases retention loading and provides an explicit reload without replaying writes', async () => {
    const pending = stall(phase); const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    await render(<RetentionPolicyControls fetcher={fetcher}/>);
    await advance(30_000);
    expect(queryByRole(container, 'status')).toBeNull();
    expect(getByRole(container, 'alert')).toBeTruthy();
    expect(getByRole(container, 'button', { name: 'Reload settings' }).hasAttribute('disabled')).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('cancels career-story transport on unmount without rendering old data', async () => {
    const pending = stall(phase); const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    await render(<InterviewPreparationConnected profileId="profile" jobId="job" jobTitle="Engineer" companyName="Fixture" fetcher={fetcher}/>);
    await render(<p>Another screen</p>);
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    pending.finish({ profile_id: 'profile', stories: [] }); await advance(30_000);
    expect(container.textContent).toBe('Another screen');
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('ends confirmation loading without changing the uncertain submission state', async () => {
    const pending = stall(phase); const fetcher = vi.fn<typeof fetch>().mockReturnValue(pending.promise);
    vi.stubGlobal('fetch', fetcher);
    await render(<ApplicationConfirmationRecovery profileId="profile" applicationId="application" state="SUBMISSION_UNCONFIRMED"/>);
    await advance(30_000);
    expect(container.textContent).not.toContain('Loading saved receipt check');
    expect(getByRole(container, 'alert').textContent).toContain('Submission remains uncertain');
    expect(getByRole(container, 'button', { name: 'Check receipt only' }).hasAttribute('disabled')).toBe(false);
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  });
});

it.each(['scope change', 'unmount'] as const)('prevents a private late export after %s', async change => {
  let resolve!: (blob: Blob) => void;
  const fetchExport = vi.fn<DataExportFetch>().mockReturnValue(new Promise(value => { resolve = value; }));
  const download = vi.fn();
  await render(<DataExport profileId="profile-a" fetchExport={fetchExport} download={download}/>);
  await click('Download complete backup');
  if (change === 'scope change') await render(<DataExport profileId="profile-b" fetchExport={fetchExport} download={download}/>);
  else await render(<p>Another screen</p>);
  expect(fetchExport.mock.calls[0][2]?.aborted).toBe(true);
  resolve(new Blob(['SYNTHETIC_PRIVATE_BACKUP'])); await advance(30_000);
  expect(download).not.toHaveBeenCalled();
  expect(container.textContent).not.toContain('Export downloaded');
  expect(container.textContent).not.toContain('SYNTHETIC_PRIVATE_BACKUP');
  expect(vi.getTimerCount()).toBe(0);
});

it('releases a stalled export and suppresses a late successful download', async () => {
  let resolve!: (blob: Blob) => void;
  const fetchExport = vi.fn<DataExportFetch>().mockReturnValue(new Promise(value => { resolve = value; }));
  const download = vi.fn();
  await render(<DataExport profileId="profile" fetchExport={fetchExport} download={download}/>);
  await click('Download complete backup'); await advance(30_000);
  expect(getByRole(container, 'alert').textContent).toContain('No file was downloaded');
  expect(getByRole(container, 'button', { name: 'Download complete backup' }).hasAttribute('disabled')).toBe(false);
  resolve(new Blob(['SYNTHETIC_PRIVATE_BACKUP'])); await advance(0);
  expect(download).not.toHaveBeenCalled(); expect(fetchExport).toHaveBeenCalledTimes(1);
});

it('cancels retention save on unmount without replaying the PUT or publishing old policy', async () => {
  const policy = { revision: 1, config_fingerprint: 'a'.repeat(64), enabled: false,
    pruning_allowed: false, rules: [], requires_review: false };
  const pending = stall('headers');
  const fetcher = vi.fn<typeof fetch>((_url, init) => init?.method === 'PUT' ? pending.promise : Promise.resolve(Response.json(policy)));
  await render(<RetentionPolicyControls fetcher={fetcher}/>);
  await act(async () => { fireEvent.click(getByLabelText(container, 'I understand cleanup applies only to configured transient data and never to application history.')); });
  await click('Save retention settings');
  await render(<p>Another screen</p>);
  expect(fetcher.mock.calls.find(([, init]) => init?.method === 'PUT')?.[1]?.signal?.aborted).toBe(true);
  pending.finish({ ...policy, revision: 2 }); await advance(30_000);
  expect(container.textContent).toBe('Another screen');
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(1);
});

it('cancels an interview creation when its profile changes and never reads back the old pack', async () => {
  const pending = stall('headers');
  const fetcher = vi.fn<typeof fetch>((_url, init) => init?.method === 'POST' ? pending.promise : Promise.resolve(Response.json({ packs: [] })));
  await render(<InterviewPreparation profileId="profile-a" jobId="job-a" jobTitle="Engineer" companyName="Fixture" fetcher={fetcher}/>);
  await click('Create interview prep pack');
  await render(<InterviewPreparation profileId="profile-b" jobId="job-b" jobTitle="Other" companyName="Fixture" fetcher={fetcher}/>);
  const posted = fetcher.mock.calls.find(([, init]) => init?.method === 'POST');
  expect(posted?.[1]?.signal?.aborted).toBe(true);
  pending.finish({ pack_id: 'old-private-pack' }); await advance(30_000);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes('old-private-pack'))).toBe(false);
  expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  expect(container.textContent).toContain('job-b');
  expect(container.textContent).not.toContain('old-private-pack');
});

it('offers explicit retry after a match-explanation timeout and then shows the saved result', async () => {
  const pending = stall('headers');
  const fetcher = vi.fn<typeof fetch>().mockReturnValueOnce(pending.promise).mockImplementation(async () => Response.json({
    profile_id: 'profile', search_profile_id: 'search', job_id: 'job', score: 50, resolution: 'LEGACY_DEFAULT',
    profile_resolution: { requested_role_family: null, selected_role_family: null, profile_id: null, profile_version: null, selection: 'LEGACY_DEFAULT' },
    scoring_profile: null, scoring_contributions: [{ dimension: 'skills', weight: 100, raw_score: 50,
      weighted_contribution: 50, explanation: 'Verified skills evidence', provenance: { source: 'legacy_match' } }],
  }));
  const client = createMatchingExplanationClient({ fetchImpl: fetcher });
  await render(<MatchingExplanationWorkflow profileId="profile" searchProfileId="search" jobId="job" client={client} onRefresh={async () => {}}/>);
  await advance(30_000);
  expect(fetcher).toHaveBeenCalledTimes(1);
  await click(/Retry/);
  expect(fetcher).toHaveBeenCalledTimes(2);
  expect(container.textContent).toContain('Verified skills evidence');
});
