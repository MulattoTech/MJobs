"""Bounded, content-minimizing browser failure diagnostics for local automation."""
import asyncio
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
DOM_TIMEOUT_MS = 300
MAX_DOM_NODES = 160
MAX_DOM_DEPTH = 8
MAX_DOM_BYTES = 8192
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
_ALLOWED_ELEMENTS = frozenset((
    "html", "head", "body", "main", "header", "footer", "nav", "section", "article",
    "aside", "div", "span", "p", "h1", "h2", "h3", "h4", "h5", "h6", "form",
    "fieldset", "legend", "label", "input", "textarea", "select", "option", "optgroup",
    "button", "a", "ul", "ol", "li", "table", "thead", "tbody", "tr", "th", "td",
    "details", "summary", "dialog", "iframe", "img", "canvas", "svg", "video",
))
_ALLOWED_ROLES = frozenset((
    "alert", "alertdialog", "button", "checkbox", "combobox", "dialog", "document",
    "form", "group", "heading", "link", "list", "listbox", "listitem", "main",
    "navigation", "option", "progressbar", "radio", "radiogroup", "region", "search",
    "searchbox", "slider", "spinbutton", "status", "switch", "tab", "table", "tablist",
    "tabpanel", "textbox", "tree", "treeitem",
))


class _DomDeadlineExpired(TimeoutError):
    pass


class _DomCancellationFailed(Exception):
    """The installed transport did not honor its verified cancellation contract."""


class _CaptureControlSignal(Exception):
    """Keep a caller checkpoint failure outside the best-effort DOM boundary."""


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
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _omitted_dom(status):
    return {
        "status": status,
        "element_counts": {},
        "role_counts": {},
        "text_omitted": True,
        "attributes_omitted": True,
        "control_values_omitted": True,
    }


def _sanitize_dom(root, checkpoint):
    """Retain topology and fixed tag/role enums, never raw CDP content."""
    if not isinstance(root, dict) or root.get("nodeType") != 9:
        raise ValueError("Invalid diagnostic document")
    nodes = []
    pending = [(iter((root,)), None, 0)]
    visited = 0
    truncated = False
    while pending and visited < MAX_DOM_NODES:
        checkpoint()
        siblings, parent, depth = pending[-1]
        try:
            node = next(siblings)
        except StopIteration:
            pending.pop()
            continue
        visited += 1
        if not isinstance(node, dict):
            raise ValueError("Invalid diagnostic node")
        child_parent = parent
        if node.get("nodeType") == 1:
            name = node.get("nodeName")
            tag = name.lower() if isinstance(name, str) and len(name) <= 16 else "other"
            if tag not in _ALLOWED_ELEMENTS:
                tag = "other"
            item = {"parent": parent, "tag": tag}
            attributes = node.get("attributes", [])
            if isinstance(attributes, list):
                # Attribute names/values may be arbitrarily large or numerous.
                # Inspect only a bounded prefix and retain an exact fixed role.
                for offset in range(0, min(len(attributes) - 1, 64), 2):
                    if attributes[offset] == "role":
                        role = attributes[offset + 1]
                        if isinstance(role, str) and len(role) <= 16 and role in _ALLOWED_ROLES:
                            item["role"] = role
                        break
            child_parent = len(nodes)
            nodes.append(item)
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("Invalid diagnostic children")
        child_count = node.get("childNodeCount", 0)
        if type(child_count) is int and child_count > len(children):
            truncated = True
        if children:
            if depth < MAX_DOM_DEPTH:
                pending.append((iter(children), child_parent, depth + 1))
            else:
                truncated = True
    truncated = truncated or bool(pending)
    snapshot = {
        **_omitted_dom("CAPTURED"),
        "nodes": nodes,
        "truncated": truncated,
        "node_limit": MAX_DOM_NODES,
        "byte_limit": MAX_DOM_BYTES,
        "depth_limit": MAX_DOM_DEPTH,
        "subframes_omitted": True,
        "shadow_roots_omitted": True,
    }
    while True:
        checkpoint()
        elements, roles = {}, {}
        for node in nodes:
            elements[node["tag"]] = elements.get(node["tag"], 0) + 1
            if "role" in node:
                roles[node["role"]] = roles.get(node["role"], 0) + 1
        snapshot.update(element_counts=elements, role_counts=roles)
        # Match _atomic_json's actual on-disk encoding, including whitespace.
        if len((json.dumps(snapshot, indent=2) + "\n").encode("utf-8")) <= MAX_DOM_BYTES:
            return snapshot
        if not nodes:
            raise ValueError("Diagnostic envelope exceeds its byte limit")
        nodes.pop()
        snapshot["truncated"] = True


