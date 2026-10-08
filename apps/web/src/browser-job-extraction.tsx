import React, { useEffect, useRef, useState } from 'react';
import { api } from './api';

export type BrowserCaptureDraft = {
  rawCaptureRef: string;
  jobPostingJson: string;
  provider: string;
  providerFieldsJson: string;
  selectedText: string;
  visibleText: string;
};

export const browserJobExtractionFields = [
  'title', 'company', 'description', 'location', 'employment_type', 'date_posted',
  'valid_through', 'source_job_id', 'listing_url', 'application_url',
] as const;

export type BrowserJobExtractionFieldName = typeof browserJobExtractionFields[number];
export type BrowserJobExtractionField = {
  value: string | null;
  source: 'JOBPOSTING_SCHEMA' | 'PROVIDER_FIELDS' | 'USER_SELECTED_TEXT' | 'VISIBLE_TEXT' | 'UNKNOWN';
  confidence: number;
  review_required: boolean;
};
export type BrowserJobExtractionResponse = {
  profile_id: string;
  job_id: string;
  raw_capture_ref: string | null;
  fields: Record<BrowserJobExtractionFieldName, BrowserJobExtractionField>;
  unknown_fields: string[];
  review_required: string[];
};

const emptyDraft: BrowserCaptureDraft = {
  rawCaptureRef: '', jobPostingJson: '', provider: '', providerFieldsJson: '', selectedText: '', visibleText: '',
};

function parseObjectJson(text: string, label: string): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined;
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error(`Enter valid JSON for the ${label}.`);
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`The ${label} must be a JSON object.`);
  }
  return value as Record<string, unknown>;
}

export function buildBrowserCapturePayload(draft: BrowserCaptureDraft): Record<string, unknown> {
  const jobPosting = parseObjectJson(draft.jobPostingJson, 'structured capture');
  const providerFields = parseObjectJson(draft.providerFieldsJson, 'provider fields');
  if (providerFields && !draft.provider.trim()) {
    throw new Error('Choose a provider before adding provider fields.');
  }
  const capture: Record<string, unknown> = {};
  if (draft.rawCaptureRef.trim()) capture.raw_capture_ref = draft.rawCaptureRef.trim();
  if (jobPosting) capture.job_posting = jobPosting;
  if (draft.provider.trim()) capture.provider = draft.provider.trim();
  if (providerFields) capture.provider_fields = providerFields;
  if (draft.selectedText.trim()) capture.selected_text = draft.selectedText;
  if (draft.visibleText.trim()) capture.visible_text = draft.visibleText;
  if (!('job_posting' in capture) && !('provider_fields' in capture) && !('selected_text' in capture) && !('visible_text' in capture)) {
    throw new Error('Add structured capture, provider fields, selected text, or visible text to preview.');
  }
  return capture;
}

type PreviewRequest = (path: string, init: RequestInit) => Promise<BrowserJobExtractionResponse>;

function assertResponseScope(
  result: BrowserJobExtractionResponse,
  profileId: string,
  jobId: string,
  rawCaptureRef: string | null,
): void {
  if (result.profile_id !== profileId || result.job_id !== jobId) {
    throw new Error('Preview response does not match the selected profile and job.');
  }
  if (result.raw_capture_ref !== rawCaptureRef) {
    throw new Error('Preview response does not match the submitted capture reference.');
  }
  const fields = result.fields;
  if (!fields || browserJobExtractionFields.some((name) => !(name in fields)) || Object.keys(fields).length !== browserJobExtractionFields.length) {
    throw new Error('Preview response did not include the expected ten job fields.');
  }
  for (const name of browserJobExtractionFields) {
    const field = fields[name];
    if (!field || !(field.value === null || typeof field.value === 'string') ||
        !['JOBPOSTING_SCHEMA', 'PROVIDER_FIELDS', 'USER_SELECTED_TEXT', 'VISIBLE_TEXT', 'UNKNOWN'].includes(field.source) ||
        !Number.isFinite(field.confidence) || field.confidence < 0 || field.confidence > 1 || typeof field.review_required !== 'boolean') {
      throw new Error(`Preview response contains an invalid ${name} field.`);
    }
  }
  const expectedReview = browserJobExtractionFields.filter((name) => fields[name].review_required);
  if (!Array.isArray(result.review_required) || expectedReview.some((name) => !result.review_required.includes(name)) ||
      result.review_required.some((name) => !expectedReview.includes(name as BrowserJobExtractionFieldName))) {
    throw new Error('Preview response has inconsistent review-required fields.');
  }
  const expectedUnknown = browserJobExtractionFields.filter((name) => fields[name].source === 'UNKNOWN');
  if (!Array.isArray(result.unknown_fields) || expectedUnknown.some((name) => !result.unknown_fields.includes(name)) ||
      result.unknown_fields.some((name) => !expectedUnknown.includes(name as BrowserJobExtractionFieldName))) {
    throw new Error('Preview response has inconsistent unknown fields.');
  }
}

