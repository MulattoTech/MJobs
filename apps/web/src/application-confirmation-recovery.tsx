import {useEffect, useRef, useState} from 'react';

export type ConfirmationCheck = {
  profile_id: string; application_id: string; check_id: string;
  status: 'CONFIRMED' | 'NO_VERIFIED_RECEIPT' | 'CHECK_UNAVAILABLE'; application_state: string;
  checked_at: string; provider_receipt_verified: boolean; lookup_failure_code?: string | null;
  confirmation_source: 'RECEIPT_LOOKUP' | 'PREVIOUS_CONFIRMATION' | 'NONE';
  confirmation: {reference_number: string; timestamp: string} | null;
  replayed: boolean; browser_started: false; submission_attempted: false;
};

const confirmationStatuses = ['CONFIRMED','NO_VERIFIED_RECEIPT','CHECK_UNAVAILABLE'];
const invalidSavedEvidence = () => new Error('Saved receipt evidence could not be loaded. Submission remains uncertain; do not resubmit.');

export function shouldLoadConfirmationRecovery(state: string): boolean {
  return ['SUBMITTING', 'SUBMISSION_UNCONFIRMED', 'CONFIRMED'].includes(state);
}

function isConfirmationCheck(value: unknown, profileId: string, applicationId: string): value is ConfirmationCheck {
  if (typeof value !== 'object' || value === null) return false;
  const check = value as Record<string, unknown>;
  return check.profile_id === profileId && check.application_id === applicationId &&
    confirmationStatuses.includes(String(check.status)) && check.browser_started === false &&
    check.submission_attempted === false &&
    (check.status === 'CONFIRMED' ? check.application_state === 'CONFIRMED'
      : check.application_state === 'SUBMISSION_UNCONFIRMED');
}

