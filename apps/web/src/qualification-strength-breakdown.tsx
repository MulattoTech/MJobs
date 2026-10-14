import React, { useState } from 'react';

export type RequirementStrength = 'REQUIRED' | 'PREFERRED' | 'NICE_TO_HAVE' | 'UNKNOWN';
export type CandidateCoverage = 'COVERED' | 'MISSING' | 'UNKNOWN';

export type QualificationItem = {
  name: string;
  qualification_type?: string;
  strength: RequirementStrength;
  weight: number;
  candidate_status: CandidateCoverage;
  score_impact: string;
  provenance?: Record<string, unknown>;
};

export type QualificationSummary = {
  total: number;
  covered: number;
  missing: number;
  unknown?: number;
  coverage_ratio: number;
};

export type QualificationStrengthBreakdownProps = {
  items?: QualificationItem[];
  counts?: Partial<Record<RequirementStrength, QualificationSummary>>;
  requiredCoveragePercent?: number | null;
  preferredCoveragePercent?: number | null;
  overallStrengthScore?: number | null;
  explanation?: string | null;
};

const STRENGTH_META: Record<
  RequirementStrength,
  { label: string; badgeClass: string; icon: string; description: string }
> = {
  REQUIRED: {
    label: 'Required (Mandatory)',
    badgeClass: 'badge-strength-required',
    icon: '⚡',
    description: 'Mandatory qualification (1.0 weight). Core requirement.',
  },
  PREFERRED: {
    label: 'Preferred (Desirable)',
    badgeClass: 'badge-strength-preferred',
    icon: '⭐',
    description: 'Preferred technology (0.5 weight). Desirable but non-disqualifying.',
  },
  NICE_TO_HAVE: {
    label: 'Nice-to-Have (Bonus)',
    badgeClass: 'badge-strength-nice',
    icon: '✨',
    description: 'Bonus qualification (0.25 weight). Minimal score impact.',
  },
  UNKNOWN: {
    label: 'Unstated / Unknown',
    badgeClass: 'badge-strength-unknown',
    icon: '❔',
    description: 'Unclassified mention. Not assumed to be mandatory.',
  },
};

