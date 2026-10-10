import {describe, expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {AttemptHistoryView, type AttemptHistory, type ApplicationAttempt} from './application-attempt-history';

const attempt: ApplicationAttempt = {
  attempt_id:'attempt-1', application_id:'app-1', state:'DRY_RUN_COMPLETE',
  started_at:'2026-10-10T08:00:00Z', finished_at:'2026-10-10T08:00:01Z',
  adapter:'mock-ats', browser_session:'opaque-session', current_step:'VALIDATE',
  error_code:null, route_id:'route-1', input_fingerprint:'abc123',
  profile_version:2, document_version:3, mode:'DRY_RUN', confirmation:null,
  steps:['NAVIGATE:COMPLETED','VALIDATE:COMPLETED'], legacy_metadata:false,
};
const history: AttemptHistory = {profile_id:'fixture', application_id:'app-1',
  attempts:[attempt], has_more:false, external_submission_enabled:false};
const render = (change: Partial<ApplicationAttempt> = {}) => renderToStaticMarkup(
  <AttemptHistoryView history={{...history, attempts:[{...attempt, ...change}]}}/>);

describe('durable application attempt history', () => {
  it('shows pinned inputs, fixed phases, identity and finish time', () => {
    const html = render();
    for (const text of ['attempt-1','mock-ats','profile version 2','document version 3','abc123','VALIDATE:COMPLETED','08:00:01Z']) expect(html).toContain(text);
  });
  it('does not fabricate missing legacy metadata', () => {
    const html = render({legacy_metadata:true, adapter:null, finished_at:null, profile_version:null, document_version:null});
    expect(html).toContain('Legacy adapter not recorded');
    expect(html).toContain('not been inferred or backfilled');
    expect(html).not.toContain('Frozen inputs');
  });
  it('explains a running reservation without advertising a safe retry', () => {
    const html = render({state:'RUNNING', finished_at:null});
    expect(html).toContain('does not prove the worker stopped');
    expect(html).toContain('Do not start a competing browser session');
  });
  it('warns about ambiguous submission and distinguishes a fixture receipt', () => {
    expect(render({state:'SUBMISSION_UNCONFIRMED'})).toContain('Check the provider receipt');
    expect(render({state:'CONFIRMED',confirmation:{reference_number:'MOCK-abc',timestamp:null}})).toContain('Verified fixture receipt');
  });
  it('escapes record text rather than interpreting markup', () => {
    const html = render({attempt_id:'<script>bad()</script>'});
    expect(html).not.toContain('<script>');
    expect(html).toContain('&lt;script&gt;');
  });
  it('reports bounded and empty history honestly', () => {
    expect(renderToStaticMarkup(<AttemptHistoryView history={{...history,attempts:[]}}/>)).toContain('No browser attempts');
    expect(renderToStaticMarkup(<AttemptHistoryView history={{...history,has_more:true}}/>)).toContain('latest 100 attempts');
  });
});
