import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  RoleLensSelectionController,
  RoleLensSelectionLoader,
  roleLensSelectionPath,
  type RoleLensSelectionApiResponse,
} from './role-lens-selection';

const catalog = [
  {id: 'support', version: 1, display_name: 'Support', description: 'Prioritize approved user-support evidence.'},
  {id: 'systems', version: 1, display_name: 'Systems', description: 'Prioritize approved systems evidence.'},
];

function response(profileId: string, jobId: string, lensId: string, reason: 'ROLE_MATCH' | 'GENERAL_FALLBACK' | 'EXPLICIT'): RoleLensSelectionApiResponse {
  const lens = catalog.find(item => item.id === lensId)!;
  return {
    schema_version: 1,
    profile_id: profileId,
    profile_version: `${profileId}-v1`,
    job_id: jobId,
    job_fingerprint: `${jobId}-fingerprint`,
    role_lenses: catalog,
    selection: {
      schema_version: 1,
      profile_id: profileId,
      profile_version: `${profileId}-v1`,
      role_text: 'Systems Administrator',
      lens,
      selection_reason: reason,
      matched_role_terms: [],
      fact_ids: [],
      emphasized_fact_ids: [],
      story_ids: [],
      emphasized_story_ids: [],
      source_refs: {facts: [], stories: []},
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

describe('role lens selection loader', () => {
  it('loads the job-scoped catalog and suggestion without reporting a selected lens', async () => {
    const calls: string[] = [];
    const api = async <T,>(path: string): Promise<T> => {
      calls.push(path);
      return response('profile-1', 'job-1', 'systems', 'ROLE_MATCH') as T;
    };
    const onSelectionChange = vi.fn();
    const controller = new RoleLensSelectionController(api, onSelectionChange);

    const loading = controller.setContext('profile-1', 'job-1');
    expect(controller.getSnapshot().loading).toBe(true);
    await loading;

    expect(calls).toEqual(['/profiles/profile-1/jobs/job-1/role-lenses/selection']);
    expect(controller.getSnapshot().suggestedResponse?.selection.lens.id).toBe('systems');
    expect(controller.getSnapshot().selectedResponse).toBeNull();
    expect(controller.getSnapshot().requestedLens).toBeNull();
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
  });

  it('loads explicit readback with both version parameters and reports only the matching choice', async () => {
    const calls: string[] = [];
    const api = async <T,>(path: string): Promise<T> => {
      calls.push(path);
      const selected = path.includes('?');
      return response('profile-1', 'job-1', selected ? 'systems' : 'support', selected ? 'EXPLICIT' : 'ROLE_MATCH') as T;
    };
    const onSelectionChange = vi.fn();
    const controller = new RoleLensSelectionController(api, onSelectionChange);
    await controller.setContext('profile-1', 'job-1');

    await controller.select({id: 'systems', version: 1});

    expect(calls[1]).toBe('/profiles/profile-1/jobs/job-1/role-lenses/selection?role_lens_id=systems&expected_role_lens_version=1');
    expect(controller.getSnapshot().selectedResponse?.selection.selection_reason).toBe('EXPLICIT');
    expect(onSelectionChange).toHaveBeenLastCalledWith({id: 'systems', version: 1});
  });

  it('builds encoded request paths and never sends only half of the version pair', () => {
    expect(roleLensSelectionPath('profile / 1', 'job?2')).toBe('/profiles/profile%20%2F%201/jobs/job%3F2/role-lenses/selection');
    expect(roleLensSelectionPath('profile-1', 'job-1', {id: 'systems', version: 2}))
      .toBe('/profiles/profile-1/jobs/job-1/role-lenses/selection?role_lens_id=systems&expected_role_lens_version=2');
  });

  it('exposes retry for an initial load failure and a selection readback failure', async () => {
    let calls = 0;
    const api = async <T,>(path: string): Promise<T> => {
      calls += 1;
      if (calls === 1 || path.includes('?')) throw new Error('Role lens service unavailable.');
      return response('profile-1', 'job-1', 'support', 'ROLE_MATCH') as T;
    };
    const controller = new RoleLensSelectionController(api, () => {});
    await controller.setContext('profile-1', 'job-1');
    expect(controller.getSnapshot().loadingError).toBe('Role lens service unavailable.');

    await controller.retry();
    expect(controller.getSnapshot().suggestedResponse?.profile_id).toBe('profile-1');
    await controller.select({id: 'systems', version: 1});
    expect(controller.getSnapshot().selectionError).toBe('Role lens service unavailable.');

    await controller.retrySelection();
    expect(calls).toBe(4);
    expect(controller.getSnapshot().selectedResponse).toBeNull();
  });

  it('clears selection on profile/job changes and ignores an older pending readback', async () => {
    const pendingSelection = deferred<RoleLensSelectionApiResponse>();
    const calls: string[] = [];
    const api = async <T,>(path: string): Promise<T> => {
      calls.push(path);
      if (path.includes('?')) return pendingSelection.promise as Promise<T>;
      const profileId = path.includes('/profile-2/') ? 'profile-2' : 'profile-1';
      const jobId = path.includes('/job-2/') ? 'job-2' : 'job-1';
      return response(profileId, jobId, 'support', 'ROLE_MATCH') as T;
    };
    const onSelectionChange = vi.fn();
    const controller = new RoleLensSelectionController(api, onSelectionChange);
    await controller.setContext('profile-1', 'job-1');
    const staleReadback = controller.select({id: 'systems', version: 1});
    expect(controller.getSnapshot().selecting).toBe(true);

    await controller.setContext('profile-2', 'job-2');
    pendingSelection.resolve(response('profile-1', 'job-1', 'systems', 'EXPLICIT'));
    await staleReadback;

    expect(controller.getSnapshot().suggestedResponse?.profile_id).toBe('profile-2');
    expect(controller.getSnapshot().suggestedResponse?.job_id).toBe('job-2');
    expect(controller.getSnapshot().selectedResponse).toBeNull();
    expect(controller.getSnapshot().requestedLens).toBeNull();
    expect(onSelectionChange).toHaveBeenLastCalledWith(null);
    expect(calls).toContain('/profiles/profile-2/jobs/job-2/role-lenses/selection');
  });

  it('ignores a late catalog response from the previous profile/job context', async () => {
    const pendingInitial = deferred<RoleLensSelectionApiResponse>();
    const api = async <T,>(path: string): Promise<T> => {
      if (path === '/profiles/profile-1/jobs/job-1/role-lenses/selection') return pendingInitial.promise as Promise<T>;
      return response('profile-2', 'job-2', 'support', 'ROLE_MATCH') as T;
    };
    const controller = new RoleLensSelectionController(api, () => {});
    const oldContext = controller.setContext('profile-1', 'job-1');
    await controller.setContext('profile-2', 'job-2');
    pendingInitial.resolve(response('profile-1', 'job-1', 'systems', 'ROLE_MATCH'));
    await oldContext;

    expect(controller.getSnapshot().suggestedResponse?.profile_id).toBe('profile-2');
    expect(controller.getSnapshot().suggestedResponse?.job_id).toBe('job-2');
  });

  it('renders the existing picker in its loading state until the profile/job catalog is read', () => {
    const api = async <T,>(): Promise<T> => { throw new Error('SSR must not fetch.'); };
    const html = renderToStaticMarkup(React.createElement(RoleLensSelectionLoader, {
      api,
      profileId: 'profile-1',
      jobId: 'job-1',
      onSelectionChange: () => {},
    }));
    expect(html).toContain('Loading supported role lenses');
    expect(html).not.toContain('Selected lens:');
  });
});
