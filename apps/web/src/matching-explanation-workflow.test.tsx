import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
  type MatchingExplanation,
  type MatchingExplanationClient,
} from './matching-explanation-client';
import { MatchingExplanationPanel } from './matching-explanation';
import {
  createMatchingExplanationScopeKey,
  MatchingExplanationWorkflow,
  reduceMatchingExplanationWorkflow,
  type MatchingExplanationWorkflowState,
} from './matching-explanation-workflow';

const hooks = vi.hoisted(() => ({
  useReducer: vi.fn(),
  useEffect: vi.fn(),
}));

vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>();
  return {
    ...actual,
    useReducer: hooks.useReducer,
    useEffect: hooks.useEffect,
    default: {
      ...actual,
      useReducer: hooks.useReducer,
      useEffect: hooks.useEffect,
    },
  };
});

const scope = { profileId: 'profile-1', searchProfileId: 'search-cloud', jobId: 'job-1' };
const scopeKey = createMatchingExplanationScopeKey(
  scope.profileId, scope.searchProfileId, scope.jobId,
)!;
const response: MatchingExplanation = {
  profile_id: scope.profileId,
  search_profile_id: scope.searchProfileId,
  job_id: scope.jobId,
  score: 82.5,
  resolution: 'CURRENT',
  profile_resolution: {
    requested_role_family: 'Cloud Operations',
    selected_role_family: 'Cloud Operations',
    profile_id: 'scoring-profile-1',
    profile_version: 'version-4',
    selection: 'ROLE_FAMILY',
  },
  scoring_profile: {
    id: 'scoring-profile-1', version: 'version-4', role_family: 'Cloud Operations',
  },
  scoring_contributions: [
    {
      dimension: 'skills', weight: 70, raw_score: 90, weighted_contribution: 63,
      explanation: 'Approved skills match the job evidence.',
      provenance: { source: 'approved_profile_skills', evidence_ref: 'fact:skill-17' },
      status: 'KNOWN',
    },
    {
      dimension: 'ai_intensity', weight: 30, raw_score: null, weighted_contribution: null,
      explanation: 'AI intensity is UNKNOWN because the source supplied no signal.',
      provenance: { source: 'UNKNOWN' },
      status: 'UNKNOWN',
    },
  ],
  state: 'UNKNOWN',
};

type Reducer = (state: MatchingExplanationWorkflowState, event: unknown) => MatchingExplanationWorkflowState;
let workflowState: MatchingExplanationWorkflowState | undefined;
let effectIndex: number;
let priorDependencies: unknown[][];
let pendingEffects: Array<() => void | (() => void)>;
let cleanups: Array<(() => void) | undefined>;

function setupHooks(seed?: MatchingExplanationWorkflowState): void {
  workflowState = seed;
  effectIndex = 0;
  priorDependencies = [];
  pendingEffects = [];
  cleanups = [];
  hooks.useReducer.mockImplementation((nextReducer: Reducer, initial: MatchingExplanationWorkflowState) => {
    workflowState ??= initial;
    return [workflowState, (event: unknown) => {
      workflowState = nextReducer(workflowState!, event);
    }];
  });
  hooks.useEffect.mockImplementation((effect: () => void | (() => void), dependencies: unknown[] = []) => {
    const index = effectIndex++;
    const previous = priorDependencies[index];
    const changed = !previous || previous.length !== dependencies.length ||
      dependencies.some((value, dependencyIndex) => !Object.is(value, previous[dependencyIndex]));
    if (changed) {
      cleanups[index]?.();
      priorDependencies[index] = [...dependencies];
      pendingEffects.push(() => {
        const cleanup = effect();
        cleanups[index] = typeof cleanup === 'function' ? cleanup : undefined;
        return cleanups[index];
      });
    }
  });
}

function beginRender(): void {
  effectIndex = 0;
}

function runEffect(): (() => void) | undefined {
  const effect = pendingEffects.shift();
  return effect?.() || undefined;
}

async function flushPromises(): Promise<void> {
  for (let index = 0; index < 6; index += 1) await Promise.resolve();
}

function renderWorkflow(
  client: MatchingExplanationClient,
  selected: { profileId: string | null; searchProfileId: string | null; jobId: string | null } = scope,
): string {
  beginRender();
  const tree = MatchingExplanationWorkflow({ ...selected, client });
  return renderToStaticMarkup(tree);
}

function workflowTree(client: MatchingExplanationClient): React.ReactElement<any> {
  beginRender();
  return MatchingExplanationWorkflow({ ...scope, client });
}

function findPanelElement(node: unknown): React.ReactElement<any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = findPanelElement(child);
      if (match) return match;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<any>;
  if (element.type === MatchingExplanationPanel) return element;
  return findPanelElement(element.props.children);
}

function findButton(node: unknown, label: string): React.ReactElement<any> | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const match = findButton(child, label);
      if (match) return match;
    }
    return null;
  }
  if (!React.isValidElement(node)) return null;
  const element = node as React.ReactElement<any>;
  if (element.type === 'button' && element.props.children === label) return element;
  return findButton(element.props.children, label);
}

function selectedState(explanation: MatchingExplanation): MatchingExplanationWorkflowState {
  return {
    scopeKey,
    state: 'ready',
    explanation,
    error: null,
    recovery: 'NONE',
    attempt: 0,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  setupHooks();
});

