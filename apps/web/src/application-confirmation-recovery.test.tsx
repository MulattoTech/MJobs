import {describe, expect, it, vi} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {
  checkApplicationConfirmation,
  loadLatestApplicationConfirmation,
  shouldLoadConfirmationRecovery,
  ConfirmationCheckResult,
  type ConfirmationCheck,
} from './application-confirmation-recovery';

const result: ConfirmationCheck = {
  profile_id:'fixture', application_id:'app', check_id:'check', status:'CONFIRMED',
  application_state:'CONFIRMED', checked_at:'2026-10-10T08:00:00Z',
  provider_receipt_verified:true, confirmation_source:'RECEIPT_LOOKUP',
  confirmation:{reference_number:'MOCK-one',timestamp:'2026-10-10T07:59:00Z'},
  replayed:false, browser_started:false, submission_attempted:false,
};
const render = (changes: Partial<ConfirmationCheck> = {}) => renderToStaticMarkup(
  <ConfirmationCheckResult result={{...result,...changes}}/>);

describe('explicit confirmation reconciliation', () => {
  it('limits saved receipt hydration to ambiguous or confirmed application states', () => {
    for (const state of ['SUBMITTING', 'SUBMISSION_UNCONFIRMED', 'CONFIRMED']) {
      expect(shouldLoadConfirmationRecovery(state)).toBe(true);
    }
    for (const state of ['PREPARED', 'REVIEW_REQUIRED', 'CANCELLED', 'DRAFT']) {
      expect(shouldLoadConfirmationRecovery(state)).toBe(false);
    }
  });

  it('shows verified fixture receipt and preserves original history meaning', () => {
    const html = render();
    expect(html).toContain('Fixture receipt verified');
    expect(html).toContain('MOCK-one');
    expect(html).toContain('original attempt history is preserved');
  });
  it('never interprets missing evidence as permission to resubmit', () => {
    const html = render({status:'NO_VERIFIED_RECEIPT',confirmation:null,provider_receipt_verified:false,confirmation_source:'NONE'});
    expect(html).toContain('does not mean the application was not submitted');
    expect(html).toContain('Do not blindly resubmit');
    expect(html).not.toContain('Fixture receipt verified');
  });
  it('distinguishes previous confirmation from a new provider verification', () => {
    const html = render({provider_receipt_verified:false,confirmation_source:'PREVIOUS_CONFIRMATION'});
    expect(html).toContain('did not verify a new receipt');
    expect(html).not.toContain('Fixture receipt verified');
  });
  it('labels a saved response and escapes receipt contents', () => {
    const html = render({replayed:true,confirmation:{reference_number:'<script>bad</script>',timestamp:''}});
    expect(html).toContain('saved result replayed');
    expect(html).toContain('&lt;script&gt;');
    expect(html).not.toContain('<script>');
  });

  it('shows a read-only unavailable lookup as uncertainty, never as a failed submission', () => {
    const html = render({status:'CHECK_UNAVAILABLE', lookup_failure_code:'RECEIPT_LOOKUP_TIMEOUT'});
    expect(html).toContain('Receipt lookup is unavailable');
    expect(html).toContain('This check did not submit an application');
    expect(html).toContain('earlier submission remains uncertain');
    expect(html).not.toContain('Do not blindly resubmit');
    expect(html).not.toContain('RECEIPT_LOOKUP_TIMEOUT');
  });

  it('loads durable latest evidence with a read-only GET and accepts an empty history', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({result:{...result, application_id:'application&b'}}))
      .mockResolvedValueOnce(Response.json({result:null}));

    await expect(loadLatestApplicationConfirmation('profile/a', 'application&b', fetcher))
      .resolves.toMatchObject({status:'CONFIRMED'});
    await expect(loadLatestApplicationConfirmation('profile/a', 'application&b', fetcher)).resolves.toBeNull();
    expect(fetcher.mock.calls.map(([url, options]) => [url, options?.method, options?.cache])).toEqual([
      ['/profiles/profile%2Fa/applications/application%26b/confirmation-check/latest', 'GET', 'no-store'],
      ['/profiles/profile%2Fa/applications/application%26b/confirmation-check/latest', 'GET', 'no-store'],
    ]);
  });

  it('rejects latest evidence scoped to another profile or application', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      result:{...result, application_id:'another-application'},
    }));

    await expect(loadLatestApplicationConfirmation('fixture', 'app', fetcher))
      .rejects.toThrow('Saved receipt evidence could not be loaded. Submission remains uncertain; do not resubmit.');
  });

  it('rejects durable evidence whose captured state no longer matches its receipt outcome', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      result:{...result, application_state:'SUBMISSION_UNCONFIRMED'},
    }));

    await expect(loadLatestApplicationConfirmation('fixture', 'app', fetcher))
      .rejects.toThrow('Saved receipt evidence could not be loaded. Submission remains uncertain; do not resubmit.');
  });

  it('accepts a durable unavailable check without exposing its safe failure code', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...result, status:'CHECK_UNAVAILABLE', application_state:'SUBMISSION_UNCONFIRMED', lookup_failure_code:'RECEIPT_LOOKUP_TIMEOUT',
      provider_receipt_verified:false, confirmation_source:'NONE', confirmation:null,
    }));

    await expect(checkApplicationConfirmation('fixture', 'app', 'stable-check-5', fetcher))
      .resolves.toMatchObject({status:'CHECK_UNAVAILABLE', lookup_failure_code:'RECEIPT_LOOKUP_TIMEOUT'});
  });

  it('reads only the scoped receipt-check endpoint and preserves a missing-confirmation result', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...result, status:'NO_VERIFIED_RECEIPT', application_state:'SUBMISSION_UNCONFIRMED',
      confirmation:null, provider_receipt_verified:false, confirmation_source:'NONE',
    }));

    await expect(checkApplicationConfirmation('profile/a', 'application&b', 'stable-check-1', fetcher))
      .resolves.toMatchObject({status:'NO_VERIFIED_RECEIPT', application_state:'SUBMISSION_UNCONFIRMED'});
    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%2Fa/applications/application%26b/confirmation-check',
      expect.objectContaining({method:'POST', cache:'no-store', credentials:'same-origin',
        body:JSON.stringify({request_id:'stable-check-1'})}),
    );
  });

  it('refuses to reconcile while the application execution remains reserved', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(
      {detail:{code:'CONFIRMATION_EXECUTION_STILL_RESERVED'}}, {status:409},
    ));

    await expect(checkApplicationConfirmation('fixture', 'app', 'stable-check-2', fetcher))
      .rejects.toThrow('A worker or unfinished browser attempt still owns this run. Its reservation was not changed.');
  });

  it('keeps transport interruption explicitly uncertain without retrying or submitting', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new Error('private transport detail'));

    await expect(checkApplicationConfirmation('fixture', 'app', 'stable-check-3', fetcher))
      .rejects.toThrow('The receipt check could not complete. Submission remains uncertain; do not resubmit.');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0][1]).toMatchObject({method:'POST', body:'{"request_id":"stable-check-3"}'});
  });

  it('rejects mismatched or side-effecting responses before rendering them', async () => {
    for (const invalid of [
      {...result, application_id:'another-application'},
      {...result, submission_attempted:true},
      {...result, browser_started:true},
    ]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(invalid));
      await expect(checkApplicationConfirmation('fixture', 'app', 'stable-check-4', fetcher))
        .rejects.toThrow('The receipt check returned an unexpected result. Reload the application before proceeding.');
    }
  });
});
