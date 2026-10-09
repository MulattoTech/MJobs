/** Read-only, profile/search-scoped client for comparing selected saved matches. */

export type JobComparisonProvenance = {
  source: string;
  evidence_ref?: string;
};

export type JobComparisonContribution = {
  dimension: string;
  weight: number;
  raw_score: number | null;
  weighted_contribution: number | null;
  explanation: string;
  provenance: JobComparisonProvenance;
};

export type ComparedJob = {
  job_id: string;
  title: string | null;
  company: string | null;
  match_score: number | null;
  scoring_contributions: JobComparisonContribution[];
};

export type SharedRequirementProvenance = JobComparisonProvenance & {
  job_id: string;
};

export type SharedRequirement = {
  term: string;
  job_ids: string[];
  provenance: SharedRequirementProvenance[];
};

export type JobSpecificRequirement = {
  term: string;
  job_id: string;
  provenance: JobComparisonProvenance;
};

export type JobComparisonUnknownCategory =
  | 'required_terms' | 'match_score' | 'contributions';

export type JobComparisonUnknownReason =
  | 'REQUIREMENTS_MISSING'
  | 'REQUIREMENTS_INVALID'
  | 'REQUIREMENT_TERM_UNKNOWN'
  | 'REQUIREMENT_PROVENANCE_UNKNOWN'
  | 'MATCH_SCORE_MISSING'
  | 'MATCH_SCORE_INVALID'
  | 'SCORING_CONTRIBUTIONS_MISSING'
  | 'SCORING_CONTRIBUTIONS_INVALID';

export type JobComparisonUnknown = {
  category: JobComparisonUnknownCategory;
  job_ids: string[];
  reason: JobComparisonUnknownReason;
};

export type JobComparisonResult = {
  profile_id: string;
  search_profile_id: string;
  job_ids: string[];
  jobs: ComparedJob[];
  shared_requirements: SharedRequirement[];
  job_specific_requirements: JobSpecificRequirement[];
  unknowns: JobComparisonUnknown[];
};

export type JobComparisonRecovery = 'NONE' | 'REFRESH' | 'RETRY';
export type JobComparisonErrorKind =
  | 'INVALID_REQUEST' | 'INVALID_RESPONSE' | 'SCOPE_MISMATCH'
  | 'STALE_CONTEXT' | 'NOT_FOUND' | 'CONFLICT' | 'HTTP_ERROR' | 'NETWORK';

export class JobComparisonClientError extends Error {
  readonly state: 'ERROR' | 'STALE';

  constructor(
    readonly kind: JobComparisonErrorKind,
    readonly status: number | null,
    readonly recovery: JobComparisonRecovery,
    readonly retryable: boolean,
    message: string,
  ) {
    super(message);
    this.name = 'JobComparisonClientError';
    this.state = kind === 'STALE_CONTEXT' ? 'STALE' : 'ERROR';
  }
}

export type JobComparisonClient = {
  compare(
    profileId: string,
    searchProfileId: string,
    jobIds: string[],
  ): Promise<JobComparisonResult>;
};

const MIN_SELECTED_JOBS = 2;
const MAX_SELECTED_JOBS = 10;
const MAX_ID_LENGTH = 128;
const MAX_TEXT_LENGTH = 4000;

const REASONS_BY_CATEGORY: Record<JobComparisonUnknownCategory, readonly string[]> = {
  required_terms: [
    'REQUIREMENTS_MISSING', 'REQUIREMENTS_INVALID',
    'REQUIREMENT_TERM_UNKNOWN', 'REQUIREMENT_PROVENANCE_UNKNOWN',
  ],
  match_score: ['MATCH_SCORE_MISSING', 'MATCH_SCORE_INVALID'],
  contributions: ['SCORING_CONTRIBUTIONS_MISSING', 'SCORING_CONTRIBUTIONS_INVALID'],
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).sort().join(',') === [...keys].sort().join(',');
}

function fail(
  kind: JobComparisonErrorKind,
  message: string,
  status: number | null = null,
  recovery: JobComparisonRecovery = 'NONE',
  retryable = false,
): JobComparisonClientError {
  return new JobComparisonClientError(kind, status, recovery, retryable, message);
}

