import React, { useState } from 'react';

export type DimensionName =
  | 'title_similarity'
  | 'required_skills'
  | 'preferred_skills'
  | 'experience'
  | 'education'
  | 'certifications'
  | 'remote_location'
  | 'salary'
  | 'employment_type'
  | 'industry'
  | 'company_preference'
  | 'negative_requirements';

export type DimensionContribution = {
  dimension: DimensionName;
  label: string;
  weight: number;
  raw_score: number | null;
  weighted_contribution: number | null;
  status: 'KNOWN' | 'UNKNOWN';
  explanation: string;
  provenance?: Record<string, unknown>;
};

export type MatchComponentsBreakdownProps = {
  score?: number | null;
  coveragePercent?: number | null;
  dimensions?: DimensionContribution[];
  knownDimensions?: string[];
  unknownDimensions?: string[];
  negativeSignalsTriggered?: string[];
  advisory?: boolean;
};

const DIMENSION_ICONS: Record<DimensionName, string> = {
  title_similarity: '🏷️',
  required_skills: '⚡',
  preferred_skills: '⭐',
  experience: '💼',
  education: '🎓',
  certifications: '📜',
  remote_location: '🌐',
  salary: '💰',
  employment_type: '🕒',
  industry: '🏢',
  company_preference: '❤️',
  negative_requirements: '🚫',
};

