import React from 'react';
import {
  buildBrowserJobExtractionConfirmation,
  validateBrowserJobExtractionCorrections,
  type BrowserJobExtractionConfirmation,
  type BrowserJobExtractionCorrections,
  type BrowserJobExtractionFieldName,
  type BrowserJobExtractionResponse,
} from './browser-job-extraction';

const editableFields = ['title', 'company', 'listing_url', 'application_url'] as const;
type EditableField = typeof editableFields[number];

const fieldLabels: Record<EditableField, string> = {
  title: 'Title',
  company: 'Company',
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
  const missingReviews = preview.review_required.filter(field => !confirmedFields.includes(field as BrowserJobExtractionFieldName));
  const missingDescription = !description.trim();
  const canConfirm = !pending && !missingDescription && missingReviews.length === 0 && Object.keys(correctionErrors).length === 0;

  const confirm = () => {
    if (!canConfirm) return;
    const safeCorrections: BrowserJobExtractionCorrections = {};
    for (const field of editableFields) {
      if (Object.prototype.hasOwnProperty.call(corrections, field)) safeCorrections[field] = corrections[field] ?? '';
    }
    const reviewed = buildBrowserJobExtractionConfirmation(preview, safeCorrections, confirmedFields);
    onConfirm({ ...reviewed, manualDetails: { description: description.trim() } });
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
                  if (preview.fields[field].review_required) onReviewFieldChange(field, false);
                }}
                disabled={pending}
              />
            </label>
            {correctionErrors[field] && <p role="alert">{correctionErrors[field]}</p>}
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
    {missingDescription && <p role="alert">Enter the job description manually before confirming. It will not be inferred from page instructions.</p>}
    {!missingDescription && <p>Use the extracted description only after checking it against the job posting. You can edit it here.</p>}

    <h3>Fields requiring review</h3>
    {preview.review_required.length === 0
      ? <p>No extracted fields were flagged for low confidence.</p>
      : <ul>{preview.review_required.map(rawField => {
        const field = rawField as BrowserJobExtractionFieldName;
        const extracted = preview.fields[field];
        const value = editableFields.includes(field as EditableField)
          ? displayValue(preview, field as EditableField, corrections)
          : extracted.value ?? 'Unknown';
        return <li key={field}>
          <p>{displayName(field)}: {value} · {extracted.source} · {Math.round(extracted.confidence * 100)}% confidence</p>
          <label><input
            type="checkbox"
            aria-label={`Confirm low-confidence ${displayName(field)}`}
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
