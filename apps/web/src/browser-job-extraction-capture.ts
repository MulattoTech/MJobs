import { api } from './api';
import {
  browserJobExtractionFields,
  correctableBrowserJobExtractionFields,
  validateBrowserJobExtractionCorrections,
  type BrowserJobExtractionConfirmation,
  type BrowserJobExtractionCorrections,
  type BrowserJobExtractionField,
  type BrowserJobExtractionFieldName,
} from './browser-job-extraction';

type JsonRequest = (path: string, init?: RequestInit) => Promise<unknown>;

export type BrowserJobExtractionManualDetails = {
  /** Must be supplied by the user when the extraction has no description. */
  description?: string;
  /** Optional user-entered replacement for an unknown or unsuitable location. */
  location?: string;
  discovery_origin?: string | null;
  observed_hops?: string[];
};

export type BrowserJobExtractionCapturePayload = {
  profile_id: string;
  request_id: string;
  title: string;
  company: string;
  description: string;
  location: string;
  reference_url: string | null;
  application_url: string | null;
  discovery_origin: string | null;
  observed_hops: string[];
  source_details: {
    extraction_fields: Record<BrowserJobExtractionFieldName, {
      value: string | null;
      source: BrowserJobExtractionField['source'];
      confidence: number;
      reviewed: boolean;
    }>;
  };
};

export type BrowserJobExtractionCaptureResult = {
  job: Record<string, unknown> & { id: string };
  created: boolean;
  sourceCandidate: {
    id: string;
    profile_id: string;
    collection_enabled?: boolean;
    observations: { job_id: string | null }[];
  };
  notices: string[];
};

export type BrowserJobExtractionCaptureClient = {
  capture: (
    confirmation: BrowserJobExtractionConfirmation,
    manualDetails?: BrowserJobExtractionManualDetails,
  ) => Promise<BrowserJobExtractionCaptureResult>;
};

const maxFieldLengths: Partial<Record<BrowserJobExtractionFieldName, number>> = {
  title: 500,
  company: 200,
  description: 50000,
  location: 1000,
  employment_type: 64,
  date_posted: 64,
  valid_through: 64,
  source_job_id: 256,
  listing_url: 2000,
  application_url: 2000,
};
const knownSources = ['JOBPOSTING_SCHEMA', 'PROVIDER_FIELDS', 'USER_SELECTED_TEXT', 'VISIBLE_TEXT', 'UNKNOWN'] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function finalValue(
  confirmation: BrowserJobExtractionConfirmation,
  name: BrowserJobExtractionFieldName,
): string | null {
  if (Object.prototype.hasOwnProperty.call(confirmation.corrections, name)) {
    const corrected = confirmation.corrections[name];
    return typeof corrected === 'string' ? corrected.trim() || null : null;
  }
  const extracted = confirmation.preview.fields[name]?.value;
  return typeof extracted === 'string' ? extracted.trim() || null : null;
}

function validateHttpUrl(value: string | null, field: string): string | null {
  if (value === null) return null;
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`${field} must be a safe HTTP or HTTPS URL before capture.`);
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || !url.hostname || url.username || url.password) {
    throw new Error(`${field} must be a safe HTTP or HTTPS URL before capture.`);
  }
  return value;
}

function reviewedAtConfirmation(
  confirmation: BrowserJobExtractionConfirmation,
  name: BrowserJobExtractionFieldName,
): boolean {
  return confirmation.reviewed_fields.includes(name);
}

function normalizedManualText(value: string | undefined, limit: number, label: string): string | null {
  if (value === undefined) return null;
  const normalized = value.trim();
  if (normalized.length > limit) throw new Error(`${label} must be no longer than ${limit} characters.`);
  return normalized || null;
}

function normalizedHops(hops: string[] | undefined): string[] {
  if (hops === undefined) return ['UNKNOWN'];
  if (hops.length > 20 || hops.some(hop => typeof hop !== 'string' || hop.length > 2000)) {
    throw new Error('Observed hops must contain at most 20 text values of no more than 2000 characters each.');
  }
  const values = hops.map(hop => hop.trim()).filter(Boolean);
  return values.length ? values : ['UNKNOWN'];
}

