import React, {useEffect, useRef, useState} from 'react';

export type ProtectedResumeTokenType = 'DATE' | 'NUMBER' | 'NAMED_CREDENTIAL';
export type ProtectedResumeToken = {type: ProtectedResumeTokenType; value: string};
export type ProtectedResumeTokenAudit = {
  required: ProtectedResumeToken[];
  retained: ProtectedResumeToken[];
  missing: ProtectedResumeToken[];
};
export type ResumeRewriteReviewFact = {
  fact_id: string;
  kind: string;
  text: string;
  source: string;
  confidence: number;
  verified: boolean;
  user_approved: boolean;
};
export type ResumeRewriteProposal = {fact_id: string; text: string; reviewed?: boolean};
export type ResumeRewritePreviewRow = {
  fact_id: string;
  fact_kind: string;
  source_text: string;
  source_sha256: string;
  proposed_text: string;
  protected_token_audit: ProtectedResumeTokenAudit;
};
export type ResumeRewriteReviewPreviewResponse = {
  profile_id: string;
  profile_version: string;
  job_id: string;
  job_fingerprint: string;
  selected_fact_ids: string[];
  read_only: true;
  review_required: true;
  semantic_equivalence: 'USER_REVIEW_REQUIRED';
  rewrites: ResumeRewritePreviewRow[];
};
export type ResumeRewriteAcknowledgement = {fact_id: string; reviewed: boolean};
export type ResumeRewriteReviewClient = (path: string, init?: RequestInit) => Promise<unknown>;
export type ProtectedResumeTokenGroup = {
  type: ProtectedResumeTokenType;
  required: string[];
  retained: string[];
  missing: string[];
};

const tokenOrder: ProtectedResumeTokenType[] = ['DATE', 'NUMBER', 'NAMED_CREDENTIAL'];
const tokenLabels: Record<ProtectedResumeTokenType, string> = {
  DATE: 'DATE', NUMBER: 'NUMBER', NAMED_CREDENTIAL: 'NAMED CREDENTIAL',
};

export function resumeRewriteReviewPreviewUrl(profileId: string, jobId: string): string {
  return `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/resume-rewrite-review-preview`;
}

export function protectedTokenAuditGroups(audit: ProtectedResumeTokenAudit): ProtectedResumeTokenGroup[] {
  return tokenOrder.map(type => ({
    type,
    required: audit.required.filter(token => token.type === type).map(token => token.value),
    retained: audit.retained.filter(token => token.type === type).map(token => token.value),
    missing: audit.missing.filter(token => token.type === type).map(token => token.value),
  }));
}

export function isResumeRewriteAcknowledgementAllowed(row: ResumeRewritePreviewRow): boolean {
  return row.protected_token_audit.missing.length === 0;
}

function eligibleFact(fact: ResumeRewriteReviewFact | undefined): fact is ResumeRewriteReviewFact {
  return !!fact && fact.verified && fact.user_approved && fact.confidence >= 80
    && !!fact.source.trim() && !!fact.text.trim();
}

function isProtectedToken(value: unknown): value is ProtectedResumeToken {
  if (!value || typeof value !== 'object') return false;
  const token = value as {type?: unknown; value?: unknown};
  return tokenOrder.includes(token.type as ProtectedResumeTokenType)
    && typeof token.value === 'string' && token.value.length > 0;
}

function validateAudit(audit: unknown): audit is ProtectedResumeTokenAudit {
  if (!audit || typeof audit !== 'object') return false;
  const value = audit as {required?: unknown; retained?: unknown; missing?: unknown};
  return Array.isArray(value.required) && value.required.every(isProtectedToken)
    && Array.isArray(value.retained) && value.retained.every(isProtectedToken)
    && Array.isArray(value.missing) && value.missing.every(isProtectedToken);
}

export type ResumeRewritePreviewInput = {
  profileId: string;
  jobId: string;
  profileVersion: string;
  expectedJobFingerprint: string;
  selectedFactIds: readonly string[];
  facts: readonly ResumeRewriteReviewFact[];
  rewrites: readonly ResumeRewriteProposal[];
};

function sameOrderedIds(actual: unknown, expected: readonly string[]): boolean {
  return Array.isArray(actual) && actual.length === expected.length
    && actual.every((value, index) => value === expected[index]);
}

