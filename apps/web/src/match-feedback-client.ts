export const MATCH_FEEDBACK_CATEGORIES = [
  'GOOD_MATCH',
  'BAD_MATCH',
  'TOO_SENIOR',
  'WRONG_TECHNOLOGY',
  'WRONG_LOCATION',
  'BAD_SALARY',
  'BAD_COMPANY',
  'NOT_INTERESTED',
] as const;

export type MatchFeedbackCategory = typeof MATCH_FEEDBACK_CATEGORIES[number];
export type PreferenceProposalDimension =
  | 'overall' | 'seniority' | 'technology' | 'location' | 'salary' | 'company';
export type PreferenceProposalDirection = 'increase' | 'decrease' | 'review';

export type MatchFeedbackRecord = {
  id: string;
  category: MatchFeedbackCategory;
  created_at: string;
};

export type AppliedMatchPreference = {
  id: string;
  job_id: string;
  feedback_id: string;
  category: MatchFeedbackCategory;
  dimension: PreferenceProposalDimension;
  direction: 'INCREASE' | 'DECREASE';
  created_at: string;
};

export type DeterministicPreferenceProposal = {
  dimension: PreferenceProposalDimension;
  direction: PreferenceProposalDirection;
  explanation: string;
  review_required: true;
};

export type MatchFeedbackResult = {
  profile_id: string;
  job_id: string;
  feedback: MatchFeedbackRecord;
  items: MatchFeedbackRecord[];
  latest: MatchFeedbackRecord | null;
  replayed: boolean;
  preference_proposal: DeterministicPreferenceProposal | null;
};

export type MatchFeedbackSubmitter = {
  load?: (profileId: string, jobId: string) => Promise<{ items: MatchFeedbackRecord[]; latest: MatchFeedbackRecord | null }>;
  loadPreferences?: (profileId: string) => Promise<AppliedMatchPreference[]>;
  applyPreference?: (
    profileId: string,
    feedbackId: string,
    dimension: PreferenceProposalDimension,
    direction: Exclude<PreferenceProposalDirection, 'review'>,
    requestId: string,
  ) => Promise<AppliedMatchPreference[]>;
  submit: (
    profileId: string,
    jobId: string,
    category: MatchFeedbackCategory,
    requestId: string,
  ) => Promise<MatchFeedbackResult>;
};

export class MatchFeedbackClientError extends Error {
  constructor(
    readonly kind:
      | 'INVALID_REQUEST'
      | 'NETWORK_ERROR'
      | 'HTTP_ERROR'
      | 'SCOPE_MISMATCH'
      | 'INVALID_RESPONSE'
      | 'READBACK_NOT_CONFIRMED',
    readonly status: number | null,
    readonly retryable: boolean,
  ) {
    super(clientMessage(kind, retryable));
    this.name = 'MatchFeedbackClientError';
  }
}

function clientMessage(
  kind: MatchFeedbackClientError['kind'],
  retryable: boolean,
): string {
  if (kind === 'SCOPE_MISMATCH') return 'Feedback readback did not match the selected profile and job.';
  if (kind === 'READBACK_NOT_CONFIRMED') return 'Feedback is not yet confirmed in saved history. Retry with the same request.';
  if (kind === 'INVALID_REQUEST') return 'Select a valid profile, job, feedback category, and request ID.';
  if (retryable) return 'Feedback could not be saved. Retry with the same request.';
  return 'Feedback could not be saved. Review the selection and try again.';
}

function invalidRequest(): MatchFeedbackClientError {
  return new MatchFeedbackClientError('INVALID_REQUEST', null, false);
}

