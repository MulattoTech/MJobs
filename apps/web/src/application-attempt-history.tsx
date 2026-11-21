import {useEffect, useState} from 'react';
import {requestWithDeadline} from './api';

export type NativeFixtureProof = {
  provider:'greenhouse'|'lever'; request_id:string; job_id:string;
  review_sha256:string; server_contract_sha256:string; metadata_fingerprint:string;
  profile_sha256:string; route_sha256:string; template_sha256:string; analysis_sha256:string;
  reviewed_fields:{field_key:'country'|'remote'|'fixture_question';field_sha256:string;
    answer_id:string;answer_sha256:string}[];
};

export type ApplicationAttempt = {
  attempt_id: string; application_id: string; state: string;
  started_at: string; finished_at: string | null; adapter: string | null;
  browser_session: string | null; current_step: string | null; error_code: string | null;
  route_id: string | null; input_fingerprint: string | null;
  profile_version: number | null; document_version: number | null; mode: string | null;
  confirmation: {reference_number: string; timestamp: string | null} | null;
  steps: string[]; legacy_metadata: boolean;
  verified_uploads?: {kind: string; document_id: string; size_bytes: number; sha256: string}[];
  native_fixture?: NativeFixtureProof | null;
};
export type AttemptHistory = {
  profile_id: string; application_id: string; attempts: ApplicationAttempt[];
  has_more: boolean; external_submission_enabled: false;
};

const objectRecord = (value:unknown):value is Record<string,unknown> =>
  value!==null&&typeof value==='object'&&!Array.isArray(value);
const digest = (value:unknown):value is string => typeof value==='string'&&/^[0-9a-f]{64}$/.test(value);
function isNativeFixtureProof(value:unknown):value is NativeFixtureProof {
  if(!objectRecord(value)||(value.provider!=='greenhouse'&&value.provider!=='lever')
    ||typeof value.request_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.request_id)
    ||typeof value.job_id!=='string'||!value.job_id.trim()||value.job_id.length>128||/[\x00-\x1f]/.test(value.job_id))return false;
  const hashes=['review_sha256','server_contract_sha256','metadata_fingerprint','profile_sha256',
    'route_sha256','template_sha256','analysis_sha256'];
  if(!hashes.every(key=>digest(value[key])))return false;
  // Mirror the bounded, value-free read_native_reviews API projection. A
  // provider name or a partial contract alone cannot identify this fixture.
  const fields=value.reviewed_fields;
  const keys=['country','remote','fixture_question'];
  return Array.isArray(fields)&&fields.length===keys.length&&fields.every(row=>objectRecord(row)
    &&Object.keys(row).sort().join(',')==='answer_id,answer_sha256,field_key,field_sha256'
    &&typeof row.field_key==='string'&&keys.includes(row.field_key)
    &&typeof row.answer_id==='string'&&/^[A-Za-z0-9_-]{1,80}$/.test(row.answer_id)
    &&digest(row.field_sha256)&&digest(row.answer_sha256))
    &&new Set(fields.map(row=>row.field_key)).size===keys.length;
}

function NativeFixtureReview({attempt,externalSubmissionEnabled}:{attempt:ApplicationAttempt;externalSubmissionEnabled:boolean}) {
  const proof=attempt.native_fixture;
  if(externalSubmissionEnabled!==false||attempt.adapter!=='mock-ats'||attempt.legacy_metadata!==false
    ||!['DRY_RUN','MOCK_SUBMISSION'].includes(attempt.mode||'')||!isNativeFixtureProof(proof))return null;
  return <section aria-label="Controlled local fixture adapter">
    <h4>Adapter: {proof.provider==='greenhouse'?'Greenhouse':'Lever'} — controlled local fixture</h4>
    <p>Owned local fixture review. No live provider submission is represented.</p>
    <p>Exact reviewed fields: {proof.reviewed_fields.length}</p>
    <p>This review does not establish form validation, file uploads, or submission.</p>
  </section>;
}

export function AttemptHistoryView({history}: {history: AttemptHistory}) {
  return <>
    {!history.attempts.length && <p>No browser attempts have been recorded for this application.</p>}
    <ol aria-label="Durable application attempts">
      {history.attempts.map(attempt => <li key={attempt.attempt_id}>
        <details open={history.attempts.length === 1}>
          <summary>{attempt.state} ? {attempt.adapter || 'Legacy adapter not recorded'} ? {attempt.started_at}</summary>
          <p>Attempt <code>{attempt.attempt_id}</code></p>
          <p>{attempt.mode || 'Mode not recorded'} ? Current step: {attempt.current_step || 'Not recorded'}</p>
          <NativeFixtureReview attempt={attempt} externalSubmissionEnabled={history.external_submission_enabled}/>
          <p>Started: <time>{attempt.started_at}</time><br/>Finished: {attempt.finished_at ? <time>{attempt.finished_at}</time> : 'Not recorded'}</p>
          {attempt.legacy_metadata && <p>Legacy record: missing metadata has not been inferred or backfilled.</p>}
          {attempt.state === 'RUNNING' && <p role="status">A run is reserved. A missing completion record does not prove the worker stopped. Do not start a competing browser session.</p>}
          {attempt.state === 'SUBMISSION_UNCONFIRMED' && <p role="status">Submission is uncertain. Check the provider receipt before considering another submission.</p>}
          {attempt.error_code && <p>Recorded outcome: <code>{attempt.error_code}</code></p>}
          {attempt.profile_version !== null && <p>Frozen inputs: profile version {attempt.profile_version}, document version {attempt.document_version}.</p>}
          {attempt.route_id && <p>Route identity: <code>{attempt.route_id}</code></p>}
          {attempt.input_fingerprint && <p>Input fingerprint: <code>{attempt.input_fingerprint}</code></p>}
          {attempt.confirmation && <p>Verified fixture receipt: <code>{attempt.confirmation.reference_number}</code></p>}
          {!!attempt.verified_uploads?.length && <section aria-label="Verified browser file selection">
            <h4>Browser file selection verified</h4>
            <p>Exact selected bytes were checked before review. This is not proof of provider acceptance.</p>
            <ul>{attempt.verified_uploads.map((file, index) => <li key={`${index}:${file.document_id}`}>
              {file.kind} ? {file.size_bytes} bytes ? SHA-256 <code>{file.sha256}</code>
            </li>)}</ul>
          </section>}
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
    void requestWithDeadline(path, {signal: controller.signal}, async response => {
      if (!response.ok) throw new Error('History unavailable');
      return await response.json() as AttemptHistory;
    }).then(value => {
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
