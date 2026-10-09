import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  loadProfileSearchHistory,
  loadMoreProfileSearchHistory,
  SearchHistoryPanel,
  SearchHistoryPanelView,
  type SearchHistoryPanelState,
  type SearchHistoryRecord,
  type SearchHistoryResponse,
} from './search-history-panel';

function findButtons(node: ReactNode): ReactElement<{
  children?: ReactNode;
  onClick?: () => void;
  'aria-label'?: string;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{
      children?: ReactNode;
      onClick?: () => void;
      'aria-label'?: string;
    }>(child)) return [];
    const current = child.type === 'button' ? [child] : [];
    return [...current, ...findButtons(child.props.children)];
  });
}

const record: SearchHistoryRecord = {
  id: 'run-1',
  profile_id: 'profile-1',
  source: 'Greenhouse',
  query: 'Infrastructure Support Engineer',
  filters: { remote: true, posted_within_days: 14, source_filters: { boards: ['Greenhouse'] } },
  started_at: '2026-10-08T15:00:00Z',
  finished_at: '2026-10-08T15:00:04Z',
  results: 12,
  new_jobs: 3,
  duplicates: 8,
  errors: 1,
  status: 'FAILED',
  error_detail: 'One source timed out; other results were retained.',
};

const response: SearchHistoryResponse = {
  profile_id: 'profile-1',
  items: [record],
  next_cursor: null,
};

describe('search history panel', () => {
  it('shows a loading state scoped to the selected profile and explains that history is read-only', () => {
    const html = renderToStaticMarkup(
      <SearchHistoryPanel profileId="profile-1" loadHistory={vi.fn()} />,
    );

    expect(html).toContain('role="status"');
    expect(html).toContain('Loading search history');
    expect(html).toContain('Profile: <code>profile-1</code>');
    expect(html).toContain('Read-only search history');
    expect(html).not.toContain('Retry search');
  });

  it('shows an explicit empty state for a profile with no durable search runs', () => {
    const state: SearchHistoryPanelState = {
      status: 'ready',
      response: { profile_id: 'profile-1', items: [], next_cursor: null },
    };
    const html = renderToStaticMarkup(
      <SearchHistoryPanelView profileId="profile-1" state={state} onRetry={() => {}} />,
    );

    expect(html).toContain('No search history for this profile yet.');
    expect(html).toContain('Profile: <code>profile-1</code>');
    expect(html).toContain('Read-only search history');
    expect(html).not.toContain('<table');
  });

  it('renders source, query, profile scope, timestamps, filters, counts, status, and read-only error detail', () => {
    const state: SearchHistoryPanelState = { status: 'ready', response };
    const html = renderToStaticMarkup(
      <SearchHistoryPanelView profileId="profile-1" state={state} onRetry={() => {}} />,
    );

    expect(html).toContain('<table');
    expect(html).toContain('Greenhouse');
    expect(html).toContain('Infrastructure Support Engineer');
    expect(html).toContain('Profile: <code>profile-1</code>');
    expect(html).toContain('dateTime="2026-10-08T15:00:00.000Z"');
    expect(html).toContain('remote');
    expect(html).toContain('boards');
    expect(html).toContain('Greenhouse');
    expect(html).toContain('12');
    expect(html).toContain('3');
    expect(html).toContain('8');
    expect(html).toContain('FAILED');
    expect(html).toContain('One source timed out; other results were retained.');
    expect(html).toContain('does not rerun searches, change criteria, or interact with employers');
    expect(html).not.toContain('<button');
  });

  it('shows unmeasured nullable counts as Unknown and preserves a measured zero', () => {
    const state: SearchHistoryPanelState = {
      status: 'ready',
      response: {
        ...response,
        items: [{ ...record, results: null, new_jobs: 0, duplicates: null }],
      },
    };
    const html = renderToStaticMarkup(
      <SearchHistoryPanelView profileId="profile-1" state={state} onRetry={() => {}} />,
    );

    expect(html).toContain('<td>Unknown</td><td>0</td><td>Unknown</td>');
  });

  it('offers retry after a load error without exposing the thrown error or changing scope', () => {
    const state: SearchHistoryPanelState = { status: 'error' };
    const onRetry = vi.fn();
    const props = { profileId: 'profile-1', state, onRetry };
    const tree = SearchHistoryPanelView(props);
    const html = renderToStaticMarkup(<SearchHistoryPanelView {...props} />);

    expect(html).toContain('role="alert"');
    expect(html).toContain('Could not load search history. Try again.');
    expect(html).toContain('aria-label="Retry loading search history"');
    expect(html).toContain('Profile: <code>profile-1</code>');

    findButtons(tree)[0]?.props.onClick?.();
    expect(onRetry).toHaveBeenCalledExactlyOnceWith();
  });

  it('fails closed when the durable loader returns a different profile scope', async () => {
    const loadHistory = vi.fn().mockResolvedValue({ ...response, profile_id: 'profile-2' });

    await expect(loadProfileSearchHistory('profile-1', loadHistory))
      .rejects.toThrow('SEARCH_HISTORY_PROFILE_MISMATCH');
    expect(loadHistory).toHaveBeenCalledExactlyOnceWith('profile-1');
  });

  it('requests the durable history for the explicit profile and keeps the response scoped', async () => {
    const loadHistory = vi.fn().mockResolvedValue(response);

    await expect(loadProfileSearchHistory('profile-1', loadHistory)).resolves.toEqual(response);
    expect(loadHistory).toHaveBeenCalledExactlyOnceWith('profile-1');
  });

  it('loads older profile history and appends the next page without replacing current rows', async () => {
    const nextRecord = { ...record, id: 'run-2', query: 'Network Administrator' };
    const loadHistory = vi.fn().mockResolvedValue({
      profile_id: 'profile-1', items: [nextRecord], next_cursor: null,
    });
    const current = { ...response, next_cursor: 'cursor-2' };

    await expect(loadMoreProfileSearchHistory('profile-1', current, loadHistory)).resolves.toEqual({
      profile_id: 'profile-1', items: [record, nextRecord], next_cursor: null,
    });
    expect(loadHistory).toHaveBeenCalledExactlyOnceWith('profile-1', { cursor: 'cursor-2' });
  });

  it('rejects pages that repeat the cursor or contain a previously loaded run', async () => {
    const repeatedCursor = vi.fn().mockResolvedValue({
      profile_id: 'profile-1', items: [], next_cursor: 'cursor-2',
    });
    await expect(loadMoreProfileSearchHistory(
      'profile-1', { ...response, next_cursor: 'cursor-2' }, repeatedCursor,
    )).rejects.toThrow('SEARCH_HISTORY_CURSOR_DID_NOT_ADVANCE');

    const repeatedRun = vi.fn().mockResolvedValue({
      profile_id: 'profile-1', items: [record], next_cursor: null,
    });
    await expect(loadMoreProfileSearchHistory(
      'profile-1', { ...response, next_cursor: 'cursor-3' }, repeatedRun,
    )).rejects.toThrow('SEARCH_HISTORY_DUPLICATE_PAGE');
  });

  it('does not render ready rows returned for another profile', () => {
    const state: SearchHistoryPanelState = {
      status: 'ready',
      response: { ...response, profile_id: 'profile-2' },
    };
    const html = renderToStaticMarkup(
      <SearchHistoryPanelView profileId="profile-1" state={state} onRetry={() => {}} />,
    );

    expect(html).toContain('role="alert"');
    expect(html).not.toContain('Infrastructure Support Engineer');
    expect(html).toContain('aria-label="Retry loading search history"');
  });
});
