// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {OperationProgressConnected} from './operation-progress-connected';
import type {OperationProgressRecord} from './operation-progress';

const row = (state: string, id = 'operation-1'): OperationProgressRecord => ({
  operation_id: id, operation_type: 'DISCOVER', entity_id: 'profile-1', state,
});
let container: HTMLDivElement;
let root: Root;
let mounted = false;

async function render(profileId: string | null, request: (path: string) => Promise<{operations: OperationProgressRecord[]}>) {
  await act(async () => root.render(<OperationProgressConnected profileId={profileId} request={request}/>));
}
async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
async function advance(milliseconds: number) { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); }); }
async function clickRefresh() {
  const button = Array.from(container.querySelectorAll('button')).find(item => item.textContent === 'Refresh progress');
  expect(button).toBeDefined();
  await act(async () => button!.click());
}
async function unmount() {
  if (mounted) { await act(async () => root.unmount()); mounted = false; }
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  mounted = true;
});
afterEach(async () => {
  await unmount();
  container.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('connected saved operation progress', () => {
  it('refreshes pending work and stops polling after a terminal result', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({operations: [row('RUNNING')]})
      .mockResolvedValueOnce({operations: [row('COMPLETED')]});
    await render('profile-1', request);
    await flush();
    expect(request).toHaveBeenCalledTimes(1);
    await advance(2000);
    expect(request).toHaveBeenCalledTimes(2);
    await advance(10000);
    expect(request).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain('Completed');
  });

  it('keeps saved cards visible while a background refresh is pending', async () => {
    let finishRefresh!: (result: {operations: OperationProgressRecord[]}) => void;
    const request = vi.fn()
      .mockResolvedValueOnce({operations: [row('RUNNING')]})
      .mockImplementationOnce(() => new Promise(resolve => { finishRefresh = resolve; }));
    await render('profile-1', request);
    await flush();
    expect(container.textContent).toContain('In progress');
    await advance(2000);
    expect(request).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain('In progress');
    expect(container.textContent).not.toContain('Loading operations');
    await act(async () => finishRefresh({operations: [row('COMPLETED')]}));
    expect(container.textContent).toContain('Completed');
  });

  it('keeps only one request in flight when manual refresh is clicked', async () => {
    let finish!: (result: {operations: OperationProgressRecord[]}) => void;
    const request = vi.fn(() => new Promise<{operations: OperationProgressRecord[]}>(resolve => { finish = resolve; }));
    await render('profile-1', request);
    expect(request).toHaveBeenCalledTimes(1);
    await clickRefresh();
    await advance(100);
    expect(request).toHaveBeenCalledTimes(1);
    await act(async () => finish({operations: [row('RUNNING')]}));
    await advance(100);
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('ignores a prior profile response and unmount response', async () => {
    let finishOld!: (result: {operations: OperationProgressRecord[]}) => void;
    let finishUnmounted!: (result: {operations: OperationProgressRecord[]}) => void;
    const request = vi.fn((path: string) => path.includes('profile-1')
      ? new Promise<{operations: OperationProgressRecord[]}>(resolve => { finishOld = resolve; })
      : path.includes('profile-3')
        ? new Promise<{operations: OperationProgressRecord[]}>(resolve => { finishUnmounted = resolve; })
      : Promise.resolve({operations: [row('COMPLETED', 'operation-2')]}));
    await render('profile-1', request);
    await render('profile-2', request);
    await advance(100);
    await flush();
    await act(async () => finishOld({operations: [row('FAILED')]}));
    await advance(100);
    expect(container.textContent).toContain('Completed');
    expect(container.textContent).not.toContain('Failed');
    await render('profile-3', request);
    await advance(100);
    await unmount();
    await act(async () => finishUnmounted({operations: [row('FAILED', 'operation-3')]}));
  });

  it('backs off after failures and keeps raw errors out of the UI', async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error('PRIVATE_PROVIDER_DETAIL'))
      .mockResolvedValueOnce({operations: [row('COMPLETED')]});
    await render('profile-1', request);
    await flush();
    expect(container.textContent).toContain('could not be loaded');
    expect(container.textContent).not.toContain('PRIVATE_PROVIDER_DETAIL');
    await advance(2000);
    expect(request).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain('Completed');
  });
});
