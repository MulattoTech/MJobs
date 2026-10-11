import React, { useEffect, useState } from 'react';

export type RetentionPolicyRule = {
  id: string;
  category: string;
  max_age_days: number;
};

export type RetentionPolicy = {
  revision: number;
  config_fingerprint: string;
  enabled: boolean;
  pruning_allowed: boolean;
  rules: RetentionPolicyRule[];
  requires_review: boolean;
};

export type RetentionPolicyUpdate = {
  expected_revision: number;
  expected_config_fingerprint: string;
  enabled: boolean;
  max_age_days_by_rule: Record<string, number>;
  confirmed: true;
};

export type RetentionPolicyErrorCode =
  | 'RETENTION_POLICY_INVALID_CONFIG'
  | 'RETENTION_POLICY_REVISION_CONFLICT'
  | 'RETENTION_POLICY_CONFIG_CHANGED'
  | 'RETENTION_POLICY_INVALID_SETTINGS'
  | 'RETENTION_POLICY_ADMIN_DISABLED'
  | 'RETENTION_POLICY_STORE_UNAVAILABLE'
  | 'RETENTION_POLICY_INVALID_RESULT'
  | 'RETENTION_POLICY_READ_FAILED';

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

const CATEGORY_LABELS: Record<string, string> = {
  DEBUG_SCREENSHOT: 'Debug screenshots',
  TEMP_HTML: 'Temporary HTML files',
  RAW_PAYLOAD: 'Temporary raw payloads',
  BROWSER_ARTIFACT: 'Temporary browser artifacts',
  GENERATED_TEMPORARY: 'Temporary generated files',
  ROTATED_LOG: 'Rotated logs',
};

const SAFE_ERROR_CODES = new Set<RetentionPolicyErrorCode>([
  'RETENTION_POLICY_INVALID_CONFIG',
  'RETENTION_POLICY_REVISION_CONFLICT',
  'RETENTION_POLICY_CONFIG_CHANGED',
  'RETENTION_POLICY_INVALID_SETTINGS',
  'RETENTION_POLICY_ADMIN_DISABLED',
  'RETENTION_POLICY_STORE_UNAVAILABLE',
  'RETENTION_POLICY_INVALID_RESULT',
  'RETENTION_POLICY_READ_FAILED',
]);

const ERROR_MESSAGES: Record<RetentionPolicyErrorCode, string> = {
  RETENTION_POLICY_INVALID_CONFIG: 'Retention settings are unavailable because server configuration is invalid.',
  RETENTION_POLICY_REVISION_CONFLICT: 'Settings changed on the server. Review them by reloading before saving again.',
  RETENTION_POLICY_CONFIG_CHANGED: 'Server retention configuration changed. Reload and review the new settings before saving again.',
  RETENTION_POLICY_INVALID_SETTINGS: 'Retention settings were not accepted. Review the bounded age values and try again.',
  RETENTION_POLICY_ADMIN_DISABLED: 'Scheduled cleanup is unavailable because server configuration does not permit pruning.',
  RETENTION_POLICY_STORE_UNAVAILABLE: 'Retention settings are unavailable because the local settings store is not ready.',
  RETENTION_POLICY_INVALID_RESULT: 'The server returned settings that could not be safely displayed.',
  RETENTION_POLICY_READ_FAILED: 'Retention settings could not be loaded. Try reloading them.',
};

export class RetentionPolicyRequestError extends Error {
  readonly code: RetentionPolicyErrorCode;
  readonly status: number;

