import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationFormReviewConnected,
  createApplicationFormReviewClient,
  type ApplicationFormReviewSnapshot,
} from './application-form-review-connected';

const base = { profile_id: 'profile/a', application_id: 'application&b' };
const completedResponse = {
  request_id: 'request-1', state: 'COMPLETED', error_code: null,
  analysis: {
    status: 'REVIEW_REQUIRED', fields: [
      { id: 'field-1', name: 'Email address', type: 'email', required: true, review_reason: 'Confirm this field manually' },
      { id: 'field-2', name: 'Preferred name', type: 'text', required: false, review_reason: null },
    ],
  },
};

describe('connected application form review hydration', () => {
  it('loads the latest profile and application review with a read-only GET', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ ...base, ...completedResponse }));
    const client = createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher);

    await expect(client.load()).resolves.toEqual({
      status: 'completed',
      fields: [
        { name: 'Email address', type: 'email', required: true, reviewRequired: true },
        { name: 'Preferred name', type: 'text', required: false, reviewRequired: false },
      ],
    } satisfies ApplicationFormReviewSnapshot);
    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%2Fa/applications/application%26b/form-reviews/latest',
      expect.objectContaining({ method: 'GET', cache: 'no-store', credentials: 'same-origin' }),
    );
  });

  it('restores only sanitized field metadata and ignores persisted values', async () => {
    const secret = 'private-applicant@example.test';
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...base, ...completedResponse,
      analysis: { ...completedResponse.analysis, fields: [
        { ...completedResponse.analysis.fields[0], value: secret },
      ] },
    }));
    const snapshot = await createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load();

    expect(snapshot).toEqual({ status: 'completed', fields: [
      { name: 'Email address', type: 'email', required: true, reviewRequired: true },
    ] });
    expect(JSON.stringify(snapshot)).not.toContain(secret);
  });

  it('maps an IDLE response to an empty idle state', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...base, request_id: null, state: 'IDLE', error_code: null, analysis: null,
    }));

    await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load())
      .resolves.toEqual({ status: 'idle', fields: [] });
  });

  it('maps UNKNOWN and other unsupported states to unavailable without retaining analysis', async () => {
    for (const state of ['UNKNOWN', 'WORKER_PENDING']) {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
        ...base, ...completedResponse, state,
      }));
      await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load())
        .resolves.toEqual({ status: 'unavailable', fields: [] });
    }
  });

  it('maps API errors to a fixed error state without exposing error codes or bodies', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...base, request_id: 'request-private', state: 'ERROR', error_code: 'PRIVATE_PROVIDER_RESPONSE',
      analysis: { status: 'ERROR', fields: [{ name: 'Do not render', value: 'PRIVATE_VALUE' }] },
    }));
    const client = createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher);

    await expect(client.load()).resolves.toEqual({ status: 'error', fields: [] });
  });

  it('rejects a response bound to another profile or application', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...base, ...completedResponse, application_id: 'other-application',
    }));

    await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load())
      .resolves.toEqual({ status: 'error', fields: [] });
  });

  it('renders a loading state while mounting and never starts inspection automatically', () => {
    const inspect = vi.fn();
    const fetcher = vi.fn<typeof fetch>();
    const html = renderToStaticMarkup(<ApplicationFormReviewConnected
      profileId={base.profile_id} applicationId={base.application_id} onInspect={inspect} fetcher={fetcher}
    />);

    expect(html).toContain('Loading saved form review');
    expect(fetcher).not.toHaveBeenCalled();
    expect(inspect).not.toHaveBeenCalled();
  });

  it('drops an in-flight response after its profile or application scope becomes stale', async () => {
    let scopeCurrent = true;
    let resolveResponse!: (response: Response) => void;
    const fetcher = vi.fn<typeof fetch>().mockReturnValue(new Promise<Response>((resolve) => {
      resolveResponse = resolve;
    }));
    const client = createApplicationFormReviewClient(
      base.profile_id, base.application_id, fetcher, () => scopeCurrent,
    );
    const pending = client.load();
    scopeCurrent = false;
    resolveResponse(Response.json({ ...base, ...completedResponse }));

    await expect(pending).resolves.toEqual({ status: 'error', fields: [] });
  });

  it('hides network and HTTP failure details behind the fixed error state', async () => {
    const rejected = vi.fn<typeof fetch>().mockRejectedValue(new Error('PRIVATE_KEY network failure'));
    const failedResponse = vi.fn<typeof fetch>().mockResolvedValue(new Response('PRIVATE_RESPONSE', { status: 503 }));

    await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, rejected).load())
      .resolves.toEqual({ status: 'error', fields: [] });
    await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, failedResponse).load())
      .resolves.toEqual({ status: 'error', fields: [] });
  });


  it('marks analysis UNKNOWN unavailable and discards any supplied fields', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...base, ...completedResponse,
      analysis: { status: 'UNKNOWN', fields: [
        { id: 'private-field', name: 'Private applicant data', type: 'text', required: true,
          review_reason: null, value: 'PRIVATE_VALUE' },
      ] },
    }));

    await expect(createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load())
      .resolves.toEqual({ status: 'unavailable', fields: [] });
  });

  it('requests a fresh latest result after the selected profile and application change', async () => {
    const nextProfile = 'profile-b';
    const nextApplication = 'application-c';
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({
        ...base, request_id: null, state: 'IDLE', error_code: null, analysis: null,
      }))
      .mockResolvedValueOnce(Response.json({
        profile_id: nextProfile, application_id: nextApplication, ...completedResponse,
      }));

    const previous = await createApplicationFormReviewClient(base.profile_id, base.application_id, fetcher).load();
    const current = await createApplicationFormReviewClient(nextProfile, nextApplication, fetcher).load();

    expect(previous).toEqual({ status: 'idle', fields: [] });
    expect(current.status).toBe('completed');
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      '/profiles/profile%2Fa/applications/application%26b/form-reviews/latest',
      '/profiles/profile-b/applications/application-c/form-reviews/latest',
    ]);
  });
});
