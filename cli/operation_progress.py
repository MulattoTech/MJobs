"""Read and watch a profile's safe, saved operation progress as JSON."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
import re
import sys
import time
from urllib.parse import quote, urlsplit

import httpx
from packages.domain.operation_errors import OPERATION_ERROR_CATEGORIES

from services.ai.providers import AIProviderError
from services.ai.providers.transport import request_guard


DEFAULT_API = "http://127.0.0.1:18700"
MAX_RESPONSE_BYTES = 64 * 1024
MAX_OPERATIONS = 200
MAX_TIMEOUT_SECONDS = 30.0
MAX_INTERVAL_SECONDS = 30.0
MAX_WAIT_SECONDS = 3600.0
TERMINAL_STATES = {"COMPLETED", "FAILED", "BLOCKED", "CANCELLED", "DEAD_LETTERED", "DISCARDED"}
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProgressReadError(Exception):
    """A safe, generic failure while reading or validating progress."""


def _bounded_number(value, *, minimum, maximum, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} is outside its allowed range")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} is outside its allowed range") from exc
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise ValueError(f"{label} is outside its allowed range")
    return number


def validate_timeout(value):
    return _bounded_number(value, minimum=0.001, maximum=MAX_TIMEOUT_SECONDS, label="timeout")


def validate_interval(value):
    return _bounded_number(value, minimum=0.25, maximum=MAX_INTERVAL_SECONDS, label="interval")


def validate_max_wait(value):
    return _bounded_number(value, minimum=1.0, maximum=MAX_WAIT_SECONDS, label="max-wait")


def validate_profile(value):
    if (not isinstance(value, str) or not value or len(value) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise ValueError("profile is invalid")
    return value


def validate_api_origin(value):
    if (not isinstance(value, str) or "?" in value or "#" in value
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)):
        raise ValueError("API origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("API origin is invalid") from exc
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or port is not None and not 0 < port <= 65535):
        raise ValueError("API origin is invalid")
    return value.rstrip("/")


def _read_response(api, path, deadline):
    """Read at most 64 KiB under one deadline; never return response text on failure."""
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProgressReadError
        with httpx.Client(base_url=api, timeout=remaining, trust_env=False,
                          follow_redirects=False) as client, request_guard(client, deadline=deadline) as guard:
            with client.stream("GET", path, headers={"Accept-Encoding": "identity"},
                               extensions={"trace": guard.trace}) as response:
                guard.bind_response(response)
                if (response.status_code != 200
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise ProgressReadError
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                    raise ProgressReadError
                body = bytearray()
                for chunk in response.iter_raw():
                    guard.check()
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise ProgressReadError
                    body.extend(chunk)
                guard.check()
                return response.status_code, bytes(body)
    except ProgressReadError:
        raise
    except (AIProviderError, httpx.HTTPError, httpx.InvalidURL, OSError, ValueError,
            RecursionError, TimeoutError) as exc:
        raise ProgressReadError from exc


def _is_timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _priority(value):
    if not isinstance(value, dict):
        return None
    tier, effective = value.get("tier"), value.get("effective_tier")
    queue = value.get("queue_priority")
    enqueued = value.get("enqueued_at")
    aging = value.get("aging_interval_seconds")
    basis = value.get("basis")
    if (type(tier) is not int or not 0 <= tier <= 5
            or type(effective) is not int or not 0 <= effective <= 5
            or queue not in {"CRITICAL", "HIGH", "NORMAL", "DEFERRED", "STALE", "BACKGROUND"}
            or not _is_timestamp(enqueued) or type(aging) is not int or not 1 <= aging <= 86400
            or basis != "APPROVED_SKILL_EVIDENCE_AND_POSTING_AGE"):
        return None
    return {"tier": tier, "effective_tier": effective, "queue_priority": queue,
            "enqueued_at": enqueued, "aging_interval_seconds": aging, "basis": basis}


def _safe_operation(value):
    if not isinstance(value, dict):
        raise ProgressReadError
    operation_id = value.get("operation_id")
    operation_type = value.get("operation_type")
    entity_id = value.get("entity_id")
    state = value.get("state")
    if (not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id)
            or not isinstance(operation_type, str) or not _CODE.fullmatch(operation_type)
            or not isinstance(entity_id, str) or not entity_id or len(entity_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in entity_id)
            or not isinstance(state, str) or not _CODE.fullmatch(state)):
        raise ProgressReadError
    safe = {
        "operation_id": operation_id,
        "operation_type": operation_type,
        "entity_id": entity_id,
        "state": state,
    }
    step = value.get("current_step")
    if "current_step" in value:
        if step is not None and (not isinstance(step, str) or not _CODE.fullmatch(step)):
            raise ProgressReadError
        safe["current_step"] = step
    progress = value.get("progress")
    if "progress" in value:
        if progress is not None:
            try:
                valid_progress = (type(progress) in {int, float} and math.isfinite(float(progress))
                                  and 0 <= progress <= 100)
            except (OverflowError, ValueError):
                valid_progress = False
            if not valid_progress:
                raise ProgressReadError
        safe["progress"] = progress
    for name in ("started_at", "updated_at", "completed_at", "next_attempt_at"):
        if name in value:
            if value[name] is not None and not _is_timestamp(value[name]):
                raise ProgressReadError
            safe[name] = value[name]
    retries = value.get("retry_count")
    if "retry_count" in value:
        if retries is not None and (type(retries) is not int or not 0 <= retries <= 1_000_000):
            raise ProgressReadError
        safe["retry_count"] = retries
    summary = value.get("recovery_summary")
    if "recovery_summary" in value:
        if (not isinstance(summary, str) or len(summary) > 130
                or summary and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}(?:: [A-Z][A-Z0-9_]{0,63})?", summary)):
            raise ProgressReadError
        safe["recovery_summary"] = summary
    if "preparation_priority" in value:
        priority = _priority(value["preparation_priority"])
        if priority is None:
            raise ProgressReadError
        safe["preparation_priority"] = priority
    if "error_category" in value:
        category = value["error_category"]
        if category is not None and category not in OPERATION_ERROR_CATEGORIES:
            raise ProgressReadError
        safe["error_category"] = category
    return safe


def _snapshot(api, profile, timeout, *, request, monotonic):
    deadline = monotonic() + timeout
    path = f"/profiles/{quote(profile, safe='')}/operations/progress"
    try:
        status, body = request(api, path, deadline)
        if monotonic() >= deadline:
            raise ProgressReadError
        if status != 200 or not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
            raise ProgressReadError
        value = json.loads(body)
        operations = value.get("operations") if isinstance(value, dict) else None
        if not isinstance(operations, list) or len(operations) > MAX_OPERATIONS:
            raise ProgressReadError
        safe_operations = [_safe_operation(operation) for operation in operations]
        return {"operations": safe_operations}
    except ProgressReadError:
        raise
    except (AIProviderError, httpx.HTTPError, httpx.InvalidURL, OSError, ValueError,
            TypeError, RecursionError, TimeoutError) as exc:
        raise ProgressReadError from exc


def _terminal(snapshot):
    return not snapshot["operations"] or all(
        operation["state"] in TERMINAL_STATES for operation in snapshot["operations"]
    )


def _emit(snapshot, stream):
    print(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), file=stream)


def run(mode, *, profile, api=DEFAULT_API, timeout=5.0, interval=2.0, max_wait=300.0,
        request=_read_response, monotonic=time.monotonic, sleep=time.sleep, stdout=None, stderr=None):
    """Run one safe list read or a bounded serial watch; return the CLI exit code."""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        if mode not in {"list", "watch"}:
            raise ValueError("mode is invalid")
        profile = validate_profile(profile)
        api = validate_api_origin(api)
        timeout = validate_timeout(timeout)
        interval = validate_interval(interval)
        max_wait = validate_max_wait(max_wait)
    except (TypeError, ValueError):
        print('{"error":"operation progress unavailable"}', file=stderr)
        return 2

    watch_deadline = monotonic() + max_wait if mode == "watch" else None
    snapshot_emitted = False
    try:
        remaining = timeout if watch_deadline is None else min(timeout, max(0.001, watch_deadline - monotonic()))
        snapshot = _snapshot(api, profile, remaining, request=request, monotonic=monotonic)
        _emit(snapshot, stdout)
        snapshot_emitted = True
        if mode == "list" or _terminal(snapshot):
            return 0
        previous = snapshot
        while True:
            remaining = watch_deadline - monotonic()
            if remaining <= 0:
                return 1
            sleep(min(interval, remaining))
            remaining = watch_deadline - monotonic()
            if remaining <= 0:
                return 1
            snapshot = _snapshot(api, profile, min(timeout, remaining), request=request, monotonic=monotonic)
            if snapshot != previous:
                _emit(snapshot, stdout)
            previous = snapshot
            if _terminal(snapshot):
                return 0
    except ProgressReadError:
        if (mode == "watch" and watch_deadline is not None and snapshot_emitted
                and monotonic() >= watch_deadline):
            return 1
        print('{"error":"operation progress unavailable"}', file=stderr)
        return 2


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        print('{"error":"operation progress unavailable"}', file=sys.stderr)
        raise SystemExit(2)


def main(argv=None):
    parser = _JsonArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for name in ("list", "watch"):
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--api", default=DEFAULT_API)
        command.add_argument("--timeout", default="5")
        if name == "watch":
            command.add_argument("--interval", default="2")
            command.add_argument("--max-wait", default="300")
    args = parser.parse_args(argv)
    try:
        return run(args.mode, profile=args.profile, api=args.api, timeout=args.timeout,
                   interval=getattr(args, "interval", 2), max_wait=getattr(args, "max_wait", 300))
    except KeyboardInterrupt:
        print('{"error":"interrupted"}', file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
