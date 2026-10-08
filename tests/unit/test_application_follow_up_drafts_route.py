import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from tests.integration.test_spine import PROFILE


class ApplicationFollowUpDraftRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-follow-up-route-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'test.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.profile_id = "follow-up-profile"
        self.store.save_profile(dict(PROFILE, id=self.profile_id))
        self.calls = []

        service = types.ModuleType("services.tracking.follow_up_drafts")

        def draft_follow_up_candidates(store, profile_id, quiet_days):
            store.profile(profile_id)
            self.calls.append((store, profile_id, quiet_days))
            return {"quiet_days": quiet_days, "candidates": []}

        service.draft_follow_up_candidates = draft_follow_up_candidates
        self.service_patch = patch.dict(sys.modules, {
            "services.tracking.follow_up_drafts": service,
        })
        self.service_patch.start()
        self.client = TestClient(create_app(
            self.store, "http://127.0.0.1:1", self.root, operation_mode="PERSONAL",
        ))
        self.url = f"/profiles/{self.profile_id}/application-follow-up-drafts"

    def tearDown(self):
        self.client.close()
        self.service_patch.stop()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_default_and_valid_quiet_days_are_forwarded_and_returned(self):
        default = self.client.get(self.url)
        valid = self.client.get(self.url, params={"quiet_days": 14})

        self.assertEqual(default.status_code, 200, default.text)
        self.assertEqual(default.json(), {"quiet_days": 7, "candidates": []})
        self.assertEqual(valid.status_code, 200, valid.text)
        self.assertEqual(valid.json(), {"quiet_days": 14, "candidates": []})
        self.assertEqual([call[1:] for call in self.calls], [
            (self.profile_id, 7), (self.profile_id, 14),
        ])

    def test_quiet_days_outside_bounds_are_rejected_without_service_call(self):
        for quiet_days in (0, 91):
            with self.subTest(quiet_days=quiet_days):
                response = self.client.get(self.url, params={"quiet_days": quiet_days})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.calls, [])

    def test_missing_profile_returns_safe_not_found(self):
        response = self.client.get("/profiles/missing/application-follow-up-drafts")

        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json(), {"detail": "Profile or application not found"})


if __name__ == "__main__":
    unittest.main()
