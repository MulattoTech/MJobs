const API_REQUEST_TIMEOUT_MS = 30_000;
const RESUME_UPLOAD_TIMEOUT_MS = 60_000;
const API_REQUEST_TIMEOUT_MESSAGE = 'Request timed out. Check the saved status before trying again; the request may have completed.';

type RequestDeadlineOptions = {
  signal?: AbortSignal | null;
  timeoutMs?: number;
  timeoutMessage?: string;
};

export function isRequestTimeout(error: unknown): error is Error & { code: 'REQUEST_TIMEOUT' } {
  return error instanceof Error && 'code' in error && error.code === 'REQUEST_TIMEOUT';
}

/** Bound a complete request/readback operation, including response-body readers.
 * Return parsed values, never an unconsumed Response. Writes are dispatched once.
 */
export async function withRequestDeadline<T>(
  operation: (request: typeof fetch, signal: AbortSignal) => Promise<T>,
  fetcher: typeof fetch = globalThis.fetch,
  { signal, timeoutMs = API_REQUEST_TIMEOUT_MS,
    timeoutMessage = API_REQUEST_TIMEOUT_MESSAGE }: RequestDeadlineOptions = {},
): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new RangeError('Request deadline must be finite and positive.');
  signal?.throwIfAborted();
  const controller = new AbortController();
  const listeners = new Map<AbortSignal, () => void>();
  const responses = new Set<Response>();
  let closed = false;
  let rejectInterrupted!: (reason: unknown) => void;
  const interrupted = new Promise<never>((_, reject) => { rejectInterrupted = reject; });
  function interrupt(reason: unknown) {
    if (closed || controller.signal.aborted) return;
    rejectInterrupted(reason);
    controller.abort(reason);
  }
  function observe(value?: AbortSignal | null) {
    if (!value || listeners.has(value)) return;
    if (value.aborted) { interrupt(value.reason); return; }
    const cancel = () => interrupt(value.reason);
    listeners.set(value, cancel);
    value.addEventListener('abort', cancel, { once: true });
  }
  function assertActive() {
    controller.signal.throwIfAborted();
    if (closed) throw new DOMException('Request operation has ended.', 'AbortError');
  }
  function releaseUnreadBody(response: Response) {
    try {
      if (response.body && !response.bodyUsed && !response.body.locked) {
        void response.body.cancel().catch(() => undefined);
      }
    } catch { /* Best-effort cleanup must not replace the request outcome. */ }
  }
  const request: typeof fetch = async (input, init) => {
    assertActive();
    observe(init?.signal === null ? null : init?.signal ??
      (typeof Request !== 'undefined' && input instanceof Request ? input.signal : undefined));
    assertActive();
    const response = await fetcher(input, { ...init, signal: controller.signal });
    if (closed || controller.signal.aborted) releaseUnreadBody(response);
    assertActive();
    responses.add(response);
    return response;
  };
  observe(signal);
  const timer = setTimeout(() => interrupt(Object.assign(new Error(timeoutMessage), {
    name: 'TimeoutError', code: 'REQUEST_TIMEOUT',
  })), timeoutMs);
  try {
    return await Promise.race([
      (async () => {
        assertActive();
        const value = await operation(request, controller.signal);
        assertActive();
        return value;
      })(),
      interrupted,
    ]);
  } finally {
    closed = true;
    clearTimeout(timer);
    for (const [value, cancel] of listeners) value.removeEventListener('abort', cancel);
    listeners.clear();
    // Do not abort a successful nested operation's signal: its parent may still
    // be consuming a later readback. Dispose only bodies we did not consume.
    for (const response of responses) releaseUnreadBody(response);
  }
}

/** Keep response consumption inside the deadline, including non-JSON readers. */
export async function requestWithDeadline<T>(path: string, init: RequestInit,
  readResponse: (response: Response) => Promise<T>,
  timeoutMs = API_REQUEST_TIMEOUT_MS, timeoutMessage = API_REQUEST_TIMEOUT_MESSAGE,
  fetcher: typeof fetch = globalThis.fetch): Promise<T> {
  return withRequestDeadline(async request => readResponse(await request(path, init)),
    fetcher, { signal: init.signal, timeoutMs, timeoutMessage });
}

function requestJSON(path: string, init: RequestInit, timeoutMs: number, timeoutMessage: string) {
  return requestWithDeadline(path, init,
    async response => ({ response, data: await response.json() }), timeoutMs, timeoutMessage);
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { response, data } = await requestJSON(path,
    { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } },
    API_REQUEST_TIMEOUT_MS,
    API_REQUEST_TIMEOUT_MESSAGE);
  if (!response.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : typeof data.detail?.message === 'string' ? data.detail.message : `Request rejected (${response.status}). Check the fields and try again.`), { status: response.status, code: typeof data.detail?.code === 'string' ? data.detail.code : undefined });
  return data as T;
}
export function resumeMediaType(filename: string): string {
  const extension = filename.split('.').pop()?.toLowerCase();
  const media: Record<string,string> = {
    pdf: 'application/pdf',
    docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    txt: 'text/plain',
  };
  if (!extension || !media[extension]) throw new Error('Choose a PDF, DOCX, or UTF-8 TXT resume.');
  return media[extension];
}
export async function uploadResume<T>(profileId: string, file: File, options: { label?: string; seriesId?: string; signal?: AbortSignal | null } = {}): Promise<T> {
  if (!file.size || file.size > 10 * 1024 * 1024) throw new Error('Resume must be nonempty and no larger than 10 MiB.');
  const { response, data } = await requestJSON('/profiles/'+encodeURIComponent(profileId)+'/resumes', {
    method: 'POST', body: file, signal: options.signal,
    headers: {
      'Content-Type': resumeMediaType(file.name),
      'X-Filename': encodeURIComponent(file.name),
      ...(options.label ? {'X-Resume-Label': encodeURIComponent(options.label)} : {}),
      ...(options.seriesId ? {'X-Resume-Series': encodeURIComponent(options.seriesId)} : {}),
    },
  }, RESUME_UPLOAD_TIMEOUT_MS,
  'Resume upload timed out. Check saved resumes before trying again; the upload may have completed.');
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Resume upload rejected (${response.status}).`);
  return data as T;
}
export function eligible(fact: {verified: boolean; user_approved: boolean; confidence: number; source: string}) {
  return fact.verified && fact.user_approved && fact.confidence >= 80 && fact.source.trim().length > 0;
}
export const terminal = new Set(['COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'DEAD_LETTERED', 'DISCARDED']);