export function buildBrowserJobExtractionCapturePayload(
  selectedProfileId: string,
  confirmation: BrowserJobExtractionConfirmation,
  manualDetails: BrowserJobExtractionManualDetails,
  requestId: string,
): { payload: BrowserJobExtractionCapturePayload; notices: string[] } {
  if (!selectedProfileId.trim()) throw new Error('Select a profile before capturing this job.');
  if (confirmation.preview.profile_id !== selectedProfileId) {
    throw new Error('Extraction preview does not match the selected profile.');
  }
  if (!requestId.trim() || requestId !== requestId.trim() || requestId.length > 64) {
    throw new Error('Capture request ID must be a stable, nonempty value of at most 64 characters.');
  }

  const fields = confirmation.preview.fields;
  if (!fields || browserJobExtractionFields.some(name => !fields[name]) ||
      Object.keys(fields).length !== browserJobExtractionFields.length) {
    throw new Error('Extraction preview must contain the expected ten fields.');
  }
  for (const name of browserJobExtractionFields) {
    const field = fields[name];
    if (!(field.value === null || typeof field.value === 'string') ||
        !knownSources.includes(field.source) || !Number.isFinite(field.confidence) ||
        field.confidence < 0 || field.confidence > 1 || typeof field.review_required !== 'boolean') {
      throw new Error(`Extraction preview contains invalid ${name} provenance.`);
    }
  }

  const knownFieldNames = new Set<string>(browserJobExtractionFields);
  if (!Array.isArray(confirmation.reviewed_fields) || confirmation.reviewed_fields.some(name =>
      !knownFieldNames.has(name))) {
    throw new Error('Review confirmation contains an unknown field name.');
  }
  if (!Array.isArray(confirmation.preview.unknown_fields) ||
      confirmation.preview.unknown_fields.some(name => !knownFieldNames.has(name))) {
    throw new Error('Extraction preview contains invalid unknown-field metadata.');
  }
  if (!Array.isArray(confirmation.preview.review_required) ||
      confirmation.preview.review_required.some(name => !knownFieldNames.has(name))) {
    throw new Error('Extraction preview contains invalid review metadata.');
  }
  const actualUnknownFields = browserJobExtractionFields.filter(name =>
    fields[name].value === null && fields[name].source === 'UNKNOWN',
  );
  if (actualUnknownFields.length !== confirmation.preview.unknown_fields.length ||
      actualUnknownFields.some(name => !confirmation.preview.unknown_fields.includes(name)) ||
      browserJobExtractionFields.some(name =>
        fields[name].review_required !== confirmation.preview.review_required.includes(name),
      )) {
    throw new Error('Extraction preview field metadata is inconsistent.');
  }

  const reviewed = new Set(confirmation.reviewed_fields);
  const unreviewedLowConfidence = browserJobExtractionFields.filter(name =>
    fields[name].review_required && !reviewed.has(name),
  );
  if (unreviewedLowConfidence.length) {
    throw new Error(`Explicitly review each low-confidence field before capture: ${unreviewedLowConfidence.join(', ')}.`);
  }

  const corrections: BrowserJobExtractionCorrections = {};
  for (const rawName of Object.keys(confirmation.corrections)) {
    if (!correctableBrowserJobExtractionFields.includes(rawName as BrowserJobExtractionFieldName)) {
      throw new Error('Extraction confirmation contains a correction for a non-editable field.');
    }
  }
  for (const name of correctableBrowserJobExtractionFields) {
    const value = confirmation.corrections[name];
    if (typeof value === 'string') corrections[name] = value;
  }
  const correctionErrors = validateBrowserJobExtractionCorrections(confirmation.preview, corrections);
  if (Object.keys(correctionErrors).length) throw new Error(Object.values(correctionErrors).join(' '));

  const finalValues = Object.fromEntries(browserJobExtractionFields.map(name => [name, finalValue(confirmation, name)])) as
    Record<BrowserJobExtractionFieldName, string | null>;
  for (const name of browserJobExtractionFields) {
    const value = finalValues[name];
    const limit = maxFieldLengths[name];
    if (value && limit !== undefined && value.length > limit) {
      throw new Error(`${name.replaceAll('_', ' ')} exceeds the ${limit}-character manual-capture limit.`);
    }
  }
  const title = finalValues.title;
  const company = finalValues.company;
  if (!title || !company) throw new Error('Review or correct the extracted title and company before capture.');
  if (title.length > 500 || company.length > 200) throw new Error('Title or company exceeds the manual-capture length limit.');

  const extractedDescription = finalValues.description;
  const manualDescription = normalizedManualText(manualDetails.description, 50000, 'Manual description');
  const description = extractedDescription || manualDescription;
  if (!description) {
    throw new Error('Description was unknown in the preview. Enter the job description manually before capture; no text will be invented.');
  }

  const referenceUrl = validateHttpUrl(finalValues.listing_url, 'Job reference URL');
  const applicationUrl = validateHttpUrl(finalValues.application_url, 'Application URL');
  if (!referenceUrl && !applicationUrl) {
    throw new Error('Review or add a job reference or application URL before capture.');
  }
  const identityAndRoutes = ['title', 'company'] as const;
  for (const name of identityAndRoutes) {
    const value = finalValues[name];
    if (!value || !reviewedAtConfirmation(confirmation, name)) {
      throw new Error(`Review ${name.replaceAll('_', ' ')} before capture.`);
    }
  }
  const routeFields = ['listing_url', 'application_url'] as const;
  const presentRoutes = routeFields.filter(name => finalValues[name]);
  if (presentRoutes.some(name => !reviewedAtConfirmation(confirmation, name))) {
    throw new Error('Review each job reference or application URL before capture.');
  }

  const manualLocation = normalizedManualText(manualDetails.location, 1000, 'Manual location');
  const location = manualLocation || finalValues.location || 'Unknown';
  const origin = normalizedManualText(manualDetails.discovery_origin ?? undefined, 253, 'Discovery origin');
  const observedHops = normalizedHops(manualDetails.observed_hops);
  const notices: string[] = [];
  if (!extractedDescription && manualDescription) notices.push('The description was entered manually because extraction returned unknown.');
  if (confirmation.preview.unknown_fields.length) {
    notices.push(`Extraction remains unknown for: ${confirmation.preview.unknown_fields.map(name => name.replaceAll('_', ' ')).join(', ')}.`);
  }
  if (!manualLocation && !finalValues.location) notices.push('Location was not extracted; it is recorded as Unknown unless you provide a manual value.');
  if (!origin) notices.push('Discovery origin was not supplied; its attribution remains unknown.');
  if (observedHops.length === 1 && observedHops[0] === 'UNKNOWN') notices.push('No redirect hops were supplied; hops remain explicitly unknown.');

  const extractionFields = Object.fromEntries(browserJobExtractionFields.map(name => {
    const value = finalValues[name];
    return [name, {
      value,
      source: fields[name].source,
      confidence: fields[name].confidence,
      reviewed: reviewedAtConfirmation(confirmation, name),
    }];
  })) as BrowserJobExtractionCapturePayload['source_details']['extraction_fields'];

  return {
    payload: {
      profile_id: selectedProfileId,
      request_id: requestId,
      title,
      company,
      description,
      location,
      reference_url: referenceUrl,
      application_url: applicationUrl,
      discovery_origin: origin,
      observed_hops: observedHops,
      source_details: { extraction_fields: extractionFields },
    },
    notices,
  };
}

