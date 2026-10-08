/** Profile-scoped HTTP adapter for the source-candidate review component. */

export type SourceCandidateState =
  | 'DISCOVERED' | 'NEEDS_REVIEW' | 'INTEGRATION_QUEUED' | 'INTEGRATED' | 'BLOCKED' | 'REJECTED';
export type SourceCandidateSiteType = 'JOB_BOARD' | 'EMPLOYER' | 'ATS' | 'UNKNOWN';
export type SourceCandidateEvidenceStatus = 'PUBLIC_DOCUMENTED' | 'UNKNOWN' | 'BLOCKED';
export type SourceCandidateReviewState = 'NEEDS_REVIEW' | 'INTEGRATION_QUEUED' | 'BLOCKED' | 'REJECTED';

export type SourceCandidateReviewInput = {
  state: SourceCandidateReviewState;
  site_type: SourceCandidateSiteType;
  reason: string;
  api_feed_evidence_status: SourceCandidateEvidenceStatus;
  api_feed_evidence_reference: string | null;
  api_feed_evidence_note: string;
  access_terms_note: string;
  capabilities_limits_note: string;
  expected_value_maintenance_note: string;
  integration_issue: number | null;
  request_id: string;
};

export type SourceCandidateReview = {
  id: string;
  profile_id: string;
  state: SourceCandidateReviewState;
  site_type: SourceCandidateSiteType;
  reason: string;
  api_feed_evidence_status: SourceCandidateEvidenceStatus;
  api_feed_evidence_reference: string | null;
  api_feed_evidence_note: string;
  access_terms_note: string;
  capabilities_limits_note: string;
  expected_value_maintenance_note: string;
  integration_issue: number | null;
  integration_issue_status: 'USER_SUPPLIED_UNVERIFIED' | 'NOT_LINKED';
  reviewed_at: string;
};

export type SourceCandidateObservation = {
  id: string;
  job_id: string | null;
  application_id: string | null;
  discovery_origin: string | null;
  origin_attribution: string;
  source_url: string | null;
  application_url: string | null;
  observed_hops: (string | { state: string })[];
  capture_method: string;
  evidence_status: string;
  observed_at: string;
};

export type SourceCandidate = {
  id: string;
  profile_id: string;
  domain: string;
  display_name: string;
  site_type: SourceCandidateSiteType;
  state: SourceCandidateState;
  first_seen_at: string;
  last_seen_at: string;
  collection_enabled: boolean;
  review_reason: string | null;
  integration_issue: number | null;
  integration_issue_status: 'USER_SUPPLIED_UNVERIFIED' | 'NOT_LINKED';
  observations: SourceCandidateObservation[];
  review_history: SourceCandidateReview[];
  blockers?: string[];
};

export type SourceCandidateReviewClient = {
  list(profileId: string): Promise<SourceCandidate[]>;
  review(profileId: string, candidateId: string,
    input: SourceCandidateReviewInput): Promise<SourceCandidate>;
};

