import React from 'react';

export const MANAGED_RETENTION_CATEGORIES = [
  { id: 'debug_screenshots', label: 'Debug screenshots' },
  { id: 'temporary_html', label: 'Temporary HTML' },
  { id: 'old_raw_payloads', label: 'Old raw payloads' },
  { id: 'temporary_browser_artifacts', label: 'Temporary browser artifacts' },
  { id: 'temporary_generated_files', label: 'Temporary generated files' },
  { id: 'rotated_logs', label: 'Rotated logs' },
] as const;

export type RetentionArtifactCategory = typeof MANAGED_RETENTION_CATEGORIES[number]['id'];
export type RetentionPanelStatus = 'loading' | 'ready' | 'error';
export type RetentionSweepStatus = 'NOT_RUN' | 'RUNNING' | 'SUCCEEDED' | 'FAILED';

export interface RetentionPolicy {
  category: RetentionArtifactCategory;
  enabled: boolean;
  maxAgeDays: number;
}

export interface RetentionPreviewRow {
  category: RetentionArtifactCategory;
  eligibleCount: number;
  eligibleBytes: number;
}

export interface RetentionLastSweep {
  status: RetentionSweepStatus;
  completedAt?: string;
  removedCount: number;
  removedBytes: number;
}

export interface RetentionSnapshot {
  policies: RetentionPolicy[];
  preview: RetentionPreviewRow[];
  lastSweep: RetentionLastSweep;
}

export interface BoundedRetentionCleanup {
  supported: boolean;
  previewId: string;
  maxAgeDays: number;
  maxItems: number;
  maxBytes: number;
  eligibleCount: number;
  eligibleBytes: number;
  busy?: boolean;
  onApply: (previewId: string) => void;
}

export interface TransientRetentionProps {
  status: RetentionPanelStatus;
  snapshot?: RetentionSnapshot;
  cleanup?: BoundedRetentionCleanup;
  onRefresh?: () => void;
}