function isManualJobResponse(value: unknown): value is { job: Record<string, unknown> & { id: string }; created: boolean } {
  return isRecord(value) && isRecord(value.job) && typeof value.job.id === 'string' &&
    typeof value.created === 'boolean';
}

function isCandidateReadback(value: unknown): value is {
  id: string;
  profile_id: string;
  collection_enabled?: boolean;
  observations: { job_id: string | null }[];
} {
  return isRecord(value) && typeof value.id === 'string' && typeof value.profile_id === 'string' &&
    Array.isArray(value.observations) && value.observations.every(observation =>
      isRecord(observation) && (observation.job_id === null || typeof observation.job_id === 'string'),
    );
}

function isReadbackJob(value: unknown, expectedJobId: string, expected: BrowserJobExtractionCapturePayload):
  value is Record<string, unknown> & { id: string } {
  return isRecord(value) && value.id === expectedJobId &&
    value.title === expected.title && value.company === expected.company &&
    value.description === expected.description && value.location === expected.location;
}

export function createBrowserJobExtractionCaptureClient(
  selectedProfileId: string,
  request: JsonRequest = (path, init) => api<unknown>(path, init),
  requestIdFactory: () => string = () => crypto.randomUUID(),
): BrowserJobExtractionCaptureClient {
  const retryIds = new Map<string, string>();

  return {
    async capture(confirmation, manualDetails = {}): Promise<BrowserJobExtractionCaptureResult> {
      if (!selectedProfileId.trim()) throw new Error('Select a profile before capturing this job.');

      // Fingerprint the complete proposed write without the idempotency key. A changed user review gets a new key.
      const provisional = buildBrowserJobExtractionCapturePayload(selectedProfileId, confirmation, manualDetails, 'capture-request-placeholder');
      const fingerprint = JSON.stringify(provisional.payload);
      let requestId = retryIds.get(fingerprint);
      if (!requestId) {
        requestId = requestIdFactory();
        if (!requestId.trim() || requestId.length > 64) throw new Error('Capture request ID factory returned an invalid value.');
        retryIds.set(fingerprint, requestId);
      }
      const { payload, notices } = buildBrowserJobExtractionCapturePayload(selectedProfileId, confirmation, manualDetails, requestId);
      const serializedPayload = JSON.stringify(payload);
      if (new TextEncoder().encode(serializedPayload).byteLength > 100_000) {
        throw new Error('Reviewed capture data exceeds the 100 KiB limit. Shorten the description or remove optional details and retry.');
      }

      // Called only by the UI's explicit confirmation handler; preview never invokes this client.
      const posted = await request('/jobs/manual', { method: 'POST', body: serializedPayload });
      if (!isManualJobResponse(posted)) throw new Error('Manual capture response did not include a durable job record. Retry with the same reviewed details.');
      const jobId = posted.job.id;

      const durableJob = await request(`/jobs/${encodeURIComponent(jobId)}?profile_id=${encodeURIComponent(selectedProfileId)}`);
      if (!isReadbackJob(durableJob, jobId, payload)) {
        throw new Error('Saved job could not be read back in the selected profile. Retry with the same reviewed details.');
      }

      const candidateResponse = await request(`/source-candidates?profile_id=${encodeURIComponent(selectedProfileId)}`);
      if (!isRecord(candidateResponse) || !Array.isArray(candidateResponse.candidates)) {
        throw new Error('Source candidate readback response was invalid. Retry with the same reviewed details.');
      }
      const candidates: unknown[] = candidateResponse.candidates;
      if (!candidates.every(isCandidateReadback) || candidates.some(candidate => candidate.profile_id !== selectedProfileId)) {
        throw new Error('Source candidate readback did not match the selected profile scope. Retry with the same reviewed details.');
      }
      const matches = candidates.filter(candidate => candidate.observations.some(observation => observation.job_id === jobId));
      if (matches.length !== 1) {
        throw new Error(`Job ${jobId} was saved, but a unique durable source candidate readback is not available. Retry with the same reviewed details.`);
      }

      retryIds.delete(fingerprint);
      return { job: durableJob, created: posted.created, sourceCandidate: matches[0], notices };
    },
  };
}