function invalidResponse(): JobComparisonClientError {
  return fail('INVALID_RESPONSE', 'Job comparison service returned an invalid response.');
}

function scopeMismatch(): JobComparisonClientError {
  return fail('SCOPE_MISMATCH', 'Job comparison does not match the selected profile, search, and jobs.');
}

function requireIdentifier(value: string, name: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() ||
      value.length > MAX_ID_LENGTH || value === '.' || value === '..' ||
      /[\\/\u0000-\u001f\u007f]/.test(value)) {
    throw fail('INVALID_REQUEST', `Invalid ${name}.`);
  }
  return value;
}

function text(value: unknown, maxLength = MAX_TEXT_LENGTH): value is string {
  return typeof value === 'string' && !!value.trim() && value.length <= maxLength;
}

function score(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

function nullableScore(value: unknown): value is number | null {
  return value === null || score(value);
}

function normalizeProvenance(value: unknown): JobComparisonProvenance {
  if (!isRecord(value) ||
      Object.keys(value).some(key => !['source', 'evidence_ref'].includes(key)) ||
      !text(value.source, 128) ||
      ('evidence_ref' in value && !text(value.evidence_ref, 2000))) {
    throw invalidResponse();
  }
  const result: JobComparisonProvenance = { source: value.source };
  if ('evidence_ref' in value) result.evidence_ref = value.evidence_ref as string;
  return result;
}

function normalizeContribution(value: unknown): JobComparisonContribution {
  if (!isRecord(value) || !hasExactKeys(value, [
    'dimension', 'weight', 'raw_score', 'weighted_contribution', 'explanation', 'provenance',
  ]) || !text(value.dimension, 100) || !score(value.weight) ||
      !nullableScore(value.raw_score) || !nullableScore(value.weighted_contribution) ||
      !text(value.explanation)) {
    throw invalidResponse();
  }
  const provenance = normalizeProvenance(value.provenance);
  const unknown = value.raw_score === null && value.weighted_contribution === null &&
    provenance.source === 'UNKNOWN';
  if ((value.raw_score === null || value.weighted_contribution === null) !== unknown ||
      (provenance.source === 'UNKNOWN') !== unknown) {
    throw invalidResponse();
  }
  return {
    dimension: value.dimension,
    weight: value.weight,
    raw_score: value.raw_score,
    weighted_contribution: value.weighted_contribution,
    explanation: value.explanation,
    provenance,
  };
}

function selectedOrder(values: unknown, selectedJobIds: string[]): string[] {
  if (!Array.isArray(values) || values.length === 0) throw invalidResponse();
  const result = values.map(value => {
    if (!text(value, MAX_ID_LENGTH)) throw invalidResponse();
    return value;
  });
  if (new Set(result).size !== result.length || result.some(id => !selectedJobIds.includes(id))) {
    throw scopeMismatch();
  }
  const positions = result.map(id => selectedJobIds.indexOf(id));
  if (positions.some((position, index) => index > 0 && position <= positions[index - 1])) {
    throw scopeMismatch();
  }
  return result;
}

function normalizeSharedRequirement(value: unknown, selectedJobIds: string[]): SharedRequirement {
  if (!isRecord(value) || !hasExactKeys(value, ['term', 'job_ids', 'provenance']) ||
      !text(value.term, 160) || !Array.isArray(value.provenance) || value.provenance.length === 0) {
    throw invalidResponse();
  }
  const jobIds = selectedOrder(value.job_ids, selectedJobIds);
  if (jobIds.length < 2) throw invalidResponse();
  const provenance = value.provenance.map(row => {
    if (!isRecord(row) ||
        !hasExactKeys(row, ['job_id', 'source']) &&
        !hasExactKeys(row, ['job_id', 'source', 'evidence_ref'])) {
      throw invalidResponse();
    }
    if (!text(row.job_id, MAX_ID_LENGTH) || !jobIds.includes(row.job_id)) throw scopeMismatch();
    return { job_id: row.job_id, ...normalizeProvenance({
      source: row.source,
      ...('evidence_ref' in row ? { evidence_ref: row.evidence_ref } : {}),
    }) };
  });
  if (jobIds.some(jobId => !provenance.some(row => row.job_id === jobId))) throw invalidResponse();
  return { term: value.term, job_ids: jobIds, provenance };
}

function normalizeJobSpecificRequirement(
  value: unknown,
  selectedJobIds: string[],
): JobSpecificRequirement {
  if (!isRecord(value) || !hasExactKeys(value, ['term', 'job_id', 'provenance']) ||
      !text(value.term, 160) || !text(value.job_id, MAX_ID_LENGTH)) {
    throw invalidResponse();
  }
  if (!selectedJobIds.includes(value.job_id)) throw scopeMismatch();
  return {
    term: value.term,
    job_id: value.job_id,
    provenance: normalizeProvenance(value.provenance),
  };
}

function normalizeUnknown(value: unknown, selectedJobIds: string[]): JobComparisonUnknown {
  if (!isRecord(value) || !hasExactKeys(value, ['category', 'job_ids', 'reason']) ||
      !['required_terms', 'match_score', 'contributions'].includes(String(value.category)) ||
      typeof value.reason !== 'string') {
    throw invalidResponse();
  }
  const category = value.category as JobComparisonUnknownCategory;
  if (!REASONS_BY_CATEGORY[category].includes(value.reason)) throw invalidResponse();
  return {
    category,
    job_ids: selectedOrder(value.job_ids, selectedJobIds),
    reason: value.reason as JobComparisonUnknownReason,
  };
}

function normalizeResult(
  value: unknown,
  profileId: string,
  searchProfileId: string,
  selectedJobIds: string[],
): JobComparisonResult {
  if (!isRecord(value)) throw invalidResponse();
  if (typeof value.profile_id !== 'string' || typeof value.search_profile_id !== 'string') {
    throw invalidResponse();
  }
  if (value.profile_id !== profileId || value.search_profile_id !== searchProfileId) {
    throw scopeMismatch();
  }
  if (!hasExactKeys(value, [
    'profile_id', 'search_profile_id', 'job_ids', 'jobs', 'shared_requirements',
    'job_specific_requirements', 'unknowns',
  ])) {
    throw invalidResponse();
  }
  if (!Array.isArray(value.job_ids)) throw invalidResponse();
  if (value.job_ids.length !== selectedJobIds.length ||
      value.job_ids.some((jobId, index) => jobId !== selectedJobIds[index])) {
    throw scopeMismatch();
  }
  if (!Array.isArray(value.jobs) || value.jobs.length !== selectedJobIds.length ||
      !Array.isArray(value.shared_requirements) || !Array.isArray(value.job_specific_requirements) ||
      !Array.isArray(value.unknowns)) {
    throw invalidResponse();
  }

  const jobs = value.jobs.map((row, index): ComparedJob => {
    if (!isRecord(row)) throw invalidResponse();
    if (typeof row.job_id !== 'string') throw invalidResponse();
    if (row.job_id !== selectedJobIds[index]) throw scopeMismatch();
    if (!hasExactKeys(row, [
      'job_id', 'title', 'company', 'match_score', 'scoring_contributions',
    ]) || !(row.title === null || text(row.title)) ||
        !(row.company === null || text(row.company)) ||
        !nullableScore(row.match_score) || !Array.isArray(row.scoring_contributions)) {
      throw invalidResponse();
    }
    return {
      job_id: row.job_id,
      title: row.title as string | null,
      company: row.company as string | null,
      match_score: row.match_score,
      scoring_contributions: row.scoring_contributions.map(normalizeContribution),
    };
  });
  const sharedRequirements = value.shared_requirements.map(
    row => normalizeSharedRequirement(row, selectedJobIds),
  );
  const jobSpecificRequirements = value.job_specific_requirements.map(
    row => normalizeJobSpecificRequirement(row, selectedJobIds),
  );
  const unknowns = value.unknowns.map(row => normalizeUnknown(row, selectedJobIds));

  const unknownByCategoryAndJob = new Set<string>();
  const seenUnknownRows = new Set<string>();
  for (const unknown of unknowns) {
    for (const jobId of unknown.job_ids) {
      const categoryJobKey = `${unknown.category}\u0000${jobId}`;
      const rowKey = `${categoryJobKey}\u0000${unknown.reason}`;
      if (seenUnknownRows.has(rowKey)) throw invalidResponse();
      seenUnknownRows.add(rowKey);
      unknownByCategoryAndJob.add(categoryJobKey);
    }
  }
  for (const job of jobs) {
    const hasUnknownScore = unknownByCategoryAndJob.has(`match_score\u0000${job.job_id}`);
    const hasUnknownContributions = unknownByCategoryAndJob.has(`contributions\u0000${job.job_id}`);
    if ((job.match_score === null) !== hasUnknownScore ||
        (hasUnknownContributions && job.scoring_contributions.length > 0)) {
      throw invalidResponse();
    }
  }

  return {
    profile_id: profileId,
    search_profile_id: searchProfileId,
    job_ids: [...selectedJobIds],
    jobs,
    shared_requirements: sharedRequirements,
    job_specific_requirements: jobSpecificRequirements,
    unknowns,
  };
}

function errorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') {
    return null;
  }
  return value.detail.code;
}

