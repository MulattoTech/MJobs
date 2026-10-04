import React from 'react';

export type SourceQualityValue = {
  score: number | null;
  coverage: number;
  components: Record<string, number | null>;
  limitations?: string[];
};

const LABELS: Record<string, string> = {
  freshness: 'Freshness', completeness: 'Completeness', availability: 'Availability',
  parser_reliability: 'Parser reliability', salary_completeness: 'Salary completeness',
  application_link_accuracy: 'Application-link accuracy', historical_uptime: 'Historical uptime',
};

function percent(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${Math.round(value)}%` : 'Unknown';
}

export function SourceQuality({quality}: {quality?: SourceQualityValue | null}) {
  if (!quality) return null;
  const score=typeof quality.score === 'number' && Number.isFinite(quality.score) ? `${Math.round(quality.score)}/100` : 'Unknown';
  return <section className="source-quality" aria-label="Source quality">
    <h4>Source quality</h4>
    <p>{score} · {percent(quality.coverage)} measurement coverage</p>
    <dl>{Object.keys(LABELS).map(key => <div key={key}><dt>{LABELS[key]}</dt><dd>{percent(quality.components?.[key])}</dd></div>)}</dl>
    {!!quality.limitations?.length && <details><summary>Quality limitations</summary><ul>{quality.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul></details>}
  </section>;
}
