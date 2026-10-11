import React from 'react';

export type CompanyDiscoveryQueueState = 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'NEEDS_REVIEW' | 'CANCELLED' | 'UNKNOWN';

export type CompanyDiscoveryQueueItem = {
  request_id: string;
  company: string;
  state: CompanyDiscoveryQueueState;
  priority: string;
  updated_at: string | null;
  attempt_count: number;
};

export type CompanyDiscoveryQueueCounts = {
  total: number;
  queued: number;
  running: number;
  succeeded: number;
  needs_review: number;
  cancelled: number;
  unknown: number;
};

export type CompanyDiscoveryQueueProps = {
  profileId: string;
  items: CompanyDiscoveryQueueItem[];
  counts: CompanyDiscoveryQueueCounts;
  loading: boolean;
  error: boolean;
  onRefresh?: () => void | Promise<void>;
};

const stateLabels: Record<CompanyDiscoveryQueueState, string> = {
  QUEUED: 'Queued',
  RUNNING: 'Running',
  SUCCEEDED: 'Completed',
  NEEDS_REVIEW: 'Needs review',
  CANCELLED: 'Cancelled',
  UNKNOWN: 'Status unavailable',
};

const countLabels: Array<[keyof CompanyDiscoveryQueueCounts, string]> = [
  ['total', 'Total'],
  ['queued', 'Queued'],
  ['running', 'Running'],
  ['succeeded', 'Succeeded'],
  ['needs_review', 'Needs review'],
  ['cancelled', 'Cancelled'],
  ['unknown', 'Unknown'],
];

function displayTime(value: string | null): string {
  if (!value) return 'Not recorded';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

export function CompanyDiscoveryQueue({
  profileId,
  items,
  counts,
  loading,
  error,
  onRefresh,
}: CompanyDiscoveryQueueProps) {
  if (!profileId) return <section className="panel company-discovery-queue" aria-label="Company discovery queue">
    <h3>Company discovery</h3>
    <p>Select a profile to view company discovery activity.</p>
  </section>;

  return <section className="panel company-discovery-queue" aria-label="Company discovery queue" aria-busy={loading}>
    <div className="toolbar">
      <div>
        <h3>Company discovery</h3>
        <p className="muted">Company source discovery runs in a separate queue, so it does not hold up job imports.</p>
      </div>
      {onRefresh && <button type="button" className="quiet" disabled={loading} onClick={() => void onRefresh()}>Refresh company discovery</button>}
    </div>

    <dl aria-label="Company discovery counts">
      {countLabels.map(([key, label]) => <div key={key}>
        <dt>{label}</dt><dd>{counts[key]}</dd>
      </div>)}
    </dl>

    {loading && <p role="status">Loading company discovery activity…</p>}
    {error && <p role="alert">Company discovery activity could not be loaded. Refresh to try again.</p>}
    {!loading && !error && items.length === 0 && counts.total === 0 && <p>No company discoveries are queued for this profile.</p>}
    {!loading && !error && items.length === 0 && counts.total > 0 && <p>No recent company discovery requests are available for this profile.</p>}

    {items.length > 0 && <ul aria-label="Company discovery requests">
      {items.map(item => <li key={item.request_id}>
        <div>
          <strong>{item.company}</strong>
          <p><span className="tag">{stateLabels[item.state] || 'Status unavailable'}</span> · {item.priority === 'HIGH' ? 'High priority' : 'Standard priority'}</p>
          <p className="muted">Updated: <time>{displayTime(item.updated_at)}</time> · {item.attempt_count} attempt{item.attempt_count === 1 ? '' : 's'}</p>
          {item.state === 'NEEDS_REVIEW' && <p className="muted">This discovery needs review before continuing.</p>}
        </div>
      </li>)}
    </ul>}
  </section>;
}
