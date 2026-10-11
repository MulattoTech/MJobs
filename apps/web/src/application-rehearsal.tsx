import React, { useEffect, useRef, useState } from 'react';

export type ApplicationRehearsalField = {
  key: string;
  label: string;
  type: string;
  required: boolean;
  value: string | number | boolean | null;
  source: 'CONTACT' | 'VERIFIED_ANSWER' | 'DOCUMENT' | 'UNRESOLVED';
  reason_code: string | null;
};

export type ApplicationRehearsalReceipt = {
  request_id: string;
  fingerprint: string;
  profile_version: number;
  reviewed_at: string;
  acknowledged: true;
};

export type ApplicationRehearsalSnapshot = {
  profile_id: string;
  application_id: string;
  profile_version: number;
  route_id: string | null;
  fingerprint: string;
  fields: ApplicationRehearsalField[];
  blockers: string[];
  ready: boolean;
  submission_enabled: false;
  latest_review: ApplicationRehearsalReceipt | null;
};

export type ApplicationRehearsalReviewRequest = {
  request_id: string;
  fingerprint: string;
  acknowledged: true;
};

export type ApplicationRehearsalClientErrorCode =
  | 'INVALID_REQUEST' | 'INVALID_RESPONSE' | 'NETWORK' | 'NOT_FOUND' | 'HTTP_ERROR' | 'STALE';

export class ApplicationRehearsalClientError extends Error {
  constructor(readonly code: ApplicationRehearsalClientErrorCode) {
    super(code === 'STALE'
      ? 'This rehearsal changed while it was being reviewed. Current values must be reloaded.'
      : code === 'NOT_FOUND'
        ? 'This application is unavailable for the selected profile.'
        : 'The local rehearsal could not be loaded or recorded. Try again.');
    this.name = 'ApplicationRehearsalClientError';
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);
const isId = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const isFingerprint = (value: unknown): value is string =>
  typeof value === 'string' && /^[A-Fa-f0-9]{64}$/.test(value);
const isSource = (value: unknown): value is ApplicationRehearsalField['source'] =>
  value === 'CONTACT' || value === 'VERIFIED_ANSWER' || value === 'DOCUMENT' || value === 'UNRESOLVED';
const isFieldValue = (value: unknown): value is ApplicationRehearsalField['value'] =>
  value === null || typeof value === 'boolean'
    || typeof value === 'number' && Number.isFinite(value)
    || typeof value === 'string' && value.length <= 12_000;

function parseReceipt(value: unknown): ApplicationRehearsalReceipt | null | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !isId(value.request_id) || !isFingerprint(value.fingerprint)
      || !Number.isInteger(value.profile_version) || (value.profile_version as number) < 0
      || typeof value.reviewed_at !== 'string' || value.reviewed_at.length > 64
      || value.acknowledged !== true) return undefined;
  return {
    request_id: value.request_id,
    fingerprint: value.fingerprint,
    profile_version: value.profile_version as number,
    reviewed_at: value.reviewed_at,
    acknowledged: true,
  };
}

function parseSnapshot(value: unknown, profileId: string, applicationId: string): ApplicationRehearsalSnapshot {
  if (!isRecord(value) || value.profile_id !== profileId || value.application_id !== applicationId
      || !Number.isInteger(value.profile_version) || (value.profile_version as number) < 0
      || value.route_id !== null && typeof value.route_id !== 'string'
      || !isFingerprint(value.fingerprint) || !Array.isArray(value.fields) || value.fields.length > 200
      || !Array.isArray(value.blockers) || value.blockers.length > 100
      || !value.blockers.every((item) => typeof item === 'string' && item.length <= 128)
      || typeof value.ready !== 'boolean' || value.submission_enabled !== false) {
    throw new ApplicationRehearsalClientError('INVALID_RESPONSE');
  }

  const fields: ApplicationRehearsalField[] = [];
  let totalValueCharacters = 0;
  for (const item of value.fields) {
    if (!isRecord(item) || !isId(item.key) || typeof item.label !== 'string' || item.label.length > 500
        || typeof item.type !== 'string' || item.type.length > 80 || typeof item.required !== 'boolean'
        || !isFieldValue(item.value) || !isSource(item.source)
        || item.reason_code !== null && (typeof item.reason_code !== 'string' || item.reason_code.length > 128)
        || item.source === 'UNRESOLVED' && item.value !== null) {
      throw new ApplicationRehearsalClientError('INVALID_RESPONSE');
    }
    if (typeof item.value === 'string') totalValueCharacters += item.value.length;
    if (totalValueCharacters > 250_000) throw new ApplicationRehearsalClientError('INVALID_RESPONSE');
    fields.push({
      key: item.key,
      label: item.label,
      type: item.type,
      required: item.required,
      value: item.value,
      source: item.source,
      reason_code: item.reason_code as string | null,
    });
  }
  const latestReview = parseReceipt(value.latest_review);
  if (latestReview === undefined) throw new ApplicationRehearsalClientError('INVALID_RESPONSE');
  return {
    profile_id: profileId,
    application_id: applicationId,
    profile_version: value.profile_version as number,
    route_id: value.route_id as string | null,
    fingerprint: value.fingerprint,
    fields,
    blockers: [...new Set(value.blockers as string[])],
    ready: value.ready,
    submission_enabled: false,
    latest_review: latestReview,
  };
}

