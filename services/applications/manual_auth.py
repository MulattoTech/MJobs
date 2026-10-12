"""Explicit, local-only sign-in window for an interrupted mock application."""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import select

from packages.persistence import models as m
from services.discovery.pipeline import MockClient


_SAFE_REQUISITION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MANUAL_AUTH_WINDOW_CLOSED = "MANUAL_AUTH_WINDOW_CLOSED"
OPEN_UNAVAILABLE = "OPEN_UNAVAILABLE"
STALE = "STALE"


def _manual_login_url(mock_origin: str, requisition: str, client: MockClient) -> str | None:
    """Build the one supported fixture login URL without trusting stored route paths."""
    if not _SAFE_REQUISITION.fullmatch(requisition):
        return None
    origin = urlsplit(mock_origin)
    if (origin.scheme != "http" or not origin.netloc or origin.username or origin.password
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        return None
    login = urlunsplit((origin.scheme, origin.netloc,
                        f"/ats/login/{quote(requisition, safe='')}", "", ""))
    return login if client.allowed(login) else None


def _resolve_auth_attempt(store, application_id: str, attempt_id: str,
                          output_dir: str | Path):
    """Return the exact persisted mock attempt and its private browser profile."""
    if not isinstance(application_id, str) or not application_id.strip():
        return None
    if not isinstance(attempt_id, str) or not attempt_id.strip():
        return None

    workspace = store.workspace_status()
    if workspace.get("state") != "ACTIVE" or workspace.get("operation_mode") != "FIXTURE":
        return None

    try:
        application = store.application(application_id)
    except Exception:
        return None
    if application.get("target") != "MOCK" or application.get("state") != "AUTH_REQUIRED":
        return None

    with store.engine.connect() as connection:
        route = connection.execute(select(m.routes).where(
            m.routes.c.id == application["route_id"])).mappings().one_or_none()
        latest_attempt = connection.execute(select(m.attempts).where(
            m.attempts.c.application_id == application_id).order_by(
                m.attempts.c.started_at.desc(), m.attempts.c.id.desc()).limit(1)
        ).mappings().one_or_none()
        selected_attempt = connection.execute(select(m.attempts).where(
            m.attempts.c.id == attempt_id,
            m.attempts.c.application_id == application_id,
        )).mappings().one_or_none()

    if (route is None or route["provider"] != "mock-ats" or latest_attempt is None
            or selected_attempt is None or latest_attempt["id"] != attempt_id
            or selected_attempt["state"] != "AUTH_REQUIRED"
            or "NAVIGATE:COMPLETED" not in (selected_attempt["steps"] or [])):
        return None

    root = Path(output_dir).resolve()
    application_root = (root / application_id).resolve()
    profile = (application_root / "browser-profile").resolve()
    if (not application_root.is_relative_to(root)
            or not profile.is_relative_to(application_root)
            or not profile.is_dir()):
        return None
    return dict(route), profile


def open_manual_auth_window(store, application_id: str, attempt_id: str,
                            mock_origin: str, output_dir: str | Path,
                            check=None) -> str:
    """Open the attempt's existing mock profile and wait for the user to close it.

    This function never queues or reruns an application. It does not inspect,
    persist, or log credentials or cookies. The caller must expose it only from
    an explicit user action and keep the application in AUTH_REQUIRED until a
    separate explicit rerun is requested.
    """
    resolved = _resolve_auth_attempt(store, application_id, attempt_id, output_dir)
    if resolved is None:
        return STALE
    route, profile = resolved

    client = None
    context = None
    try:
        client = MockClient(mock_origin, check=check)
        if not client.allowed(route["url"]):
            return STALE
        login_url = _manual_login_url(mock_origin, route["requisition"], client)
        if login_url is None:
            return STALE
        if check is not None:
            check()

        # Import lazily so unsupported local runtimes fail closed without
        # preventing the API from serving unrelated application workflows.
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False, args=["--disable-gpu"], timeout=15000)
            context.route("**/*", lambda browser_route: (
                browser_route.continue_()
                if client.allowed(browser_route.request.url)
                else browser_route.abort()))
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
            while not page.is_closed():
                if check is not None:
                    check()
                page.wait_for_timeout(250)
        return MANUAL_AUTH_WINDOW_CLOSED
    except Exception:
        # Browser/desktop availability errors can include local profile paths
        # or page URLs; return only a stable, value-free status.
        return OPEN_UNAVAILABLE
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                # Never release admission when browser cleanup is uncertain.
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                return "CLOSE_UNCONFIRMED"
        if client is not None:
            client.close()
