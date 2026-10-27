# Maintainer reference: resume workspace editor API

**Source pin:** commit `2f01b876762fcbdd70f11e69865a48257503af93`
**Exact source:** `apps/api/resume_workspace_editor.py`
**Source blob:** `f3e99834cb8f6f471dc4a6d5b0f5af07668f807e`
**Outcome:** `documented` — existing architecture documentation names the service but does not describe this API's route/request/response/error contract.

This note is a static maintainer reference, not a test report or acceptance claim. No CI or tests were run for this documentation task.

## Role and wiring

`create_resume_workspace_editor_router(store)` builds an `APIRouter` whose handlers close over the supplied persistence `store` (`apps/api/resume_workspace_editor.py:80-81`). The application factory imports and includes that router with the store (`apps/api/main.py:758-762`). The module itself does not define a route prefix, request authentication dependency, or separate service container; deployment-level middleware/prefix behavior must be checked at the application boundary, not inferred from this router.

The editor is a reviewed preview/save/history/export API. Its handlers delegate to template, revision, preview, and export services (`apps/api/resume_workspace_editor.py:10-17,83-155`). It exposes no employer-submission handler. The integration test verifies that preview/save/export leave the application and generated-document counts unchanged (`tests/integration/test_resume_workspace_editor_api.py:60-70,103-108,120-127`).

## Route contract

All paths below are the literal route templates declared in the module; `{profile_id}` and `{job_id}` are path values.

| Method and path | Input | Result |
|---|---|---|
| `GET /resume-workspace/templates` | None | `{"templates": ...}` from `list_resume_templates()` (`:83-85`). |
| `GET /profiles/{profile_id}/jobs/{job_id}/resume-workspace` | Profile/job scope | `profile_id`, `job_id`, `current` (latest revision or `null`) and ordered `versions` (`:87-96`). Datetimes in rows are serialized as ISO strings (`:73-77`). |
| `POST /profiles/{profile_id}/jobs/{job_id}/resume-workspace/preview` | `PreviewInput` below | Draft preview service result (`:98-111`). The service returns text, sections, source-fact references, alignment evidence and a preview fingerprint; it rechecks the profile/job snapshot before returning (`services/documents/resume_workspace_preview.py:225-326`). |
| `PUT /profiles/{profile_id}/jobs/{job_id}/resume-workspace` | `SaveInput` below | `{"workspace_revision": ..., "replayed": bool}` (`:113-129`). The service re-renders and appends one reviewed revision; an exact retry with the same request ID and same payload returns the prior row (`services/documents/resume_workspace_export.py:34-40,63-83`). |
| `GET /profiles/{profile_id}/jobs/{job_id}/resume-workspace/versions/{version}/text` | Version path value | Plain-text attachment `resume-workspace-v{version}.txt` (`:131-139`). |
| `GET /profiles/{profile_id}/jobs/{job_id}/resume-workspace/versions/{version}/{format}` | `format` must be `docx`, `pdf`, or `txt` | Rendered attachment with format-specific media type and filename (`:141-155`). Other format strings return 404 with `RESUME_WORKSPACE_EXPORT_NOT_FOUND` (`:142-144`). |

## Request models and validation

All three Pydantic models use strict types and reject extra fields (`apps/api/resume_workspace_editor.py:20-21,28-29,45-46`). A `Section` has exactly the declared schema: `section_id`, `visible`, `position`, and `fact_ids` (`:20-26`). The ID must match `^[a-z][a-z0-9_-]*$`; positions are integers 0–49; each section needs 1–100 fact IDs. Both preview and save require 1–50 sections. Their layout validators reject duplicate section IDs, non-contiguous positions (the positions must sort to `0..n-1`), and a layout with no visible section (`:36-42,56-62`). Service validation additionally restricts section IDs to supported headings and fact IDs to unique, currently eligible/approved profile facts (`services/documents/resume_workspace_preview.py:48-89`; `services/documents/resume_workspace.py:63-94`).

`PreviewInput` requires `template_id`, `sections`, `expected_profile_version`, `expected_job_fingerprint`, and `expected_template_version`; string lengths are bounded in the model (`apps/api/resume_workspace_editor.py:28-34`). These expected values bind the preview to current profile, job and template snapshots; stale profile/job/template context is rejected by the service (`services/documents/resume_workspace_preview.py:27-45,103-143`).

`SaveInput` requires `expected_version` (its value may be `null` for the first revision), profile version, job fingerprint, template ID, sections, a 64-character lowercase SHA-256 `preview_fingerprint`, explicit boolean `reviewed`, and a non-whitespace `request_id` (`apps/api/resume_workspace_editor.py:45-54`). The service rejects an unreviewed save, malformed request IDs, stale preview fingerprints, and a reused request ID whose payload differs (`services/documents/resume_workspace_export.py:34-49,67-83,85-95`). On a later revision, `expected_version` is the optimistic version expectation; the handler passes it through to the save service (`apps/api/resume_workspace_editor.py:113-125`).

