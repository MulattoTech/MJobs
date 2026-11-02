"""Real fixture browser launch and subsequent auth use one pinned selection."""
import io
import json
import os
from unittest.mock import patch

from sqlalchemy import select

from packages.persistence import models as m
from services.applications.manual_auth import _resolve_auth_attempt
from services.applications import manual_auth_host
from tests.integration import test_manual_auth_session as session_fixture


def test_actual_mock_browser_and_auth_reopen_keep_configured_selection():
    # Reuse the controlled fixture journey, including real Chromium AUTH_REQUIRED.
    fixture = session_fixture.ManualAuthSessionApiTests()
    with patch.dict(os.environ, {"MJOBS_BROWSER_PROFILE": "review"}):
        fixture.setUp()
    try:
        with fixture.store.engine.connect() as connection:
            attempt = connection.execute(select(m.attempts).where(
                m.attempts.c.application_id == fixture.application_id
            )).mappings().one()
        expected = fixture.root / fixture.application_id / "browser-profile/review"
        assert expected.is_dir()
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
        settings = fixture.client.get('/settings').json()
        assert settings['browser_profile'] == {
            'name': 'review', 'scope': 'PER_APPLICATION', 'existing_sessions': 'PINNED'}
        assert settings['external_submission_enabled'] is False
        assert fixture.store.application(fixture.application_id)['state'] == 'AUTH_REQUIRED'
        assert fixture.store.count('application_attempts') == 1
    finally:
        fixture.tearDown()
        fixture.doCleanups()
