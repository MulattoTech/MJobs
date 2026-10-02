import React from 'react';

export type CompanySourceView = {
  id: string;
  company_id: string;
  company_name: string;
  official_domain: string | null;
  career_url: string | null;
  provider: string;
  tenant: string;
  collection_strategy: string;
  confidence: number | null;
  priority: string;
  enabled: boolean;
  origin: string;
  discovered_automatically: boolean;
  status: string;
  jobs_found: number;
  relevant_jobs: number;
  last_checked: string | null;
  last_success?: string | null;
  next_check: string | null;
  provenance: {
    domain_method?: string | null;
    domain_candidates_ambiguous?: boolean;
    tenant_evidence: string[];
    events: unknown[];
    source_linked_history_available: boolean;
  };
};
export type SourceControl = 'pin' | 'enabled' | 'watch';
type Props = {
  rows: CompanySourceView[];
  busy: boolean;
  profileSelected: boolean;
  onSet: (row: CompanySourceView, control: SourceControl, value: boolean) => Promise<void>;
};

function safeLink(value: string | null): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) return null;
    return parsed.href;
  } catch { return null; }
}
function timestamp(value: string | null): string {
  if (!value) return 'Not yet';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

export function CompanySources({rows, busy, profileSelected, onSet}: Props) {
  return <section className="panel company-sources" aria-labelledby="company-sources-heading">
    <div className="toolbar">
      <div><h2 id="company-sources-heading">Companies / Sources</h2>
        <p>Inspect learned employer sources and control your saved source preferences.</p></div>
      <span className="tag">{rows.length} enrolled</span>
    </div>
    <p className="muted">Jobs Found counts distinct jobs with postings linked to this exact source.
      Relevant Jobs counts match records for the selected profile's current version, not confirmed eligibility.
      {!profileSelected && ' Select a profile to see its match counts.'}</p>
    <p className="safety">These controls save preferences. Automatic company-source scheduling and the actions
      marked unavailable are not implemented here. External application submission remains OFF.</p>
    {rows.length === 0 && <p className="empty">No company sources enrolled. Configured public boards below remain separate from learned employer sources.</p>}
    {rows.map(row => {
      const pinned = row.priority === 'USER_PINNED';
      const watched = row.origin === 'USER_WATCHED';
      const careerLink = safeLink(row.career_url);
      const confidence = row.confidence !== null && Number.isFinite(row.confidence)
        ? `${Math.round(row.confidence * 100)}%` : 'Unknown';
      return <article className="company-source-card" key={row.id} data-source-id={row.id} aria-label={`${row.company_name} source`}>
        <h3>{row.company_name}</h3>
        <dl className="company-source-facts">
          <div><dt>Company</dt><dd>{row.company_name}</dd></div>
          <div><dt>Official Domain</dt><dd>{row.official_domain || 'Not verified'}</dd></div>
          <div><dt>Careers URL</dt><dd>{careerLink
            ? <a href={careerLink} target="_blank" rel="noreferrer">{row.career_url}</a> : 'Unavailable'}</dd></div>
          <div><dt>ATS</dt><dd>{row.provider} / {row.tenant}</dd></div>
          <div><dt>Source Type</dt><dd>{row.collection_strategy} &#183; {row.origin}</dd></div>
          <div><dt>Confidence</dt><dd>{confidence}</dd></div>
          <div><dt>Priority</dt><dd>{row.priority}</dd></div>
          <div><dt>Status</dt><dd>{row.status}{!row.enabled && row.status !== 'DISABLED' ? ' \u00b7 DISABLED' : ''}</dd></div>
          <div><dt>Jobs Found</dt><dd>{row.jobs_found}</dd></div>
          <div><dt>Relevant Jobs</dt><dd>{profileSelected ? row.relevant_jobs : 0}</dd></div>
          <div><dt>Last Checked</dt><dd>{timestamp(row.last_checked)}</dd></div>
          <div><dt>Next Check</dt><dd>{row.next_check ? timestamp(row.next_check) : 'Not scheduled'}</dd></div>
        </dl>
        <div className="company-source-controls">
          <button type="button" disabled={busy} aria-label={`${pinned ? 'Unpin' : 'Pin'} ${row.company_name} source`}
            onClick={() => void onSet(row, 'pin', !pinned)}>{pinned ? 'Unpin' : 'Pin'}</button>
          <button type="button" disabled={busy} aria-label={`${row.enabled ? 'Disable' : 'Enable'} ${row.company_name} source`}
            onClick={() => void onSet(row, 'enabled', !row.enabled)}>{row.enabled ? 'Disable' : 'Enable'}</button>
          <button type="button" disabled={busy || (!row.discovered_automatically && watched)}
            aria-label={`${watched ? 'Unwatch' : 'Watch'} ${row.company_name} source`}
            onClick={() => void onSet(row, 'watch', !watched)}>{watched ? 'Unwatch' : 'Watch'}</button>
        </div>
        {!row.discovered_automatically && <p className="muted">Manually added sources remain watched. Disable the source to turn off its saved enabled preference.</p>}
        <details className="company-source-evidence">
          <summary>View Evidence</summary>
          {!row.provenance.source_linked_history_available && <p>No source-linked discovery history is available for this record. Historical evidence has not been inferred.</p>}
          <pre>{JSON.stringify(row.provenance, null, 2)}</pre>
        </details>
        <div className="company-source-controls" aria-label="Unavailable company source actions">
          {['Run Now', 'Rediscover', 'Verify', 'Edit'].map(label =>
            <button key={label} type="button" disabled title="Not implemented for this company-source view">{label} &#8212; unavailable</button>)}
        </div>
      </article>;
    })}
  </section>;
}
