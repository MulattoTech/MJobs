/** Read-only adapter for the selected profile/job's advisory priority projection. */

export type ApplicationPriorityFactorStatus = 'KNOWN' | 'UNKNOWN';
export type ApplicationPriorityBand = 'UNKNOWN' | 'LOW' | 'NORMAL' | 'HIGH';

export type ApplicationPriorityFactor = {
  key: string;
  label: string;
  score: number | null;
  contribution: number | null;
  status: ApplicationPriorityFactorStatus;
  explanation: string;
  source: string;
};

export type ApplicationPriority = {
  profile_id: string;
  job_id: string;
  match_score: number;
  priority_score: number | null;
  priority_band: ApplicationPriorityBand;
  coverage_percent: number;
  factors: ApplicationPriorityFactor[];
  unknowns: string[];
  advisory: true;
  affects_match_score: false;
  /** UNKNOWN is a successful projection with insufficient known priority inputs. */
  state: 'READY' | 'UNKNOWN';
};

export type ApplicationPriorityRecovery = 'NONE' | 'REFRESH' | 'RETRY';
export type ApplicationPriorityErrorKind =
  | 'NOT_FOUND' | 'STALE' | 'HTTP_ERROR' | 'NETWORK' | 'INVALID_REQUEST' | 'INVALID_RESPONSE';

export class ApplicationPriorityClientError extends Error {
  readonly state: 'ERROR' | 'STALE';

  constructor(
    readonly kind: ApplicationPriorityErrorKind,
    readonly status: number | null,
    readonly recovery: ApplicationPriorityRecovery,
    readonly retryable: boolean,
    message: string,
  ) {
    super(message);
    this.name = 'ApplicationPriorityClientError';
    this.state = kind === 'STALE' ? 'STALE' : 'ERROR';
  }
}

export type ApplicationPriorityClientOptions = {
  /** Empty by default for same-origin deployments. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export type ApplicationPriorityClient = {
  load(profileId: string, jobId: string): Promise<ApplicationPriority>;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function invalidResponse(): ApplicationPriorityClientError {
  return new ApplicationPriorityClientError(
    'INVALID_RESPONSE', null, 'NONE', false,
    'Application priority service returned an invalid response.',
  );
}

function requireIdentifier(value: string, name: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > 128 ||
      value === '.' || value === '..' || /[\\/\u0000-\u001f\u007f]/.test(value)) {
    throw new ApplicationPriorityClientError(
      'INVALID_REQUEST', null, 'NONE', false, `Invalid ${name}.`,
    );
  }
  return value;
}

function finiteScore(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100;
}

function nonemptyText(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && !!value.trim() && value.length <= maxLength;
}

function normalizeFactor(value: unknown): ApplicationPriorityFactor {
  if (!isRecord(value) ||
      Object.keys(value).sort().join(',') !== 'contribution,explanation,key,label,score,source,status' ||
      !nonemptyText(value.key, 100) || !nonemptyText(value.label, 200) ||
      !nonemptyText(value.explanation, 2000) || !nonemptyText(value.source, 200) ||
      (value.status !== 'KNOWN' && value.status !== 'UNKNOWN')) {
    throw invalidResponse();
  }
  if (value.status === 'UNKNOWN') {
    if (value.score !== null || value.contribution !== null) throw invalidResponse();
  } else if (!finiteScore(value.score) || !finiteScore(value.contribution)) {
    throw invalidResponse();
  }
  return {
    key: value.key, label: value.label, score: value.score as number | null,
    contribution: value.contribution as number | null,
    status: value.status, explanation: value.explanation, source: value.source,
  };
}

function normalizePriority(value: unknown, profileId: string, jobId: string): ApplicationPriority {
  if (!isRecord(value) || value.profile_id !== profileId || value.job_id !== jobId ||
      !finiteScore(value.match_score) ||
      !(value.priority_score === null || finiteScore(value.priority_score)) ||
      !['UNKNOWN', 'LOW', 'NORMAL', 'HIGH'].includes(String(value.priority_band)) ||
      !finiteScore(value.coverage_percent) || !Array.isArray(value.factors) ||
      value.factors.length > 32 || !Array.isArray(value.unknowns) || value.unknowns.length > 32 ||
      value.advisory !== true || value.affects_match_score !== false) {
    throw invalidResponse();
  }
  const band = value.priority_band as ApplicationPriorityBand;
  if ((value.priority_score === null) !== (band === 'UNKNOWN')) throw invalidResponse();

  const factors = value.factors.map(normalizeFactor);
  const keys = factors.map(factor => factor.key);
  if (new Set(keys).size !== keys.length ||
      !value.unknowns.every(item => nonemptyText(item, 100)) ||
      new Set(value.unknowns).size !== value.unknowns.length) {
    throw invalidResponse();
  }
  const expectedUnknowns = factors.filter(factor => factor.status === 'UNKNOWN').map(factor => factor.key);
  if (JSON.stringify(value.unknowns) !== JSON.stringify(expectedUnknowns)) throw invalidResponse();

  return {
    profile_id: profileId,
    job_id: jobId,
    match_score: value.match_score,
    priority_score: value.priority_score as number | null,
    priority_band: band,
    coverage_percent: value.coverage_percent,
    factors,
    unknowns: [...expectedUnknowns],
    advisory: true,
    affects_match_score: false,
    state: band === 'UNKNOWN' ? 'UNKNOWN' : 'READY',
  };
}

function httpError(status: number): ApplicationPriorityClientError {
  if (status === 404) {
    return new ApplicationPriorityClientError(
      'NOT_FOUND', status, 'NONE', false,
      'The selected profile or job priority is no longer available (404). Refresh the selected job.',
    );
  }
  if (status === 409) {
    return new ApplicationPriorityClientError(
      'STALE', status, 'REFRESH', false,
      'The selected job or profile changed (409). Refresh before viewing priority again.',
    );
  }
  const retryable = status === 408 || status === 429 || status >= 500;
  return new ApplicationPriorityClientError(
    'HTTP_ERROR', status, retryable ? 'RETRY' : 'NONE', retryable,
    `Application priority request failed (${status}).${retryable ? ' Retry when the service is available.' : ''}`,
  );
}

export function createApplicationPriorityClient(
  options: ApplicationPriorityClientOptions = {},
): ApplicationPriorityClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
          parsed.search || parsed.hash) throw new Error();
    } catch {
      throw new ApplicationPriorityClientError(
        'INVALID_REQUEST', null, 'NONE', false, 'Invalid application priority service URL.',
      );
    }
  }
  const fetchImpl = options.fetchImpl ?? fetch;

  return {
    async load(profileId: string, jobId: string): Promise<ApplicationPriority> {
      const selectedProfile = requireIdentifier(profileId, 'profile identifier');
      const selectedJob = requireIdentifier(jobId, 'job identifier');
      const url = `${baseUrl}/profiles/${encodeURIComponent(selectedProfile)}` +
        `/jobs/${encodeURIComponent(selectedJob)}/application-priority`;
      let response: Response;
      try {
        response = await fetchImpl(url, { method: 'GET', headers: { accept: 'application/json' } });
      } catch {
        throw new ApplicationPriorityClientError(
          'NETWORK', null, 'RETRY', true,
          'Application priority service could not be reached. Retry when the connection is available.',
        );
      }
      if (!response.ok) throw httpError(response.status);
      let body: unknown;
      try {
        body = await response.json() as unknown;
      } catch {
        throw invalidResponse();
      }
      return normalizePriority(body, selectedProfile, selectedJob);
    },
  };
}