  constructor(code: RetentionPolicyErrorCode, status: number) {
    super(ERROR_MESSAGES[code]);
    this.name = 'RetentionPolicyRequestError';
    this.code = code;
    this.status = status;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isRetentionPolicyErrorCode(value: unknown): value is RetentionPolicyErrorCode {
  return typeof value === 'string' && SAFE_ERROR_CODES.has(value as RetentionPolicyErrorCode);
}

function errorCodeFrom(body: unknown): RetentionPolicyErrorCode | null {
  if (!isRecord(body)) return null;
  const detail = body.detail;
  const nested = isRecord(detail) ? detail.code : null;
  const candidate = isRetentionPolicyErrorCode(nested) ? nested : body.code;
  return isRetentionPolicyErrorCode(candidate) ? candidate : null;
}

function isPolicyRule(value: unknown): value is RetentionPolicyRule {
  if (!isRecord(value)) return false;
  return Object.keys(value).sort().join(',') === 'category,id,max_age_days'
    && typeof value.id === 'string'
    && /^[A-Za-z0-9_-]{1,40}$/.test(value.id)
    && typeof value.category === 'string'
    && Object.hasOwn(CATEGORY_LABELS, value.category)
    && Number.isInteger(value.max_age_days)
    && Number(value.max_age_days) >= 1
    && Number(value.max_age_days) <= 365;
}

function isRetentionPolicy(value: unknown): value is RetentionPolicy {
  if (!isRecord(value) || Object.keys(value).sort().join(',') !== 'config_fingerprint,enabled,pruning_allowed,requires_review,revision,rules') return false;
  return Number.isInteger(value.revision)
    && Number(value.revision) >= 0
    && typeof value.config_fingerprint === 'string'
    && /^[a-f0-9]{64}$/.test(value.config_fingerprint)
    && typeof value.pruning_allowed === 'boolean'
    && typeof value.enabled === 'boolean'
    && (!value.enabled || value.pruning_allowed)
    && typeof value.requires_review === 'boolean'
    && Array.isArray(value.rules)
    && value.rules.every(isPolicyRule)
    && new Set(value.rules.map(rule => rule.id)).size === value.rules.length;
}

async function readResponse(response: Response): Promise<unknown> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  if (!response.ok) {
    const code = errorCodeFrom(body) ?? (response.status === 409
      ? 'RETENTION_POLICY_REVISION_CONFLICT'
      : response.status === 403
        ? 'RETENTION_POLICY_ADMIN_DISABLED'
        : 'RETENTION_POLICY_READ_FAILED');
    throw new RetentionPolicyRequestError(code, response.status);
  }
  if (!isRetentionPolicy(body)) throw new RetentionPolicyRequestError('RETENTION_POLICY_INVALID_RESULT', response.status);
  return body;
}

export async function loadRetentionPolicy(fetcher: Fetcher = globalThis.fetch): Promise<RetentionPolicy> {
  const response = await fetcher('/retention/policy', {
    method: 'GET',
    headers: { Accept: 'application/json' },
  });
  return await readResponse(response) as RetentionPolicy;
}

export async function saveRetentionPolicy(fetcher: Fetcher, update: RetentionPolicyUpdate): Promise<RetentionPolicy> {
  const response = await fetcher('/retention/policy', {
    method: 'PUT',
    headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
  return await readResponse(response) as RetentionPolicy;
}

export type RetentionPolicyEditorProps = {
  policy: RetentionPolicy;
  ages: Record<string, number>;
  enabled: boolean;
  confirmed: boolean;
  errorCode: RetentionPolicyErrorCode | null;
  saving: boolean;
  statusMessage?: string | null;
  onAgeChange: (ruleId: string, age: number) => void;
  onEnabledChange: (enabled: boolean) => void;
  onConfirmedChange: (confirmed: boolean) => void;
  onSave: () => void;
  onReload: () => void;
};

export function RetentionPolicyEditor({
  policy,
  ages,
  enabled,
  confirmed,
  errorCode,
  saving,
  statusMessage,
  onAgeChange,
  onEnabledChange,
  onConfirmedChange,
  onSave,
  onReload,
}: RetentionPolicyEditorProps) {
  const validAges = policy.rules.every(rule => Number.isInteger(ages[rule.id]) && ages[rule.id] >= 1 && ages[rule.id] <= 365);
  const saveDisabled = saving || !validAges || !confirmed || (enabled && (!policy.pruning_allowed || policy.rules.length === 0));
  const errorMessage = errorCode ? ERROR_MESSAGES[errorCode] : null;

  return <section className="panel" aria-labelledby="retention-policy-heading">
    <h2 id="retention-policy-heading">Retention settings</h2>
    <p className="muted">Choose how long configured transient files are kept. Core application history is never eligible for cleanup.</p>
    <p role="status" aria-live="polite">
      {policy.enabled && policy.pruning_allowed
        ? 'Scheduled cleanup is enabled for configured transient data only.'
        : 'Cleanup is off. Retention preview remains dry-run.'}
    </p>
    {policy.requires_review && <p role="status">Server configuration changed or has no saved consent. Review these defaults and save again to enable scheduled cleanup.</p>}
    <label>
      <input
        type="checkbox"
        aria-label="Enable scheduled cleanup"
        checked={enabled}
        disabled={!policy.pruning_allowed || policy.rules.length === 0 || saving}
        onChange={event => onEnabledChange(event.currentTarget.checked)}
      />
      Enable scheduled cleanup
    </label>
    {!policy.pruning_allowed && <p className="muted">Scheduled cleanup is unavailable because server configuration does not permit pruning (pruning disabled or dry-run mode active).</p>}
    {policy.rules.length === 0 && <p className="muted">No managed transient categories are configured for cleanup.</p>}
    <div className="two-col">
      {policy.rules.map(rule => <label key={rule.id}>
        Maximum age for {CATEGORY_LABELS[rule.category]}
        <input
          aria-label={`Maximum age for ${CATEGORY_LABELS[rule.category]}`}
          type="number"
          min={1}
          max={365}
          step={1}
          value={ages[rule.id] ?? rule.max_age_days}
          disabled={saving}
          onChange={event => onAgeChange(rule.id, Number(event.currentTarget.value))}
        />
        <span className="muted">Files older than this age may be removed by the configured scheduled cleanup.</span>
      </label>)}
    </div>
    <label>
      <input
        type="checkbox"
        aria-label="I understand cleanup applies only to configured transient data and never to application history."
        checked={confirmed}
        disabled={saving}
        onChange={event => onConfirmedChange(event.currentTarget.checked)}
      />
      I understand cleanup applies only to configured transient data and never to application history.
    </label>
    <p className="muted">Only server-configured managed categories are eligible. Their locations and file patterns are controlled by the server.</p>
    {errorMessage && <div role="alert">
      <p>{errorMessage}</p>
      {(errorCode === 'RETENTION_POLICY_REVISION_CONFLICT' || errorCode === 'RETENTION_POLICY_CONFIG_CHANGED') && <button type="button" onClick={onReload} disabled={saving}>Reload settings</button>}
      {errorCode === 'RETENTION_POLICY_INVALID_CONFIG' && <button type="button" onClick={onReload} disabled={saving}>Reload settings</button>}
    </div>}
    {statusMessage && <p role="status" aria-live="polite">{statusMessage}</p>}
    <button type="button" onClick={onSave} disabled={saveDisabled}>{saving ? 'Saving retention settings…' : 'Save retention settings'}</button>
  </section>;
}

type RetentionPolicyControlsProps = {
  fetcher?: Fetcher;
};

export function RetentionPolicyControls({ fetcher = globalThis.fetch }: RetentionPolicyControlsProps = {}) {
  const [policy, setPolicy] = useState<RetentionPolicy | null>(null);
  const [ages, setAges] = useState<Record<string, number>>({});
  const [enabled, setEnabled] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [errorCode, setErrorCode] = useState<RetentionPolicyErrorCode | null>(null);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [reloadSequence, setReloadSequence] = useState(0);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setErrorCode(null);
    setStatusMessage(null);
    void loadRetentionPolicy(fetcher).then(value => {
      if (!current) return;
      setPolicy(value);
      setAges(Object.fromEntries(value.rules.map(rule => [rule.id, rule.max_age_days])));
      setEnabled(value.enabled);
      setConfirmed(false);
      setErrorCode(null);
      setLoading(false);
    }).catch(error => {
      if (!current) return;
      const code = error instanceof RetentionPolicyRequestError ? error.code : 'RETENTION_POLICY_READ_FAILED';
      setErrorCode(code);
      setLoading(false);
    });
    return () => { current = false; };
  }, [fetcher, reloadSequence]);

  const save = async () => {
    if (!policy || saving) return;
    if (!confirmed || (enabled && (!policy.pruning_allowed || policy.rules.length === 0))) return;
    if (!policy.rules.every(rule => Number.isInteger(ages[rule.id]) && ages[rule.id] >= 1 && ages[rule.id] <= 365)) return;
    setSaving(true);
    setErrorCode(null);
    setStatusMessage(null);
    try {
      const updated = await saveRetentionPolicy(fetcher, {
        expected_revision: policy.revision,
        expected_config_fingerprint: policy.config_fingerprint,
        enabled,
        max_age_days_by_rule: Object.fromEntries(policy.rules.map(rule => [rule.id, ages[rule.id]])),
        confirmed: true,
      });
      setPolicy(updated);
      setAges(Object.fromEntries(updated.rules.map(rule => [rule.id, rule.max_age_days])));
      setEnabled(updated.enabled);
      setConfirmed(false);
      setStatusMessage(updated.enabled
        ? 'Retention settings saved. Scheduled cleanup is enabled for configured transient data only.'
        : 'Retention settings saved. Cleanup remains off and preview remains dry-run.');
    } catch (error) {
      setErrorCode(error instanceof RetentionPolicyRequestError ? error.code : 'RETENTION_POLICY_READ_FAILED');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <section className="panel" aria-labelledby="retention-policy-heading">
    <h2 id="retention-policy-heading">Retention settings</h2>
    <p role="status" aria-live="polite">Loading retention settings</p>
  </section>;

  if (!policy) return <section className="panel" aria-labelledby="retention-policy-heading">
    <h2 id="retention-policy-heading">Retention settings</h2>
    <div role="alert">{ERROR_MESSAGES[errorCode ?? 'RETENTION_POLICY_READ_FAILED']}</div>
    <button type="button" onClick={() => setReloadSequence(value => value + 1)}>Reload settings</button>
  </section>;

  return <RetentionPolicyEditor
    policy={policy}
    ages={ages}
    enabled={enabled}
    confirmed={confirmed}
    errorCode={errorCode}
    saving={saving}
    statusMessage={statusMessage}
    onAgeChange={(ruleId, age) => setAges(previous => ({ ...previous, [ruleId]: age }))}
    onEnabledChange={setEnabled}
    onConfirmedChange={setConfirmed}
    onSave={() => { void save(); }}
    onReload={() => setReloadSequence(value => value + 1)}
  />;
}