function validatePreviewResponse(
  response: unknown,
  input: ResumeRewritePreviewInput,
): ResumeRewriteReviewPreviewResponse {
  if (!response || typeof response !== 'object') {
    throw new Error('The rewrite preview returned an invalid response. Retry the preview.');
  }
  const value = response as Partial<ResumeRewriteReviewPreviewResponse>;
  if (value.profile_id !== input.profileId || value.profile_version !== input.profileVersion
      || value.job_id !== input.jobId || value.job_fingerprint !== input.expectedJobFingerprint
      || value.read_only !== true || value.review_required !== true) {
    throw new Error('The preview did not match the current profile, job and approved facts. Refresh inputs, then retry.');
  }
  if (!sameOrderedIds(value.selected_fact_ids, input.selectedFactIds)) {
    throw new Error('The preview selected fact IDs did not match your current selection. Refresh and retry.');
  }
  if (value.semantic_equivalence !== 'USER_REVIEW_REQUIRED') {
    throw new Error('The preview must report USER_REVIEW_REQUIRED; automated checks cannot approve rewritten meaning.');
  }
  if (!Array.isArray(value.rewrites) || value.rewrites.length !== input.rewrites.length) {
    throw new Error('The preview did not return every selected proposed rewrite in order. Retry the preview.');
  }
  const facts = new Map(input.facts.map(fact => [fact.fact_id, fact]));
  for (const [index, row] of value.rewrites.entries()) {
    const proposal = input.rewrites[index];
    const fact = facts.get(proposal.fact_id);
    if (!row || row.fact_id !== proposal.fact_id || !eligibleFact(fact)
        || row.fact_kind !== fact.kind || row.source_text !== fact.text
        || row.proposed_text !== proposal.text.trim()
        || typeof row.source_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(row.source_sha256)
        || !validateAudit(row.protected_token_audit)) {
      throw new Error('The preview source text or proposed wording changed. Refresh the approved fact and retry.');
    }
  }
  return value as ResumeRewriteReviewPreviewResponse;
}

function validatePreviewInput(input: ResumeRewritePreviewInput) {
  if (!input.profileId || !input.jobId || !input.profileVersion || !input.expectedJobFingerprint) {
    throw new Error('Refresh the current profile and job before previewing resume rewrites.');
  }
  if (!input.selectedFactIds.length || new Set(input.selectedFactIds).size !== input.selectedFactIds.length) {
    throw new Error('Select unique approved source facts before previewing rewrites.');
  }
  const facts = new Map(input.facts.map(fact => [fact.fact_id, fact]));
  if (input.selectedFactIds.some(id => !eligibleFact(facts.get(id)))) {
    throw new Error('A selected source fact is no longer approved. Refresh facts before previewing.');
  }
  if (!input.rewrites.length || input.rewrites.length !== input.selectedFactIds.length
      || new Set(input.rewrites.map(item => item.fact_id)).size !== input.rewrites.length
      || input.rewrites.some((item, index) => item.fact_id !== input.selectedFactIds[index] || !item.text.trim())) {
    throw new Error('Add nonempty proposed wording for every selected approved fact in the same order before previewing.');
  }
}

export async function prepareResumeRewriteReviewPreview(
  input: ResumeRewritePreviewInput,
  client: ResumeRewriteReviewClient,
): Promise<ResumeRewriteReviewPreviewResponse> {
  validatePreviewInput(input);
  const result = await client(resumeRewriteReviewPreviewUrl(input.profileId, input.jobId), {
    method: 'POST',
    body: JSON.stringify({
      expected_profile_version: input.profileVersion,
      expected_job_fingerprint: input.expectedJobFingerprint,
      selected_fact_ids: [...input.selectedFactIds],
      rewrites: input.rewrites.map(({fact_id, text}) => ({fact_id, text: text.trim()})),
    }),
  });
  return validatePreviewResponse(result, input);
}

function scopeFingerprint(serialized: string): string {
  // A local change detector only; integrity is validated by the API snapshot.
  let hash = 14695981039346656037n;
  for (let index = 0; index < serialized.length; index += 1) {
    hash ^= BigInt(serialized.charCodeAt(index));
    hash = BigInt.asUintN(64, hash * 1099511628211n);
  }
  return hash.toString(16).padStart(16, '0');
}

export function resumeRewriteScopeSignature(input: ResumeRewritePreviewInput): string {
  const sourceAndProposal = [
    input.profileId, input.jobId, input.profileVersion, input.expectedJobFingerprint,
    [...input.selectedFactIds],
    input.facts.map(({fact_id, kind, text, source, confidence, verified, user_approved}) =>
      [fact_id, kind, text, source, confidence, verified, user_approved]),
    input.rewrites.map(({fact_id, text}) => [fact_id, text]),
  ];
  return scopeFingerprint(JSON.stringify(sourceAndProposal));
}

