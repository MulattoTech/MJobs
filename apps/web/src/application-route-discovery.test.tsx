import React, {Children, isValidElement, type ReactElement, type ReactNode} from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const hooks = vi.hoisted(() => ({
  states: [] as unknown[],
  refs: [] as {current: unknown}[],
  stateCursor: 0,
  refCursor: 0,
}));

vi.mock('react', async importOriginal => {
  const actual = await importOriginal<typeof import('react')>();
  return {
    ...actual,
    useState: <T,>(initial: T | (() => T)) => {
      const index = hooks.stateCursor++;
      if (!(index in hooks.states)) hooks.states[index] = typeof initial === 'function'
        ? (initial as () => T)() : initial;
      const setState = (next: T | ((current: T) => T)) => {
        hooks.states[index] = typeof next === 'function'
          ? (next as (current: T) => T)(hooks.states[index] as T) : next;
      };
      return [hooks.states[index] as T, setState] as const;
    },
    useRef: <T,>(initial: T) => {
      const index = hooks.refCursor++;
      if (!(index in hooks.refs)) hooks.refs[index] = {current: initial};
      return hooks.refs[index] as {current: T};
    },
  };
});

import { ApplicationRouteDiscovery, safeCandidateUrl,
  type ApplicationRouteDiscoveryResult } from './application-route-discovery';

type ElementProps = {children?: ReactNode; disabled?: boolean; onClick?: () => void; href?: string};
type Props = Parameters<typeof ApplicationRouteDiscovery>[0];

const destinationA: ApplicationRouteDiscoveryResult = {
  source_route_id: 'aggregator', status: 'RESOLVED_ATS_ROUTE',
  candidate_url: 'https://boards.greenhouse.io/routefeedback/jobs/1',
  provider: 'greenhouse', tenant: 'routefeedback',
};
const destinationB = {...destinationA,
  candidate_url: 'https://boards.greenhouse.io/routefeedback/jobs/2'};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return {promise, resolve, reject};
}

function findElements(node: ReactNode, type: string): ReactElement<ElementProps>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<ElementProps>(child)) return [];
    return [...(child.type === type ? [child] : []), ...findElements(child.props.children, type)];
  });
}

function renderDiscovery(props: Props) {
  hooks.stateCursor = 0;
  hooks.refCursor = 0;
  return ApplicationRouteDiscovery(props);
}

function button(tree: ReactNode, label: string) {
  const found = findElements(tree, 'button').find(item => item.props.children === label);
  if (!found) throw new Error('Expected route control: ' + label);
  return found;
}

function observation(tree: ReactNode) {
  const html = renderToStaticMarkup(tree);
  return {
    destination: findElements(tree, 'a')[0]?.props.href ?? null,
    saved: html.includes('Saved as a selectable route.'),
    saveAvailable: findElements(tree, 'button').some(item =>
      item.props.children === 'Save detected destination as selectable route' && !item.props.disabled),
  };
}

async function settle() {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  hooks.states.length = 0;
  hooks.refs.length = 0;
  hooks.stateCursor = 0;
  hooks.refCursor = 0;
});
afterEach(() => vi.clearAllMocks());

