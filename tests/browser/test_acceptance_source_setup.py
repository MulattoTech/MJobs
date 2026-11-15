import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from cli.acceptance_source_setup import _policy_sha, compose_owned_public_containment
from cli.acceptance_public_source import _validate_lease_continuity
from packages.persistence.store import identity
from services.discovery.sources.registry import source_adapter
from tests.unit import test_acceptance_containment as owner_fixtures


_TEST_NOW = datetime.now(timezone.utc)
STAMP = (_TEST_NOW - timedelta(seconds=2)).isoformat()
SOURCE = {
    "commit": "a" * 40,
    "tree": "b" * 40,
    "working_tree_diff_sha256": "c" * 64,
    "working_tree_status_sha256": "d" * 64,
}
BASELINE = {
    "source": SOURCE,
    "profile_id": "profile-1",
    "application_id": "application-1",
    "selected_application_job_id": "fixture-job-1",
    "external_submission_enabled": False,
}
RUNTIME = {"source_manifest_sha256": "e" * 64, "images": {"api": "sha256:" + "f" * 64}}
PROOF = {
    "scheduler_contained": True,
    "source_policy_exclusive": True,
    "source_collection_exclusive": True,
    "owner": "browser-test-owner",
    "token": "private-token",
    "lease_expires_at": (_TEST_NOW + timedelta(minutes=5)).isoformat(),
}


class ClockedOwner:
    """A controlled owner that cannot renew an expired or released lease."""
    def __init__(self, now):
        self.now = now
        self.expires = now[0] + timedelta(seconds=30)
        self.owner, self.token = PROOF['owner'], PROOF['token']
        self.calls = []
        self.released = False

    def _check(self):
        if self.released or self.now[0] >= self.expires:
            raise RuntimeError('CONTAINMENT_LEASE_EXPIRED')
        if self.expires <= self.now[0] + timedelta(seconds=16):
            self.expires = self.now[0] + timedelta(seconds=30)

    def __call__(self, stage, context):
        self.calls.append(('primary', stage))
        self._check()
        return dict(PROOF, owner=self.owner, token=self.token, lease_expires_at=self.expires.isoformat())

    def bind_additional_source(self, *, policy, primary_context):
        self.calls.append(('additional', 'bind'))
        self._check()
        return dict(additional_source_absent=True, provider=policy['provider'], tenant='default',
            lease_expires_at=self.expires.isoformat(), lease_model='LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS')

    def additional_source(self, event, context):
        self.calls.append(('additional', event))
        self._check()
        return dict(additional_source_policy_verified=True,
            additional_source_id=context['additional_source_id'],
            additional_source_enabled=context['additional_enabled'], lease_expires_at=self.expires.isoformat())