export function resumeRewriteFailureMessage(failure: unknown): string {
  const message = failure instanceof Error ? failure.message : String(failure);
  if (/RESUME_REWRITE_(PROFILE|JOB)_STALE|stale|profile.*changed|job.*changed|fingerprint|version/i.test(message)) {
    return 'The profile or job changed. Refresh the approved facts and job, then retry this preview.';
  }
  if (/UNSUPPORTED_CLAIM|unsupported claim/i.test(message)) {
    return 'The proposed wording does not preserve all required dates, numbers, or named credentials. Edit it and preview again.';
  }
  return message || 'Unable to load the resume rewrite preview. Retry.';
}

export type ResumeRewriteReviewPreviewProps = ResumeRewritePreviewInput & {
  client: ResumeRewriteReviewClient;
  disabled?: boolean;
  onReviewChange: (reviews: readonly ResumeRewriteAcknowledgement[]) => void;
};

export type ResumeRewritePreviewRowsProps = {
  rows: readonly ResumeRewritePreviewRow[];
  facts: readonly ResumeRewriteReviewFact[];
  reviews: Readonly<Record<string, boolean>>;
  disabled: boolean;
  onReviewChange: (factId: string, checked: boolean) => void;
};

export function ResumeRewritePreviewRows({rows, facts, reviews, disabled, onReviewChange}: ResumeRewritePreviewRowsProps) {
  const factsById = new Map(facts.map(fact => [fact.fact_id, fact]));
  return <>
    {rows.map(row => {
      const fact = factsById.get(row.fact_id);
      const missing = row.protected_token_audit.missing.length > 0;
      const id = `resume-rewrite-review-${row.fact_id}`;
      return <article key={row.fact_id} aria-label={`Rewrite preview for fact ${row.fact_id}`}>
        <h4>{row.fact_kind} · Fact {row.fact_id}</h4>
        <p>Source provenance: {fact?.source || 'Unavailable'} · {fact?.confidence ?? 'Unknown'}% confidence · {fact?.verified ? 'Verified' : 'Not verified'} · {fact?.user_approved ? 'Approved for document use' : 'Not approved for document use'}</p>
        <p>Approved source SHA-256: <code aria-label={`Source SHA-256 for fact ${row.fact_id}`}>{row.source_sha256}</code></p>
        <div className="two-col">
          <section aria-label={`Approved source text for fact ${row.fact_id}`}>
            <h5>Approved source text</h5>
            <pre>{row.source_text}</pre>
          </section>
          <section aria-label={`User-authored proposed wording for fact ${row.fact_id}`}>
            <h5>User-authored proposed wording</h5>
            <pre>{row.proposed_text}</pre>
          </section>
        </div>
        <section role="region" aria-label={`Protected token audit for ${row.fact_id}`}>
          <h5>Protected date, number and credential indicators</h5>
          {protectedTokenAuditGroups(row.protected_token_audit).map(group => <section key={group.type} aria-label={`${tokenLabels[group.type]} protected token indicators`}>
            <h6>{tokenLabels[group.type]} tokens</h6>
            <p>Required {tokenLabels[group.type]} tokens: {group.required.length ? group.required.join('; ') : 'None'}</p>
            <p>Retained {tokenLabels[group.type]} tokens: {group.retained.length ? group.retained.join('; ') : 'None'}</p>
            <p>Missing {tokenLabels[group.type]} tokens: {group.missing.length ? group.missing.join('; ') : 'None'}</p>
          </section>)}
          {missing && <p role="alert">A protected token is missing. Edit the proposal and preview it again before accepting this rewrite.</p>}
        </section>
        <label>
          <input id={id} type="checkbox" aria-label={`I reviewed the proposed wording for fact ${row.fact_id}`}
            checked={reviews[row.fact_id] === true} disabled={disabled || missing}
            onChange={event => onReviewChange(row.fact_id, event.target.checked)}/>
          I reviewed this proposed wording and its source.
        </label>
        {missing && <p id={`${id}-missing`} role="status">Review acknowledgement is blocked until every required protected token is retained.</p>}
      </article>;
    })}
  </>;
}

type ReviewState = {scope: string; response: ResumeRewriteReviewPreviewResponse; reviews: Record<string, boolean>};

