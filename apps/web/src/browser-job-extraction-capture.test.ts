import { describe, expect, it, vi } from 'vitest';
import {
  buildBrowserJobExtractionCapturePayload,
  createBrowserJobExtractionCaptureClient,
  type BrowserJobExtractionManualDetails,
} from './browser-job-extraction-capture';
import type {
  BrowserJobExtractionConfirmation,
} from './browser-job-extraction';

const profileId = 'profile-1';
const fields: BrowserJobExtractionConfirmation['preview']['fields'] = {
  title: { value: 'Systems Administrator', source: 'JOBPOSTING_SCHEMA', confidence: 0.98, review_required: false },
  company: { value: 'Example Systems', source: 'PROVIDER_FIELDS', confidence: 0.92, review_required: false },
  description: { value: 'Manage local infrastructure.', source: 'JOBPOSTING_SCHEMA', confidence: 0.9, review_required: false },
  location: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
  employment_type: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
  date_posted: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
  valid_through: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
  source_job_id: { value: 'role-42', source: 'PROVIDER_FIELDS', confidence: 0.86, review_required: false },
  listing_url: { value: 'https://jobs.example.test/role-42', source: 'VISIBLE_TEXT', confidence: 0.42, review_required: true },
  application_url: { value: 'https://jobs.example.test/apply/42', source: 'JOBPOSTING_SCHEMA', confidence: 0.84, review_required: false },
};

function confirmation(overrides: Partial<BrowserJobExtractionConfirmation> = {}): BrowserJobExtractionConfirmation {
  return {
    preview: {
      profile_id: profileId,
      job_id: 'preview-job-1',
      raw_capture_ref: 'local-capture-ref',
      fields,
      unknown_fields: ['location', 'employment_type', 'date_posted', 'valid_through'],
      review_required: ['listing_url'],
    },
    corrections: { title: 'Senior Systems Administrator', listing_url: 'https://jobs.example.test/corrected' },
    reviewed_fields: ['title', 'company', 'listing_url', 'application_url'],
    ...overrides,
  };
}

const manual: BrowserJobExtractionManualDetails = {
  description: '',
  location: '',
  discovery_origin: 'LinkedIn',
  observed_hops: [],
};

function sourceCandidate(jobId = 'saved-job-1', selectedProfile = profileId) {
  return {
    id: 'source-candidate-1', profile_id: selectedProfile,
    observations: [{ id: 'observation-1', job_id: jobId }],
  };
}

function durableJob(id = 'saved-job-1') {
  return {
    id, title: 'Senior Systems Administrator', company: 'Example Systems',
    description: 'Manage local infrastructure.', location: 'Unknown',
  };
}

