import React, { useEffect, useRef, useState } from 'react';

export type SourceCandidateState =
  | 'DISCOVERED' | 'NEEDS_REVIEW' | 'INTEGRATION_QUEUED' | 'INTEGRATED' | 'BLOCKED' | 'REJECTED';
export type SourceCandidateSiteType = 'JOB_BOARD' | 'EMPLOYER' | 'ATS' | 'UNKNOWN';
export type SourceCandidateEvidenceStatus = 'PUBLIC_DOCUMENTED' | 'UNKNOWN' | 'BLOCKED';
export type SourceCandidateReviewState = 'NEEDS_REVIEW' | 'INTEGRATION_QUEUED' | 'BLOCKED' | 'REJECTED';

export type SourceCandidateReview = {
  id: string;
  profile_id: string;
  state: SourceCandidateReviewState;
  site_type: SourceCandidateSiteType;
  reason: string;
  api_feed_evidence_status: SourceCandidateEvidenceStatus;
  api_feed_evidence_reference: string | null;
  api_feed_evidence_note: string;
  access_terms_note: string;
  capabilities_limits_note: string;
  expected_value_maintenance_note: string;
  integration_issue: number | null;
  integration_issue_status: 'USER_SUPPLIED_UNVERIFIED' | 'NOT_LINKED';
  reviewed_at: string;
};

export type SourceCandidateObservation = {
  id: string;
  job_id: string | null;
  application_id: string | null;
  job_context: {id: string; title: string; company: string} | null;
  application_context: {id: string; state: string} | null;
  discovery_origin: string | null;
  origin_attribution: string;
  source_url: string | null;
  application_url: string | null;
  observed_hops: (string | { state: string })[];
  capture_method: string;
  evidence_status: string;
  observed_at: string;
};

export type SourceCandidate = {
  id: string;
  profile_id: string;
  domain: string;
  display_name: string;
  site_type: SourceCandidateSiteType;
  state: SourceCandidateState;
  first_seen_at: string;
  last_seen_at: string;
  collection_enabled: boolean;
  review_reason: string | null;
  integration_issue: number | null;
  integration_issue_status: 'USER_SUPPLIED_UNVERIFIED' | 'NOT_LINKED';
  observations: SourceCandidateObservation[];
  review_history: SourceCandidateReview[];
  blockers?: string[];
};

export type SourceCandidateReviewDraft = {
  site_type: SourceCandidateSiteType;
  reason: string;
  api_feed_evidence_status: SourceCandidateEvidenceStatus;
  api_feed_evidence_reference: string;
  api_feed_evidence_note: string;
  access_terms_note: string;
  capabilities_limits_note: string;
  expected_value_maintenance_note: string;
  integration_issue: string;
};

export type SourceCandidateReviewInput = Omit<SourceCandidateReviewDraft,
  'api_feed_evidence_reference' | 'integration_issue'> & {
    state: SourceCandidateReviewState;
    api_feed_evidence_reference: string | null;
    integration_issue: number | null;
    request_id: string;
  };

export type SourceCandidateReviewApi = {
  review: (profileId: string, candidateId: string, input: SourceCandidateReviewInput) => Promise<SourceCandidate>;
};

export type SourceCandidateReviewQueueProps = {
  profileId: string | null;
  candidates: SourceCandidate[];
  api: SourceCandidateReviewApi;
  loading?: boolean;
  error?: string | null;
  onRetry: () => void;
  busy?: boolean;
  onCandidateUpdated?: (candidate: SourceCandidate) => void;
  onOpenJob?: (jobId: string) => void;
  onOpenApplication?: (applicationId: string) => void;
};

const reviewStates: { state: SourceCandidateReviewState; label: string }[] = [
  { state: 'NEEDS_REVIEW', label: 'Keep needs review' },
  { state: 'INTEGRATION_QUEUED', label: 'Queue integration review' },
  { state: 'BLOCKED', label: 'Mark blocked' },
  { state: 'REJECTED', label: 'Reject candidate' },
];

function safeHttpUrl(value: string): boolean {
  try {
    const url = new URL(value);
    return (url.protocol === 'http:' || url.protocol === 'https:') && !!url.hostname &&
      !url.username && !url.password;
  } catch {
    return false;
  }
}

