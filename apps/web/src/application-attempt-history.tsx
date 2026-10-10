import {useEffect, useState} from 'react';

export type ApplicationAttempt = {
  attempt_id: string; application_id: string; state: string;
  started_at: string; finished_at: string | null; adapter: string | null;
  browser_session: string | null; current_step: string | null; error_code: string | null;
  route_id: string | null; input_fingerprint: string | null;
  profile_version: number | null; document_version: number | null; mode: string | null;
  confirmation: {reference_number: string; timestamp: string | null} | null;
  steps: string[]; legacy_metadata: boolean;
};
export type AttemptHistory = {
  profile_id: string; application_id: string; attempts: ApplicationAttempt[];
  has_more: boolean; external_submission_enabled: false;
};

export function AttemptHistoryView({history}: {history: AttemptHistory}) {
  return <>
    {!history.attempts.length && <p>No browser attempts have been recorded for this application.</p>}
    <ol aria-label="Durable application attempts">
      {history.attempts.map(attempt => <li key={attempt.attempt_id}>
        <details open={history.attempts.length === 1}>
          <summary>{attempt.state} ? {attempt.adapter || 'Legacy adapter not recorded'} ? {attempt.started_at}</summary>
          <p>Attempt <code>{attempt.attempt_id}</code></p>
          <p>{attempt.mode || 'Mode not recorded'} ? Current step: {attempt.current_step || 'Not recorded'}</p>
          <p>Started: <time>{attempt.started_at}</time><br/>Finished: {attempt.finished_at ? <time>{attempt.finished_at}</time> : 'Not recorded'}</p>
          {attempt.legacy_metadata && <p>Legacy record: missing metadata has not been inferred or backfilled.</p>}
          {attempt.state === 'RUNNING' && <p role="status">A run is reserved. A missing completion record does not prove the worker stopped. Do not start a competing browser session.</p>}
          {attempt.state === 'SUBMISSION_UNCONFIRMED' && <p role="status">Submission is uncertain. Check the provider receipt before considering another submission.</p>}
          {attempt.error_code && <p>Recorded outcome: <code>{attempt.error_code}</code></p>}
          {attempt.profile_version !== null && <p>Frozen inputs: profile version {attempt.profile_version}, document version {attempt.document_version}.</p>}
          {attempt.route_id && <p>Route identity: <code>{attempt.route_id}</code></p>}
          {attempt.input_fingerprint && <p>Input fingerprint: <code>{attempt.input_fingerprint}</code></p>}
          {attempt.confirmation && <p>Verified fixture receipt: <code>{attempt.confirmation.reference_number}</code></p>}
          <ol aria-label={`Steps for attempt ${attempt.attempt_id}`}>
            {attempt.steps.map((step, index) => <li key={`${index}:${step}`}>{step}</li>)}
          </ol>
        </details>
      </li>)}
    </ol>
    {history.has_more && <p>Showing the latest 100 attempts. Earlier records are retained in the local database.</p>}
  </>;
}

export function ApplicationAttemptHistory({profileId, applicationId}: {profileId: string; applicationId: string}) {
  const [history, setHistory] = useState<AttemptHistory | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    setHistory(null); setError(''); setLoading(true);
    const path = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/attempt-history`;
    void fetch(path, {signal: controller.signal}).then(async response => {
      if (!response.ok) throw new Error('History unavailable');
      const value = await response.json() as AttemptHistory;
      if (value.profile_id !== profileId || value.application_id !== applicationId || !Array.isArray(value.attempts)) {
        throw new Error('History scope mismatch');
      }
      if (!controller.signal.aborted) setHistory(value);
    }).catch(() => {
      if (!controller.signal.aborted) setError('Attempt history could not be loaded. No application action was performed.');
    }).finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [profileId, applicationId, revision]);
  const visible = history?.profile_id === profileId && history.application_id === applicationId ? history : null;
  return <section className="panel" aria-label="Durable attempt history">
    <h3>Execution history</h3>
    <p>These are local execution records, not proof of an employer application. External submission remains off.</p>
    <button type="button" disabled={loading} onClick={() => setRevision(value => value + 1)}>Refresh attempt history</button>
    {loading && <p role="status">Loading attempt history?</p>}
    {error && <p role="alert">{error}</p>}
    {visible && <AttemptHistoryView history={visible}/>}
  </section>;
}
