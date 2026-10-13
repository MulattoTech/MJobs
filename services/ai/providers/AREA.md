# AI Provider Implementations

Provider implementations live behind the provider-neutral contract in `services/ai/providers/__init__.py`.

## Remote transport boundary

OpenAI, OpenAI-compatible and Azure OpenAI endpoints require HTTPS for remote
hosts. Plain HTTP is allowed only for literal loopback IPs, such as
`127.0.0.1` or `[::1]`; hostnames (including `localhost`), private LAN addresses
and ambiguous numeric hosts cannot use this exception. Configure the provider's
HTTPS endpoint for a remote service. URL credentials, query strings, fragments,
whitespace, control characters and backslashes are rejected with a stable
configuration error before provider I/O. Valid endpoint path prefixes are preserved.

Completion requests never follow redirects, including when an injected HTTP
client normally follows them, so a redirect cannot replay a private prompt to
another origin. Proxies remain disabled. These controls do not enable a provider
or authorize paid calls; task defaults remain disabled. Synthetic transport and
profile-settings tests cover rejection, successful local/HTTPS requests, unchanged
settings/audit on failure and restart readback.

## Local Ollama boundary

`OllamaProvider` sends requests only to a numeric loopback IP over HTTP, with proxies disabled and a fixed `/api/generate` path. The optional `MJOBS_OLLAMA_BASE_URL` setting is validated before use. Request, schema, timeout, and response sizes are bounded; transport and response errors use stable codes without echoing prompts or provider bodies. An evaluation cancellation event closes an in-flight client connection and is classified separately from timeout and transport failure. Structured requests pass the caller's JSON Schema through Ollama's `format` field, then the task router validates the returned string again.

The provider is selected only when task configuration explicitly names `ollama`; all default tasks remain `disabled`. Tests use synthetic `httpx.MockTransport` responses and never require an installed model or make network requests.

## Saved profile OpenAI credentials

The durable scoring consumer can supply an explicitly selected profile's saved
key to `OpenAIProvider`. That path pins `https://api.openai.com/v1` and ignores
environment endpoint overrides. Credential source and ciphertext identity are
frozen at admission; rotation/deletion and unavailable decryption fail closed.
No fallback to another profile or runtime key occurs for a saved-key plan.
Legacy and runtime-configured plans preserve their existing transport settings.
Preview/history expose only the source and fixed destination. Synthetic tests
exercise the actual router/adapter and a separate worker process; live inference
and paid calls remain unverified and are never part of automated tests.
