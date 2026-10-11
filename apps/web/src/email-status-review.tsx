import React, { useEffect, useRef, useState } from 'react';

export const EMAIL_STATUS_MAX_BYTES = 256 * 1024;

export type EmailStatusValue = 'APPLIED' | 'REJECTED' | 'INTERVIEW' | 'RECRUITER_CONTACT' | 'OFFER';
export type EmailStatusFormat = 'text' | 'eml';

export type EmailStatusPreview = {
  preview_id: string;
  content_sha256: string;
  category: string;
  suggested_status: EmailStatusValue | null;
  reason_codes: string[];
  expected_tracking_sequence: number;
  attribution: string;
};

export type EmailStatusHistoryItem = {
  preview_id: string;
  content_sha256: string;
  category: string;
  suggested_status: EmailStatusValue | null;
  reason_codes: string[];
  decision: string;
  reported_status: EmailStatusValue | null;
  attribution: string;
  sequence: number;
  created_at: string;
};

export type EmailStatusHistory = { items: EmailStatusHistoryItem[] };

type EmailStatusApplyResponse = {
  preview_id: string;
  applied: boolean;
  idempotent: boolean;
  status: EmailStatusValue;
  attribution: string;
  sequence: number;
};

type EmailStatusReviewState = {
  scopeKey: string;
  draft: string;
  format: EmailStatusFormat;
  fileSelected: boolean;
  preview: EmailStatusPreview | null;
  history: EmailStatusHistory | null;
  loading: boolean;
  busy: boolean;
  confirmed: boolean;
  stale: boolean;
  error: string;
  success: string;
};

const STATUS_LABELS: Record<EmailStatusValue, string> = {
  APPLIED: 'Application confirmed',
  REJECTED: 'Rejected',
  INTERVIEW: 'Interview',
  RECRUITER_CONTACT: 'Recruiter outreach',
  OFFER: 'Offer',
};

const CATEGORY_LABELS: Record<string, string> = {
  AMBIGUOUS: 'Ambiguous message',
  CONFIRMATION: 'Application confirmation',
  INTERVIEW_INVITATION: 'Interview invitation',
  OFFER: 'Offer',
  REJECTION: 'Rejection',
  RECRUITER_OUTREACH: 'Recruiter outreach',
  UNKNOWN: 'No clear status signal',
};

const REASON_LABELS: Record<string, string> = {
  CONDITIONAL_SIGNAL: 'The status depends on a future condition.',
  CONFLICTING_SIGNALS: 'The message contains conflicting status signals.',
  EXPLICIT_CONFIRMATION: 'The message explicitly confirms an application.',
  EXPLICIT_INTERVIEW_INVITATION: 'The message explicitly invites an interview.',
  EXPLICIT_OFFER: 'The message explicitly presents an offer.',
  EXPLICIT_RECRUITER_OUTREACH: 'The message explicitly indicates recruiter outreach.',
  EXPLICIT_REJECTION: 'The message explicitly communicates a rejection.',
  NEGATED_SIGNAL: 'The message negates a status signal.',
  NO_EXPLICIT_SIGNAL: 'The message does not contain an explicit status signal.',
  INVALID_FORMAT: 'The message format could not be reviewed.',
};

const STATUS_FOR_CATEGORY: Record<string, EmailStatusValue> = {
  CONFIRMATION: 'APPLIED',
  INTERVIEW_INVITATION: 'INTERVIEW',
  OFFER: 'OFFER',
  REJECTION: 'REJECTED',
  RECRUITER_OUTREACH: 'RECRUITER_CONTACT',
};

function initialState(scopeKey: string, loading = false): EmailStatusReviewState {
  return { scopeKey, draft: '', format: 'text', fileSelected: false, preview: null, history: null,
    loading, busy: false, confirmed: false, stale: false, error: '', success: '' };
}

export function emailStatusStateForScope(state: EmailStatusReviewState, scopeKey: string): EmailStatusReviewState {
  return state.scopeKey === scopeKey ? state : initialState(scopeKey, true);
}

export function isReviewableEmailStatusPreview(value: EmailStatusPreview | null | undefined): value is EmailStatusPreview & { suggested_status: EmailStatusValue } {
  if (!value || value.attribution !== 'USER_REPORTED_EMAIL') return false;
  const expected = STATUS_FOR_CATEGORY[value.category];
  return !!expected && value.suggested_status === expected && Array.isArray(value.reason_codes)
    && Number.isSafeInteger(value.expected_tracking_sequence) && value.expected_tracking_sequence >= 0
    && typeof value.preview_id === 'string' && value.preview_id.length > 0;
}

