"""Real controlled manual contexts retain ownership, cleanup, and profile metadata."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from packages.persistence import models as m
from tests.integration import test_manual_auth_session as sessions
from tests.integration.test_named_browser_profile import _close_manual_auth_fixture


@contextmanager
def controlled_manual_window(events, *, sign_in=False, profiles=None):
    """Substitute display/human actions, retaining real production browser IO."""
    class Page:
        def __init__(self, page):
            self.page = page

        def goto(self, *args, **kwargs):
            return self.page.goto(*args, **kwargs)

        def is_closed(self):
            return self.page.is_closed()

        def wait_for_timeout(self, milliseconds):
            if sign_in:
                self.page.set_default_timeout(5000)
                self.page.get_by_label("Fixture username", exact=True).fill("synthetic-fixture-user")
                self.page.get_by_label("Fixture password", exact=True).fill("synthetic-fixture-only-password")
                self.page.get_by_role("button", name="Sign in", exact=True).click()
                self.page.wait_for_url("**/ats/apply/*?auth_expired=true")
                assert self.page.locator("[data-auth-required]").count() == 0
                assert self.page.get_by_label("Resume", exact=True).count() == 1
                events.append("fixture_signed_in")
            self.page.close()
            events.append("user_closed_page")
            # Real Playwright raises TargetClosedError here.
            return self.page.wait_for_timeout(milliseconds)

    class Context:
        def __init__(self, context):
            self.context = context

        @property
        def browser(self):
            return self.context.browser

        @property
        def pages(self):
            return [Page(page) for page in self.context.pages]

        def new_page(self):
            return Page(self.context.new_page())

        def route(self, *args, **kwargs):
            return self.context.route(*args, **kwargs)

        def close(self):
            self.context.close()
            events.append("context_cleanup_complete")

    class Chromium:
        def __init__(self, chromium):
            self.chromium = chromium

        def launch_persistent_context(self, *args, **kwargs):
            kwargs["headless"] = True
            context = self.chromium.launch_persistent_context(*args, **kwargs)
            if profiles is not None:
                profiles.append(Path(args[0]).resolve())
            return Context(context)

    class Manager:
        def __enter__(self):
            self.manager = sync_playwright()
            self.playwright = self.manager.__enter__()
            return SimpleNamespace(chromium=Chromium(self.playwright.chromium))

        def __exit__(self, *args):
            events.append("driver_exit")
            return self.manager.__exit__(*args)

    def legacy_window(**kwargs):
        from services.applications.mock_adapter import open_mock_authentication
        for key in ("operation_id", "owner_token", "budget_seconds"):
            kwargs.pop(key, None)
        return open_mock_authentication(**kwargs)

    # This qualifies the existing in-process production adapter. The Windows
    # isolated bootstrap is qualified separately; no subprocess proof is implied.
    with patch.dict(os.environ, {"MJOBS_BROWSER_DEBUG_CAPTURE": "0", "MJOBS_OPERATION_TIMEOUT": "30"}), \
            patch("playwright.sync_api.sync_playwright", side_effect=Manager), \
            patch("apps.api.manual_auth.open_mock_authentication", side_effect=legacy_window):
        yield


class ManualAuthCloseLifecycleTests(unittest.TestCase):
    setUp = sessions.ManualAuthSessionApiTests.setUp
    tearDown = sessions.ManualAuthSessionApiTests.tearDown

    def test_user_close_during_wait_releases_only_owned_capacity_before_driver_exit(self):
        events = []
        before = self.store.application(self.application_id)
        attempts = self.store.count("application_attempts")
        with controlled_manual_window(events):
            for _ in range(2):  # Second launch is an explicit retry with the same saved profile.
                response = self.client.post(self.url)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], "MANUAL_AUTH_WINDOW_CLOSED")
        self.assertEqual(events, ["user_closed_page", "context_cleanup_complete", "driver_exit"] * 2)
        self.assertEqual(self.store.application(self.application_id), before)
        self.assertEqual(self.store.count("application_attempts"), attempts)
        with self.store.engine.connect() as connection:
            states = connection.execute(select(m.operations.c.state).where(
                m.operations.c.kind == "MANUAL_AUTH")).scalars().all()
        self.assertEqual(states, ["COMPLETED", "COMPLETED"])
        from services.applications.browser_process import process_status
        with self.store.engine.connect() as connection:
            owners = connection.execute(select(m.operations.c.recovery_metadata).where(
                m.operations.c.kind == "MANUAL_AUTH")).scalars().all()
        for owner in owners:
            self.assertEqual(process_status(owner["manual_auth_owner"]["process"]), "EXITED")
        import httpx
        self.assertEqual(httpx.get(self.origin + "/_test/request-counts").json(),
                         {"review": 0, "submit": 0})


def test_real_manual_sign_in_preserves_profile_metadata_and_requires_explicit_rerun(record_property):
    """Session reuse is proved by a later real dry run, never by reading cookies."""
    import httpx
    from services.applications.browser_process import process_status
    from services.applications.mock_adapter import apply_mock

    fixture = sessions.ManualAuthSessionApiTests()
    receipt = {
        "schema_version": 1, "qualified": False,
        "scope": "controlled-local-fixture-in-process-manual-auth",
        "authentication_hint_is_authorization": False,
        "cookie_or_storage_api_reads": False,
        "windows_isolated_runtime_qualified": False,
    }
    try:
        # The legal nested label makes accidental profile.parent inference visible.
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "browser-profile"}):
            fixture.setUp()
        expected = fixture.root / fixture.application_id / "browser-profile/browser-profile"
        path = expected / "mjobs-profile-metadata.json"
        assert path.is_file(), "The actual initial browser launch must persist profile metadata"
        from services.applications.browser_profile_metadata import read_browser_profile_metadata
        def saved():
            return read_browser_profile_metadata(
                fixture.root / fixture.application_id, expected,
                provider="mock-ats", site=fixture.origin)

        initial = saved()
        assert initial["status"] == "AUTH_REQUIRED"
        assert initial["likely_authenticated"] is False
        assert initial["profile_name"] == "browser-profile"
        receipt["initial"] = initial
        before = fixture.store.application(fixture.application_id)
        attempts = fixture.store.count("application_attempts")
        selection = (fixture.root / fixture.application_id / "browser-profile-selection.json").read_bytes()
        events, profiles = [], []
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "different"}), \
                controlled_manual_window(events, sign_in=True, profiles=profiles):
            response = fixture.client.post(fixture.url)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "MANUAL_AUTH_WINDOW_CLOSED"
        assert profiles == [expected.resolve()]
        assert events == ["fixture_signed_in", "user_closed_page",
                          "context_cleanup_complete", "driver_exit"]
        assert fixture.store.application(fixture.application_id) == before
        assert fixture.store.count("application_attempts") == attempts
        assert httpx.get(fixture.origin + "/_test/request-counts").json() == {"review": 0, "submit": 0}
        manual = saved()
        assert manual["profile_id"] == initial["profile_id"]
        assert manual["created_at"] == initial["created_at"]
        assert manual["last_used"] > initial["last_used"]
        assert manual["status"] == "AUTHENTICATION_UNKNOWN"
        assert manual["likely_authenticated"] is None
        with fixture.store.engine.connect() as connection:
            owners = connection.execute(select(m.operations).where(
                m.operations.c.kind == "MANUAL_AUTH")).mappings().all()
        assert len(owners) == 1 and owners[0]["state"] == "COMPLETED"
        owner_status = process_status(owners[0]["recovery_metadata"]["manual_auth_owner"]["process"])
        assert owner_status == "EXITED"
        receipt.update({
            "after_manual": manual, "manual_application_state": before["state"],
            "manual_attempt_count_unchanged": True, "manual_owner_status": owner_status,
            "manual_request_counts": {"review": 0, "submit": 0}, "manual_events": events,
            "actual_launch_path_matches_pinned_profile": True,
        })

        # This distinct explicit operation uses the retained session and cannot submit.
        resumed = apply_mock(
            fixture.store, fixture.application_id, fixture.origin, fixture.root,
            submit=False, request_id="browser-profile-metadata-explicit-rerun")
        assert resumed["state"] == "DRY_RUN_COMPLETE"
        assert fixture.store.count("application_attempts") == attempts + 1
        counts = httpx.get(fixture.origin + "/_test/request-counts").json()
        assert counts == {"review": 1, "submit": 0}
        final = saved()
        assert final["profile_id"] == initial["profile_id"]
        assert final["created_at"] == initial["created_at"]
        assert final["last_used"] > manual["last_used"]
        assert final["likely_authenticated"] is None
        assert (fixture.root / fixture.application_id / "browser-profile-selection.json").read_bytes() == selection
        exposed = json.dumps([initial, manual, final, response.json()])
        for private in ("mock_session", "fixture-authenticated", "synthetic-fixture-only-password"):
            assert private not in exposed
        receipt.update({
            "qualified": True, "after_explicit_dry_run": final,
            "final_application_state": resumed["state"], "final_request_counts": counts,
            "profile_identity_and_creation_preserved": True,
            "selection_manifest_unchanged": True,
        })
    finally:
        # Preserve the outcome and observed metadata before fixture cleanup.
        try:
            record_property("browser_profile_metadata_receipt", json.dumps(receipt, sort_keys=True))
        finally:
            _close_manual_auth_fixture(fixture)
