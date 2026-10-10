import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { CredentialConnectionFeedback } from './credential-connection-feedback';

describe('credential connection feedback', () => {
  it('confirms a successful authentication check without suggesting inference ran', () => {
    const html = renderToStaticMarkup(
      <CredentialConnectionFeedback status="CONNECTED" code={null} />,
    );

    expect(html).toContain('Provider authentication succeeded.');
    expect(html).toContain('No model inference was run.');
    expect(html).toContain('role="status"');
  });

  it('explains timeout separately from provider unavailability', () => {
    const timeout = renderToStaticMarkup(
      <CredentialConnectionFeedback status="FAILED" code="CREDENTIAL_PROVIDER_TIMEOUT" />,
    );
    const unavailable = renderToStaticMarkup(
      <CredentialConnectionFeedback status="FAILED" code="CREDENTIAL_PROVIDER_UNAVAILABLE" />,
    );

    expect(timeout).toContain('timed out');
    expect(timeout).not.toContain('provider rejected this credential');
    expect(unavailable).toContain('could not be reached');
    expect(unavailable).not.toContain('timed out');
  });

  it('gives a credential-specific next step for provider rejection', () => {
    const html = renderToStaticMarkup(
      <CredentialConnectionFeedback status="FAILED" code="CREDENTIAL_REJECTED" />,
    );

    expect(html).toContain('provider rejected this credential');
    expect(html).toContain('rotate it and test again');
    expect(html).toContain('role="alert"');
  });

  it('hides unknown codes and presents safe fallback guidance', () => {
    const secretLikeProviderBody = 'PRIVATE_KEY provider returned raw response';
    const html = renderToStaticMarkup(
      <CredentialConnectionFeedback status="FAILED" code={secretLikeProviderBody} />,
    );

    expect(html).toContain('check could not be completed');
    expect(html).toContain('Try again');
    expect(html).not.toContain(secretLikeProviderBody);
    expect(html).not.toContain('PRIVATE_KEY');
  });

  it('handles an unknown status without trusting its accompanying code', () => {
    const html = renderToStaticMarkup(
      <CredentialConnectionFeedback status="MAYBE" code="CREDENTIAL_REJECTED" />,
    );

    expect(html).toContain('check could not be completed');
    expect(html).not.toContain('provider rejected this credential');
  });
});