class SourceSetupLeaseContinuityTests(unittest.TestCase):
    def wrapper(self, *, primary_provider='wwr'):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.now = [datetime.fromisoformat(STAMP)]
        self.owner = ClockedOwner(self.now)
        def forbidden(*args, **kwargs):
            self.fail('Protocol-only test reached browser/API transport')
        wrapped = compose_owned_public_containment(self.owner,
            base_url='http://127.0.0.1:18700', baseline=BASELINE, runtime_identity=RUNTIME,
            acceptance_run_id='lease-continuity-contract', artifact_path=Path(directory) / 'setup.json',
            primary_provider=primary_provider, request=forbidden,
            execution_scope='CONTROLLED_TRANSPORT', utc_now=lambda: self.now[0])
        self.context = dict(provider=primary_provider, base_url='http://127.0.0.1:18700')
        return wrapped

    def test_long_setup_returns_fresh_read_and_continuous_same_owner_checks(self):
        for primary_provider in ('wwr', 'remotive'):
            with self.subTest(primary_provider=primary_provider):
                wrapped = self.wrapper(primary_provider=primary_provider)
                started, prior_expiry = self.now[0], self.owner.expires
                wrapped.extra_id = 'owned-extra'
                wrapped.extra_bound = True
                def setup(context):
                    for _ in range(2):
                        wrapped._with_lease(context,
                            lambda: self.now.__setitem__(0, self.now[0] + timedelta(seconds=20)))
                    wrapped.observation = {'controlled': True}
                wrapped._observe_and_disable = setup
                proof = wrapped('dispatch', self.context)
                self.assertGreater(self.now[0], prior_expiry)
                self.assertGreater(self.owner.expires, self.now[0] + timedelta(seconds=15))
                self.assertEqual(proof['lease_expires_at'], self.owner.expires.isoformat())
                self.assertEqual(self.owner.calls[-1], ('primary', 'read'))
                self.assertEqual(self.owner.calls.count(('primary', 'dispatch')), 1)
                checks = proof['lease_continuity']['checks']
                self.assertEqual(checks[-1]['checked_at'], self.now[0].isoformat())
                self.assertEqual(checks[-1]['lease_expires_at'], proof['lease_expires_at'])
                self.assertEqual(_validate_lease_continuity(proof, prior_expiry=prior_expiry,
                    started_at=started, finished_at=self.now[0], final_expiry=self.owner.expires),
                    proof['lease_continuity'])
                for index, call in enumerate(self.owner.calls):
                    if call[0] == 'additional':
                        self.assertEqual(self.owner.calls[index + 1], ('primary', 'read'))
                self.assertNotIn(PROOF['token'], json.dumps(proof['lease_continuity']))
                self.assertIsNone(wrapped._lease_checks)

    def test_preflight_uses_one_admission_and_fresh_final_read_after_bind_and_ui(self):
        wrapped = self.wrapper()
        def create(context):
            wrapped._with_lease(context,
                lambda: self.now.__setitem__(0, self.now[0] + timedelta(seconds=20)))
            wrapped.extra_id = 'owned-extra'
        wrapped._create_extra = create
        proof = wrapped('preflight', self.context)
        self.assertEqual(self.owner.calls[:3], [('primary', 'preflight'), ('additional', 'bind'), ('primary', 'read')])
        self.assertEqual(self.owner.calls.count(('primary', 'preflight')), 1)
        self.assertEqual(self.owner.calls[-1], ('primary', 'read'))
        self.assertEqual(proof['lease_expires_at'], self.owner.expires.isoformat())
        self.assertEqual(proof['lease_continuity']['checks'][-1]['checked_at'], self.now[0].isoformat())
        self.assertIsNone(wrapped._lease_checks)

    def test_inner_expiry_is_propagated_without_reacquisition_and_capture_is_reset(self):
        wrapped = self.wrapper()
        browser = Mock()
        wrapped.browser = browser
        def setup(context):
            self.now[0] += timedelta(seconds=31)
            wrapped._call_owned('read', context)
        wrapped._observe_and_disable = setup
        original_expiry = self.owner.expires
        with self.assertRaisesRegex(RuntimeError, 'CONTAINMENT_LEASE_EXPIRED'):
            wrapped('dispatch', self.context)
        self.assertEqual(self.owner.expires, original_expiry)
        browser.close.assert_called_once()
        self.assertIsNone(wrapped._lease_checks)
        with self.assertRaisesRegex(RuntimeError, 'CONTAINMENT_LEASE_EXPIRED'):
            wrapped('read', self.context)
        self.assertIsNone(wrapped._lease_checks)
        self.assertEqual(self.owner.expires, original_expiry)

    def test_inner_identity_change_cannot_be_hidden_by_restoring_final_identity(self):
        for attribute in ('owner', 'token'):
            with self.subTest(attribute=attribute):
                wrapped = self.wrapper()
                original = getattr(self.owner, attribute)
                def setup(context):
                    setattr(self.owner, attribute, 'replacement')
                    try:
                        wrapped._call_owned('read', context)
                    finally:
                        setattr(self.owner, attribute, original)
                wrapped._observe_and_disable = setup
                with self.assertRaisesRegex(ValueError, 'SOURCE_SETUP_LEASE_CONTINUITY_LOST'):
                    wrapped('dispatch', self.context)
                self.assertIsNone(wrapped._lease_checks)

    def test_stale_final_owner_proof_and_clock_regression_fail_closed(self):
        for mode in ('expired', 'clock-regressed'):
            with self.subTest(mode=mode):
                wrapped = self.wrapper()
                def setup(_context):
                    if mode == 'expired':
                        self.now[0] += timedelta(seconds=31)
                    else:
                        self.now[0] -= timedelta(seconds=1)
                wrapped._observe_and_disable = setup
                code = 'CONTAINMENT_LEASE_EXPIRED' if mode == 'expired' else 'SOURCE_SETUP_LEASE_CONTINUITY_LOST'
                with self.assertRaisesRegex((RuntimeError, ValueError), code):
                    wrapped('dispatch', self.context)
                self.assertIsNone(wrapped._lease_checks)

    def test_single_callback_checkpoint_count_is_bounded_and_resets_after_failure(self):
        wrapped = self.wrapper()
        def setup(context):
            for _ in range(128):
                wrapped._call_owned('read', context)
        wrapped._observe_and_disable = setup
        with self.assertRaisesRegex(ValueError, 'SOURCE_SETUP_LEASE_CONTINUITY_LIMIT'):
            wrapped('dispatch', self.context)
        self.assertIsNone(wrapped._lease_checks)
        # A separate current read has its own one-check capture, not a replay.
        proof = wrapped('read', self.context)
        self.assertEqual(len(proof['lease_continuity']['checks']), 1)
        self.assertIsNone(wrapped._lease_checks)

    def test_reentrant_callback_is_rejected_and_capture_is_reset(self):
        wrapped = self.wrapper()
        wrapped._observe_and_disable = lambda context: wrapped('read', context)
        with self.assertRaisesRegex(ValueError, 'SOURCE_SETUP_CONTAINMENT_REENTRY'):
            wrapped('dispatch', self.context)
        self.assertIsNone(wrapped._lease_checks)


