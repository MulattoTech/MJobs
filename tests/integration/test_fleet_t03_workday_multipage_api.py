"""API boundary tests for the lead-owned Workday checkpoint coordinator."""
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy import update

from apps.api.main import create_app
from fixtures.mock_server import fixture_server
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.pipeline import discover
from services.documents.generate import prepare
from tests.integration.test_spine import PROFILE


def observation(heading):
    return {
        "url": "https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply?session=synthetic",
        "headings": [heading],
        "field_labels": [heading],
        "actions": ["Save and Continue"],
        "stale": False,
    }


class TestFleetT03WorkdayMultipageApi:
    def setup_method(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-fleet-t03-multipage-api-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'multipage.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("FIXTURE")
        self.store.save_profile(dict(PROFILE))
        with fixture_server() as origin:
            discover(self.store, origin, "fixture")
        job_id = self.store.jobs("fixture")[0]["id"]
        application_id = prepare(self.store, job_id, "fixture", self.root)
        self.application_id = application_id
        with self.store.engine.begin() as connection:
            connection.execute(update(m.routes).where(
                m.routes.c.id == self.store.application(application_id)["route_id"],
            ).values(
                provider="workday",
                url="https://acme.wd5.myworkdayjobs.com/en-US/Acme/job/REQ-1/apply",
            ))
        self.url = f"/profiles/fixture/applications/{application_id}/multipage-state"
        self.app = create_app(self.store, "http://127.0.0.1:1", self.root,
                              operation_mode="FIXTURE")
        self.client = TestClient(self.app)

    def teardown_method(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_workday_route_passes_exact_contract_and_captured_workspace_mode(self):
        module = ModuleType("services.applications.workday_recovery")
        reconcile = Mock(return_value={"decision": "INITIALIZED", "execute": False})
        module.reconcile_workday_checkpoint = reconcile
        payload = {
            "action": "INITIALIZE",
            "request_id": "fleet-t03-initialize",
            "expected_version": 0,
            "current_observation": observation("My Information"),
        }
        with patch.dict(sys.modules, {module.__name__: module}):
            response = self.client.post(self.url + "/workday", json=payload)
        assert response.status_code == 200, response.text
        assert response.json() == {"decision": "INITIALIZED", "execute": False}
        reconcile.assert_called_once_with(
            self.store, "fixture", self.application_id,
            action="INITIALIZE",
            request_id="fleet-t03-initialize",
            expected_version=0,
            current_observation=payload["current_observation"],
            previous_observation=None,
            operation_mode="FIXTURE",
        )

    def test_workday_observation_contract_rejects_extra_and_oversized_fields(self):
        valid = {
            "action": "INITIALIZE",
            "request_id": "fleet-t03-invalid",
            "expected_version": 0,
            "current_observation": observation("My Information"),
        }
        for payload in (
            {**valid, "submit": True},
            {**valid, "current_observation": {**valid["current_observation"], "cookie": "synthetic"}},
            {**valid, "current_observation": observation("x" * 257)},
            {**valid, "progress": {"password": "synthetic"}},
        ):
            response = self.client.post(self.url + "/workday", json=payload)
            assert response.status_code == 422, response.text


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
