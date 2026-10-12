"""Legacy in-process lifecycle through the API's supported adapter boundary."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from packages.persistence import models as m
from tests.integration import test_manual_auth_session as sessions


class ManualAuthCloseLifecycleTests(unittest.TestCase):
    setUp = sessions.ManualAuthSessionApiTests.setUp
    tearDown = sessions.ManualAuthSessionApiTests.tearDown

    def test_user_close_during_wait_releases_only_owned_capacity_before_driver_exit(self):
        events = []

        class Page:
            def __init__(self, page):
                self.page = page

            def goto(self, *args, **kwargs):
                return self.page.goto(*args, **kwargs)

            def is_closed(self):
                return self.page.is_closed()

            def wait_for_timeout(self, milliseconds):
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
                return Context(self.chromium.launch_persistent_context(*args, **kwargs))

        class Manager:
            def __enter__(self):
                self.manager = sync_playwright()
                self.playwright = self.manager.__enter__()
                return SimpleNamespace(chromium=Chromium(self.playwright.chromium))

            def __exit__(self, *args):
                events.append("driver_exit")
                return self.manager.__exit__(*args)

        before = self.store.application(self.application_id)
        attempts = self.store.count("application_attempts")
        def legacy_window(**kwargs):
            from services.applications.mock_adapter import open_mock_authentication
            for key in ('operation_id', 'owner_token', 'budget_seconds'):
                kwargs.pop(key, None)
            return open_mock_authentication(**kwargs)

        # The non-Windows adapter still needs this close-during-wait regression.
        # Native isolated API admission has its own actual process/browser test.
        with patch("playwright.sync_api.sync_playwright", side_effect=Manager), \
                patch('apps.api.manual_auth.open_mock_authentication', side_effect=legacy_window):
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