class SourceSetupBrowserTests(unittest.TestCase):
    def test_unsupported_primary_fails_before_browser_or_owner_calls(self):
        def forbidden(*args, **kwargs):
            self.fail("Unsupported provider reached an execution boundary")
        for provider in ("remoteok", "greenhouse", "", None):
            with self.subTest(provider=provider), self.assertRaisesRegex(
                    ValueError, "PUBLIC_PRIMARY_PROVIDER_UNSUPPORTED"):
                compose_owned_public_containment(forbidden,
                    base_url="http://127.0.0.1:18700", baseline=BASELINE,
                    acceptance_run_id="unsupported-primary", artifact_path=Path("unused.json"),
                    primary_provider=provider, request=forbidden,
                    execution_scope="CONTROLLED_TRANSPORT")

    def test_preexisting_extra_is_preserved_and_setup_fails_closed(self):
        self._exercise_preexisting_extra_is_preserved_and_setup_fails_closed("wwr", "remotive")

    def test_remotive_primary_preexisting_extra_is_preserved_and_setup_fails_closed(self):
        self._exercise_preexisting_extra_is_preserved_and_setup_fails_closed("remotive", "wwr")

    def _exercise_preexisting_extra_is_preserved_and_setup_fails_closed(self, primary_provider, extra_provider):
        existing = {"id": "operator-source", "provider": extra_provider, "tenant": "default",
            "name": "default", "enabled": False}
        writes = []

        def request(method, path, **kwargs):
            if method == "GET" and path in {"/settings", "/health"}:
                return {"operation_mode": "FIXTURE", "external_submission_enabled": False}
            if method == "GET" and path == "/sources":
                return {"sources": [dict(existing)]}
            writes.append((method, path, kwargs))
            raise AssertionError((method, path))

        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "public-source-setup.json"
            wrapped = compose_owned_public_containment(
                lambda *_: dict(PROOF),
                base_url="http://127.0.0.1:18700", baseline=BASELINE,
                acceptance_run_id="step9-preexisting-extra", runtime_identity=RUNTIME,
                artifact_path=artifact_path,
                **({"primary_provider": primary_provider} if primary_provider != "wwr" else {}), request=request, execution_scope="CONTROLLED_TRANSPORT",
                utc_now=lambda: datetime.fromisoformat(STAMP),
            )
            with self.assertRaisesRegex(ValueError, "EXTRA_SOURCE_MUST_BE_ABSENT"):
                wrapped("preflight", {"provider": primary_provider, "base_url": "http://127.0.0.1:18700"})
            self.assertEqual(writes, [])
            self.assertFalse(artifact_path.exists())
            self.assertEqual(existing["enabled"], False)

    def test_extra_public_source_is_observed_then_disabled_before_release(self):
        self._exercise_extra_public_source_is_observed_then_disabled_before_release("wwr", "remotive")

    def test_remotive_primary_extra_public_source_is_observed_then_disabled_before_release(self):
        self._exercise_extra_public_source_is_observed_then_disabled_before_release("remotive", "wwr")

    def _exercise_extra_public_source_is_observed_then_disabled_before_release(self, primary_provider, extra_provider):
        rows = []
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path, kwargs))
            if method == "GET" and path in {"/settings", "/health"}:
                return {"operation_mode": "FIXTURE", "external_submission_enabled": False}
            if method == "GET" and path == "/sources":
                return {"sources": [dict(row) for row in rows]}
            if method == "GET" and path == "/operations":
                return {"operations": []}
            if method == "PUT" and path == "/sources":
                policy = dict(kwargs["json"])
                row = next((item for item in rows if (item["provider"], item["tenant"])
                            == (policy["provider"], policy["tenant"])), None)
                configured = _configured_source(policy, previous=row)
                if row is None:
                    rows.append(configured)
                    source_id = configured["id"]
                else:
                    row.clear()
                    row.update(configured)
                    source_id = row["id"]
                return {"id": source_id}
            raise AssertionError((method, path))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.route("**/*", lambda route: _route_ui_request(route, rows))
                page.goto("http://127.0.0.1:18700/", wait_until="domcontentloaded")
                with tempfile.TemporaryDirectory() as directory:
                    artifact_path = Path(directory) / "public-source-setup.json"
                    stages = []
                    source_events = []
                    class Containment:
                        def __call__(self, stage, context):
                            stages.append(stage)
                            if source_events and "additional_source" in context:
                                self.assert_binding(context["additional_source"])
                            if stage == "release":
                                return {"released": True, "scheduler_contained": False,
                                    "source_policy_exclusive": False, "source_collection_exclusive": False,
                                    "additional_source_exclusive": False, "project": "fixture-project",
                                    "scheduler_container_id": "fixture-scheduler",
                                    "lease_expired_at": STAMP}
                            return dict(PROOF)

                        def assert_binding(self, binding):
                            if binding.get("provider") != extra_provider or binding.get("tenant") != "default":
                                raise AssertionError(binding)
                            if binding.get("source_id") is None:
                                if (binding.get("enabled_policy_sha256") is not None
                                        or binding.get("disabled_policy_sha256") is not None
                                        or binding.get("enabled") is not False):
                                    raise AssertionError(binding)
                            elif (not isinstance(binding.get("source_id"), str)
                                  or not isinstance(binding.get("enabled_policy_sha256"), str)
                                  or binding.get("enabled") not in {True, False}
                                  or binding.get("enabled") is False
                                  and not isinstance(binding.get("disabled_policy_sha256"), str)):
                                raise AssertionError(binding)

                        def bind_additional_source(self, *, policy, primary_context):
                            if policy["provider"] != extra_provider or policy["tenant"] != "default":
                                raise AssertionError(policy)
                            source_events.append(("bind", dict(policy)))
                            return {"additional_source_absent": True, "provider": extra_provider,
                                "tenant": "default", "lease_expires_at": PROOF["lease_expires_at"],
                                "lease_model": "LOCAL_OPERATOR_CLAIM_NO_SERVER_SIDE_CAS"}

                        def additional_source(self, event, context):
                            source_events.append((event, dict(context)))
                            return {"additional_source_policy_verified": True,
                                "additional_source_id": context["additional_source_id"],
                                "additional_source_enabled": context["additional_enabled"],
                                "lease_expires_at": PROOF["lease_expires_at"]}

                        @staticmethod
                        def policy_sha256(source):
                            return _policy_sha(source)

                    wrapped = compose_owned_public_containment(
                        Containment(),
                        base_url="http://127.0.0.1:18700",
                        baseline=BASELINE,
                        acceptance_run_id="step9-browser-contract",
                        runtime_identity=RUNTIME,
                        artifact_path=artifact_path,
                        **({"primary_provider": primary_provider} if primary_provider != "wwr" else {}),
                        request=request,
                        page=page,
                        execution_scope="CONTROLLED_TRANSPORT",
                        utc_now=lambda: datetime.fromisoformat(STAMP),
                    )
                    admitted = wrapped("preflight", {"provider": primary_provider, "base_url": "http://127.0.0.1:18700"})
                    self.assertEqual(admitted['lease_continuity']['schema_version'], 1)
                    self.assertEqual(stages.count('preflight'), 1)
                    self.assertEqual(stages[-1], 'read')
                    self.assertEqual([row["provider"] for row in rows], [extra_provider])

                    primary_policy = {
                        "provider": primary_provider, "tenant": "default", "enabled": True,
                        "poll_interval": 604800, "concurrency": 1, "request_timeout": 10,
                        "rate_limit": 1, "jitter_seconds": 0,
                    }
                    request("PUT", "/sources", json=primary_policy)
                    primary = next(row for row in rows if row["provider"] == primary_provider)
                    dispatched = wrapped("dispatch", {"provider": primary_provider, "base_url": "http://127.0.0.1:18700",
                        "source_id": primary["id"], "source_policy_sha256": _policy_sha(primary)})
                    checks = dispatched['lease_continuity']['checks']
                    self.assertGreater(len(checks), 1)
                    self.assertEqual(checks[-1]['lease_expires_at'], dispatched['lease_expires_at'])
                    self.assertEqual(checks[-1]['token_sha256'], hashlib.sha256(PROOF['token'].encode()).hexdigest())
                    self.assertEqual(stages[-1], 'read')
                    self.assertEqual(stages.count('dispatch'), 1)
                    self.assertNotIn(PROOF['token'], json.dumps(dispatched['lease_continuity']))
                    extra = next(row for row in rows if row["provider"] == extra_provider)
                    self.assertFalse(extra["enabled"])
                    self.assertIsNone(extra["last_attempt"])
                    self.assertIsNone(extra["last_success"])
                    self.assertEqual(extra["jobs_seen"], 0)

                    release_context = {"provider": primary_provider, "base_url": "http://127.0.0.1:18700",
                        "cleanup_status": "RESTORED_DISABLED"}
                    wrapped("release", release_context)
                    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
                    self.assertEqual(artifact["schema_version"], 2)
                    self.assertEqual(artifact["acceptance_observations"][0]["step"], 9)
                    self.assertEqual(artifact["acceptance_observations"][0]["evidence_kind"], "PUBLIC_READ_ONLY")
                    self.assertEqual(artifact["acceptance_observations"][0]["status"], "NOT_RUN")
                    self.assertEqual(artifact["primary"]["provider"], primary_provider)
                    self.assertEqual(artifact["owned_extra"]["provider"], extra_provider)
                    self.assertEqual(artifact["release_receipt"]["binding"]["extra_provider"], extra_provider)
                    self.assertEqual(artifact["owned_extra"]["source_id"], extra["id"])
                    self.assertEqual(artifact["owned_extra"]["disabled"], True)
                    self.assertEqual(artifact["extra_collection_operation_ids"], [])
                    self.assertEqual(artifact["extra_operation_readback_sha256"],
                                     __import__("hashlib").sha256(b"[]").hexdigest())
                    policy_stages = [row["stage"] for row in artifact["extra_policy_readbacks"]]
                    self.assertEqual(policy_stages[:5], [
                        "preflight_absent", "immediately_before_create", "created_enabled",
                        "before_disable", "disabled_readback",
                    ])
                    self.assertEqual(policy_stages[5:], ["release"])
                    operation_stages = [row["stage"] for row in artifact["extra_operation_readbacks"]]
                    self.assertEqual(operation_stages[:2], [
                        "created_before_primary_collection", "disabled_before_primary_collection",
                    ])
                    self.assertEqual(operation_stages[2:], ["release"])
                    self.assertIn("release", [row["stage"] for row in artifact["primary_containment_checks"]])
                    self.assertEqual([row["receipt"].get("released") for row in artifact["primary_containment_checks"]
                                      if row["stage"] == "release"], [True])
                    self.assertTrue(artifact["release_receipt"]["released"])
                    self.assertEqual(artifact["release_receipt"]["receipt"]["project"], "fixture-project")
                    self.assertEqual(artifact["release_receipt"]["binding"]["extra_source_id"], extra["id"])
                    self.assertNotIn('"token":', json.dumps(artifact))
                    self.assertTrue(all(row["verified"] is False
                                        for row in artifact["primary_containment_checks"]))
                    self.assertNotIn("/collect", [path for _, path, _ in calls])
                    self.assertLess(stages.index("finalize"), stages.index("release"))
                    self.assertEqual(source_events[0][0], "bind")
                    self.assertIn("created", [event for event, _ in source_events])
                    self.assertIn("before_disable", [event for event, _ in source_events])
                    self.assertIn("disabled", [event for event, _ in source_events])
                    self.assertIn("release", [event for event, _ in source_events])
            finally:
                browser.close()


