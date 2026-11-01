import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, uploadResume } from './api';

const requests = [
  {
    name: 'JSON API',
    timeoutMs: 30_000,
    timeoutMessage: 'Check the saved status before trying again',
    start: (signal?: AbortSignal) => api('/synthetic-operation', {
      method: 'POST', body: JSON.stringify({ request_id: 'one-intent' }), signal,
    }),
  },
  {
    name: 'resume upload',
    timeoutMs: 60_000,
    timeoutMessage: 'Check saved resumes before trying again',
    start: (signal?: AbortSignal) => uploadResume('fixture',
      new File(['Synthetic resume'], 'resume.txt', { type: 'text/plain' }), { signal }),
  },
];

function pendingBody() {
  let writer!: ReadableStreamDefaultController<Uint8Array>;
  const response = new Response(new ReadableStream<Uint8Array>({
    start(controller) { writer = controller; },
  }));
  return {
    response,
    finish() {
      writer.enqueue(new TextEncoder().encode('{"late":true}'));
      writer.close();
    },
  };
}

describe.each(requests)('$name bounded requests', request => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('times out a stalled fetch, aborts transport and never repeats the mutation', async () => {
    const fetchMock = vi.fn<typeof fetch>(() => new Promise<Response>(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    const caller = new AbortController();
    const removeListener = vi.spyOn(caller.signal, 'removeEventListener');
    const result = request.start(caller.signal);
    let settled = false;
    void result.then(() => { settled = true; }, () => { settled = true; });
    const rejected = expect(result).rejects.toMatchObject({
      name: 'TimeoutError', code: 'REQUEST_TIMEOUT',
      message: expect.stringContaining(request.timeoutMessage),
    });

    await vi.advanceTimersByTimeAsync(request.timeoutMs - 1);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    await rejected;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]?.method).toBe('POST');
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(removeListener).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps the same deadline while reading a stalled response body', async () => {
    const body = pendingBody();
    let resolveHeaders!: (response: Response) => void;
    const fetchMock = vi.fn<typeof fetch>(() => new Promise<Response>(resolve => { resolveHeaders = resolve; }));
    vi.stubGlobal('fetch', fetchMock);
    const result = request.start();
    const rejected = expect(result).rejects.toMatchObject({
      name: 'TimeoutError', code: 'REQUEST_TIMEOUT',
    });
    await vi.advanceTimersByTimeAsync(request.timeoutMs / 2);
    resolveHeaders(body.response);
    await vi.advanceTimersByTimeAsync(0);
    expect(body.response.bodyUsed).toBe(true);

    await vi.advanceTimersByTimeAsync(request.timeoutMs / 2);
    await rejected;
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);

    // A noncooperative body can finish later without turning the timeout into
    // success or causing another write to be sent.
    body.finish();
    await vi.advanceTimersByTimeAsync(0);
    await expect(result).rejects.toMatchObject({ code: 'REQUEST_TIMEOUT' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('preserves cancellation before admission without starting a request', async () => {
    const fetchMock = vi.fn<typeof fetch>();
    vi.stubGlobal('fetch', fetchMock);
    const caller = new AbortController();
    const reason = new DOMException('The user left this view.', 'AbortError');
    caller.abort(reason);

    await expect(request.start(caller.signal)).rejects.toBe(reason);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(['fetch', 'body'])('preserves caller cancellation during %s and removes its listener', async phase => {
    const body = phase === 'body' ? pendingBody() : undefined;
    const fetchMock = vi.fn<typeof fetch>(() => body
      ? Promise.resolve(body.response) : new Promise<Response>(() => {}));
    vi.stubGlobal('fetch', fetchMock);
    const caller = new AbortController();
    const addListener = vi.spyOn(caller.signal, 'addEventListener');
    const removeListener = vi.spyOn(caller.signal, 'removeEventListener');
    const reason = new DOMException('Selection changed.', 'AbortError');
    const result = request.start(caller.signal);
    const rejected = expect(result).rejects.toBe(reason);
    await vi.advanceTimersByTimeAsync(0);
    if (body) expect(body.response.bodyUsed).toBe(true);

    caller.abort(reason);
    await rejected;
    const outgoingSignal = fetchMock.mock.calls[0][1]?.signal;
    expect(outgoingSignal).not.toBe(caller.signal);
    expect(outgoingSignal?.aborted).toBe(true);
    expect(outgoingSignal?.reason).toBe(reason);
    expect(removeListener).toHaveBeenCalledWith('abort', addListener.mock.calls[0][1]);
    expect(vi.getTimerCount()).toBe(0);
    body?.finish();
    await vi.advanceTimersByTimeAsync(request.timeoutMs);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('returns normal JSON and removes both deadline and caller cancellation listener', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response('{"saved":true}'));
    vi.stubGlobal('fetch', fetchMock);
    const caller = new AbortController();
    const addListener = vi.spyOn(caller.signal, 'addEventListener');
    const removeListener = vi.spyOn(caller.signal, 'removeEventListener');

    await expect(request.start(caller.signal)).resolves.toEqual({ saved: true });
    expect(vi.getTimerCount()).toBe(0);
    expect(removeListener).toHaveBeenCalledWith('abort', addListener.mock.calls[0][1]);
    caller.abort();
    await vi.advanceTimersByTimeAsync(request.timeoutMs);
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('preserves ordinary network failure and cleans up without retrying', async () => {
    const failure = new TypeError('Network connection closed.');
    const fetchMock = vi.fn<typeof fetch>().mockRejectedValue(failure);
    vi.stubGlobal('fetch', fetchMock);
    const caller = new AbortController();
    const removeListener = vi.spyOn(caller.signal, 'removeEventListener');

    await expect(request.start(caller.signal)).rejects.toBe(failure);
    expect(removeListener).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(request.timeoutMs);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('preserves response parse errors and clears the deadline', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response('not JSON'));
    vi.stubGlobal('fetch', fetchMock);

    await expect(request.start()).rejects.toBeInstanceOf(SyntaxError);
    expect(vi.getTimerCount()).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('keeps useful server error messages instead of reporting a timeout', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(
      '{"detail":"Review the saved profile before continuing."}', { status: 422 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(request.start()).rejects.toThrow('Review the saved profile before continuing.');
    expect(vi.getTimerCount()).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
