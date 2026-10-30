import React, {Children, isValidElement, type ReactElement, type ReactNode} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';

const hooks = vi.hoisted(() => ({
  states: [] as unknown[],
  refs: [] as {current: unknown}[],
  effects: [] as (() => void | (() => void))[],
  stateCursor: 0,
  refCursor: 0,
  effectCursor: 0,
  effectDependencies: [] as (readonly unknown[] | undefined)[],
}));

vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>();
  return {
    ...actual,
    useState: <T,>(initial: T | (() => T)) => {
      const index = hooks.stateCursor++;
      if (!(index in hooks.states)) hooks.states[index] = typeof initial === 'function'
        ? (initial as () => T)()
        : initial;
      const setState = (next: T | ((current: T) => T)) => {
        const current = hooks.states[index] as T;
        hooks.states[index] = typeof next === 'function'
          ? (next as (value: T) => T)(current)
          : next;
      };
      return [hooks.states[index] as T, setState] as const;
    },
    useRef: <T,>(initial: T) => {
      const index = hooks.refCursor++;
      if (!(index in hooks.refs)) hooks.refs[index] = {current: initial};
      return hooks.refs[index] as {current: T};
    },
    useEffect: (effect: () => void | (() => void), dependencies?: readonly unknown[]) => {
      const index = hooks.effectCursor++;
      const previous = hooks.effectDependencies[index];
      const changed = !previous || !dependencies || dependencies.some((value, item) => value !== previous[item]);
      hooks.effectDependencies[index] = dependencies;
      if (changed) hooks.effects.push(effect);
    },
  };
});

vi.mock('./api', () => ({api: vi.fn()}));

import {api} from './api';
import {ApplicationSupportingDocuments} from './application-supporting-documents';

type ElementProps = {
  children?: ReactNode;
  disabled?: boolean;
  onClick?: () => void;
};

const selection = {
  profile_id: 'profile-1',
  application_id: 'application-1',
  job_id: 'job-1',
  available: [{
    revision_id: 'revision-1',
    document_kind: 'CERTIFICATION' as const,
    label: 'Cloud certification',
    version: 2,
    filename: 'cloud.pdf',
    format: 'pdf',
    size_bytes: 2048,
  }],
  selected_revision_ids: [],
  unavailable_revision_ids: [],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(resolvePromise => { resolve = resolvePromise; });
  return {promise, resolve};
}

function findElements(node: ReactNode, type: string): ReactElement<ElementProps>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<ElementProps>(child)) return [];
    const current = child.type === type ? [child] : [];
    return [...current, ...findElements(child.props.children, type)];
  });
}

function render(profileId = 'profile-1', applicationId = 'application-1') {
  hooks.stateCursor = 0;
  hooks.refCursor = 0;
  hooks.effectCursor = 0;
  return ApplicationSupportingDocuments({profileId, applicationId});
}

async function runEffects() {
  const effects = hooks.effects.splice(0);
  effects.forEach(effect => effect());
  await Promise.resolve();
  await Promise.resolve();
}

async function runInitialLoad() {
  render();
  await runEffects();
}

