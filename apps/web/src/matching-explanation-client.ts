/** Read-only client for a persisted, profile/search/job-scoped match explanation. */

export type MatchExplanationResolution = 'CURRENT' | 'LEGACY_DEFAULT';
export type ScoringProfileSelection = 'ROLE_FAMILY' | 'DEFAULT_PROFILE' | 'LEGACY_DEFAULT';
export type MatchExplanationContributionStatus = 'KNOWN' | 'UNKNOWN';

export type MatchExplanationProfileResolution = {
  requested_role_family: string | null;
  selected_role_family: string | null;
  profile_id: string | null;
  profile_version: string | null;
  selection: ScoringProfileSelection;
};

export type MatchExplanationScoringProfile = {
  id: string;
  version: string;
  role_family: string;
};

export type MatchExplanationProvenance = {
  source: string;
  evidence_ref?: string;
};

export type MatchExplanationContribution = {
  dimension: string;
  weight: number;
  raw_score: number | null;
  weighted_contribution: number | null;
  explanation: string;
  provenance: MatchExplanationProvenance;
  status: MatchExplanationContributionStatus;
};

export type MatchingExplanation = {
  profile_id: string;
  search_profile_id: string;
  job_id: string;
  score: number;
  resolution: MatchExplanationResolution;
  profile_resolution: MatchExplanationProfileResolution;
  scoring_profile: MatchExplanationScoringProfile | null;
  scoring_contributions: MatchExplanationContribution[];
  state: 'READY' | 'UNKNOWN';
};

export type MatchingExplanationRecovery = 'NONE' | 'REFRESH' | 'RETRY';
export type MatchingExplanationErrorKind =
  | 'INVALID_REQUEST' | 'INVALID_RESPONSE' | 'SCOPE_MISMATCH'
  | 'STALE_CONTEXT' | 'NOT_FOUND' | 'HTTP_ERROR' | 'NETWORK';

export class MatchingExplanationClientError extends Error {
  readonly state: 'ERROR' | 'STALE';

  constructor(
    readonly kind: MatchingExplanationErrorKind,
    readonly status: number | null,
    readonly recovery: MatchingExplanationRecovery,
    readonly retryable: boolean,
    message: string,
  ) {
    super(message);
    this.name = 'MatchingExplanationClientError';
    this.state = kind === 'STALE_CONTEXT' ? 'STALE' : 'ERROR';
  }
}

