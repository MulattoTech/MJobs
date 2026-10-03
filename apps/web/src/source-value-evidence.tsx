import React from 'react';

export type SourceValueResolution = {
  selected_candidate_id: string | null;
  selected_posting_id: string | null;
  authority: string;
  authority_evidence: string[];
  quality: {score: number | null; coverage: number};
  effective_quality: number | null;
  evaluated_at: string;
  candidate_count: number;
  conflicts: string[];
  selected_application_url: string;
  route_review_required: boolean;
  preserved_route_id: string | null;
  preserved_route_url: string | null;
};

export function authorityLabel(authority: string): string {
  const labels: Record<string, string> = {
    VERIFIED_ATS: 'Verified applicant-tracking source',
    VERIFIED_EMPLOYER: 'Verified employer source',
    AGGREGATOR: 'Job aggregator source',
    UNKNOWN: 'Source authority not established',
  };
  return labels[authority] || authority.toLowerCase().split('_').filter(Boolean).map(word => word[0].toUpperCase() + word.slice(1)).join(' ') || 'an available source';
}

export function qualityLabel(quality: SourceValueResolution['quality']): string {
  const score = quality.score === null ? 'unknown' : `${Math.round(quality.score)}/100`;
  return `${score} quality score · ${Math.round(quality.coverage)}% measurement coverage`;
}

export function SourceValueEvidence({resolution, onPreparePreserved}: {
  resolution?: SourceValueResolution | null;
  onPreparePreserved?: () => void;
}) {
  if (!resolution) return null;
  const selectedLink = /^https?:\/\//i.test(resolution.selected_application_url) ? resolution.selected_application_url : '';
  const preservedLink = resolution.preserved_route_url && /^https?:\/\//i.test(resolution.preserved_route_url) ? resolution.preserved_route_url : '';
  return <section className="panel source-value-evidence" aria-label="Source value resolution">
    <h3>Selected source evidence</h3>
    <p>We selected <strong>{authorityLabel(resolution.authority)}</strong> as the current value authority from {resolution.candidate_count} candidate{resolution.candidate_count === 1 ? '' : 's'}.</p>
    <p>{qualityLabel(resolution.quality)}. This is a comparison measure, not a probability or employer verification.</p>
    {resolution.effective_quality !== null && <p>Effective quality: {Math.round(resolution.effective_quality)}/100.</p>}
    {!!resolution.authority_evidence.length && <><h4>Why this source was selected</h4><ul>{resolution.authority_evidence.map((item, index)=><li key={`evidence-${index}`}>{item}</li>)}</ul></>}
    {!!resolution.conflicts.length && <><h4>Source disagreements</h4><ul>{resolution.conflicts.map((item, index)=><li key={`conflict-${index}`}>{item}</li>)}</ul></>}
    <p className="muted">Evaluated {resolution.evaluated_at || 'at an unknown time'}; raw postings remain available in source evidence.</p>
    {selectedLink && <p>Selected application URL: <a href={selectedLink} target="_blank" rel="noreferrer">{selectedLink}</a></p>}
    {resolution.route_review_required && <div className="alert" role="alert">
      <strong>Application route needs review</strong>
      <p>The selected source has a different application route. The previously used route is preserved, and existing application targets are unchanged.</p>
      {preservedLink && <p>Previously used route: <a href={preservedLink} target="_blank" rel="noreferrer">{preservedLink}</a></p>}
      <button type="button" className="quiet" disabled={!onPreparePreserved} onClick={onPreparePreserved}>Prepare using preserved route</button>
    </div>}
  </section>;
}
