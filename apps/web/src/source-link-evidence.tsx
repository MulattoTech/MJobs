import React from 'react';

export type LinkObservation = {
  posting_id: string;
  requested_url: string;
  final_url: string | null;
  result: 'ACCURATE' | 'INACCURATE' | 'UNKNOWN';
  reason: string;
  observed_at: string;
  response_sha256: string | null;
  identity_evidence: string[];
};

export type LinkVerification = {
  sampled_count: number;
  accepted_posting_count: number;
  conclusive_sample_count: number;
  accurate_sample_count: number;
  unknown_sample_count: number;
  scope: string;
  latest: LinkObservation[];
};

function count(value: number): number {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function observedTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Time not recorded' : parsed.toLocaleString();
}

function outcomeLabel(result: LinkObservation['result']): string {
  if (result === 'ACCURATE') return 'Identifier match observed';
  if (result === 'INACCURATE') return 'Link returned not found or gone';
  return 'Unknown result';
}

export function SourceLinkEvidence({evidence}: {evidence?: LinkVerification | null}) {
  if (!evidence) return <section className="source-link-evidence" aria-label="Job link observations">
    <h4>Job link observations</h4>
    <p className="muted">Not checked. No bounded link observations are recorded for this source.</p>
  </section>;
  const conclusive = count(evidence.conclusive_sample_count);
  const accurate = Math.min(count(evidence.accurate_sample_count), conclusive);
  const unknown = count(evidence.unknown_sample_count);
  const notFound = Math.max(0, conclusive - accurate);
  const total = conclusive + unknown;
  const sampled = count(evidence.sampled_count);
  const acceptedPostings = count(evidence.accepted_posting_count);
  const latest = Array.isArray(evidence.latest) ? evidence.latest.slice(0, 5) : [];
  if (total === 0 && latest.length === 0) return <section className="source-link-evidence" aria-label="Job link observations">
    <h4>Job link observations</h4>
    <p className="muted">Not checked. No bounded link observations are recorded for this source.</p>
  </section>;
  return <section className="source-link-evidence" aria-label="Job link observations">
    <h4>Job link observations</h4>
    <p>{sampled} sampled posting observations across {acceptedPostings} accepted feed row{acceptedPostings === 1 ? '' : 's'} in this window. Observed {total} bounded link{total === 1 ? '' : 's'}: {accurate} identifier match{accurate === 1 ? '' : 'es'}, {notFound} not found, {unknown} unknown.</p>
    {conclusive > 0 && <p>Conclusive checks: {accurate}/{conclusive} matched ({Math.round(accurate * 100 / conclusive)}% among conclusive checks only).</p>}
    <p className="muted">Scope: {evidence.scope || 'Not specified'}. These observations do not verify an employer, guarantee a current application route, cover every posting, or permit submission.</p>
    {latest.length > 0 && <details>
      <summary>View latest observed links</summary>
      <ul>
        {latest.map((observation, index) => <li key={`${observation.posting_id}-${index}`}>
          <strong>{outcomeLabel(observation.result)}</strong>
          <p>Requested: <code>{observation.requested_url}</code></p>
          <p>Final: <code>{observation.final_url || 'Not available'}</code></p>
          <p>Reason: {observation.reason || 'Not recorded'} · {observedTime(observation.observed_at)}</p>
          {!!observation.identity_evidence?.length && <p>Evidence: {observation.identity_evidence.join('; ')}</p>}
        </li>)}
      </ul>
    </details>}
  </section>;
}
