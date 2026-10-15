import React, { useEffect, useRef, useState } from 'react';
import { api as defaultApi } from './api';

export const MAX_PROFILE_RESTORE_BYTES = 5 * 1024 * 1024;

export type RestoreBackup = Record<string, unknown>;
export type RestoreApi = (path: string, init?: RequestInit) => Promise<unknown>;

export type RestoreBackupParseResult =
  | { ok: true; backup: RestoreBackup }
  | { ok: false; reason: 'file_too_large' | 'invalid_json' | 'unsupported_format' };

export type ProfileRestorePreview = {
  fingerprint: string;
  sourceProfile: { name: string; email: string };
  counts: { facts: number; answers: number; uploaded_files: number; generated_documents: number; jobs: number; applications: number; search_profiles: number; company_discovery_history: number };
  warnings: string[];
  omittedSections: string[];
};

export type RestoreRequestResult =
  | { ok: true; profileId: string }
  | { ok: false; reason: 'confirmation_required' | 'invalid_response' };

const COUNT_LABELS = {
  facts: 'fact', answers: 'answer', uploaded_files: 'uploaded file',
  generated_documents: 'generated document', jobs: 'job record', applications: 'application record',
  search_profiles: 'saved search profile', company_discovery_history: 'recovered discovery history record',
} as const;

const WARNING_LABELS: Record<string, string> = {
  IMPORTED_FACTS_REQUIRE_REVIEW: 'Imported facts are unverified and require review before use.',
  IMPORTED_ANSWERS_REQUIRE_REVIEW: 'Imported answers require review before reuse.',
  SEARCH_PROFILES_DISABLED: 'Imported search profiles are disabled until reviewed.',
  AUTOMATION_NOT_RESTORED: 'Automation settings are not activated by restore.',
  UPLOADED_FILES_NOT_INCLUDED: 'Original uploaded files are not included in this backup.',
  RESUME_REVISION_METADATA_PARTIALLY_RESTORED: 'Uploaded resume history was partially restored because file bytes were unavailable.',
  GENERATED_DOCUMENTS_NOT_INCLUDED: 'Some generated document bytes are not included in this backup.',
  APPLICATION_HISTORY_PARTIALLY_RESTORED: 'Some history could not be restored because its safe links or document bytes were unavailable.',
  APPLICATION_STATES_REQUIRE_REVIEW: 'Imported applications are never replayed; active states require review and ambiguous submissions remain unconfirmed.',
  CAREER_STORIES_REQUIRE_REVIEW: 'Imported career stories are pending review in the new profile.',
  SOURCE_CANDIDATES_DISABLED: 'Imported source candidates are disabled and require review before collection.',
  COMPANY_DISCOVERY_HISTORY_ARCHIVED: 'Company discovery records were recovered as read-only history.',
  DISCOVERY_HISTORY_REFERENCES_INCOMPLETE: 'Some historical job references were not present in the backup.',
  DISCOVERY_HISTORY_METADATA_PARTIAL: 'Some discovery history metadata was missing from this older backup.',
};

const OMITTED_SECTION_LABELS: Record<string, string> = {
  ai_task_provider_model: 'AI task provider and model choices (restored disabled)',
  ai_cost_policy: 'AI cost policy',
  answer_confidence_policy: 'Answer confidence policy',
  provider_credentials: 'Provider credentials',
  browser_state: 'Private browser state',
  worker_operations: 'Queued or running operations',
  source_monitoring_and_feed_state: 'Source monitoring and feed state',
  posting_raw_diagnostics: 'Raw posting diagnostics',
  resume_revision_metadata_without_upload_bytes: 'Resume revision metadata without its original upload bytes',
  application_attempt_execution_steps: 'Application attempt execution steps (not replayed)',
  application_submission_traces_and_confirmations: 'Application submission traces and confirmation receipts (not portable)',
  document_generation_context: 'Private document generation context',
  unsafe_application_routes: 'Application routes rejected because their URLs were unsafe',
  dependent_history_without_safe_routes: 'Dependent matches, documents, attempts and preparation history without safe routes',
  jobs_without_safe_routes: 'Jobs without a safe application route',
  applications_without_safe_routes: 'Applications without a safe route',
  route_choices_without_safe_routes: 'Route choices without a safe route',
  generated_documents_without_verified_bytes: 'Generated document records without verified file bytes',
  ai_job_evaluation_history_without_safe_jobs: 'AI job evaluation history without a restored safe job',
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= maxLength;
}

