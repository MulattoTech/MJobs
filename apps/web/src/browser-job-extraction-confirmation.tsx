import React from 'react';
import {
  browserJobExtractionFields,
  buildBrowserJobExtractionConfirmation,
  validateBrowserJobExtractionCorrections,
  type BrowserJobExtractionConfirmation,
  type BrowserJobExtractionCorrections,
  type BrowserJobExtractionFieldName,
  type BrowserJobExtractionResponse,
} from './browser-job-extraction';

const editableFields = ['title', 'company', 'source_job_id', 'listing_url', 'application_url'] as const;
type EditableField = typeof editableFields[number];

const fieldLabels: Record<EditableField, string> = {
  title: 'Title',
  company: 'Company',
  source_job_id: 'Source job ID',
  listing_url: 'Listing URL',
  application_url: 'Application URL',
};

export type BrowserJobExtractionConfirmedPayload = BrowserJobExtractionConfirmation & {
  manualDetails: { description: string };
};

export type BrowserJobExtractionConfirmationStepProps = {
  preview: BrowserJobExtractionResponse;
  description?: string;
  corrections?: BrowserJobExtractionCorrections;
  confirmedFields?: BrowserJobExtractionFieldName[];
  pending?: boolean;
  onDescriptionChange: (description: string) => void;
  onCorrectionChange: (field: EditableField, value: string) => void;
  onReviewFieldChange: (field: BrowserJobExtractionFieldName, checked: boolean) => void;
  onConfirm: (payload: BrowserJobExtractionConfirmedPayload) => void;
};

function displayValue(preview: BrowserJobExtractionResponse, field: EditableField, corrections: BrowserJobExtractionCorrections): string {
  if (Object.prototype.hasOwnProperty.call(corrections, field)) return corrections[field]?.trim() || 'Unknown';
  return preview.fields[field].value?.trim() || 'Unknown';
}

function displayName(field: BrowserJobExtractionFieldName): string {
  const names: Record<BrowserJobExtractionFieldName, string> = {
    title: 'Title', company: 'Company', description: 'Description', location: 'Location',
    employment_type: 'Employment type', date_posted: 'Date posted', valid_through: 'Valid through',
    source_job_id: 'Source job ID', listing_url: 'Listing URL', application_url: 'Application URL',
  };
  return names[field];
}

function requiredAcknowledgements(
  preview: BrowserJobExtractionResponse,
  corrections: BrowserJobExtractionCorrections,
): BrowserJobExtractionFieldName[] {
  const required = new Set<BrowserJobExtractionFieldName>(['title', 'company', 'description']);
  for (const field of editableFields) {
    if (Object.prototype.hasOwnProperty.call(corrections, field)) required.add(field);
  }
  for (const field of preview.review_required) {
    if ((browserJobExtractionFields as readonly string[]).includes(field)) required.add(field as BrowserJobExtractionFieldName);
  }
  for (const route of ['listing_url', 'application_url'] as const) {
    const value = Object.prototype.hasOwnProperty.call(corrections, route)
      ? corrections[route]
      : preview.fields[route].value;
    if (value?.trim()) required.add(route);
  }
  return browserJobExtractionFields.filter(field => required.has(field));
}

