import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  BrowserJobExtractionConfirmationStep,
  type BrowserJobExtractionConfirmationStepProps,
} from './browser-job-extraction-confirmation';
import type { BrowserJobExtractionResponse } from './browser-job-extraction';

const preview: BrowserJobExtractionResponse = {
  profile_id: 'profile-synthetic',
  job_id: 'job-synthetic',
  raw_capture_ref: 'capture-ref-synthetic',
  fields: {
    title: { value: 'Systems Administrator', source: 'JOBPOSTING_SCHEMA', confidence: 0.98, review_required: false },
    company: { value: 'Synthetic Employer', source: 'PROVIDER_FIELDS', confidence: 0.92, review_required: false },
    description: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    location: { value: 'Tulsa', source: 'USER_SELECTED_TEXT', confidence: 0.64, review_required: true },
    employment_type: { value: 'Full-time', source: 'JOBPOSTING_SCHEMA', confidence: 0.91, review_required: false },
    date_posted: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    valid_through: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    source_job_id: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
    listing_url: { value: 'https://jobs.example.test/listing/42', source: 'VISIBLE_TEXT', confidence: 0.38, review_required: true },
    application_url: { value: 'https://jobs.example.test/apply/42', source: 'JOBPOSTING_SCHEMA', confidence: 0.88, review_required: false },
  },
  unknown_fields: ['description', 'date_posted', 'valid_through', 'source_job_id'],
  review_required: ['location', 'listing_url'],
};
const allAcknowledgements = ['title', 'company', 'description', 'location', 'listing_url', 'application_url'] as const;

function props(overrides: Partial<BrowserJobExtractionConfirmationStepProps> = {}): BrowserJobExtractionConfirmationStepProps {
  return {
    preview,
    description: '',
    corrections: {},
    confirmedFields: [],
    onDescriptionChange: vi.fn(),
    onCorrectionChange: vi.fn(),
    onReviewFieldChange: vi.fn(),
    onConfirm: vi.fn(),
    ...overrides,
  };
}

function elements(node: React.ReactNode): React.ReactElement[] {
  if (!React.isValidElement(node)) return [];
  const children = (node.props as { children?: React.ReactNode }).children;
  return [node, ...(Array.isArray(children) ? children.flatMap(elements) : elements(children))];
}

function fieldInput(tree: React.ReactElement, ariaLabel: string): React.ReactElement {
  const found = elements(tree).find(element => (element.props as { 'aria-label'?: string })['aria-label'] === ariaLabel);
  if (!found) throw new Error(`Missing control: ${ariaLabel}`);
  return found;
}

type TestControlProps = { disabled?: boolean; onClick?: () => void; onChange?: (event: any) => void };
function controlProps(element: React.ReactElement): TestControlProps {
  return element.props as TestControlProps;
}

function confirmButton(tree: React.ReactElement): React.ReactElement {
  const found = elements(tree).find(element =>
    (element.type === 'button') && (element.props as { children?: React.ReactNode }).children === 'Confirm reviewed job details',
  );
  if (!found) throw new Error('Missing confirm button.');
  return found;
}