/** Reads only supported, size-bounded profile JSON. The payload is never included in errors. */
export async function parseRestoreBackup(file: Blob): Promise<RestoreBackupParseResult> {
  if (file.size > MAX_PROFILE_RESTORE_BYTES) return { ok: false, reason: 'file_too_large' };

  let text: string;
  try {
    text = await file.text();
  } catch {
    return { ok: false, reason: 'invalid_json' };
  }
  if (new TextEncoder().encode(text).byteLength > MAX_PROFILE_RESTORE_BYTES) {
    return { ok: false, reason: 'file_too_large' };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, reason: 'invalid_json' };
  }
  if (!isRecord(parsed) || parsed.schema_version !== 1 ||
      !['profile_backup', 'profile_json_export'].includes(String(parsed.export_type))) {
    return { ok: false, reason: 'unsupported_format' };
  }
  return { ok: true, backup: parsed };
}

function projectPreview(value: unknown): ProfileRestorePreview | null {
  if (!isRecord(value) || !isBoundedString(value.fingerprint, 128) || !isRecord(value.source_profile) ||
      !isBoundedString(value.source_profile.name, 200) || !isBoundedString(value.source_profile.email, 320) ||
      !isRecord(value.counts) || !Array.isArray(value.warnings) || value.warnings.length > 20 ||
      !Array.isArray(value.omitted_sections) || value.omitted_sections.length > 30) return null;

  const countKeys = ['facts', 'answers', 'uploaded_files', 'generated_documents', 'jobs', 'applications', 'search_profiles', 'company_discovery_history'] as const;
  const counts = {} as ProfileRestorePreview['counts'];
  for (const key of countKeys) {
    const count = value.counts[key];
    if (!Number.isSafeInteger(count) || (count as number) < 0) return null;
    counts[key] = count as number;
  }

  const warnings = value.warnings;
  if (!warnings.every(code => typeof code === 'string' && Object.hasOwn(WARNING_LABELS, code))) return null;
  const omittedSections = value.omitted_sections;
  if (!omittedSections.every(id => typeof id === 'string' && Object.hasOwn(OMITTED_SECTION_LABELS, id))) return null;

  return {
    fingerprint: value.fingerprint,
    sourceProfile: { name: value.source_profile.name, email: value.source_profile.email },
    counts,
    warnings: [...new Set(warnings as string[])],
    omittedSections: [...new Set(omittedSections as string[])],
  };
}

/** POSTs an untrusted backup and projects the response to fixed safe summary fields. */
export async function requestRestorePreview(
  request: RestoreApi,
  backup: RestoreBackup,
): Promise<ProfileRestorePreview | null> {
  try {
    const response = await request('/profile-restores/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backup }),
    });
    return projectPreview(response);
  } catch {
    return null;
  }
}

/** Reuses the caller's request ID on retries; only the fixed profile ID leaves this helper. */
export async function submitProfileRestore(
  request: RestoreApi,
  backup: RestoreBackup,
  requestId: string,
  expectedFingerprint: string,
  confirmed: boolean,
): Promise<RestoreRequestResult> {
  if (!confirmed) return { ok: false, reason: 'confirmation_required' };
  try {
    const response = await request('/profile-restores', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ backup, request_id: requestId, expected_fingerprint: expectedFingerprint, confirmed: true }),
    });
    if (!isRecord(response) || !isBoundedString(response.profile_id, 128) || response.request_id !== requestId) {
      return { ok: false, reason: 'invalid_response' };
    }
    return { ok: true, profileId: response.profile_id };
  } catch {
    return { ok: false, reason: 'invalid_response' };
  }
}

export type ProfileRestorePreviewCardProps = {
  preview: ProfileRestorePreview;
  confirmed: boolean;
  onConfirmedChange: (confirmed: boolean) => void;
  onRestore: () => void;
  restoring: boolean;
};

export type RecoveredHistoryRecord = {
  sourceIdentity: string;
  state: string;
  reasons: string[];
  referenceStatus: string;
  metadataStatus: string;
  createdAt: string;
};