describe('application route discovery', () => {
  it('offers a user-triggered check and explains that the saved route stays selected', () => {
    const onCheck = vi.fn(async () => ({source_route_id:'aggregator', status:'UNRESOLVED' as const}));
    const html = renderToStaticMarkup(<ApplicationRouteDiscovery
      routeId="aggregator" routeLabel="job-board · board" onCheck={onCheck}/>);
    expect(html).toContain('Check employer route');
    expect(html).toContain('Check only when requested');
    expect(html).toContain('never changes the selected route or submits');
    expect(onCheck).not.toHaveBeenCalled();
  });

  it('does not render a candidate from an unsafe scheme, query string, or fragment', () => {
    expect(safeCandidateUrl('javascript:alert(1)')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job?token=secret')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job#personal')).toBeNull();
    expect(safeCandidateUrl('https://ats.example/job')).toBe('https://ats.example/job');
  });

  it('keeps a pending save bound across attempted and subsequent rechecks', async () => {
    const firstSave = deferred<void>();
    const onCheck = vi.fn<(...args: [string]) => Promise<ApplicationRouteDiscoveryResult>>()
      .mockResolvedValueOnce(destinationA).mockResolvedValue(destinationB);
    const onSaveDestination = vi.fn<(...args: [string]) => Promise<void>>()
      .mockReturnValue(firstSave.promise);
    const props = {routeId: 'aggregator', routeLabel: 'job-board · board', onCheck, onSaveDestination};

    button(renderDiscovery(props), 'Check employer route').props.onClick!();
    await settle();
    button(renderDiscovery(props), 'Save detected destination as selectable route').props.onClick!();

    const pendingCheck = button(renderDiscovery(props), 'Check employer route');
    const disabledDuringSave = pendingCheck.props.disabled;
    // Invoke the handler even when disabled: admission must be synchronous, not just visual.
    pendingCheck.props.onClick!();
    await settle();
    const duringSave = {...observation(renderDiscovery(props)), checks: onCheck.mock.calls.length};

    firstSave.resolve(undefined);
    await settle();
    const afterSave = observation(renderDiscovery(props));

    button(renderDiscovery(props), 'Check employer route').props.onClick!();
    await settle();
    const laterCheck = observation(renderDiscovery(props));

    // Complete the entire sequence before asserting: an attribute-only check cannot prove this race.
    expect({disabledDuringSave, duringSave, afterSave, laterCheck,
      checkRoutes: onCheck.mock.calls.map(call => call[0]),
      saveRoutes: onSaveDestination.mock.calls.map(call => call[0])}).toEqual({
      disabledDuringSave: true,
      duringSave: {destination: destinationA.candidate_url, saved: false, saveAvailable: false, checks: 1},
      afterSave: {destination: destinationA.candidate_url, saved: true, saveAvailable: false},
      laterCheck: {destination: destinationB.candidate_url, saved: false, saveAvailable: true},
      checkRoutes: ['aggregator', 'aggregator'],
      saveRoutes: ['aggregator'],
    });
  });

  it('recovers from rejected checks and saves without allowing an intervening action', async () => {
    const firstSave = deferred<void>();
    const onCheck = vi.fn<(...args: [string]) => Promise<ApplicationRouteDiscoveryResult>>()
      .mockRejectedValueOnce(new Error('private check diagnostics')).mockResolvedValue(destinationA);
    const onSaveDestination = vi.fn<(...args: [string]) => Promise<void>>()
      .mockReturnValueOnce(firstSave.promise).mockResolvedValue(undefined);
    const props = {routeId: 'aggregator', routeLabel: 'job-board · board', onCheck, onSaveDestination};

    button(renderDiscovery(props), 'Check employer route').props.onClick!();
    await settle();
    const failedCheck = renderDiscovery(props);
    expect(renderToStaticMarkup(failedCheck)).toContain('This route could not be checked.');
    expect(renderToStaticMarkup(failedCheck)).not.toContain('private check diagnostics');
    expect(button(failedCheck, 'Check employer route').props.disabled).toBe(false);

    button(failedCheck, 'Check employer route').props.onClick!();
    await settle();
    button(renderDiscovery(props), 'Save detected destination as selectable route').props.onClick!();
    button(renderDiscovery(props), 'Check employer route').props.onClick!();
    await settle();
    firstSave.reject(new Error('private save diagnostics'));
    await settle();

    const failedSave = renderDiscovery(props);
    expect(renderToStaticMarkup(failedSave)).toContain('The destination could not be saved.');
    expect(renderToStaticMarkup(failedSave)).not.toContain('private save diagnostics');
    expect(observation(failedSave)).toEqual({
      destination: destinationA.candidate_url, saved: false, saveAvailable: true,
    });
    button(failedSave, 'Save detected destination as selectable route').props.onClick!();
    await settle();
    expect(observation(renderDiscovery(props))).toEqual({
      destination: destinationA.candidate_url, saved: true, saveAvailable: false,
    });
    expect(onCheck).toHaveBeenCalledTimes(2);
    expect(onSaveDestination).toHaveBeenCalledTimes(2);
  });

  it('admits one same-turn check and one same-turn save', async () => {
    const pendingCheck = deferred<ApplicationRouteDiscoveryResult>();
    const pendingSave = deferred<void>();
    const onCheck = vi.fn(() => pendingCheck.promise);
    const onSaveDestination = vi.fn(() => pendingSave.promise);
    const props = {routeId: 'aggregator', routeLabel: 'job-board · board', onCheck, onSaveDestination};
    const check = button(renderDiscovery(props), 'Check employer route');
    check.props.onClick!();
    check.props.onClick!();
    const checksBeforeCompletion = onCheck.mock.calls.length;
    pendingCheck.resolve(destinationA);
    await settle();

    const save = button(renderDiscovery(props), 'Save detected destination as selectable route');
    save.props.onClick!();
    save.props.onClick!();
    const savesBeforeCompletion = onSaveDestination.mock.calls.length;
    pendingSave.resolve(undefined);
    await settle();
    expect({checksBeforeCompletion, savesBeforeCompletion,
      result: observation(renderDiscovery(props))}).toEqual({
      checksBeforeCompletion: 1, savesBeforeCompletion: 1,
      result: {destination: destinationA.candidate_url, saved: true, saveAvailable: false},
    });
  });
});
