import React, { useEffect, useRef, useState } from 'react';

export const COMPANY_PREFERENCE_STATUSES = ['BLACKLISTED', 'PREFERRED', 'NEUTRAL'] as const;
export type CompanyPreferenceStatus = typeof COMPANY_PREFERENCE_STATUSES[number];

export type CompanyPreferenceReadback = {
  profile_id: string;
  company_id: string;
  status: CompanyPreferenceStatus;
  updated_at: string | null;
  discovery?: { request_id: string | null; status: string; reason: string | null };
};

export type CompanyPreferenceClient = {
  get(profileId: string, companyId: string): Promise<CompanyPreferenceReadback>;
  set(profileId: string, companyId: string, status: CompanyPreferenceStatus, jobId?: string): Promise<CompanyPreferenceReadback>;
};

export type CompanyPreferenceOperation = 'get' | 'set' | null;
export type CompanyPreferenceViewProps = {
  profileId: string;
  companyId: string;
  companyName: string;
  saved: CompanyPreferenceReadback | null;
  selectedStatus: CompanyPreferenceStatus | null;
  loading: boolean;
  saving: boolean;
  error: string | null;
  errorOperation: CompanyPreferenceOperation;
  saveConfirmed: boolean;
  onStatusChange: (status: CompanyPreferenceStatus) => void;
  onSave: () => void;
  onRetry: () => void;
};

const STATUS_LABEL: Record<CompanyPreferenceStatus, string> = {
  BLACKLISTED: 'Avoid this company',
  PREFERRED: 'Prefer this company',
  NEUTRAL: 'No preference',
};

function scopeKey(profileId: string, companyId: string): string {
  return `${profileId}\u0000${companyId}`;
}

function validateReadback(
  readback: CompanyPreferenceReadback,
  profileId: string,
  companyId: string,
  expectedStatus?: CompanyPreferenceStatus,
): CompanyPreferenceReadback {
  if (!readback || readback.profile_id !== profileId || readback.company_id !== companyId ||
      !COMPANY_PREFERENCE_STATUSES.includes(readback.status) ||
      (expectedStatus && readback.status !== expectedStatus) ||
      (readback.updated_at !== null &&
        (typeof readback.updated_at !== 'string' || Number.isNaN(Date.parse(readback.updated_at))))) {
    throw new Error('The company preference response did not confirm the selected profile, company, status, and update time.');
  }
  return readback;
}

export async function confirmCompanyPreference(
  client: CompanyPreferenceClient,
  profileId: string,
  companyId: string,
  status: CompanyPreferenceStatus,
  jobId?: string,
): Promise<CompanyPreferenceReadback> {
  return validateReadback(await (jobId ? client.set(profileId, companyId, status, jobId)
    : client.set(profileId, companyId, status)), profileId, companyId, status);
}

export function CompanyPreferenceView({
  profileId, companyId, companyName, saved, selectedStatus, loading, saving, error,
  errorOperation, saveConfirmed, onStatusChange, onSave, onRetry,
}: CompanyPreferenceViewProps) {
  const busy = loading || saving;
  const dirty = Boolean(saved && selectedStatus && saved.status !== selectedStatus);
  return <section className="panel" aria-label="Company preference">
    <div className="toolbar">
      <div>
        <h2>Company preference</h2>
        <p>{companyName} · Company ID <code>{companyId}</code></p>
      </div>
      {saved && <span className="tag">Saved: {saved.status}</span>}
    </div>
    {!profileId || !companyId ? <p role="alert">Select a profile and company to view company preference.</p> : null}
    {loading && <p role="status">Loading saved company preference…</p>}
    {error && <p role="alert">{errorOperation === 'set'
      ? `Could not confirm the saved preference: ${error}. Showing the last confirmed preference.`
      : `Could not load company preference: ${error}. ${saved ? 'Last confirmed preference remains shown.' : ''}`}</p>}
    {saved && !error && !loading && <p>Saved preference: <strong>{saved.status}</strong> — {STATUS_LABEL[saved.status]}.</p>}
    {saved && error && <p>Last confirmed preference: <strong>{saved.status}</strong> — {STATUS_LABEL[saved.status]}.</p>}
    {saved?.updated_at && <p>Last confirmed at: <time dateTime={saved.updated_at}>{saved.updated_at}</time></p>}
    {saved && !saved.updated_at && <p>No explicit choice has been saved; the current default is neutral.</p>}
    <form onSubmit={event => { event.preventDefault(); onSave(); }}>
      <fieldset disabled={busy || !saved}>
        <legend>Change preference for {companyName}</legend>
        {COMPANY_PREFERENCE_STATUSES.map(status => <label key={status}>
          <input type="radio" name={`company-preference-${companyId}`} value={status}
            checked={selectedStatus === status} onChange={() => onStatusChange(status)} />
          {STATUS_LABEL[status]} <span className="muted">({status})</span>
        </label>)}
      </fieldset>
      {selectedStatus && saved && selectedStatus !== saved.status && <p role="status">Unsaved selection: {selectedStatus} — {STATUS_LABEL[selectedStatus]}.</p>}
      <div className="company-source-controls">
        <button type="submit" disabled={busy || !saved || !dirty}>
          {saving && errorOperation === 'set' ? 'Retrying save…' : errorOperation === 'set' && error ? 'Retry save preference' : 'Save company preference'}
        </button>
        {errorOperation === 'get' && <button type="button" disabled={busy} onClick={onRetry}>Retry company preference</button>}
      </div>
    </form>
    {saveConfirmed && <p role="status">Preference saved and read back from the selected company.</p>}
    {saved?.discovery && <p role="status">Employer discovery: <strong>{saved.discovery.status}</strong>.
      {saved.discovery.status === 'NEEDS_ORIGIN_REVIEW' ? ' Review the employer website in Sources before discovery can run.'
        : saved.discovery.status === 'NOT_QUEUED' ? ' Discovery was not queued because workspace policy or source provenance is not eligible.'
          : ' Open Sources to inspect the durable discovery request and its current outcome.'}</p>}
    <p className="safety">Blacklisted companies are omitted from job search unless “Show blacklisted companies” is enabled. Preferred companies affect application-priority sorting and request employer discovery when policy and posting provenance permit. Unverified employer websites require review in Sources. This never submits or rejects an application.</p>
    <p className="muted">Profile ID: <code>{profileId}</code></p>
  </section>;
}

