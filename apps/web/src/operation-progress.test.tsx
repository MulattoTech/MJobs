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
  it('explains recovered queued intent and the worker retry vocabulary without reporting completion', () => {
    const queued = { ...operation, state: 'QUEUED', current_step: 'RECOVERING',
      progress: 1, completed_at: null, recovery_summary: 'RESUME' };
    const html = renderToStaticMarkup(<OperationProgress operations={[queued,
      { ...queued, operation_id: 'retry-operation', recovery_summary: 'RETRY: NETWORK_ERROR' }]} />);
    expect(html).toContain('Recovery requests continuation from saved progress.');
    expect(html).toContain('A retry is planned.');
    expect(html).not.toContain('NETWORK_ERROR');
    expect(html).not.toContain('<dt>Completed</dt>');
    expect(html).not.toContain('resumed after');
  });
  it('explains saved preparation priority and bounded aging without exposing internal snapshots', () => {
    const waiting = { ...operation, operation_type: 'PREPARE', state: 'QUEUED', preparation_priority: {
      tier: 4, effective_tier: 0, queue_priority: 'STALE', enqueued_at: '2026-10-11T00:00:00Z',
      aging_interval_seconds: 3600, basis: 'APPROVED_SKILL_EVIDENCE_AND_POSTING_AGE',
      timestamp_source: 'attributes.reported_posted_at', timestamp_kind: 'POSTED', timestamp_state: 'KNOWN',
    }};
    const html = renderToStaticMarkup(<OperationProgress operations={[waiting]} />);
    expect(html).toContain('Preparation priority:');
    expect(html).toContain('Promoted while waiting');
    expect(html).toContain('Approved skill evidence and reported posting age');
    expect(html).toContain('internal processing order');
  });
  it.each([
    ['POSTED', 'job.posted_at', 'POSTING_AGE', 'reported posting age'],
    ['FIRST_SEEN', 'attributes.first_seen_at', 'FIRST_SEEN_RECENCY', 'first-seen recency'],
    ['CREATED', 'job.created_at', 'RECORD_CREATION_RECENCY', 'local record age'],
  ])('explains known %s evidence using its saved meaning', (kind, source, basis, expected) => {
    const priority = {
      tier: 0, effective_tier: 0, queue_priority: 'CRITICAL', enqueued_at: '2026-10-11T00:00:00Z',
      aging_interval_seconds: 3600, basis: `APPROVED_SKILL_EVIDENCE_AND_${basis}`,
      timestamp_source: source, timestamp_kind: kind, timestamp_state: 'KNOWN',
    };
    const html = renderToStaticMarkup(<OperationProgress operations={[{
      ...operation, operation_type: 'PREPARE', preparation_priority: priority,
    }]} />);
    expect(html).toContain(`Approved skill evidence and ${expected}`);
    expect(html).toContain('Highest');
    expect(html).toContain('internal processing order');
    expect(html).not.toContain('Fresh excellent match');
    expect(html).not.toContain(source);
    if (kind !== 'POSTED') {
      expect(html).toContain('The publication date was unknown when queued.');
      expect(html).not.toContain('reported posting age');
    }
  });
  it.each([
    ['FUTURE', 'The saved date was in the future when queued, so recency was unknown.'],
    ['INVALID', 'The saved date could not be validated when queued, so recency was unknown.'],
    ['UNKNOWN', 'Date evidence for the initial order is unknown.'],
  ])('keeps %s date evidence unknown even with a stale posting-age basis', (state, expected) => {
    const html = renderToStaticMarkup(<OperationProgress operations={[{
      ...operation, operation_type: 'PREPARE', preparation_priority: {
        tier: 5, effective_tier: 0, queue_priority: 'BACKGROUND', enqueued_at: '2026-10-11T00:00:00Z',
        aging_interval_seconds: 3600, basis: 'APPROVED_SKILL_EVIDENCE_AND_POSTING_AGE',
        timestamp_source: 'attributes.reported_posted_at', timestamp_kind: 'POSTED', timestamp_state: state,
      },
    }]} />);
    expect(html).toContain(expected);
    expect(html).not.toContain('reported posting age');
    expect(html).not.toContain('Fresh excellent match');
  });
  it.each([
    {},
    { timestamp_kind: 'POSTED', timestamp_state: 'KNOWN' },
    { timestamp_source: 'job.created_at', timestamp_kind: 'POSTED', timestamp_state: 'KNOWN' },
    { timestamp_source: 'private-source-value', timestamp_kind: 'private-kind-value', timestamp_state: 'private-state-value' },
    { timestamp_source: 'job.posted_at', timestamp_kind: 'POSTED', timestamp_state: 'KNOWN', basis: 'private-basis-value' },
  ])('uses an unknown fallback for legacy, inconsistent, or unrecognized evidence %j', evidence => {
    const html = renderToStaticMarkup(<OperationProgress operations={[{
      ...operation, operation_type: 'PREPARE', preparation_priority: {
        tier: 0, effective_tier: 0, queue_priority: 'CRITICAL', enqueued_at: '2026-10-11T00:00:00Z',
        aging_interval_seconds: 3600, basis: 'APPROVED_SKILL_EVIDENCE_AND_POSTING_AGE', ...evidence,
      },
    }]} />);
    expect(html).toContain('Date evidence for the initial order is unknown.');
    expect(html).not.toContain('reported posting age');
    expect(html).not.toContain('Fresh excellent match');
    expect(html).not.toContain('private-');
  });
  it('shows the persisted retry deadline while safely queued', () => {
    const waiting = { ...operation, state: 'QUEUED', current_step: 'RETRY_WAIT',
      next_attempt_at: '2026-10-11T10:04:00Z', completed_at: null };
    const html = renderToStaticMarkup(<OperationProgress operations={[waiting]} />);
    expect(html).toContain('Waiting to retry');
    expect(html).toContain('Next retry');
    expect(html).toContain('2026-10-11T10:04:00.000Z');
    expect(html).not.toContain('<dt>Completed</dt>');
  });
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
