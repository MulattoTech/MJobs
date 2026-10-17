"""T08 exact and latest readback behavior across changed review inputs."""
from unittest.mock import patch
import unittest

from tests.integration.test_lever_form_review_flow import (
    LEVER_URL,
    LeverFormReviewFlowTests,
)
from workers.tasks import execute_operation


class T08ReviewLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = LeverFormReviewFlowTests(
            "test_lever_route_persists_safe_review_metadata_by_request_id",
        )
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def _run_review(self, operation_id, request_id):
        fixture = self.fixture

        class FakeResponse:
            url = LEVER_URL
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
                return FakeResponse()

            def allowed(self, url):
                return url == self.website

            def close(self):
                pass

        with patch.dict("os.environ", {
            "MJOBS_OPERATION_MODE": "FIXTURE",
            "MJOBS_DATABASE_URL": fixture.database_url,
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_MOCK_ORIGIN": "http://127.0.0.1:1",
            "MJOBS_OUTPUT_DIR": str(fixture.root / "documents"),
        }), patch("workers.tasks.PublicCompanyClient", FakePublicClient):
            return execute_operation(operation_id)

    def test_latest_hides_stale_result_while_exact_readback_recovers_it(self):
        fixture = self.fixture
        path = fixture._path()
        first_id = "t08-profile-version-one"
        first = fixture.client.post(path, json={"request_id": first_id})
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self._run_review(first.json()["operation_id"], first_id)["status"], "COMPLETED")

        exact_before = fixture.client.get(path + "/" + first_id)
        self.assertEqual(exact_before.status_code, 200, exact_before.text)
        self.assertEqual(fixture.client.get(path + "/latest").json(), exact_before.json())

        profile = fixture.store.profile("fixture")
        profile["name"] = "Synthetic T08 Profile Revision"
        fixture.store.save_profile(profile)

        latest_stale = fixture.client.get(path + "/latest")
        self.assertEqual(latest_stale.status_code, 200, latest_stale.text)
        self.assertEqual(latest_stale.json(), {
            "request_id": None, "state": "UNKNOWN", "error_code": None,
            "analysis": None,
        })
        self.assertEqual(fixture.client.get(path + "/" + first_id).json(), exact_before.json())

        second_id = "t08-profile-version-two"
        second = fixture.client.post(path, json={"request_id": second_id})
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(second.json()["operation_id"], first.json()["operation_id"])
        self.assertEqual(self._run_review(second.json()["operation_id"], second_id)["status"], "COMPLETED")
        latest_current = fixture.client.get(path + "/latest")
        self.assertEqual(latest_current.status_code, 200, latest_current.text)
        self.assertEqual(latest_current.json()["request_id"], second_id)
        self.assertEqual(fixture.client.get(path + "/" + first_id).json(), exact_before.json())


if __name__ == "__main__":
    unittest.main()
