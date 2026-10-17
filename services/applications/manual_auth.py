"""Explicit, local-only sign-in window for an interrupted mock application."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import math
import re
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import select

from packages.persistence import models as m
from packages.persistence.store import now
from services.discovery.pipeline import MockClient


_SAFE_REQUISITION = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MANUAL_AUTH_WINDOW_CLOSED = "MANUAL_AUTH_WINDOW_CLOSED"
OPEN_UNAVAILABLE = "OPEN_UNAVAILABLE"
STALE = "STALE"
_AUTH_DECLARED = "USER_AUTH_COMPLETION_DECLARED"
_AUTH_REVOKED = "USER_AUTH_COMPLETION_REVOKED"
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MAX_AUTH_DECLARATION_SECONDS = 900


class ManualAuthCompletionError(ValueError):
    """Stable, value-free failure for user-declared auth lifecycle actions."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _completion_error(code: str):
    raise ManualAuthCompletionError(code)


def _completion_identity(connection, profile_id: str, application_id: str,
                         review_operation_id: str, *, expected_route_url=None):
    """Load and validate the exact live Workday review identity under locks."""
    # Preserve form-review admission's application -> profile -> route order.
    application = connection.execute(select(m.applications).where(
        m.applications.c.id == application_id,
        m.applications.c.profile_id == profile_id,
    ).with_for_update()).mappings().one_or_none()
    if application is None:
        _completion_error("AUTH_COMPLETION_NOT_FOUND")
    if application["target"] != "REAL":
        _completion_error("AUTH_COMPLETION_STALE")

    profile_version = connection.execute(select(m.profiles.c.version).where(
        m.profiles.c.id == profile_id,
    ).with_for_update()).scalar_one_or_none()
    if not isinstance(profile_version, str) or not profile_version:
        _completion_error("AUTH_COMPLETION_STALE")
    route_row = connection.execute(select(m.routes).where(
        m.routes.c.id == application["route_id"],
    ).with_for_update()).mappings().one_or_none()
    if route_row is None or route_row["provider"] != "workday":
        _completion_error("AUTH_COMPLETION_STALE")

    try:
        from services.applications.adapters.workday import WorkdayApplicationAdapter

        route = WorkdayApplicationAdapter().normalize_route({
            "provider": route_row["provider"],
            "tenant": route_row["tenant"],
            "requisition": route_row["requisition"],
            "url": route_row["url"],
        })
    except Exception:
        route = None
    if route is None or route["url"] != route_row["url"]:
        _completion_error("AUTH_COMPLETION_STALE")
    parsed_route = urlsplit(route["url"])
    if expected_route_url is not None and (
            not isinstance(expected_route_url, str)
            or expected_route_url != route["url"]):
        _completion_error("AUTH_COMPLETION_STALE")

    operation = connection.execute(select(m.operations).where(
        m.operations.c.id == review_operation_id,
        m.operations.c.kind == "APPLICATION_FORM_REVIEW",
        m.operations.c.entity_id == application_id,
    ).with_for_update()).mappings().one_or_none()
    if operation is None:
        _completion_error("AUTH_COMPLETION_NOT_FOUND")
    if operation["cancel_requested"] is True or operation["state"] in {
            "CANCELLED", "CANCELED", "FAILED", "DISCARDED", "DEAD_LETTERED"}:
        _completion_error("AUTH_COMPLETION_REVIEW_CANCELLED")
    if operation["state"] not in {"QUEUED", "RUNNING", "COMPLETED"}:
        _completion_error("AUTH_COMPLETION_STALE")

    params = operation["params"] if isinstance(operation["params"], dict) else {}
    fingerprint = params.get("input_fingerprint")
    if (params.get("profile_id") != profile_id
            or params.get("application_id") != application_id
            or params.get("route_id") != application["route_id"]
            or params.get("provider") != "workday"
            or params.get("route_url") != route["url"]
            or params.get("profile_version") != profile_version
            or not isinstance(fingerprint, str)
            or not _FINGERPRINT.fullmatch(fingerprint)):
        _completion_error("AUTH_COMPLETION_STALE")

    return {
        "schema_version": 1,
        "profile_id": profile_id,
        "application_id": application_id,
        "review_operation_id": review_operation_id,
        "route_id": application["route_id"],
        "provider": "workday",
        "tenant": route["tenant"],
        "requisition": route["requisition"],
        "route_origin": f"{parsed_route.scheme}://{parsed_route.netloc}",
        "route_url_sha256": hashlib.sha256(route["url"].encode("utf-8")).hexdigest(),
        "profile_version": profile_version,
        "review_fingerprint": fingerprint,
        "authenticated_session_available": False,
    }