export function BrowserJobExtractionConfirmationStep({
  preview,
  description: suppliedDescription,
  corrections = {},
  confirmedFields = [],
  pending = false,
  onDescriptionChange,
  onCorrectionChange,
  onReviewFieldChange,
  onConfirm,
}: BrowserJobExtractionConfirmationStepProps) {
  const description = suppliedDescription ?? preview.fields.description.value ?? '';
  const correctionErrors = validateBrowserJobExtractionCorrections(preview, corrections);
  const requiredFields = requiredAcknowledgements(preview, corrections);
  const missingReviews = requiredFields.filter(field => !confirmedFields.includes(field));
  const missingDescription = !description.trim();
  const canConfirm = !pending && !missingDescription && missingReviews.length === 0 && Object.keys(correctionErrors).length === 0;

  const confirm = () => {
    if (!canConfirm) return;
    const safeCorrections: BrowserJobExtractionCorrections = {};
    for (const field of editableFields) {
      if (Object.prototype.hasOwnProperty.call(corrections, field)) safeCorrections[field] = corrections[field] ?? '';
    }
    const reviewed = buildBrowserJobExtractionConfirmation(preview, safeCorrections, confirmedFields);
    onConfirm({
      ...reviewed,
      reviewed_fields: requiredFields,
      manualDetails: { description: description.trim() },
    });
  };

  return <section aria-labelledby="job-extraction-confirmation-heading" data-profile-id={preview.profile_id} data-job-id={preview.job_id}>
    <h2 id="job-extraction-confirmation-heading">Confirm extracted job details</h2>
    <p>Review the extracted title, employer, routes, and provenance. Low-confidence fields need your explicit review.</p>
    <p>Profile {preview.profile_id} · Job {preview.job_id}</p>

    <h3>Job identity and routes</h3>
    <dl>
      {editableFields.map(field => {
        const extracted = preview.fields[field];
        return <React.Fragment key={field}>
          <dt>{fieldLabels[field]}</dt>
          <dd>
            <p>Current value: {displayValue(preview, field, corrections)}</p>
            <p>Extracted provenance: {extracted.source} · {Math.round(extracted.confidence * 100)}% confidence</p>
            <label>{`Correction for ${fieldLabels[field]}`}
              <input
                aria-label={`Correction for ${fieldLabels[field]}`}
                type={field === 'listing_url' || field === 'application_url' ? 'url' : 'text'}
                maxLength={field === 'title' ? 500 : field === 'company' ? 200 : 2000}
                value={corrections[field] ?? ''}
                onChange={event => {
                  onCorrectionChange(field, event.currentTarget.value);
                  onReviewFieldChange(field, false);
                }}
                disabled={pending}
              />
            </label>
            {correctionErrors[field] && <p role="alert">{correctionErrors[field]}</p>}
            {requiredFields.includes(field) && <label><input
              type="checkbox"
              aria-label={Object.prototype.hasOwnProperty.call(corrections, field) && !corrections[field]?.trim()
                ? `Confirm removal of extracted ${fieldLabels[field]}`
                : preview.fields[field].review_required
                  ? `Confirm low-confidence ${fieldLabels[field]}`
                  : `Confirm extracted ${fieldLabels[field]}`}
              checked={confirmedFields.includes(field)}
              onChange={event => onReviewFieldChange(field, event.currentTarget.checked)}
              disabled={pending}
            />I reviewed this value</label>}
          </dd>
        </React.Fragment>;
      })}
    </dl>

    <h3>Description</h3>
    <label htmlFor="confirmed-job-description">Job description (required)
      <textarea
        id="confirmed-job-description"
        aria-label="Confirmed job description"
        value={description}
        onChange={event => onDescriptionChange(event.currentTarget.value)}
        maxLength={100000}
        disabled={pending}
      />
    </label>
    <label><input
      type="checkbox"
      aria-label="Confirm reviewed job description"
      checked={confirmedFields.includes('description')}
      onChange={event => onReviewFieldChange('description', event.currentTarget.checked)}
      disabled={pending}
    />I reviewed this description text</label>
    {missingDescription && <p role="alert">Enter the job description manually before confirming. It will not be inferred from page instructions.</p>}
    {!missingDescription && <p>Use the extracted description only after checking it against the job posting. You can edit it here.</p>}

    <h3>Fields requiring review</h3>
    {preview.review_required.length === 0
      ? <p>No extracted fields were flagged for low confidence.</p>
      : <ul>{requiredFields.filter(field => field !== 'description' && !editableFields.includes(field as EditableField)).map(field => {
        const extracted = preview.fields[field];
        const value = extracted.value ?? 'Unknown';
        return <li key={field}>
          <p>{displayName(field)}: {value} · {extracted.source} · {Math.round(extracted.confidence * 100)}% confidence</p>
          <label><input
            type="checkbox"
            aria-label={preview.fields[field].review_required
              ? `Confirm low-confidence ${displayName(field)}`
              : `Confirm extracted ${displayName(field)}`}
            checked={confirmedFields.includes(field)}
            onChange={event => onReviewFieldChange(field, event.currentTarget.checked)}
            disabled={pending}
          />I reviewed this field</label>
        </li>;
      })}</ul>}
    {missingReviews.length > 0 && <p role="status">Review every low-confidence field before confirming.</p>}
    <p>This confirmation reports the values you reviewed; it does not submit an application.</p>
    <button type="button" className="primary" onClick={confirm} disabled={!canConfirm}>
      {pending ? 'Confirming reviewed details…' : 'Confirm reviewed job details'}
    </button>
  </section>;
}
