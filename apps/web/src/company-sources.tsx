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
  applications: number;
  last_checked: string | null;
  last_success?: string | null;
  next_check: string | null;
  source_version?: string;
  monitoring_supported?: boolean;
  monitoring?: Monitoring | null;
  collector?: {status: 'ACTIVE' | 'PAUSED' | 'REVIEW_REQUIRED' | 'NO_SCHEDULED_SEARCH'; poll_interval: number;
    identity_basis?: 'USER_CONFIRMED_DOMAIN' | 'VERIFIED_DOMAIN'} | null;
  quality?: SourceQualityValue & {link_verification?: LinkVerification | null};
  provenance: {
    domain_method?: string | null;
    domain_candidates_ambiguous?: boolean;
    tenant_evidence: string[];
    events: unknown[];
    source_linked_history_available: boolean;
  };
  company_contexts?: CompanyContext[];
};
type CompanyContext = {
  id: string; version: number; content: string; content_origin: 'USER_PROVIDED';
  content_status: 'UNVERIFIED';
  source_url: string; identity_status: 'VERIFIED' | 'UNVERIFIED';
  content_sha256: string; user_reviewed: boolean; user_approved: boolean;
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

function CompanyContextPanel({row}: {row: CompanySourceView}) {
  const [contexts, setContexts] = useState<CompanyContext[]>(row.company_contexts || []);
  const [text, setText] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [acknowledged, setAcknowledged] = useState<Record<string, boolean>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const requestId = useRef<string | null>(null);

  useEffect(() => { setContexts(row.company_contexts || []); }, [row.company_contexts]);

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !text.trim() || !sourceUrl.trim()) return;
    requestId.current ||= crypto.randomUUID();
    setSaving(true); setError('');
    try {
      const value = await api<CompanyContext>(`/company-sources/${encodeURIComponent(row.id)}/contexts`, {
        method: 'POST', body: JSON.stringify({text: text.trim(), source_url: sourceUrl.trim(), request_id: requestId.current}),
      });
      setContexts(current => [value, ...current.filter(item => item.id !== value.id)]);
      setText(''); setSourceUrl(''); requestId.current = null;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Company context could not be saved.');
    } finally { setSaving(false); }
  }

  async function review(context: CompanyContext, approved: boolean) {
    setSaving(true); setError('');
    try {
      const value = await api<CompanyContext>(
        `/company-sources/${encodeURIComponent(row.id)}/contexts/${encodeURIComponent(context.id)}/review`,
        {method: 'POST', body: JSON.stringify({approved, content_sha256: context.content_sha256})});
      setContexts(current => current.map(item => item.id === value.id ? value : item));
      setAcknowledged(current => ({...current, [context.id]: false}));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Company context review could not be saved.');
    } finally { setSaving(false); }
  }

  return <section className="company-context" aria-label={`Company context for ${row.company_name}`}>
    <h4>Company context</h4>
    <p className="muted">Context is user-provided and unverified until its source identity is verified and you review and approve it.</p>
    {contexts.length === 0 ? <p className="muted">No company context saved.</p> :
      <ol>{contexts.map(context => <li key={context.id}>
        <p>{context.content}</p>
        <p><small>{context.content_origin} · CONTENT {context.content_status} · SOURCE IDENTITY {context.identity_status} · Version {context.version} · Source: {context.source_url}</small></p>
        {context.identity_status !== 'VERIFIED' && <p role="status">Unverified source identity; this context will not be used in letters.</p>}
        {context.user_approved
          ? <p role="status">Reviewed and approved for job-specific letters.</p>
          : <>
            <label><input type="checkbox" checked={!!acknowledged[context.id]}
              onChange={event => setAcknowledged(current => ({...current, [context.id]: event.target.checked}))}
              disabled={saving || context.identity_status !== 'VERIFIED'}/>
              I reviewed this USER_PROVIDED context and approve its use in job-specific letters.
            </label>
            <button type="button" disabled={saving || context.identity_status !== 'VERIFIED' || !acknowledged[context.id]}
              onClick={() => void review(context, true)}>Approve context</button>
          </>}
      </li>)}</ol>}
    <form onSubmit={event => void save(event)}>
      <label>Context text from the source<textarea aria-label={`Company context for ${row.company_name}`}
        maxLength={4000} value={text} onChange={event => setText(event.target.value)} disabled={saving}/></label>
      <label>Source URL<input aria-label={`Company context source URL for ${row.company_name}`} type="url"
        maxLength={2000} value={sourceUrl} onChange={event => setSourceUrl(event.target.value)} disabled={saving}/></label>
      <button type="submit" disabled={saving || !text.trim() || !sourceUrl.trim()}>Save user-provided context</button>
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
      const preference = [pinned ? 'Pinned' : 'Not pinned', row.enabled ? 'Enabled' : 'Disabled',
        watched ? 'Watched' : 'Not watched'].join(' · ');
      const careerLink = safeLink(row.career_url);
      const activity = discoveryActivity(row.provenance.events);
      const confidence = row.confidence !== null && Number.isFinite(row.confidence)
        ? `${Math.round(row.confidence * 100)}%` : 'Unknown';
      return <article className="company-source-card" key={`${row.profile_id || ''}:${row.company_id}:${row.id}`} data-source-id={row.id} aria-label={`${row.company_name} source`}>
        <h3>{row.company_name}</h3>
        <dl className="company-source-facts">
          <div><dt>Company</dt><dd>{row.company_name}</dd></div>
          <div><dt>Domain</dt><dd>{row.official_domain || 'Not verified'}</dd></div>
          <div><dt>Career Site</dt><dd>{careerLink
            ? <a href={careerLink} target="_blank" rel="noreferrer">{row.career_url}</a> : 'Unavailable'}</dd></div>
          <div><dt>ATS</dt><dd>{row.provider} / {row.tenant}</dd></div>
          <div><dt>Source Type</dt><dd>{row.collection_strategy} &#183; {row.origin}</dd></div>
          <div><dt>Confidence</dt><dd>{confidence}</dd></div>
          <div><dt>Source Priority</dt><dd>{row.priority}</dd></div>
          <div><dt>Status</dt><dd>{row.status}{!row.enabled && row.status !== 'DISABLED' ? ' \u00b7 DISABLED' : ''}</dd></div>
          <div><dt>Jobs Found</dt><dd>{row.jobs_found}</dd></div>
          <div><dt>Relevant Jobs</dt><dd>{profileSelected ? row.relevant_jobs : 0}</dd></div>
          <div><dt>Applications</dt><dd>{row.applications}</dd></div>
          <div><dt>User Preference</dt><dd>{preference}</dd></div>
          <div><dt>Last Checked</dt><dd>{timestamp(row.last_checked)}</dd></div>
          <div><dt>Next Check</dt><dd>{row.next_check ? timestamp(row.next_check)
            : row.collector?.status === 'ACTIVE' ? 'Scheduled by saved searches' : 'Not scheduled'}</dd></div>
        </dl>
        <SourceLinkEvidence evidence={row.quality?.link_verification} />
        <SourceQuality quality={row.quality} />
        <CompanyContextPanel key={`${row.id}:company-context`} row={row} />
        {row.collector && <section aria-label={`Automatic collection for ${row.company_name}`}>
          <h4>Automatic collection</h4>
          {row.collector.identity_basis === 'USER_CONFIRMED_DOMAIN' &&
            <p>User-confirmed employer identity. Not independently verified.</p>}
          <p>{{ACTIVE:'Active', PAUSED:'Paused', REVIEW_REQUIRED:'Employer verification needs review',
            NO_SCHEDULED_SEARCH:'No enabled saved search schedules this source'}[row.collector.status]}</p>
          <p>Minimum collection interval: {row.collector.poll_interval} seconds. Saved searches control recurring discovery.</p>
        </section>}
        {!row.collector && onMonitor && onRun && <CompanySourceMonitoring sourceId={row.id} sourceVersion={row.source_version || ''}
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
