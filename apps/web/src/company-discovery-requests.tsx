import React, { useState } from 'react';

export type DiscoveryRequest = {
  id: string;
  revision: number;
  company_name: string;
  job_title: string | null;
  reasons: string[];
  highest_match_score: number | null;
  status: string;
  reason: string | null;
  selected_website: string | null;
  authorization: string | null;
  operation_id: string | null;
  last_seen_at: string;
};

type Props = {
  rows: DiscoveryRequest[];
  busy: boolean;
  onReview: (row: DiscoveryRequest, website: string) => Promise<void>;
  onDismiss: (row: DiscoveryRequest) => Promise<void>;
};

const statusLabels: Record<string, string> = {
  NEEDS_ORIGIN_REVIEW: 'Needs website review', QUEUED: 'Queued', RUNNING: 'In progress',
  COMPLETED: 'Completed', FAILED: 'Failed — review needed', BLOCKED: 'Blocked — review needed',
  CANCELLED: 'Cancelled', DISMISSED: 'Dismissed',
};
const reasonLabels: Record<string, string> = {
  PREVIOUSLY_UNSEEN_COMPANY: 'Company not seen before', HIGH_MATCH_JOB: 'Strong job match',
  USER_SAVED_COMPANY: 'Saved company', USER_PREFERRED_COMPANY: 'Preferred company',
  NEW_ATS_TENANT: 'New job feed', REPEATED_AGGREGATOR_APPEARANCE: 'Repeated public listing',
  KNOWN_SOURCE_BROKEN: 'Existing source needs repair',
};

function plainLabel(value: string): string {
  return value.split('_').filter(Boolean).map(word => word[0] + word.slice(1).toLowerCase()).join(' ') || 'Unspecified';
}
function reasonLabel(value: string): string { return reasonLabels[value] || plainLabel(value); }
function statusLabel(value: string): string { return statusLabels[value] || 'Status unavailable'; }
function reviewable(status: string): boolean { return status === 'NEEDS_ORIGIN_REVIEW' || ['FAILED', 'BLOCKED', 'CANCELLED'].includes(status); }
function terminal(status: string): boolean { return ['COMPLETED', 'DISMISSED'].includes(status); }

export function CompanyDiscoveryRequests({rows, busy, onReview, onDismiss}: Props) {
  const [websites, setWebsites] = useState<Record<string, string>>({});
  return <section className="panel" aria-labelledby="company-discovery-requests-heading">
    <div className="toolbar"><div><h2 id="company-discovery-requests-heading">Company discovery requests</h2>
      <p>Review the latest 100 company requests raised while finding jobs. Failed or blocked attempts require an explicit review before retrying.</p></div>
      <span className="tag">{rows.length} request{rows.length === 1 ? '' : 's'}</span></div>
    {rows.length === 0 && <p className="empty">No company discovery requests need attention.</p>}
    {rows.map(row => {
      const canReview = reviewable(row.status);
      const canDismiss = row.status === 'NEEDS_ORIGIN_REVIEW';
      const website = websites[row.id] ?? row.selected_website ?? '';
      return <article className="company-source-card" key={row.id} aria-label={`${row.company_name} discovery request`}>
        <div className="toolbar"><div><h3>{row.company_name}</h3>{row.job_title && <p>{row.job_title}</p>}</div><span className="tag">{statusLabel(row.status)}</span></div>
        <p><strong>Why this was raised:</strong> {row.reasons.length ? row.reasons.map(reasonLabel).join('; ') : row.reason ? reasonLabel(row.reason) : 'No reason recorded'}</p>
        {row.highest_match_score !== null && <p><strong>Highest observed match:</strong> {row.highest_match_score}%</p>}
        {!row.selected_website && <p className="alert">A website must be reviewed before discovery can continue.</p>}
        {row.reason && <p className="muted">{reasonLabel(row.reason)}</p>}
        <p className="safety">{row.authorization === 'VERIFIED_DOMAIN' ? 'Automatic discovery uses stored employer-domain evidence. It does not submit applications.' : 'Reviewing a site does not verify employer ownership, merge identities or submit an application.'}</p>
        {canReview && <form onSubmit={event => { event.preventDefault(); void onReview(row, website); }}><div className="two-col"><label>Website to review (HTTPS)<input type="url" required pattern="https://.*" value={website} disabled={busy} onChange={event => setWebsites(current => ({...current, [row.id]: event.target.value}))} /></label>
          <div className="company-source-controls"><button type="submit" disabled={busy}>Review website and discover</button>
            {canDismiss && <button type="button" className="quiet" disabled={busy} onClick={() => void onDismiss(row)}>Dismiss request</button>}</div></div></form>}
        {!canReview && <p className="muted">{terminal(row.status) ? 'This request is complete and needs no further action.' : 'This request is already being processed.'}</p>}
      </article>;
    })}
  </section>;
}