def _auth_events(connection, application_id: str, review_operation_id: str):
    rows = connection.execute(select(m.audit).where(
        m.audit.c.entity_id == application_id,
        m.audit.c.action.in_((_AUTH_DECLARED, _AUTH_REVOKED)),
    ).order_by(m.audit.c.timestamp.desc(), m.audit.c.id.desc())).mappings()
    return [dict(row) for row in rows
            if isinstance(row["payload"], dict)
            and row["payload"].get("review_operation_id") == review_operation_id]


def _completion_projection(payload: dict, status: str):
    return {
        "status": status,
        "profile_id": payload["profile_id"],
        "application_id": payload["application_id"],
        "review_operation_id": payload["review_operation_id"],
        "route_id": payload["route_id"],
        "provider": payload["provider"],
        "tenant": payload["tenant"],
        "requisition": payload["requisition"],
        "route_origin": payload["route_origin"],
        "route_url_sha256": payload["route_url_sha256"],
        "profile_version": payload["profile_version"],
        "review_fingerprint": payload["review_fingerprint"],
        "declared_at": payload["declared_at"],
        "expires_at": payload["expires_at"],
        "authenticated_session_available": False,
    }


def _assert_declaration_current(payload: dict, current: dict):
    if any(payload.get(key) != current.get(key) for key in (
            "schema_version", "profile_id", "application_id", "review_operation_id",
            "route_id", "provider", "tenant", "requisition", "route_origin",
            "route_url_sha256", "profile_version", "review_fingerprint",
            "authenticated_session_available")):
        _completion_error("AUTH_COMPLETION_STALE")
    if (payload.get("declaration") != "USER_DECLARED"
            or not isinstance(payload.get("declared_at"), str)
            or not isinstance(payload.get("expires_at"), str)
            or type(payload.get("expires_in_seconds")) is not int
            or not 1 <= payload["expires_in_seconds"] <= _MAX_AUTH_DECLARATION_SECONDS):
        _completion_error("AUTH_COMPLETION_STALE")


def record_user_auth_completion(store, profile_id: str, application_id: str,
                                review_operation_id: str, *,
                                expected_route_url: str,
                                expires_in_seconds: int = 900) -> dict:
    """Persist a bounded USER_DECLARED marker, never an authenticated session.

    The value is bound to one current Workday route, profile version and public
    form-review fingerprint. It does not inspect cookies, credentials, browser
    state, or authorize a fetch, retry, resume, or submission.
    """
    if (type(expires_in_seconds) is not int
            or not 1 <= expires_in_seconds <= _MAX_AUTH_DECLARATION_SECONDS):
        _completion_error("AUTH_COMPLETION_REQUEST_INVALID")
    if (not isinstance(expected_route_url, str) or not expected_route_url.strip()
            or len(expected_route_url) > 2048
            or any(not isinstance(value, str) or not value.strip() or len(value) > 128
                   for value in (profile_id, application_id, review_operation_id))):
        _completion_error("AUTH_COMPLETION_REQUEST_INVALID")

    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = _completion_identity(
            connection, profile_id, application_id, review_operation_id,
            expected_route_url=expected_route_url,
        )
        events = _auth_events(connection, application_id, review_operation_id)
        if events:
            latest = events[0]
            payload = latest["payload"]
            _assert_declaration_current(payload, current)
            if latest["action"] == _AUTH_REVOKED:
                _completion_error("AUTH_COMPLETION_REVOKED")
            try:
                expires_at = datetime.fromisoformat(payload["expires_at"])
            except (KeyError, TypeError, ValueError):
                _completion_error("AUTH_COMPLETION_STALE")
            if expires_at.tzinfo is None or expires_at <= _utc_now():
                _completion_error("AUTH_COMPLETION_EXPIRED")
            if payload.get("expires_in_seconds") != expires_in_seconds:
                _completion_error("AUTH_COMPLETION_CONFLICT")
            return _completion_projection(payload, "USER_DECLARED")

        declared_time = _utc_now().astimezone(timezone.utc)
        declared_at = declared_time.isoformat()
        expires_at = (declared_time + timedelta(seconds=expires_in_seconds)).isoformat()
        payload = {
            **current,
            "declaration": "USER_DECLARED",
            "expires_in_seconds": expires_in_seconds,
            "declared_at": declared_at,
            "expires_at": expires_at,
        }
        store._audit(connection, application_id, _AUTH_DECLARED, payload)
        return _completion_projection(payload, "USER_DECLARED")