export function buildSourceCandidateReviewPayload(
  draft: SourceCandidateReviewDraft,
  state: SourceCandidateReviewState,
  requestId: string,
): SourceCandidateReviewInput {
  const reason = draft.reason.trim();
  const notes = [draft.api_feed_evidence_note, draft.access_terms_note,
    draft.capabilities_limits_note, draft.expected_value_maintenance_note].map(value => value.trim());
  if (!reason || reason.length > 1000 || notes.some(value => !value || value.length > 1000)) {
    throw new Error('Complete the review reason and all four source evaluation notes.');
  }
  const reference = draft.api_feed_evidence_reference.trim();
  if (reference && (reference.length > 2000 || !safeHttpUrl(reference))) {
    throw new Error('Evidence reference must be a safe HTTP or HTTPS URL.');
  }
  if (draft.api_feed_evidence_status === 'PUBLIC_DOCUMENTED' && !reference) {
    throw new Error('Public documented API or feed evidence requires a reference URL.');
  }
  const issueText = draft.integration_issue.trim();
  let integrationIssue: number | null = null;
  if (issueText && state === 'INTEGRATION_QUEUED') {
    integrationIssue = Number(issueText);
    if (!Number.isSafeInteger(integrationIssue) || integrationIssue < 1 || integrationIssue > 2147483647) {
      throw new Error('Enter a positive whole integration issue number. The reference is not checked externally.');
    }
  }
  if (state === 'INTEGRATION_QUEUED' && integrationIssue === null) {
    throw new Error('An existing provider issue is required to queue integration.');
  }
  if (!requestId.trim() || requestId !== requestId.trim()) {
    throw new Error('Review request identifier is invalid.');
  }
  return {
    state,
    site_type: draft.site_type,
    reason,
    api_feed_evidence_status: draft.api_feed_evidence_status,
    api_feed_evidence_reference: reference || null,
    api_feed_evidence_note: notes[0],
    access_terms_note: notes[1],
    capabilities_limits_note: notes[2],
    expected_value_maintenance_note: notes[3],
    integration_issue: integrationIssue,
    request_id: requestId,
  };
}

export async function requestSourceCandidateReview(
  api: SourceCandidateReviewApi,
  profileId: string,
  candidate: SourceCandidate,
  draft: SourceCandidateReviewDraft,
  state: SourceCandidateReviewState,
  requestId: string,
): Promise<SourceCandidate> {
  if (!profileId || candidate.profile_id !== profileId) {
    throw new Error('Source candidate does not belong to the selected profile.');
  }
  return api.review(profileId, candidate.id, buildSourceCandidateReviewPayload(draft, state, requestId));
}

function initialDraft(candidate: SourceCandidate): SourceCandidateReviewDraft {
  const latest = candidate.review_history.at(-1);
  return {
    site_type: latest?.site_type ?? candidate.site_type,
    reason: candidate.review_reason ?? latest?.reason ?? '',
    api_feed_evidence_status: latest?.api_feed_evidence_status ?? 'UNKNOWN',
    api_feed_evidence_reference: latest?.api_feed_evidence_reference ?? '',
    api_feed_evidence_note: latest?.api_feed_evidence_note ?? '',
    access_terms_note: latest?.access_terms_note ?? '',
    capabilities_limits_note: latest?.capabilities_limits_note ?? '',
    expected_value_maintenance_note: latest?.expected_value_maintenance_note ?? '',
    integration_issue: candidate.integration_issue === null ? '' : String(candidate.integration_issue),
  };
}

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value || 'Unknown' : date.toISOString();
}

function hopLabel(hop: string | { state: string }): string {
  const value = typeof hop === 'string' ? hop : hop.state;
  return value === 'UNKNOWN' ? 'Unknown hop' : value;
}

function blockersFor(candidate: SourceCandidate): string[] {
  if (candidate.blockers) return candidate.blockers;
  const latest = candidate.review_history.at(-1);
  const blockers: string[] = [];
  if (!latest) blockers.push('No review outcome recorded.');
  if ((latest?.api_feed_evidence_status ?? 'UNKNOWN') === 'UNKNOWN') {
    blockers.push('Public API or feed support is not verified.');
  }
  if (candidate.site_type === 'UNKNOWN' && !latest) blockers.push('Source kind is unknown.');
  if (latest?.api_feed_evidence_status === 'BLOCKED') blockers.push('Public API/feed access is blocked.');
  if (candidate.state === 'INTEGRATION_QUEUED' && candidate.integration_issue === null) {
    blockers.push('No existing provider issue is linked.');
  }
  return blockers;
}

