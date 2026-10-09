/** Typed, saved-search-scoped client for editable role-scoring profiles. */

export type RoleScoringProfileDraft = {
  schema_version: 1;
  role_family: string;
  weights: Record<string, number>;
  thresholds: Record<string, number>;
};

export type SavedRoleScoringProfile = RoleScoringProfileDraft & {
  id: string;
  version: string;
};

export type RoleScoringProfileRecovery = 'NONE' | 'RELOAD' | 'RETRY';
export type RoleScoringProfileErrorKind =
  | 'INVALID_REQUEST' | 'INVALID_RESPONSE' | 'SCOPE_MISMATCH'
  | 'STALE_VERSION' | 'NOT_FOUND' | 'CONFLICT' | 'HTTP_ERROR' | 'NETWORK';

export class RoleScoringProfileClientError extends Error {
  readonly state: 'ERROR' | 'STALE';

  constructor(
    readonly kind: RoleScoringProfileErrorKind,
    readonly status: number | null,
    readonly recovery: RoleScoringProfileRecovery,
    readonly retryable: boolean,
    message: string,
  ) {
    super(message);
    this.name = 'RoleScoringProfileClientError';
    this.state = kind === 'STALE_VERSION' ? 'STALE' : 'ERROR';
  }
}

export type RoleScoringProfileClientOptions = {
  /** Empty by default for same-origin API deployments. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export type RoleScoringProfileClient = {
  load(profileId: string, searchProfileId: string): Promise<SavedRoleScoringProfile | null>;
  readback(profileId: string, searchProfileId: string): Promise<SavedRoleScoringProfile | null>;
  save(
    profileId: string,
    searchProfileId: string,
    draft: RoleScoringProfileDraft,
    expectedVersion: string | null,
  ): Promise<SavedRoleScoringProfile>;
};

type ScopedEnvelope = {
  profile_id: string;
  search_profile_id: string;
  profile: SavedRoleScoringProfile | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function fail(kind: RoleScoringProfileErrorKind, message: string,
  status: number | null = null, recovery: RoleScoringProfileRecovery = 'NONE',
  retryable = false): RoleScoringProfileClientError {
  return new RoleScoringProfileClientError(kind, status, recovery, retryable, message);
}

function invalidResponse(): RoleScoringProfileClientError {
  return fail('INVALID_RESPONSE', 'Role-scoring profile service returned an invalid response.');
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

function normalizeDimensions(value: unknown): Record<string, number> {
  if (!isRecord(value)) throw invalidResponse();
  const normalized: Record<string, number> = {};
  for (const [dimension, amount] of Object.entries(value)) {
    if (!dimension.trim() || dimension !== dimension.trim() || dimension.length > 100 ||
        typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0 || amount > 100) {
      throw invalidResponse();
    }
    normalized[dimension] = amount;
  }
  return normalized;
}

function normalizeProfile(value: unknown): SavedRoleScoringProfile {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !== 'id,role_family,schema_version,thresholds,version,weights' ||
      !text(value.id, 200) || !text(value.version, 200) || value.schema_version !== 1 ||
      !text(value.role_family, 100) || value.role_family !== value.role_family.trim()) {
    throw invalidResponse();
  }
  return {
    id: value.id,
    version: value.version,
    schema_version: 1,
    role_family: value.role_family,
    weights: normalizeDimensions(value.weights),
    thresholds: normalizeDimensions(value.thresholds),
  };
}

function normalizeDraft(value: unknown): RoleScoringProfileDraft {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !== 'role_family,schema_version,thresholds,weights' ||
      value.schema_version !== 1 || !text(value.role_family, 100)) {
    throw fail('INVALID_REQUEST', 'Invalid role-scoring profile draft.');
  }
  return {
    schema_version: 1,
    role_family: value.role_family.split(/\s+/).join(' '),
    weights: normalizeRequestDimensions(value.weights),
    thresholds: normalizeRequestDimensions(value.thresholds),
  };
}

function normalizeRequestDimensions(value: unknown): Record<string, number> {
  if (!isRecord(value)) throw fail('INVALID_REQUEST', 'Invalid role-scoring profile dimensions.');
  const normalized: Record<string, number> = {};
  for (const [dimension, amount] of Object.entries(value)) {
    if (!dimension.trim() || dimension !== dimension.trim() || dimension.length > 100 ||
        typeof amount !== 'number' || !Number.isFinite(amount) || amount < 0 || amount > 100) {
      throw fail('INVALID_REQUEST', 'Invalid role-scoring profile dimensions.');
    }
    normalized[dimension] = amount;
  }
  return normalized;
}

function normalizeEnvelope(value: unknown, profileId: string,
  searchProfileId: string): ScopedEnvelope {
  if (!isRecord(value) || typeof value.profile_id !== 'string' ||
      typeof value.search_profile_id !== 'string' || !('profile' in value)) {
    throw invalidResponse();
  }
  if (value.profile_id !== profileId || value.search_profile_id !== searchProfileId) {
    throw fail(
      'SCOPE_MISMATCH', nullSafeScopeMessage(), null, 'NONE', false,
    );
  }
  return {
    profile_id: profileId,
    search_profile_id: searchProfileId,
    profile: value.profile === null ? null : normalizeProfile(value.profile),
  };
}

function nullSafeScopeMessage(): string {
  return 'Role-scoring profile response does not match the selected profile and saved search.';
}

function stableErrorCode(value: unknown): string | null {
  if (!isRecord(value) || !isRecord(value.detail) || typeof value.detail.code !== 'string') return null;
  return value.detail.code;
}

async function mapHttpError(response: Response): Promise<RoleScoringProfileClientError> {
  let code: string | null = null;
  try {
    code = stableErrorCode(await response.json() as unknown);
  } catch {
    // Status-based fallback below stays safe when an error response is not JSON.
  }
  if (response.status === 409 && code === 'SCORING_PROFILE_PROFILE_MISMATCH') {
    return fail('SCOPE_MISMATCH', nullSafeScopeMessage(), 409);
  }
  if (response.status === 409 && code === 'SCORING_PROFILE_VERSION_CONFLICT') {
    return fail(
      'STALE_VERSION', 'The saved scoring profile changed. Reload it before saving again.',
      409, 'RELOAD', false,
    );
  }
  if (response.status === 404) {
    return fail('NOT_FOUND', 'The selected profile or saved search is no longer available.', 404);
  }
  if (response.status === 409) {
    return fail('CONFLICT', 'The saved scoring profile conflicts with current data.', 409);
  }
  if (response.status === 422) {
    return fail('INVALID_REQUEST', 'The role-scoring profile was rejected as invalid.', 422);
  }
  const retryable = response.status === 408 || response.status === 429 || response.status >= 500;
  return fail(
    'HTTP_ERROR', `Role-scoring profile request failed (${response.status}).`, response.status,
    retryable ? 'RETRY' : 'NONE', retryable,
  );
}

function sameDimensions(left: Record<string, number>, right: Record<string, number>): boolean {
  const keys = Object.keys(left).sort();
  return keys.length === Object.keys(right).length &&
    keys.every(key => Object.prototype.hasOwnProperty.call(right, key) && left[key] === right[key]);
}

export function createRoleScoringProfileClient(
  options: RoleScoringProfileClientOptions = {},
): RoleScoringProfileClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
          parsed.search || parsed.hash) throw new Error();
    } catch {
      throw fail('INVALID_REQUEST', 'Invalid role-scoring profile service URL.');
    }
  }
  const fetchImpl = options.fetchImpl ?? fetch;

  const scopeUrl = (profileId: string, searchProfileId: string) => {
    const profile = requireIdentifier(profileId, 'profile identifier');
    const search = requireIdentifier(searchProfileId, 'saved-search identifier');
    return `${baseUrl}/profiles/${encodeURIComponent(profile)}` +
      `/search-profiles/${encodeURIComponent(search)}/role-scoring-profile`;
  };

  const request = async (url: string, init: RequestInit): Promise<unknown> => {
    let response: Response;
    try {
      response = await fetchImpl(url, init);
    } catch {
      throw fail(
        'NETWORK', 'Role-scoring profile service could not be reached.', null, 'RETRY', true,
      );
    }
    if (!response.ok) throw await mapHttpError(response);
    try {
      return await response.json() as unknown;
    } catch {
      throw invalidResponse();
    }
  };

  const load = async (profileId: string,
    searchProfileId: string): Promise<SavedRoleScoringProfile | null> => {
    const url = scopeUrl(profileId, searchProfileId);
    const envelope = normalizeEnvelope(await request(url, {
      method: 'GET', headers: { accept: 'application/json' },
    }), profileId, searchProfileId);
    return envelope.profile;
  };

  return {
    load,
    readback: load,
    async save(profileId, searchProfileId, draft, expectedVersion) {
      const url = scopeUrl(profileId, searchProfileId);
      if (expectedVersion !== null &&
          (!text(expectedVersion, 200) || expectedVersion !== expectedVersion.trim())) {
        throw fail('INVALID_REQUEST', 'Invalid expected scoring-profile version.');
      }
      const normalizedDraft = normalizeDraft(draft);
      const raw = await request(url, {
        method: 'PUT',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ expected_version: expectedVersion, profile: normalizedDraft }),
      });
      const envelope = normalizeEnvelope(raw, profileId, searchProfileId);
      if (!envelope.profile) throw invalidResponse();
      if (expectedVersion !== null && envelope.profile.version === expectedVersion) throw invalidResponse();
      if (envelope.profile.role_family !== normalizedDraft.role_family ||
          !sameDimensions(envelope.profile.weights, normalizedDraft.weights) ||
          !sameDimensions(envelope.profile.thresholds, normalizedDraft.thresholds)) {
        throw invalidResponse();
      }
      return envelope.profile;
    },
  };
}