def rows_update_from_ui(body, rows):
    import json as json_module

    payload = json_module.loads(body)
    row = next((item for item in rows if (item["provider"], item["tenant"])
                == (payload["provider"], payload["tenant"])), None)
    configured = _configured_source(payload, previous=row)
    if row is None:
        rows.append(configured)
        source_id = configured["id"]
    else:
        row.clear()
        row.update(configured)
        source_id = row["id"]
    return json.dumps({"id": source_id})


def _configured_source(policy, *, previous=None):
    capabilities = source_adapter({
        "provider": policy["provider"], "tenant": policy["tenant"],
        "request_timeout": policy["request_timeout"],
        "rate_limit": policy["rate_limit"], "concurrency": policy["concurrency"],
    }).capabilities()
    return {
        "id": identity(policy["provider"], policy["tenant"]),
        "provider": policy["provider"], "tenant": policy["tenant"], "name": policy["tenant"],
        **policy,
        "strategy": capabilities.strategy.value,
        "capabilities": capabilities.as_dict(),
        "health_status": "NOT_CHECKED",
        "last_attempt": previous.get("last_attempt") if previous else None,
        "last_success": previous.get("last_success") if previous else None,
        "jobs_seen": previous.get("jobs_seen", 0) if previous else 0,
    }


def _route_ui_request(route, rows):
    request = route.request
    if request.url == "http://127.0.0.1:18700/":
        route.fulfill(status=200, content_type="text/html", body=_sources_ui())
    elif request.url == "http://127.0.0.1:18700/sources" and request.method == "GET":
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"sources": rows}))
    elif request.url == "http://127.0.0.1:18700/sources" and request.method == "PUT":
        route.fulfill(status=200, content_type="application/json",
                      body=rows_update_from_ui(request.post_data, rows))
    else:
        route.abort()


