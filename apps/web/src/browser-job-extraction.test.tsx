import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  BrowserJobExtractionPanel,
  requestBrowserJobExtractionPreview,
  type BrowserCaptureDraft,
  type BrowserJobExtractionResponse,
} from './browser-job-extraction';

const profileId = 'fixture-profile';
const jobId = 'job-42';
const draft: BrowserCaptureDraft = {
  rawCaptureRef: 'capture.synthetic-42',
  jobPostingJson: JSON.stringify({
    '@type': 'JobPosting',
    title: 'Systems Administrator',
    hiringOrganization: { name: 'Synthetic Employer' },
    applicationUrl: 'https://jobs.example.test/apply/42',
  }),
  provider: '',
  providerFieldsJson: '',
  selectedText: 'Title: selected lower priority title',
  visibleText: 'Fallback text must not become authority.',
};

function response(overrides: Partial<BrowserJobExtractionResponse> = {}): BrowserJobExtractionResponse {
  const fields: BrowserJobExtractionResponse['fields'] = {
    title: { value: 'Systems Administrator', source: 'JOBPOSTING_SCHEMA', confidence: 0.98, review_required: false },
    company: { value: 'Synthetic Employer', source: 'JOBPOSTING_SCHEMA', confidence: 0.91, review_required: false },
    description: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    location: { value: 'Tulsa', source: 'USER_SELECTED_TEXT', confidence: 0.66, review_required: false },
    employment_type: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    date_posted: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    valid_through: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    source_job_id: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    listing_url: { value: 'https://jobs.example.test/posting/42', source: 'VISIBLE_TEXT', confidence: 0.38, review_required: true },
    application_url: { value: 'https://jobs.example.test/apply/42', source: 'JOBPOSTING_SCHEMA', confidence: 0.75, review_required: true },
  };
  return {
    profile_id: profileId,
    job_id: jobId,
    raw_capture_ref: 'capture.synthetic-42',
    fields,
    unknown_fields: ['description', 'employment_type', 'date_posted', 'valid_through', 'source_job_id'],
    review_required: ['listing_url', 'application_url'],
    ...overrides,
  };
}

