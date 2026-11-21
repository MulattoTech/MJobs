// @vitest-environment jsdom
import React, { act, type ReactElement, type ReactNode } from 'react';
import type { Root } from 'react-dom/client';
import { fireEvent, getByRole } from '@testing-library/dom';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

const entrypoint = vi.hoisted(() => ({ element: undefined as ReactElement<{children: ReactNode}> | undefined }));
vi.mock('react-dom/client', async original => ({
  ...await original<typeof import('react-dom/client')>(),
  createRoot: () => ({ render(element: ReactElement<{children: ReactNode}>) { entrypoint.element = element; } }),
}));

const profile = { id: 'fixture', name: 'Synthetic profile', email: 'fixture@example.invalid',
  version: 1, facts: [], answers: [], contact: {}, preferences: {} };
const application = { id: 'app-a', profile_id: 'fixture', job_id: '', title: 'Synthetic role',
  company: 'Synthetic company', state: 'CONFIRMED', target: 'MOCK', fact_ids: [], trace: [],
  document_version: 1, profile_version: 1, answer_bank: [] };
const applicationB = { ...application, id: 'app-b', title: 'Other role', company: 'Other company' };
type TrackingState = { status: string | null; attribution: string | null; notes: unknown[];
  history: { type: string; status?: string; attribution?: string; sequence: number; timestamp: string }[];
  external_reports: unknown[] };
const trackingBefore: TrackingState = { status: null, attribution: null, notes: [], history: [], external_reports: [] };
const trackingAfter: TrackingState = { status: 'READY', attribution: 'USER_REPORTED', notes: [],
  history: [{ type: 'status', status: 'READY', attribution: 'USER_REPORTED', sequence: 1,
    timestamp: '2026-11-06T19:00:00Z' }], external_reports: [] };
const noDocumentText = { documents: [] };

function json(body: unknown, status = 200): Response { return Response.json(body, { status }); }
let container: HTMLDivElement;
let root: Root;
let activeFetch: typeof fetch;
const dispatchFetch: typeof fetch = (input, init) => activeFetch(input, init);

beforeAll(async () => {
  vi.stubGlobal('fetch', dispatchFetch);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  await import('./main');
}, 30_000);

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.stubGlobal('fetch', dispatchFetch);
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false, addEventListener() {}, removeEventListener() {} })));
  localStorage.clear();
  localStorage.setItem('mjobs-profile', 'fixture');
  container = document.createElement('div');
  document.body.append(container);
  const actual = await vi.importActual<typeof import('react-dom/client')>('react-dom/client');
  root = actual.createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function response(path: string, method: string, applications = [application]): Response {
  const route = new URL(path, 'http://localhost').pathname;
  if (route === '/profiles') return json({ profiles: [profile] });
  if (route === '/applications') return json({ applications });
  if (route === '/sources') return json({ sources: [] });
  if (route === '/operations') return json({ operations: [] });
  if (route === '/company-sources/view') return json({ company_sources: [] });
  if (route === '/company-discovery/requests') return json({ requests: [] });
  if (route === '/search-profiles') return json({ search_profiles: [] });
  if (route === '/source-candidates') return json({ candidates: [] });
  if (route === '/jobs') return json({ jobs: [] });
  if (route === '/search-skills') return json({ skills: [] });
  if (route === '/settings') return json({ operation_mode: 'PERSONAL' });
  if (route === '/readiness') return json({ status: 'ready', checks: { api: true } });
  if (route === '/profiles/fixture') return json(profile);
  if (route === '/profiles/fixture/resumes') return json({ resumes: [] });
  if (route === '/profiles/fixture/ai-task-settings') return json({ tasks: {}, fallback_capabilities: {} });
  if (route === '/profiles/fixture/ai-cost-policy') return json({ policy: {} });
  if (route === '/profiles/fixture/ai-cost-plan/evaluations') return json({ runs: [] });
  if (route === '/profiles/fixture/applications/app-a') return json(application);
  if (route === '/profiles/fixture/applications/app-a/documents') return json(noDocumentText);
  if (route === '/profiles/fixture/applications/app-a/answers') return json({ answers: [] });
  if (route === '/profiles/fixture/applications/app-a/tracking/status' && method === 'PUT') return json(trackingAfter);
  if (route === '/profiles/fixture/applications/app-a/tracking') return json(trackingBefore);
  if (route === '/profiles/fixture/applications/app-a/attempt-history') {
    return json({ profile_id: 'fixture', application_id: 'app-a', attempts: [], has_more: false });
  }
  if (route === '/profiles/fixture/applications/app-a/email-status') return json({ items: [] });
  return json({ detail: 'Not found' }, 404);
}

async function mount() {
  await act(async () => root.render(entrypoint.element!.props.children));
}

async function click(name: string) {
  await act(async () => { fireEvent.click(getByRole(container, 'button', { name })); });
}

