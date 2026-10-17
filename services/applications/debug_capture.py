"""Bounded, content-minimizing browser failure diagnostics for local automation."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import urlsplit, urlunsplit

from services.applications.browser_capture import capture_redacted_screenshot


CAPTURE_RETENTION_DAYS = 30
MAX_CAPTURES_PER_APPLICATION = 20
MAX_DIAGNOSTIC_EVENTS = 50
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
SCREENSHOT_TIMEOUT_MS = 2000
_ATTEMPT_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_ERROR_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FINGERPRINT_KEY = secrets.token_bytes(32)
_MAX_MESSAGE_BYTES = 65536
_ALLOWED_STEPS = {
    "NAVIGATE", "DETECT_FORM", "FILL_CONTACT", "FILL_LOCATION", "UPLOAD_RESUME",
    "ANSWER_STANDARD", "ANSWER_CUSTOM", "VALIDATE",
    "STOP_BEFORE_SUBMIT", "POLICY_GATE", "SUBMIT", "VERIFY_CONFIRMATION",
    "RECONCILE_CONFIRMATION", "CHALLENGE_DETECTED", "UNKNOWN",
}
_ALLOWED_SOURCES = {"console", "pageerror", "requestfailed"}


def sanitize_debug_url(value):
    """Keep only scheme/host and path shape; discard credentials and URL data."""
    try:
        parsed = urlsplit(str(value or "")[:2048])
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    host = parsed.hostname.lower()
    netloc = host + (f":{port}" if port else "")
    segments = [segment for segment in parsed.path.split("/") if segment]
    path = "/" + "/".join(":redacted" for _ in segments[:16]) if segments else "/"
    return urlunsplit((parsed.scheme, netloc, path, "", ""))


def append_diagnostic_event(events, source, url, message):
    """Append a bounded fingerprint record; never retain the browser message."""
    if source not in _ALLOWED_SOURCES:
        source = "unknown"
    raw = str(message or "").encode("utf-8", errors="replace")[:_MAX_MESSAGE_BYTES]
    event = {
        "source": source,
        "url": sanitize_debug_url(url),
        "message_fingerprint": hmac.new(_FINGERPRINT_KEY, raw, hashlib.sha256).hexdigest(),
        "message_length": len(raw),
    }
    events.append(event)
    del events[:-MAX_DIAGNOSTIC_EVENTS]
    return event


def append_console_error(events, message, url):
    """Keep only error-level console records, never ordinary page logging."""
    if getattr(message, "type", None) != "error":
        return None
    return append_diagnostic_event(events, "console", url, getattr(message, "text", ""))


def _atomic_json(path, value):
    """Write a complete JSON artifact or leave no partial destination behind."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dom_snapshot(page):
    """Keep the package schema without an unbounded renderer evaluation."""
    return {
        "status": "OMITTED_UNBOUNDED_EVALUATION" if page is not None else "UNAVAILABLE",
        "element_counts": {},
        "role_counts": {},
        "text_omitted": True,
        "attributes_omitted": True,
        "control_values_omitted": True,
    }


def prune_debug_captures(capture_root, *, now=None, checkpoint=None):
    """Delete only owned attempt directories by age/count; preserve unknown paths."""
    root = Path(capture_root)
    if checkpoint is not None:
        checkpoint()
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return []
    root.mkdir(parents=True, exist_ok=True)
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Debug capture retention clock must include a timezone")
    managed = []
    for child in root.iterdir():
        if checkpoint is not None:
            checkpoint()
        if not _ATTEMPT_RE.fullmatch(child.name) or child.is_symlink() or not child.is_dir():
            continue
        try:
            if child.resolve(strict=True).parent != resolved_root:
                continue
            modified = datetime.fromtimestamp(child.stat().st_mtime, timezone.utc)
        except OSError:
            continue
        managed.append((modified, child))
    removed = []
    cutoff = current.astimezone(timezone.utc) - timedelta(days=CAPTURE_RETENTION_DAYS)
    keep = []
    for modified, child in managed:
        if checkpoint is not None:
            checkpoint()
        if modified < cutoff:
            shutil.rmtree(child)
            removed.append(child.name)
        else:
            keep.append((modified, child))
    keep.sort(key=lambda item: item[0], reverse=True)
    for _, child in keep[MAX_CAPTURES_PER_APPLICATION:]:
        if checkpoint is not None:
            checkpoint()
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


