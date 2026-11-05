import React, { useEffect, useState } from 'react';
import type { RecentlyViewedResponse } from './recently-viewed-client';
import { recentlyViewedClient } from './recently-viewed-client';

type State = { kind: 'loading' } | { kind: 'error' } |
  { kind: 'ready'; response: RecentlyViewedResponse };

type Props = {
  profileId: string;
  refreshToken: number;
  onOpenJob: (jobId: string) => void;
};

type ViewProps = {
  profileId: string;
  state: State;
  clearing: boolean;
  clearError: boolean;
  onRefresh: () => void;
  onClear: () => void;
  onOpenJob: (jobId: string) => void;
};

export function RecentlyViewedPanelView({
  profileId, state, clearing, clearError, onRefresh, onClear, onOpenJob,
}: ViewProps) {
  const scopeReady = state.kind === 'ready' && state.response.profile_id === profileId;
  return <section className="panel" aria-label="Recently viewed jobs">
    <div className="toolbar">
      <div>
        <h2>Recently viewed</h2>
        <p className="muted">A visit is not a save or an application. Saved-only filtering is unavailable in this visit list.</p>
      </div>
      <div>
        <button type="button" disabled={!profileId || clearing} onClick={onRefresh}>Refresh visits</button>{' '}
        <button type="button" disabled={!scopeReady ||
          (state.kind === 'ready' && state.response.items.length === 0) || clearing} onClick={onClear}>
          {clearing ? 'Clearing…' : 'Clear visit history'}
        </button>
      </div>
    </div>
    {!profileId ? <p>Select a profile to see its job visits.</p>
      : state.kind === 'loading' || (state.kind === 'ready' && state.response.profile_id !== profileId)
        ? <p role="status">Loading job visits…</p>
        : state.kind === 'error'
          ? <p role="alert">Could not load recent visits. Use Refresh visits to retry.</p>
          : state.response.items.length === 0
            ? <p>No jobs viewed in this profile yet.</p>
            : <ol aria-label="Recent job visits">
              {state.response.items.map(item => <li key={item.job_id}>
                {item.available
                  ? <button type="button" onClick={() => onOpenJob(item.job_id)}>
                    {item.title} — {item.company}
                  </button>
                  : item.unavailable_reason === 'NO_LONGER_OPEN'
                    ? <span>{item.title} &mdash; {item.company} &middot; No longer open</span>
                    : <span>Job no longer in the local catalog</span>}
                <small className="muted"> · Viewed {new Date(item.viewed_at).toLocaleString()}</small>
              </li>)}
            </ol>}
    {state.kind === 'ready' && state.response.profile_id === profileId &&
      <p className="muted">Showing up to {state.response.max_items} visits from the last {state.response.retention_days} days. Older rows are purged on the next visit. Clearing removes only this profile's visit list.</p>}
    {clearError && <p role="alert">Could not clear visit history. Check the list before trying again.</p>}
  </section>;
}

export function RecentlyViewedPanel({profileId, refreshToken, onOpenJob}: Props) {
  const [state, setState] = useState<State>({kind: 'loading'});
  const [retry, setRetry] = useState(0);
  const [clearing, setClearing] = useState(false);
  const [clearError, setClearError] = useState(false);

  useEffect(() => {
    let active = true;
    setState({kind: 'loading'});
    setClearError(false);
    if (profileId) {
      void recentlyViewedClient.list(profileId).then(response => {
        if (active) setState({kind: 'ready', response});
      }).catch(() => {
        if (active) setState({kind: 'error'});
      });
    }
    return () => {active = false;};
  }, [profileId, refreshToken, retry]);

  async function clear() {
    if (!profileId || clearing || !window.confirm('Clear recently viewed jobs for this profile? This cannot be undone.')) return;
    setClearing(true);
    setClearError(false);
    try {
      await recentlyViewedClient.clear(profileId);
      setRetry(value => value + 1);
    } catch {
      setClearError(true);
    } finally {
      setClearing(false);
    }
  }

  return <RecentlyViewedPanelView
    profileId={profileId} state={state} clearing={clearing} clearError={clearError}
    onRefresh={() => setRetry(value => value + 1)} onClear={() => void clear()}
    onOpenJob={onOpenJob}
  />;
}