function completeDraft(draft: SourceCandidateReviewDraft, state: SourceCandidateReviewState): boolean {
  const required = [draft.reason, draft.api_feed_evidence_note, draft.access_terms_note,
    draft.capabilities_limits_note, draft.expected_value_maintenance_note];
  if (!required.every(value => !!value.trim())) return false;
  const reference = draft.api_feed_evidence_reference.trim();
  if (draft.api_feed_evidence_status === 'PUBLIC_DOCUMENTED' && !safeHttpUrl(reference)) return false;
  if (reference && !safeHttpUrl(reference)) return false;
  if (draft.integration_issue.trim() && state === 'INTEGRATION_QUEUED') {
    const value = Number(draft.integration_issue);
    if (!Number.isSafeInteger(value) || value < 1 || value > 2147483647) return false;
  }
  if (state === 'INTEGRATION_QUEUED' && !draft.integration_issue.trim()) return false;
  return true;
}

export function SourceCandidateReviewQueue({
  profileId,
  candidates,
  api,
  loading = false,
  error = null,
  onRetry,
  busy = false,
  onCandidateUpdated,
  onOpenJob,
  onOpenApplication,
}: SourceCandidateReviewQueueProps) {
  const [drafts, setDrafts] = useState<Record<string, SourceCandidateReviewDraft>>({});
  const [updatedCandidates, setUpdatedCandidates] = useState<Record<string, SourceCandidate>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failedStates, setFailedStates] = useState<Record<string, SourceCandidateReviewState | undefined>>({});
  const [pendingId, setPendingId] = useState<string | null>(null);
  const requestIds = useRef<Record<string, string>>({});
  const selectionGeneration = useRef(0);

  useEffect(() => {
    const generation = ++selectionGeneration.current;
    setDrafts({});
    setUpdatedCandidates({});
    setErrors({});
    setFailedStates({});
    setPendingId(null);
    requestIds.current = {};
    return () => {
      if (selectionGeneration.current === generation) selectionGeneration.current += 1;
    };
  }, [profileId]);

  if (!profileId) return <section className="panel" aria-labelledby="source-candidate-review-heading">
    <h2 id="source-candidate-review-heading">Source candidate review queue</h2>
    <p>Select a profile to review its source candidates.</p>
  </section>;

  if (loading) return <section className="panel" aria-labelledby="source-candidate-review-heading" aria-busy="true">
    <h2 id="source-candidate-review-heading">Source candidate review queue</h2>
    <p role="status">Loading source candidates…</p>
  </section>;

  if (error) return <section className="panel" aria-labelledby="source-candidate-review-heading">
    <h2 id="source-candidate-review-heading">Source candidate review queue</h2>
    <p role="alert">{error}</p>
    <button type="button" onClick={onRetry}>Retry loading candidates</button>
  </section>;

  const rows = candidates
    .filter(candidate => candidate.profile_id === profileId)
    .map(candidate => updatedCandidates[candidate.id] ?? candidate);
  const updateDraft = (candidate: SourceCandidate, field: keyof SourceCandidateReviewDraft, value: string) => {
    setDrafts(current => ({
      ...current,
      [candidate.id]: { ...(current[candidate.id] ?? initialDraft(candidate)), [field]: value },
    }));
    delete requestIds.current[candidate.id];
    setErrors(current => ({ ...current, [candidate.id]: '' }));
    setFailedStates(current => { const next = { ...current }; delete next[candidate.id]; return next; });
  };
  const submitReview = async (candidate: SourceCandidate, state: SourceCandidateReviewState) => {
    const draft = drafts[candidate.id] ?? initialDraft(candidate);
    const requestId = requestIds.current[candidate.id] ?? crypto.randomUUID();
    const generation = selectionGeneration.current;
    requestIds.current[candidate.id] = requestId;
    setPendingId(candidate.id);
    setErrors(current => ({ ...current, [candidate.id]: '' }));
    try {
      const updated = await requestSourceCandidateReview(api, profileId, candidate, draft, state, requestId);
      if (selectionGeneration.current !== generation) return;
      setUpdatedCandidates(current => ({ ...current, [candidate.id]: updated }));
      setDrafts(current => ({ ...current, [candidate.id]: initialDraft(updated) }));
      delete requestIds.current[candidate.id];
      setFailedStates(current => { const next = { ...current }; delete next[candidate.id]; return next; });
      onCandidateUpdated?.(updated);
    } catch (reviewError) {
      if (selectionGeneration.current !== generation) return;
      setErrors(current => ({ ...current, [candidate.id]: reviewError instanceof Error ? reviewError.message : 'Review could not be saved. Retry with the same criteria.' }));
      setFailedStates(current => ({ ...current, [candidate.id]: state }));
    } finally {
      if (selectionGeneration.current === generation) setPendingId(null);
    }
  };

  return <section className="panel source-candidate-review-queue" aria-labelledby="source-candidate-review-heading">
    <div className="toolbar"><div><h2 id="source-candidate-review-heading">Source candidate review queue</h2>
      <p>Review recorded evidence and source suitability. Reviewing a candidate does not inspect the site or enable collection.</p></div>
      <span className="tag">{rows.length} candidate{rows.length === 1 ? '' : 's'}</span></div>
    {rows.length === 0 ? <p className="empty">No source candidates need review for this profile.</p> :
      <ul aria-label="Source candidates to review" className="source-candidate-review-list">
        {rows.map(candidate => {
          const draft = drafts[candidate.id] ?? initialDraft(candidate);
          const errorText = errors[candidate.id];
          const failedState = failedStates[candidate.id];
          const pending = busy || pendingId !== null;
          const blockers = blockersFor(candidate);
          return <li key={candidate.id}>
            <article className="fact source-candidate-review-card" aria-label={`Source candidate ${candidate.domain}`}>
              <header className="toolbar"><div><h3>{candidate.display_name}</h3><p><code>{candidate.domain}</code></p></div>
                <span className="tag">Review state: {candidate.state.replaceAll('_', ' ')}</span></header>
              <dl className="source-candidate-summary">
                <div><dt>Site type</dt><dd>{candidate.site_type.replaceAll('_', ' ')}</dd></div>
                <div><dt>First seen</dt><dd><time dateTime={candidate.first_seen_at}>{displayTime(candidate.first_seen_at)}</time></dd></div>
                <div><dt>Last seen</dt><dd><time dateTime={candidate.last_seen_at}>{displayTime(candidate.last_seen_at)}</time></dd></div>
                <div><dt>Collection state</dt><dd>{candidate.collection_enabled ? 'Enabled outside this review' : 'Disabled'}</dd></div>
              </dl>
              {candidate.collection_enabled
                ? <p className="alert">Collection is already enabled separately. Review actions here do not change collection state.</p>
                : <p className="safety">Collection is disabled. Review does not enable collection.</p>}
              {candidate.integration_issue === null
                ? <p>No integration task linked.</p>
                : <p>Integration issue #{candidate.integration_issue} · user supplied; not externally verified ({candidate.integration_issue_status}).</p>}
              <h4>Blockers and unknowns</h4>
              {blockers.length ? <ul>{blockers.map((blocker, index) => <li key={`${index}-${blocker}`}>{blocker}</li>)}</ul> : <p>No recorded blockers.</p>}
              <h4>Evidence and discovery history</h4>
              {candidate.observations.length ? <ol className="source-candidate-observations">
                {candidate.observations.map(observation => <li key={observation.id}>
                  <p>{observation.capture_method.replaceAll('_', ' ')} · Evidence status: {observation.evidence_status} · <time dateTime={observation.observed_at}>{displayTime(observation.observed_at)}</time></p>
                  <p>Discovery origin ({observation.origin_attribution.replaceAll('_', ' ').toLowerCase()}): {observation.discovery_origin || 'Unknown'}</p>
                  <p>Source URL: <code>{observation.source_url || 'Unknown'}</code></p>
                  <p>Application destination: <code>{observation.application_url || 'Unknown'}</code></p>
                  <p>Observed hops: {observation.observed_hops.length ? observation.observed_hops.map(hopLabel).join(' → ') : 'Unknown hops'}</p>
                  <p>Linked job: {observation.job_context
                    ? `${observation.job_context.title} at ${observation.job_context.company} (${observation.job_context.id})`
                    : observation.job_id || 'None'} · Linked application: {observation.application_context
                    ? `${observation.application_context.state.replaceAll('_', ' ')} (user reported, ${observation.application_context.id})`
                    : observation.application_id || 'None'}</p>
                  {(observation.job_context && onOpenJob) ||
                  (observation.application_context && onOpenApplication) ? <div className="toolbar" aria-label="Open linked records">
                    {observation.job_context && onOpenJob && <button type="button"
                      onClick={() => onOpenJob(observation.job_context!.id)}>
                      Open job: {observation.job_context.title} at {observation.job_context.company}
                    </button>}
                    {observation.application_context && onOpenApplication && <button type="button"
                      onClick={() => onOpenApplication(observation.application_context!.id)}>
                      Open application: {observation.application_context.state.replaceAll('_', ' ')}
                    </button>}
                  </div> : null}
                </li>)}
              </ol> : <p>No evidence observations recorded.</p>}
              {!!candidate.review_history.length && <details>
                <summary>Review history ({candidate.review_history.length})</summary>
                <ol>{candidate.review_history.map((review, index) => <li key={`${review.id}-${index}`}>
                  <time dateTime={review.reviewed_at}>{displayTime(review.reviewed_at)}</time> · {review.state.replaceAll('_', ' ')} · {review.site_type.replaceAll('_', ' ')} · {review.api_feed_evidence_status.replaceAll('_', ' ')} · {review.reason}.
                  {review.integration_issue === null ? ' No integration task linked.' : ` Integration issue #${review.integration_issue} is user supplied and unverified.`}
                  {' '}Public API or feed reference: <code>{review.api_feed_evidence_reference ?? 'Unknown'}</code>.
                  {' '}Evidence: {review.api_feed_evidence_note} Access and terms: {review.access_terms_note} Capabilities and limits: {review.capabilities_limits_note} Value and maintenance: {review.expected_value_maintenance_note}
                </li>)}</ol>
              </details>}
              <div className="source-candidate-review-form">
                <h4>Record review outcome</h4>
                <p>Review records your assessment only. It does not authorize collection.</p>
                <label>Reviewed source kind<select aria-label={`Site type assessment for ${candidate.domain}`} value={draft.site_type} disabled={pending}
                  onChange={event => updateDraft(candidate, 'site_type', event.currentTarget.value as SourceCandidateSiteType)}>
                  <option value="UNKNOWN">Unknown</option><option value="JOB_BOARD">Job board or aggregator</option>
                  <option value="EMPLOYER">Employer source</option><option value="ATS">ATS or application destination</option>
                </select></label>
                <label>Review outcome reason<textarea aria-label={`Review reason for ${candidate.domain}`} maxLength={1000} value={draft.reason} disabled={pending}
                  onChange={event => updateDraft(candidate, 'reason', event.currentTarget.value)} /></label>
                <label>Integration task issue number (required to queue integration)<input aria-label={`Integration task issue number for ${candidate.domain}`} type="number" min="1" max="2147483647" step="1" value={draft.integration_issue} disabled={pending}
                  onChange={event => updateDraft(candidate, 'integration_issue', event.currentTarget.value)} /></label>
                <p className="muted">Only a user-supplied issue number is recorded; the reference is not checked externally.</p>
                <label>Public API or feed evidence status<select aria-label={`API or feed evidence status for ${candidate.domain}`} value={draft.api_feed_evidence_status} disabled={pending}
                  onChange={event => updateDraft(candidate, 'api_feed_evidence_status', event.currentTarget.value as SourceCandidateEvidenceStatus)}>
                  <option value="UNKNOWN">Unknown / not verified</option><option value="PUBLIC_DOCUMENTED">Public documented evidence</option><option value="BLOCKED">Blocked or access denied</option>
                </select></label>
                <label>Public documentation or feed reference<input aria-label={`API or feed evidence reference for ${candidate.domain}`} type="url" maxLength={2000} value={draft.api_feed_evidence_reference} disabled={pending}
                  onChange={event => updateDraft(candidate, 'api_feed_evidence_reference', event.currentTarget.value)} /></label>
                <label>Evidence note<textarea aria-label={`API or feed evidence note for ${candidate.domain}`} maxLength={1000} value={draft.api_feed_evidence_note} disabled={pending}
                  onChange={event => updateDraft(candidate, 'api_feed_evidence_note', event.currentTarget.value)} /></label>
                <label>Access and terms note<textarea aria-label={`Access and terms note for ${candidate.domain}`} maxLength={1000} value={draft.access_terms_note} disabled={pending}
                  onChange={event => updateDraft(candidate, 'access_terms_note', event.currentTarget.value)} /></label>
                <label>Capabilities and limits note<textarea aria-label={`Capabilities and limits note for ${candidate.domain}`} maxLength={1000} value={draft.capabilities_limits_note} disabled={pending}
                  onChange={event => updateDraft(candidate, 'capabilities_limits_note', event.currentTarget.value)} /></label>
                <label>Expected value and maintenance note<textarea aria-label={`Expected value and maintenance note for ${candidate.domain}`} maxLength={1000} value={draft.expected_value_maintenance_note} disabled={pending}
                  onChange={event => updateDraft(candidate, 'expected_value_maintenance_note', event.currentTarget.value)} /></label>
                {pending && <p role="status">Saving source candidate review…</p>}
                {errorText && <p role="alert">{errorText}</p>}
                <div className="toolbar">
                  {failedState ? <button type="button" disabled={pending || !completeDraft(draft, failedState)} onClick={() => void submitReview(candidate, failedState)}>Retry review</button> :
                    reviewStates.map(option => <button key={option.state} type="button" disabled={pending || !completeDraft(draft, option.state)}
                      onClick={() => void submitReview(candidate, option.state)}>{option.label}</button>)}
                </div>
              </div>
            </article>
          </li>;
        })}
      </ul>}
  </section>;
}
