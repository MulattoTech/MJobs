"""Read-only, loopback-only MJobs maintenance entry point (MJ-REQ-207).

This thin client never creates operations, modifies server state or submits jobs.
Only the server's existing liveness and configured-source readbacks are exposed.
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

import httpx


MAX_RESPONSE_BYTES = 1_048_576
MAX_SOURCE_ROWS = 1_000
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class CLIError(ValueError):
    """Stable, non-sensitive public failure code."""


def _validated_origin(value: str) -> str:
    if (not isinstance(value, str) or not value or not value.isascii()
            or value != value.strip() or len(value) > 128
            or any(ord(char) < 33 or ord(char) == 127 or char == "\\" for char in value)):
        raise CLIError("CLI_ORIGIN_INVALID")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise CLIError("CLI_ORIGIN_INVALID") from None
    if (parsed.scheme != "http" or port is None or not (1 <= port <= 65535)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise CLIError("CLI_ORIGIN_INVALID")
    host = parsed.hostname
    expected = f"127.0.0.1:{port}" if host == "127.0.0.1" else (
        f"[::1]:{port}" if host == "::1" else None)
    if expected is None or parsed.netloc != expected:
        raise CLIError("CLI_ORIGIN_INVALID")
    return "http://" + expected


def _request_json(origin: str, path: str, *, transport=None):
    try:
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=1.0),
                          trust_env=False, follow_redirects=False,
                          transport=transport) as client:
            with client.stream("GET", origin + path,
                               headers={"Accept": "application/json"}) as response:
                if (response.status_code != 200
                        or response.headers.get("content-type", "").split(";")[0].strip().lower()
                        != "application/json"
                        or response.headers.get("content-encoding", "identity").strip().lower()
                        not in ("", "identity")):
                    raise CLIError("CLI_REMOTE_UNAVAILABLE")
                chunks = bytearray()
                # An in-memory MockTransport response can already be consumed.
                # A real streamed response stays raw and is bounded as read.
                raw_stream = ((response.content,) if response.is_stream_consumed
                              else response.iter_raw())
                for chunk in raw_stream:
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise CLIError("CLI_RESPONSE_TOO_LARGE")
                    chunks.extend(chunk)
        result = json.loads(chunks)
    except (httpx.RequestError, httpx.StreamError, httpx.DecodingError,
            UnicodeError, json.JSONDecodeError):
        raise CLIError("CLI_REMOTE_UNAVAILABLE") from None
    if not isinstance(result, dict):
        raise CLIError("CLI_RESPONSE_INVALID")
    return result


def _health(data: dict) -> dict:
    if (data.get("status") != "ok" or data.get("check") != "liveness"
            or data.get("mode") != "LOCAL_FIRST"
            or data.get("operation_mode") not in ("PERSONAL", "FIXTURE")
            or data.get("external_submission_enabled") is not False):
        raise CLIError("CLI_HEALTH_INVALID")
    return {key: data[key] for key in (
        "status", "check", "mode", "operation_mode", "external_submission_enabled")}


def _sources(data: dict) -> dict:
    rows = data.get("sources")
    if not isinstance(rows, list) or len(rows) > MAX_SOURCE_ROWS:
        raise CLIError("CLI_SOURCES_INVALID")
    output = []
    for row in rows:
        if not isinstance(row, dict):
            raise CLIError("CLI_SOURCES_INVALID")
        for key in ("id", "name", "provider"):
            if not isinstance(row.get(key), str) or len(row[key]) > 500:
                raise CLIError("CLI_SOURCES_INVALID")
        if (type(row.get("enabled")) is not bool
                or not (row.get("health_status") is None
                        or isinstance(row["health_status"], str))):
            raise CLIError("CLI_SOURCES_INVALID")
        output.append({key: row[key] for key in (
            "id", "name", "provider", "enabled", "health_status")})
    return {"sources": output}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cli",
        description="Bounded read-only MJobs maintenance against a local API.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help="Numeric loopback API origin only (default: 127.0.0.1:8000)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("health", help="Read API liveness without changing state")
    source = commands.add_parser("source", help="Configured job source readbacks")
    source.add_subparsers(dest="source_command", required=True).add_parser(
        "list", help="List non-sensitive source identifiers and health states")
    return parser


def main(argv=None, *, transport=None) -> int:
    args = _parser().parse_args(argv)
    try:
        origin = _validated_origin(args.base_url)
        if args.command == "health":
            payload = _health(_request_json(origin, "/health", transport=transport))
        elif args.command == "source" and args.source_command == "list":
            payload = _sources(_request_json(origin, "/sources", transport=transport))
        else:
            raise CLIError("CLI_UNSUPPORTED_COMMAND")
    except CLIError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
