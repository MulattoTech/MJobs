import React, { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { discoveryActivity } from './company-activity';
import { SourceLinkEvidence, type LinkVerification } from './source-link-evidence';
import { CompanySourceMonitoring, type Monitoring } from './company-source-monitoring';
import { SourceQuality, type SourceQualityValue } from './source-quality';

export type CompanySourceView = {
  id: string;
  company_id: string;
  profile_id?: string | null;
  profile_company_associated?: boolean;
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
  source_version?: string;
  monitoring_supported?: boolean;
  monitoring?: Monitoring | null;
  quality?: SourceQualityValue & {link_verification?: LinkVerification | null};
  provenance: {
    domain_method?: string | null;
    domain_candidates_ambiguous?: boolean;
    tenant_evidence: string[];
    events: unknown[];
    source_linked_history_available: boolean;
  };
};
type CompanyNote = {id: string; text: string; sequence: number; timestamp: string};
type CompanyTracking = {notes: CompanyNote[]; history: {type: 'note'; id: string; text: string; sequence: number; timestamp: string}[]};
export type SourceControl = 'pin' | 'enabled' | 'watch';
type Props = {
  rows: CompanySourceView[];
  busy: boolean;
  profileSelected: boolean;
  onSet: (row: CompanySourceView, control: SourceControl, value: boolean) => Promise<void>;
  onMonitor?: (row: CompanySourceView, interval: number) => Promise<void>;
  onRun?: (row: CompanySourceView) => Promise<void>;
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

function CompanyNotes({row}: {row: CompanySourceView}) {
  const [notes, setNotes] = useState<CompanyNote[]>([]);
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef<string | null>(null);
  const path = `/profiles/${encodeURIComponent(row.profile_id || '')}/companies/${encodeURIComponent(row.company_id)}/tracking`;

  useEffect(() => {
    let current = true;
    setNotes([]);
    setDraft('');
    setError('');
    setLoading(true);
    requestId.current = null;
    void api<CompanyTracking>(path).then(value => {
      if (current) setNotes(value.notes);
    }).catch(reason => {
      if (current) setError(reason instanceof Error ? reason.message : 'Company notes could not be loaded.');
    }).finally(() => { if (current) setLoading(false); });
    return () => { current = false; };
  }, [path]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const text = draft.trim();
    if (!row.profile_id || !text || text.length > 4000 || saving) return;
    requestId.current ||= crypto.randomUUID();
    setSaving(true);
    setError('');
    try {
      const value = await api<CompanyTracking>(path + '/notes', {method: 'POST', body: JSON.stringify({
        text, request_id: requestId.current,
      })});
      setNotes(value.notes);
      setDraft('');
      requestId.current = null;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Company note could not be saved.');
    } finally { setSaving(false); }
  }

  return <section className="company-notes" aria-label={`Notes for ${row.company_name}`}>
    <h4>Company notes</h4>
    {loading ? <p role="status">Loading company notes…</p> : notes.length === 0
      ? <p className="muted">No notes saved for this profile and company.</p>
      : <ol>{notes.map(note => <li key={note.id}><p>{note.text}</p>
          <small>Note {note.sequence} · {timestamp(note.timestamp)}</small></li>)}</ol>}
    <form onSubmit={event => void submit(event)}>
      <label>Add a private note<textarea aria-label={`Add a note for ${row.company_name}`} maxLength={4000}
        value={draft} onChange={event => setDraft(event.target.value)} disabled={saving || loading}/></label>
      <button type="submit" disabled={saving || loading || !draft.trim()}>Save note</button>
    </form>
    {error && <p role="alert">{error}</p>}
  </section>;
}

export function CompanySources({rows, busy, profileSelected, onSet, onMonitor, onRun}: Props) {
  return <section className="panel company-sources" aria-labelledby="company-sources-heading">
    <div className="toolbar">
      <div><h2 id="company-sources-heading">Companies / Sources</h2>
        <p>Inspect learned employer sources and control your saved source preferences.</p></div>
      <span className="tag">{rows.length} enrolled</span>
    </div>
    <p className="muted">Jobs Found counts distinct jobs with postings linked to this exact source.
      Relevant Jobs counts match records for the selected profile's current version, not confirmed eligibility.
      {!profileSelected && ' Select a profile to see its match counts.'}</p>
    <p className="safety">Reviewed public feeds can be monitored below. Automatic checks require an enabled search profile covering all sources.
      Other source types and actions marked unavailable remain unsupported. External application submission remains OFF.</p>
    {rows.length === 0 && <p className="empty">No company sources enrolled. Configured public boards below remain separate from learned employer sources.</p>}
    {rows.map(row => {
      const pinned = row.priority === 'USER_PINNED';
      const watched = row.origin === 'USER_WATCHED';
      const careerLink = safeLink(row.career_url);
      const activity = discoveryActivity(row.provenance.events);
      const confidence = row.confidence !== null && Number.isFinite(row.confidence)
        ? `${Math.round(row.confidence * 100)}%` : 'Unknown';
      return <article className="company-source-card" key={`${row.profile_id || ''}:${row.company_id}:${row.id}`} data-source-id={row.id} aria-label={`${row.company_name} source`}>
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
        <SourceLinkEvidence evidence={row.quality?.link_verification} />
        <SourceQuality quality={row.quality} />
        {onMonitor && onRun && <CompanySourceMonitoring sourceId={row.id} sourceVersion={row.source_version || ''}
          sourceEnabled={row.enabled} supported={!!row.monitoring_supported} monitoring={row.monitoring || null}
          busy={busy} profileSelected={profileSelected} onMonitor={interval=>onMonitor(row,interval)} onRun={()=>onRun(row)}/>}
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
        <section className="company-source-history" aria-label={`Recent discovery for ${row.company_name}`}>
          <h4>Recent discovery</h4>
          {activity.length === 0 ? <p className="muted">No source-linked activity recorded.</p> :
            <ol className="company-source-activity">{activity.map(event => <li key={event.id}>
              <p>{event.message}</p>
              <small>{event.timestamp ? <time dateTime={event.timestamp}>{timestamp(event.timestamp)}</time> : 'Time not recorded'}</small>
            </li>)}</ol>}
        </section>
        {row.profile_id && row.profile_company_associated !== false
          ? <CompanyNotes key={`${row.profile_id}:${row.company_id}`} row={row} />
          : row.profile_id && <p className="muted">Company notes are available after this company is associated with the selected profile.</p>}
        <details className="company-source-evidence">
          <summary>View Evidence</summary>
          {!row.provenance.source_linked_history_available && <p>No source-linked discovery history is available for this record. Historical evidence has not been inferred.</p>}
          <pre>{JSON.stringify(row.provenance, null, 2)}</pre>
        </details>
        <div className="company-source-controls" aria-label="Unavailable company source actions">
          {(row.monitoring_supported && onRun ? ['Rediscover', 'Verify', 'Edit'] : ['Run Now', 'Rediscover', 'Verify', 'Edit']).map(label =>
            <button key={label} type="button" disabled title="Not implemented for this company-source view">{label} &#8212; unavailable</button>)}
        </div>
      </article>;
    })}
  </section>;
}