describe('application supporting documents recovery', () => {
  const mockedApi = vi.mocked(api);

  beforeEach(() => {
    hooks.states.length = 0;
    hooks.refs.length = 0;
    hooks.effects.length = 0;
    hooks.effectDependencies.length = 0;
  });

  afterEach(() => vi.clearAllMocks());

  it('retries the same scoped load once, locks concurrent retries, and restores document choices', async () => {
    const retry = deferred<typeof selection>();
    mockedApi
      .mockRejectedValueOnce(new Error('private initial response'))
      .mockReturnValueOnce(retry.promise);

    await runInitialLoad();

    const failedTree = render();
    const failedMarkup = renderToStaticMarkup(failedTree);
    const retryButton = findElements(failedTree, 'button')
      .find(button => button.props.children === 'Retry loading supporting documents');
    expect(failedMarkup).toContain('role="alert"');
    expect(failedMarkup).toContain('Supporting document choices could not be loaded.');
    expect(failedMarkup).not.toContain('private initial response');
    expect(retryButton?.props.disabled).toBe(false);

    retryButton?.props.onClick?.();

    const pendingTree = render();
    const pendingMarkup = renderToStaticMarkup(pendingTree);
    const pendingButton = findElements(pendingTree, 'button')
      .find(button => button.props.children === 'Retrying supporting documents…');
    expect(pendingMarkup).toContain('aria-busy="true"');
    expect(pendingButton?.props.disabled).toBe(true);
    pendingButton?.props.onClick?.();
    expect(mockedApi).toHaveBeenCalledTimes(2);
    expect(mockedApi).toHaveBeenNthCalledWith(
      2,
      '/profiles/profile-1/applications/application-1/supporting-documents',
    );

    retry.resolve(selection);
    await Promise.resolve();
    await Promise.resolve();

    const recoveredMarkup = renderToStaticMarkup(render());
    expect(recoveredMarkup).not.toContain('role="alert"');
    expect(recoveredMarkup).toContain('aria-busy="false"');
    expect(recoveredMarkup).toContain('Cloud certification');
    expect(recoveredMarkup).toContain('type="checkbox"');
  });

  it('keeps repeated failures generic and retryable without exposing response details', async () => {
    mockedApi
      .mockRejectedValueOnce(new Error('private initial response'))
      .mockRejectedValueOnce(new Error('SECRET retry response'));

    await runInitialLoad();
    const retryButton = findElements(render(), 'button')
      .find(button => button.props.children === 'Retry loading supporting documents');
    retryButton?.props.onClick?.();
    await Promise.resolve();
    await Promise.resolve();

    const failedAgainTree = render();
    const failedAgainMarkup = renderToStaticMarkup(failedAgainTree);
    const nextRetry = findElements(failedAgainTree, 'button')
      .find(button => button.props.children === 'Retry loading supporting documents');
    expect(failedAgainMarkup).toContain('role="alert"');
    expect(failedAgainMarkup).toContain('Supporting document choices could not be loaded.');
    expect(failedAgainMarkup).not.toContain('private initial response');
    expect(failedAgainMarkup).not.toContain('SECRET retry response');
    expect(nextRetry?.props.disabled).toBe(false);
    expect(mockedApi).toHaveBeenCalledTimes(2);
  });

  it('ignores a completed retry after the panel moves to another profile and application scope', async () => {
    const retryA = deferred<typeof selection>();
    const loadB = deferred<typeof selection>();
    mockedApi
      .mockRejectedValueOnce(new Error('scope A unavailable'))
      .mockReturnValueOnce(retryA.promise)
      .mockReturnValueOnce(loadB.promise);

    await runInitialLoad();
    findElements(render(), 'button')
      .find(button => button.props.children === 'Retry loading supporting documents')
      ?.props.onClick?.();

    render('profile-2', 'application-2');
    await runEffects();
    expect(renderToStaticMarkup(render('profile-2', 'application-2'))).toContain('aria-busy="true"');

    retryA.resolve({
      ...selection,
      selected_revision_ids: ['revision-1'],
      available: [{...selection.available[0], label: 'Scope A private label'}],
    });
    await Promise.resolve();
    await Promise.resolve();

    const pendingB = renderToStaticMarkup(render('profile-2', 'application-2'));
    expect(pendingB).toContain('aria-busy="true"');
    expect(pendingB).not.toContain('Scope A private label');
    expect(pendingB).not.toContain('checked=""');

    loadB.resolve({
      ...selection,
      profile_id: 'profile-2',
      application_id: 'application-2',
      available: [{...selection.available[0], revision_id: 'revision-2', label: 'Scope B certification'}],
    });
    await Promise.resolve();
    await Promise.resolve();

    const recoveredB = renderToStaticMarkup(render('profile-2', 'application-2'));
    expect(recoveredB).toContain('aria-busy="false"');
    expect(recoveredB).toContain('Scope B certification');
    expect(recoveredB).not.toContain('Scope A private label');
  });
});
