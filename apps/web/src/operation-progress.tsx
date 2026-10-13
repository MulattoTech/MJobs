import React from 'react';

/** Safe projection of the proposed GET /operations response. Raw params and provider payloads are intentionally absent. */
export type OperationProgressRecord = {
  operation_id: string;
  operation_type: string;
  entity_id: string;
  state: string;
  current_step?: string | null;
  progress?: number | null;
  started_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
  next_attempt_at?: string | null;
  retry_count?: number | null;
  recovery_summary?: string | null;
  preparation_priority?: { tier: number; effective_tier: number; queue_priority: string;
    enqueued_at: string; aging_interval_seconds: number; basis: string } | null;
};

export type OperationProgressProps = {
  operations: OperationProgressRecord[];
  loading?: boolean;
  error?: unknown;
  recovered?: boolean;
};

const OPERATION_TYPES: Record<string, string> = {
  APPLY_MOCK: 'Mock application',
  BACKUP: 'Backup',
  COMPANY_DISCOVER: 'Company discovery',
  DISCOVER: 'Discovery',
  DOCUMENT_GENERATE: 'Document generation',
  DOCUMENT_GENERATION: 'Document generation',
  PREPARE: 'Document preparation',
  RESTORE: 'Restore',
  RESUME_EXTRACT: 'Resume extraction',
};

const STATES: Record<string, { label: string; tone: 'normal' | 'success' | 'attention' }> = {
  BLOCKED: { label: 'Blocked', tone: 'attention' },
  CANCELLED: { label: 'Cancelled', tone: 'normal' },
  COMPLETED: { label: 'Completed', tone: 'success' },
  DEAD_LETTERED: { label: 'Needs attention', tone: 'attention' },
  FAILED: { label: 'Failed', tone: 'attention' },
  QUEUED: { label: 'Queued', tone: 'normal' },
  RECOVERED: { label: 'Recovered', tone: 'success' },
  RUNNING: { label: 'In progress', tone: 'normal' },
};

const RECOVERY_SUMMARIES: Record<string, string> = {
  MANUAL_ACTION_REQUIRED: 'Manual recovery is required.',
  NONE: 'No recovery action is available.',
  RESUMED_AFTER_RESTART: 'The operation resumed after an interruption.',
  RETRY_BLOCKED: 'Retry is blocked pending review.',
  SAFE_TO_RETRY: 'A safe retry is available.',
};

const SAFE_STEPS: Record<string, string> = {
  COMPLETED: 'Completed',
  FETCHING_POSTINGS: 'Fetching postings',
  FINALIZING: 'Finalizing',
  NORMALIZING: 'Normalizing results',
  PERSISTING: 'Saving results',
  PREPARING: 'Preparing',
  QUEUED: 'Queued',
  RECOVERING: 'Recovering',
  RENDERING: 'Rendering',
  RETRYING: 'Retrying',
  RETRY_WAIT: 'Waiting to retry',
  RUNNING: 'In progress',
  VALIDATING: 'Validating',
  WAITING_FOR_CAPACITY: 'Waiting for capacity',
};

function code(value: unknown): string | null {
  return typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(value) ? value : null;
}

export function operationTypeLabel(value: unknown): string {
  const safeCode = code(value);
  return safeCode ? OPERATION_TYPES[safeCode] || 'Background operation' : 'Background operation';
}

export function operationStepLabel(value: unknown): string {
  const safeCode = code(value);
  return safeCode ? SAFE_STEPS[safeCode] || 'Progress step unavailable' : 'Progress step unavailable';
}

function entityLabel(value: unknown): string {
  if (typeof value !== 'string') return 'Unavailable';
  const normalized = value.trim();
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const namedId = /^(profile|application|search-profile|resume|source|company|job)-[A-Za-z0-9_-]{1,64}$/i;
  return uuid.test(normalized) || namedId.test(normalized) ? normalized : 'Unavailable';
}

function timestamp(label: string, value: unknown) {
  if (typeof value !== 'string' || !value.trim()) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return <div><dt>{label}</dt><dd><time dateTime={date.toISOString()}>{date.toLocaleString()}</time></dd></div>;
}

function progressValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value) : null;
}

