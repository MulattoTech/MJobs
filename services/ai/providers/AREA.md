# AI Provider Implementations

Provider implementations live behind the provider-neutral contract in `services/ai/providers/__init__.py`.

## Local Ollama boundary

`OllamaProvider` sends requests only to a numeric loopback IP over HTTP, with proxies disabled and a fixed `/api/generate` path. The optional `MJOBS_OLLAMA_BASE_URL` setting is validated before use. Request, schema, timeout, and response sizes are bounded; transport and response errors use stable codes without echoing prompts or provider bodies. Structured requests pass the caller's JSON Schema through Ollama's `format` field, then the task router validates the returned string again.

The provider is selected only when task configuration explicitly names `ollama`; all default tasks remain `disabled`. Tests use synthetic `httpx.MockTransport` responses and never require an installed model or make network requests.