class _PublicSetupAPI(owner_fixtures.FakeAPI):
    """Controlled transport shared by independent producer and owner reads."""

    def __init__(self, provider, *, mutate_created=None, mutate_readback=None):
        super().__init__()
        from cli.acceptance_public_source import ENDPOINTS

        self.provider = provider
        self.collection_state = "COMPLETED"
        self.mutate_created = mutate_created
        self.mutate_readback = mutate_readback
        self.creation_read_seen = False
        self.public["attributes"].update(provider=provider, source_endpoint=ENDPOINTS[provider])
        self.public["postings"][0]["source"] = ENDPOINTS[provider]

    def request(self, method, path, **kwargs):
        if (method, path) != ("PUT", "/sources"):
            return super().request(method, path, **kwargs)
        self.calls.append((method, path, deepcopy(kwargs)))
        policy = kwargs["json"]
        old = next((row for row in self.sources if row["provider"] == policy["provider"]
                    and row["tenant"] == policy["tenant"]), None)
        configured = _configured_source(policy, previous=old)
        configured["backoff_until"] = old.get("backoff_until") if old else None
        if policy["provider"] == self.provider:
            configured["id"] = "public-source"
        returned_id = configured["id"]
        if old is None:
            self.sources.append(configured)
            if policy["provider"] == self.provider and self.mutate_created is not None:
                self.mutate_created(configured)
        else:
            old.clear()
            old.update(configured)
        # The inherited controlled collection mutates the primary row only.
        self.sources.sort(key=lambda row: row["provider"] != self.provider)
        return {"id": returned_id}

    def producer_request(self, method, path, **kwargs):
        result = self.request(method, path, **kwargs)
        if ((method, path) == ("GET", "/sources") and not self.creation_read_seen
                and any(row.get("id") == "public-source" for row in self.sources)):
            self.creation_read_seen = True
            if self.mutate_readback is not None:
                self.mutate_readback(result)
        return result


