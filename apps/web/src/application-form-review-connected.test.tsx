import { describe, expect, it, vi } from 'vitest';
import { inspectApplicationForm } from './application-form-review-connected';

describe('connected application form review', () => {
  it('queues only a bounded read-only request and returns sanitized persisted metadata', async () => {
    const api = vi.fn()
      .mockResolvedValueOnce({ request_id: 'request-1', operation_id: 'opaque', state: 'QUEUED' })
      .mockResolvedValueOnce({
        request_id: 'request-1', state: 'COMPLETED', error_code: null,
        analysis: { status: 'REVIEW_REQUIRED', fields: [{
          id: 'email', name: 'Email address', type: 'email', required: true, review_reason: 'SENSITIVE_FIELD',
          value: 'private@example.test',
        }] },
      });

    const result = await inspectApplicationForm(api, 'profile 1', 'application/1', 'request-1');

    expect(result).toEqual({ status: 'completed', fields: [{
      name: 'Email address', type: 'email', required: true, reviewRequired: true,
    }] });
    expect(api).toHaveBeenCalledTimes(2);
    expect(api.mock.calls[0][0]).toContain('/profiles/profile%201/applications/application%2F1/form-reviews');
    expect(api.mock.calls[0][1]?.method).toBe('POST');
    expect(api.mock.calls[1][0]).toContain('/form-reviews/request-1');
    expect(api.mock.calls[1][1]).toBeUndefined();
  });

  it('does not expose provider failure text and treats an unknown analysis as unavailable', async () => {
    const failedApi = vi.fn().mockRejectedValue(new Error('PRIVATE_PROVIDER_BODY'));
    expect(await inspectApplicationForm(failedApi, 'p', 'a', 'r'))
      .toEqual({ status: 'error', fields: [] });
    expect(JSON.stringify(await inspectApplicationForm(failedApi, 'p', 'a', 'r')))
      .not.toContain('PRIVATE_PROVIDER_BODY');

    const unknownApi = vi.fn()
      .mockResolvedValueOnce({ request_id: 'r', operation_id: 'o', state: 'QUEUED' })
      .mockResolvedValueOnce({ request_id: 'r', state: 'COMPLETED', analysis: { status: 'UNKNOWN', fields: [] } });
    expect(await inspectApplicationForm(unknownApi, 'p', 'a', 'r'))
      .toEqual({ status: 'unavailable', fields: [] });
  });
});