/** Creates a same-origin, read-only rehearsal client bound to one profile/application scope. */
export function createApplicationRehearsalClient(
  profileId: string,
  applicationId: string,
  fetcher: typeof fetch = globalThis.fetch,
  isScopeCurrent: () => boolean = () => true,
  signal?: AbortSignal,
) {
  const assertScope = () => {
    if (!profileId.trim() || !applicationId.trim()) throw new ApplicationRehearsalClientError('INVALID_REQUEST');
    if (signal?.aborted || !isScopeCurrent()) throw new ApplicationRehearsalClientError('STALE');
  };
  const url = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/rehearsal`;
  const request = async (path: string, init: RequestInit) => {
    assertScope();
    let response: Response;
    try {
      response = await fetcher(path, { ...init, credentials: 'same-origin', signal });
    } catch {
      assertScope();
      throw new ApplicationRehearsalClientError('NETWORK');
    }
    assertScope();
    if (response.status === 409) throw new ApplicationRehearsalClientError('STALE');
    if (response.status === 404) throw new ApplicationRehearsalClientError('NOT_FOUND');
    if (!response.ok) throw new ApplicationRehearsalClientError('HTTP_ERROR');
    return response;
  };

  return {
    async load(): Promise<ApplicationRehearsalSnapshot> {
      const response = await request(url, { method: 'GET', cache: 'no-store' });
      let body: unknown;
      try {
        body = await response.json();
      } catch {
        assertScope();
        throw new ApplicationRehearsalClientError('INVALID_RESPONSE');
      }
      assertScope();
      return parseSnapshot(body, profileId, applicationId);
    },
    async recordReview(input: ApplicationRehearsalReviewRequest): Promise<void> {
      if (!isId(input.request_id) || !isFingerprint(input.fingerprint) || input.acknowledged !== true) {
        throw new ApplicationRehearsalClientError('INVALID_REQUEST');
      }
      await request(`${url}/reviews`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
      });
    },
  };
}

const reasonMessages: Record<string, string> = {
  NO_APPROVED_ANSWER: 'No approved saved answer is available for this required field.',
  REQUIRED_FIELD_UNRESOLVED: 'This required field needs a verified value before it can be resolved.',
  SENSITIVE_FIELD: 'This field needs your review and will not be filled automatically.',
  FORM_FIELD_REVIEW_REQUIRED: 'The inspected form metadata requires manual review.',
  FORM_REQUIRES_HUMAN_REVIEW: 'This form requires manual review before rehearsal can be resolved.',
  FORM_REVIEW_REQUIRED: 'Inspect the current form metadata before reviewing this rehearsal.',
  FORM_REVIEW_STALE: 'The inspected form changed. Reload its current metadata before reviewing.',
  FORM_METADATA_INVALID: 'This field could not be safely matched to an approved value.',
  UNSUPPORTED_FIELD: 'This field is not supported for a safe local rehearsal.',
  MISSING_REQUIRED_SOURCE: 'No verified value is available for this required field.',
};
const blockerMessages: Record<string, string> = {
  FORM_REVIEW_REQUIRED: 'Inspect the current form before rehearsing values.',
  FORM_REVIEW_STALE: 'The inspected form changed and needs a fresh review.',
  FORM_REQUIRES_HUMAN_REVIEW: 'The form needs manual review.',
  REQUIRED_FIELD_UNRESOLVED: 'One or more required fields do not have a verified value.',
  FORM_FIELD_REVIEW_REQUIRED: 'One or more form fields need manual review.',
  FORM_METADATA_INVALID: 'Some form metadata could not be safely matched.',
};
const sourceLabels: Record<ApplicationRehearsalField['source'], string> = {
  CONTACT: 'Profile contact',
  VERIFIED_ANSWER: 'Approved saved answer',
  DOCUMENT: 'Owned document',
  UNRESOLVED: 'Unresolved',
};
function displayValue(field: ApplicationRehearsalField): string {
  if (field.source === 'UNRESOLVED' || field.value === null) return 'No value populated';
  if (typeof field.value === 'boolean') return field.value ? 'Yes' : 'No';
  if (field.source === 'DOCUMENT' && typeof field.value === 'string' && /[\\/]/u.test(field.value)) {
    return field.value.split(/[\\/]/u).filter(Boolean).at(-1) || 'Owned document';
  }
  return String(field.value);
}

export function ApplicationRehearsalView({
  snapshot,
  reviewChecked,
  reviewPending,
  notice,
  onReviewCheck,
  onRecordReview,
}: {
  snapshot: ApplicationRehearsalSnapshot;
  reviewChecked: boolean;
  reviewPending: boolean;
  notice: string | null;
  onReviewCheck: (checked: boolean) => void;
  onRecordReview: () => void;
}) {
  const currentReceipt = snapshot.latest_review?.fingerprint === snapshot.fingerprint;
  return (
    <section className="panel" aria-label="Local application rehearsal">
      <h2>Local application rehearsal</h2>
      <p className="muted">
        Review these prepared values on this device. MJobs will not fill or submit an employer form.
        External submission is disabled.
      </p>
      <p role="status">{snapshot.ready
        ? 'The local rehearsal is ready for your review.'
        : 'Some fields still need review before this rehearsal is resolved.'}</p>
      {snapshot.fields.length === 0
        ? <p>No inspected form fields are available for this rehearsal.</p>
        : <ul aria-label="Rehearsal field values">
          {snapshot.fields.map((field) => (
            <li key={field.key}>
              <h3>{field.label}</h3>
              <dl>
                <dt>Type</dt><dd>{field.type}</dd>
                <dt>Required</dt><dd>{field.required ? 'Yes' : 'No'}</dd>
                <dt>Source</dt><dd>{sourceLabels[field.source]}</dd>
                <dt>Value</dt><dd><output aria-label={`${field.label} rehearsal value`}>{displayValue(field)}</output></dd>
                {field.source === 'UNRESOLVED' && (
                  <><dt>Why this is unresolved</dt><dd>{reasonMessages[field.reason_code || '']
                    || 'No verified value is available; review this field manually.'}</dd></>
                )}
              </dl>
            </li>
          ))}
        </ul>}
      {snapshot.blockers.length > 0 && (
        <div aria-label="Rehearsal blockers">
          <h3>Outstanding review items</h3>
          <ul>{snapshot.blockers.map((code) => <li key={code}>
            {blockerMessages[code] || 'A review item needs your attention.'}
          </li>)}</ul>
        </div>
      )}
      {snapshot.latest_review && <p role="status">
        {currentReceipt
          ? `Review receipt recorded locally${Number.isFinite(Date.parse(snapshot.latest_review.reviewed_at))
            ? ` on ${new Date(snapshot.latest_review.reviewed_at).toLocaleString()}` : ''}. This does not submit an application.`
          : 'A previous review receipt is saved for an earlier rehearsal snapshot. Review the current values again.'}
      </p>}
      <label className="consent">
        <input type="checkbox" checked={reviewChecked} disabled={reviewPending}
          onChange={(event) => onReviewCheck(event.currentTarget.checked)} />
        I have reviewed these rehearsal values locally
      </label>
      <button type="button" className="primary" disabled={!reviewChecked || reviewPending}
        onClick={onRecordReview}>
        {reviewPending ? 'Recording local review…' : 'Record local review receipt'}
      </button>
      {notice && <p role="status" aria-live="polite">{notice}</p>}
    </section>
  );
}

type LoadState =
  | { scopeKey: string; status: 'loading'; notice?: string }
  | { scopeKey: string; status: 'error'; message: string }
  | { scopeKey: string; status: 'ready'; snapshot: ApplicationRehearsalSnapshot; notice?: string };

function loadMessage(error: unknown): string {
  if (error instanceof ApplicationRehearsalClientError && error.code === 'NOT_FOUND') {
    return 'This application is unavailable for the selected profile.';
  }
  return 'The local rehearsal could not be loaded or recorded. Try again.';
}

export function ApplicationRehearsal({
  profileId,
  applicationId,
  fetcher,
  requestIdFactory = () => globalThis.crypto.randomUUID(),
}: {
  profileId: string | null;
  applicationId: string | null;
  fetcher?: typeof fetch;
  requestIdFactory?: () => string;
}) {
  const scopeKey = JSON.stringify([profileId, applicationId]);
  const selectedScope = useRef(scopeKey);
  selectedScope.current = scopeKey;
  const [load, setLoad] = useState<LoadState | null>(null);
  const [reviewCheck, setReviewCheck] = useState<{ scopeKey: string; fingerprint: string; checked: boolean } | null>(null);
  const [reviewPending, setReviewPending] = useState(false);
  const requestRef = useRef<{ scopeKey: string; fingerprint: string; requestId: string } | null>(null);
  const controllerRef = useRef<AbortController | null>(null);

  const reload = async (activeScope: string, client: ReturnType<typeof createApplicationRehearsalClient>, notice?: string) => {
    if (selectedScope.current !== activeScope) return;
    setLoad({ scopeKey: activeScope, status: 'loading', notice });
    try {
      const snapshot = await client.load();
      if (selectedScope.current === activeScope) {
        setLoad({ scopeKey: activeScope, status: 'ready', snapshot, notice });
      }
    } catch (error) {
      if (selectedScope.current === activeScope) setLoad({ scopeKey: activeScope, status: 'error', message: loadMessage(error) });
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    requestRef.current = null;
    setReviewCheck(null);
    setReviewPending(false);
    setLoad({ scopeKey, status: 'loading' });
    if (!profileId || !applicationId) {
      setLoad({ scopeKey, status: 'error', message: 'Select a profile and application to create a local rehearsal.' });
      return () => controller.abort();
    }
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher,
      () => selectedScope.current === scopeKey, controller.signal);
    void reload(scopeKey, client);
    return () => {
      controller.abort();
      if (controllerRef.current === controller) controllerRef.current = null;
    };
  }, [scopeKey, profileId, applicationId, fetcher]);

  const current = load?.scopeKey === scopeKey ? load : null;
  if (!current || current.status === 'loading') {
    return <section className="panel" aria-label="Local application rehearsal">
      <h2>Local application rehearsal</h2>
      <p role="status" aria-live="polite">{current?.status === 'loading' && current.notice
        ? current.notice : 'Loading local rehearsal…'}</p>
    </section>;
  }
  if (current.status === 'error') return <section className="panel" aria-label="Local application rehearsal">
    <h2>Local application rehearsal</h2>
    <p role="alert" aria-live="assertive">{current.message}</p>
  </section>;

  const snapshot = current.snapshot;
  const checked = reviewCheck?.scopeKey === scopeKey && reviewCheck.fingerprint === snapshot.fingerprint
    && reviewCheck.checked;
  const setChecked = (value: boolean) => setReviewCheck({ scopeKey, fingerprint: snapshot.fingerprint, checked: value });
  const recordReview = async () => {
    if (!checked || reviewPending || selectedScope.current !== scopeKey) return;
    let requestId = requestRef.current?.scopeKey === scopeKey
        && requestRef.current.fingerprint === snapshot.fingerprint
      ? requestRef.current.requestId : '';
    if (!requestId) {
      requestId = requestIdFactory();
      requestRef.current = { scopeKey, fingerprint: snapshot.fingerprint, requestId };
    }
    const controller = controllerRef.current;
    if (!controller || controller.signal.aborted) return;
    const client = createApplicationRehearsalClient(profileId!, applicationId!, fetcher,
      () => selectedScope.current === scopeKey, controller.signal);
    setReviewPending(true);
    try {
      await client.recordReview({ request_id: requestId, fingerprint: snapshot.fingerprint, acknowledged: true });
      if (selectedScope.current !== scopeKey) return;
      requestRef.current = null;
      setReviewCheck({ scopeKey, fingerprint: snapshot.fingerprint, checked: false });
      await reload(scopeKey, client, 'Review receipt saved. Reloading the latest local rehearsal.');
    } catch (error) {
      if (selectedScope.current !== scopeKey) return;
      if (error instanceof ApplicationRehearsalClientError && error.code === 'STALE') {
        requestRef.current = null;
        setReviewCheck({ scopeKey, fingerprint: snapshot.fingerprint, checked: false });
        await reload(scopeKey, client, 'The rehearsal changed during review. Current values are being reloaded. Review them again before recording a receipt.');
      } else {
        setLoad({ scopeKey, status: 'ready', snapshot,
          notice: 'The review receipt could not be recorded. Your values remain on this device; retry after checking the current rehearsal.' });
      }
    } finally {
      if (selectedScope.current === scopeKey) setReviewPending(false);
    }
  };

  return <ApplicationRehearsalView snapshot={snapshot} reviewChecked={checked}
    reviewPending={reviewPending} notice={current.notice || null}
    onReviewCheck={setChecked} onRecordReview={() => { void recordReview(); }} />;
}