function invalidResponse(): MatchFeedbackClientError {
  return new MatchFeedbackClientError('INVALID_RESPONSE', null, false);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isText(value: unknown, maxLength = 2000): value is string {
  return typeof value === 'string' && !!value.trim() && value.length <= maxLength;
}

function isScopeId(value: unknown): value is string {
  return isText(value, 256) && !/[\u0000-\u001f\u007f]/.test(value);
}

function isCategory(value: unknown): value is MatchFeedbackCategory {
  return MATCH_FEEDBACK_CATEGORIES.includes(value as MatchFeedbackCategory);
}

function isFeedbackRecord(value: unknown): value is MatchFeedbackRecord {
  return isRecord(value)
    && isText(value.id, 256)
    && isCategory(value.category)
    && isText(value.created_at, 128)
    && Number.isFinite(Date.parse(value.created_at));
}

function isAppliedMatchPreference(value: unknown): value is AppliedMatchPreference {
  return isRecord(value)
    && isText(value.id, 256)
    && isScopeId(value.job_id)
    && isText(value.feedback_id, 256)
    && isCategory(value.category)
    && ['overall', 'seniority', 'technology', 'location', 'salary', 'company'].includes(String(value.dimension))
    && ['INCREASE', 'DECREASE'].includes(String(value.direction))
    && isText(value.created_at, 128)
    && Number.isFinite(Date.parse(value.created_at));
}

function normalizeProposal(value: unknown): DeterministicPreferenceProposal | null {
  if (value === null) return null;
  if (!isRecord(value)
    || !['overall', 'seniority', 'technology', 'location', 'salary', 'company'].includes(String(value.dimension))
    || !['increase', 'decrease', 'review'].includes(String(value.direction))
    || !isText(value.explanation, 2000)
    || value.review_required !== true) {
    throw invalidResponse();
  }
  return {
    dimension: value.dimension as PreferenceProposalDimension,
    direction: value.direction as PreferenceProposalDirection,
    explanation: value.explanation.trim(),
    review_required: true,
  };
}

function normalizePostResponse(
  value: unknown,
  profileId: string,
  jobId: string,
  category: MatchFeedbackCategory,
): {
  feedback: MatchFeedbackRecord;
  replayed: boolean;
  preferenceProposal: DeterministicPreferenceProposal | null;
} {
  if (!isRecord(value)) throw invalidResponse();
  if (value.profile_id !== profileId || value.job_id !== jobId) {
    throw new MatchFeedbackClientError('SCOPE_MISMATCH', null, false);
  }
  if (!isFeedbackRecord(value.feedback)
    || value.feedback.category !== category
    || typeof value.replayed !== 'boolean') {
    throw invalidResponse();
  }
  return {
    feedback: value.feedback,
    replayed: value.replayed,
    preferenceProposal: normalizeProposal(value.preference_proposal),
  };
}

function normalizeReadback(
  value: unknown,
  profileId: string,
  jobId: string,
  feedback?: MatchFeedbackRecord,
): { items: MatchFeedbackRecord[]; latest: MatchFeedbackRecord | null } {
  if (!isRecord(value)) throw invalidResponse();
  if (value.profile_id !== profileId || value.job_id !== jobId) {
    throw new MatchFeedbackClientError('SCOPE_MISMATCH', null, false);
  }
  if (!Array.isArray(value.items) || value.items.length > 1000) throw invalidResponse();
  const items = value.items.map(item => {
    if (!isFeedbackRecord(item)) throw invalidResponse();
    return {
      id: item.id,
      category: item.category,
      created_at: item.created_at,
    };
  });
  const rawLatest = value.latest;
  if (rawLatest !== null && !isFeedbackRecord(rawLatest)) throw invalidResponse();
  const latest = rawLatest === null ? null : {
    id: (rawLatest as MatchFeedbackRecord).id,
    category: (rawLatest as MatchFeedbackRecord).category,
    created_at: (rawLatest as MatchFeedbackRecord).created_at,
  };
  if (latest && !items.some(item => item.id === latest.id && item.category === latest.category)) {
    throw invalidResponse();
  }
  const confirmed = feedback === undefined || items.some(item => item.id === feedback.id && item.category === feedback.category);
  if (!confirmed) {
    throw new MatchFeedbackClientError('READBACK_NOT_CONFIRMED', null, true);
  }
  return { items, latest };
}

function normalizePreferenceHistory(value: unknown, profileId: string): AppliedMatchPreference[] {
  if (!isRecord(value) || value.profile_id !== profileId
    || !Array.isArray(value.items) || !isRecord(value.by_dimension)) {
    throw new MatchFeedbackClientError('SCOPE_MISMATCH', null, false);
  }
  const items = value.items.map(item => {
    if (!isAppliedMatchPreference(item)) throw invalidResponse();
    return item;
  });
  if (new Set(items.map(item => item.id)).size !== items.length) throw invalidResponse();
  const expected: Record<string, AppliedMatchPreference> = {};
  for (const item of items) expected[item.dimension] = item;
  if (JSON.stringify(expected) !== JSON.stringify(value.by_dimension)) throw invalidResponse();
  return items;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw invalidResponse();
  }
}

function checkResponse(response: Response): void {
  if (response.ok) return;
  const retryable = response.status === 408 || response.status === 429 || response.status >= 500;
  throw new MatchFeedbackClientError('HTTP_ERROR', response.status, retryable);
}