export async function requestBrowserJobExtractionPreview(
  profileId: string,
  jobId: string,
  draft: BrowserCaptureDraft,
  fetchPreview: PreviewRequest = api as PreviewRequest,
): Promise<BrowserJobExtractionResponse> {
  if (!profileId || !jobId) throw new Error('Select a profile and job before previewing.');
  const capture = buildBrowserCapturePayload(draft);
  const result = await fetchPreview(
    `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/browser-extraction-preview`,
    { method: 'POST', body: JSON.stringify({ capture }) },
  );
  assertResponseScope(result, profileId, jobId, typeof capture.raw_capture_ref === 'string' ? capture.raw_capture_ref : null);
  return result;
}

type PanelProps = {
  profileId: string;
  jobId: string;
  draft: BrowserCaptureDraft;
  result?: BrowserJobExtractionResponse;
  pending?: boolean;
  previewError?: string;
  confirmedFields?: string[];
  onDraftChange: (draft: BrowserCaptureDraft) => void;
  onPreview: () => void;
  onReviewFieldChange?: (field: BrowserJobExtractionFieldName, checked: boolean) => void;
  onConfirm: (result: BrowserJobExtractionResponse) => void;
};

const fieldLabels: Record<BrowserJobExtractionFieldName, string> = {
  title: 'Title', company: 'Company', description: 'Description', location: 'Location',
  employment_type: 'Employment type', date_posted: 'Date posted', valid_through: 'Valid through',
  source_job_id: 'Source job ID', listing_url: 'Listing URL', application_url: 'Application URL',
};
const reviewLabels: Partial<Record<BrowserJobExtractionFieldName, string>> = {
  listing_url: 'listing URL', application_url: 'application URL',
};