def _dom_snapshot(page, *, deadline, checkpoint):
    """Bound all native CDP awaits, including session creation and cleanup.

    Sync CDP calls expose no timeout. Use Playwright's guarded sync-to-async
    bridge with finite waits for both work and cancellation. Playwright's first
    cancellation waits for an abort acknowledgement, so a second, delivered
    cancellation must terminate that wait. An unexpected nonterminal task stops
    publication. The owner still closes its browser context after failure,
    including any session whose creation was interrupted.
    CDP replies are decoded before the retained topology/byte limits are applied.
    """
    if page is None:
        return _omitted_dom("UNAVAILABLE")
    sync = getattr(page, "_sync", None)
    implementation = getattr(page, "_impl_obj", None)
    context = getattr(implementation, "context", None)
    create_session = getattr(context, "new_cdp_session", None)
    if not callable(sync) or not callable(create_session):
        return _omitted_dom("OMITTED_UNSUPPORTED_TRANSPORT")
    dom_deadline = min(deadline, time.monotonic() + DOM_TIMEOUT_MS / 1000)
    # Reserve a slice for detach; both work and cleanup share the same deadline.
    work_deadline = dom_deadline - min(.025, max(0, dom_deadline - time.monotonic()) / 10)

    def check_owner():
        try:
            checkpoint()
        except Exception as error:
            raise _CaptureControlSignal() from error

    def check_dom():
        check_owner()
        if time.monotonic() >= dom_deadline:
            raise _DomDeadlineExpired()

    def consume_terminal(task):
        if not task.done():
            raise _DomCancellationFailed()
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    async def cancel_owned(task, limit):
        task.cancel("MJobs diagnostic deadline")
        # Deliver the first cancellation into Channel._abort before a second
        # cancellation. Two immediate cancel() calls can coalesce and hang.
        await asyncio.wait({task}, timeout=min(.005, max(0, limit - time.monotonic()) / 2))
        if not task.done():
            task.cancel("MJobs diagnostic abort deadline")
            # Even timeout=0 yields one scheduler turn to deliver cancellation.
            # Never replace this with await task or wait_for(task, ...).
            await asyncio.wait({task}, timeout=max(0, limit - time.monotonic()))
        if not task.done():
            task.add_done_callback(consume_terminal)
            raise _DomCancellationFailed()
        consume_terminal(task)

    async def bounded_call(factory, limit):
        check_owner()
        remaining = limit - time.monotonic()
        if remaining < .001:
            raise _DomDeadlineExpired()
        # Leave cancellation time inside this call's existing absolute limit.
        cancel_reserve = min(.01, remaining / 4)
        task = asyncio.create_task(factory(), name="mjobs-debug-dom-operation")
        try:
            done, _ = await asyncio.wait({task}, timeout=remaining - cancel_reserve)
        except BaseException:
            await cancel_owned(task, limit)
            raise
        if not done:
            await cancel_owned(task, limit)
            raise _DomDeadlineExpired()
        if time.monotonic() >= limit:
            consume_terminal(task)
            raise _DomDeadlineExpired()
        value = task.result()
        check_owner()
        if time.monotonic() >= limit:
            raise _DomDeadlineExpired()
        return value

    async def capture():
        session = None
        interrupted = False
        try:
            session = await bounded_call(lambda: create_session(implementation), work_deadline)
            result = await bounded_call(lambda: session.send(
                "DOM.getDocument", {"depth": MAX_DOM_DEPTH, "pierce": False}), work_deadline)
        except (_CaptureControlSignal, _DomCancellationFailed):
            interrupted = True
            raise
        finally:
            if session is not None and not interrupted:
                await bounded_call(session.detach, dom_deadline)
        return result

    try:
        result = sync(capture())
        check_dom()
        return _sanitize_dom(result.get("root") if isinstance(result, dict) else None, check_dom)
    except _CaptureControlSignal:
        raise
    except _DomCancellationFailed as error:
        raise _CaptureControlSignal() from error
    except _DomDeadlineExpired:
        return _omitted_dom("OMITTED_TIMEOUT")
    except (asyncio.CancelledError, Exception):
        return _omitted_dom("OMITTED_CAPTURE_FAILED")


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

    Native DOM protocol work and the screenshot share this deadline. DOM
    capture does not evaluate page JavaScript. Phase checkpoints
    preserve caller cancellation; existing local OS I/O remains synchronous.
    Diagnostics are best-effort and never replace the primary browser error.
    """
    if not isinstance(attempt_id, str) or not _ATTEMPT_RE.fullmatch(attempt_id):
        return None
    root = Path(capture_root)
    target = None
    target_created = False
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
        target_created = True
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
        dom = _dom_snapshot(page, deadline=deadline, checkpoint=checkpoint)
        checkpoint()
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
        checkpoint()
        return target
    except Exception:
        # Diagnostics are best-effort. They must never replace the browser error
        # or interrupt the application state transition that follows it. Remove
        # any unpublished package so failed atomic writes leave no diagnostics.
        if target_created and target is not None:
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
            except OSError:
                pass
        return None