export function ResumeRewriteReviewPreview({
  profileId, jobId, profileVersion, expectedJobFingerprint, selectedFactIds, facts, rewrites, client,
  disabled = false, onReviewChange,
}: ResumeRewriteReviewPreviewProps) {
  const input: ResumeRewritePreviewInput = {profileId, jobId, profileVersion, expectedJobFingerprint, selectedFactIds, facts, rewrites};
  const scope = resumeRewriteScopeSignature(input);
  const [review, setReview] = useState<ReviewState | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const callbackRef = useRef(onReviewChange);
  useEffect(() => { callbackRef.current = onReviewChange; }, [onReviewChange]);

  useEffect(() => {
    sequence.current += 1;
    setReview(null);
    setLoading(false);
    setError('');
    callbackRef.current(rewrites.map(item => ({fact_id: item.fact_id, reviewed: false})));
  }, [scope]);

  const currentReview = review?.scope === scope ? review : null;
  const canPreview = !disabled && selectedFactIds.length > 0 && rewrites.length === selectedFactIds.length
    && selectedFactIds.every(id => eligibleFact(facts.find(fact => fact.fact_id === id)))
    && rewrites.every((item, index) => item.fact_id === selectedFactIds[index] && !!item.text.trim());

  async function requestPreview() {
    const requestId = ++sequence.current;
    const requestScope = scope;
    setLoading(true);
    setError('');
    setReview(null);
    callbackRef.current(rewrites.map(item => ({fact_id: item.fact_id, reviewed: false})));
    try {
      const response = await prepareResumeRewriteReviewPreview(input, client);
      if (requestId !== sequence.current || requestScope !== scopeRef.current) return;
      const reviews = Object.fromEntries(response.rewrites.map(item => [item.fact_id, false]));
      setReview({scope: requestScope, response, reviews});
      callbackRef.current(response.rewrites.map(item => ({fact_id: item.fact_id, reviewed: false})));
    } catch (failure) {
      if (requestId !== sequence.current || requestScope !== scopeRef.current) return;
      setReview(null);
      callbackRef.current(rewrites.map(item => ({fact_id: item.fact_id, reviewed: false})));
      setError(resumeRewriteFailureMessage(failure));
    } finally {
      if (requestId === sequence.current && requestScope === scopeRef.current) setLoading(false);
    }
  }

  function acknowledge(factId: string, checked: boolean) {
    if (!currentReview) return;
    const row = currentReview.response.rewrites.find(item => item.fact_id === factId);
    if (!row || !isResumeRewriteAcknowledgementAllowed(row)) return;
    const reviews = {...currentReview.reviews, [factId]: checked};
    setReview({...currentReview, reviews});
    callbackRef.current(currentReview.response.rewrites.map(item => ({fact_id: item.fact_id, reviewed: reviews[item.fact_id] === true})));
  }

  const allReviewed = !!currentReview && currentReview.response.rewrites.length > 0
    && currentReview.response.rewrites.every(row => isResumeRewriteAcknowledgementAllowed(row) && currentReview.reviews[row.fact_id] === true);

  return <section aria-label="Resume rewrite review preview" aria-busy={loading}>
    <h3>Review proposed resume wording</h3>
    <p>Profile <code>{profileId || 'not selected'}</code> · Job <code>{jobId || 'not selected'}</code></p>
    <p>Preview only. Approved profile facts stay unchanged. Semantic equivalence is <strong>USER_REVIEW_REQUIRED</strong>; automated checks do not establish that wording preserves meaning.</p>
    <button type="button" disabled={!canPreview || loading} onClick={() => void requestPreview()}>
      {loading ? 'Previewing exact resume rewrites…' : 'Preview exact resume rewrites'}
    </button>
    {error && <div role="alert"><p>{error}</p><button type="button" disabled={disabled || loading} onClick={() => void requestPreview()}>Retry resume rewrite preview</button></div>}
    {!currentReview && !loading && !error && <p>No wording has been reviewed yet. Preview the exact source and proposal first.</p>}
    {currentReview && <>
      <ResumeRewritePreviewRows rows={currentReview.response.rewrites} facts={facts}
        reviews={currentReview.reviews} disabled={disabled || loading} onReviewChange={acknowledge}/>
      <p role="status">{allReviewed ? 'Every rewrite was explicitly reviewed.' : 'Every rewrite needs explicit user review; missing-token rewrites cannot be accepted.'}</p>
    </>}
  </section>;
}
