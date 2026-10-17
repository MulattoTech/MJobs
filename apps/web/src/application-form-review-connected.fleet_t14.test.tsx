import { describe, expect, it, vi } from 'vitest';
import {
  createApplicationFormReviewClient,
  inspectApplicationForm,
} from './application-form-review-connected';

type ReviewRequest = <T>(path: string, options?: RequestInit) => Promise<T>;

const profileId = 'synthetic/profile';
const applicationId = 'fixture&application';
const operationPath = `/profiles/${encodeURIComponent(profileId)}/applications/${encodeURIComponent(applicationId)}/form-reviews`;
const sanitizedResult = {
  profile_id: profileId,
  application_id: applicationId,
  request_id: 't14-request',
  state: 'COMPLETED',
  error_code: null,
  analysis: {
    status: 'REVIEW_REQUIRED',
    fields: [{
      id: 'work-history',
      name: 'Work history',
      type: 'textarea',
      required: true,
      review_reason: 'Confirm this answer manually',
      value: 'private synthetic applicant response',
    }],
  },
};

describe('T14 connected application form review acceptance', () => {
  it('explicitly requests review, reads the durable result, then hydrates the latest saved metadata', async () => {
    let durableResult: typeof sanitizedResult | null = null;
    const calls: Array<[string, RequestInit?]> = [];
    const request: ReviewRequest = async <T,>(path: string, options?: RequestInit): Promise<T> => {
      calls.push([path, options]);
      if (options?.method === 'POST') {
        expect(path).toBe(operationPath);
        expect(JSON.parse(String(options.body))).toEqual({ request_id: 't14-request' });
        durableResult = sanitizedResult;
        return {
          profile_id: profileId,
          application_id: applicationId,
          request_id: 't14-request',
          state: 'QUEUED',
          operation_id: 'fixture-operation',
          route_id: 'fixture-route',
        } as T;
      }
      expect(path).toBe(`${operationPath}/t14-request`);
      return durableResult as T;
    };

    await expect(inspectApplicationForm(request, profileId, applicationId, 't14-request', async () => {}))
      .resolves.toEqual({
        status: 'completed',
        fields: [{ name: 'Work history', type: 'textarea', required: true, reviewRequired: true }],
      });
    expect(calls).toHaveLength(2);
    expect(calls[0]?.[1]).toMatchObject({ method: 'POST' });

    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(durableResult));
    const latest = await createApplicationFormReviewClient(profileId, applicationId, fetcher).load();
    expect(latest).toEqual({
      status: 'completed',
      fields: [{ name: 'Work history', type: 'textarea', required: true, reviewRequired: true }],
    });
    expect(JSON.stringify(latest)).not.toContain('private synthetic applicant response');
    expect(fetcher).toHaveBeenCalledWith(
      `${operationPath}/latest`,
      expect.objectContaining({ method: 'GET', cache: 'no-store', credentials: 'same-origin' }),
    );
  });

  it('fails closed when operation polling is interrupted and never turns interruption into submission', async () => {
    const calls: Array<[string, RequestInit?]> = [];
    const request: ReviewRequest = async <T,>(_path: string, options?: RequestInit): Promise<T> => {
      calls.push([_path, options]);
      if (options?.method === 'POST') {
        return {
          profile_id: profileId,
          application_id: applicationId,
          request_id: 't14-interrupted',
          state: 'QUEUED',
          operation_id: 'fixture-operation',
        } as T;
      }
      return { request_id: 't14-interrupted', state: 'RUNNING', error_code: null } as T;
    };

    await expect(inspectApplicationForm(request, profileId, applicationId, 't14-interrupted', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
    expect(calls).toHaveLength(31);
    expect(calls.filter(([, options]) => options?.method === 'POST')).toHaveLength(1);
    expect(calls.every(([path]) => !path.toLowerCase().includes('submit'))).toBe(true);
  });

  it('rejects mismatched latest-readback scope without displaying the returned analysis', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...sanitizedResult,
      profile_id: 'another-profile',
    }));

    await expect(createApplicationFormReviewClient(profileId, applicationId, fetcher).load())
      .resolves.toEqual({ status: 'error', fields: [] });
  });
});
