import React, { useRef, useState } from 'react';
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
  counts: { facts: number; answers: number; uploaded_files: number; search_profiles: number };
  warnings: string[];
  omittedSections: string[];
};

export type RestoreRequestResult =
  | { ok: true; profileId: string }
  | { ok: false; reason: 'confirmation_required' | 'invalid_response' };

const COUNT_LABELS = {
  facts: 'fact', answers: 'answer', uploaded_files: 'uploaded file', search_profiles: 'saved search profile',
} as const;

const WARNING_LABELS: Record<string, string> = {
  IMPORTED_FACTS_REQUIRE_REVIEW: 'Imported facts are unverified and require review before use.',
  IMPORTED_ANSWERS_REQUIRE_REVIEW: 'Imported answers require review before reuse.',
  SEARCH_PROFILES_DISABLED: 'Imported search profiles are disabled until reviewed.',
  AUTOMATION_NOT_RESTORED: 'Automation settings are not activated by restore.',
  UPLOADED_FILES_NOT_INCLUDED: 'Original uploaded files are not included in this backup.',
};

const OMITTED_SECTION_LABELS: Record<string, string> = {
  job_history: 'Employment history and job records',
  application_history: 'Application history',
  application_routes: 'Application routes',
  application_attempts: 'Application attempts',
  ai_evaluations: 'AI evaluation history',
  source_discovery: 'Source discovery',
  company_discovery: 'Company discovery',
  generated_documents: 'Generated documents',
  resume_workspace: 'Resume workspace',
  interview_prep: 'Interview preparation',
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

  const countKeys = ['facts', 'answers', 'uploaded_files', 'search_profiles'] as const;
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

export function ProfileRestore({ api = (path, init) => defaultApi<unknown>(path, init), onRestored }: ProfileRestoreProps) {
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
  </section>;
}