export async function loadRecoveredHistory(request: RestoreApi, profileId: string): Promise<RecoveredHistoryRecord[] | null> {
  try {
    const response = await request(`/profile-restores/${encodeURIComponent(profileId)}/history`);
    if (!isRecord(response) || response.profile_id !== profileId || !Array.isArray(response.history) ||
        response.history.length > 10000 || response.mode !== 'READ_ONLY_HISTORICAL_ARCHIVE' ||
        response.authorization_required_for_future_live_action !== true) return null;
    const valid = response.history.every(value => {
      if (!isRecord(value) || !isRecord(value.payload)) return false;
      return value.kind === 'company_discovery_request' && value.schema_version === 1 &&
        isBoundedString(value.source_identity, 128) && typeof value.payload.state === 'string' &&
        Array.isArray(value.payload.reasons) &&
        value.payload.reasons.every(reason => typeof reason === 'string' && reason.length <= 500) &&
        typeof value.payload.reference_status === 'string' &&
        ['COMPLETE', 'PARTIAL'].includes(String(value.payload.metadata_status)) &&
        typeof value.created_at === 'string';
    });
    if (!valid) return null;
    return response.history.map(value => {
      if (!isRecord(value) || !isRecord(value.payload)) throw new Error('invalid recovered history response');
      return { sourceIdentity: value.source_identity as string, state: value.payload.state as string,
        reasons: value.payload.reasons as string[], referenceStatus: value.payload.reference_status as string,
        metadataStatus: value.payload.metadata_status as string,
        createdAt: value.created_at as string };
    });
  } catch {
    return null;
  }
}

export function ProfileRestorePreviewCard({ preview, confirmed, onConfirmedChange, onRestore, restoring }: ProfileRestorePreviewCardProps) {
  const summary = (Object.keys(COUNT_LABELS) as (keyof ProfileRestorePreview['counts'])[])
    .filter(key => preview.counts[key] > 0)
    .map(key => `${preview.counts[key]} ${COUNT_LABELS[key]}${preview.counts[key] === 1 ? '' : 's'}`);
  const warnings = preview.warnings.map(code => WARNING_LABELS[code]);
  const omissions = preview.omittedSections.map(id => OMITTED_SECTION_LABELS[id]);

  return <div aria-live="polite" aria-label="Profile restore preview">
    <h3>Backup preview</h3>
    <p>Source profile: {preview.sourceProfile.name} ({preview.sourceProfile.email})</p>
    <p>{summary.length ? summary.join(', ') : 'No supported profile items'} found.</p>
    <p>Imported facts and answers will require review and verification. This restore creates a new profile and does not overwrite an existing profile.</p>
    {warnings.length > 0 && <section aria-label="Restore warnings">
      <h4>Warnings</h4>
      <ul>{warnings.map((warning, index) => <li key={`${preview.warnings[index]}-${index}`}>{warning}</li>)}</ul>
    </section>}
    <section aria-label="Sections not restored">
      <h4>Not restored</h4>
      {omissions.length ? <ul>{omissions.map((label, index) => <li key={`${preview.omittedSections[index]}-${index}`}>{label}</li>)}</ul> : <p>No additional sections are omitted.</p>}
    </section>
    <label>
      <input type="checkbox" checked={confirmed} disabled={restoring} onChange={event => onConfirmedChange(event.currentTarget.checked)} />
      I understand this creates a new profile and imported information must be reviewed.
    </label>
    <button type="button" disabled={!confirmed || restoring} onClick={onRestore}>
      {restoring ? 'Restoring profile…' : 'Restore into a new profile'}
    </button>
  </div>;
}

export type ProfileRestoreProps = {
  api?: RestoreApi;
  profileId?: string;
  onRestored?: (profileId: string) => void;
};

function safeError(reason: string): string {
  switch (reason) {
    case 'file_too_large': return 'This backup exceeds the 5 MiB limit.';
    case 'invalid_json': return 'This file is not valid readable JSON.';
    case 'unsupported_format': return 'This file is not a supported profile backup.';
    default: return 'The backup could not be processed. No profile was changed. Try again or select the file again.';
  }
}

const restoreApi: RestoreApi = (path, init) => defaultApi<unknown>(path, init);

/** A keyed instance owns one profile's reads, including retries and late responses. */
function RecoveredHistory({ api, profileId }: { api: RestoreApi; profileId: string }) {
  const [history, setHistory] = useState<RecoveredHistoryRecord[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true;
    setLoading(true);
    setHistory(null);
    void loadRecoveredHistory(api, profileId).then(result => {
      if (!active) return;
      setHistory(result);
      setLoading(false);
    });
    return () => { active = false; };
  }, [api, profileId, retry]);

  return <section aria-label="Recovered company discovery history" aria-busy={loading}>
    <h3>Recovered company discovery history</h3>
    <p>These records are read-only historical snapshots. Restore does not authorize or schedule new discovery; fresh authorization is required for any future live action.</p>
    {loading ? <p role="status">Loading recovered history…</p> : history === null ? <>
      <p role="alert">Recovered history could not be loaded. Try again.</p>
      <button type="button" onClick={() => { setLoading(true); setRetry(value => value + 1); }}>Retry recovered history</button>
    </> : history.length === 0 ? <p>No recovered company discovery history for this profile.</p> :
      <ul>{history.map(item => <li key={item.sourceIdentity}>
        <strong>{item.state}</strong> — {item.reasons.join('; ') || 'No reason recorded'}
        <span> ({item.referenceStatus}; {item.metadataStatus.toLowerCase()} metadata; {item.createdAt || 'time unavailable'})</span>
      </li>)}</ul>}
  </section>;
}