describe('application tracking write readback', () => {
  it('keeps a committed status visible if a same-application selection read overlaps the write', async () => {
    let releaseWrite!: (value: Response) => void;
    let pending = false;
    let persistedTracking = trackingBefore;
    activeFetch = async (input, init) => {
      const path = String(input);
      const method = init?.method || 'GET';
      if (pending && method === 'PUT' && path.includes('/tracking/status')) {
        return await new Promise<Response>(resolve => {
          releaseWrite = value => { persistedTracking = trackingAfter; resolve(value); };
        });
      }
      if (path.includes('/tracking/status') && method === 'PUT') persistedTracking = trackingAfter;
      if (path.endsWith('/tracking')) return json(persistedTracking);
      return response(path, method);
    };

    await mount();
    await click('Applications 1');
    await click('Synthetic company Synthetic role');
    const tracking = getByRole(container, 'region', { name: 'Application tracking' });
    fireEvent.change(getByRole(tracking, 'combobox', { name: 'Manual application status' }), {
      target: { value: 'READY' },
    });

    pending = true;
    await act(async () => { fireEvent.click(getByRole(tracking, 'button', { name: 'Update status' })); });
    await click('Synthetic company Synthetic role');
    pending = false;
    await act(async () => { releaseWrite(json(trackingAfter)); await new Promise(resolve => setTimeout(resolve, 0)); });

    expect((getByRole(tracking, 'combobox', { name: 'Manual application status' }) as HTMLSelectElement).value).toBe('READY');
    expect(getByRole(tracking, 'list', { name: 'Application history' }).textContent).toContain('Ready');
  });

  it('does not apply a pending status response after the user selects a different application', async () => {
    let releaseWrite!: (value: Response) => void;
    let pending = false;
    activeFetch = async (input, init) => {
      const path = String(input);
      const method = init?.method || 'GET';
      if (pending && method === 'PUT' && path.includes('/app-a/tracking/status')) {
        return await new Promise<Response>(resolve => { releaseWrite = resolve; });
      }
      const route = new URL(path, 'http://localhost').pathname;
      if (route === '/applications') return response(path, method, [application, applicationB]);
      if (route.includes('/app-b/tracking')) return json(trackingBefore);
      if (route.endsWith('/tracking')) return json(trackingBefore);
      if (route === '/profiles/fixture/applications/app-b') return json(applicationB);
      if (route === '/profiles/fixture/applications/app-b/documents') return json(noDocumentText);
      if (route === '/profiles/fixture/applications/app-b/answers') return json({ answers: [] });
      return response(path, method);
    };

    await mount();
    await click('Applications 2');
    await click('Synthetic company Synthetic role');
    const trackingA = getByRole(container, 'region', { name: 'Application tracking' });
    fireEvent.change(getByRole(trackingA, 'combobox', { name: 'Manual application status' }), {
      target: { value: 'READY' },
    });
    pending = true;
    await act(async () => { fireEvent.click(getByRole(trackingA, 'button', { name: 'Update status' })); });
    await click('Other company Other role');
    pending = false;
    await act(async () => { releaseWrite(json(trackingAfter)); await new Promise(resolve => setTimeout(resolve, 0)); });

    const trackingB = getByRole(container, 'region', { name: 'Application tracking' });
    expect((getByRole(trackingB, 'combobox', { name: 'Manual application status' }) as HTMLSelectElement).value).toBe('');
    expect(getByRole(trackingB, 'list', { name: 'Application history' }).textContent).toBe('');
  });

  it('reuses the write request id when the PUT commits but the readback fails', async () => {
    const requestIds: string[] = [];
    const recorded = new Set<string>();
    let persistedTracking = trackingBefore;
    let failNextTrackingRead = false;
    activeFetch = async (input, init) => {
      const path = String(input);
      const method = init?.method || 'GET';
      if (path.endsWith('/tracking/status') && method === 'PUT') {
        const requestId = (JSON.parse(String(init?.body)) as {request_id:string}).request_id;
        requestIds.push(requestId);
        if (!recorded.has(requestId)) {
          recorded.add(requestId);
          const sequence = recorded.size;
          persistedTracking = {...trackingAfter, history: Array.from({length: sequence}, (_, index) => ({
            ...trackingAfter.history[0], sequence: index + 1,
          }))};
        }
        failNextTrackingRead = requestIds.length === 1;
        return json(persistedTracking);
      }
      if (path.endsWith('/tracking')) {
        if (failNextTrackingRead) {
          failNextTrackingRead = false;
          return json({detail:'Temporary readback failure.'}, 503);
        }
        return json(persistedTracking);
      }
      return response(path, method);
    };

    await mount();
    await click('Applications 1');
    await click('Synthetic company Synthetic role');
    const tracking = getByRole(container, 'region', { name: 'Application tracking' });
    fireEvent.change(getByRole(tracking, 'combobox', { name: 'Manual application status' }), {
      target: { value: 'READY' },
    });
    await click('Update status');
    expect(requestIds).toHaveLength(1);
    expect((getByRole(tracking, 'button', { name: 'Update status' }) as HTMLButtonElement).disabled).toBe(false);

    await click('Update status');
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
    expect(persistedTracking.history).toHaveLength(1);
    expect(getByRole(tracking, 'list', { name: 'Application history' }).textContent).toContain('Ready');
  });
});
