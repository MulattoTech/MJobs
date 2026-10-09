"""Per-profile discovery policy controls durable company-discovery intake."""
import copy
from pathlib import Path
import tempfile
import unittest

import httpx
from pydantic import ValidationError
from sqlalchemy import delete, select, update

from packages.domain.profile import JobPreferences
from packages.persistence import models as m
from packages.persistence.store import Store
from services.discovery.sources.collect import collect_source, configure_source
from services.company_discovery.requests import approve_discovery_request, record_discovery_request
from workers.dispatch import _pending_operations
from tests.integration.test_greenhouse import RAW
from tests.integration.test_spine import PROFILE


class CompanyDiscoveryPolicyTests(unittest.TestCase):
    def test_profile_preferences_have_safe_defaults_and_validate_thresholds(self):
        defaults = JobPreferences().model_dump()["company_discovery"]
        self.assertEqual(defaults, {
            "company_discovery_enabled": True,
            "minimum_match_for_discovery": 60,
            "priority_match_for_discovery": 80,
            "max_new_company_discoveries_per_hour": 12,
        })
        configured = JobPreferences.model_validate({"company_discovery": {
            "company_discovery_enabled": False,
            "minimum_match_for_discovery": 72,
            "priority_match_for_discovery": 91,
            "max_new_company_discoveries_per_hour": 4,
        }})
        self.assertEqual(configured.company_discovery.minimum_match_for_discovery, 72)
        for invalid in (
            {"minimum_match_for_discovery": 91, "priority_match_for_discovery": 90},
            {"minimum_match_for_discovery": 101},
            {"max_new_company_discoveries_per_hour": 0},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                JobPreferences.model_validate({"company_discovery": invalid})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mjobs-company-discovery-policy-")
        self.root = Path(self.temp.name)
        self.store = Store(f"sqlite:///{(self.root / 'policy.db').as_posix()}")
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.source = configure_source(self.store, "policy-example")
        self.save_profile()

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def save_profile(self, **policy):
        profile = copy.deepcopy(PROFILE)
        profile["preferences"] = {
            **profile.get("preferences", {}),
            "company_discovery": {
                "company_discovery_enabled": True,
                "minimum_match_for_discovery": 60,
                "priority_match_for_discovery": 80,
                "max_new_company_discoveries_per_hour": 12,
                **policy,
            },
        }
        self.store.save_profile(profile)
        return self.store.profile("fixture")

    def collect(self, source=None, raw=None):
        def response(request):
            if request.url.path.endswith("/jobs"):
                return httpx.Response(200, json={"jobs": [raw or RAW]})
            tenant = request.url.path.split("/")[3]
            return httpx.Response(200, json={"name": f"Example Employer {tenant}"})
        return collect_source(
            self.store, source or self.source, "fixture",
            transport=httpx.MockTransport(response),
        )

    def test_disabled_policy_prevents_new_profile_discovery_requests(self):
        self.save_profile(company_discovery_enabled=False)
        self.collect()
        self.assertEqual(self.store.count("company_discovery_requests"), 0)
        persisted = self.store.profile("fixture")
        self.assertFalse(persisted["preferences"]["company_discovery"]["company_discovery_enabled"])

    def test_thresholds_control_high_priority_and_rate_limit_new_requests(self):
        self.save_profile(
            minimum_match_for_discovery=70,
            priority_match_for_discovery=90,
            max_new_company_discoveries_per_hour=1,
        )
        self.collect()
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == "fixture")).mappings().one())
            job_id = request["trigger_job_id"]
            company_id = request["company_id"]
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]))
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == job_id,
                m.matches.c.profile_id == "fixture",
            ).values(score=85))
            new_id = record_discovery_request(connection, job_id, self.store.profile("fixture"))
            self.assertIsNotNone(new_id)
            below_priority = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == new_id)).mappings().one())
            self.assertNotIn("HIGH_MATCH_JOB", below_priority["reasons"])
            self.assertIn("MATCH_ELIGIBLE_JOB", below_priority["reasons"])

        self.assertEqual(self.store.count("company_discovery_requests"), 1)
        # A second, independently recognized employer is held by the hourly cap.
        second_source = configure_source(self.store, "policy-second")
        second_raw = dict(RAW, id=9876, internal_job_id=9876,
                          absolute_url="https://job-boards.greenhouse.io/policy-second/jobs/9876")
        self.collect(second_source, second_raw)
        self.assertEqual(self.store.count("company_discovery_requests"), 1)

    def test_high_match_threshold_is_configurable_and_dispatch_priority_survives_review(self):
        self.save_profile(minimum_match_for_discovery=70, priority_match_for_discovery=90)
        self.collect()
        with self.store.engine.begin() as connection:
            request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.profile_id == "fixture")).mappings().one())
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_id == "fixture",
            ).values(score=89))
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request["id"]))
            below_priority_id = record_discovery_request(
                connection, request["trigger_job_id"], self.store.profile("fixture"),
                is_new_company=False,
            )
            below_priority = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == below_priority_id)).mappings().one())
            self.assertIn("MATCH_ELIGIBLE_JOB", below_priority["reasons"])
            self.assertNotIn("HIGH_MATCH_JOB", below_priority["reasons"])
            connection.execute(delete(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == below_priority_id))
            connection.execute(update(m.matches).where(
                m.matches.c.job_id == request["trigger_job_id"],
                m.matches.c.profile_id == "fixture",
            ).values(score=90))
            request_id = record_discovery_request(
                connection, request["trigger_job_id"], self.store.profile("fixture"),
                is_new_company=False,
            )
            queued_request = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request_id)).mappings().one())
        self.assertIn("HIGH_MATCH_JOB", queued_request["reasons"])
        from services.company_discovery.domains import record_company_domain
        record_company_domain(self.store, request["company_id"], "employer.example",
                              "VERIFIED_EXISTING_DOMAIN", ["Synthetic reviewed evidence"])
        # A reviewed domain allows discovery to queue, but never enables submission.
        with self.store.engine.begin() as connection:
            current = dict(connection.execute(select(m.company_discovery_requests).where(
                m.company_discovery_requests.c.id == request_id)).mappings().one())
        receipt = approve_discovery_request(
            self.store, request_id, "https://employer.example/", current["revision"],
        )
        with self.store.engine.connect() as connection:
            operation = connection.execute(select(m.operations).where(
                m.operations.c.id == receipt["operation_id"])).mappings().one()
        self.assertEqual(operation["params"]["dispatch_priority"], "HIGH")
        self.assertEqual(operation["params"]["fetch_mode"], "PUBLIC_HTTPS")

    def test_outbox_dispatches_high_priority_work_before_older_normal_work(self):
        with self.store.engine.begin() as connection:
            connection.execute(m.operations.insert(), [
                {"id": "normal-company", "kind": "COMPANY_DISCOVER", "entity_id": "normal",
                 "params": {"dispatch_priority": "NORMAL"}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T10:00:00+00:00"},
                {"id": "high-company", "kind": "COMPANY_DISCOVER", "entity_id": "high",
                 "params": {"dispatch_priority": "HIGH"}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T11:00:00+00:00"},
                {"id": "other-kind", "kind": "PREPARE", "entity_id": "profile",
                 "params": {}, "state": "QUEUED", "error": None,
                 "updated_at": "2026-10-09T09:00:00+00:00"},
            ])
            ids = [row["id"] for row in _pending_operations(connection)]
        self.assertEqual(ids, ["high-company", "other-kind", "normal-company"])


if __name__ == "__main__":
    unittest.main()
