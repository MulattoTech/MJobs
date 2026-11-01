// @vitest-environment jsdom
import React, { act, type ReactElement, type ReactNode } from 'react';
import type { Root } from 'react-dom/client';
import { fireEvent, getByRole, queryByRole } from '@testing-library/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Capture only the entrypoint mount; render the real App and its real children.
const entrypoint = vi.hoisted(() => ({ element: undefined as ReactElement<{children: ReactNode}> | undefined }));
vi.mock('react-dom/client', async original => ({
  ...await original<typeof import('react-dom/client')>(),
  createRoot: () => ({ render(element: ReactElement<{children: ReactNode}>) { entrypoint.element = element; } }),
}));

const profile = { id: 'fixture', name: 'Synthetic profile', email: 'fixture@example.invalid',
  version: 'v1', facts: [], answers: [], contact: {}, preferences: {} };
const application = { id: 'app-a', profile_id: 'fixture', job_id: '', title: 'Synthetic role',
  company: 'Synthetic company', state: 'REVIEW_REQUIRED', target: 'UNKNOWN',
  fact_ids: [], trace: [], document_version: 1, profile_version: 'v1', answer_bank: [] };
const exportDocument = { id: 'answers-a', profile_id: 'fixture', application_id: 'app-a',
  kind: 'application-answer-export', version: 1, profile_version: 'v1', fact_ids: [],
  files: { txt: 'owned-answer-export.txt' }, sha256: {}, template: 'answer-export',
  created_at: null, generation_model: null, facts_used: [], missing_fact_ids: [] };

function fixtureResponse(path: string, populated: boolean): Response {
  const route = new URL(path, 'http://localhost').pathname;
  const bodies: Record<string, unknown> = {
    '/profiles': { profiles: populated ? [profile] : [] },
    '/applications': { applications: populated ? [application] : [] },
    '/sources': { sources: [] }, '/operations': { operations: [] },
    '/company-sources/view': { company_sources: [] }, '/company-discovery/requests': { requests: [] },
    '/search-skills': { skills: [] }, '/settings': { operation_mode: 'PERSONAL' },
    '/jobs': { jobs: [] }, '/search-profiles': { search_profiles: [] }, '/source-candidates': { candidates: [] },
    '/profiles/fixture': profile, '/profiles/fixture/resumes': { resumes: [] },
    '/profiles/fixture/ai-task-settings': { tasks: {}, fallback_capabilities: {} },
    '/profiles/fixture/ai-cost-plan/evaluations': { runs: [] },
    '/profiles/fixture/applications/app-a': application,
    '/profiles/fixture/applications/app-a/documents': { documents: [exportDocument] },
    '/profiles/fixture/applications/app-a/answers': { answers: [] },
    '/profiles/fixture/applications/app-a/tracking': { status: null, history: [], notes: [], external_reports: [] },
    '/profiles/fixture/applications/app-a/attempt-history': {
      profile_id: 'fixture', application_id: 'app-a', attempts: [], has_more: false },
    '/profiles/fixture/applications/app-a/email-status': { items: [] },
    '/readiness': { status: 'ready', checks: { api: true } },
  };
  return Object.hasOwn(bodies, route) ? Response.json(bodies[route])
    : Response.json({ detail: 'Synthetic optional data unavailable.' }, { status: 404 });
}

function stalled(phase: 'fetch' | 'body') {
  let resolve!: (response: Response) => void;
  let writer!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({ start(controller) { writer = controller; } }), {
    headers: { 'Content-Type': 'text/plain' },
  });
  return {
    response,
    promise: phase === 'fetch' ? new Promise<Response>(done => { resolve = done; }) : Promise.resolve(response),
    finish(text: string) {
      if (phase === 'fetch') resolve(new Response(text));
      else { writer.enqueue(new TextEncoder().encode(text)); writer.close(); }
    },
  };
}