describe('MatchingExplanationWorkflow', () => {
  it('fetches only the exact selected profile, saved search, and job tuple', async () => {
    const client = { load: vi.fn(async () => response) } satisfies MatchingExplanationClient;
    renderWorkflow(client);

    expect(client.load).not.toHaveBeenCalled();
    runEffect();
    await flushPromises();

    expect(client.load).toHaveBeenCalledExactlyOnceWith(
      scope.profileId, scope.searchProfileId, scope.jobId,
    );
    expect(workflowState).toMatchObject({
      scopeKey, state: 'ready', explanation: response, recovery: 'NONE',
    });
  });

  it('clears the previous scope immediately and ignores a late result from it', () => {
    const oldState = selectedState(response);
    const changed = { profileId: 'profile-2', searchProfileId: 'search-2', jobId: 'job-2' };
    const nextKey = createMatchingExplanationScopeKey(
      changed.profileId, changed.searchProfileId, changed.jobId,
    )!;

    const selected = reduceMatchingExplanationWorkflow(oldState, {
      type: 'SELECT', scopeKey: nextKey,
    });
    expect(selected).toMatchObject({
      scopeKey: nextKey, state: 'loading', explanation: null, error: null, recovery: 'NONE',
    });
    const lateResult = reduceMatchingExplanationWorkflow(selected, {
      type: 'LOADED', scopeKey, explanation: response,
    });
    expect(lateResult).toEqual(selected);

    setupHooks(oldState);
    const client = { load: vi.fn(async () => response) } satisfies MatchingExplanationClient;
    const markup = renderWorkflow(client, changed);
    expect(markup).not.toContain('82.5');
    expect(markup).not.toContain('fact:skill-17');
    runEffect();
    expect(client.load).toHaveBeenCalledExactlyOnceWith(
      changed.profileId, changed.searchProfileId, changed.jobId,
    );
  });

  it('clears an absent selection and does not fetch without all scope identifiers', () => {
    const empty = reduceMatchingExplanationWorkflow(selectedState(response), {
      type: 'SELECT', scopeKey: null,
    });
    expect(empty).toMatchObject({
      scopeKey: null, state: 'empty', explanation: null, error: null, recovery: 'NONE',
    });
    expect(createMatchingExplanationScopeKey(scope.profileId, null, scope.jobId)).toBeNull();

    const client = { load: vi.fn(async () => response) } satisfies MatchingExplanationClient;
    const markup = renderWorkflow(client, { ...scope, searchProfileId: null });
    expect(markup).toContain('Select a profile, saved search, and job');
    runEffect();
    expect(client.load).not.toHaveBeenCalled();
  });

  it('recovers from a retryable client failure only after the user retries', async () => {
    const transient = Object.assign(new Error('private transport detail'), {
      kind: 'HTTP_ERROR', status: 503, retryable: true,
    });
    const client = {
      load: vi.fn()
        .mockRejectedValueOnce(transient)
        .mockResolvedValueOnce(response),
    } satisfies MatchingExplanationClient;

    renderWorkflow(client);
    runEffect();
    await flushPromises();
    expect(workflowState).toMatchObject({ state: 'error', recovery: 'RETRY', explanation: null });

    const errorMarkup = renderWorkflow(client);
    expect(errorMarkup).toContain('Retry explanation');
    expect(errorMarkup).not.toContain('private transport detail');
    const panel = findPanelElement(workflowTree(client));
    expect(panel?.props.onRetry).toBeTypeOf('function');
    panel?.props.onRetry();
    expect(workflowState).toMatchObject({ state: 'loading', recovery: 'NONE', explanation: null, attempt: 1 });

    renderWorkflow(client);
    runEffect();
    await flushPromises();
    expect(client.load).toHaveBeenCalledTimes(2);
    expect(client.load).toHaveBeenNthCalledWith(2, scope.profileId, scope.searchProfileId, scope.jobId);
    expect(workflowState).toMatchObject({ state: 'ready', explanation: response });
  });

  it('maps a stale 409 to explicit refresh guidance without a retry action', async () => {
    const stale = Object.assign(new Error('private stale response detail'), {
      kind: 'STALE_CONTEXT', status: 409, recovery: 'REFRESH', retryable: false,
    });
    const client = {
      load: vi.fn().mockRejectedValue(stale),
    } satisfies MatchingExplanationClient;

    renderWorkflow(client);
    runEffect();
    await flushPromises();
    expect(workflowState).toMatchObject({
      state: 'error', recovery: 'REFRESH', explanation: null,
    });

    const markup = renderWorkflow(client);
    expect(markup).toContain('Refresh explanation');
    expect(markup).not.toContain('Retry explanation');
    expect(markup).not.toContain('private stale response detail');
    const tree = workflowTree(client);
    expect(findPanelElement(tree)?.props.onRetry).toBeUndefined();
    const refresh = findButton(tree, 'Refresh explanation');
    expect(refresh).not.toBeNull();
    refresh?.props.onClick();
    expect(workflowState).toMatchObject({ state: 'loading', recovery: 'NONE', attempt: 1 });
  });

  it('renders UNKNOWN and provenance through the existing explanation panel', () => {
    setupHooks(selectedState(response));
    const client = { load: vi.fn(async () => response) } satisfies MatchingExplanationClient;
    const tree = workflowTree(client);
    expect(findPanelElement(tree)?.type).toBe(MatchingExplanationPanel);

    const markup = renderToStaticMarkup(tree);
    expect(markup).toContain('aria-label="Scoring signal contributions"');
    expect(markup).toContain('AI intensity is UNKNOWN');
    expect(markup).toContain('approved_profile_skills');
    expect(markup).toContain('fact:skill-17');
    expect(markup).toContain('<td>UNKNOWN</td><td>UNKNOWN</td>');
    expect(markup).not.toMatch(/\b(Submit application|Generate|Apply now)\b/);
  });
});
