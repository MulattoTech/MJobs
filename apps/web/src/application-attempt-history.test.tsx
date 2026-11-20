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
const nativeProof = (provider='greenhouse') => ({
  provider, request_id:'operation-1', job_id:'job-1', review_sha256:'a'.repeat(64),
  server_contract_sha256:'b'.repeat(64), metadata_fingerprint:'c'.repeat(64),
  profile_sha256:'d'.repeat(64), route_sha256:'e'.repeat(64),
  template_sha256:'f'.repeat(64), analysis_sha256:'0'.repeat(64),
  reviewed_fields:['country','remote','fixture_question'].map(field_key=>({
    field_key,field_sha256:'1'.repeat(64),answer_id:field_key,answer_sha256:'2'.repeat(64),
  })),
});
const renderNative = (proof:unknown, change:Partial<ApplicationAttempt>={}) => render({
  ...change, native_fixture:proof,
} as Partial<ApplicationAttempt>);

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
  it('shows verified browser bytes without claiming provider acceptance', () => {
    const html = render({verified_uploads:[{kind:'resume',document_id:'doc',size_bytes:123,sha256:'abc'}]});
    expect(html).toContain('Browser file selection verified');
    expect(html).toContain('not proof of provider acceptance');
    expect(html).toContain('123 bytes');
  });
  it('reports bounded and empty history honestly', () => {
    expect(renderToStaticMarkup(<AttemptHistoryView history={{...history,attempts:[]}}/>)).toContain('No browser attempts');
    expect(renderToStaticMarkup(<AttemptHistoryView history={{...history,has_more:true}}/>)).toContain('latest 100 attempts');
  });
  it.each([['greenhouse','Greenhouse'],['lever','Lever']])('names the %s adapter only as a controlled local fixture', (provider,label) => {
    const html=renderNative(nativeProof(provider));
    expect(html).toContain('aria-label="Controlled local fixture adapter"');
    expect(html).toContain(`Adapter: ${label} — controlled local fixture`);
    expect(html).toContain('Exact reviewed fields: 3');
    expect(html).toContain('Owned local fixture review. No live provider submission is represented.');
    expect(html).not.toContain('a'.repeat(64));
    expect(html).not.toContain('2'.repeat(64));
    expect(html).toContain('VALIDATE:COMPLETED');
  });
  it('leaves absent native proof and legacy rendering unchanged', () => {
    expect(renderNative(undefined)).toBe(render());
    expect(renderNative(null)).toBe(render());
    const legacy={legacy_metadata:true,adapter:null,mode:null};
    expect(renderNative(null,legacy)).toBe(render(legacy));
  });
  it.each([
    'greenhouse', [], {}, {provider:'greenhouse'},
    {...nativeProof(),provider:'workday'},
    {...nativeProof(),review_sha256:'invalid'},
    {...nativeProof(),review_sha256:'a'.repeat(64)+'\n'},
    {...nativeProof(),analysis_sha256:null},
    {...nativeProof(),request_id:''},
    {...nativeProof(),request_id:'operation-1\n'},
    {...nativeProof(),job_id:[]},
    {...nativeProof(),reviewed_fields:[]},
    {...nativeProof(),reviewed_fields:[nativeProof().reviewed_fields[0]]},
    {...nativeProof(),reviewed_fields:Array(3).fill(nativeProof().reviewed_fields[0])},
    {...nativeProof(),reviewed_fields:nativeProof().reviewed_fields.map(row=>({...row,answer_sha256:'bad'}))},
    {...nativeProof(),reviewed_fields:nativeProof().reviewed_fields.map(row=>({...row,answer_id:'<script>'}))},
    {...nativeProof(),reviewed_fields:nativeProof().reviewed_fields.map(row=>({...row,answer_id:row.answer_id+'\n'}))},
    {...nativeProof(),reviewed_fields:nativeProof().reviewed_fields.map(row=>({...row,unexpected:'value'}))},
  ])('suppresses malformed or unsupported native proof %#', proof=>{
    const html=renderNative(proof);
    expect(html).not.toContain('Controlled local fixture adapter');
    expect(html).not.toContain('Exact reviewed fields');
    expect(html).not.toContain('Adapter: Greenhouse');
    expect(html).not.toContain('Adapter: Lever');
  });
  it.each([{adapter:'greenhouse'}, {mode:'PERSONAL'}, {legacy_metadata:true}])('does not relabel an incompatible attempt as an owned fixture %#', change=>{
    expect(renderNative(nativeProof(),change)).not.toContain('Controlled local fixture adapter');
  });
  it('shows a failed attempt review without converting it to validation or upload success', () => {
    const html=renderNative(nativeProof(),{state:'FAILED',current_step:'VALIDATE',
      steps:['DETECT_FORM:COMPLETED','VALIDATE:FAILED'],error_code:'FORM_REVIEW_REQUIRED'});
    expect(html).toContain('Adapter: Greenhouse — controlled local fixture');
    expect(html).toContain('Exact reviewed fields: 3');
    expect(html).toContain('This review does not establish form validation, file uploads, or submission.');
    expect(html).toContain('VALIDATE:FAILED');
    expect(html).not.toContain('VALIDATE:COMPLETED');
    expect(html).not.toContain('Browser file selection verified');
    expect(html).not.toContain('Verified fixture receipt');
  });
  it('does not present fixture proof under contradictory external-submission metadata', () => {
    const malformed={...history, external_submission_enabled:true,
      attempts:[{...attempt,native_fixture:nativeProof()}]} as unknown as AttemptHistory;
    expect(renderToStaticMarkup(<AttemptHistoryView history={malformed}/>)).not.toContain('Controlled local fixture adapter');
  });
  it('retains separately verified upload evidence beside a native adapter review', () => {
    const html=renderNative(nativeProof('lever'),{verified_uploads:[{kind:'resume',document_id:'doc',size_bytes:123,sha256:'upload-hash'}]});
    expect(html).toContain('Adapter: Lever — controlled local fixture');
    expect(html).toContain('Browser file selection verified');
    expect(html).toContain('upload-hash');
    expect(html).toContain('not proof of provider acceptance');
  });
});