let container: HTMLDivElement;
let root: Root;
beforeEach(async () => {
  vi.useFakeTimers();
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
async function mount(populated = false) {
  if (populated) localStorage.setItem('mjobs-profile', 'fixture');
  await import('./main');
  await act(async () => root.render(entrypoint.element!.props.children));
}
async function advance(ms: number) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }
async function click(name: string) {
  await act(async () => { fireEvent.click(getByRole(container, 'button', { name })); });
}

describe.each(['fetch', 'body'] as const)('workspace readback stalled during %s', phase => {
  it('releases application loading when optional saved-answer text cannot finish', async () => {
    const pending = stalled(phase);
    const fetcher = vi.fn<typeof fetch>((url) => String(url).endsWith('/documents/answers-a/txt')
      ? pending.promise : Promise.resolve(fixtureResponse(String(url), true)));
    vi.stubGlobal('fetch', fetcher);
    await mount(true);
    await click('Applications 1');
    await click('Synthetic company Synthetic role');
    expect(getByRole(container, 'status', { name: 'Application loading' }).textContent).toContain('Loading the selected application');
    await advance(30_000);
    expect(queryByRole(container, 'status', { name: 'Application loading' })).toBeNull();
    const review = getByRole(container, 'region', { name: 'Application review' });
    expect(review.textContent).toContain('UNKNOWN — the saved answer export could not be read back.');
    expect(review.textContent).toContain('Synthetic role');
    const read = fetcher.mock.calls.find(([url]) => String(url).endsWith('/documents/answers-a/txt'));
    expect(read?.[1]?.signal?.aborted).toBe(true);
    pending.finish('SYNTHETIC_LATE_PRIVATE_ANSWER');
    await advance(0);
    expect(container.textContent).not.toContain('SYNTHETIC_LATE_PRIVATE_ANSWER');
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  });

  it('turns stalled readiness into unavailable without overlapping or permanently replacing the poll', async () => {
    const pending = stalled(phase);
    let healthy = false;
    let polls = 0;
    const fetcher = vi.fn<typeof fetch>((url) => {
      if (String(url) === '/readiness' && !healthy) {
        polls += 1;
        return polls === 1 ? pending.promise : new Promise<Response>(() => {});
      }
      return Promise.resolve(fixtureResponse(String(url), false));
    });
    vi.stubGlobal('fetch', fetcher);
    await mount();
    expect(container.textContent).toContain('Services checking');
    await advance(15_000);
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/readiness')).toHaveLength(1);
    await advance(15_000);
    expect(container.textContent).toContain('Services unavailable');
    healthy = true;
    pending.finish(JSON.stringify({ status: 'ready', checks: { api: true } }));
    await advance(0);
    // The late aborted response is never shown as fresh health evidence.
    expect(container.textContent).toContain('Services unavailable');
    await advance(30_000);
    expect(container.textContent).toContain('Services ready');
    expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(0);
  });
});

it('retains non-success JSON readiness details and reads saved answers as plain text', async () => {
  const saved = 'Question: Why this role?\nAnswer: <b>Literal reviewed answer</b>';
  vi.stubGlobal('fetch', vi.fn<typeof fetch>((url) => {
    if (String(url) === '/readiness') return Promise.resolve(Response.json({
      status: 'degraded', checks: { api: true, redis: false },
    }, { status: 503 }));
    if (String(url).endsWith('/documents/answers-a/txt')) return Promise.resolve(new Response(saved, {
      headers: { 'Content-Type': 'text/plain; charset=utf-8' },
    }));
    return Promise.resolve(fixtureResponse(String(url), true));
  }));
  await mount(true);
  expect(container.textContent).toContain('Services degraded');
  await click('Applications 1');
  await click('Synthetic company Synthetic role');
  const text = getByRole(container, 'region', { name: 'Application review' }).querySelector('pre');
  expect(text?.textContent).toBe(saved);
  expect(text?.querySelector('b')).toBeNull();
});
