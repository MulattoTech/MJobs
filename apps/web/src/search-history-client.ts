import type { SearchHistoryPanelProps, SearchHistoryRecord, SearchHistoryResponse } from './search-history-panel';

export type SearchHistoryClientErrorKind =
  | 'INVALID_REQUEST'
  | 'HTTP_ERROR'
  | 'NETWORK'
  | 'INVALID_RESPONSE'
  | 'SCOPE_MISMATCH';

export class SearchHistoryClientError extends Error {
  constructor(
    readonly kind: SearchHistoryClientErrorKind,
    readonly status: number | null,
    readonly retryable: boolean,
    message: string,
  ) {
    super(message);
    this.name = 'SearchHistoryClientError';
  }
}

export type SearchHistoryPageOptions = {
  limit?: number;
  cursor?: string | null;
};

export type SearchHistoryClientOptions = {
  /** Empty by default for same-origin deployments. */
  baseUrl?: string;
  fetchImpl?: typeof fetch;
};

export type SearchHistoryClient = {
  /** Compatible with SearchHistoryPanel.loadHistory; callers may request later pages. */
  loadHistory: SearchHistoryPanelProps['loadHistory'] &
    ((profileId: string, page?: SearchHistoryPageOptions) => Promise<SearchHistoryResponse>);
};

type ApiSearchHistoryRecord = Omit<SearchHistoryRecord, 'errors' | 'error_detail'> & {
  errors: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function invalidRequest(message: string): SearchHistoryClientError {
  return new SearchHistoryClientError('INVALID_REQUEST', null, false, message);
}

function invalidResponse(): SearchHistoryClientError {
  return new SearchHistoryClientError(
    'INVALID_RESPONSE', null, false,
    'Search history service returned an invalid response.',
  );
}

function scopeMismatch(): SearchHistoryClientError {
  return new SearchHistoryClientError(
    'SCOPE_MISMATCH', null, false,
    'Search history response does not match the selected profile.',
  );
}

function requireProfileId(value: string): string {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim() || value.length > 128 ||
      value === '.' || value === '..' || /[\\/\u0000-\u001f\u007f]/.test(value)) {
    throw invalidRequest('Invalid profile identifier.');
  }
  return value;
}

function validatePageOptions(options: SearchHistoryPageOptions = {}): Required<Pick<SearchHistoryPageOptions, 'limit'>> &
  Pick<SearchHistoryPageOptions, 'cursor'> {
  const limit = options.limit ?? 50;
  const cursor = options.cursor;
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    throw invalidRequest('Search history page size must be between 1 and 100.');
  }
  if (cursor !== undefined && cursor !== null &&
      (typeof cursor !== 'string' || cursor.length === 0 || cursor.length > 1024 ||
        /[\u0000-\u001f\u007f]/.test(cursor))) {
    throw invalidRequest('Invalid search history page cursor.');
  }
  return { limit, cursor };
}

function nonemptyText(value: unknown, maxLength: number): value is string {
  return typeof value === 'string' && !!value.trim() && value.length <= maxLength &&
    !/[\u0000-\u001f\u007f]/.test(value);
}

function timestampOrNull(value: unknown): value is string | null {
  return value === null || (typeof value === 'string' && value.length <= 100 &&
    Number.isFinite(Date.parse(value)) && !/[\u0000-\u001f\u007f]/.test(value));
}

function nonnegativeCountOrNull(value: unknown): value is number | null {
  return value === null || (typeof value === 'number' && Number.isSafeInteger(value) && value >= 0);
}

