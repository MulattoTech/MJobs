import React, { useEffect, useState } from 'react';

export type FactRejection = {
  fact_id: string;
  fact_sha256: string;
  review_status: 'PENDING' | 'APPROVED' | 'REJECTED';
  decision_id: string | null;
  reviewed_at: string | null;
};
export type FactRejections = {profile_id:string;profile_version:string;facts:FactRejection[]};

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