def _run_public_setup_join(tmp_path, provider, **api_options):
    """Run real producers/owner/serializer; every transport and job is controlled."""
    from cli.acceptance_public_source import child_containment_context, run

    fixture = owner_fixtures.OwnedPublicContainmentTests(methodName="runTest")
    fixture.setUp()
    try:
        fixture.api = api = _PublicSetupAPI(provider, **api_options)
        confirmation = {"application_id": "application", "receipt": "controlled-mock", "submission_count": 1}
        api.app["confirmation"] = confirmation
        baseline = {
            "source": {"commit": owner_fixtures.REVISION, "tree": owner_fixtures.TREE,
                "working_tree_diff_sha256": "e" * 64, "working_tree_status_sha256": "e" * 64},
            "profile_id": "profile", "application_id": "application",
            "operation_id": "baseline-operation", "request_id": "baseline-request",
            "profile_version": "v1", "selected_application_job_id": "fixture-job",
            "selected_application_route_id": "fixture-route", "confirmation": confirmation,
            "timestamp": fixture.now[0].isoformat(), "external_submission_enabled": False,
        }
        owner = fixture.containment(baseline=baseline)
        origin = "http://127.0.0.1:18732"
        runtime = {"source_manifest_sha256": "e" * 64, "images": {"api": owner_fixtures.IMAGE_ID}}
        target = {"base_url": origin, "port": 18732, "project_name": owner_fixtures.PROJECT}
        callback_trace = []
        setup_path = tmp_path / "public-source-setup.json"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def route_request(route):
                    request = route.request
                    if request.url == origin + "/" and request.method == "GET":
                        route.fulfill(status=200, content_type="text/html", body=_sources_ui())
                    elif request.url == origin + "/sources" and request.method in {"GET", "PUT"}:
                        options = {"json": request.post_data_json} if request.method == "PUT" else {}
                        value = api.request(request.method, "/sources", **options)
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(value))
                    else:
                        route.abort()

                page.route("**/*", route_request)
                composed = compose_owned_public_containment(owner, base_url=origin, baseline=baseline,
                    acceptance_run_id="controlled-public-setup-join", primary_provider=provider,
                    runtime_identity=runtime, artifact_path=setup_path, request=api.request, page=page,
                    execution_scope="CONTROLLED_TRANSPORT", utc_now=lambda: fixture.now[0])

                def traced_owner(stage, context):
                    callback_trace.append({"stage": stage, "source_id": context.get("source_id"),
                        "source_policy_sha256": context.get("source_policy_sha256")})
                    return composed(stage, context)

                public = run(origin, baseline, tmp_path / "public.json",
                    acceptance_run_id="controlled-public-setup-join", provider=provider,
                    containment_preflight=traced_owner, request=api.producer_request,
                    execution_scope="CONTROLLED_TRANSPORT", utc_now=lambda: fixture.now[0])
                if public["result"] == "PASS":
                    composed("release", {"provider": provider, "base_url": origin,
                        "acceptance_run_id": public["acceptance_run_id"], "newly_created": True,
                        "cleanup_status": public["cleanup"]["status"], **child_containment_context(public),
                        **{key: public[key] for key in ("source_id", "source_policy_sha256",
                            "collection_operation_id", "collection_request_id")}})
                setup = json.loads(setup_path.read_text()) if setup_path.exists() else None
                trace = {"execution_scope": "CONTROLLED_TRANSPORT", "callback_trace": callback_trace,
                    "public_result": public["result"], "source_setup_published": setup is not None}
                (tmp_path / "callback-trace.json").write_text(json.dumps(trace, indent=2) + "\n")
                return {"public": public, "setup": setup, "baseline": baseline, "target": target,
                    "runtime": {**runtime, "containers": {"scheduler": "b" * 64}},
                    "callback_trace": callback_trace, "calls": deepcopy(api.calls),
                    "claim_retained": owner.claim_path.exists(), "paused": fixture.docker.rows["scheduler"]["paused"]}
            finally:
                browser.close()
    finally:
        fixture.doCleanups()


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_public_creation_history_joins_actual_setup_and_journey(tmp_path, provider):
    from cli.acceptance_journey import validate_source_setup_artifact

    observed = _run_public_setup_join(tmp_path, provider)
    public, setup = observed["public"], observed["setup"]
    assert public["result"] == "PASS", public
    assert setup is not None and setup["result"] == "NOT_RUN"
    assert public["cleanup"]["status"] == "RESTORED_DISABLED"
    assert observed["claim_retained"] is False and observed["paused"] is False
    # No seeded serializer/history and no controlled-to-live relabeling.
    row = validate_source_setup_artifact(setup, observed["baseline"], public,
        observed["target"], observed["runtime"])
    assert row["status"] == "NOT_RUN"
    created_reads = [row for row in observed["callback_trace"] if row["source_id"] is not None][:2]
    assert [row["stage"] for row in created_reads] == ["read", "read"]
    assert all(row["source_policy_sha256"] == public["source_policy_sha256"] for row in created_reads)
    assert all(row["primary_policy_sha256"] == public["source_policy_sha256"]
        for row in setup["primary_containment_checks"] if row["primary_source_id"] is not None)
    assert sum(path.endswith("/collect") for _, path, _ in observed["calls"]) == 1
    for field, value in (("primary_source_id", "foreign-source"), ("primary_policy_sha256", None),
                         ("primary_policy_sha256", "f" * 64)):
        changed = deepcopy(setup)
        bound = next(row for row in changed["primary_containment_checks"] if row["primary_source_id"] is not None)
        bound[field] = value
        with pytest.raises(ValueError, match="primary policy changed"):
            validate_source_setup_artifact(changed, observed["baseline"], public,
                observed["target"], observed["runtime"])


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
@pytest.mark.parametrize("field,value", [("rate_limit", 2), ("capabilities", {}),
                                         ("id", "foreign-source")])