describe('browser job extraction confirmation step', () => {
  it('blocks confirmation while the extracted description is missing', () => {
    const onConfirm = vi.fn();
    const tree = BrowserJobExtractionConfirmationStep(props({
      confirmedFields: [...allAcknowledgements],
      onConfirm,
    }));

    expect(controlProps(confirmButton(tree)).disabled).toBe(true);
    expect(renderToStaticMarkup(tree)).toContain('Enter the job description manually before confirming');
    controlProps(confirmButton(tree)).onClick?.();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it('enables confirmation after the user enters a missing description and reviews flagged fields', () => {
    const onConfirm = vi.fn();
    const tree = BrowserJobExtractionConfirmationStep(props({
      description: 'Maintain Linux systems and internal services.',
      confirmedFields: [...allAcknowledgements],
      onConfirm,
    }));

    expect(controlProps(confirmButton(tree)).disabled).toBe(false);
    controlProps(confirmButton(tree)).onClick?.();
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({
      manualDetails: { description: 'Maintain Linux systems and internal services.' },
    }));
  });

  it('requires an explicit review for each low-confidence field', () => {
    const tree = BrowserJobExtractionConfirmationStep(props({ description: 'Manual description.' }));
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('aria-label="Confirm low-confidence Location"');
    expect(html).toContain('aria-label="Confirm low-confidence Listing URL"');
    expect(html).toContain('aria-label="Confirm extracted Title"');
    expect(html).toContain('aria-label="Confirm extracted Company"');
    expect(html).toContain('aria-label="Confirm extracted Application URL"');
    expect(html).toContain('aria-label="Confirm reviewed job description"');
    expect(controlProps(confirmButton(tree)).disabled).toBe(true);
  });

  it('requires explicit acknowledgements for title, company, provided routes, and low-confidence fields', () => {
    const tree = BrowserJobExtractionConfirmationStep(props({
      description: 'Manual description.',
      confirmedFields: ['location', 'listing_url'],
    }));
    expect(controlProps(confirmButton(tree)).disabled).toBe(true);

    const allReviewed = BrowserJobExtractionConfirmationStep(props({
      description: 'Manual description.',
      confirmedFields: [...allAcknowledgements],
    }));
    expect(controlProps(confirmButton(allReviewed)).disabled).toBe(false);
  });

  it.each([
    ['title', 'Correction for Title', 'Corrected title'],
    ['company', 'Correction for Company', 'Corrected employer'],
    ['listing_url', 'Correction for Listing URL', 'https://jobs.example.test/new-listing'],
    ['application_url', 'Correction for Application URL', 'https://jobs.example.test/new-application'],
  ] as const)('resets the %s acknowledgement when a correction changes', (field, label, value) => {
    const onReviewFieldChange = vi.fn();
    const tree = BrowserJobExtractionConfirmationStep(props({
      description: 'Manual description.',
      confirmedFields: [...allAcknowledgements],
      onReviewFieldChange,
    }));
    controlProps(fieldInput(tree, label)).onChange?.({ currentTarget: { value } });
    expect(onReviewFieldChange).toHaveBeenCalledWith(field, false);

    const afterEdit = BrowserJobExtractionConfirmationStep(props({
      description: 'Manual description.',
      corrections: { [field]: value },
      confirmedFields: allAcknowledgements.filter(acknowledged => acknowledged !== field),
    }));
    expect(controlProps(confirmButton(afterEdit)).disabled).toBe(true);
  });

  it('requires acknowledgement when a route is added and omits acknowledgement for absent routes', () => {
    const noRoutes: BrowserJobExtractionResponse = {
      ...preview,
      fields: {
        ...preview.fields,
        listing_url: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
        application_url: { value: null, source: 'UNKNOWN', confidence: 0, review_required: false },
      },
      unknown_fields: [...preview.unknown_fields, 'listing_url', 'application_url'],
      review_required: ['location'],
    };
    const absent = BrowserJobExtractionConfirmationStep(props({
      preview: noRoutes,
      description: 'Manual description.',
      confirmedFields: ['title', 'company', 'description', 'location'],
    }));
    expect(controlProps(confirmButton(absent)).disabled).toBe(false);

    const added = BrowserJobExtractionConfirmationStep(props({
      preview: noRoutes,
      description: 'Manual description.',
      corrections: { application_url: 'https://jobs.example.test/apply' },
      confirmedFields: ['title', 'company', 'location'],
    }));
    expect(controlProps(confirmButton(added)).disabled).toBe(true);
  });

  it('requires a separate acknowledgement when the user removes an extracted route', () => {
    const tree = BrowserJobExtractionConfirmationStep(props({
      description: 'Reviewed description.',
      corrections: { application_url: '' },
      confirmedFields: ['title', 'company', 'description', 'location', 'listing_url'],
    }));
    expect(renderToStaticMarkup(tree)).toContain('aria-label="Confirm removal of extracted Application URL"');
    expect(controlProps(confirmButton(tree)).disabled).toBe(true);
  });

  it('emits the edited description and identity/routes with safe extraction provenance', () => {
    const onDescriptionChange = vi.fn();
    const onCorrectionChange = vi.fn();
    const onConfirm = vi.fn();
    const initial = props({
      description: '',
      corrections: {},
      confirmedFields: [...allAcknowledgements],
      onDescriptionChange,
      onCorrectionChange,
      onConfirm,
    });
    const firstTree = BrowserJobExtractionConfirmationStep(initial);
    controlProps(fieldInput(firstTree, 'Confirmed job description')).onChange?.({ currentTarget: { value: 'Edited role description.' } });
    controlProps(fieldInput(firstTree, 'Correction for Title')).onChange?.({ currentTarget: { value: 'Network Administrator' } });
    controlProps(fieldInput(firstTree, 'Correction for Company')).onChange?.({ currentTarget: { value: 'Corrected Employer' } });
    controlProps(fieldInput(firstTree, 'Correction for Listing URL')).onChange?.({ currentTarget: { value: 'https://jobs.example.test/updated-listing' } });
    controlProps(fieldInput(firstTree, 'Correction for Application URL')).onChange?.({ currentTarget: { value: 'https://jobs.example.test/updated-apply' } });

    expect(onDescriptionChange).toHaveBeenCalledWith('Edited role description.');
    expect(onCorrectionChange).toHaveBeenCalledWith('title', 'Network Administrator');
    expect(onCorrectionChange).toHaveBeenCalledWith('company', 'Corrected Employer');
    expect(onCorrectionChange).toHaveBeenCalledWith('listing_url', 'https://jobs.example.test/updated-listing');
    expect(onCorrectionChange).toHaveBeenCalledWith('application_url', 'https://jobs.example.test/updated-apply');

    const edited = BrowserJobExtractionConfirmationStep(props({
      description: 'Edited role description.',
      corrections: {
        title: 'Network Administrator',
        company: 'Corrected Employer',
        listing_url: 'https://jobs.example.test/updated-listing',
        application_url: 'https://jobs.example.test/updated-apply',
      },
      confirmedFields: [...allAcknowledgements],
      onConfirm,
    }));
    controlProps(confirmButton(edited)).onClick?.();

    expect(onConfirm).toHaveBeenCalledWith({
      preview,
      corrections: {
        title: 'Network Administrator',
        company: 'Corrected Employer',
        listing_url: 'https://jobs.example.test/updated-listing',
        application_url: 'https://jobs.example.test/updated-apply',
      },
      reviewed_fields: ['title', 'company', 'description', 'location', 'listing_url', 'application_url'],
      manualDetails: { description: 'Edited role description.' },
    });
    const html = renderToStaticMarkup(edited);
    expect(html).toContain('JOBPOSTING_SCHEMA');
    expect(html).toContain('VISIBLE_TEXT');
    expect(html).not.toContain('capture-ref-synthetic');
  });
});