async function httpError(response: Response): Promise<JobComparisonClientError> {
  let code: string | null = null;
  try {
    code = errorCode(await response.json() as unknown);
  } catch {
    // Status-based fallback remains safe for non-JSON error bodies.
  }
  if (response.status === 409 && code === 'JOB_COMPARISON_SCOPE_MISMATCH') {
    return fail('SCOPE_MISMATCH', 'The comparison no longer matches the selected profile and search.', 409);
  }
  if (response.status === 409 && code === 'JOB_COMPARISON_STALE_CONTEXT') {
    return fail(
      'STALE_CONTEXT', 'The saved job matches changed. Refresh the selected comparison.',
      409, 'REFRESH', false,
    );
  }
  if (response.status === 404) {
    return fail('NOT_FOUND', 'The selected profile, search, or job is no longer available.', 404);
  }
  if (response.status === 422) {
    return fail('INVALID_REQUEST', 'The selected job comparison was rejected as invalid.', 422);
  }
  if (response.status === 409) {
    return fail('CONFLICT', 'The selected job comparison conflicts with current data.', 409);
  }
  const retryable = response.status === 408 || response.status === 429 || response.status >= 500;
  return fail(
    'HTTP_ERROR', `Job comparison request failed (${response.status}).`, response.status,
    retryable ? 'RETRY' : 'NONE', retryable,
  );
}