def test_created_primary_drift_is_rejected_by_real_owner_before_dispatch(tmp_path, provider, field, value):
    observed = _run_public_setup_join(tmp_path, provider,
        mutate_created=lambda row: row.update({field: value}))
    public = observed["public"]
    assert public["result"] == "FAIL"
    assert public["cleanup"]["status"] == "PRESERVED_OWNERSHIP_CHANGED"
    assert observed["claim_retained"] is True and observed["paused"] is True
    assert observed["setup"] is None
    bound = [row for row in observed["callback_trace"] if row["source_id"] is not None]
    assert bound[0]["stage"] == "read" and bound[0]["source_policy_sha256"] == public["source_policy_sha256"]
    assert not any(row["stage"] == "dispatch" for row in observed["callback_trace"])
    assert not any(path.endswith("/collect") for _, path, _ in observed["calls"])
    primary_writes = [options["json"] for method, path, options in observed["calls"]
                      if method == "PUT" and path == "/sources" and options["json"]["provider"] == provider]
    assert len(primary_writes) == 1 and primary_writes[0]["enabled"] is True


@pytest.mark.parametrize("provider", ["wwr", "remotive"])
def test_forged_producer_readback_cannot_replace_owner_verified_creation_constraint(tmp_path, provider):
    def forge(response):
        primary = next(row for row in response["sources"] if row["provider"] == provider)
        primary["capabilities"] = {}

    observed = _run_public_setup_join(tmp_path, provider, mutate_readback=forge)
    public = observed["public"]
    assert public["result"] == "FAIL" and public["error_code"] == "SOURCE_CONFIGURATION_CHANGED"
    assert public["cleanup"]["status"] == "PRESERVED_POLICY_CHANGED"
    assert observed["claim_retained"] is True and observed["paused"] is True
    assert observed["setup"] is None
    assert not any(row["stage"] == "dispatch" for row in observed["callback_trace"])
    assert not any(path.endswith("/collect") for _, path, _ in observed["calls"])
    primary_writes = [options["json"] for method, path, options in observed["calls"]
                      if method == "PUT" and path == "/sources" and options["json"]["provider"] == provider]
    assert len(primary_writes) == 1 and primary_writes[0]["enabled"] is True