## Error behavior

Service exceptions handled by `_raise` become `HTTPException` responses with `{"code": code}` as the `detail` value (`apps/api/resume_workspace_editor.py:65-70`). Status selection is source-defined and order matters: any code containing `STALE` or `CONFLICT`, plus `AVOID_LIST_BLOCKED`, maps to 409; otherwise codes containing `NOT_FOUND` map to 404; otherwise only `RESUME_WORKSPACE_INVALID` and `RESUME_WORKSPACE_REVIEW_REQUIRED` reach the explicit 422 branch; all other service errors map to 500 (`apps/api/resume_workspace_editor.py:65-70`). In particular, `RESUME_WORKSPACE_PREVIEW_STALE` contains `STALE`, so the earlier branch maps it to 409, as confirmed by the integration test (`tests/integration/test_resume_workspace_editor_api.py:129-150).

The read-history and text-export handlers specially translate `RESUME_WORKSPACE_INVALID` to 404 `RESUME_WORKSPACE_NOT_FOUND` (`:87-96,131-139`). Unsupported export formats return 404 directly (`:141-145`). FastAPI/Pydantic request-shape validation occurs before handler code; this module does not customize that framework response. The integration tests demonstrate 422 for an unreviewed save, 409 for a stale preview, 404 for a foreign profile/job export, and 409 when the avoid list blocks an export (`tests/integration/test_resume_workspace_editor_api.py:120-150`).

## Safe synthetic call sequence

Use a synthetic/local fixture profile and job. Get the current template catalog first, and obtain current profile/job snapshot values from the caller's already-scoped data source; do not invent or reuse stale versions/fingerprints. The integration test uses `verified-headings-v1` and synthetic facts `fact-7`/`fact-8` (`tests/integration/test_resume_workspace_editor_api.py:28-58`).

1. `GET /resume-workspace/templates` and select a returned template and its current version.
2. `POST /profiles/fixture/jobs/{job-id}/resume-workspace/preview` with a body shaped like:

```json
{
  "template_id": "verified-headings-v1",
  "sections": [
    {"section_id": "experience", "visible": true, "position": 0, "fact_ids": ["fact-7"]},
    {"section_id": "skill", "visible": true, "position": 1, "fact_ids": ["fact-8"]}
  ],
  "expected_profile_version": "<current profile version>",
  "expected_job_fingerprint": "<current job fingerprint>",
  "expected_template_version": "<current catalog template version>"
}
```

This mirrors the synthetic integration fixture; the placeholders must be replaced with values read for the same profile/job/template snapshot (`tests/integration/test_resume_workspace_editor_api.py:41-58`). Review the returned preview. It is read-only and is not a saved document (`:60-70`).

3. Only after reviewing that exact preview, `PUT /profiles/fixture/jobs/{job-id}/resume-workspace` with `expected_version: null` for the first save, the same profile/job/template/sections, the returned `preview_fingerprint`, `reviewed: true`, and a stable unique `request_id`. On a retry, reuse that request ID only with the identical payload; a changed payload conflicts (`services/documents/resume_workspace_export.py:63-83`). The example does not represent a real applicant or authorize submission.
4. Read the saved revision using the base GET route, then export a chosen version as `/versions/{version}/text`, `/docx`, or `/pdf`. A later profile preference change can make an existing export fail with `AVOID_LIST_BLOCKED` rather than returning blocked text (`tests/integration/test_resume_workspace_editor_api.py:89-127`).

## Existing documentation coverage

`docs/ARCHITECTURE.md:190-204` distinguishes resume-workspace revisions from original/generated artifacts and discusses storage/recovery, but it does not describe the editor router, endpoints, models, snapshot tokens, response shapes, or status mapping. The requirement-gap analysis names the broader resume-workspace capability, not this API contract (`docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md:184`). This focused reference therefore fills a maintainer-contract gap without duplicating the architecture overview.

## Reference index

- Router, schemas, validation, handlers, error mapping: `apps/api/resume_workspace_editor.py:20-155` (blob `f3e99834cb8f6f471dc4a6d5b0f5af07668f807e`).
- Application router registration: `apps/api/main.py:758-762`.
- Draft-preview snapshot and response: `services/documents/resume_workspace_preview.py:27-45,225-326`.
- Revision scope/history: `services/documents/resume_workspace.py:39-60,105-134`.
- Reviewed save, stale fingerprint and retry behavior: `services/documents/resume_workspace_export.py:34-95`.
- Synthetic end-to-end API coverage: `tests/integration/test_resume_workspace_editor_api.py:21-58,60-150`.
- Existing higher-level doc coverage: `docs/ARCHITECTURE.md:190-204`; capability reference `docs/reference/REFERENCE_PROJECT_GAP_ANALYSIS.md:184`.