export function isEmailStatusStaleConflict(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const value = error as { status?: unknown; statusCode?: unknown; code?: unknown; detail?: unknown };
  const detailCode = value.detail && typeof value.detail === 'object'
    ? (value.detail as { code?: unknown }).code
    : undefined;
  return (value.status === 409 || value.statusCode === 409)
    && (value.code === 'EMAIL_STATUS_STALE_PREVIEW' || detailCode === 'EMAIL_STATUS_STALE_PREVIEW');
}

function formatStatus(value: unknown): string {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(STATUS_LABELS, value)
    ? STATUS_LABELS[value as EmailStatusValue] : 'Status not recorded';
}

function formatCategory(value: unknown): string {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(CATEGORY_LABELS, value)
    ? CATEGORY_LABELS[value] : 'Reviewed email';
}

function formatReasons(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  return values.filter((item): item is string => typeof item === 'string'
    && Object.prototype.hasOwnProperty.call(REASON_LABELS, item))
    .map(item => REASON_LABELS[item]);
}

function dateLabel(value: unknown): { iso: string; visible: string } | null {
  if (typeof value !== 'string') return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return null;
  return { iso: date.toISOString(), visible: date.toLocaleString() };
}

function errorDetails(error: unknown): { status?: number; code?: string } {
  if (!error || typeof error !== 'object') return {};
  const value = error as { status?: unknown; statusCode?: unknown; code?: unknown; detail?: unknown };
  const detailCode = value.detail && typeof value.detail === 'object'
    ? (value.detail as { code?: unknown }).code
    : undefined;
  const status = typeof value.status === 'number' ? value.status
    : typeof value.statusCode === 'number' ? value.statusCode : undefined;
  const code = typeof value.code === 'string' ? value.code : typeof detailCode === 'string' ? detailCode : undefined;
  return { status, code };
}

