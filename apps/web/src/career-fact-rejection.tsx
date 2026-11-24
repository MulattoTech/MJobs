import React, { useEffect, useState } from 'react';

export type FactRejection = {
  fact_id: string;
  fact_sha256: string;
  review_status: 'PENDING' | 'APPROVED' | 'REJECTED';
  decision_id: string | null;
  reviewed_at: string | null;
};
export type FactRejections = {profile_id:string;profile_version:string;facts:FactRejection[]};
export const FACT_REVIEW_VERSION_MISMATCH_MESSAGE =
  'Fact review belongs to a different profile version. Reload before reviewing.';

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, stableValue(item)]));
  }
  return value;
}

export function hasUnsavedProfileDraft(draft: unknown, baseline: unknown): boolean {
  if (baseline === null || baseline === undefined) return true;
  return JSON.stringify(stableValue(draft)) !== JSON.stringify(stableValue(baseline));
}

export function profileDraftFingerprint(draft: unknown): string {
  return JSON.stringify(stableValue(draft)) ?? 'undefined';
}

export function profileDraftChangedSince(draft: unknown, fingerprint: string): boolean {
  return profileDraftFingerprint(draft) !== fingerprint;
}

export function assertProfileDraftUnchanged(draft: unknown, fingerprint: string): void {
  if (profileDraftChangedSince(draft, fingerprint))
    throw new Error('The profile draft changed during reload. No edits were discarded; review the current draft and retry.');
}

export function factReviewMatchesProfileVersion(
  profileId: string, profileVersion: string,
  review: Pick<FactRejections, 'profile_id' | 'profile_version'>,
): boolean {
  return review.profile_id === profileId && review.profile_version === profileVersion;
}

export function isFactReviewVersionConflict(error: unknown): boolean {
  return error instanceof Error && 'status' in error && error.status === 409
    && 'code' in error && error.code === 'FACT_REJECTION_VERSION_CHANGED';
}

export function CareerFactReviewReload({stale, hasUnsavedDraft, disabled, onReload}: {
  stale: boolean;
  hasUnsavedDraft: boolean;
  disabled: boolean;
  onReload: () => void;
}) {
  if (!stale) return null;
  return <div className="fact-review-reload" role="alert">
    <p>Fact review belongs to a different profile version. Reload before reviewing.</p>
    {hasUnsavedDraft && <p>Reloading will discard unsaved profile changes.</p>}
    <button type="button" disabled={disabled} onClick={onReload}>
      {hasUnsavedDraft ? 'Discard draft and reload' : 'Reload saved facts'}
    </button>
  </div>;
}

export function CareerFactRejection({review, pending, disabled, onReject}: {
  review: FactRejection | undefined;
  pending: boolean;
  disabled: boolean;
  onReject: () => void;
}) {
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => setConfirmed(false), [review?.fact_sha256, review?.decision_id, pending]);
  if (review?.review_status === 'REJECTED') return <p role="status">Rejected — original fact retained and excluded from documents.</p>;
  if (!pending) return null;
  const ready = review?.review_status === 'PENDING' && !disabled;
  return <div className="fact-rejection">
    <p>Reject an incorrect extraction without deleting its original text or approving it for documents. Editing or saving a new profile version requires a new review.</p>
    {!review && <p className="muted">Save this fact and wait for its review status before rejecting it.</p>}
    <label><input type="checkbox" checked={confirmed} disabled={!ready}
      onChange={event => setConfirmed(event.target.checked)}/> I reject this fact as incorrect</label>
    <button type="button" disabled={!ready || !confirmed} onClick={onReject}>Reject this fact</button>
  </div>;
}