def capture_browser_failure(page, capture_root, *, attempt_id, step, configured_url, error,
                            events=(), now=None, check=None):
    """Capture diagnostics under one caller-derived, capped monotonic budget.

    Optional DOM evaluation is omitted because synchronous evaluate has no
    timeout. Native screenshot work shares this deadline. Phase checkpoints
    preserve caller cancellation; existing local OS I/O remains synchronous.
    Diagnostics are best-effort and never replace the primary browser error.
    """
    if not isinstance(attempt_id, str) or not _ATTEMPT_RE.fullmatch(attempt_id):
        return None
    root = Path(capture_root)
    published_metadata = None
    try:
        started = time.monotonic()
        budget = SCREENSHOT_TIMEOUT_MS / 1000
        if check is not None:
            check()
            remaining_seconds = getattr(check, "remaining_seconds", None)
            if callable(remaining_seconds):
                remaining = remaining_seconds()
                if (type(remaining) not in (int, float) or not math.isfinite(remaining)
                        or remaining < .001):
                    return None
                budget = min(budget, remaining)
        deadline = started + budget

        def checkpoint():
            if check is not None:
                check()
            remaining_ms = (deadline - time.monotonic()) * 1000
            if remaining_ms < 1:
                raise TimeoutError("Diagnostic capture budget exhausted")
            return remaining_ms

        checkpoint()
        prune_debug_captures(root, now=now, checkpoint=checkpoint)
        checkpoint()
        if root.is_symlink() or not root.is_dir():
            return None
        resolved_root = root.resolve(strict=True)
        target = root / attempt_id
        target.mkdir(exist_ok=False)
        checkpoint()
        if target.resolve(strict=True).parent != resolved_root:
            shutil.rmtree(target)
            return None
        occurred_at = now or datetime.now(timezone.utc)
        if occurred_at.tzinfo is None:
            shutil.rmtree(target)
            return None
        error_bytes = str(error or "").encode("utf-8", errors="replace")[:_MAX_MESSAGE_BYTES]
        error_type = type(error).__name__ if error is not None else "UnknownError"
        if not _SAFE_ERROR_TYPE_RE.fullmatch(error_type):
            error_type = "UnknownError"
        safe_step = step if step in _ALLOWED_STEPS else "UNKNOWN"
        safe_events = []
        for item in list(events)[-MAX_DIAGNOSTIC_EVENTS:]:
            checkpoint()
            if not isinstance(item, dict):
                continue
            digest = item.get("message_fingerprint")
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                continue
            source = item.get("source") if item.get("source") in _ALLOWED_SOURCES else "unknown"
            length = item.get("message_length")
            safe_events.append({
                "source": source,
                "url": sanitize_debug_url(item.get("url")),
                "message_fingerprint": digest,
                "message_length": max(0, min(length, 1_000_000)) if type(length) is int else 0,
            })

        checkpoint()
        dom = _dom_snapshot(page)
        screenshot_status = "UNAVAILABLE"
        if page is not None:
            screenshot = target / "screenshot.png"
            screenshot_timeout_ms = checkpoint()
            try:
                capture_redacted_screenshot(
                    page, screenshot, timeout_ms=screenshot_timeout_ms, full_page=False,
                    deadline=deadline,
                )
                if screenshot.stat().st_size > MAX_SCREENSHOT_BYTES:
                    screenshot.unlink()
                    screenshot_status = "OMITTED_SIZE_LIMIT"
                else:
                    screenshot_status = "CAPTURED"
            except Exception:
                screenshot_status = "FAILED"
                try:
                    (target / "screenshot.png").unlink(missing_ok=True)
                except OSError:
                    pass
            # Caller control signals are terminal, not screenshot failures.
            # Keep checkpoints outside the best-effort native-error boundary.
            checkpoint()

        checkpoint()
        try:
            current_url = sanitize_debug_url(page.url if page is not None else configured_url)
        except Exception:
            current_url = sanitize_debug_url(configured_url)
        checkpoint()
        _atomic_json(target / "dom.json", dom)
        metadata = {
            "schema_version": 1,
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
            "attempt_id": attempt_id,
            "current_step": safe_step,
            "current_url": current_url,
            "error": {
                "type": error_type,
                "message_fingerprint": hmac.new(
                    _FINGERPRINT_KEY, error_bytes, hashlib.sha256,
                ).hexdigest(),
                "message_length": len(error_bytes),
            },
            "diagnostics": safe_events,
            "dom_snapshot": {"file": "dom.json", "status": dom["status"]},
            "screenshot": {"file": "screenshot.png" if screenshot_status == "CAPTURED" else None,
                           "status": screenshot_status},
            "retention_days": CAPTURE_RETENTION_DAYS,
            "max_captures_per_application": MAX_CAPTURES_PER_APPLICATION,
        }
        # Retention precedes the metadata publication marker. Expiry during
        # earlier work must never leave a successful, discoverable package.
        checkpoint()
        prune_debug_captures(root, now=occurred_at, checkpoint=checkpoint)
        checkpoint()
        if not target.is_dir():
            return None
        _atomic_json(target / "metadata.json", metadata)
        published_metadata = target / "metadata.json"
        checkpoint()
        return target
    except Exception:
        # Diagnostics are best-effort. They must never replace the browser error
        # or interrupt the application state transition that follows it.
        if published_metadata is not None:
            try:
                published_metadata.unlink(missing_ok=True)
            except OSError:
                pass
        return None