export function ProfileRestore({ api = restoreApi, profileId, onRestored }: ProfileRestoreProps) {
  const input = useRef<HTMLInputElement>(null);
  const [backup, setBackup] = useState<RestoreBackup | null>(null);
  const [preview, setPreview] = useState<ProfileRestorePreview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [reading, setReading] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [requestId, setRequestId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [selected, setSelected] = useState(false);
  const busy = reading || previewing || restoring;

  const clear = () => {
    setBackup(null);
    setPreview(null);
    setConfirmed(false);
    setRequestId(null);
    setError(null);
    setSuccess(null);
    setSelected(false);
    if (input.current) input.current.value = '';
  };

  const chooseFile = async (file?: File) => {
    clear();
    if (!file) return;
    setSelected(true);
    setReading(true);
    try {
      const result = await parseRestoreBackup(file);
      if (!result.ok) setError(safeError(result.reason));
      else setBackup(result.backup);
    } catch {
      setError(safeError('invalid_json'));
    } finally {
      setReading(false);
      if (input.current) input.current.value = '';
    }
  };

  const getPreview = async () => {
    if (!backup || busy) return;
    setPreviewing(true);
    setPreview(null);
    setConfirmed(false);
    setError(null);
    setSuccess(null);
    try {
      const result = await requestRestorePreview(api, backup);
      if (!result) setError(safeError('api'));
      else setPreview(result);
    } finally {
      setPreviewing(false);
    }
  };

  const restore = async () => {
    if (!backup || !preview || !confirmed || busy) return;
    setRestoring(true);
    setError(null);
    setSuccess(null);
    try {
      const stableRequestId = requestId || crypto.randomUUID();
      setRequestId(stableRequestId);
      const result = await submitProfileRestore(api, backup, stableRequestId, preview.fingerprint, confirmed);
      if (!result.ok) {
        setError(safeError('api'));
        return;
      }
      setSuccess(`Profile ${result.profileId} was restored. Review imported facts, answers, and saved search profiles before using them.`);
      setBackup(null);
      setPreview(null);
      setConfirmed(false);
      setSelected(false);
      if (input.current) input.current.value = '';
      try {
        onRestored?.(result.profileId);
      } catch {
        setError('The profile was restored, but the profile view could not be refreshed. Reload and select the new profile.');
      }
    } catch {
      setError(safeError('api'));
    } finally {
      setRestoring(false);
    }
  };

  return <section className="panel" aria-labelledby="profile-restore-title" aria-busy={busy}>
    <h2 id="profile-restore-title">Restore a profile backup</h2>
    <p>This creates a new profile. Existing profiles are not overwritten, and application, automation, and submission state is not restored.</p>
    <p>Imported facts and answers will require review and verification.</p>
    <label htmlFor="profile-restore-file">Choose a JSON backup file (maximum 5 MiB)</label>
    <input ref={input} id="profile-restore-file" type="file" accept="application/json,.json" disabled={busy} onChange={event => void chooseFile(event.currentTarget.files?.[0])} />
    {reading && <p role="status">Reading backup…</p>}
    {selected && backup && <p role="status">Backup selected. File contents are not displayed.</p>}
    {selected && <button type="button" disabled={busy} onClick={clear}>Clear selected backup</button>}
    {backup && <button type="button" disabled={busy} onClick={() => void getPreview()}>
      {previewing ? 'Preparing preview…' : preview ? 'Refresh preview' : 'Preview backup'}
    </button>}
    {preview && <ProfileRestorePreviewCard preview={preview} confirmed={confirmed} onConfirmedChange={setConfirmed} onRestore={() => void restore()} restoring={restoring} />}
    {error && <p role="alert">{error}</p>}
    {success && <p role="status">{success}</p>}
    {profileId && <RecoveredHistory key={profileId} api={api} profileId={profileId} />}
  </section>;
}
