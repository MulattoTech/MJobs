# Verify the platform and run the complete dry-run journey

Run these commands from a current MJobs checkout with its pinned Python
dependencies. Functional modes also need the installed Playwright Chromium
dependency. They never install a browser automatically.

## Choose a mode

| Mode | What it executes |
| --- | --- |
| Default | Reads the selected API's current configuration, schema, PostgreSQL, Redis, scheduler and required worker health. |
| `--dry-run --fixture` | Creates an isolated fictional workspace, executes company discovery, global discovery, fresh scoring, document generation, browser navigation/fill/upload/validation, and stops with zero submissions. |
| `--connected --fixture` | Executes that functional journey, then one submission to its own mock ATS, duplicate protection and durable confirmation readback. |
| `--full --fixture` | Combines current API readiness and deployment identity with a fresh connected functional run. It repeats live observations afterward and requires the same clean source revision throughout. |

The three explicit modes are mutually exclusive. `--fixture` is required for
every mode that creates temporary data or launches a browser. The dry-run and
connected modes own their loopback target; they reject a custom `--api` address.

## Complete dry-run

```powershell
python -m cli.verify_readiness --dry-run --fixture --json
```

The command admits real durable product operations through the API and executes
their worker paths inline in the temporary workspace. It uses fictional career
facts and controlled sources. It does not use personal stores, inherited
provider credentials, proxy settings, paid AI calls or an external employer.

Scoring is explicitly refreshed for a new profile revision with changed
matching inputs. Earlier discovery scores cannot qualify that check. The
global discovery check also requires a new persisted completion event from its
own operation. Generated PDF/DOCX bytes are parsed and checked against saved
hashes. Chromium fills the mock form, selects the generated document bytes and
performs server validation.

Successful completion requires a fresh database read showing
`DRY_RUN_COMPLETE`, no confirmation, no receipt, and zero submission requests.
The parent process independently checks the saved operations, scores,
documents and request counters before removing the workspace. A child cannot
change the selected mode or omit required checks. Confirmation checks do not
appear in the dry-run inventory because that mode does not execute them.

## Check an installed stack

For the existing micro deployment, the local API port is 18801:

```powershell
python -m cli.verify_readiness --api http://127.0.0.1:18801
python -m cli.verify_readiness --full --fixture --api http://127.0.0.1:18801 --json
```

Use the exact deployed Git revision in a clean checkout for `--full`. A dirty
checkout, different revision, changed deployment during the run, missing
identity, failed dependency or unproven cleanup produces a nonzero result.

The full report retains distinct sections:

- `runtime_before` and `runtime_after`: observations from the selected API,
  including its deployment commit, version and mode.
- `functional`: the newly executed isolated workflow, source digest,
  dependency versions, invocation identity and cleanup result.
- `revision_identity`: whether the deployed and tested source stayed equal.

An isolated Chromium success does not replace the deployed application-worker
heartbeat. Temporary SQLite execution does not replace observed PostgreSQL
readiness. Failed live observations remain failed even when functional checks
succeed. Failed identity retrieval also preserves any successful readiness
observations rather than erasing them.

Only GET requests reach the selected installed API. Mock confirmation happens
inside the newly owned fixture, never against that API's applications.

## Bounds, cleanup and exit status

`--timeout` accepts a finite value greater than zero and no more than 30 seconds
(default 5). Each live observation shares one absolute deadline across its
readiness and deployment-identity requests, including DNS, TCP, TLS, headers
and body. Responses are limited to 64 KiB; compressed responses are rejected.
Redirects and inherited proxies are disabled. Raw response bodies and
transport exception details are not retained in the public report.

`--connected-timeout` bounds the owned functional child to 1–120 seconds
(default 120). Functional execution uses a retained Windows Job to contain
the child and descendants. Every outcome finalizes that ownership, checks for
zero active members and removes the temporary workspace. It does not kill
unrelated processes.

Functional modes currently require Windows retained Job support. On other
platforms they report `BLOCKED` without launching an uncontained fallback. The
ordinary live-readiness command remains available independently.

| Exit code | Meaning |
| --- | --- |
| `0` | Every required check for the selected mode executed successfully, with required cleanup and identity proof. |
| `1` | A check failed, admission/containment was blocked, cleanup was unproven, or full-mode source/deployment identity did not match. |
| `2` | A required functional dependency was unavailable; or command-line usage was invalid. The report and stderr distinguish these cases. |

Reports describe the current invocation. Do not import an older report as a
current PASS or equate a successful verification command with completion of
every original product requirement.