function requestId(): string {
  return typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID()
    : `email-status-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

async function readJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  });
  let body: unknown = null;
  try { body = await response.json(); } catch { /* Keep fixed safe errors below. */ }
  if (!response.ok) {
    const candidate = body && typeof body === 'object' ? body as { code?: unknown; detail?: unknown } : {};
    const nestedCode = candidate.detail && typeof candidate.detail === 'object'
      ? (candidate.detail as { code?: unknown }).code : undefined;
    const code = typeof candidate.code === 'string' ? candidate.code : nestedCode;
    const error = new Error('Email status request failed.') as Error & { status: number; code?: string };
    error.status = response.status;
    if (typeof code === 'string') error.code = code;
    throw error;
  }
  return body as T;
}

function path(profileId: string, applicationId: string, suffix = ''): string {
  return `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/email-status${suffix}`;
}

export function isEmailStatusMessageWithinLimit(text: string): boolean {
  return new TextEncoder().encode(text).byteLength <= EMAIL_STATUS_MAX_BYTES;
}

function currentDraftSize(text: string): number {
  return new TextEncoder().encode(text).byteLength;
}

export function requestIdForEmailStatusRetry(
  existing: { scopeKey: string; previewId: string; requestId: string } | null,
  scopeKey: string,
  previewId: string,
  create: () => string = requestId,
): { scopeKey: string; previewId: string; requestId: string } {
  return existing?.scopeKey === scopeKey && existing.previewId === previewId
    ? existing
    : { scopeKey, previewId, requestId: create() };
}

function emailStatusErrorText(code: string, stale: boolean): string {
  if (stale) return 'Application status changed since this preview. Refresh history and review the email again.';
  switch (code) {
    case 'format': return 'Choose an .eml or text file to import.';
    case 'size': return 'Message is larger than the 256 KiB review limit.';
    case 'read': return 'The selected message could not be read. Choose it again or paste its text.';
    case 'history': return 'Reported status history could not be loaded. Refresh this view to try again.';
    case 'not-applicable': return 'This message does not have a safe status that can be reported.';
    case 'request-conflict': return 'This confirmation conflicts with a prior request. Refresh history and review the message again.';
    case 'apply': return 'Status update could not be confirmed. Retry to safely check the same reviewed request.';
    default: return 'Email status could not be processed. Check the message and try again.';
  }
}

export type EmailStatusReviewContentProps = {
  draft: string;
  format: EmailStatusFormat;
  fileSelected: boolean;
  preview: EmailStatusPreview | null;
  history: EmailStatusHistory | null;
  loading: boolean;
  busy: boolean;
  confirmed: boolean;
  stale: boolean;
  error: string | null;
  success?: string | null;
  onDraftChange: (value: string) => void;
  onFormatChange: (value: EmailStatusFormat) => void;
  onSelectFile: (file?: File) => void;
  onClearFile: () => void;
  onPreview: () => void;
  onConfirm: () => void;
  onRefresh: () => void;
  onConfirmationChange: (value: boolean) => void;
};

export function EmailStatusReviewContent({ draft, format, fileSelected, preview, history, loading, busy, confirmed,
  stale, error, success, onDraftChange, onFormatChange, onSelectFile, onClearFile, onPreview, onConfirm,
  onRefresh, onConfirmationChange }: EmailStatusReviewContentProps) {
  const safePreview = isReviewableEmailStatusPreview(preview) ? preview : null;
  const previewStatus = safePreview?.suggested_status;
  const reasons = formatReasons(preview?.reason_codes);
  const canPreview = draft.trim().length > 0 && currentDraftSize(draft) <= EMAIL_STATUS_MAX_BYTES && !busy;

  return <section className="panel" aria-labelledby="email-status-review-title">
    <h2 id="email-status-review-title">Review email status</h2>
    <p>Import a message you received for this application. No mailbox is connected, and MJobs does not verify delivery or receipt.</p>
    <p>Email content is analyzed as text; links and attachments are never opened.</p>

    <label htmlFor="email-status-text">Paste email text</label>
    <textarea id="email-status-text" value={draft} maxLength={EMAIL_STATUS_MAX_BYTES} disabled={busy}
      onChange={event => { onFormatChange('text'); onDraftChange(event.currentTarget.value); }} />
    <p>Maximum {EMAIL_STATUS_MAX_BYTES / 1024} KiB. Message content is not added to history.</p>
    <label htmlFor="email-status-file">Choose a .eml or text file</label>
    <input id="email-status-file" type="file" accept=".eml,.txt,message/rfc822,text/plain" disabled={busy}
      onChange={event => { onSelectFile(event.currentTarget.files?.[0]); event.currentTarget.value = ''; }} />
    {fileSelected && <p role="status">Message file selected for review.</p>}
    {(!!draft || fileSelected) && <button type="button" disabled={busy} onClick={onClearFile}>{fileSelected ? 'Clear imported file' : 'Clear message'}</button>}
    {draft && currentDraftSize(draft) > EMAIL_STATUS_MAX_BYTES && <p role="alert">Message is larger than the 256 KiB review limit.</p>}
    <button type="button" disabled={!canPreview} onClick={onPreview}>{busy ? 'Working…' : 'Preview message'}</button>

    {error && <p role="alert">{emailStatusErrorText(error, stale)}</p>}
    {success && <p role="status">Reported status saved. Application submission and employer receipt are unchanged.</p>}

    {preview && <section aria-label="Email status preview">
      <h3>Message review</h3>
      <p>Message category: {formatCategory(preview.category)}</p>
      {safePreview && previewStatus
        ? <p><strong>Suggested status: {formatStatus(previewStatus)}</strong></p>
        : <p>No status can be safely suggested from this message.</p>}
      {!!reasons.length && <ul aria-label="Review reasons">{reasons.map((reason, index) => <li key={`${index}:${reason}`}>{reason}</li>)}</ul>}
      {safePreview && previewStatus && !stale && <>
        <p>This is a status you report from the email, not a verified employer receipt.</p>
        <label>
          <input type="checkbox" checked={confirmed} disabled={busy} onChange={event => onConfirmationChange(event.currentTarget.checked)} />
          I reviewed this email and want to record the suggested status.
        </label>
        <button type="button" disabled={!confirmed || busy} onClick={onConfirm}>Confirm reported status</button>
      </>}
    </section>}

    <section aria-label="Reported email status history">
      <h3>Reported status history</h3>
      <button type="button" disabled={loading || busy} onClick={onRefresh}>Refresh history</button>
      {loading && <p role="status">Loading reported status history…</p>}
      {!loading && !history && error !== 'history' && <p role="alert">Reported status history could not be loaded.</p>}
      {!loading && history && history.items.length === 0 && <p>No email status reports have been recorded for this application.</p>}
      {!loading && history && !!history.items.length && <ol>
        {history.items.map((item, index) => {
          const created = dateLabel(item.created_at);
          return <li key={`${item.sequence}:${index}`}>
            <strong>{formatStatus(item.reported_status || item.decision)}</strong>
            <span> · {formatCategory(item.category)}</span>
            <p>Reported by you from email · sequence {Number.isSafeInteger(item.sequence) && item.sequence >= 0 ? item.sequence : 'not recorded'}</p>
            {created && <time dateTime={created.iso}>{created.visible}</time>}
          </li>;
        })}
      </ol>}
    </section>
  </section>;
}

export function EmailStatusReview({ profileId, applicationId, onStatusChanged }: {
  profileId: string;
  applicationId: string;
  onStatusChanged?: () => void;
}) {
  const scopeKey = `${profileId}\u0000${applicationId}`;
  const [state, setState] = useState(() => initialState(scopeKey, true));
  const requestScope = useRef(scopeKey);
  const fileReadSequence = useRef(0);
  const inflight = useRef(new Set<AbortController>());
  const retryRequest = useRef<{ scopeKey: string; previewId: string; requestId: string } | null>(null);
  requestScope.current = scopeKey;
  const visible = emailStatusStateForScope(state, scopeKey);
  if (state.scopeKey !== scopeKey) setState(visible);

  const updateIfCurrent = (key: string, update: (current: EmailStatusReviewState) => EmailStatusReviewState) => {
    if (requestScope.current !== key) return;
    setState(current => current.scopeKey === key ? update(current) : current);
  };

  const scopedReadJson = async <T,>(url: string, init?: RequestInit): Promise<T> => {
    const controller = new AbortController();
    inflight.current.add(controller);
    try {
      return await readJson<T>(url, { ...init, signal: controller.signal });
    } finally {
      inflight.current.delete(controller);
    }
  };

  const loadHistory = async (key: string, profile: string, application: string, keepDraft = false) => {
    if (requestScope.current !== key) return;
    updateIfCurrent(key, current => ({ ...current, loading: true, error: '', success: keepDraft ? '' : current.success }));
    try {
      const result = await scopedReadJson<EmailStatusHistory>(path(profile, application));
      if (!result || !Array.isArray(result.items)) throw new Error('Invalid history response.');
      updateIfCurrent(key, current => ({
        ...current,
        history: result,
        loading: false,
        stale: false,
        preview: keepDraft ? null : current.preview,
        confirmed: false,
        error: '',
      }));
    } catch {
      updateIfCurrent(key, current => ({ ...current, loading: false, error: 'history' }));
    }
  };

  useEffect(() => {
    const key = scopeKey;
    const profile = profileId;
    const application = applicationId;
    let active = true;
    fileReadSequence.current += 1;
    retryRequest.current = null;
    setState(initialState(key, true));
    void readJson<EmailStatusHistory>(path(profile, application)).then(result => {
      if (!active || requestScope.current !== key) return;
      if (!result || !Array.isArray(result.items)) throw new Error('Invalid history response.');
      setState(current => current.scopeKey === key ? { ...current, history: result, loading: false } : current);
    }).catch(() => {
      if (active && requestScope.current === key) {
        setState(current => current.scopeKey === key ? { ...current, loading: false, error: 'history' } : current);
      }
    });
    return () => { active = false; };
  }, [scopeKey]);

  useEffect(() => () => {
    for (const controller of inflight.current) controller.abort();
    inflight.current.clear();
  }, [scopeKey]);

  const updateDraft = (text: string, format: EmailStatusFormat, fileSelected: boolean) => {
    fileReadSequence.current += 1;
    retryRequest.current = null;
    updateIfCurrent(scopeKey, current => ({ ...current, draft: text, format, fileSelected,
      preview: null, confirmed: false, stale: false, busy: false, error: '', success: '' }));
  };

  const selectFile = async (file?: File) => {
    if (!file) return;
    const readSequence = ++fileReadSequence.current;
    const key = scopeKey;
    retryRequest.current = null;
    updateIfCurrent(key, current => ({ ...current, draft: '', format: 'text', fileSelected: false,
      preview: null, confirmed: false, stale: false, busy: true, error: '', success: '' }));
    const extension = file.name.toLowerCase().split('.').pop();
    const format: EmailStatusFormat = extension === 'eml' ? 'eml' : 'text';
    if (!['eml', 'txt'].includes(extension || '')) {
      updateIfCurrent(key, current => ({ ...current, busy: false, draft: '', format: 'text', fileSelected: false, preview: null,
        error: 'format', confirmed: false, success: '' }));
      return;
    }
    if (file.size > EMAIL_STATUS_MAX_BYTES) {
      updateIfCurrent(key, current => ({ ...current, busy: false, draft: '', format: 'text', fileSelected: false, preview: null,
        error: 'size', confirmed: false, success: '' }));
      return;
    }
    try {
      const text = await file.text();
      if (currentDraftSize(text) > EMAIL_STATUS_MAX_BYTES) throw new Error('Message too large.');
      if (fileReadSequence.current !== readSequence || requestScope.current !== key) return;
      updateDraft(text, format, true);
    } catch {
      if (fileReadSequence.current !== readSequence || requestScope.current !== key) return;
      updateIfCurrent(key, current => ({ ...current, busy: false, draft: '', format: 'text', fileSelected: false, preview: null,
        error: 'read', confirmed: false, success: '' }));
    }
  };

  const previewMessage = async () => {
    const text = visible.draft;
    if (!text.trim() || !isEmailStatusMessageWithinLimit(text) || visible.busy) return;
    retryRequest.current = null;
    updateIfCurrent(scopeKey, current => ({ ...current, busy: true, preview: null, confirmed: false, stale: false, error: '', success: '' }));
    try {
      const result = await scopedReadJson<EmailStatusPreview>(path(profileId, applicationId, '/preview'), {
        method: 'POST', body: JSON.stringify({ content: text, format: visible.format }),
      });
      updateIfCurrent(scopeKey, current => ({ ...current, busy: false, preview: result, confirmed: false }));
    } catch {
      updateIfCurrent(scopeKey, current => ({ ...current, busy: false, error: 'preview' }));
    }
  };

  const confirmStatus = async () => {
    const reviewed = visible.preview;
    if (!visible.confirmed || !isReviewableEmailStatusPreview(reviewed) || visible.stale || visible.busy) return;
    const key = scopeKey;
    const retry = requestIdForEmailStatusRetry(retryRequest.current, key, reviewed.preview_id);
    retryRequest.current = retry;
    updateIfCurrent(key, current => ({ ...current, busy: true, error: '', success: '' }));
    try {
      const result = await scopedReadJson<EmailStatusApplyResponse>(path(profileId, applicationId, `/${encodeURIComponent(reviewed.preview_id)}/apply`), {
        method: 'POST',
        body: JSON.stringify({ request_id: retry.requestId, status: reviewed.suggested_status,
          expected_tracking_sequence: reviewed.expected_tracking_sequence, reviewed: true }),
      });
      if (!result?.applied || result.preview_id !== reviewed.preview_id
        || result.status !== reviewed.suggested_status || result.attribution !== 'USER_REPORTED_EMAIL') {
        throw new Error('Status response did not match the reviewed suggestion.');
      }
      retryRequest.current = null;
      updateIfCurrent(key, current => ({ ...current, busy: false, success: 'saved', draft: '', fileSelected: false,
        preview: null, confirmed: false, stale: false, error: '' }));
      try { onStatusChanged?.(); } catch { /* The durable history remains the source of truth. */ }
      await loadHistory(key, profileId, applicationId);
    } catch (reason) {
      const details = errorDetails(reason);
      if (isEmailStatusStaleConflict(reason)) {
        retryRequest.current = null;
        updateIfCurrent(key, current => ({ ...current, busy: false, stale: true, error: 'stale', confirmed: false }));
      } else if (details.status === 409 && details.code === 'EMAIL_STATUS_NOT_APPLICABLE') {
        retryRequest.current = null;
        updateIfCurrent(key, current => ({ ...current, busy: false, preview: null, error: 'not-applicable', confirmed: false }));
      } else if (details.status === 409 && details.code === 'EMAIL_STATUS_REQUEST_CONFLICT') {
        retryRequest.current = null;
        updateIfCurrent(key, current => ({ ...current, busy: false, preview: null, error: 'request-conflict', confirmed: false }));
      } else {
        updateIfCurrent(key, current => ({ ...current, busy: false, error: 'apply' }));
      }
    }
  };

  return <EmailStatusReviewContent
    key={scopeKey}
    draft={visible.draft}
    format={visible.format}
    fileSelected={visible.fileSelected}
    preview={visible.preview}
    history={visible.history}
    loading={visible.loading}
    busy={visible.busy}
    confirmed={visible.confirmed}
    stale={visible.stale}
    error={visible.error}
    success={visible.success}
    onDraftChange={text => updateDraft(text, 'text', false)}
    onFormatChange={format => updateIfCurrent(scopeKey, current => ({ ...current, format }))}
    onSelectFile={file => void selectFile(file)}
    onClearFile={() => updateDraft('', 'text', false)}
    onPreview={() => void previewMessage()}
    onConfirm={() => void confirmStatus()}
    onRefresh={() => void loadHistory(scopeKey, profileId, applicationId, true)}
    onConfirmationChange={confirmed => updateIfCurrent(scopeKey, current => ({ ...current, confirmed }))}
  />;
}
