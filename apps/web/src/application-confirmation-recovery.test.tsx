import {describe, expect, it, vi} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {checkApplicationConfirmation, ConfirmationCheckResult, type ConfirmationCheck} from './application-confirmation-recovery';

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
