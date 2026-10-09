"""API adapter contract tests with an injected revision-service test double."""
from copy import deepcopy
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.resume_workspace import create_resume_workspace_router


class WorkspaceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class WorkspaceRepository:
    """Injected stand-in for Tech's durable revision service contract."""

    def __init__(self):
        self.rows = {}
        self.scopes = {
            ("profile-a", "job-a"): ("profile-v3", "job-fingerprint-a"),
            ("profile-b", "job-a"): ("profile-v8", "job-fingerprint-b"),
        }
        self.calls = []

    def list_revisions(self, store, profile_id, job_id):
        self.calls.append(("list", profile_id, job_id))
        if (profile_id, job_id) not in self.scopes:
            raise WorkspaceError("RESUME_WORKSPACE_NOT_FOUND")
        return list(self.rows.get((profile_id, job_id), []))

    def save_revision(self, store, *, profile_id, job_id, expected_version,
                      profile_version, job_fingerprint, template_id, sections):
        self.calls.append(("save", profile_id, job_id, expected_version))
        scope = self.scopes.get((profile_id, job_id))
        if scope is None:
            raise WorkspaceError("RESUME_WORKSPACE_NOT_FOUND")
        if (profile_version, job_fingerprint) != scope:
            raise WorkspaceError("RESUME_WORKSPACE_STALE_CONTEXT")
        rows = self.rows.setdefault((profile_id, job_id), [])
        current = rows[-1]["version"] if rows else None
        if expected_version != current:
            raise WorkspaceError("RESUME_WORKSPACE_STALE_VERSION")
        row = {
            "profile_id": profile_id,
            "job_id": job_id,
            "version": (current or 0) + 1,
            "profile_version": profile_version,
            "job_fingerprint": job_fingerprint,
            "template_id": template_id,
            "sections": deepcopy(sections),
            "created_at": "2026-10-09T00:00:00Z",
        }
        rows.append(row)
        return deepcopy(row)


class ResumeWorkspaceApiTests(unittest.TestCase):
    def setUp(self):
        self.repository = WorkspaceRepository()
        self.store = type("SourceStore", (), {})()
        self.store.facts = {
            "profile-a": [{"id": "fact-a", "text": "Canonical approved fact A"}],
            "profile-b": [{"id": "fact-b", "text": "Canonical approved fact B"}],
        }
        self.facts_before = deepcopy(self.store.facts)
        app = FastAPI()
        app.include_router(create_resume_workspace_router(
            self.store,
            list_revisions=self.repository.list_revisions,
            save_revision=self.repository.save_revision,
        ))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    @staticmethod
    def url(profile_id="profile-a", job_id="job-a"):
        return f"/profiles/{profile_id}/jobs/{job_id}/resume-workspace"

    @staticmethod
    def layout(*, visible=True):
        return [
            {"section_id": "summary", "visible": visible, "position": 0,
             "fact_ids": ["fact-a"]},
            {"section_id": "experience", "visible": True, "position": 1,
             "fact_ids": ["fact-a"]},
        ]

    def save_body(self, *, expected_version=None, **overrides):
        body = {
            "expected_version": expected_version,
            "profile_version": "profile-v3",
            "job_fingerprint": "job-fingerprint-a",
            "template_id": "verified-headings-v1",
            "sections": self.layout(),
        }
        body.update(overrides)
        return body

    def test_get_returns_current_and_version_history_for_exact_job_scope(self):
        saved = self.client.put(self.url(), json=self.save_body())
        self.assertEqual(saved.status_code, 200, saved.text)
        second = self.client.put(self.url(), json=self.save_body(
            expected_version=1, sections=self.layout(visible=False),
        ))
        self.assertEqual(second.status_code, 200, second.text)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["profile_id"], "profile-a")
        self.assertEqual(body["job_id"], "job-a")
        self.assertEqual(body["current"]["version"], 2)
        self.assertEqual(body["current"]["sections"], self.layout(visible=False))
        self.assertEqual([row["version"] for row in body["versions"]], [1, 2])
        self.assertEqual(body["current"]["created_at"], "2026-10-09T00:00:00Z")
        self.assertIn(("list", "profile-a", "job-a"), self.repository.calls)

    def test_put_creates_exact_versioned_readback_and_never_mutates_canonical_facts(self):
        response = self.client.put(self.url(), json=self.save_body())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual({key: body[key] for key in (
            "profile_id", "job_id", "version", "profile_version", "job_fingerprint",
            "template_id", "sections",
        )}, {
            "profile_id": "profile-a", "job_id": "job-a", "version": 1,
            "profile_version": "profile-v3", "job_fingerprint": "job-fingerprint-a",
            "template_id": "verified-headings-v1", "sections": self.layout(),
        })
        self.assertEqual(self.store.facts, self.facts_before)
        self.assertEqual(self.repository.calls[-1], ("save", "profile-a", "job-a", None))

    def test_profile_and_job_contexts_are_isolated(self):
        self.assertEqual(self.client.put(self.url(), json=self.save_body()).status_code, 200)
        other = self.client.get(self.url("profile-b", "job-a"))
        self.assertEqual(other.status_code, 200, other.text)
        self.assertIsNone(other.json()["current"])
        missing = self.client.get(self.url("profile-a", "foreign-job"))
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(self.store.facts, self.facts_before)

    def test_invalid_template_or_section_layout_is_rejected_without_writes(self):
        invalid = self.save_body(template_id="decorative-multi-column-v9")
        response = self.client.put(self.url(), json=invalid)
        self.assertEqual(response.status_code, 422, response.text)
        bad_positions = self.save_body(sections=[
            {"section_id": "summary", "visible": True, "position": 2, "fact_ids": ["fact-a"]},
        ])
        response = self.client.put(self.url(), json=bad_positions)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.repository.rows, {})
        self.assertEqual(self.store.facts, self.facts_before)

    def test_stale_version_and_stale_profile_job_snapshots_conflict_without_writes(self):
        first = self.client.put(self.url(), json=self.save_body())
        self.assertEqual(first.status_code, 200, first.text)
        before = deepcopy(self.repository.rows)
        stale_version = self.client.put(self.url(), json=self.save_body(expected_version=None))
        self.assertEqual(stale_version.status_code, 409, stale_version.text)
        self.assertEqual(stale_version.json()["detail"]["code"], "RESUME_WORKSPACE_STALE_VERSION")
        stale_context = self.client.put(self.url(), json=self.save_body(
            expected_version=1, profile_version="old-profile-version"))
        self.assertEqual(stale_context.status_code, 409, stale_context.text)
        self.assertEqual(stale_context.json()["detail"]["code"], "RESUME_WORKSPACE_STALE_CONTEXT")
        stale_job = self.client.put(self.url(), json=self.save_body(
            expected_version=1, job_fingerprint="old-job-fingerprint"))
        self.assertEqual(stale_job.status_code, 409, stale_job.text)
        self.assertEqual(stale_job.json()["detail"]["code"], "RESUME_WORKSPACE_STALE_CONTEXT")
        self.assertEqual(self.repository.rows, before)
        self.assertEqual(self.store.facts, self.facts_before)

    def test_empty_get_is_explicit_and_contains_no_invented_layout(self):
        response = self.client.get(self.url("profile-b", "job-a"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "profile_id": "profile-b", "job_id": "job-a", "current": None, "versions": [],
        })