export function createMatchFeedbackClient(fetchImpl: typeof fetch = fetch): MatchFeedbackSubmitter {
  return {
    async load(profileId, jobId) {
      if (!isScopeId(profileId) || !isScopeId(jobId)) throw invalidRequest();
      const path = `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/match-feedback`;
      let response: Response;
      try { response = await fetchImpl(path, { headers: { accept: 'application/json' } }); }
      catch { throw new MatchFeedbackClientError('NETWORK_ERROR', null, true); }
      checkResponse(response);
      return normalizeReadback(await readJson(response), profileId, jobId);
    },
    async loadPreferences(profileId) {
      if (!isScopeId(profileId)) throw invalidRequest();
      let response: Response;
      try {
        response = await fetchImpl(`/profiles/${encodeURIComponent(profileId)}/match-preferences`, {
          headers: { accept: 'application/json' },
        });
      } catch { throw new MatchFeedbackClientError('NETWORK_ERROR', null, true); }
      checkResponse(response);
      return normalizePreferenceHistory(await readJson(response), profileId);
    },
    async applyPreference(profileId, feedbackId, dimension, direction, requestId) {
      if (!isScopeId(profileId) || !isText(feedbackId, 256)
        || !['overall', 'seniority', 'technology', 'location', 'salary', 'company'].includes(dimension)
        || !['increase', 'decrease'].includes(direction)
        || !/^[A-Za-z0-9._:-]{1,128}$/.test(requestId)) throw invalidRequest();
      const path = `/profiles/${encodeURIComponent(profileId)}/match-preferences`;
      let response: Response;
      try {
        response = await fetchImpl(path, {
          method: 'POST',
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          body: JSON.stringify({
            feedback_id: feedbackId, dimension, direction, request_id: requestId,
          }),
        });
      } catch { throw new MatchFeedbackClientError('NETWORK_ERROR', null, true); }
      checkResponse(response);
      const posted = await readJson(response);
      const preference = isRecord(posted) ? posted.preference : null;
      if (!isRecord(posted) || posted.profile_id !== profileId || typeof posted.replayed !== 'boolean'
        || !isAppliedMatchPreference(preference)
        || preference.feedback_id !== feedbackId
        || preference.dimension !== dimension
        || preference.direction !== direction.toUpperCase()) {
        throw new MatchFeedbackClientError('SCOPE_MISMATCH', null, false);
      }

      let readbackResponse: Response;
      try { readbackResponse = await fetchImpl(path, { headers: { accept: 'application/json' } }); }
      catch { throw new MatchFeedbackClientError('NETWORK_ERROR', null, true); }
      checkResponse(readbackResponse);
      const readback = normalizePreferenceHistory(await readJson(readbackResponse), profileId);
      if (!readback.some(item => item.id === preference.id
        && item.feedback_id === feedbackId && item.dimension === dimension
        && item.direction === direction.toUpperCase())) {
        throw new MatchFeedbackClientError('READBACK_NOT_CONFIRMED', null, true);
      }
      return readback;
    },
    async submit(profileId, jobId, category, requestId) {
      if (!isScopeId(profileId) || !isScopeId(jobId) || !isCategory(category)
        || !/^[A-Za-z0-9._:-]{1,128}$/.test(requestId)) {
        throw invalidRequest();
      }
      const path = `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/match-feedback`;
      let postResponse: Response;
      try {
        postResponse = await fetchImpl(path, {
          method: 'POST',
          headers: { accept: 'application/json', 'content-type': 'application/json' },
          body: JSON.stringify({ category, request_id: requestId }),
        });
      } catch {
        throw new MatchFeedbackClientError('NETWORK_ERROR', null, true);
      }
      checkResponse(postResponse);
      const posted = normalizePostResponse(await readJson(postResponse), profileId, jobId, category);

      let readbackResponse: Response;
      try {
        readbackResponse = await fetchImpl(path, {
          method: 'GET',
          headers: { accept: 'application/json' },
        });
      } catch {
        throw new MatchFeedbackClientError('NETWORK_ERROR', null, true);
      }
      checkResponse(readbackResponse);
      const readback = normalizeReadback(
        await readJson(readbackResponse), profileId, jobId, posted.feedback,
      );
      const feedback = readback.items.find(item => item.id === posted.feedback.id
        && item.category === posted.feedback.category);
      if (!feedback) throw new MatchFeedbackClientError('READBACK_NOT_CONFIRMED', null, true);
      return {
        profile_id: profileId,
        job_id: jobId,
        feedback,
        items: readback.items,
        latest: readback.latest,
        replayed: posted.replayed,
        preference_proposal: posted.preferenceProposal,
      };
    },
  };
}