describe('explicit browser extraction capture client', () => {
  it('submits corrected confirmed values with sanitized extraction provenance and reads back durable records', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ job: { id: 'saved-job-1', title: 'Senior Systems Administrator' }, created: true })
      .mockResolvedValueOnce(durableJob())
      .mockResolvedValueOnce({ candidates: [sourceCandidate()] });
    const client = createBrowserJobExtractionCaptureClient(profileId, request, () => 'stable-request-id');
    expect(request).not.toHaveBeenCalled();

    const result = await client.capture(confirmation(), manual);

    expect(request).toHaveBeenNthCalledWith(1, '/jobs/manual', expect.objectContaining({ method: 'POST' }));
    const body = JSON.parse(request.mock.calls[0][1].body);
    expect(body).toMatchObject({
      profile_id: profileId,
      request_id: 'stable-request-id',
      title: 'Senior Systems Administrator',
      company: 'Example Systems',
      description: 'Manage local infrastructure.',
      location: 'Unknown',
      reference_url: 'https://jobs.example.test/corrected',
      application_url: 'https://jobs.example.test/apply/42',
      discovery_origin: 'LinkedIn',
      observed_hops: ['UNKNOWN'],
      source_details: { extraction_fields: expect.any(Object) },
    });
    expect(body.source_details.extraction_fields.title).toEqual({
      value: 'Senior Systems Administrator', source: 'JOBPOSTING_SCHEMA', confidence: 0.98, reviewed: true,
    });
    expect(body.source_details.extraction_fields.listing_url).toEqual({
      value: 'https://jobs.example.test/corrected', source: 'VISIBLE_TEXT', confidence: 0.42, reviewed: true,
    });
    expect(body.source_details.extraction_fields.location).toEqual({
      value: null, source: 'UNKNOWN', confidence: 0, reviewed: false,
    });
    expect(body).not.toHaveProperty('raw_capture_ref');
    expect(JSON.stringify(body)).not.toContain('local-capture-ref');
    expect(request.mock.calls[1][0]).toBe('/jobs/saved-job-1?profile_id=profile-1');
    expect(request.mock.calls[2][0]).toBe('/source-candidates?profile_id=profile-1');
    expect(result).toMatchObject({ job: { id: 'saved-job-1' }, created: true, sourceCandidate: { id: 'source-candidate-1' } });
  });

  it('requires reviewed low-confidence fields, identity, a route, and description without inventing unknown values', () => {
    const unreviewed = confirmation({ reviewed_fields: [] });
    expect(() => buildBrowserJobExtractionCapturePayload(profileId, unreviewed, manual, 'validate-id'))
      .toThrow(/review each low-confidence field/i);

    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      corrections: { title: null, company: null },
    }), manual, 'validate-id')).toThrow(/title and company/i);

    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      preview: { ...confirmation().preview, unknown_fields: [...confirmation().preview.unknown_fields, 'description'], fields: { ...fields, description: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false } } },
    }), manual, 'validate-id')).toThrow(/enter the job description manually/i);

    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      corrections: { listing_url: null, application_url: null },
    }), manual, 'validate-id')).toThrow(/job reference or application URL/i);

    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      preview: { ...confirmation().preview, fields: { ...fields, application_url: { ...fields.application_url, value: 'javascript:alert(1)' } } },
    }), manual, 'validate-id')).toThrow(/safe HTTP or HTTPS/i);
  });

  it('keeps manually supplied description separate from unknown extraction provenance', () => {
    const preview = confirmation({
      preview: { ...confirmation().preview, unknown_fields: [...confirmation().preview.unknown_fields, 'description'], fields: { ...fields, description: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false } } },
    });
    const result = buildBrowserJobExtractionCapturePayload(profileId, preview, {
      description: 'Description typed by the user.',
      location: 'Tulsa, OK',
      discovery_origin: null,
      observed_hops: [],
    }, 'manual-id');

    expect(result.payload.description).toBe('Description typed by the user.');
    expect(result.payload.location).toBe('Tulsa, OK');
    expect(result.payload.source_details.extraction_fields.description).toEqual({
      value: null, source: 'UNKNOWN', confidence: 0, reviewed: false,
    });
    expect(result.notices).toContain('The description was entered manually because extraction returned unknown.');
    expect(result.notices).toContain('Extraction remains unknown for: location, employment type, date posted, valid through, description.');
    expect(result.payload.observed_hops).toEqual(['UNKNOWN']);
  });

  it('requires an explicit selected-profile match and never substitutes another profile', () => {
    expect(() => buildBrowserJobExtractionCapturePayload('other-profile', confirmation(), manual, 'validate-id'))
      .toThrow(/does not match the selected profile/i);
  });

  it('retains the request ID when POST or durable source-candidate readback fails, then retries idempotently', async () => {
    const request = vi.fn()
      .mockResolvedValueOnce({ job: { id: 'saved-job-1' }, created: true })
      .mockResolvedValueOnce(durableJob())
      .mockRejectedValueOnce(new Error('Temporary source readback failure.'))
      .mockResolvedValueOnce({ job: { id: 'saved-job-1' }, created: false })
      .mockResolvedValueOnce(durableJob())
      .mockResolvedValueOnce({ candidates: [sourceCandidate()] });
    const requestIdFactory = vi.fn(() => 'retry-request-id');
    const client = createBrowserJobExtractionCaptureClient(profileId, request, requestIdFactory);

    await expect(client.capture(confirmation(), manual)).rejects.toThrow('Temporary source readback failure.');
    const result = await client.capture(confirmation(), manual);

    expect(requestIdFactory).toHaveBeenCalledOnce();
    expect(request.mock.calls[0][0]).toBe('/jobs/manual');
    expect(JSON.parse(request.mock.calls[0][1].body).request_id).toBe('retry-request-id');
    expect(JSON.parse(request.mock.calls[3][1].body).request_id).toBe('retry-request-id');
    expect(result.created).toBe(false);
  });

  it('fails closed if readback returns another profile or no candidate linked to the created job', async () => {
    const wrongProfile = createSourceCandidateReviewClientForTest([
      { job: { id: 'saved-job-1' }, created: true },
      durableJob(),
      { candidates: [sourceCandidate('saved-job-1', 'other-profile')] },
    ]);
    await expect(wrongProfile.capture(confirmation(), manual)).rejects.toThrow(/profile scope/i);

    const missingCandidate = createSourceCandidateReviewClientForTest([
      { job: { id: 'saved-job-1' }, created: true },
      durableJob(),
      { candidates: [sourceCandidate('another-job')] },
    ]);
    await expect(missingCandidate.capture(confirmation(), manual)).rejects.toThrow(/durable source candidate readback/i);
  });

  it('requires explicit review for identity and every supplied route', () => {
    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      reviewed_fields: ['company', 'listing_url', 'application_url'],
    }), manual, 'validate-id')).toThrow(/review title/i);
    expect(() => buildBrowserJobExtractionCapturePayload(profileId, confirmation({
      reviewed_fields: ['title', 'company', 'listing_url'],
    }), manual, 'validate-id')).toThrow(/review each job reference or application URL/i);
  });

  it('rejects oversized UTF-8 payload before making a request', async () => {
    const request = vi.fn();
    const client = createBrowserJobExtractionCaptureClient(profileId, request, () => 'large-id');
    const largeDescription = '界'.repeat(30_000);
    const largeConfirmation = confirmation({
      preview: {
        ...confirmation().preview,
        fields: { ...fields, description: { ...fields.description, value: largeDescription } },
      },
    });
    await expect(client.capture(largeConfirmation, manual)).rejects.toThrow(/100 KiB limit/i);
    expect(request).not.toHaveBeenCalled();
  });
});

function createSourceCandidateReviewClientForTest(responses: unknown[]) {
  const request = vi.fn();
  for (const response of responses) request.mockResolvedValueOnce(response);
  return createBrowserJobExtractionCaptureClient(profileId, request, () => 'test-id');
}
