import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { OperationProgress, type OperationProgressRecord } from './operation-progress';

const pending: OperationProgressRecord = {
  operation_id: 'operation-recovery', operation_type: 'DISCOVER', entity_id: 'profile-fixture',
  state: 'QUEUED', current_step: 'RECOVERING', progress: 1, retry_count: 1, completed_at: null,
};
const render = (summary: unknown) => renderToStaticMarkup(
  <OperationProgress operations={[{ ...pending, recovery_summary: summary as string }]} />,
);

// These are the five actions accepted by services.operations.progress. Bare RESUME
// comes from recover_expired; RETRY: NETWORK_ERROR comes from the discovery worker.
const actions = [
  ['RESUME', 'Recovery requests continuation from saved progress.'],
  ['RETRY', 'A retry is planned.'],
  ['RECONCILE', 'Outcome reconciliation is required before continuing.'],
  ['MANUAL_REVIEW', 'Manual review is required before continuing.'],
  ['NONE', 'No recovery action is available.'],
] as const;

describe('durable recovery vocabulary', () => {
  it.each(actions)('explains producer action %s without claiming execution', (action, copy) => {
    for (const summary of [action, `${action}: NETWORK_ERROR`]) {
      const html = render(summary);
      expect(html).toContain(copy);
      expect(html).not.toContain('NETWORK_ERROR');
      expect(html).not.toContain('Operation status: Completed');
      expect(html).not.toContain('<dt>Completed</dt>');
      expect(html).not.toContain('resumed after');
      expect(html).not.toContain('100%');
    }
  });

  it('treats a valid opaque reason as private rather than displaying its content', () => {
    const html = render('RESUME: PRIVATE_REASON_SENTINEL');
    expect(html).toContain(actions[0][1]);
    expect(html).not.toContain('PRIVATE_REASON_SENTINEL');
  });

  it.each([
    'UNKNOWN_ACTION', 'UNKNOWN_ACTION: NETWORK_ERROR', 'RESUME: ', 'RESUME:NETWORK_ERROR',
    'RESUME: NETWORK_ERROR: PRIVATE_CANARY', 'RESUME: private_canary', ' RESUME', 'RESUME ',
    'RESUME\n', 'RESUME: NETWORK_ERROR\n', 'RESUME: NETWORK_ERROR\r',
    'RESUME: PRIVATE_CANARY<script>', `RESUME: ${'R'.repeat(65)}`, 'R'.repeat(65),
    'RESUME: NETWORK_ERROR PRIVATE_CANARY', 'RESUME\u0000: NETWORK_ERROR',
  ])('rejects the entire malformed or unknown summary %j', summary => {
    const html = render(summary);
    expect(html).toContain('Recovery details are available.');
    expect(html).not.toContain(actions[0][1]);
    expect(html).not.toContain('PRIVATE_CANARY');
    expect(html).not.toContain('NETWORK_ERROR');
  });

  it('accepts the producer maximum reason length without exposing it', () => {
    const reason = 'R'.repeat(64);
    expect(render(`RESUME: ${reason}`)).toContain(actions[0][1]);
    expect(render(`RESUME: ${reason}`)).not.toContain(reason);
  });

  it.each([
    ['MANUAL_ACTION_REQUIRED', 'Manual recovery is required.'],
    ['RESUMED_AFTER_RESTART', 'The operation resumed after an interruption.'],
    ['RETRY_BLOCKED', 'Retry is blocked pending review.'],
    ['SAFE_TO_RETRY', 'A safe retry is available.'],
  ])('preserves legacy summary %s', (summary, copy) => {
    expect(render(summary)).toContain(copy);
  });

  it('withholds non-string values and raw private operation fields', () => {
    for (const value of [null, undefined, { action: 'RESUME', owner: 'PRIVATE_OWNER' }, 5]) {
      expect(render(value)).not.toContain('PRIVATE_OWNER');
    }
    const html = renderToStaticMarkup(<OperationProgress operations={[{
      ...pending, recovery_summary: 'RETRY: PRIVATE_REASON_SENTINEL',
      params: { token: 'PRIVATE_PARAMS' }, error: 'PRIVATE_ERROR',
      recovery_metadata: { manual_auth_owner: 'PRIVATE_OWNER' },
    } as OperationProgressRecord]} />);
    for (const value of ['PRIVATE_REASON_SENTINEL', 'PRIVATE_PARAMS', 'PRIVATE_ERROR', 'PRIVATE_OWNER']) {
      expect(html).not.toContain(value);
    }
  });
});