export type SourceCandidateReviewClientOptions = {
  /** Empty by default for same-origin API deployments. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export class SourceCandidateReviewClientError extends Error {
  constructor(readonly status: number | null, message: string) {
    super(message);
    this.name = 'SourceCandidateReviewClientError';
  }
}

const candidateStates = new Set<SourceCandidateState>([
  'DISCOVERED', 'NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'INTEGRATED', 'BLOCKED', 'REJECTED',
]);
const reviewStates = new Set<SourceCandidateReviewState>([
  'NEEDS_REVIEW', 'INTEGRATION_QUEUED', 'BLOCKED', 'REJECTED',
]);
const siteTypes = new Set<SourceCandidateSiteType>(['JOB_BOARD', 'EMPLOYER', 'ATS', 'UNKNOWN']);
const evidenceStatuses = new Set<SourceCandidateEvidenceStatus>([
  'PUBLIC_DOCUMENTED', 'UNKNOWN', 'BLOCKED',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function cleanText(value: unknown, maxLength: number): string {
  if (typeof value !== 'string') return '';
  return value.replace(/[\u0000-\u001f\u007f]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, maxLength);
}

function nullableText(value: unknown, maxLength: number): string | null {
  const cleaned = cleanText(value, maxLength);
  return cleaned || null;
}

function safeDisplayUrl(value: unknown): string | null {
  if (typeof value !== 'string' || !value || value.length > 2000) return null;
  try {
    const url = new URL(value);
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) {
      return null;
    }
    return `${url.origin}${url.pathname}`.slice(0, 2000);
  } catch {
    return null;
  }
}

function nullableId(value: unknown): string | null {
  return typeof value === 'string' && value.length <= 128 ? cleanText(value, 128) || null : null;
}

function optionalIssue(value: unknown): number | null {
  return Number.isSafeInteger(value) && Number(value) > 0 && Number(value) <= 2147483647
    ? Number(value) : null;
}

function enumValue<T extends string>(value: unknown, choices: Set<T>, fallback: T): T {
  return typeof value === 'string' && choices.has(value as T) ? value as T : fallback;
}

function sanitizeReview(value: unknown, profileId: string): SourceCandidateReview {
  if (!isRecord(value) || value.profile_id !== profileId) {
    throw new SourceCandidateReviewClientError(null, 'Source candidate review response did not match the selected profile.');
  }
  return {
    id: cleanText(value.id, 128),
    profile_id: profileId,
    state: enumValue(value.state, reviewStates, 'NEEDS_REVIEW'),
    site_type: enumValue(value.site_type, siteTypes, 'UNKNOWN'),
    reason: cleanText(value.reason, 1000),
    api_feed_evidence_status: enumValue(value.api_feed_evidence_status, evidenceStatuses, 'UNKNOWN'),
    api_feed_evidence_reference: safeDisplayUrl(value.api_feed_evidence_reference),
    api_feed_evidence_note: cleanText(value.api_feed_evidence_note, 1000),
    access_terms_note: cleanText(value.access_terms_note, 1000),
    capabilities_limits_note: cleanText(value.capabilities_limits_note, 1000),
    expected_value_maintenance_note: cleanText(value.expected_value_maintenance_note, 1000),
    integration_issue: optionalIssue(value.integration_issue),
    integration_issue_status: value.integration_issue == null ? 'NOT_LINKED' : 'USER_SUPPLIED_UNVERIFIED',
    reviewed_at: cleanText(value.reviewed_at, 64),
  };
}

function sanitizeCandidate(value: unknown, profileId: string): SourceCandidate {
  if (!isRecord(value) || value.profile_id !== profileId) {
    throw new SourceCandidateReviewClientError(null, 'Source candidate response did not match the selected profile.');
  }
  if (!Array.isArray(value.observations) || !Array.isArray(value.review_history)) {
    throw new SourceCandidateReviewClientError(null, 'Invalid source candidate response.');
  }
  const observations = value.observations.map(item => {
    if (!isRecord(item)) throw new SourceCandidateReviewClientError(null, 'Invalid source candidate response.');
    const hops = Array.isArray(item.observed_hops) ? item.observed_hops.slice(0, 20).map(hop => {
      if (typeof hop === 'string') {
        const safeUrl = safeDisplayUrl(hop);
        if (safeUrl) return safeUrl;
        if (/^https?:\/\//i.test(hop)) return 'Unknown';
        return cleanText(hop, 2000) || 'Unknown';
      }
      if (isRecord(hop)) return { state: cleanText(hop.state, 32) || 'UNKNOWN' };
      return { state: 'UNKNOWN' };
    }) : [{ state: 'UNKNOWN' }];
    return {
      id: cleanText(item.id, 128),
      job_id: nullableId(item.job_id),
      application_id: nullableId(item.application_id),
      discovery_origin: nullableText(item.discovery_origin, 253),
      origin_attribution: cleanText(item.origin_attribution, 64) || 'UNKNOWN',
      source_url: safeDisplayUrl(item.source_url),
      application_url: safeDisplayUrl(item.application_url),
      observed_hops: hops,
      capture_method: cleanText(item.capture_method, 64) || 'UNKNOWN',
      evidence_status: cleanText(item.evidence_status, 64) || 'UNKNOWN',
      observed_at: cleanText(item.observed_at, 64),
    };
  });
  const reviews = value.review_history.map(item => sanitizeReview(item, profileId));
  const integrationIssue = optionalIssue(value.integration_issue);
  const id = cleanText(value.id, 128);
  const domain = cleanText(value.domain, 253);
  const displayName = cleanText(value.display_name, 253);
  if (!id || !domain || !displayName) {
    throw new SourceCandidateReviewClientError(null, 'Invalid source candidate response.');
  }
  const blockers = Array.isArray(value.blockers)
    ? value.blockers.slice(0, 30).map(item => cleanText(item, 1000)).filter(Boolean)
    : undefined;
  return {
    id,
    profile_id: profileId,
    domain,
    display_name: displayName,
    site_type: enumValue(value.site_type, siteTypes, 'UNKNOWN'),
    state: enumValue(value.state, candidateStates, 'DISCOVERED'),
    first_seen_at: cleanText(value.first_seen_at, 64),
    last_seen_at: cleanText(value.last_seen_at, 64),
    collection_enabled: value.collection_enabled === true,
    review_reason: nullableText(value.review_reason, 1000),
    integration_issue: integrationIssue,
    integration_issue_status: integrationIssue === null ? 'NOT_LINKED' : 'USER_SUPPLIED_UNVERIFIED',
    observations,
    review_history: reviews,
    ...(blockers ? { blockers } : {}),
  };
}

function requireIdentifier(value: string, name: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > 128 ||
      value === '.' || value === '..' || /[\\/?#\u0000-\u001f\u007f]/.test(value)) {
    throw new SourceCandidateReviewClientError(null, `Invalid ${name}.`);
  }
  return value;
}

function errorMessage(status: number): string {
  if (status === 404) return 'Source candidate or profile is no longer available (404). Refresh before retrying.';
  if (status === 409) return 'Source candidate review conflicts with current data (409). Reconcile the review before retrying.';
  if (status === 422) return 'Source candidate review was rejected as invalid (422). Check the review details.';
  return `Source candidate service request failed (${status}). Retry with the same review details.`;
}

export function createSourceCandidateReviewClient(
  options: SourceCandidateReviewClientOptions = {},
): SourceCandidateReviewClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
          parsed.search || parsed.hash) throw new Error();
    } catch {
      throw new SourceCandidateReviewClientError(null, 'Invalid source candidate service URL.');
    }
  }
  const fetchImpl = options.fetchImpl ?? fetch;

  const request = async (url: string, init: RequestInit): Promise<unknown> => {
    let response: Response;
    try {
      response = await fetchImpl(url, init);
    } catch {
      throw new SourceCandidateReviewClientError(null, 'Source candidate service could not be reached. Retry with the same review details.');
    }
    if (!response.ok) throw new SourceCandidateReviewClientError(response.status, errorMessage(response.status));
    try {
      return await response.json() as unknown;
    } catch {
      throw new SourceCandidateReviewClientError(response.status, 'Source candidate service returned an invalid response.');
    }
  };

  return {
    async list(profileId: string): Promise<SourceCandidate[]> {
      const selectedProfile = requireIdentifier(profileId, 'profile identifier');
      const query = new URLSearchParams({ profile_id: selectedProfile });
      const result = await request(`${baseUrl}/source-candidates?${query.toString()}`, {
        method: 'GET', headers: { accept: 'application/json' },
      });
      if (!isRecord(result) || !Array.isArray(result.candidates)) {
        throw new SourceCandidateReviewClientError(null, 'Source candidate service returned an invalid list.');
      }
      return result.candidates.map(item => sanitizeCandidate(item, selectedProfile));
    },

    async review(profileId: string, candidateId: string,
      input: SourceCandidateReviewInput): Promise<SourceCandidate> {
      const selectedProfile = requireIdentifier(profileId, 'profile identifier');
      const selectedCandidate = requireIdentifier(candidateId, 'candidate identifier');
      if (!input || !requireIdentifier(input.request_id, 'review request identifier')) {
        throw new SourceCandidateReviewClientError(null, 'Invalid review request.');
      }
      const url = `${baseUrl}/profiles/${encodeURIComponent(selectedProfile)}` +
        `/source-candidates/${encodeURIComponent(selectedCandidate)}/review`;
      const result = await request(url, {
        method: 'PUT',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify(input),
      });
      const updated = sanitizeCandidate(result, selectedProfile);
      if (updated.id !== selectedCandidate) {
        throw new SourceCandidateReviewClientError(null, 'Source candidate response did not match the requested candidate.');
      }
      return updated;
    },
  };
}
