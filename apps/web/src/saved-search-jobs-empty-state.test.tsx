import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  savedSearchEmptyStateKind,
  SavedSearchJobsEmptyState,
  type SearchReceiptLookup,
} from './saved-search-jobs-empty-state';
import type { SearchHistoryRecord } from './search-history-panel';

const receipt: SearchHistoryRecord = {
  id: 'run-1',
  profile_id: 'profile-1',
  search_profile_id: 'search-1',
  source: 'Greenhouse',
  query: 'Infrastructure Engineer',
  filters: null,
  started_at: null,
  finished_at: null,
  results: 0,
  new_jobs: 0,
  duplicates: 0,
  errors: 0,
  status: 'COMPLETED',
};

describe('saved-search Jobs empty state', () => {
  it.each([
    [0, { status: 'ready', receipt } as SearchReceiptLookup, 'no-enabled-source'],
    [2, { status: 'loading' } as SearchReceiptLookup, 'loading'],
    [2, { status: 'unavailable' } as SearchReceiptLookup, 'unknown'],
    [2, { status: 'ready', receipt: null } as SearchReceiptLookup, 'no-receipt'],
    [2, { status: 'ready', receipt: { ...receipt, status: 'FAILED' } } as SearchReceiptLookup, 'failed'],
    [2, { status: 'ready', receipt: { ...receipt, status: 'CANCELLED' } } as SearchReceiptLookup, 'cancelled'],
    [2, { status: 'ready', receipt } as SearchReceiptLookup, 'zero-results'],
    [2, { status: 'ready', receipt: { ...receipt, results: null } } as SearchReceiptLookup, 'results-unavailable'],
    [2, { status: 'ready', receipt: { ...receipt, results: 5 } } as SearchReceiptLookup, 'no-visible-matches'],
  ])('maps %s enabled sources and receipt status to %s', (sourceCount, lookup, expected) => {
    expect(savedSearchEmptyStateKind(sourceCount as number, lookup as SearchReceiptLookup)).toBe(expected);
  });

  it('shows a loading explanation until enabled source data is loaded', () => {
    expect(savedSearchEmptyStateKind(0, { status: 'ready', receipt: null }, false)).toBe('loading');
  });

  it('keeps source management and manual capture available for an empty saved search', () => {
    const onManageSources = vi.fn();
    const onCaptureJob = vi.fn();
    const html = renderToStaticMarkup(<SavedSearchJobsEmptyState
      searchName="Texoma Local IT / Support"
      enabledSourceCount={0}
      lookup={{ status: 'ready', receipt: null }}
      onManageSources={onManageSources}
      onCaptureJob={onCaptureJob}
    />);
    expect(html).toContain('No jobs in Texoma Local IT / Support yet.');
    expect(html).toContain('No enabled source is allowed by this saved search.');
    expect(html).toContain('Manage sources');
    expect(html).toContain('Capture a job');
  });

  it.each([
    [{ status: 'ready', receipt: null } as SearchReceiptLookup, 'No completed collection has been recorded'],
    [{ status: 'ready', receipt: { ...receipt, status: 'FAILED' } } as SearchReceiptLookup, 'Greenhouse failed'],
    [{ status: 'ready', receipt } as SearchReceiptLookup, 'completed with zero results'],
    [{ status: 'unavailable' } as SearchReceiptLookup, 'latest collection status is unknown'],
  ])('renders an honest collection explanation for %s', (lookup, expectedText) => {
    const html = renderToStaticMarkup(<SavedSearchJobsEmptyState
      searchName="Remote IT / Infrastructure"
      enabledSourceCount={1}
      lookup={lookup as SearchReceiptLookup}
      onManageSources={() => {}}
      onCaptureJob={() => {}}
    />);
    expect(html).toContain(expectedText);
  });
});
