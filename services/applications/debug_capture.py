"""Bounded, content-minimizing browser failure diagnostics for local automation."""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
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
    "NAVIGATE", "DETECT_FORM", "FILL_CONTACT", "UPLOAD_RESUME", "VALIDATE",
    "STOP_BEFORE_SUBMIT", "POLICY_GATE", "SUBMIT", "VERIFY_CONFIRMATION",
    "RECONCILE_CONFIRMATION", "CHALLENGE_DETECTED", "UNKNOWN",
}
_ALLOWED_SOURCES = {"console", "pageerror", "requestfailed"}
_ALLOWED_TAGS = {
    "a", "article", "button", "canvas", "dialog", "div", "embed", "fieldset",
    "form", "h1", "h2", "h3", "h4", "h5", "h6", "html", "iframe", "img",
    "input", "label", "legend", "li", "main", "nav", "ol", "option", "p",
    "picture", "section", "select", "span", "svg", "table", "tbody", "td",
    "textarea", "tfoot", "th", "thead", "tr", "ul", "video",
}
_ALLOWED_ROLES = {
    "alert", "button", "checkbox", "combobox", "dialog", "form", "heading",
    "img", "link", "list", "listbox", "main", "navigation", "option", "radio",
    "search", "textbox",
}
_DOM_SUMMARY_SCRIPT = r"""
() => {
  const elementCounts = {};
  const roleCounts = {};
  const stack = document.documentElement ? [document.documentElement] : [];
  let visited = 0;
  while (stack.length && visited < 2000) {
    const node = stack.pop();
    if (!node || node.nodeType !== Node.ELEMENT_NODE) continue;
    visited += 1;
    const tag = node.tagName.toLowerCase();
    elementCounts[tag] = (elementCounts[tag] || 0) + 1;
    const role = node.getAttribute('role');
    if (role) roleCounts[role.toLowerCase()] = (roleCounts[role.toLowerCase()] || 0) + 1;
    for (let index = node.children.length - 1; index >= 0; index -= 1) stack.push(node.children[index]);
  }
  return {element_counts: elementCounts, role_counts: roleCounts, nodes_visited: visited,
          truncated: stack.length > 0, text_omitted: true, attributes_omitted: true,
          control_values_omitted: true};
}
"""


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


def _atomic_json(path, value):
    """Write a complete JSON artifact or leave no partial destination behind."""
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _dom_snapshot(page):
    if page is None:
        return {"status": "UNAVAILABLE", "element_counts": {}, "role_counts": {}}
    raw = page.evaluate(_DOM_SUMMARY_SCRIPT)
    if not isinstance(raw, dict):
        return {"status": "UNAVAILABLE", "element_counts": {}, "role_counts": {}}
    elements = raw.get("element_counts") if isinstance(raw.get("element_counts"), dict) else {}
    roles = raw.get("role_counts") if isinstance(raw.get("role_counts"), dict) else {}
    safe_elements = {name: max(0, min(int(count), 2000)) for name, count in elements.items()
                     if name in _ALLOWED_TAGS and type(count) is int}
    safe_roles = {name: max(0, min(int(count), 2000)) for name, count in roles.items()
                  if name in _ALLOWED_ROLES and type(count) is int}
    return {
        "status": "CAPTURED",
        "element_counts": safe_elements,
        "role_counts": safe_roles,
        "nodes_visited": max(0, min(int(raw.get("nodes_visited", 0)), 2000))
        if type(raw.get("nodes_visited", 0)) is int else 0,
        "truncated": raw.get("truncated") is True,
        "text_omitted": True,
        "attributes_omitted": True,
        "control_values_omitted": True,
    }


def prune_debug_captures(capture_root, *, now=None):
    """Delete only owned attempt directories by age/count; preserve unknown paths."""
    root = Path(capture_root)
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
        if modified < cutoff:
            shutil.rmtree(child)
            removed.append(child.name)
        else:
            keep.append((modified, child))
    keep.sort(key=lambda item: item[0], reverse=True)
    for _, child in keep[MAX_CAPTURES_PER_APPLICATION:]:
        shutil.rmtree(child)
        removed.append(child.name)
    return removed


def capture_browser_failure(page, capture_root, *, attempt_id, step, configured_url, error,
                            events=(), now=None):
    """Write one bounded failure package without page text or raw error content."""
    if not isinstance(attempt_id, str) or not _ATTEMPT_RE.fullmatch(attempt_id):
        return None
    root = Path(capture_root)
    try:
        prune_debug_captures(root, now=now)
        if root.is_symlink() or not root.is_dir():
            return None
        resolved_root = root.resolve(strict=True)
        target = root / attempt_id
        target.mkdir(exist_ok=False)
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

        dom = {"status": "UNAVAILABLE", "element_counts": {}, "role_counts": {}}
        screenshot_status = "UNAVAILABLE"
        if page is not None:
            try:
                dom = _dom_snapshot(page)
            except Exception:
                dom = {"status": "FAILED", "element_counts": {}, "role_counts": {}}
            try:
                from services.applications.browser_capture import capture_redacted_screenshot

                screenshot = target / "screenshot.png"
                capture_redacted_screenshot(
                    page, screenshot, timeout_ms=SCREENSHOT_TIMEOUT_MS, full_page=False,
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

        try:
            current_url = sanitize_debug_url(page.url if page is not None else configured_url)
        except Exception:
            current_url = sanitize_debug_url(configured_url)
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
        _atomic_json(target / "metadata.json", metadata)
        prune_debug_captures(root, now=occurred_at)
        return target if target.is_dir() else None
    except Exception:
        # Diagnostics are best-effort. They must never replace the browser error
        # or interrupt the application state transition that follows it.
        return None
