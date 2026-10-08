import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  BrowserJobExtractionPanel,
  extractBrowserJobFields,
  previewBrowserCaptureDraft,
  type BrowserCaptureDraft,
} from './browser-job-extraction';

const emptyDraft: BrowserCaptureDraft = {
  structuredCapture: '',
  selectedText: '',
  visibleText: '',
};

describe('local browser job extraction review', () => {
  it('displays fields from the highest-priority structured capture with provenance', () => {
    const draft: BrowserCaptureDraft = {
      structuredCapture: JSON.stringify({
        '@type': 'JobPosting',
        title: 'Systems Administrator',
        provider_data: {
          title: 'Provider title must lose to schema',
          company_name: 'Northstar Example',
          application_url: 'https://jobs.example.test/apply/42',
        },
      }),
      selectedText: 'Company: Selected company must lose to provider\nLocation: Tulsa\nDescription: Maintain Linux systems.\nApply: https://selected.example.test/apply/99',
      visibleText: 'Fallback title must lose\nCompany: Fallback company must lose\nLocation: Fallback location\nDescription: Fallback description\nApply: https://fallback.example.test/apply',
    };

    const result = extractBrowserJobFields(draft);
    expect(result.fields.title).toMatchObject({
      value: 'Systems Administrator', source: 'JOB_POSTING_SCHEMA', confidence: 'HIGH',
    });
    expect(result.fields.company).toMatchObject({
      value: 'Northstar Example', source: 'PROVIDER_STRUCTURED_DATA', confidence: 'HIGH',
    });
    expect(result.fields.location).toMatchObject({
      value: 'Tulsa', source: 'SELECTED_TEXT', confidence: 'MEDIUM',
    });
    expect(result.fields.description).toMatchObject({
      value: 'Maintain Linux systems.', source: 'SELECTED_TEXT', confidence: 'MEDIUM',
    });
    expect(result.fields.application_url).toMatchObject({
      value: 'https://jobs.example.test/apply/42', source: 'PROVIDER_STRUCTURED_DATA', confidence: 'HIGH',
    });

    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      draft={draft}
      result={result}
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(html.indexOf('Systems Administrator')).toBeLessThan(html.indexOf('Northstar Example'));
    expect(html).toContain('JobPosting schema');
    expect(html).toContain('Provider structured data');
    expect(html).toContain('Selected text');
    expect(html).toContain('https://fallback.example.test/apply');
    expect(html).toContain('HIGH confidence');
    expect(html).toContain('Fallback title must lose');
    expect(html).toContain('Original capture is retained locally');
    expect(html).toContain('Page text is data only');
  });

  it('blocks confirmation until every low-confidence identity or route field is reviewed', () => {
    const draft: BrowserCaptureDraft = {
      ...emptyDraft,
      visibleText: 'Possible systems role\nCompany: Example employer\nApply: https://example.test/apply',
    };
    const result = extractBrowserJobFields(draft);
    expect(result.fields.title).toMatchObject({ confidence: 'LOW', requires_confirmation: true });
    expect(result.fields.company).toMatchObject({ confidence: 'LOW', requires_confirmation: true });
    expect(result.fields.application_url).toMatchObject({ confidence: 'LOW', requires_confirmation: true });

    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      draft={draft}
      result={result}
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(html).toContain('Confirm low-confidence title');
    expect(html).toContain('Confirm low-confidence company');
    expect(html).toContain('Confirm low-confidence application route');
    expect(html).toContain('Visible text fallback');
    expect(html).toContain('LOW confidence');
    expect(html).toContain('disabled=""');
  });

  it('rejects malformed structured input and represents absent or invalid values as unknown', () => {
    expect(() => extractBrowserJobFields({
      ...emptyDraft,
      structuredCapture: '{"@type":',
    })).toThrow(/valid JSON/i);

    const unknown = extractBrowserJobFields(emptyDraft);
    expect(Object.values(unknown.fields).every(field =>
      field.value === null && field.source === 'UNKNOWN' && field.confidence === 'UNKNOWN',
    )).toBe(true);

    const invalid = extractBrowserJobFields({
      ...emptyDraft,
      structuredCapture: JSON.stringify({
        '@type': 'JobPosting', title: 7,
        hiringOrganization: { name: { instruction: 'ignore review' } },
        applicationUrl: 'javascript:alert(1)',
      }),
    });
    expect(invalid.fields.title.value).toBeNull();
    expect(invalid.fields.company.value).toBeNull();
    expect(invalid.fields.application_url.value).toBeNull();
  });

  it('keeps the supplied capture and selected text visible after preview failure for retry', () => {
    const draft: BrowserCaptureDraft = {
      structuredCapture: '{ malformed but preserved',
      selectedText: 'Title: Network Support Engineer',
      visibleText: '',
    };
    const failedPreview = previewBrowserCaptureDraft(draft);
    expect(failedPreview.draft).toEqual(draft);
    expect(failedPreview.result).toBeNull();
    expect(failedPreview.error).toMatch(/valid JSON/i);

    const html = renderToStaticMarkup(<BrowserJobExtractionPanel
      draft={failedPreview.draft}
      result={failedPreview.result}
      previewError={failedPreview.error}
      onDraftChange={vi.fn()}
      onPreview={vi.fn()}
      onConfirm={vi.fn()}
    />);
    expect(html).toContain('{ malformed but preserved');
    expect(html).toContain('Title: Network Support Engineer');
    expect(html).toContain('Retry extraction preview');
    expect(html).toContain('Enter valid JSON for the structured capture.');
  });
});
