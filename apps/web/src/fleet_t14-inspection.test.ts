import { describe, expect, it, vi } from 'vitest';
import { inspectApplicationForm, parseApplicationFormReviewSnapshot } from './application-form-inspection-client';

const completed = {
  request_id: 'review-1', state: 'COMPLETED', error_code: null,
  analysis: { status: 'ANALYZED', fields: [
    { name: 'Email', type: 'email', required: true, review_reason: null },
  ] },
};

describe('fleet_t14 inspection identity and fail-closed metadata', () => {
  it('rejects an acknowledgement for a different request before polling', async () => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'other', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue(completed);
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
    expect(request).toHaveBeenCalledTimes(1);
  });

  it('rejects a completed response for a different request', async () => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ ...completed, request_id: 'other' });
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
  });

  it('does not interpret malformed field metadata as inspected successfully', async () => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ ...completed, analysis: { status: 'ANALYZED', fields: [null] } });
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
  });

  it.each([
    { profile_id: 'other-profile' },
    { application_id: 'other-application' },
    { error_code: 'FORM_REVIEW_FAILED' },
    { state: 'UNRECOGNIZED' },
  ])('rejects inconsistent response metadata %j', async (overrides) => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ ...completed, ...overrides });
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
  });

  it('bounds untrusted field collections instead of displaying oversized results', async () => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ ...completed, analysis: {
        status: 'ANALYZED', fields: Array.from({ length: 201 }, () => completed.analysis.fields[0]),
      } });
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}))
      .resolves.toEqual({ status: 'error', fields: [] });
  });

  it('excludes raw field values from the completed metadata snapshot', async () => {
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ ...completed, analysis: { status: 'ANALYZED', fields: [
        { ...completed.analysis.fields[0], value: 'synthetic-private-value' },
      ] } });
    const result = await inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {});
    expect(result).toEqual({ status: 'completed', fields: [
      { name: 'Email', type: 'email', required: true, reviewRequired: false },
    ] });
    expect(JSON.stringify(result)).not.toContain('synthetic-private-value');
  });

  it('stops polling after selection becomes stale during a delay', async () => {
    let current = true;
    const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
      .mockResolvedValue({ request_id: 'review-1', state: 'RUNNING', error_code: null, analysis: null });
    const result = await inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {
      current = false;
    }, { isScopeCurrent: () => current });
    expect(result).toEqual({ status: 'error', fields: [] });
    expect(request).toHaveBeenCalledTimes(2);
  });

  it('rejects an echoed operation or route identity that differs from the selected review', async () => {
    for (const overrides of [{ operation_id: 'other' }, { route_id: 'other' }]) {
      const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
        .mockResolvedValue({ ...completed, ...overrides });
      await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}, { routeId: 'route' }))
        .resolves.toEqual({ status: 'error', fields: [] });
    }
  });

  it('never queues an inspection after cancellation', async () => {
    const controller = new AbortController();
    controller.abort();
    const request = vi.fn();
    await expect(inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}, { signal: controller.signal }))
      .resolves.toEqual({ status: 'error', fields: [] });
    expect(request).not.toHaveBeenCalled();
  });

  it('uses the same strict metadata boundary for durable latest readback', () => {
    expect(parseApplicationFormReviewSnapshot(completed, 'profile', 'application')).toEqual({
      status: 'completed', fields: [{ name: 'Email', type: 'email', required: true, reviewRequired: false }],
    });
    for (const overrides of [{ request_id: null }, { analysis: { status: 'ANALYZED', fields: [null] } },
      { profile_id: 'other' }, { error_code: 'FORM_REVIEW_FAILED' }]) {
      expect(parseApplicationFormReviewSnapshot({ ...completed, ...overrides }, 'profile', 'application'))
        .toEqual({ status: 'error', fields: [] });
    }
  });
});
