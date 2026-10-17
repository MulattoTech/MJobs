import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationFormReviewCapabilityConnected, createApplicationFormReviewCapabilityClient } from './application-form-review-connected';

const capability = {
  schema_version: 1, profile_id: 'profile/a', application_id: 'application&b', provider: 'ashby',
  review_supported: true, read_only: true, submission_supported: false,
};
const load = (body: unknown) => createApplicationFormReviewCapabilityClient(
  capability.profile_id, capability.application_id, vi.fn<typeof fetch>().mockResolvedValue(Response.json(body)),
).load();

describe('fleet T14 registry capability gate', () => {
  it('accepts scoped read-only capability without a frontend provider allowlist', async () => {
    for (const provider of ['ashby', 'workday', 'future_provider']) {
      await expect(load({ ...capability, provider })).resolves.toEqual({ status: 'supported', provider });
    }
  });

  it('requests only the scoped capability endpoint without admitting a review', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(capability));
    await createApplicationFormReviewCapabilityClient(capability.profile_id, capability.application_id, fetcher).load();
    expect(fetcher).toHaveBeenCalledExactlyOnceWith(
      '/profiles/profile%2Fa/applications/application%26b/form-review-capabilities',
      expect.objectContaining({ method: 'GET', cache: 'no-store', credentials: 'same-origin' }),
    );
  });

  it.each([
    { schema_version: 2 }, { profile_id: 'other' }, { application_id: 'other' },
    { read_only: false }, { submission_supported: true }, { provider: null },
    { provider: '' }, { provider: 'private@example.test' }, { review_supported: 'true' },
  ])('fails closed on malformed or wrong-scope capability %j', async overrides => {
    await expect(load({ ...capability, ...overrides })).resolves.toEqual({ status: 'error', provider: null });
  });

  it('keeps an unsupported route unavailable even with unrelated authentication metadata', async () => {
    await expect(load({ ...capability, provider: null, review_supported: false,
      user_auth_declaration: { status: 'USER_DECLARED', authenticated_session_available: true } }))
      .resolves.toEqual({ status: 'unsupported', provider: null });
  });

  it('drops a late capability response after scope changes', async () => {
    let current = true;
    let resolve!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>(done => { resolve = done; }));
    const pending = createApplicationFormReviewCapabilityClient(capability.profile_id, capability.application_id,
      fetcher, () => current).load();
    current = false;
    resolve(Response.json(capability));
    await expect(pending).resolves.toEqual({ status: 'error', provider: null });
  });

  it('never fetches capability for an aborted or missing selection', async () => {
    const controller = new AbortController();
    controller.abort();
    const fetcher = vi.fn<typeof fetch>();
    await expect(createApplicationFormReviewCapabilityClient('p', 'a', fetcher, () => true, controller.signal).load())
      .resolves.toEqual({ status: 'error', provider: null });
    await expect(createApplicationFormReviewCapabilityClient('', 'a', fetcher).load())
      .resolves.toEqual({ status: 'error', provider: null });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('hides HTTP and malformed JSON errors without attempting a POST', async () => {
    for (const response of [new Response('private error', { status: 404 }), new Response('not JSON')]) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(response);
      await expect(createApplicationFormReviewCapabilityClient('p', 'a', fetcher).load())
        .resolves.toEqual({ status: 'error', provider: null });
      expect(fetcher).toHaveBeenCalledTimes(1);
    }
  });

  it('does not expose inspection controls before capability is established', () => {
    const fetcher = vi.fn<typeof fetch>();
    const html = renderToStaticMarkup(<ApplicationFormReviewCapabilityConnected
      profileId="p" applicationId="a" fetcher={fetcher} />);
    expect(html).toContain('Checking read-only form review availability');
    expect(html).not.toContain('Inspect form fields');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
