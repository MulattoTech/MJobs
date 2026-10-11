import React, { useEffect, useRef, useState } from 'react';

export type SearchHistoryRecord = {
  id: string;
  profile_id: string;
  source: string;
  query: string;
  filters: Record<string, unknown> | null;
  started_at: string | null;
  finished_at: string | null;
  results: number | null;
  new_jobs: number | null;
  duplicates: number | null;
  errors: number;
  status: 'COMPLETED' | 'FAILED' | 'CANCELLED';
  error_detail?: string | null;
};

export type SearchHistoryResponse = {
  profile_id: string;
  items: SearchHistoryRecord[];
  next_cursor: string | null;
};

export type SearchHistoryPanelState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; response: SearchHistoryResponse };

export type SearchHistoryPanelProps = {
  profileId: string;
  loadHistory: (profileId: string, page?: { limit?: number; cursor?: string | null }) => Promise<SearchHistoryResponse>;
};

type SearchHistoryPanelViewProps = {
  profileId: string;
  state: SearchHistoryPanelState;
  onRetry: () => void;
  onLoadMore?: () => void;
  loadingMore?: boolean;
  loadMoreError?: boolean;
};

export async function loadProfileSearchHistory(
  profileId: string,
  loadHistory: SearchHistoryPanelProps['loadHistory'],
): Promise<SearchHistoryResponse> {
  if (!profileId.trim()) throw new Error('SEARCH_HISTORY_PROFILE_REQUIRED');
  const response = await loadHistory(profileId);
  if (response.profile_id !== profileId) throw new Error('SEARCH_HISTORY_PROFILE_MISMATCH');
  if (!Array.isArray(response.items)) throw new Error('SEARCH_HISTORY_RESPONSE_INVALID');
  return response;
}

export async function loadMoreProfileSearchHistory(
  profileId: string,
  current: SearchHistoryResponse,
  loadHistory: SearchHistoryPanelProps['loadHistory'],
): Promise<SearchHistoryResponse> {
  const cursor = current.next_cursor;
  if (!cursor) return current;
  if (current.profile_id !== profileId) throw new Error('SEARCH_HISTORY_PROFILE_MISMATCH');
  const next = await loadHistory(profileId, { cursor });
  if (next.profile_id !== profileId) throw new Error('SEARCH_HISTORY_PROFILE_MISMATCH');
  if (next.next_cursor === cursor) throw new Error('SEARCH_HISTORY_CURSOR_DID_NOT_ADVANCE');
  const loadedIds = new Set(current.items.map(item => item.id));
  if (next.items.some(item => loadedIds.has(item.id))) throw new Error('SEARCH_HISTORY_DUPLICATE_PAGE');
  return { profile_id: profileId, items: [...current.items, ...next.items], next_cursor: next.next_cursor };
}

function timestamp(value: string | null): { label: string; dateTime?: string } {
  if (!value) return { label: 'Not recorded' };
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return { label: 'Not recorded' };
  const dateTime = date.toISOString();
  return { label: date.toLocaleString(), dateTime };
}

function count(value: number | null): string {
  if (value === null) return 'Unknown';
  return Number.isSafeInteger(value) && value >= 0 ? String(value) : 'Unknown';
}

function filtersText(filters: Record<string, unknown> | null): string {
  if (!filters || Object.keys(filters).length === 0) return 'No filters recorded.';
  try {
    const sortedFilters = Object.fromEntries(
      Object.entries(filters).sort(([left], [right]) => left.localeCompare(right)),
    );
    const serialized = JSON.stringify(sortedFilters);
    if (!serialized) return 'No filters recorded.';
    return serialized.length > 1000 ? `${serialized.slice(0, 1000)}…` : serialized;
  } catch {
    return 'Filters unavailable.';
  }
}

function safeErrorDetail(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const sanitized = value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim();
  if (!sanitized) return null;
  return sanitized.length > 500 ? `${sanitized.slice(0, 500)}…` : sanitized;
}

