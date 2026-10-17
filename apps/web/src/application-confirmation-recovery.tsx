import {useEffect, useRef, useState} from 'react';

export type ConfirmationCheck = {
  profile_id: string; application_id: string; check_id: string;
  status: 'CONFIRMED' | 'NO_VERIFIED_RECEIPT'; application_state: string;
  checked_at: string; provider_receipt_verified: boolean;
  confirmation_source: 'RECEIPT_LOOKUP' | 'PREVIOUS_CONFIRMATION' | 'NONE';
  confirmation: {reference_number: string; timestamp: string} | null;
  replayed: boolean; browser_started: false; submission_attempted: false;
};

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
  if (check.profile_id !== profileId || check.application_id !== applicationId ||
      !['CONFIRMED','NO_VERIFIED_RECEIPT'].includes(String(check.status)) ||
      check.browser_started !== false || check.submission_attempted !== false) {
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
    </> : <p>No verified receipt is available. This does not mean the application was not submitted. Do not blindly resubmit.</p>}
    <p>Checked <time>{result.checked_at}</time>{result.replayed ? ' (saved result replayed)' : ''}. No browser was launched and no application was submitted by this check.</p>
  </div>;
}

export function ApplicationConfirmationRecovery({profileId, applicationId, state, onReconciled}: {
  profileId: string; applicationId: string; state: string; onReconciled?: () => void;
}) {
  const [result, setResult] = useState<ConfirmationCheck | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const pending = useRef<AbortController | null>(null);
  const request = useRef<{profile: string; application: string; id: string} | null>(null);
  useEffect(() => {
    setResult(null); setError(''); setBusy(false); request.current = null;
    return () => pending.current?.abort();
  }, [profileId, applicationId]);
  const eligible = ['SUBMITTING', 'SUBMISSION_UNCONFIRMED'].includes(state);
  const visible = result?.profile_id === profileId && result.application_id === applicationId ? result : null;
  if (!eligible && !visible) return null;
  async function checkReceipt() {
    if (busy || !eligible) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError('');
    if (request.current?.profile !== profileId || request.current.application !== applicationId) {
      request.current = {profile: profileId, application: applicationId, id: crypto.randomUUID()};
    }
    try {
      const value = await checkApplicationConfirmation(profileId, applicationId, request.current.id, fetch, controller.signal);
      if (!controller.signal.aborted) {
        setResult(value); request.current = null;
        if (value.status === 'CONFIRMED') onReconciled?.();
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
    <button type="button" disabled={busy || !eligible} onClick={() => void checkReceipt()}>Check receipt only</button>
    {busy && <p role="status">Checking the local provider receipt?</p>}
    {error && <p role="alert">{error}</p>}
    {visible && <ConfirmationCheckResult result={visible}/>}
  </section>;
}