def _sources_ui():
    return """
    <nav aria-label="Main navigation"><button type="button">Sources</button></nav>
    <form id="source-form">
      <label>Source provider<select aria-label="Source provider">
        <option value="wwr">We Work Remotely</option><option value="remotive">Remotive</option>
      </select></label>
      <label>Interval (minutes)<input aria-label="Interval (minutes)" value="60"></label>
      <label>Request timeout (seconds)<input aria-label="Request timeout (seconds)" value="10"></label>
      <label>Rate limit (requests/minute)<input aria-label="Rate limit (requests/minute)" value="60"></label>
      <label>Source concurrency<input aria-label="Source concurrency" value="1"></label>
      <label>Schedule jitter (seconds)<input aria-label="Schedule jitter (seconds)" value="30"></label>
      <button type="submit">Add source</button>
    </form><p role="status" id="notice"></p><main id="sources"></main>
    <script>
      async function load(){const rows=await (await fetch('/sources')).json();
        document.querySelector('#sources').innerHTML=rows.sources.map(s=>`<section class="panel">
          <div class="toolbar"><div><h2>${s.name}</h2><p>${s.provider} / ${s.tenant}</p></div>
          <span class="tag">${s.enabled?'HEALTHY':'DISABLED'}</span></div>
          <p>FEED · every ${s.poll_interval/60} minutes</p><button type="button">Collect now</button>
          <button class="quiet" type="button">${s.enabled?'Disable':'Enable'}</button></section>`).join('');
        document.querySelectorAll('#sources section').forEach((section,i)=>section.querySelectorAll('button')[1].onclick=async()=>{
          const rows=await (await fetch('/sources')).json(); const s=rows.sources[i];
          await fetch('/sources',{method:'PUT',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({provider:s.provider,tenant:s.tenant,enabled:!s.enabled,
              poll_interval:s.poll_interval,concurrency:s.concurrency,request_timeout:s.request_timeout,
              rate_limit:s.rate_limit,jitter_seconds:s.jitter_seconds})}); await load();});
      }
      document.querySelector('#source-form').onsubmit=async e=>{e.preventDefault();
        await fetch('/sources',{method:'PUT',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({provider:document.querySelector('[aria-label="Source provider"]').value,
            tenant:'default',enabled:true,poll_interval:Number(document.querySelector('[aria-label="Interval (minutes)"]').value)*60,
            request_timeout:Number(document.querySelector('[aria-label="Request timeout (seconds)"]').value),
            rate_limit:Number(document.querySelector('[aria-label="Rate limit (requests/minute)"]').value),
            concurrency:Number(document.querySelector('[aria-label="Source concurrency"]').value),
            jitter_seconds:Number(document.querySelector('[aria-label="Schedule jitter (seconds)"]').value)})});
        await load(); document.querySelector('#notice').textContent='Source saved. Create a search profile below to enable scheduled discovery.';}; load();
    </script>"""


if __name__ == "__main__":
    unittest.main()