def current_user_auth_completion(store, profile_id: str, application_id: str,
                                 review_operation_id: str) -> dict:
    """Read a still-current declaration without renewing or fetching a session."""
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = _completion_identity(
            connection, profile_id, application_id, review_operation_id,
        )
        events = _auth_events(connection, application_id, review_operation_id)
        if not events:
            _completion_error("AUTH_COMPLETION_NOT_FOUND")
        latest = events[0]
        payload = latest["payload"]
        _assert_declaration_current(payload, current)
        if latest["action"] == _AUTH_REVOKED:
            _completion_error("AUTH_COMPLETION_REVOKED")
        try:
            expires_at = datetime.fromisoformat(payload["expires_at"])
        except (KeyError, TypeError, ValueError):
            _completion_error("AUTH_COMPLETION_STALE")
        if expires_at.tzinfo is None or expires_at <= _utc_now():
            _completion_error("AUTH_COMPLETION_EXPIRED")
        return _completion_projection(payload, "USER_DECLARED")


def revoke_user_auth_completion(store, profile_id: str, application_id: str,
                                review_operation_id: str) -> dict:
    """Append a durable revocation for this one review without changing app state."""
    with store.engine.begin() as connection:
        if connection.dialect.name == "sqlite":
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        current = _completion_identity(
            connection, profile_id, application_id, review_operation_id,
        )
        events = _auth_events(connection, application_id, review_operation_id)
        if not events:
            _completion_error("AUTH_COMPLETION_NOT_FOUND")
        latest = events[0]
        payload = latest["payload"]
        _assert_declaration_current(payload, current)
        if latest["action"] == _AUTH_REVOKED:
            return _completion_projection(payload, "REVOKED")
        revoked = {**payload, "revoked_at": now()}
        store._audit(connection, application_id, _AUTH_REVOKED, revoked)
        return _completion_projection(revoked, "REVOKED")


def _browser_wait_ms(check, maximum_ms):
    """Never pass Playwright zero (unbounded) or exceed the owning budget."""
    if check is not None:
        check()
    remaining_seconds = getattr(check, "remaining_seconds", None)
    if not callable(remaining_seconds):
        return maximum_ms
    remaining = remaining_seconds()
    if (type(remaining) not in (int, float) or not math.isfinite(remaining)
            or remaining < .001):
        raise TimeoutError("Manual auth budget expired or invalid")
    return min(maximum_ms, int(remaining * 1000))


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
                            check=None, on_process=None) -> str:
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
    try:
        client = MockClient(mock_origin, check=check)
        if not client.allowed(route["url"]):
            client.close()
            return STALE
        login_url = _manual_login_url(mock_origin, route["requisition"], client)
        if login_url is None:
            client.close()
            return STALE
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        return OPEN_UNAVAILABLE
    return run_browser(profile, login_url, client, check=check, on_process=on_process)


def run_browser(profile: Path, login_url: str, client: MockClient,
                *, check=None, on_process=None) -> str:
    """Run the validated fixture window in the caller's containment boundary."""
    context = None
    launch_attempted = False
    close_confirmed = False
    try:
        if check is not None:
            check()

        # Import lazily so unsupported local runtimes fail closed without
        # preventing the API from serving unrelated application workflows.
        from playwright.sync_api import Error as PlaywrightError, sync_playwright

        with sync_playwright() as playwright:
            launch_timeout = _browser_wait_ms(check, 15000)
            launch_attempted = True
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False, args=["--disable-gpu", "--enable-automation"], timeout=launch_timeout)
            try:
                if on_process is not None:
                    from services.applications.browser_process import capture_browser_identity

                    # Persist before navigation or any wait for user input. A
                    # crash before this observation remains unknown and held.
                    on_process(capture_browser_identity(context, profile))
                context.route("**/*", lambda browser_route: (
                    browser_route.continue_()
                    if client.allowed(browser_route.request.url)
                    else browser_route.abort()))
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(login_url, wait_until="domcontentloaded",
                          timeout=_browser_wait_ms(check, 15000))
                while not page.is_closed():
                    try:
                        page.wait_for_timeout(_browser_wait_ms(check, 250))
                    except PlaywrightError:
                        # The user can close the window while the wait is in
                        # flight. A transport failure with a live page remains
                        # unavailable; it is not evidence of a user close.
                        if not page.is_closed():
                            raise
                        break
            finally:
                # The context needs its live driver even after the page closes.
                # Cleanup must precede sync_playwright's __exit__ on every path.
                context.close()
                close_confirmed = True
        return MANUAL_AUTH_WINDOW_CLOSED
    except Exception:
        # Browser/desktop availability errors can include local profile paths
        # or page URLs; return only a stable, value-free status.
        # A failed launch may have created a process without returning its
        # context. No context handle means we cannot confirm browser closure.
        if launch_attempted and not close_confirmed:
            return "CLOSE_UNCONFIRMED"
        return OPEN_UNAVAILABLE
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                if launch_attempted and not close_confirmed:
                    return "CLOSE_UNCONFIRMED"
                return OPEN_UNAVAILABLE