export async function loadLatestApplicationConfirmation(profileId: string, applicationId: string,
  fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<ConfirmationCheck | null> {
  try {
    const response = await fetcher(`/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/confirmation-check/latest`, {
      method: 'GET', signal, cache: 'no-store', credentials: 'same-origin',
    });
    if (!response.ok) throw invalidSavedEvidence();
    const body: unknown = await response.json();
    if (typeof body !== 'object' || body === null || !('result' in body)) throw invalidSavedEvidence();
    const result = (body as {result: unknown}).result;
    if (result === null) return null;
    if (!isConfirmationCheck(result, profileId, applicationId)) throw invalidSavedEvidence();
    return result;
  } catch {
    throw invalidSavedEvidence();
  }
}

export async function checkApplicationConfirmation(profileId: string, applicationId: string, requestId: string,
  fetcher: typeof fetch = fetch, signal?: AbortSignal): Promise<ConfirmationCheck> {
  let response: Response;
  try {
    response = await fetcher(`/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/confirmation-check`, {
      method: 'POST', signal, cache: 'no-store', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify({request_id: requestId}),
    });
  } catch {
    throw new Error('The receipt check could not complete. Submission remains uncertain; do not resubmit.');
  }
  let value: unknown = null;
  try { value = await response.json(); } catch { /* use the fixed error below */ }
  if (!response.ok) {
    const code = typeof value === 'object' && value !== null
      ? (value as {detail?: {code?: unknown}}).detail?.code : undefined;
    if (code === 'CONFIRMATION_EXECUTION_STILL_RESERVED') {
      throw new Error('A worker or unfinished browser attempt still owns this run. Its reservation was not changed.');
    }
    throw new Error('The receipt check could not complete. Submission remains uncertain; do not resubmit.');
  }
  if (typeof value !== 'object' || value === null) {
    throw new Error('The receipt check returned an unexpected result. Reload the application before proceeding.');
  }
  const check = value as Record<string, unknown>;
  if (!isConfirmationCheck(check, profileId, applicationId)) {
    throw new Error('The receipt check returned an unexpected result. Reload the application before proceeding.');
  }
  return check as unknown as ConfirmationCheck;
}

export function ConfirmationCheckResult({result}: {result: ConfirmationCheck}) {
  return <div role="status">
    {result.status === 'CONFIRMED' ? <>
      <p>{result.provider_receipt_verified ? 'Fixture receipt verified.' : 'The earlier fixture confirmation is preserved; this lookup did not verify a new receipt.'}</p>
      {result.confirmation && <p>Receipt: <code>{result.confirmation.reference_number}</code></p>}
      <p>The original attempt history is preserved; this is a later confirmation check.</p>
    </> : result.status === 'NO_VERIFIED_RECEIPT' ? <p>No verified receipt is available. This does not mean the application was not submitted. Do not blindly resubmit.</p>
      : <p>Receipt lookup is unavailable. This check did not submit an application. The earlier submission remains uncertain; do not resubmit.</p>}
    {result.checked_at && <p>Checked <time>{result.checked_at}</time>{result.replayed ? ' (saved result replayed)' : ''}. No browser was launched and no application was submitted by this check.</p>}
  </div>;
}

export function ApplicationConfirmationRecovery({profileId, applicationId, state, onReconciled}: {
  profileId: string; applicationId: string; state: string; onReconciled?: () => void;
}) {
  const [result, setResult] = useState<ConfirmationCheck | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const pending = useRef<AbortController | null>(null);
  const request = useRef<{profile: string; application: string; id: string} | null>(null);
  const loadedScope = useRef<string | null>(null);
  const shouldLoad = shouldLoadConfirmationRecovery(state);
  const readbackScope = JSON.stringify([profileId, applicationId, state]);
  useEffect(() => {
    if (!shouldLoad) {
      pending.current?.abort(); pending.current = null;
      loadedScope.current = null;
      setResult(null); setError(''); setBusy(false); setLoading(false); request.current = null;
      return;
    }
    const controller = new AbortController();
    pending.current?.abort(); pending.current = controller;
    loadedScope.current = null;
    setResult(null); setError(''); setBusy(false); setLoading(true); request.current = null;
    void loadLatestApplicationConfirmation(profileId, applicationId, fetch, controller.signal)
      .then(value => {if (!controller.signal.aborted) setResult(value);})
      .catch(problem => {if (!controller.signal.aborted) setError(state === 'CONFIRMED'
        ? 'Saved receipt evidence could not be loaded. The application remains confirmed; no new receipt lookup was performed.'
        : problem instanceof Error ? problem.message : invalidSavedEvidence().message);})
      .finally(() => {if (!controller.signal.aborted) {loadedScope.current = readbackScope; setLoading(false);}});
    return () => {controller.abort(); pending.current?.abort();};
  }, [profileId, applicationId, state, shouldLoad, readbackScope]);
  const eligible = ['SUBMITTING', 'SUBMISSION_UNCONFIRMED'].includes(state);
  const waitingForReadback = shouldLoad && (loading || loadedScope.current !== readbackScope);
  const resultMatchesState = result?.status === 'CONFIRMED' ? state === 'CONFIRMED'
    : result?.application_state === 'SUBMISSION_UNCONFIRMED' && state === 'SUBMISSION_UNCONFIRMED';
  const visible = shouldLoad && resultMatchesState && result?.profile_id === profileId && result.application_id === applicationId ? result : null;
  if (!shouldLoad) return null;
  if (!eligible && !visible && !waitingForReadback && !error) return null;
  async function checkReceipt() {
    if (busy || waitingForReadback || !eligible) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError('');
    if (request.current?.profile !== profileId || request.current.application !== applicationId) {
      request.current = {profile: profileId, application: applicationId, id: crypto.randomUUID()};
    }
    try {
      const value = await checkApplicationConfirmation(profileId, applicationId, request.current.id, fetch, controller.signal);
      if (!controller.signal.aborted) {
        setResult(value); request.current = null;
        onReconciled?.();
      }
    } catch (problem) {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : 'Receipt check unavailable.');
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return <section className="panel" aria-label="Verify uncertain mock submission">
    <h3>Verify an uncertain mock submission</h3>
    <p>Ask only the configured fictional provider for its receipt. This cannot submit, retry an application, or open a browser.</p>
    <button type="button" disabled={busy || waitingForReadback || !eligible} onClick={() => void checkReceipt()}>Check receipt only</button>
    {waitingForReadback && <p role="status">Loading saved receipt check…</p>}
    {busy && <p role="status">Checking the local provider receipt?</p>}
    {error && <p role="alert">{error}</p>}
    {visible && <ConfirmationCheckResult result={visible}/>}
  </section>;
}