function recoverySummary(value: unknown): string | null {
  if (typeof value !== 'string' || !value) return null;
  const safeCode = code(value);
  return (safeCode && RECOVERY_SUMMARIES[safeCode]) || 'Recovery details are available.';
}

function retryCount(value: unknown): number | null {
  return Number.isSafeInteger(value) && typeof value === 'number' && value >= 0 && value <= 1_000_000 ? value : null;
}

function OperationCard({ operation }: { operation: OperationProgressRecord }) {
  const priority = operation.operation_type === 'PREPARE' ? operation.preparation_priority : null;
  const priorityLabels = ['Fresh excellent match', 'High', 'Normal', 'Deferred', 'Low', 'Unknown evidence'];
  const priorityValid = priority && Number.isInteger(priority.tier) && priority.tier >= 0 && priority.tier <= 5
    && Number.isInteger(priority.effective_tier) && priority.effective_tier >= 0 && priority.effective_tier <= 5;
  const stateCode = code(operation.state);
  const state = (stateCode && STATES[stateCode]) || { label: 'Status unavailable', tone: 'normal' as const };
  const percent = progressValue(operation.progress);
  const retries = retryCount(operation.retry_count);
  const summary = recoverySummary(operation.recovery_summary);
  const needsAttention = state.tone === 'attention';

  return <article className="panel" aria-label={`${operationTypeLabel(operation.operation_type)} operation`}>
    <div className="toolbar">
      <div>
        <h3>{operationTypeLabel(operation.operation_type)}</h3>
        <p>Entity: <code>{entityLabel(operation.entity_id)}</code></p>
      </div>
      <span className={`tag${state.tone === 'attention' ? ' alert' : ''}`} aria-label={`Operation status: ${state.label}`}>
        {state.label}
      </span>
    </div>

    {needsAttention && <p role="alert">This operation needs attention. Review the recovery summary before taking action.</p>}
    {stateCode === 'RECOVERED' && <p role="status">The saved operation state was recovered.</p>}

    <p>Current step: <strong>{operationStepLabel(operation.current_step)}</strong></p>
    {percent === null
      ? <p aria-label="Progress not reported">Progress not reported</p>
      : <div>
        <label>Progress: {percent}% <progress max={100} value={percent}>{percent}%</progress></label>
      </div>}

    <dl>
      {timestamp('Started', operation.started_at)}
      {timestamp('Last updated', operation.updated_at)}
      {timestamp('Completed', operation.completed_at)}
      {stateCode === 'QUEUED' && timestamp('Next retry', operation.next_attempt_at)}
      <div><dt>Retry attempts</dt><dd>{retries === null ? 'Not reported' : retries}</dd></div>
    </dl>
    {priorityValid && <div>
      <p><strong>Preparation priority:</strong> {priorityLabels[priority.tier]}</p>
      <p>Approved skill evidence and posting age determine the initial order; this is not a qualification assessment.</p>
      {operation.state === 'QUEUED' && <p>{priority.effective_tier < priority.tier ? 'Promoted while waiting. ' : ''}
        Waiting work moves up one tier each hour. Running work is not interrupted.</p>}
    </div>}
    {summary && <p><strong>Recovery:</strong> {summary}</p>}
  </article>;
}

export function OperationProgress({ operations, loading = false, error, recovered = false }: OperationProgressProps) {
  return <section className="panel" aria-labelledby="operation-progress-title">
    <div className="toolbar">
      <div>
        <h2 id="operation-progress-title">Background operations</h2>
        <p>Progress is saved independently of the page request.</p>
      </div>
    </div>
    {loading && <p role="status" aria-live="polite">Loading operations…</p>}
    {!loading && Boolean(error) && <p role="alert">Operations could not be loaded. Try refreshing this view.</p>}
    {!loading && !error && recovered && <p role="status">Saved operations were recovered. Their latest recorded state is shown below.</p>}
    {!loading && !error && operations.length === 0 && <p>No operations to show.</p>}
    {!loading && !error && operations.length > 0 && <div aria-label="Operation list">
      {operations.map(operation => <OperationCard key={operation.operation_id} operation={operation} />)}
    </div>}
  </section>;
}
