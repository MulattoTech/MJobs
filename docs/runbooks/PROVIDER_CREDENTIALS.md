# Provider credential storage

MJobs supports saved OpenAI and Anthropic provider tokens and explicit connection
checks. An explicitly admitted AI job-scoring operation can use its bound saved
OpenAI token: the AI worker decrypts that token and sends it only to the validated
OpenAI destination (`https://api.openai.com/v1`), without using a runtime endpoint
override. Saved Anthropic tokens support connection checks, not this scoring
path. Saving a token does not enable AI or start inference; other supported AI
providers use their configured runtime credentials.

## Encryption key boundary

Credential values are encrypted with AES-256-GCM before persistence. The database
stores only ciphertext, a random nonce, and non-secret metadata. Profile ID and
provider name are authenticated as associated data, so moving ciphertext to a
different profile or provider makes it undecryptable.

`MJOBS_CREDENTIAL_ENCRYPTION_KEY` is a URL-safe base64 encoding of 32 random
bytes used to encrypt and decrypt saved provider tokens. This master encryption
key is distinct from the provider tokens used to authenticate requests.
`python -m cli.init_env` adds a new master key to the ignored, private project
`.env`. The API and AI worker need this key, and Compose supplies it to both:
the API manages encrypted credentials and connection checks; the AI worker
decrypts the saved OpenAI token bound to an explicitly admitted operation.
Other workers and migrations do not receive this variable through the checked-in
Compose configuration. The master key is not returned to the browser or stored
in database rows or portable database backups. Backup restore verifies saved
credentials using an API-service command with the same master key. Existing
`.env` files are preserved; add a new key to an existing private `.env` before
using credential storage:

```powershell
$credentialKey = python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
Add-Content .env "MJOBS_CREDENTIAL_ENCRYPTION_KEY=$credentialKey"
Remove-Variable credentialKey
```

Keep this key private and retain it separately from database backups. A restored
database requires the same key to decrypt saved credentials. Restore the
original key into the private project `.env` before running
`cli.local_backup restore`; restore verifies that every saved credential can be
decrypted and fails closed before starting the restored application if the key
is missing or wrong. The database may already have been restored into the new
target when this check fails, so preserve that target and retry with a different
new target name after correcting the key. Losing or changing the key makes
existing credential ciphertext unusable; back up the key through the operator's
protected secret-management process before any planned key replacement. The UI's
**Rotate credential** action rotates the provider API key while preserving the
encryption key.

## Connection checks

The user must click **Test connection**. MJobs sends one bounded HTTPS GET to a
fixed provider models endpoint, with redirects disabled and an eight-second
timeout. It sends only the saved provider key in the provider-specific
authentication header; it
does not send a prompt, job, profile facts, or application data, and it does not
run model inference. Responses are reduced to stable success/failure codes and
timestamps. Raw provider responses and exceptions are not persisted or logged.

Credential management is profile-scoped. The API returns a constant mask and
non-secret metadata only. Replacing a key clears the last connection result;
deletion removes the active database record. Historical database backups may
still contain encrypted ciphertext until their normal retention period expires.

Local tests use synthetic keys and a mock HTTP transport. Live provider
connection checks are external actions and are not part of repository
validation.