export function SearchHistoryPanelView({
  profileId, state, onRetry, onLoadMore = () => {}, loadingMore = false, loadMoreError = false,
}: SearchHistoryPanelViewProps) {
  if (!profileId) {
    return <section aria-labelledby="search-history-heading">
      <h3 id="search-history-heading">Search history</h3>
      <p className="muted">Choose a profile to view its search history.</p>
    </section>;
  }

  if (state.status === 'ready' && state.response.profile_id !== profileId) {
    return <section aria-labelledby="search-history-heading">
      <h3 id="search-history-heading">Search history</h3>
      <p role="alert">Could not load search history for this profile. Try again.</p>
      <button type="button" aria-label="Retry loading search history" onClick={onRetry}>
        Retry loading history
      </button>
    </section>;
  }

  return <section aria-labelledby="search-history-heading">
    <h3 id="search-history-heading">Search history</h3>
    <p className="muted">Profile: <code>{profileId}</code></p>
    <p className="muted"><strong>Read-only search history.</strong> This view does not rerun searches, change criteria, or interact with employers.</p>

    {state.status === 'loading'
      ? <p className="muted" role="status" aria-live="polite">Loading search history…</p>
      : state.status === 'error'
        ? <div role="alert">
          <p>Could not load search history. Try again.</p>
          <button type="button" aria-label="Retry loading search history" onClick={onRetry}>
            Retry loading history
          </button>
        </div>
        : state.response.items.length === 0
          ? <p className="muted">No search history for this profile yet.</p>
          : <>
          <div className="table-scroll" role="region" aria-label="Scrollable search history" tabIndex={0}>
            <table>
              <caption>Saved search runs for profile {profileId}</caption>
              <thead>
                <tr>
                  <th scope="col">Source</th>
                  <th scope="col">Query</th>
                  <th scope="col">Started</th>
                  <th scope="col">Finished</th>
                  <th scope="col">Run status</th>
                  <th scope="col">Results</th>
                  <th scope="col">New jobs</th>
                  <th scope="col">Duplicates</th>
                  <th scope="col">Errors</th>
                  <th scope="col">Filters and details</th>
                </tr>
              </thead>
              <tbody>
                {state.response.items.map(item => {
                  const started = timestamp(item.started_at);
                  const finished = timestamp(item.finished_at);
                  const errorDetail = safeErrorDetail(item.error_detail);
                  return <tr key={item.id}>
                    <td>{item.source || 'Unknown source'}</td>
                    <td>{item.query || 'No query recorded'}</td>
                    <td>{started.dateTime ? <time dateTime={started.dateTime}>{started.label}</time> : started.label}</td>
                    <td>{finished.dateTime ? <time dateTime={finished.dateTime}>{finished.label}</time> : finished.label}</td>
                    <td>{item.status}</td>
                    <td>{count(item.results)}</td>
                    <td>{count(item.new_jobs)}</td>
                    <td>{count(item.duplicates)}</td>
                    <td>{count(item.errors)}</td>
                    <td>
                      <details>
                        <summary>View filters and run details</summary>
                        <p><strong>Filters:</strong> <code>{filtersText(item.filters)}</code></p>
                        {errorDetail && <p><strong>Recorded error detail:</strong> {errorDetail}</p>}
                      </details>
                    </td>
                  </tr>;
                })}
              </tbody>
            </table>
          </div>
          {state.response.next_cursor && <div>
            {loadMoreError && <p role="alert">Could not load older search history. Try again.</p>}
            <button type="button" disabled={loadingMore} onClick={onLoadMore}>
              {loadingMore ? 'Loading older history…' : 'Load older history'}
            </button>
          </div>}
          </>}
  </section>;
}

export function SearchHistoryPanel({ profileId, loadHistory }: SearchHistoryPanelProps) {
  const [state, setState] = useState<SearchHistoryPanelState>({ status: profileId ? 'loading' : 'error' });
  const [retryVersion, setRetryVersion] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadMoreError, setLoadMoreError] = useState(false);
  const sequence = useRef(0);
  const loadHistoryRef = useRef(loadHistory);
  loadHistoryRef.current = loadHistory;

  useEffect(() => {
    const requestSequence = ++sequence.current;
    let active = true;
    if (!profileId) {
      setState({ status: 'error' });
      return () => { active = false; };
    }
    setState({ status: 'loading' });
    setLoadingMore(false);
    setLoadMoreError(false);
    void loadProfileSearchHistory(profileId, loadHistoryRef.current)
      .then(response => {
        if (active && requestSequence === sequence.current) setState({ status: 'ready', response });
      })
      .catch(() => {
        if (active && requestSequence === sequence.current) setState({ status: 'error' });
      });
    return () => {
      active = false;
      if (requestSequence === sequence.current) sequence.current += 1;
    };
  }, [profileId, retryVersion]);

  async function loadMore() {
    if (state.status !== 'ready' || !state.response.next_cursor || loadingMore) return;
    const requestSequence = sequence.current;
    setLoadingMore(true);
    setLoadMoreError(false);
    try {
      const response = await loadMoreProfileSearchHistory(
        profileId, state.response, loadHistoryRef.current,
      );
      if (requestSequence === sequence.current && response.profile_id === profileId) {
        setState({ status: 'ready', response });
      }
    } catch {
      if (requestSequence === sequence.current) setLoadMoreError(true);
    } finally {
      if (requestSequence === sequence.current) setLoadingMore(false);
    }
  }

  const visibleState = state.status === 'ready' && state.response.profile_id !== profileId
    ? { status: 'loading' as const }
    : state;

  return <SearchHistoryPanelView
    profileId={profileId}
    state={visibleState}
    onRetry={() => setRetryVersion(value => value + 1)}
    onLoadMore={() => void loadMore()}
    loadingMore={loadingMore}
    loadMoreError={loadMoreError}
  />;
}