export function createJobComparisonClient(
  fetchImpl: typeof fetch = fetch,
): JobComparisonClient {
  return {
    async compare(profileId, searchProfileId, jobIds) {
      const selectedProfile = requireIdentifier(profileId, 'profile identifier');
      const selectedSearch = requireIdentifier(searchProfileId, 'saved-search identifier');
      if (!Array.isArray(jobIds) || jobIds.length < MIN_SELECTED_JOBS ||
          jobIds.length > MAX_SELECTED_JOBS) {
        throw fail('INVALID_REQUEST', 'Select between two and ten jobs to compare.');
      }
      const selectedJobs = jobIds.map(jobId => requireIdentifier(jobId, 'job identifier'));
      if (new Set(selectedJobs).size !== selectedJobs.length) {
        throw fail('INVALID_REQUEST', 'Selected job identifiers must be unique.');
      }
      const url = `/profiles/${encodeURIComponent(selectedProfile)}` +
        `/search-profiles/${encodeURIComponent(selectedSearch)}/jobs/compare`;
      let response: Response;
      try {
        response = await fetchImpl(url, {
          method: 'POST',
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          body: JSON.stringify({ job_ids: selectedJobs }),
        });
      } catch {
        throw fail('NETWORK', 'Job comparison service could not be reached.', null, 'RETRY', true);
      }
      if (!response.ok) throw await httpError(response);
      let body: unknown;
      try {
        body = await response.json() as unknown;
      } catch {
        throw invalidResponse();
      }
      return normalizeResult(body, selectedProfile, selectedSearch, selectedJobs);
    },
  };
}
