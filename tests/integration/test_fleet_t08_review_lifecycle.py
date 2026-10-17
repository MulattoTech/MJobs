"""T08 connected lifecycle regressions for durable application form review."""
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


LEVER_URL = "https://jobs.lever.co/example/01234567-89ab-cdef-0123-456789abcdef"


class T08ReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-t08-review-lifecycle-")
        self.root = Path(self.temp.name)
        self.database_url = f"sqlite:///{(self.root / 't08-review.db').as_posix()}"
        self.store = Store(self.database_url)
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
            job = next(row for row in self.store.jobs("fixture")
                       if row["requisition"] == "B")
            self.application_id = prepare(self.store, job["id"], "fixture", self.root)
        application = self.store.application(self.application_id)
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == application["route_id"],
            ).values(provider="lever", tenant="example",
                     requisition="01234567-89ab-cdef-0123-456789abcdef",
                     url=LEVER_URL))
            connection.execute(update(m.applications).where(
                m.applications.c.id == self.application_id,
            ).values(target="REAL"))
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root,
            "redis://127.0.0.1:1/0", operation_mode="FIXTURE",
        ))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def _path(self):
        return f"/profiles/fixture/applications/{self.application_id}/form-reviews"

    def _queue(self, request_id):
        response = self.client.post(self._path(), json={"request_id": request_id})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def _profile_revision(self, name):
        profile = self.store.profile("fixture")
        profile["name"] = name
        self.store.save_profile(profile)

    def _run_review(self, operation_id, *, on_fetch=None):
        store, route_url = self.store, LEVER_URL

        class FakeResponse:
            url = route_url
            headers = {"content-type": "text/html; charset=utf-8"}
            content = b"<form><label for='email'>Email</label><input id='email' name='email' type='email' required></form>"

        class FakePublicClient:
            def __init__(self, website, check=None, total_timeout=None):
                self.website = website
                self.check = check

            def get(self, url):
                self.check()
                if url != self.website:
                    raise AssertionError("unexpected public route")
                if on_fetch is not None:
                    on_fetch()
                return FakeResponse()

            def allowed(self, url):
                return url == self.website

            def close(self):
                pass

        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(self.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", FakePublicClient):
            return execute_operation(operation_id)

    def _operation(self, operation_id):
        with self.store.engine.connect() as connection:
            return dict(connection.execute(select(m.operations).where(
                m.operations.c.id == operation_id,
            )).mappings().one())

    def test_latest_uses_newest_admission_when_older_operation_updates_later(self):
        older = self._queue("t08-admission-order-older")
        newer = self._queue("t08-admission-order-newer")

        # Worker progress can update an older row after a newer request has been
        # admitted. Readback ordering must use immutable admission identity.
        with self.store.engine.begin() as connection:
            connection.execute(update(m.operations).where(
                m.operations.c.id == older["operation_id"],
            ).values(updated_at="9999-12-31T23:59:59.999999+00:00"))

        latest = self.client.get(self._path() + "/latest")
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(latest.json()["request_id"], "t08-admission-order-newer")
        self.assertEqual(latest.json()["state"], "QUEUED")
        self.assertEqual(newer["state"], "QUEUED")

    def test_profile_revision_before_fetch_blocks_review_with_stale_code(self):
        queued = self._queue("t08-profile-change-before-fetch")
        self._profile_revision("Synthetic profile changed before fetch")

        result = self._run_review(queued["operation_id"])

        self.assertEqual(result["status"], "DEAD_LETTERED")
        self.assertEqual(self._operation(queued["operation_id"])["error"],
                         "FORM_REVIEW_INPUT_STALE")
        exact = self.client.get(self._path() + "/t08-profile-change-before-fetch")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(exact.json()["error_code"], "FORM_REVIEW_INPUT_STALE")
        self.assertIsNone(exact.json()["analysis"])

    def test_profile_revision_during_fetch_blocks_result_persistence(self):
        queued = self._queue("t08-profile-change-during-fetch")

        result = self._run_review(
            queued["operation_id"],
            on_fetch=lambda: self._profile_revision("Synthetic profile changed during fetch"),
        )

        self.assertEqual(result["status"], "DEAD_LETTERED")
        self.assertEqual(self._operation(queued["operation_id"])["error"],
                         "FORM_REVIEW_INPUT_STALE")
        exact = self.client.get(self._path() + "/t08-profile-change-during-fetch")
        self.assertEqual(exact.status_code, 200, exact.text)
        self.assertEqual(exact.json()["error_code"], "FORM_REVIEW_INPUT_STALE")
        self.assertIsNone(exact.json()["analysis"])


if __name__ == "__main__":
    unittest.main()
