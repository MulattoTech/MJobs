import {describe, expect, it} from 'vitest';
import {renderToStaticMarkup} from 'react-dom/server';
import {ConfirmationCheckResult, type ConfirmationCheck} from './application-confirmation-recovery';

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
});
