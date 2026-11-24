import React from 'react';
import type { SearchHistoryRecord } from './search-history-panel';

export type SearchReceiptLookup =
  | { status: 'loading' }
  | { status: 'unavailable' }
  | { status: 'ready'; receipt: SearchHistoryRecord | null };

export type SavedSearchEmptyStateKind =
  | 'no-enabled-source'
  | 'loading'
  | 'unknown'
  | 'no-receipt'
  | 'failed'
  | 'cancelled'
  | 'zero-results'
  | 'results-unavailable'
  | 'no-visible-matches';

export function savedSearchEmptyStateKind(
  enabledSourceCount: number,
  lookup: SearchReceiptLookup,
  sourcesLoaded = true,
): SavedSearchEmptyStateKind {
  if (!sourcesLoaded) return 'loading';
  if (enabledSourceCount === 0) return 'no-enabled-source';
  if (lookup.status === 'loading') return 'loading';
  if (lookup.status === 'unavailable') return 'unknown';
  if (!lookup.receipt) return 'no-receipt';
  if (lookup.receipt.status === 'FAILED') return 'failed';
  if (lookup.receipt.status === 'CANCELLED') return 'cancelled';
  if (lookup.receipt.results === 0) return 'zero-results';
  if (lookup.receipt.results === null) return 'results-unavailable';
  return 'no-visible-matches';
}

function emptyStateText(kind: SavedSearchEmptyStateKind, receipt: SearchHistoryRecord | null) {
  switch (kind) {
    case 'no-enabled-source':
      return 'No enabled source is allowed by this saved search. Enable an allowed source in Sources or update the search’s source scope.';
    case 'loading':
      return 'Checking collection history for this saved search…';
    case 'unknown':
      return 'Collection history is unavailable, so the latest collection status is unknown. Check Activity or reload.';
    case 'no-receipt':
      return 'No completed collection has been recorded for this saved search yet. Collect from an enabled source or wait for scheduled discovery.';
    case 'failed':
      return `The latest collection from ${receipt?.source || 'an unknown source'} failed. Review Activity for details, then retry.`;
    case 'cancelled':
      return `The latest collection from ${receipt?.source || 'an unknown source'} was cancelled. Complete results are not available yet.`;
    case 'zero-results':
      return `The latest collection from ${receipt?.source || 'an unknown source'} completed with zero results for this saved search.`;
    case 'results-unavailable':
      return `The latest collection from ${receipt?.source || 'an unknown source'} completed, but its result count is unknown. Check Activity for details.`;
    case 'no-visible-matches':
      return `The latest collection from ${receipt?.source || 'an unknown source'} completed, but no jobs are visible in this saved-search view. Review its filters or check Activity.`;
  }
}

export function SavedSearchJobsEmptyState({
  searchName,
  enabledSourceCount,
  sourcesLoaded = true,
  lookup,
  onManageSources,
  onCaptureJob,
}: {
  searchName: string;
  enabledSourceCount: number;
  sourcesLoaded?: boolean;
  lookup: SearchReceiptLookup;
  onManageSources: () => void;
  onCaptureJob: () => void;
}) {
  const kind = savedSearchEmptyStateKind(enabledSourceCount, lookup, sourcesLoaded);
  const receipt = lookup.status === 'ready' ? lookup.receipt : null;
  return <section className="empty" aria-label={`Empty jobs for ${searchName}`}>
    <h2>No jobs in {searchName} yet.</h2>
    <p role={kind === 'loading' || kind === 'unknown' ? 'status' : undefined}>{emptyStateText(kind, receipt)}</p>
    <button type="button" onClick={onManageSources}>Manage sources</button>{' '}
    <button type="button" onClick={onCaptureJob}>Capture a job</button>
  </section>;
}
