import React from 'react';
import type { DiscoveryActivity } from './company-activity';

type Props = {
  rows: DiscoveryActivity[];
  loading: boolean;
  error: boolean;
  onRetry?: () => void;
};

function activityDate(value: string | null): {dateTime: string; label: string} | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return null;
  return {
    dateTime: parsed.toISOString(),
    label: new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'}).format(parsed),
  };
}

/** Presents source activity already normalized by the caller; it never fetches or interprets event payloads. */
export function CompanyDiscoveryActivity({rows, loading, error, onRetry}: Props) {
  return <section className="panel" aria-labelledby="company-discovery-activity-heading">
    <div className="toolbar"><div>
      <h2 id="company-discovery-activity-heading">Company discovery activity</h2>
      <p>Recent recorded changes to discovered companies and their job sources.</p>
    </div></div>
    {loading && <p role="status">Loading company discovery activity…</p>}
    {!loading && error && <div role="alert">
      <p>Company discovery activity could not be loaded. Try again.</p>
      {onRetry && <button type="button" className="quiet" onClick={onRetry}>Retry activity</button>}
    </div>}
    {!loading && !error && rows.length === 0 && <p className="empty">No company discovery activity has been recorded yet.</p>}
    {!loading && rows.length > 0 && <ol aria-label="Company discovery activity">
      {rows.map(row => {
        const date = activityDate(row.timestamp);
        return <li key={row.id}>
          {date ? <time dateTime={date.dateTime}>{date.label}</time> : <span className="muted">Date unavailable</span>}
          {' · '}{row.message}
        </li>;
      })}
    </ol>}
  </section>;
}
