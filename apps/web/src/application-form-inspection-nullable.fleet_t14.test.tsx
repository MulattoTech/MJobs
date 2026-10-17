import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationFormReview } from './application-form-review';
import { createApplicationFormReviewClient, inspectApplicationForm } from './application-form-review-connected';

const field = { id: 'email', name: null, type: 'email', required: true, review_reason: null };
const error = { status: 'error', fields: [] };

async function inspectAndRead(fields: unknown[]) {
  const result = { request_id: 'review-1', state: 'COMPLETED', error_code: null,
    analysis: { status: 'ANALYZED', fields } };
  const request = vi.fn().mockResolvedValueOnce({ request_id: 'review-1', operation_id: 'op', state: 'QUEUED' })
    .mockResolvedValue(result);
  return [
    await inspectApplicationForm(request, 'profile', 'application', 'review-1', async () => {}),
    await createApplicationFormReviewClient('profile', 'application',
      vi.fn<typeof fetch>().mockResolvedValue(Response.json(result))).load(),
  ];
}

describe('fleet T14 nullable worker field metadata', () => {
  it.each([
    { id: 'email', name: null, display: 'email' },
    { id: 'email', name: 'Email', display: 'Email' },
    { id: null, name: 'Email', display: 'Email' },
    { id: null, name: null, display: 'Unnamed field' },
    { id: 'x'.repeat(100), name: null, display: 'x'.repeat(100) },
  ])('renders worker-valid nullable name/id through inspection and saved readback: %j', async ({ id, name, display }) => {
    const snapshots = await inspectAndRead([{ ...field, id, name, value: 'PRIVATE_SYNTHETIC_VALUE' }]);
    for (const snapshot of snapshots) {
      expect(snapshot).toEqual({ status: 'completed', fields: [
        { name: display, type: 'email', required: true, reviewRequired: false },
      ] });
      expect(JSON.stringify(snapshot)).not.toContain('PRIVATE_SYNTHETIC_VALUE');
      const html = renderToStaticMarkup(<ApplicationFormReview {...snapshot} onInspect={() => {}} />);
      expect(html).toContain('Inspected form fields');
      expect(html).toContain(`<strong>${display.slice(0, 80)}</strong>`);
      expect(html).not.toContain('PRIVATE_SYNTHETIC_VALUE');
    }
  });

  it.each([
    { name: undefined }, { name: 42 }, { name: {} }, { name: 'x'.repeat(501) },
    { id: undefined }, { id: 42 }, { id: {} }, { id: '' }, { id: 'x'.repeat(101) },
    { id: 'private@example.test' }, { id: 'email\n' },
    { type: 1 }, { required: 'true' }, { review_reason: {} },
  ])('still rejects malformed metadata in inspection and readback: %j', async overrides => {
    for (const snapshot of await inspectAndRead([{ ...field, ...overrides }])) expect(snapshot).toEqual(error);
  });

  it('retains the collection bound for nullable fields', async () => {
    for (const snapshot of await inspectAndRead(Array.from({ length: 201 }, () => field))) expect(snapshot).toEqual(error);
  });
});
