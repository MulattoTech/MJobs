import React from 'react';

export type MatchCategory =
  | 'EXCEPTIONAL'
  | 'EXCELLENT'
  | 'STRONG'
  | 'POSSIBLE'
  | 'WEAK'
  | 'POOR'
  | 'INCOMPLETE';

export type JobMatchScoreBadgeProps = {
  score?: number | null;
  scoreFormatted?: string | null;
  category?: MatchCategory | null;
  categoryLabel?: string | null;
  coveragePercent?: number | null;
  recommendation?: string | null;
  advisory?: boolean;
  compact?: boolean;
  negativeSignalsTriggered?: string[];
};

const CATEGORY_META: Record<
  MatchCategory,
  { icon: string; className: string; defaultLabel: string }
> = {
  EXCEPTIONAL: { icon: '🌟', className: 'match-exceptional', defaultLabel: 'Exceptional Match' },
  EXCELLENT: { icon: '⭐', className: 'match-excellent', defaultLabel: 'Excellent Match' },
  STRONG: { icon: '🎯', className: 'match-strong', defaultLabel: 'Strong Match' },
  POSSIBLE: { icon: '🔎', className: 'match-possible', defaultLabel: 'Possible Match' },
  WEAK: { icon: '⚠️', className: 'match-weak', defaultLabel: 'Weak Match' },
  POOR: { icon: '🚫', className: 'match-poor', defaultLabel: 'Poor Match' },
  INCOMPLETE: { icon: '⏱️', className: 'match-incomplete', defaultLabel: 'Incomplete Match' },
};

export function JobMatchScoreBadge({
  score,
  scoreFormatted,
  category = 'INCOMPLETE',
  categoryLabel,
  coveragePercent,
  recommendation,
  advisory = true,
  compact = false,
  negativeSignalsTriggered = [],
}: JobMatchScoreBadgeProps) {
  const cat = category && category in CATEGORY_META ? category : 'INCOMPLETE';
  const meta = CATEGORY_META[cat];
  const label = categoryLabel || meta.defaultLabel;

  const scoreDisplay =
    scoreFormatted ||
    (score != null ? `${Math.round(score)}/100 — ${label}` : `Incomplete — Insufficient Data`);

  const hasDealbreakers = negativeSignalsTriggered && negativeSignalsTriggered.length > 0;

  if (compact) {
    return (
      <span
        className={`tag job-match-badge-compact ${meta.className}`}
        role="status"
        aria-label={`Match score: ${scoreDisplay}`}
        data-testid="job-match-badge-compact"
      >
        <span aria-hidden="true">{meta.icon}</span> {scoreDisplay}
      </span>
    );
  }

  return (
    <div
      className={`panel job-match-score-card ${meta.className}`}
      role="region"
      aria-label="Job match score transparency card"
      data-testid="job-match-score-card"
    >
      <div className="toolbar">
        <div>
          <span className="eyebrow">JOB MATCH TRANSPARENCY (MJ-REQ-112)</span>
          <h3 data-testid="job-match-score-header">
            <span aria-hidden="true">{meta.icon}</span> {scoreDisplay}
          </h3>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {advisory && (
            <span className="tag warn" title="Algorithmic match is advisory to user">
              Advisory Match
            </span>
          )}
          <span className={`status ${cat === 'EXCEPTIONAL' || cat === 'EXCELLENT' ? 'good' : cat === 'STRONG' || cat === 'POSSIBLE' ? 'warn' : 'bad'}`}>
            Category: {cat}
          </span>
        </div>
      </div>

      {hasDealbreakers && (
        <div
          className="alert alert-danger"
          role="alert"
          data-testid="job-match-dealbreaker-alert"
          style={{
            border: '1px solid var(--danger)',
            backgroundColor: 'var(--danger-bg)',
            color: 'var(--danger)',
            padding: '0.5rem 0.75rem',
            borderRadius: '4px',
            marginBottom: '0.75rem',
            fontSize: '0.875rem',
          }}
        >
          <strong>Dealbreaker Alert:</strong> Triggers candidate avoid term(s):{' '}
          <strong>{negativeSignalsTriggered.join(', ')}</strong>.
        </div>
      )}

      {recommendation && (
        <p className="recommendation" data-testid="job-match-recommendation" style={{ margin: '0.5rem 0' }}>
          <strong>Recommendation: </strong>
          {recommendation}
        </p>
      )}

      {coveragePercent != null && (
        <div
          className="coverage-indicator"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            fontSize: '0.8125rem',
            color: 'var(--muted)',
            marginTop: '0.5rem',
          }}
        >
          <span>Signal Coverage: {Math.round(coveragePercent)}%</span>
          <div
            style={{
              width: '100px',
              height: '6px',
              backgroundColor: 'var(--border)',
              borderRadius: '3px',
              overflow: 'hidden',
              marginLeft: '0.5rem',
            }}
          >
            <div
              style={{
                width: `${Math.min(100, Math.max(0, coveragePercent))}%`,
                height: '100%',
                backgroundColor: 'var(--info)',
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}