export function QualificationStrengthBreakdown({
  items = [],
  counts,
  requiredCoveragePercent,
  preferredCoveragePercent,
  overallStrengthScore,
  explanation,
}: QualificationStrengthBreakdownProps) {
  const [activeFilter, setActiveFilter] = useState<'ALL' | RequirementStrength>('ALL');

  const filteredItems =
    activeFilter === 'ALL' ? items : items.filter((item) => item.strength === activeFilter);

  const reqCount = counts?.REQUIRED;
  const prefCount = counts?.PREFERRED;
  const niceCount = counts?.NICE_TO_HAVE;
  const unkCount = counts?.UNKNOWN;

  return (
    <section
      className="panel qualification-strength-breakdown-panel"
      role="region"
      aria-label="Qualification requirement strength breakdown"
      data-testid="qualification-strength-panel"
    >
      <div className="toolbar">
        <div>
          <span className="eyebrow">REQUIREMENT EXTRACTION (MJ-REQ-115)</span>
          <h3>Required vs Preferred Qualifications</h3>
        </div>
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {overallStrengthScore != null && (
            <span
              className={`status ${overallStrengthScore >= 75 ? 'good' : overallStrengthScore >= 50 ? 'warn' : 'bad'}`}
              data-testid="overall-strength-score"
            >
              Alignment: {overallStrengthScore.toFixed(0)}%
            </span>
          )}
        </div>
      </div>

      <p className="description">
        MJobs distinguishes between mandatory qualifications and preferred bonuses. Missing a
        preferred technology is bounded to not penalize your match score as severely as missing a
        mandatory requirement.
      </p>

      {/* Summary KPI Cards */}
      <div
        className="kpi-grid"
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
          gap: '0.75rem',
          marginBottom: '1rem',
        }}
      >
        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: 'var(--warning-bg)',
            border: '1px solid var(--warning-bg)',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--warning)' }}>
            ⚡ MANDATORY REQUIREMENTS
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }}>
            {requiredCoveragePercent != null ? `${requiredCoveragePercent.toFixed(0)}% Met` : '—'}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>
            {reqCount ? `${reqCount.covered}/${reqCount.total} verified` : `${items.filter((i) => i.strength === 'REQUIRED').length} items`}
          </span>
        </div>

        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: 'var(--info-bg)',
            border: '1px solid var(--info-bg)',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--info)' }}>
            ⭐ PREFERRED QUALIFICATIONS
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }}>
            {preferredCoveragePercent != null ? `${preferredCoveragePercent.toFixed(0)}% Met` : '—'}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>
            {prefCount ? `${prefCount.covered}/${prefCount.total} verified` : `${items.filter((i) => i.strength === 'PREFERRED').length} items`}
          </span>
        </div>

        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: 'var(--good-bg)',
            border: '1px solid var(--good-bg)',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--good)' }}>
            ✨ NICE-TO-HAVE / BONUS
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }}>
            {niceCount ? `${niceCount.covered}/${niceCount.total} Met` : 'Bonus Only'}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>Non-penalizing</span>
        </div>

        <div
          className="kpi-card"
          style={{
            padding: '0.75rem',
            backgroundColor: 'var(--surface-subtle)',
            border: '1px solid var(--border)',
            borderRadius: '6px',
          }}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 600, color: 'var(--text)' }}>
            ❔ UNSTATED / UNKNOWN
          </span>
          <div style={{ fontSize: '1.25rem', fontWeight: 700, margin: '0.25rem 0' }}>
            {unkCount ? `${unkCount.total} Unstated` : `${items.filter((i) => i.strength === 'UNKNOWN').length} Unstated`}
          </div>
          <span style={{ fontSize: '0.75rem', color: 'var(--muted)' }}>Not assumed required</span>
        </div>
      </div>

      {explanation && (
        <div
          className="alert alert-info"
          data-testid="strength-explanation"
          style={{
            fontSize: '0.875rem',
            padding: '0.5rem 0.75rem',
            backgroundColor: 'var(--surface-subtle)',
            borderRadius: '4px',
            marginBottom: '1rem',
          }}
        >
          {explanation}
        </div>
      )}

      {/* Filter Tabs */}
      <div
        className="filter-bar"
        role="tablist"
        aria-label="Filter qualifications by requirement strength"
        style={{ display: 'flex', gap: '0.5rem', marginBottom: '1rem', flexWrap: 'wrap' }}
      >
        {(['ALL', 'REQUIRED', 'PREFERRED', 'NICE_TO_HAVE', 'UNKNOWN'] as const).map((filterKey) => {
          const isActive = activeFilter === filterKey;
          const count =
            filterKey === 'ALL'
              ? items.length
              : items.filter((i) => i.strength === filterKey).length;

          return (
            <button
              key={filterKey}
              type="button"
              role="tab"
              aria-selected={isActive}
              onClick={() => setActiveFilter(filterKey)}
              style={{
                padding: '0.4rem 0.8rem',
                borderRadius: '4px',
                border: isActive ? '1px solid var(--info)' : '1px solid var(--border)',
                backgroundColor: isActive ? 'var(--info)' : 'var(--surface)',
                color: isActive ? 'var(--surface)' : 'var(--text)',
                fontWeight: 600,
                fontSize: '0.8125rem',
                cursor: 'pointer',
              }}
            >
              {filterKey === 'ALL' ? 'All' : STRENGTH_META[filterKey].label.split(' ')[0]} ({count})
            </button>
          );
        })}
      </div>

      {/* Qualification Items Table */}
      <div className="table-responsive">
        <table className="table qualification-strength-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr style={{ borderBottom: '2px solid var(--surface-subtle)', textAlign: 'left' }}>
              <th style={{ padding: '0.5rem' }}>Qualification</th>
              <th style={{ padding: '0.5rem' }}>Strength</th>
              <th style={{ padding: '0.5rem' }}>Candidate Status</th>
              <th style={{ padding: '0.5rem' }}>Weight & Impact</th>
            </tr>
          </thead>
          <tbody>
            {filteredItems.length === 0 ? (
              <tr>
                <td colSpan={4} style={{ padding: '1rem', textAlign: 'center', color: 'var(--muted)' }}>
                  No qualifications match the selected filter.
                </td>
              </tr>
            ) : (
              filteredItems.map((item, idx) => {
                const meta = STRENGTH_META[item.strength];
                const isCovered = item.candidate_status === 'COVERED';

                return (
                  <tr
                    key={`${item.name}-${idx}`}
                    data-testid={`qualification-row-${item.strength.toLowerCase()}`}
                    style={{ borderBottom: '1px solid var(--surface-subtle)' }}
                  >
                    <td style={{ padding: '0.6rem 0.5rem', fontWeight: 600 }}>
                      <span aria-hidden="true" style={{ marginRight: '0.4rem' }}>
                        {meta.icon}
                      </span>
                      {item.name}
                      {item.qualification_type && (
                        <span
                          style={{
                            fontSize: '0.75rem',
                            color: 'var(--muted)',
                            marginLeft: '0.5rem',
                            fontWeight: 400,
                          }}
                        >
                          ({item.qualification_type})
                        </span>
                      )}
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      <span
                        className={`tag ${meta.badgeClass}`}
                        data-testid={`strength-tag-${item.strength.toLowerCase()}`}
                        style={{
                          fontSize: '0.75rem',
                          padding: '0.2rem 0.5rem',
                          borderRadius: '4px',
                          fontWeight: 600,
                          backgroundColor:
                            item.strength === 'REQUIRED'
                              ? 'var(--warning-bg)'
                              : item.strength === 'PREFERRED'
                              ? 'var(--info-bg)'
                              : item.strength === 'NICE_TO_HAVE'
                              ? 'var(--good-bg)'
                              : 'var(--surface-subtle)',
                          color:
                            item.strength === 'REQUIRED'
                              ? 'var(--warning)'
                              : item.strength === 'PREFERRED'
                              ? 'var(--info)'
                              : item.strength === 'NICE_TO_HAVE'
                              ? 'var(--good)'
                              : 'var(--text)',
                        }}
                      >
                        {item.strength}
                      </span>
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem' }}>
                      <span
                        className={`status ${isCovered ? 'good' : 'warn'}`}
                        data-testid="candidate-status-tag"
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '0.25rem',
                          fontWeight: 600,
                          fontSize: '0.8125rem',
                          color: isCovered ? 'var(--good)' : 'var(--warning)',
                        }}
                      >
                        {isCovered ? '✓ Covered in Profile' : '✗ Missing from Facts'}
                      </span>
                    </td>
                    <td style={{ padding: '0.6rem 0.5rem', fontSize: '0.8125rem', color: 'var(--text)' }}>
                      {item.score_impact}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
