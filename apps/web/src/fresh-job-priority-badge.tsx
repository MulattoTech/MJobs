import React from 'react';

export type QueueTier = 'CRITICAL' | 'HIGH' | 'NORMAL' | 'DEFERRED' | 'STALE' | 'BACKGROUND';
export type FreshnessBand = 'FRESH' | 'RECENT' | 'AGING' | 'STALE' | 'UNKNOWN';

export type FreshJobPriorityBadgeProps = {
  priorityTier?: QueueTier | null;
  freshnessBand?: FreshnessBand | null;
  matchScore?: number | null;
  matchQuality?: string | null;
  ageHours?: number | null;
  explanation?: string | null;
  compact?: boolean;
};

const TIER_META: Record<QueueTier, { label: string; icon: string; className: string; description: string }> = {
  CRITICAL: {
    label: 'Expedited • New Excellent Match',
    icon: '⚡',
    className: 'badge-tier-critical',
    description: 'New excellent match processed first for expedited review.',
  },
  HIGH: {
    label: 'High Priority • Fresh Match',
    icon: '⭐',
    className: 'badge-tier-high',
    description: 'Fresh strong match scheduled for prioritized review.',
  },
  NORMAL: {
    label: 'Standard Queue',
    icon: '⏱️',
    className: 'badge-tier-normal',
    description: 'Standard review queue order.',
  },
  DEFERRED: {
    label: 'Deferred • Aging Posting',
    icon: '⏳',
    className: 'badge-tier-deferred',
    description: 'Aging posting deferred behind fresher opportunities.',
  },
  STALE: {
    label: 'Low Priority • Stale Posting',
    icon: '📉',
    className: 'badge-tier-stale',
    description: 'Stale or low-match opportunity processed after fresh jobs.',
  },
  BACKGROUND: {
    label: 'Background Queue',
    icon: '📋',
    className: 'badge-tier-background',
    description: 'Pending evaluation queue.',
  },
};

export function FreshJobPriorityBadge({
  priorityTier,
  freshnessBand,
  matchScore,
  matchQuality,
  ageHours,
  explanation,
  compact = false,
}: FreshJobPriorityBadgeProps) {
  if (!priorityTier || !(priorityTier in TIER_META)) {
    return null;
  }

  const meta = TIER_META[priorityTier];

  if (compact) {
    return (
      <span
        className={`tag ${meta.className}`}
        role="status"
        aria-label={`Queue priority: ${meta.label}`}
        title={explanation || meta.description}
      >
        <span aria-hidden="true">{meta.icon}</span> {meta.label}
      </span>
    );
  }

  return (
    <div
      className={`panel fresh-job-priority-card ${meta.className}`}
      role="region"
      aria-label="Fresh job queue priority card"
    >
      <div className="toolbar">
        <div>
          <span className="eyebrow">QUEUE DISPATCH PRIORITY</span>
          <h3>
            <span aria-hidden="true">{meta.icon}</span> {meta.label}
          </h3>
        </div>
        <span className={`status ${priorityTier === 'CRITICAL' || priorityTier === 'HIGH' ? 'good' : 'warn'}`}>
          Tier: {priorityTier}
        </span>
      </div>
      <p>{explanation || meta.description}</p>
      <dl className="two-col">
        {matchScore !== undefined && matchScore !== null && (
          <>
            <dt>Match alignment</dt>
            <dd>
              {matchScore}% {matchQuality ? `(${matchQuality})` : ''}
            </dd>
          </>
        )}
        {freshnessBand && (
          <>
            <dt>Posting freshness</dt>
            <dd>
              {freshnessBand}{' '}
              {ageHours !== undefined && ageHours !== null
                ? ageHours < 24
                  ? `(${ageHours.toFixed(1)}h old)`
                  : `(${(ageHours / 24).toFixed(1)}d old)`
                : ''}
            </dd>
          </>
        )}
      </dl>
      <p className="muted">
        <strong>Queue rule (MJ-REQ-119):</strong> New excellent matches are processed ahead of stale lower-quality jobs.
        This orders review queues and does not alter underlying qualification facts.
      </p>
    </div>
  );
}
