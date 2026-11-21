"""Real fixture browser launch and subsequent auth use one pinned selection."""
import io
import json
import os
import sys
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from services.applications.manual_auth import _resolve_auth_attempt
from services.applications import manual_auth_host
from tests.integration import test_manual_auth_session as session_fixture


def _close_manual_auth_fixture(fixture):
    """Close partial/full setup without replacing the first test failure."""
    primary = sys.exc_info()[1]
    cleanup_error = None
    actions = []
    client = getattr(fixture, "client", None)
    if client is not None:
        actions.append(client.close)
    store = getattr(fixture, "store", None)
    if store is not None:
        actions.append(store.engine.dispose)
    directory = getattr(fixture, "directory", None)
    if directory is not None:
        actions.append(directory.cleanup)

    def registered():
        if not fixture.doCleanups():
            raise AssertionError("Registered manual fixture cleanup failed")
    actions.append(registered)
    for action in actions:
        try:
            action()
        except Exception as error:
            if primary is not None:
                primary.add_note("Manual fixture cleanup also reported a failure.")
            elif cleanup_error is None:
                cleanup_error = error
    if primary is None and cleanup_error is not None:
        raise cleanup_error


def test_actual_mock_browser_and_auth_reopen_keep_configured_selection():
    # Reuse the controlled fixture journey, including real Chromium AUTH_REQUIRED.
    fixture = session_fixture.ManualAuthSessionApiTests()
    try:
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "review"}):
            fixture.setUp()
        with fixture.store.engine.connect() as connection:
            attempt = connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == fixture.application_id
            )).mappings().one()
        expected = fixture.root / fixture.application_id / "browser-profile/review"
        assert expected.is_dir()
        metadata_path = expected / "mjobs-profile-metadata.json"
        assert metadata_path.is_file(), "The real initial launch must record its pinned browser profile"
        metadata_before = json.loads(metadata_path.read_text(encoding="utf-8"))
        assert metadata_before["application_id"] == fixture.application_id
        assert metadata_before["profile_name"] == "review"
        assert metadata_before["provider"] == "mock-ats"
        assert metadata_before["site"] == fixture.origin
        assert metadata_before["status"] == "AUTH_REQUIRED"
        assert metadata_before["likely_authenticated"] is False
        with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "different"}):
            resolved = _resolve_auth_attempt(fixture.store, fixture.application_id,
                                             attempt["id"], fixture.root)
            assert resolved is not None and resolved[1] == expected
            route = resolved[0]
            payload = {"schema": 1, "output_dir": str(fixture.root),
                "application_id": fixture.application_id, "mock_origin": fixture.origin,
                "requisition": route["requisition"], "budget_seconds": 10}
            with patch.object(manual_auth_host, "run_browser",
                              return_value="MANUAL_AUTH_WINDOW_CLOSED") as browser:
                assert manual_auth_host.run(io.BytesIO(json.dumps(payload).encode())) == 0
                assert browser.call_args.args[0] == expected
                assert browser.call_args.kwargs["application_root"] == fixture.root / fixture.application_id
        # The host above is mocked: lookup/forwarding must not invent another use.
        assert json.loads(metadata_path.read_text(encoding="utf-8")) == metadata_before
        settings = fixture.client.get('/settings').json()
        assert settings['browser_profile'] == {
            'name': 'review', 'scope': 'PER_APPLICATION', 'existing_sessions': 'PINNED'}
        assert settings['external_submission_enabled'] is False
        assert fixture.store.application(fixture.application_id)['state'] == 'AUTH_REQUIRED'
        assert fixture.store.count('application_attempts') == 1
    finally:
        _close_manual_auth_fixture(fixture)
