const API_REQUEST_TIMEOUT_MS = 30_000;
const RESUME_UPLOAD_TIMEOUT_MS = 60_000;

async function requestJSON(path: string, init: RequestInit, timeoutMs: number, timeoutMessage: string) {
  const callerSignal = init.signal;
  if (callerSignal?.aborted) throw callerSignal.reason;

  const controller = new AbortController();
  let rejectInterrupted!: (reason: unknown) => void;
  const interrupted = new Promise<never>((_, reject) => { rejectInterrupted = reject; });
  function interrupt(reason: unknown) {
    if (controller.signal.aborted) return;
    controller.abort(reason);
    rejectInterrupted(reason);
  }
  const cancel = () => interrupt(callerSignal?.reason);
  callerSignal?.addEventListener('abort', cancel, { once: true });
  const timer = setTimeout(() => interrupt(Object.assign(new Error(timeoutMessage), {
    name: 'TimeoutError', code: 'REQUEST_TIMEOUT',
  })), timeoutMs);

  try {
    // The deadline covers both headers and body. The race also releases callers
    // when a transport fails to settle after receiving the abort signal.
    return await Promise.race([
      (async () => {
        const response = await fetch(path, { ...init, signal: controller.signal });
        controller.signal.throwIfAborted();
        const data = await response.json();
        controller.signal.throwIfAborted();
        return { response, data };
      })(),
      interrupted,
    ]);
  } finally {
    clearTimeout(timer);
    callerSignal?.removeEventListener('abort', cancel);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const { response, data } = await requestJSON(path,
    { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } },
    API_REQUEST_TIMEOUT_MS,
    'Request timed out. Check the saved status before trying again; the request may have completed.');
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