function normalizeRecord(value: unknown, profileId: string): SearchHistoryRecord {
  if (!isRecord(value) ||
      !nonemptyText(value.id, 128) ||
      value.profile_id !== profileId ||
      !nonemptyText(value.source, 200) ||
      typeof value.query !== 'string' || value.query.length > 2000 || /[\u0000-\u001f\u007f]/.test(value.query) ||
      !(value.filters === null || isRecord(value.filters)) ||
      !timestampOrNull(value.started_at) || !timestampOrNull(value.finished_at) ||
      !nonnegativeCountOrNull(value.results) || !nonnegativeCountOrNull(value.new_jobs) ||
      !nonnegativeCountOrNull(value.duplicates) || !Array.isArray(value.errors) || value.errors.length > 100 ||
      !value.errors.every(error => typeof error === 'string' && !!error.trim() && error.length <= 1000) ||
      !['COMPLETED', 'FAILED', 'CANCELLED'].includes(String(value.status))) {
    throw invalidResponse();
  }
  const apiRecord = value as unknown as ApiSearchHistoryRecord;
  return {
    id: apiRecord.id,
    profile_id: profileId,
    source: apiRecord.source,
    query: apiRecord.query,
    filters: apiRecord.filters,
    started_at: apiRecord.started_at,
    finished_at: apiRecord.finished_at,
    results: apiRecord.results,
    new_jobs: apiRecord.new_jobs,
    duplicates: apiRecord.duplicates,
    errors: apiRecord.errors.length,
    status: apiRecord.status,
    error_detail: apiRecord.errors.length ? `${apiRecord.errors.length} recorded error${apiRecord.errors.length === 1 ? '' : 's'}.` : null,
  };
}

function normalizeResponse(
  value: unknown,
  profileId: string,
  limit: number,
  requestedCursor: string | null | undefined,
): SearchHistoryResponse {
  if (!isRecord(value)) throw invalidResponse();
  if (value.profile_id !== profileId) throw scopeMismatch();
  if (!Array.isArray(value.items) || value.items.length > limit) throw invalidResponse();
  if (!(value.next_cursor === null ||
      (typeof value.next_cursor === 'string' && value.next_cursor.length > 0 &&
        value.next_cursor.length <= 1024 && !/[\u0000-\u001f\u007f]/.test(value.next_cursor)))) {
    throw invalidResponse();
  }
  if (typeof requestedCursor === 'string' && value.next_cursor === requestedCursor) throw invalidResponse();
  const items = value.items.map(item => normalizeRecord(item, profileId));
  if (new Set(items.map(item => item.id)).size !== items.length) throw invalidResponse();
  return {
    profile_id: profileId,
    items,
    next_cursor: value.next_cursor as string | null,
  };
}

function httpError(status: number): SearchHistoryClientError {
  const retryable = status === 408 || status === 429 || status >= 500;
  const message = status === 404
    ? 'Search history is unavailable for the selected profile.'
    : `Search history request failed (${status}).${retryable ? ' Retry when the service is available.' : ''}`;
  return new SearchHistoryClientError('HTTP_ERROR', status, retryable, message);
}

export function createSearchHistoryClient(
  options: SearchHistoryClientOptions = {},
): SearchHistoryClient {
  const baseUrl = (options.baseUrl ?? '').replace(/\/+$/, '');
  if (baseUrl) {
    try {
      const parsed = new URL(baseUrl);
      if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
          parsed.search || parsed.hash) throw new Error();
    } catch {
      throw invalidRequest('Invalid search history service URL.');
    }
  }
  const fetchImpl = options.fetchImpl ?? fetch;

  return {
    async loadHistory(profileId: string, page: SearchHistoryPageOptions = {}): Promise<SearchHistoryResponse> {
      const selectedProfile = requireProfileId(profileId);
      const { limit, cursor } = validatePageOptions(page);
      const query = new URLSearchParams({ limit: String(limit) });
      if (cursor !== undefined && cursor !== null) query.set('cursor', cursor);
      const url = `${baseUrl}/profiles/${encodeURIComponent(selectedProfile)}/search-history?${query.toString()}`;
      let response: Response;
      try {
        response = await fetchImpl(url, { method: 'GET', headers: { accept: 'application/json' } });
      } catch {
        throw new SearchHistoryClientError(
          'NETWORK', null, true,
          'Search history service could not be reached. Retry when the connection is available.',
        );
      }
      if (!response.ok) throw httpError(response.status);
      let body: unknown;
      try {
        body = await response.json() as unknown;
      } catch {
        throw invalidResponse();
      }
      return normalizeResponse(body, selectedProfile, limit, cursor);
    },
  };
}
