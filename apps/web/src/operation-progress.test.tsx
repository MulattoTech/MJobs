import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { OperationProgress, type OperationProgressRecord } from './operation-progress';

const operation: OperationProgressRecord = {
  operation_id: 'op-123',
  operation_type: 'DISCOVER',
  entity_id: 'profile-456',
  state: 'RUNNING',
  current_step: 'FETCHING_POSTINGS',
  progress: 45,
  started_at: '2026-10-11T10:00:00Z',
  updated_at: '2026-10-11T10:02:00Z',
  completed_at: null,
  retry_count: 2,
  recovery_summary: 'SAFE_TO_RETRY',
};

describe('persistent operation progress', () => {
  it('shows safe operation identity, step, progress, timestamps, retries, and recovery summary', () => {
    const html = renderToStaticMarkup(<OperationProgress operations={[operation]} />);

    expect(html).toContain('Discovery');
    expect(html).toContain('profile-456');
    expect(html).toContain('Fetching postings');
    expect(html).toContain('45%');
    expect(html).toContain('Started');
    expect(html).toContain('Last updated');
    expect(html).toContain('<dt>Retry attempts</dt><dd>2</dd>');
    expect(html).toContain('A safe retry is available.');
  });

  it('reports completion timestamps and recovered operation state', () => {
    const completed = { ...operation, state: 'COMPLETED', progress: 100, completed_at: '2026-10-11T10:03:00Z' };
    const recovered = { ...operation, state: 'RECOVERED', recovery_summary: 'RESUMED_AFTER_RESTART' };
    const html = renderToStaticMarkup(<OperationProgress operations={[completed, recovered]} />);

    expect(html).toContain('Completed');
    expect(html).toContain('Recovered');
    expect(html).toContain('2026');
    expect(html).toContain('The operation resumed after an interruption.');
  });

  it('provides clear loading, request-error, and no-operations states', () => {
    expect(renderToStaticMarkup(<OperationProgress loading operations={[]} />)).toContain('Loading operations');
    expect(renderToStaticMarkup(<OperationProgress error="private backend detail" operations={[]} />)).toContain('Operations could not be loaded.');
    expect(renderToStaticMarkup(<OperationProgress operations={[]} />)).toContain('No operations to show.');
  });

  it('never displays raw parameters, provider errors, or recovery metadata', () => {
    const unsafe = {
      ...operation,
      params: { token: 'secret-token-value' },
      error: 'provider response includes api-key-value',
      recovery_metadata: { payload: 'private-provider-payload' },
      operation_type: 'UNKNOWN_PROVIDER: secret-token-value',
      current_step: 'TOKEN_RETRIEVAL_SECRET',
      recovery_summary: 'payload private-provider-payload',
    } as OperationProgressRecord;
    const html = renderToStaticMarkup(<OperationProgress operations={[unsafe]} />);

    expect(html).not.toContain('secret-token-value');
    expect(html).not.toContain('api-key-value');
    expect(html).not.toContain('private-provider-payload');
    expect(html).toContain('Background operation');
    expect(html).toContain('Recovery details are available.');
  });
});