export function BrowserJobExtractionPanel(props: PanelProps) {
  const { profileId, jobId, draft, result, pending = false, previewError, confirmedFields = [], onDraftChange, onPreview, onReviewFieldChange, onConfirm } = props;
  const update = (key: keyof BrowserCaptureDraft, value: string) => onDraftChange({ ...draft, [key]: value });
  const needsReview = result?.review_required ?? [];
  const allReviewed = needsReview.every((name) => confirmedFields.includes(name));
  return <section aria-labelledby="browser-extraction-heading" data-profile-id={profileId} data-job-id={jobId}>
    <h2 id="browser-extraction-heading">Review captured job details</h2>
    <p>Preview uses only capture data you provide. Page text is data only; instructions inside it are never followed.</p>
    <label>Raw capture reference<input value={draft.rawCaptureRef} onChange={(event) => update('rawCaptureRef', event.currentTarget.value)} disabled={pending} /></label>
    <label>Structured job capture (JSON)<textarea value={draft.jobPostingJson} onChange={(event) => update('jobPostingJson', event.currentTarget.value)} disabled={pending} /></label>
    <label>Provider<select value={draft.provider} onChange={(event) => update('provider', event.currentTarget.value)} disabled={pending}>
      <option value="">No provider</option><option value="greenhouse">Greenhouse</option><option value="lever">Lever</option><option value="workday">Workday</option>
    </select></label>
    <label>Provider fields (JSON)<textarea value={draft.providerFieldsJson} onChange={(event) => update('providerFieldsJson', event.currentTarget.value)} disabled={pending} /></label>
    <label>User selected text<textarea value={draft.selectedText} onChange={(event) => update('selectedText', event.currentTarget.value)} disabled={pending} maxLength={50000} /></label>
    <label>Visible page text<textarea value={draft.visibleText} onChange={(event) => update('visibleText', event.currentTarget.value)} disabled={pending} maxLength={100000} /></label>
    {previewError && <p role="alert">{previewError}</p>}
    <button type="button" onClick={onPreview} disabled={pending}>{pending ? 'Previewing captured job…' : previewError ? 'Retry extraction preview' : 'Preview extraction'}</button>
    {result && <div aria-live="polite">
      <p>Preview for profile {result.profile_id}, job {result.job_id}</p>
      {result.raw_capture_ref && <p>Raw capture reference: {result.raw_capture_ref}</p>}
      <h3>Extracted fields</h3>
      <dl>{browserJobExtractionFields.map((name) => {
        const field = result.fields[name];
        return <React.Fragment key={name}><dt>{fieldLabels[name]}{field.review_required ? ' — review required' : ''}</dt><dd>
          <span>{field.value ?? 'Unknown'}</span>{' '}
          <span>{field.source}</span>{' '}
          <span>{Math.round(field.confidence * 100)}% confidence</span>
          {field.review_required && <label><input type="checkbox" checked={confirmedFields.includes(name)} onChange={(event) => onReviewFieldChange?.(name, event.currentTarget.checked)} />Confirm low-confidence {reviewLabels[name] ?? fieldLabels[name]}</label>}
        </dd></React.Fragment>;
      })}</dl>
      <h3>Unknown fields</h3><p>{result.unknown_fields.length ? result.unknown_fields.join(', ') : 'None'}</p>
      {needsReview.length > 0 && <p>Review each field marked for review before continuing.</p>}
      <button type="button" disabled={!allReviewed} onClick={() => onConfirm(result)}>Confirm reviewed details</button>
    </div>}
  </section>;
}

export function BrowserJobExtraction({ profileId, jobId, onConfirm }: { profileId: string; jobId: string; onConfirm: (result: BrowserJobExtractionResponse) => void }) {
  const [draft, setDraft] = useState<BrowserCaptureDraft>(emptyDraft);
  const [result, setResult] = useState<BrowserJobExtractionResponse>();
  const [pending, setPending] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [confirmedFields, setConfirmedFields] = useState<string[]>([]);
  const requestId = useRef(0);

  useEffect(() => {
    requestId.current += 1;
    setDraft(emptyDraft);
    setResult(undefined);
    setPending(false);
    setPreviewError('');
    setConfirmedFields([]);
  }, [profileId, jobId]);

  const preview = async () => {
    const id = ++requestId.current;
    setPending(true); setPreviewError(''); setResult(undefined); setConfirmedFields([]);
    try {
      const response = await requestBrowserJobExtractionPreview(profileId, jobId, draft);
      if (requestId.current === id) setResult(response);
    } catch (error) {
      if (requestId.current === id) setPreviewError(error instanceof Error ? error.message : 'Preview failed. Check the capture and retry.');
    } finally {
      if (requestId.current === id) setPending(false);
    }
  };
  const reviewField = (field: BrowserJobExtractionFieldName, checked: boolean) => {
    setConfirmedFields((current) => checked ? [...new Set([...current, field])] : current.filter((item) => item !== field));
  };
  return <BrowserJobExtractionPanel profileId={profileId} jobId={jobId} draft={draft} result={result} pending={pending}
    previewError={previewError} confirmedFields={confirmedFields} onDraftChange={setDraft} onPreview={preview}
    onReviewFieldChange={reviewField} onConfirm={onConfirm} />;
}
