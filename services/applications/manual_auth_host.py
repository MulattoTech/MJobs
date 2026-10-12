"""Silent fixture-only browser host; private bootstrap arrives exclusively on stdin.

Exit status is diagnostic only. The retained native tree observer remains the
authority for closure and capacity; this host never writes closure certificates.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from time import monotonic

from services.applications.manual_auth import (
    MANUAL_AUTH_WINDOW_CLOSED, OPEN_UNAVAILABLE, STALE, _manual_login_url, run_browser,
)
from services.discovery.pipeline import MockClient

_FIELDS = {"schema", "output_dir", "application_id", "mock_origin", "requisition", "budget_seconds"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def run(stream) -> int:
    client = None
    try:
        raw = stream.read(2049)
        if not isinstance(raw, bytes) or not raw or len(raw) > 2048:
            return 2
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if (not isinstance(payload, dict) or set(payload) != _FIELDS
                or type(payload["schema"]) is not int or payload["schema"] != 1
                or type(payload["budget_seconds"]) is not int
                or not 1 <= payload["budget_seconds"] <= 900):
            return 2
        for field in ("application_id", "requisition"):
            if not isinstance(payload[field], str) or not _IDENTIFIER.fullmatch(payload[field]):
                return 2
        if not isinstance(payload["output_dir"], str) or not isinstance(payload["mock_origin"], str):
            return 2
        configured_root = Path(payload["output_dir"])
        if not configured_root.is_absolute():
            return 2
        root = configured_root.resolve(strict=True)
        application = (root / payload["application_id"]).resolve(strict=True)
        profile = (application / "browser-profile").resolve(strict=True)
        if (application.parent != root or application.name != payload["application_id"]
                or profile.parent != application or profile.name != "browser-profile"
                or not profile.is_dir()):
            return 2
        client = MockClient(payload["mock_origin"])
        login = _manual_login_url(payload["mock_origin"], payload["requisition"], client)
        if login is None:
            client.close()
            return 2
        deadline = monotonic() + payload["budget_seconds"]

        def remaining_seconds():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Host budget expired")
            return remaining

        def check():
            remaining_seconds()

        check.remaining_seconds = remaining_seconds

        check()
    except Exception:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        return 2
    try:
        status = run_browser(profile, login, client, check=check)
        return 0 if status == MANUAL_AUTH_WINDOW_CLOSED else 2 if status in {OPEN_UNAVAILABLE, STALE} else 3
    except Exception:
        return 3
    finally:
        try:
            client.close()
        except Exception:
            pass


def main() -> int:
    if sys.platform != "win32" or len(sys.argv) != 1:
        return 2
    return run(sys.stdin.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
