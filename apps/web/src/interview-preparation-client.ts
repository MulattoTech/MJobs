/** Profile- and job-scoped client for the persisted interview-preparation API. */
export type InterviewPrepQuestion = {
  id: string;
  prompt: string;
  source_refs: string[];
};

export type InterviewPrepTalkingPoint = {
  text: string;
  source_refs: string[];
};

export type InterviewPrepStarPrompt = {
  question: string;
  situation_prompt: string;
  task_prompt: string;
  action_prompt: string;
  result_prompt: string;
  source_refs: string[];
};

export type InterviewPrepPack = {
  schema_version: 1;
  pack_id: string;
  pack_version: number;
  profile_id: string;
  profile_version: string;
  job_id: string;
  job_fingerprint: string;
  created_at: string;
  questions: InterviewPrepQuestion[];
  talking_points: InterviewPrepTalkingPoint[];
  star_prompts: InterviewPrepStarPrompt[];
  missing_evidence: Array<{topic: string; reason: string}>;
  selected_story_revision_ids: string[];
  export_text: string;
};

export type InterviewPrepClientResult<T> =
  | {status: 'success'; value: T}
  | {status: 'stale'; stage: 'save'; code: 'INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE'; message: string}
  | {status: 'transport_error'; stage: 'load' | 'save' | 'readback'; message: string}
  | {status: 'http_error'; stage: 'load' | 'save' | 'readback'; status_code: number; code?: string; message: string}
  | {status: 'scope_error'; stage: 'load' | 'save' | 'readback'; message: string}
  | {status: 'invalid_response'; stage: 'load' | 'save' | 'readback'; message: string}
  | {status: 'readback_error'; stage: 'readback'; message: string};

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type Stage = 'load' | 'save' | 'readback';

class ClientFailure extends Error {
  constructor(
    readonly kind: 'transport_error' | 'http_error' | 'scope_error' | 'invalid_response',
    readonly stage: Stage,
    message: string,
    readonly statusCode?: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = 'ClientFailure';
  }
}

function collectionPath(profileId: string, jobId: string): string {
  return `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/interview-prep-packs`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

async function requestJson<T>(
  url: string,
  init: RequestInit,
  fetcher: Fetcher,
  stage: Stage,
): Promise<T> {
  let response: Response;
  try {
    response = await fetcher(url, {
      ...init,
      headers: {'Content-Type': 'application/json', ...init.headers},
    });
  } catch (failure) {
    throw new ClientFailure(
      'transport_error', stage,
      failure instanceof Error ? failure.message : String(failure),
    );
  }

  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new ClientFailure('invalid_response', stage, 'Interview preparation API returned invalid JSON.');
  }

  if (!response.ok) {
    const detail = isRecord(body) ? body.detail : undefined;
    const code = isRecord(detail) && typeof detail.code === 'string' ? detail.code : undefined;
    const message = typeof detail === 'string' ? detail : `Interview preparation request failed (${response.status}).`;
    throw new ClientFailure('http_error', stage, message, response.status, code);
  }
  return body as T;
}

function validatePack(
  value: unknown,
  profileId: string,
  jobId: string,
  stage: Stage,
  expectedPackId?: string,
): InterviewPrepPack {
  if (!isRecord(value)
      || value.schema_version !== 1
      || typeof value.pack_id !== 'string' || !value.pack_id
      || !Number.isSafeInteger(value.pack_version) || (value.pack_version as number) < 1
      || typeof value.profile_id !== 'string'
      || typeof value.job_id !== 'string'
      || typeof value.profile_version !== 'string'
      || typeof value.job_fingerprint !== 'string'
      || typeof value.created_at !== 'string'
      || !Array.isArray(value.questions)
      || !Array.isArray(value.talking_points)
      || !Array.isArray(value.star_prompts)
      || !Array.isArray(value.missing_evidence)
      || !Array.isArray(value.selected_story_revision_ids)
      || typeof value.export_text !== 'string') {
    throw new ClientFailure('invalid_response', stage, 'Interview preparation API returned an invalid pack.');
  }
  if (value.profile_id !== profileId || value.job_id !== jobId
      || (expectedPackId !== undefined && value.pack_id !== expectedPackId)) {
    throw new ClientFailure('scope_error', stage, 'Interview preparation response did not match the selected profile and job.');
  }
  return value as unknown as InterviewPrepPack;
}

function mapFailure<T>(failure: unknown, stage: Stage): InterviewPrepClientResult<T> {
  if (failure instanceof ClientFailure) {
    if (failure.code === 'INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE') {
      return {
        status: 'stale', stage: 'save',
        code: 'INTERVIEW_PREPARATION_STORY_NOT_SELECTABLE', message: failure.message,
      };
    }
    if (failure.kind === 'http_error') {
      return {
        status: 'http_error', stage, status_code: failure.statusCode || 500,
        ...(failure.code ? {code: failure.code} : {}), message: failure.message,
      };
    }
    return {status: failure.kind, stage, message: failure.message};
  }
  return {
    status: 'invalid_response', stage,
    message: failure instanceof Error ? failure.message : String(failure),
  };
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (isRecord(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'null';
}

/** Load the collection without mutating or silently retrying it. */
export async function loadInterviewPreparation(
  profileId: string,
  jobId: string,
  fetcher: Fetcher = fetch,
): Promise<InterviewPrepClientResult<InterviewPrepPack[]>> {
  try {
    const response = await requestJson<unknown>(collectionPath(profileId, jobId), {}, fetcher, 'load');
    if (!isRecord(response) || response.schema_version !== 1 || !Array.isArray(response.packs)) {
      throw new ClientFailure('invalid_response', 'load', 'Interview preparation history response was invalid.');
    }
    if (response.profile_id !== profileId || response.job_id !== jobId) {
      throw new ClientFailure('scope_error', 'load', 'Interview preparation history did not match the selected profile and job.');
    }
    const packs = response.packs.map(pack => validatePack(pack, profileId, jobId, 'load'));
    const versions = new Set<number>();
    for (const pack of packs) {
      if (versions.has(pack.pack_version)) {
        throw new ClientFailure('invalid_response', 'load', 'Interview preparation history contained duplicate pack versions.');
      }
      versions.add(pack.pack_version);
    }
    return {status: 'success', value: packs};
  } catch (failure) {
    return mapFailure(failure, 'load');
  }
}

/** Save once, then confirm the immutable pack through its exact detail route. */
export async function saveInterviewPreparation(
  profileId: string,
  jobId: string,
  selectedStoryRevisionIds: readonly string[],
  fetcher: Fetcher = fetch,
): Promise<InterviewPrepClientResult<InterviewPrepPack>> {
  try {
    const created = validatePack(await requestJson<unknown>(collectionPath(profileId, jobId), {
      method: 'POST',
      body: JSON.stringify({selected_story_revision_ids: [...selectedStoryRevisionIds]}),
    }, fetcher, 'save'), profileId, jobId, 'save');
    const readback = validatePack(await requestJson<unknown>(
      `${collectionPath(profileId, jobId)}/${encodeURIComponent(created.pack_id)}`,
      {}, fetcher, 'readback',
    ), profileId, jobId, 'readback', created.pack_id);
    if (stableJson(created) !== stableJson(readback)) {
      return {status: 'readback_error', stage: 'readback', message: 'Saved interview preparation pack did not match its durable readback.'};
    }
    return {status: 'success', value: readback};
  } catch (failure) {
    const stage = failure instanceof ClientFailure ? failure.stage : 'save';
    return mapFailure(failure, stage);
  }
}