function countLabel(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

function safeCount(value: number): number {
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function safeBytes(value: number): number {
  return Number.isFinite(value) && value >= 0 ? Math.floor(value) : 0;
}

function formatBytes(value: number): string {
  const bytes = safeBytes(value);
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = bytes / 1024;
  let unit = units[0];
  for (let index = 1; amount >= 1024 && index < units.length; index += 1) {
    amount /= 1024;
    unit = units[index];
  }
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(amount)} ${unit}`;
}

function sweepLabel(status: RetentionSweepStatus): string {
  switch (status) {
    case 'NOT_RUN': return 'Not run yet';
    case 'RUNNING': return 'Running';
    case 'SUCCEEDED': return 'Succeeded';
    case 'FAILED': return 'Failed';
  }
}

function safeTimestamp(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Time unavailable' : parsed.toISOString();
}

function isManagedCategory(category: unknown): category is RetentionArtifactCategory {
  return typeof category === 'string' && MANAGED_RETENTION_CATEGORIES.some(managed => managed.id === category);
}

function validPolicy(policy: RetentionPolicy): boolean {
  return isManagedCategory(policy.category)
    && typeof policy.enabled === 'boolean'
    && Number.isInteger(policy.maxAgeDays) && policy.maxAgeDays >= 1 && policy.maxAgeDays <= 365;
}

function validPreviewRow(preview: RetentionPreviewRow): boolean {
  return isManagedCategory(preview.category)
    && Number.isInteger(preview.eligibleCount) && preview.eligibleCount >= 0
    && Number.isInteger(preview.eligibleBytes) && preview.eligibleBytes >= 0;
}

function snapshotCoversManagedCategories(snapshot: RetentionSnapshot): boolean {
  const expected = new Set(MANAGED_RETENTION_CATEGORIES.map(category => category.id));
  const policyCategories = snapshot.policies.map(policy => policy.category);
  const previewCategories = snapshot.preview.map(preview => preview.category);
  return policyCategories.length === expected.size
    && previewCategories.length === expected.size
    && snapshot.policies.every(validPolicy)
    && snapshot.preview.every(validPreviewRow)
    && new Set(policyCategories).size === expected.size
    && new Set(previewCategories).size === expected.size
    && [...expected].every(category => policyCategories.includes(category) && previewCategories.includes(category));
}

function validCleanup(cleanup: BoundedRetentionCleanup | undefined, snapshot: RetentionSnapshot): cleanup is BoundedRetentionCleanup {
  return cleanup?.supported === true
    && (snapshot.lastSweep.status === 'SUCCEEDED' || snapshot.lastSweep.status === 'NOT_RUN')
    && snapshotCoversManagedCategories(snapshot)
    && cleanup.previewId.trim().length > 0
    && Number.isInteger(cleanup.maxAgeDays) && cleanup.maxAgeDays > 0 && cleanup.maxAgeDays <= 365
    && Number.isInteger(cleanup.maxItems) && cleanup.maxItems > 0
    && Number.isInteger(cleanup.maxBytes) && cleanup.maxBytes > 0
    && Number.isInteger(cleanup.eligibleCount) && cleanup.eligibleCount > 0
    && Number.isInteger(cleanup.eligibleBytes) && cleanup.eligibleBytes >= 0
    && cleanup.maxItems >= cleanup.eligibleCount
    && cleanup.maxBytes >= cleanup.eligibleBytes
    && cleanup.eligibleCount === snapshot.preview.reduce((total, row) => total + safeCount(row.eligibleCount), 0)
    && cleanup.eligibleBytes === snapshot.preview.reduce((total, row) => total + safeBytes(row.eligibleBytes), 0)
    && typeof cleanup.onApply === 'function';
}

export function TransientRetention({ status, snapshot, cleanup, onRefresh }: TransientRetentionProps) {
  const canApplyCleanup = status === 'ready' && snapshot !== undefined && validCleanup(cleanup, snapshot);

  return (
    <section className="panel" aria-labelledby="transient-retention-title" aria-busy={status === 'loading'}>
      <h2 id="transient-retention-title">Transient data retention</h2>
      <p>Only these managed transient artifacts are eligible for cleanup. Application history and submitted application records are never included.</p>
      {status === 'loading' ? <p role="status">Loading retention status and cleanup preview…</p> : null}
      {status === 'error' || (status === 'ready' && !snapshot) ? (
        <div className="alert" role="alert">Retention status and preview could not be loaded.</div>
      ) : null}
      {snapshot && status === 'ready' ? (
        <>
          <div className="table-scroll">
            <table>
              <caption>Managed artifact retention policy and current cleanup preview</caption>
              <thead>
                <tr><th scope="col">Managed category</th><th scope="col">Policy</th><th scope="col">Eligible now</th></tr>
              </thead>
              <tbody>
                {MANAGED_RETENTION_CATEGORIES.map(category => {
                  const policy = snapshot.policies.find(row => row.category === category.id);
                  const preview = snapshot.preview.find(row => row.category === category.id);
                  const eligibleCount = safeCount(preview?.eligibleCount ?? 0);
                  const eligibleBytes = safeBytes(preview?.eligibleBytes ?? 0);
                  return (
                    <tr key={category.id}>
                      <th scope="row">{category.label}</th>
                      <td>{policy && validPolicy(policy) ? `${policy.enabled ? 'Enabled' : 'Disabled'} · ${policy.maxAgeDays} day${policy.maxAgeDays === 1 ? '' : 's'}` : 'Policy unavailable'}</td>
                      <td>{countLabel(eligibleCount, 'item')} · {formatBytes(eligibleBytes)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div role="group" aria-labelledby="retention-last-sweep-title">
            <h3 id="retention-last-sweep-title">Last automatic sweep</h3>
            <p>Last sweep: {sweepLabel(snapshot.lastSweep.status)}</p>
            {snapshot.lastSweep.completedAt ? <p>Completed: {safeTimestamp(snapshot.lastSweep.completedAt)}</p> : null}
            {snapshot.lastSweep.status === 'SUCCEEDED' ? (
              <p>{countLabel(safeCount(snapshot.lastSweep.removedCount), 'item')} · {formatBytes(snapshot.lastSweep.removedBytes)} removed</p>
            ) : null}
            {snapshot.lastSweep.status === 'FAILED' ? (
              <div className="alert" role="alert">The last cleanup reported a failure. Refresh the status before applying cleanup.</div>
            ) : null}
            {snapshot.lastSweep.status === 'RUNNING' ? <p role="status">A retention sweep is in progress.</p> : null}
          </div>
          {canApplyCleanup ? (
            <div className="toolbar">
              <p>
                This preview is limited to {countLabel(cleanup.eligibleCount, 'item')}, {formatBytes(cleanup.eligibleBytes)},
                within {cleanup.maxAgeDays} days, at most {cleanup.maxItems} items and {formatBytes(cleanup.maxBytes)}.
              </p>
              <button type="button" disabled={cleanup.busy} onClick={() => cleanup.onApply(cleanup.previewId)}>
                {cleanup.busy ? 'Deleting temporary data…' : 'Delete listed temporary data'}
              </button>
            </div>
          ) : null}
          {onRefresh ? <button type="button" className="quiet" onClick={onRefresh}>Refresh status and preview</button> : null}
        </>
      ) : null}
      {(status === 'error' || (status === 'ready' && !snapshot)) && onRefresh ? (
        <button type="button" className="quiet" onClick={onRefresh}>Retry status load</button>
      ) : null}
    </section>
  );
}