export type MatchingExplanationClientOptions = {
  /** Empty by default for same-origin API deployments. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export type MatchingExplanationClient = {
  load(profileId: string, searchProfileId: string, jobId: string): Promise<MatchingExplanation>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function fail(
  kind: MatchingExplanationErrorKind,
  message: string,
  status: number | null = null,
  recovery: MatchingExplanationRecovery = 'NONE',
  retryable = false,
): MatchingExplanationClientError {
  return new MatchingExplanationClientError(kind, status, recovery, retryable, message);
}

function invalidResponse(): MatchingExplanationClientError {
  return fail('INVALID_RESPONSE', 'Match explanation service returned an invalid response.');
}

function scopeMismatch(): MatchingExplanationClientError {
  return fail(
    'SCOPE_MISMATCH',
    'Match explanation does not match the selected profile, saved search, and job.',
  );
}

function requireIdentifier(value: string, name: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > 128 ||
      value === '.' || value === '..' || /[\\/\u0000-\u001f\u007f]/.test(value)) {
    throw fail('INVALID_REQUEST', `Invalid ${name}.`);
  }
  return value;
}

function text(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && !!value.trim() && value.length <= maxLength;
}

function score(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

function nullableScore(value: unknown): value is number | null {
  return value === null || score(value);
}

function normalizeProfileResolution(value: unknown): MatchExplanationProfileResolution {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !==
        'profile_id,profile_version,requested_role_family,selected_role_family,selection' ||
      !(value.requested_role_family === null || text(value.requested_role_family, 100)) ||
      !(value.selected_role_family === null || text(value.selected_role_family, 100)) ||
      !(value.profile_id === null || text(value.profile_id, 200)) ||
      !(value.profile_version === null || text(value.profile_version, 200)) ||
      !['ROLE_FAMILY', 'DEFAULT_PROFILE', 'LEGACY_DEFAULT'].includes(String(value.selection))) {
    throw invalidResponse();
  }
  return {
    requested_role_family: value.requested_role_family as string | null,
    selected_role_family: value.selected_role_family as string | null,
    profile_id: value.profile_id as string | null,
    profile_version: value.profile_version as string | null,
    selection: value.selection as ScoringProfileSelection,
  };
}

function normalizeScoringProfile(value: unknown): MatchExplanationScoringProfile {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !== 'id,role_family,version' ||
      !text(value.id, 200) || !text(value.version, 200) || !text(value.role_family, 100)) {
    throw invalidResponse();
  }
  return { id: value.id, version: value.version, role_family: value.role_family };
}

function normalizeProvenance(value: unknown): MatchExplanationProvenance {
  if (!isRecord(value) || Object.keys(value).some(key => !['source', 'evidence_ref'].includes(key)) ||
      !text(value.source, 128) ||
      ('evidence_ref' in value && !text(value.evidence_ref, 2000))) {
    throw invalidResponse();
  }
  const result: MatchExplanationProvenance = { source: value.source };
  if ('evidence_ref' in value) result.evidence_ref = value.evidence_ref as string;
  return result;
}

function normalizeContribution(value: unknown): MatchExplanationContribution {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !==
        'dimension,explanation,provenance,raw_score,weight,weighted_contribution' ||
      !text(value.dimension, 100) || !score(value.weight) ||
      !nullableScore(value.raw_score) || !nullableScore(value.weighted_contribution) ||
      !text(value.explanation, 4000)) {
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
    status: unknown ? 'UNKNOWN' : 'KNOWN',
  };
}

function normalizeExplanation(
  value: unknown,
  profileId: string,
  searchProfileId: string,
  jobId: string,
): MatchingExplanation {
  if (!isRecord(value)) throw invalidResponse();
  if (value.profile_id !== profileId || value.search_profile_id !== searchProfileId ||
      value.job_id !== jobId) throw scopeMismatch();
  if (!score(value.score) || !['CURRENT', 'LEGACY_DEFAULT'].includes(String(value.resolution)) ||
      !Array.isArray(value.scoring_contributions) || value.scoring_contributions.length > 64) {
    throw invalidResponse();
  }

  const profileResolution = normalizeProfileResolution(value.profile_resolution);
  const selection = profileResolution.selection;
  let scoringProfile: MatchExplanationScoringProfile | null;
  if (value.resolution === 'CURRENT') {
    if (selection === 'LEGACY_DEFAULT' || profileResolution.profile_id === null ||
        profileResolution.profile_version === null || profileResolution.selected_role_family === null ||
        value.scoring_profile === null) throw invalidResponse();
    scoringProfile = normalizeScoringProfile(value.scoring_profile);
    if (scoringProfile.id !== profileResolution.profile_id ||
        scoringProfile.version !== profileResolution.profile_version ||
        scoringProfile.role_family !== profileResolution.selected_role_family) throw invalidResponse();
  } else {
    if (selection !== 'LEGACY_DEFAULT' || profileResolution.profile_id !== null ||
        profileResolution.profile_version !== null || profileResolution.selected_role_family !== null ||
        value.scoring_profile !== null) throw invalidResponse();
    scoringProfile = null;
  }

  const contributions = value.scoring_contributions.map(normalizeContribution);
  const dimensions = contributions.map(contribution => contribution.dimension);
  if (new Set(dimensions).size !== dimensions.length) throw invalidResponse();

  return {
    profile_id: profileId,
    search_profile_id: searchProfileId,
    job_id: jobId,
    score: value.score,
    resolution: value.resolution as MatchExplanationResolution,
    profile_resolution: profileResolution,
    scoring_profile: scoringProfile,
    scoring_contributions: contributions,
    state: contributions.some(contribution => contribution.status === 'UNKNOWN') ? 'UNKNOWN' : 'READY',
  };
}

function httpError(status: number): MatchingExplanationClientError {
  if (status === 404) {
    return fail(
      'NOT_FOUND', 'The selected profile, saved search, job, or saved match is no longer available.',
      status,
    );
  }
  if (status === 409) {
    return fail(
      'STALE_CONTEXT', 'The saved match explanation is out of date. Refresh the selected job.',
      status, 'REFRESH', false,
    );
  }
  const retryable = status === 408 || status === 429 || status >= 500;
  return fail(
    'HTTP_ERROR', `Match explanation request failed (${status}).`, status,
    retryable ? 'RETRY' : 'NONE', retryable,
  );
}

export function createMatchingExplanationClient(
  options: MatchingExplanationClientOptions = {},
): MatchingExplanationClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
          parsed.search || parsed.hash) throw new Error();
    } catch {
      throw fail('INVALID_REQUEST', 'Invalid match explanation service URL.');
    }
  }
  const fetchImpl = options.fetchImpl ?? fetch;

  return {
    async load(profileId, searchProfileId, jobId) {
      const selectedProfile = requireIdentifier(profileId, 'profile identifier');
      const selectedSearch = requireIdentifier(searchProfileId, 'saved-search identifier');
      const selectedJob = requireIdentifier(jobId, 'job identifier');
      const url = `${baseUrl}/profiles/${encodeURIComponent(selectedProfile)}` +
        `/search-profiles/${encodeURIComponent(selectedSearch)}` +
        `/jobs/${encodeURIComponent(selectedJob)}/match-explanation`;
      let response: Response;
      try {
        response = await fetchImpl(url, { method: 'GET', headers: { accept: 'application/json' } });
      } catch {
        throw fail(
          'NETWORK', 'Match explanation service could not be reached.', null, 'RETRY', true,
        );
      }
      if (!response.ok) throw httpError(response.status);
      let body: unknown;
      try {
        body = await response.json() as unknown;
      } catch {
        throw invalidResponse();
      }
      return normalizeExplanation(body, selectedProfile, selectedSearch, selectedJob);
    },
  };
}
