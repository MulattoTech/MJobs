"""Two fresh worker processes can use one synthetic mock application safely."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from cli import ui_e2e


class MockWorkerProcessReuseTests(unittest.TestCase):
    """Exercise dry-run, context shutdown, then a fresh submit worker."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-mock-worker-reuse-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'reuse.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        profile = deepcopy(PROFILE)
        fixture_answer = next(
            row for row in profile["answers"] if row["id"] == "fixture_question"
        )
        fixture_answer.update({
            "text": "Unreviewed synthetic response.",
            "question": "Approved fixture answer",
            "verified": False,
            "user_approved": False,
            "confidence": 0,
        })
        self.store.save_profile(profile)
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job = self.store.jobs("fixture")[0]
        self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        self.origin = None

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_reviewed_answer_matches_actual_mock_ats_gate(self):
        with fixture_server() as origin, httpx.Client() as client:
            rejected = client.post(f"{origin}/ats/submit/requisition-A", data={
                "application_id": "synthetic-reviewed-answer",
                "answer": "Controlled fictional test application, reviewed for this job.",
                "confirmed": "on",
            })
            self.assertEqual(rejected.status_code, 422)
            self.assertEqual(
                client.get(f"{origin}/ats/receipts/synthetic-reviewed-answer").json(),
                {"status": "not_found"},
            )

            accepted = client.post(f"{origin}/ats/submit/requisition-A", data={
                "application_id": "synthetic-reviewed-answer",
                "answer": ui_e2e.REVIEWED_FIXTURE_ANSWER,
                "confirmed": "on",
            })
            self.assertEqual(accepted.status_code, 200)
            expected_reference = "MOCK-" + "synthetic-reviewed-answer"[:16]
            self.assertIn(f'data-confirmation="{expected_reference}"', accepted.text)
            receipt = client.get(
                f"{origin}/ats/receipts/synthetic-reviewed-answer").json()
            self.assertEqual(receipt["submission_count"], 1)
            self.assertEqual(receipt["reference_number"], expected_reference)

    def _worker_environment(self, origin):
        environment = {
            key: os.environ[key]
            for key in (
                "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
                "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH",
            )
            if key in os.environ
        }
        environment.update({
            "MJOBS_DATABASE_URL": self.store.url,
            "MJOBS_MOCK_ORIGIN": origin,
            "MJOBS_OUTPUT_DIR": str(self.root),
            "MJOBS_OPERATION_MODE": "FIXTURE",
            # Cooperative production deadline bounds each browser wait and
            # closes the browser context before the process exits.
            "MJOBS_OPERATION_TIMEOUT": "60",
        })
        return environment

    def _run_worker_process(self, operation_id, origin):
        script = (
            "import json, os, sys; "
            "from workers.tasks import execute_operation; "
            "print(json.dumps({'pid': os.getpid(), **execute_operation(sys.argv[1])}), flush=True)"
        )
        project_root = Path(__file__).resolve().parents[2]
        completed = subprocess.run(
            [sys.executable, "-c", script, operation_id],
            cwd=project_root,
            env=self._worker_environment(origin),
            capture_output=True,
            text=True,
            timeout=75,
            check=False,
        )
        self.assertEqual(completed.returncode, 0,
                         f"worker subprocess exited with code {completed.returncode}")
        records = [line for line in completed.stdout.splitlines() if line.startswith("{")]
        self.assertTrue(records, "worker subprocess returned no JSON result")
        return json.loads(records[-1])

    def _failure_boundary(self, client):
        with self.store.engine.connect() as connection:
            operation = connection.execute(
                select(m.operations.c.state, m.operations.c.error, m.operations.c.current_step)
                .where(m.operations.c.kind == "APPLY_MOCK")
                .order_by(m.operations.c.updated_at.desc())
                .limit(1)
            ).mappings().one()
        history = client.get(
            f"/profiles/fixture/applications/{self.application_id}/attempt-history"
        )
        attempt = (history.json().get("attempts") or [{}])[0] if history.status_code == 200 else {}
        application = client.get(
            f"/profiles/fixture/applications/{self.application_id}"
        ).json()
        preflight = application.get("question_preflight") or {}
        reason = preflight.get("reason")
        answer_review = preflight.get("answer_review") or []
        answer_review_reasons = [
            row.get("reason") for row in answer_review
            if isinstance(row, dict) and isinstance(row.get("reason"), str)
            and row["reason"].isupper()
        ]
        answer_review_ids = [
            row.get("question_id") for row in answer_review
            if isinstance(row, dict) and isinstance(row.get("question_id"), str)
            and row["question_id"].replace("_", "").isalnum()
        ]
        return {
            "operation_state": operation["state"],
            "operation_error": operation["error"],
            "operation_step": operation["current_step"],
            "attempt_state": attempt.get("state"),
            "attempt_error_code": attempt.get("error_code"),
            "attempt_current_step": attempt.get("current_step"),
            "preflight_result": preflight.get("result"),
            "preflight_is_current": preflight.get("is_current"),
            "preflight_reason": reason if isinstance(reason, str) and reason.isupper() else None,
            "answer_review_reasons": answer_review_reasons,
            "answer_review_question_ids": answer_review_ids,
        }

    def test_dry_run_worker_closes_before_fresh_submit_worker_uses_same_profile(self):
        with fixture_server() as origin:
            self.origin = origin
            application = self.store.application(self.application_id)
            job = self.store.job(application["job_id"])
            with self.store.engine.begin() as connection:
                connection.execute(update(m.routes).where(
                    m.routes.c.id == application["route_id"],
                ).values(url=f"{origin}/ats/apply/{job['requisition']}"))

            app = create_app(self.store, origin, self.root, operation_mode="FIXTURE")
            endpoint = f"/profiles/fixture/applications/{self.application_id}/mock-run"
            reprepare_endpoint = (
                f"/profiles/fixture/applications/{self.application_id}/reprepare"
            )
            history_path = (
                f"/profiles/fixture/applications/{self.application_id}/attempt-history"
            )
            with TestClient(app) as client:
                # The actual mock worker must stop on the unreviewed answer
                # before the simulated UI review and explicit reprepare.
                initial_admission = client.post(endpoint, json={
                    "submit": False,
                    "request_id": "before-answer-review",
                })
                self.assertEqual(initial_admission.status_code, 202, initial_admission.text)
                initial_operation_id = initial_admission.json()["operation_id"]
                initial_worker = self._run_worker_process(initial_operation_id, origin)
                self.assertEqual(initial_worker["status"], "BLOCKED",
                                 self._failure_boundary(client))
                initial_receipt = client.get(endpoint + "s/before-answer-review")
                self.assertEqual(initial_receipt.status_code, 200, initial_receipt.text)
                self.assertEqual(initial_receipt.json()["state"], "BLOCKED")
                self.assertEqual(initial_receipt.json()["error_code"], "REVIEW_REQUIRED")
                self.assertEqual(self.store.application(self.application_id)["state"],
                                 "REVIEW_REQUIRED")

                # Review the answer and explicitly reprepare the same
                # application. The final pins match the connected document-v6
                # flow before dry-run and submission use fresh worker processes.
                for revision in range(1, 6):
                    profile = client.get("/profiles/fixture").json()
                    if revision == 1:
                        # Answer Bank review creates a new approved revision;
                        # the former unreviewed candidate is retained out of
                        # this synthetic profile snapshot.
                        profile["answers"] = [
                            row for row in profile["answers"]
                            if row["id"] != "fixture_question"
                        ]
                        profile["answers"].append({
                            "id": "reviewed-fixture-question",
                            "text": ui_e2e.REVIEWED_FIXTURE_ANSWER,
                            "question": "Approved fixture answer",
                            "source": "User-reviewed fixture",
                            "verified": True,
                            "user_approved": True,
                            "confidence": 100,
                        })
                    else:
                        profile["email"] = f"synthetic-applicant-{revision}@example.invalid"
                    saved_profile = client.put("/profiles", json={
                        "id": profile["id"], "name": profile["name"],
                        "email": profile["email"], "expected_version": profile["version"],
                        "contact": profile.get("contact") or {},
                        "preferences": profile.get("preferences") or {},
                        "facts": [{
                            "id": row.get("fact_id") or row.get("id"),
                            "kind": row.get("kind", "skill"),
                            **{key: row[key] for key in
                               ("text", "source", "confidence", "verified", "user_approved")},
                        } for row in profile["facts"]],
                        "answers": [{
                            key: row[key] for key in
                            ("id", "question", "text", "source", "confidence", "verified", "user_approved")
                            if key in row
                        } for row in profile["answers"]],
                    })
                    self.assertEqual(saved_profile.status_code, 200, saved_profile.text)
                    current_application = self.store.application(self.application_id)
                    current_profile = client.get("/profiles/fixture").json()
                    resumed = client.post(reprepare_endpoint, json={
                        "expected_document_version": current_application["document_version"],
                        "expected_application_profile_version": current_application["profile_version"],
                        "expected_profile_version": current_profile["version"],
                        "expected_state": current_application["state"],
                    })
                    self.assertEqual(resumed.status_code, 200, resumed.text)
                self.assertEqual(
                    self.store.application(self.application_id)["document_version"], 6
                )
                self.assertEqual(client.get(endpoint + "s/before-answer-review").json()["state"],
                                 "BLOCKED")
                self.assertEqual(client.get("/operations/" + initial_operation_id).json()["state"],
                                 "BLOCKED")

                dry_admission = client.post(endpoint, json={
                    "submit": False,
                    "request_id": "separate-worker-dry-run",
                })
                self.assertEqual(dry_admission.status_code, 202, dry_admission.text)
                dry_operation = dry_admission.json()["operation_id"]
                dry_worker = self._run_worker_process(dry_operation, origin)
                self.assertEqual(dry_worker["status"], "COMPLETED",
                                 self._failure_boundary(client))
                self.assertEqual(self.store.application(self.application_id)["state"],
                                 "DRY_RUN_COMPLETE")
                dry_receipt = client.get(endpoint + "s/separate-worker-dry-run")
                self.assertEqual(dry_receipt.status_code, 200, dry_receipt.text)
                self.assertEqual(dry_receipt.json()["state"], "COMPLETED")
                dry_app = client.get(
                    f"/profiles/fixture/applications/{self.application_id}"
                ).json()
                self.assertEqual(dry_app["question_preflight"]["result"], "PASS")
                self.assertTrue(dry_app["question_preflight"]["is_current"])

                submit_admission = client.post(endpoint, json={
                    "submit": True,
                    "request_id": "separate-worker-local-submit",
                })
                self.assertEqual(submit_admission.status_code, 202, submit_admission.text)
                submit_operation = submit_admission.json()["operation_id"]
                submit_worker = self._run_worker_process(submit_operation, origin)
                self.assertNotEqual(dry_worker["pid"], submit_worker["pid"])
                self.assertEqual(submit_worker["status"], "COMPLETED",
                                 self._failure_boundary(client))

                submit_receipt = client.get(endpoint + "s/separate-worker-local-submit")
                self.assertEqual(submit_receipt.status_code, 200, submit_receipt.text)
                self.assertEqual(submit_receipt.json()["state"], "COMPLETED")
                final = client.get(
                    f"/profiles/fixture/applications/{self.application_id}"
                ).json()
                self.assertEqual(final["state"], "CONFIRMED")
                self.assertEqual(final["confirmation"]["submission_count"], 1)
                self.assertFalse(client.get("/health").json()["external_submission_enabled"])

                history = client.get(history_path)
                self.assertEqual(history.status_code, 200, history.text)
                attempts = history.json()["attempts"]
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0]["state"], "CONFIRMED")
                self.assertEqual(attempts[1]["state"], "DRY_RUN_COMPLETE")
                self.assertEqual(attempts[0]["current_step"], "VERIFY_CONFIRMATION")
                self.assertEqual(attempts[1]["current_step"], "VALIDATE")
                self.assertEqual(attempts[0]["document_version"], 6)
                self.assertEqual(attempts[1]["document_version"], 6)
                self.assertEqual(attempts[0]["profile_version"], attempts[1]["profile_version"])
                self.assertEqual(attempts[0]["error_code"], None)
                self.assertEqual(attempts[1]["error_code"], None)


if __name__ == "__main__":
    unittest.main()