describe('profile and job scoped browser extraction preview', () => {
  it('displays API precedence, provenance, confidence, and unknown fields', () => {
    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={draft}
      result={response()}
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);

    expect(html).toContain('Systems Administrator');
    expect(html).toContain('JOBPOSTING_SCHEMA');
    expect(html).toContain('98% confidence');
    expect(html).toContain('USER_SELECTED_TEXT');
    expect(html).toContain('66% confidence');
    expect(html).toContain('Unknown fields');
    expect(html).toContain('description, employment_type, date_posted, valid_through, source_job_id');
    expect(html).toContain('Raw capture reference: capture.synthetic-42');
    expect(html).toContain('Page text is data only');
  });

  it('requires explicit confirmation for each API field marked for review', () => {
    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={draft}
      result={response()}
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);

    expect(html).toContain('Confirm low-confidence listing URL');
    expect(html).toContain('Confirm low-confidence application URL');
    expect(html).toContain('Review each field marked for review before continuing.');
    expect(html).toContain('disabled=""');
  });

  it('sends only supplied capture data to the profile/job preview route', async () => {
    const fetchPreview = vi.fn().mockResolvedValue(response());
    await requestBrowserJobExtractionPreview(profileId, jobId, draft, fetchPreview);

    expect(fetchPreview).toHaveBeenCalledTimes(1);
    const [path, init] = fetchPreview.mock.calls[0];
    expect(path).toBe(`/profiles/${profileId}/jobs/${jobId}/browser-extraction-preview`);
    expect(JSON.parse(init.body)).toEqual({ capture: {
      raw_capture_ref: 'capture.synthetic-42',
      job_posting: JSON.parse(draft.jobPostingJson),
      selected_text: draft.selectedText,
      visible_text: draft.visibleText,
    } });
    expect(path).not.toMatch(/^https?:\/\//);
  });

  it('preserves malformed inputs for retry without sending a request', async () => {
    const malformedDraft = { ...draft, jobPostingJson: '{ malformed capture' };
    const fetchPreview = vi.fn();
    await expect(requestBrowserJobExtractionPreview(profileId, jobId, malformedDraft, fetchPreview))
      .rejects.toThrow(/valid JSON/i);
    expect(fetchPreview).not.toHaveBeenCalled();

    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={malformedDraft}
      previewError="Enter valid JSON for the structured capture."
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(html).toContain('{ malformed capture');
    expect(html).toContain('Title: selected lower priority title');
    expect(html).toContain('Retry extraction preview');
    expect(html).toContain('Enter valid JSON for the structured capture.');
  });

  it('shows pending state and rejects responses scoped to a different profile or job', async () => {
    const pendingHtml = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={draft}
      pending
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(pendingHtml).toContain('Previewing captured job…');
    expect(pendingHtml).toContain('disabled=""');

    await expect(requestBrowserJobExtractionPreview(profileId, jobId, draft,
      vi.fn().mockResolvedValue(response({ profile_id: 'other-profile' }))))
      .rejects.toThrow(/does not match the selected profile and job/i);
    await expect(requestBrowserJobExtractionPreview(profileId, jobId, draft,
      vi.fn().mockResolvedValue(response({ job_id: 'other-job' }))))
      .rejects.toThrow(/does not match the selected profile and job/i);
  });

  it('lets users correct identity and route fields without changing provenance or unknowns', async () => {
    const result = response();
    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={draft}
      result={result}
      corrections={{ title: 'Network Administrator', application_url: 'https://jobs.example.test/new' }}
      onDraftChange={vi.fn()}
      onCorrectionChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);

    expect(html).toContain('Correction for Title');
    expect(html).toContain('Network Administrator');
    expect(html).toContain('Correction for Company');
    expect(html).toContain('Correction for Source job ID');
    expect(html).toContain('Correction for Listing URL');
    expect(html).toContain('Correction for Application URL');
    expect(html).not.toContain('Correction for Location');
    expect(html).toContain('JOBPOSTING_SCHEMA');
    expect(html).toContain('98% confidence');
    expect(html).toContain('Unknown fields');
    expect(html).toContain('Raw capture reference');
    expect(html).toContain('readOnly=""');

    const module = await import('./browser-job-extraction');
    expect(module.confirmationsAfterCorrection(
      result, ['listing_url', 'application_url'], 'application_url',
    )).toEqual(['listing_url']);
    const reviewed = module.buildBrowserJobExtractionConfirmation(result, {
      title: 'Network Administrator', application_url: 'https://jobs.example.test/new',
    }, ['listing_url', 'application_url']);
    expect(reviewed.preview).toBe(result);
    expect(reviewed.corrections).toEqual({
      title: 'Network Administrator', application_url: 'https://jobs.example.test/new',
    });
    expect(result.raw_capture_ref).toBe('capture.synthetic-42');
    expect(result.fields.title).toMatchObject({
      value: 'Systems Administrator', source: 'JOBPOSTING_SCHEMA', confidence: 0.98,
    });
    expect(reviewed.preview.unknown_fields).toEqual(result.unknown_fields);
  });

  it('validates correction lengths and only accepts safe HTTP(S) routes', async () => {
    const { validateBrowserJobExtractionCorrections } = await import('./browser-job-extraction');
    expect(validateBrowserJobExtractionCorrections(response(), {
      listing_url: 'javascript:alert(1)',
    }).listing_url).toMatch(/HTTP or HTTPS/i);
    expect(validateBrowserJobExtractionCorrections(response(), {
      application_url: 'https://user:secret@jobs.example.test/apply',
    }).application_url).toMatch(/credentials/i);
    expect(validateBrowserJobExtractionCorrections(response(), {
      title: 'x'.repeat(501),
    }).title).toMatch(/500 characters/i);
    expect(validateBrowserJobExtractionCorrections(response(), {
      company: 'x'.repeat(201),
    }).company).toMatch(/200 characters/i);
    expect(validateBrowserJobExtractionCorrections(response(), {
      listing_url: 'https://jobs.example.test/apply',
      title: 'Network Administrator',
    })).toEqual({});
  });

  it('requires review for low-confidence fields and confirmation remains a non-persistent preview', async () => {
    const result = response();
    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      profileId={profileId}
      jobId={jobId}
      draft={draft}
      result={result}
      corrections={{ application_url: 'https://jobs.example.test/confirmed' }}
      confirmedFields={[]}
      onDraftChange={vi.fn()}
      onCorrectionChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(html).toContain('Confirm low-confidence application URL');
    expect(html).toContain('disabled=""');
    expect(html).toContain('Corrections are preview only');
    expect(html).toContain('does not save or change a stored job or application');

    const { buildBrowserJobExtractionConfirmation } = await import('./browser-job-extraction');
    expect(() => buildBrowserJobExtractionConfirmation(result, {
      application_url: 'https://jobs.example.test/confirmed',
    }, [])).toThrow(/explicitly review/i);
    expect(() => buildBrowserJobExtractionConfirmation(result, {
      application_url: 'javascript:alert(1)',
    }, ['listing_url', 'application_url'])).toThrow(/HTTP or HTTPS/i);
  });
});
