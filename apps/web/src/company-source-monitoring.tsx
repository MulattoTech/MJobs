import React, { useState } from 'react';

export type Monitoring = {
  revision: number;
  enabled: boolean;
  poll_interval: number;
  status: string;
  failure_count: number;
  last_attempt: string | null;
  next_due: string | null;
  last_error: string | null;
  operation_id: string | null;
  rediscovery_operation_id: string | null;
  authorization: string | null;
};

type Props = {
  sourceId: string;
  sourceVersion: string;
  sourceEnabled: boolean;
  supported: boolean;
  monitoring: Monitoring | null;
  busy: boolean;
  profileSelected: boolean;
  onMonitor: (pollInterval: number) => Promise<void>;
  onRun: () => Promise<void>;
};

const statusLabels: Record<string, string> = {
  IDLE: 'Idle', SCHEDULED: 'Scheduled', HEALTHY: 'Healthy', QUEUED: 'Queued', RUNNING: 'Running', COMPLETED: 'Completed',
  FAILED: 'Failed', BLOCKED: 'Blocked', REDISCOVERY_REQUIRED: 'Review required',
};
const reviewStatuses = new Set(['BLOCKED', 'REDISCOVERY_REQUIRED']);
const updateStatuses = new Set(['IDLE', 'HEALTHY', 'FAILED']);

function statusLabel(monitoring: Monitoring): string {
  if (['IDLE', 'SCHEDULED', 'QUEUED', 'RUNNING'].includes(monitoring.status) &&
      monitoring.failure_count > 0 && monitoring.failure_count < 3) {
    return 'Degraded';
  }
  return statusLabels[monitoring.status] || 'Status unavailable';
}
function displayTime(value: string | null): string {
  if (!value) return 'Not recorded';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? 'Unknown' : parsed.toLocaleString();
}

export function CompanySourceMonitoring({sourceId, sourceVersion, sourceEnabled, supported, monitoring, busy, profileSelected, onMonitor, onRun}: Props) {
  const [intervalMinutes, setIntervalMinutes] = useState(() => monitoring?.poll_interval ? Math.max(5, Math.round(monitoring.poll_interval / 60)) : 60);
  const canReview = !monitoring || updateStatuses.has(monitoring.status) || reviewStatuses.has(monitoring.status);
  const initialReview = !monitoring || reviewStatuses.has(monitoring.status);
  const canRun = !!monitoring?.enabled && ['IDLE', 'HEALTHY', 'FAILED'].includes(monitoring.status)
    && sourceEnabled && supported && profileSelected && !busy;
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void onMonitor(intervalMinutes * 60);
  };
  void sourceId;
  void sourceVersion;
  if (!supported) return <section className="panel source-monitoring" aria-label="Source monitoring">
    <h4>Source monitoring</h4><p className="muted">Monitoring is not supported for this source.</p>
  </section>;
  return <section className="panel source-monitoring" aria-label="Source monitoring">
    <div className="toolbar"><div><h4>Source monitoring</h4><p>Bounded public read checks can refresh this reviewed source. Review does not verify employer identity.</p></div>
      {monitoring && <span className="tag">{statusLabel(monitoring)}</span>}</div>
    {!sourceEnabled && <p className="alert">Enable the existing source before monitoring can run.</p>}
    {monitoring && <dl><div><dt>Last attempt</dt><dd>{displayTime(monitoring.last_attempt)}</dd></div><div><dt>Next check</dt><dd>{displayTime(monitoring.next_due)}</dd></div>{monitoring.failure_count > 0 && <div><dt>Consecutive failures</dt><dd>{monitoring.failure_count}{monitoring.failure_count >= 3 ? ' (automatic retries stopped)' : ' (retry scheduled)'}</dd></div>}</dl>}
    {monitoring?.last_error && <p className="error-text">{monitoring.last_error}</p>}
    {canReview && <form onSubmit={submit}><label>Check interval (minutes)<input type="number" min={5} max={10080} required value={intervalMinutes} disabled={busy} onChange={event => setIntervalMinutes(Number(event.target.value))} /></label>
      <button type="submit" disabled={busy || !sourceEnabled}>{initialReview ? 'Monitor this reviewed source' : 'Update monitoring'}</button>
      <p className="muted">The existing source must be enabled. Monitoring reads public pages within their access rules; it does not authorize submission.</p>
    </form>}
    {!canReview && monitoring && <p className="muted">Monitoring is active or awaiting its next durable check.</p>}
    {canRun && <button type="button" className="quiet" onClick={() => void onRun()}>Run now</button>}
    {monitoring && ['IDLE', 'HEALTHY', 'FAILED'].includes(monitoring.status) && !canRun && sourceEnabled && profileSelected && !busy && <p className="muted">Run now is available when monitoring is enabled.</p>}
  </section>;
}