type CompanyPreferenceState = {
  scope: string;
  saved: CompanyPreferenceReadback | null;
  loading: boolean;
  saving: boolean;
  error: string | null;
  errorOperation: CompanyPreferenceOperation;
  saveConfirmed: boolean;
};

export type CompanyPreferencesProps = {
  profileId: string;
  companyId: string;
  companyName: string;
  jobId?: string;
  client: CompanyPreferenceClient;
  onPreferenceSaved?: () => void;
};

export function CompanyPreferences({ profileId, companyId, companyName, jobId, client, onPreferenceSaved }: CompanyPreferencesProps) {
  const scope = `${scopeKey(profileId, companyId)}\u0000${jobId || ''}`;
  const requestSequence = useRef(0);
  const [state, setState] = useState<CompanyPreferenceState>(() => ({
    scope, saved: null, loading: Boolean(profileId && companyId), saving: false,
    error: null, errorOperation: null, saveConfirmed: false,
  }));
  const [draft, setDraft] = useState<{ scope: string; status: CompanyPreferenceStatus } | null>(null);
  const scopedState = state.scope === scope ? state : null;
  const saved = scopedState?.saved || null;
  const selectedStatus = draft?.scope === scope ? draft.status : saved?.status || null;

  async function loadPreference() {
    if (!profileId || !companyId) return;
    const requestId = ++requestSequence.current;
    setState(previous => ({
      scope,
      saved: previous.scope === scope ? previous.saved : null,
      loading: true, saving: false, error: null, errorOperation: null, saveConfirmed: false,
    }));
    try {
      const readback = validateReadback(await client.get(profileId, companyId), profileId, companyId);
      if (requestId === requestSequence.current) {
        setState({ scope, saved: readback, loading: false, saving: false, error: null, errorOperation: null, saveConfirmed: false });
        setDraft({ scope, status: readback.status });
      }
    } catch (reason) {
      if (requestId === requestSequence.current) setState(previous => ({
        scope,
        saved: previous.scope === scope ? previous.saved : null,
        loading: false, saving: false,
        error: reason instanceof Error ? reason.message : 'Company preference could not be read.',
        errorOperation: 'get', saveConfirmed: false,
      }));
    }
  }

  useEffect(() => {
    void loadPreference();
    return () => { requestSequence.current += 1; };
  }, [client, profileId, companyId, jobId]);

  async function savePreference() {
    if (!saved || !selectedStatus || selectedStatus === saved.status || !profileId || !companyId) return;
    const requestedStatus = selectedStatus;
    const requestId = ++requestSequence.current;
    setState(previous => ({
      scope,
      saved: previous.scope === scope ? previous.saved : null,
      loading: false, saving: true, error: null, errorOperation: null, saveConfirmed: false,
    }));
    try {
      const readback = await confirmCompanyPreference(client, profileId, companyId, requestedStatus, jobId);
      if (requestId === requestSequence.current) {
        setState({ scope, saved: readback, loading: false, saving: false, error: null, errorOperation: null, saveConfirmed: true });
        setDraft({ scope, status: readback.status });
        onPreferenceSaved?.();
      }
    } catch (reason) {
      if (requestId === requestSequence.current) setState(previous => ({
        scope,
        saved: previous.scope === scope ? previous.saved : null,
        loading: false, saving: false,
        error: reason instanceof Error ? reason.message : 'Company preference could not be confirmed.',
        errorOperation: 'set', saveConfirmed: false,
      }));
    }
  }

  const viewState = scopedState;
  return <CompanyPreferenceView
    profileId={profileId}
    companyId={companyId}
    companyName={companyName}
    saved={saved}
    selectedStatus={selectedStatus}
    loading={viewState ? viewState.loading : Boolean(profileId && companyId)}
    saving={viewState?.saving || false}
    error={viewState?.error || null}
    errorOperation={viewState?.errorOperation || null}
    saveConfirmed={viewState?.saveConfirmed || false}
    onStatusChange={status => {
      setDraft({ scope, status });
      setState(previous => previous.scope === scope
        ? { ...previous, error: null, errorOperation: null, saveConfirmed: false }
        : previous);
    }}
    onSave={() => { void savePreference(); }}
    onRetry={() => { void loadPreference(); }}
  />;
}
