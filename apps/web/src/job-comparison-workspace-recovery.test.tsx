import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { createJobComparisonClient } from './job-comparison-client';
import type { JobComparisonRequest } from './job-comparison-workspace';
import {
  JobComparisonWorkspaceView,
  loadJobComparison,
  type JobComparisonResponse,
  type JobComparisonWorkspaceViewProps,
} from './job-comparison-workspace';

type Request = JobComparisonRequest;

const choices = [
  { job_id: 'job-a', title: 'Cloud Administrator', company: 'Example One' },
  { job_id: 'job-b', title: 'Platform Engineer', company: 'Example Two' },
];

const request: Request = {
  profile_id: 'profile-current',
  search_profile_id: 'search-current',
  job_ids: ['job-b', 'job-a'],
};

function responseFor(scope: Request, score: number): JobComparisonResponse {
  return {
    profile_id: scope.profile_id,
    search_profile_id: scope.search_profile_id,
    job_ids: [...scope.job_ids],
    jobs: scope.job_ids.map((job_id, index) => ({
      job_id,
      title: job_id === 'job-a' ? 'Cloud Administrator' : 'Platform Engineer',
      company: job_id === 'job-a' ? 'Example One' : 'Example Two',
      match_score: score - index,
      scoring_contributions: [{
        dimension: 'skills',
        weight: 100,
        raw_score: score - index,
        weighted_contribution: score - index,
        explanation: `Saved score evidence for ${job_id}.`,
        provenance: { source: 'saved_match', evidence_ref: `match:${job_id}:skills` },
      }],
    })),
    shared_requirements: [],
    job_specific_requirements: [],
    unknowns: [],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

function viewProps(
  scope: Request,
  result: Awaited<ReturnType<typeof loadJobComparison>>,
  onRetry: () => void = vi.fn(),
): JobComparisonWorkspaceViewProps {
  return {
    profileId: scope.profile_id,
    searchProfileId: scope.search_profile_id,
    availableJobs: choices,
    selectedJobIds: scope.job_ids,
    state: result.state,
    response: result.response,
    error: result.error,
    onSelectionChange: vi.fn(),
    onCompare: vi.fn(),
    onRetry,
  };
}

function findButton(
  node: React.ReactNode,
  text: string,
): React.ReactElement<{ children?: React.ReactNode; onClick?: () => void }> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child, text);
      if (found) return found;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<{ children?: React.ReactNode; onClick?: () => void }>;
  if (element.type === 'button' && element.props.children === text) return element;
  return findButton(element.props.children, text);
}

const adaptedCompare = (client: ReturnType<typeof createJobComparisonClient>) =>
  (selection: Request) => client.compare(
    selection.profile_id,
    selection.search_profile_id,
    selection.job_ids,
  );

describe('job comparison client-to-view recovery contract', () => {
  it.each([
    ['API rejection', () => Promise.resolve(jsonResponse({ detail: { message: 'private upstream detail' } }, 503))],
    ['request timeout', () => Promise.reject(new DOMException('private timeout detail', 'TimeoutError'))],
  ])('offers a safe retry after %s and recovers without extra writes', async (_label, firstAttempt) => {
    const success = responseFor(request, 86);
    const fetchImpl = vi.fn<typeof fetch>()
      .mockImplementationOnce(firstAttempt)
      .mockResolvedValueOnce(jsonResponse(success));
    const client = createJobComparisonClient(fetchImpl);
    const compare = adaptedCompare(client);
    const failed = await loadJobComparison(compare, request);

    expect(failed.state).toBe('error');
    expect(failed.response).toBeNull();
    expect(failed.error).toBe('Job comparison could not be loaded. Check the selected jobs and retry.');
    const errorMarkup = renderToStaticMarkup(<JobComparisonWorkspaceView {...viewProps(request, failed)} />);
    expect(errorMarkup).toContain('Retry comparison');
    expect(errorMarkup).not.toContain('private upstream detail');
    expect(errorMarkup).not.toContain('private timeout detail');
    expect(errorMarkup).not.toContain('86/100');

    let retryResult: ReturnType<typeof loadJobComparison> | undefined;
    const onRetry = vi.fn(() => { retryResult = loadJobComparison(compare, request); });
    const retryTree = JobComparisonWorkspaceView({ ...viewProps(request, failed, onRetry) });
    const retryButton = findButton(retryTree, 'Retry comparison');
    expect(retryButton?.props.onClick).toBeTypeOf('function');
    retryButton!.props.onClick!();
    expect(onRetry).toHaveBeenCalledExactlyOnceWith();
    const recovered = await retryResult;

    expect(recovered?.state).toBe('ready');
    const recoveredMarkup = renderToStaticMarkup(
      <JobComparisonWorkspaceView {...viewProps(request, recovered!)} />,
    );
    const resultMarkup = recoveredMarkup.slice(recoveredMarkup.indexOf('aria-label="Job comparison results"'));
    expect(resultMarkup.indexOf('Platform Engineer — Example Two'))
      .toBeLessThan(resultMarkup.indexOf('Cloud Administrator — Example One'));
    expect(recoveredMarkup).toContain('match:job-b:skills');
    expect(recoveredMarkup).toContain('Read-only comparison');

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    for (const [url, init] of fetchImpl.mock.calls) {
      expect(url).toBe('/profiles/profile-current/search-profiles/search-current/jobs/compare');
      expect(init?.method).toBe('POST');
      expect(JSON.parse(String(init?.body))).toEqual({ job_ids: ['job-b', 'job-a'] });
    }
  });

  it('discards a late result from the prior profile/search/selection scope', async () => {
    const oldResponse = deferred<Response>();
    const changedScope: Request = {
      profile_id: 'profile-next',
      search_profile_id: 'search-next',
      job_ids: ['job-a', 'job-b'],
    };
    const oldBody = responseFor(request, 91);
    const currentBody = responseFor(changedScope, 72);
    const fetchImpl = vi.fn<typeof fetch>()
      .mockImplementationOnce(() => oldResponse.promise)
      .mockResolvedValueOnce(jsonResponse(currentBody));
    const client = createJobComparisonClient(fetchImpl);
    const compare = adaptedCompare(client);

    const oldPending = loadJobComparison(compare, request);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    const current = await loadJobComparison(compare, changedScope);
    expect(current.state).toBe('ready');

    oldResponse.resolve(jsonResponse(oldBody));
    const lateOld = await oldPending;
    expect(lateOld.state).toBe('ready');

    const currentScopeMarkup = renderToStaticMarkup(
      <JobComparisonWorkspaceView {...viewProps(changedScope, lateOld)} />,
    );
    expect(currentScopeMarkup).toContain('does not match the selected profile, saved search, and jobs');
    expect(currentScopeMarkup).not.toContain('91/100');
    expect(currentScopeMarkup).not.toContain('match:job-b:skills');
    expect(currentScopeMarkup).not.toContain('aria-label="Job comparison results"');

    expect(fetchImpl).toHaveBeenCalledTimes(2);
    expect(fetchImpl.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method,
      body: JSON.parse(String(init?.body)),
    }))).toEqual([
      {
        url: '/profiles/profile-current/search-profiles/search-current/jobs/compare',
        method: 'POST', body: { job_ids: ['job-b', 'job-a'] },
      },
      {
        url: '/profiles/profile-next/search-profiles/search-next/jobs/compare',
        method: 'POST', body: { job_ids: ['job-a', 'job-b'] },
      },
    ]);
  });
});