export function MatchComponentsBreakdown({
  score,
  coveragePercent,
  dimensions = [],
  knownDimensions = [],
  unknownDimensions = [],
  negativeSignalsTriggered = [],
  advisory = true,
}: MatchComponentsBreakdownProps) {
  const [selectedDimension, setSelectedDimension] = useState<DimensionName | null>(null);

  const hasNegativeSignals = negativeSignalsTriggered && negativeSignalsTriggered.length > 0;
  const scoreDisplay = score != null ? `${score.toFixed(1)}%` : 'Incomplete';
  const coverageDisplay = coveragePercent != null ? `${coveragePercent.toFixed(0)}%` : '0%';

  return (
    <section
      className="panel match-components-breakdown-panel"
      role="region"
      aria-label="Match score dimensions breakdown"
    >
      <div className="toolbar">
        <div>
          <span className="eyebrow">TRANSPARENT MATCH ARCHITECTURE</span>
          <h3>12-Dimension Match Evaluation</h3>
        </div>
        <div className="status-group" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {advisory && (
            <span className="tag warn" title="Advisory: requires candidate review">
              Advisory Match
            </span>
          )}
          <span
            className={`status ${score != null && score >= 80 ? 'good' : score != null && score >= 60 ? 'warn' : 'bad'}`}
            data-testid="overall-match-score"
          >
            Match Score: {scoreDisplay}
          </span>
        </div>
      </div>

      <p className="description">
        MJobs breaks down matching across 12 distinct dimensions with honest UNKNOWN handling
        and dynamic denominator scaling. Data that is unstated in the posting or candidate profile
        is marked as UNKNOWN rather than penalized.
      </p>

      {hasNegativeSignals && (
        <div
          className="alert alert-danger"
          role="alert"
          data-testid="negative-requirements-alert"
          style={{
            border: '1px solid var(--danger)',
            backgroundColor: 'var(--danger-bg)',
            color: 'var(--danger)',
            padding: '0.75rem 1rem',
            borderRadius: '6px',
            marginBottom: '1rem',
          }}
        >
          <strong>🚫 Negative Requirement Alert:</strong> This posting triggers candidate dealbreaker terms:{' '}
          <strong>{negativeSignalsTriggered.join(', ')}</strong>. A penalty has been applied to this match.
        </div>
      )}

      <div
        className="coverage-bar-container"
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          backgroundColor: 'var(--surface-subtle)',
          padding: '0.75rem 1rem',
          borderRadius: '6px',
          marginBottom: '1rem',
        }}
      >
        <div>
          <span style={{ fontWeight: 600 }}>Signal Coverage: </span>
          <span data-testid="coverage-percent">{coverageDisplay}</span>
          <span style={{ color: 'var(--muted)', fontSize: '0.875rem', marginLeft: '0.5rem' }}>
            ({knownDimensions.length} known, {unknownDimensions.length} unstated/unknown)
          </span>
        </div>
        <div
          style={{
            width: '120px',
            height: '8px',
            backgroundColor: 'var(--border)',
            borderRadius: '4px',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              width: `${Math.min(100, Math.max(0, coveragePercent ?? 0))}%`,
              height: '100%',
              backgroundColor: 'var(--info)',
            }}
          />
        </div>
      </div>

      <div className="table-responsive">
        <table className="table match-components-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '2px solid var(--surface-subtle)', textAlign: 'left' }}>
              <th style={{ padding: '0.5rem' }}>Dimension</th>
              <th style={{ padding: '0.5rem' }}>Weight</th>
              <th style={{ padding: '0.5rem' }}>Raw Score</th>
              <th style={{ padding: '0.5rem' }}>Contribution</th>
              <th style={{ padding: '0.5rem' }}>Status</th>
              <th style={{ padding: '0.5rem' }}>Details</th>
            </tr>
          </thead>
          <tbody>
            {dimensions.map((dim) => {
              const icon = DIMENSION_ICONS[dim.dimension] || '📊';
              const isSelected = selectedDimension === dim.dimension;
              const isKnown = dim.status === 'KNOWN';

              return (
                <React.Fragment key={dim.dimension}>
                  <tr
                    data-testid={`dimension-row-${dim.dimension}`}
                    style={{
                      borderBottom: '1px solid var(--surface-subtle)',
                      backgroundColor: isSelected ? 'var(--info-bg)' : 'transparent',
                      cursor: 'pointer',
                    }}
                    onClick={() => setSelectedDimension(isSelected ? null : dim.dimension)}
                  >
                    <td style={{ padding: '0.6rem 0.5rem', fontWeight: 600 }}>
                      <span aria-hidden="true" style={{ marginRight: '0.4rem' }}>
                        {icon}
                      </span>
                      {dim.label}
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>{dim.weight.toFixed(0)}%</td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      {isKnown && dim.raw_score != null ? (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                          <span>{dim.raw_score.toFixed(0)}%</span>
                          <div
                            style={{
                              width: '50px',
                              height: '6px',
                              backgroundColor: 'var(--border)',
                              borderRadius: '3px',
                              overflow: 'hidden',
                            }}
                          >
                            <div
                              style={{
                                width: `${Math.min(100, Math.max(0, dim.raw_score))}%`,
                                height: '100%',
                                backgroundColor:
                                  dim.raw_score >= 80 ? 'var(--good)' : dim.raw_score >= 50 ? 'var(--warning)' : 'var(--danger)',
                              }}
                            />
                          </div>
                        </div>
                      ) : (
                        <span style={{ color: 'var(--muted)', fontStyle: 'italic' }}>—</span>
                      )}
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      {isKnown && dim.weighted_contribution != null ? (
                        <span>+{dim.weighted_contribution.toFixed(1)} pts</span>
                      ) : (
                        <span style={{ color: 'var(--muted)' }}>0.0 pts</span>
                      )}
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      <span
                        className={`tag ${isKnown ? 'good' : 'neutral'}`}
                        data-testid={`dimension-status-${dim.dimension}`}
                        style={{
                          padding: '0.2rem 0.5rem',
                          borderRadius: '4px',
                          fontSize: '0.75rem',
                          fontWeight: 600,
                          backgroundColor: isKnown ? 'var(--good-bg)' : 'var(--surface-subtle)',
                          color: isKnown ? 'var(--good)' : 'var(--text)',
                        }}
                      >
                        {dim.status}
                      </span>
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      <button
                        type="button"
                        aria-expanded={isSelected}
                        aria-label={`Toggle details for ${dim.label}`}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: 'var(--info)',
                          cursor: 'pointer',
                          padding: 0,
                          fontSize: '0.875rem',
                        }}
                      >
                        {isSelected ? '▲ Hide' : '▼ View'}
                      </button>
                    </td>
                  </tr>
                  {isSelected && (
                    <tr
                      data-testid={`dimension-details-${dim.dimension}`}
                      style={{ backgroundColor: 'var(--surface-subtle)', borderBottom: '1px solid var(--surface-subtle)' }}
                    >
                      <td colSpan={6} style={{ padding: '0.75rem 1rem' }}>
                        <div style={{ fontSize: '0.875rem', lineHeight: '1.5' }}>
                          <p style={{ margin: '0 0 0.5rem 0' }}>
                            <strong>Analysis: </strong>
                            {dim.explanation}
                          </p>
                          {dim.provenance && Object.keys(dim.provenance).length > 0 && (
                            <details style={{ marginTop: '0.5rem', color: 'var(--muted)' }}>
                              <summary style={{ cursor: 'pointer', fontWeight: 500 }}>Audit Provenance</summary>
                              <pre
                                style={{
                                  backgroundColor: 'var(--surface-subtle)',
                                  padding: '0.5rem',
                                  borderRadius: '4px',
                                  marginTop: '0.25rem',
                                  fontSize: '0.75rem',
                                  overflowX: 'auto',
                                }}
                              >
                                {JSON.stringify(dim.provenance, null, 2)}
                              </pre>
                            </details>
                          )}
                        </div>
                      </td>
                    </tr>
                  )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
