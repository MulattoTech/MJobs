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
export type RetentionPreviewStatus = 'OK' | 'BLOCKED' | 'PARTIAL';
export type RetentionCleanupControl =
  | { source: 'PERSISTED_POLICY'; state: 'ALLOWED' | 'DISABLED' | 'REVIEW_REQUIRED' | 'SERVER_DISABLED'; policyRevision: number }
  | { source: 'SERVER_CONFIGURATION'; state: 'ALLOWED' | 'SERVER_DISABLED'; policyRevision: null };

export interface RetentionPolicy {
  category: RetentionArtifactCategory;
  /** Legacy callers may omit configured; enabled alone never proves global cleanup permission. */
  configured?: boolean;
  enabled: boolean;
  maxAgeDays: number;
}

export interface RetentionPreviewRow {
  category: RetentionArtifactCategory;
  eligibleCount: number;
  eligibleBytes: number;
  available?: boolean;
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
  cleanupControl?: RetentionCleanupControl;
  previewStatus?: RetentionPreviewStatus;
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
    && (policy.configured === undefined || typeof policy.configured === 'boolean')
    && Number.isInteger(policy.maxAgeDays) && policy.maxAgeDays >= 1 && policy.maxAgeDays <= 365;
}

function validPreviewRow(preview: RetentionPreviewRow): boolean {
  return isManagedCategory(preview.category)
    && Number.isSafeInteger(preview.eligibleCount) && preview.eligibleCount >= 0
    && Number.isSafeInteger(preview.eligibleBytes) && preview.eligibleBytes >= 0
    && (preview.available === undefined || typeof preview.available === 'boolean');
}

export function isRetentionCleanupControl(value: unknown): value is RetentionCleanupControl {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const control = value as Record<string, unknown>;
  if (Object.keys(control).length !== 3 ||
      !Object.hasOwn(control, 'source') || !Object.hasOwn(control, 'state') || !Object.hasOwn(control, 'policyRevision')) return false;
  if (control.source === 'SERVER_CONFIGURATION') {
    return control.policyRevision === null && (control.state === 'ALLOWED' || control.state === 'SERVER_DISABLED');
  }
  return control.source === 'PERSISTED_POLICY'
    && typeof control.policyRevision === 'number' && Number.isSafeInteger(control.policyRevision) && control.policyRevision >= 0
    && ['ALLOWED', 'DISABLED', 'REVIEW_REQUIRED', 'SERVER_DISABLED'].includes(control.state as string)
    && (control.policyRevision >= 1 || (control.state !== 'ALLOWED' && control.state !== 'DISABLED'));
}

function cleanupPermissionLabel(control: RetentionCleanupControl | undefined): string {
  if (!isRetentionCleanupControl(control)) return 'Unknown';
  switch (control.state) {
    case 'ALLOWED': return control.source === 'PERSISTED_POLICY' ? 'Allowed by saved settings' : 'Allowed by server configuration';
    case 'DISABLED': return 'Disabled by saved settings';
    case 'REVIEW_REQUIRED': return 'Review required';
    case 'SERVER_DISABLED': return 'Disabled by server configuration';
  }
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
    && isRetentionCleanupControl(snapshot.cleanupControl) && snapshot.cleanupControl.state === 'ALLOWED'
    && snapshot.previewStatus === 'OK'
    && snapshot.preview.every(row => row.available !== false)
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
                <tr><th scope="col">Managed category</th><th scope="col">Configured rule</th><th scope="col">Preview candidates</th></tr>
              </thead>
              <tbody>
                {MANAGED_RETENTION_CATEGORIES.map(category => {
                  const policy = snapshot.policies.find(row => row.category === category.id);
                  const preview = snapshot.preview.find(row => row.category === category.id);
                  const eligibleCount = safeCount(preview?.eligibleCount ?? 0);
                  const eligibleBytes = safeBytes(preview?.eligibleBytes ?? 0);
                  const configured = policy && validPolicy(policy) && (policy.configured ?? policy.enabled);
                  const countsAvailable = preview && validPreviewRow(preview) && preview.available !== false && snapshot.previewStatus !== 'BLOCKED';
                  return (
                    <tr key={category.id}>
                      <th scope="row">{category.label}</th>
                      <td>{policy && validPolicy(policy) ? configured ? `Configured · ${policy.maxAgeDays} day${policy.maxAgeDays === 1 ? '' : 's'}` : 'Not configured' : 'Policy unavailable'}</td>
                      <td>{snapshot.previewStatus === 'BLOCKED' ? 'Unavailable'
                        : !configured ? 'Not configured'
                        : countsAvailable ? `${snapshot.previewStatus === 'PARTIAL' ? 'Observed: ' : ''}${countLabel(eligibleCount, 'item')} · ${formatBytes(eligibleBytes)}`
                        : 'Unavailable'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div role="group" aria-labelledby="retention-cleanup-control-title">
            <h3 id="retention-cleanup-control-title">Cleanup permission</h3>
            <p role="status">Cleanup permission: {cleanupPermissionLabel(snapshot.cleanupControl)}</p>
            {isRetentionCleanupControl(snapshot.cleanupControl)
              ? snapshot.cleanupControl.source === 'PERSISTED_POLICY'
                ? snapshot.cleanupControl.policyRevision === 0
                  ? <p>No reviewed cleanup policy is available.</p>
                  : <p>Saved policy revision: {snapshot.cleanupControl.policyRevision}</p>
                : <p>No saved policy revision was used; this status comes from server configuration.</p>
              : <p>This snapshot does not report whether cleanup is allowed. Configured rules alone do not enable deletion.</p>}
            {snapshot.cleanupControl?.state === 'REVIEW_REQUIRED' ? <p>Review the current retention settings and save consent before cleanup can be allowed.</p> : null}
            <p>This preview does not delete files. Cleanup permission does not confirm that scheduled cleanup is running.</p>
          </div>
          {snapshot.previewStatus === 'BLOCKED' ? <p role="alert">Preview blocked. Candidate counts are unavailable; no successful scan is recorded.</p>
            : snapshot.previewStatus === 'PARTIAL' ? <p role="alert">Preview incomplete. Shown counts are partial observations; unreported categories remain unavailable.</p>
            : snapshot.previewStatus === 'OK' ? <p>Preview completed for the configured rules. Listed candidates have not been deleted by this preview.</p>
            : <p role="status">Preview status: Unknown. These displayed counts do not prove a complete scan.</p>}
          <div role="group" aria-labelledby="retention-last-sweep-title">
            <h3 id="retention-last-sweep-title">Last recorded sweep</h3>
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
