"""Privacy-conscious headed-browser feedback recorder for local MJobs development."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "outputs" / "live-feedback"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_METADATA_TEXT = 160
FEEDBACK_RETENTION_DAYS = 30
SESSION_DIRECTORY_RE = re.compile(r"^(\d{8}T\d{6}Z)(?:-\d{2})?$")
SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"\b(?:bearer\s+)?(?:gh[pousr]_[A-Z0-9_]{12,}|sk-[A-Z0-9_-]{12,}|xox[baprs]-[A-Z0-9-]{12,})\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\b", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|token|api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|cookie)\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
    re.compile(r"(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)"),
)
SENSITIVE_LABEL_RE = re.compile(
    r"\b(password|passwd|cookie|token|api key|access key|gender|sex|race|ethnicity|"
    r"veteran|disability|sexual orientation|marital status|religion|pronouns?|"
    r"date of birth|birth date|social security|ssn|national id|work authorization|"
    r"citizenship|visa status)\b",
    re.IGNORECASE,
)
SCREENSHOT_REDACTION_SCRIPT = r"""
(() => {
  const sensitive = /\b(?:password|passwd|cookie|token|api\s+key|access\s+key|gender|sex|race|ethnicity|veteran|disability|sexual\s+orientation|marital\s+status|religion|pronouns?|date\s+of\s+birth|birth\s+date|social\s+security|ssn|national\s+id|work\s+authorization|citizenship|visa\s+status)\b/i;
  const value = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|\b(?:bearer\s+)?(?:gh[pousr]_[A-Z0-9_]{12,}|sk-[A-Z0-9_-]{12,}|xox[baprs]-[A-Z0-9-]{12,})\b|\beyJ[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\.[A-Z0-9_-]{5,}\b|\b(?:password|passwd|token|api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|cookie)\s*[:=]\s*[^\s,;]+|(?<!\w)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\w)/i;
  const mask = element => {
    if (element && element !== document.body && element !== document.documentElement) element.setAttribute('data-mjobs-sensitive-mask', '');
  };
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = (node.nodeValue || '').trim();
    if (!text) continue;
    const element = node.parentElement;
    if (value.test(text)) mask(element);
    if (sensitive.test(text) && element.parentElement) mask(element.parentElement);
  }
})();
"""
SCREENSHOT_PRIVACY_STYLE = """
* {
  color: transparent !important;
  -webkit-text-fill-color: transparent !important;
  text-shadow: none !important;
  background-image: none !important;
}
*::before, *::after { content: none !important; }
img, picture, canvas, svg, video, iframe, object, embed {
  visibility: hidden !important;
}
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact_url(value):
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    path = redact_sensitive_text(parsed.path or "/")
    return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", path, "", ""))


def safe_url(value):
    parsed = urlsplit(str(value or ""))
    if parsed.hostname not in ALLOWED_HOSTS or parsed.scheme != "http":
        raise ValueError("Live feedback is restricted to local HTTP MJobs instances")
    return redact_url(value)
def compact_text(value, limit=MAX_METADATA_TEXT):
    text = redact_sensitive_text(" ".join(str(value or "").split()))
    return text[:limit]


def redact_sensitive_text(value):
    text = str(value or "")
    if SENSITIVE_LABEL_RE.search(text):
        return "[REDACTED]"
    for pattern in SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def text_fingerprint(value):
    raw = str(value or "").encode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}


def capture_redacted_screenshot(page, path):
    """Capture layout context while excluding visible page text and embedded media."""
    page.evaluate(SCREENSHOT_REDACTION_SCRIPT)
    masks = [
        page.locator("input"),
        page.locator("textarea"),
        page.locator("select"),
        page.locator("[contenteditable]:not([contenteditable='false'])"),
        page.locator("[role='textbox']"),
        page.locator("[role='combobox']"),
        page.locator("[data-sensitive]"),
        page.locator("[data-mjobs-sensitive-mask]"),
    ]
    try:
        page.screenshot(
            path=str(path), full_page=True, mask=masks, mask_color="#000000",
            style=SCREENSHOT_PRIVACY_STYLE,
        )
    finally:
        page.evaluate(
            "document.querySelectorAll('[data-mjobs-sensitive-mask]')"
            ".forEach(node => node.removeAttribute('data-mjobs-sensitive-mask'))"
        )


def sanitize_interaction(payload):
    if not isinstance(payload, dict):
        return {"type": "invalid"}
    kind = compact_text(payload.get("type"), 32) or "unknown"
    tag = compact_text(payload.get("tag"), 24).lower()
    raw_text = str(payload.get("text") or "")
    raw_aria = str(payload.get("aria_label") or "")
    sensitive_context = bool(
        payload.get("sensitive_context")
        or SENSITIVE_LABEL_RE.search(raw_text)
        or SENSITIVE_LABEL_RE.search(raw_aria)
    )
    result = {
        "type": kind,
        "tag": tag,
        "id": compact_text(payload.get("id"), 80),
        "name": compact_text(payload.get("name"), 80),
        "role": compact_text(payload.get("role"), 80),
        "aria_label": "[REDACTED]" if sensitive_context else compact_text(raw_aria, 120),
    }
    if tag not in {"input", "textarea", "select", "option"}:
        result["text"] = "[REDACTED]" if sensitive_context else compact_text(raw_text)
    return result


def new_session_directory():
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    prune_expired_sessions(output_root=OUTPUT_ROOT)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_ROOT / stamp
    suffix = 1
    while path.exists():
        path = OUTPUT_ROOT / f"{stamp}-{suffix:02d}"
        suffix += 1
    path.mkdir(parents=True)
    return path


def prune_expired_sessions(*, now=None, output_root=OUTPUT_ROOT):
    """Remove only expired, directly owned live-feedback session directories."""
    root = Path(output_root)
    if _is_link_or_junction(root) or not root.is_dir():
        return []
    for parent in root.parents:
        if _is_link_or_junction(parent):
            return []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return []
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Retention clock must be timezone-aware")
    cutoff = current.astimezone(timezone.utc) - timedelta(days=FEEDBACK_RETENTION_DAYS)
    expired = []
    for entry in root.iterdir():
        if _is_link_or_junction(entry) or not entry.is_dir():
            continue
        try:
            if entry.resolve(strict=True).parent != resolved_root:
                continue
        except OSError:
            continue
        match = SESSION_DIRECTORY_RE.fullmatch(entry.name)
        if match is None:
            continue
        try:
            created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if created < cutoff:
            expired.append((created, entry.name, entry))

    removed = []
    for _, name, entry in sorted(expired):
        try:
            shutil.rmtree(entry)
        except FileNotFoundError:
            continue
        except OSError:
            continue
        removed.append(name)

    marker = root / "LATEST.txt"
    if removed and not _is_link_or_junction(marker) and marker.is_file():
        try:
            latest_name = marker.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            latest_name = ""
        if latest_name in removed:
            retained = []
            for entry in root.iterdir():
                match = SESSION_DIRECTORY_RE.fullmatch(entry.name)
                if _is_link_or_junction(entry) or not entry.is_dir() or match is None:
                    continue
                try:
                    created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                retained.append((created, entry.name))
            if retained:
                try:
                    marker.write_text(max(retained)[1] + "\n", encoding="utf-8")
                except OSError:
                    pass
            else:
                try:
                    marker.unlink(missing_ok=True)
                except OSError:
                    pass
    return removed


def _is_link_or_junction(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def latest_session_directory():
    marker = OUTPUT_ROOT / "LATEST.txt"
    if not marker.is_file():
        raise FileNotFoundError("No live-feedback session has been recorded")
    path = (OUTPUT_ROOT / marker.read_text(encoding="utf-8").strip()).resolve()
    if not path.is_relative_to(OUTPUT_ROOT.resolve()) or not path.is_dir():
        raise ValueError("LATEST.txt does not reference a valid feedback session")
    return path


def write_latest(session_dir):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUTPUT_ROOT / "LATEST.txt").write_text(session_dir.name + "\n", encoding="utf-8")


class Recorder:
    def __init__(self, session_dir, diagnostic_text=False):
        self.session_dir = session_dir
        self.diagnostic_text = diagnostic_text
        self.events = []
        self.stream = (session_dir / "events.jsonl").open("a", encoding="utf-8")

    def close(self):
        self.stream.close()
    def record(self, kind, payload):
        event = {"timestamp": utc_now(), "kind": kind, **payload}
        self.events.append(event)
        self.stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        self.stream.flush()

    def diagnostic_message(self, kind, level, value):
        safe_value = redact_sensitive_text(value)
        payload = {
            "level": compact_text(level, 24),
            **text_fingerprint(safe_value),
            "sensitive_values_redacted": safe_value != str(value or ""),
        }
        self.record(kind, payload)


INIT_SCRIPT = r"""
(() => {
  const describe = (node, type) => {
    const element = node && node.closest ? node.closest('button,a,input,textarea,select,[role]') : null;
    if (!element) return null;
    const tag = (element.tagName || '').toLowerCase();
    const sensitive = ['input','textarea','select','option'].includes(tag);
    const context = element.closest('fieldset,[role="group"],[data-field]') || element.parentElement;
    const sensitive_context = /\b(password|passwd|cookie|token|api\s+key|access\s+key|gender|sex|race|ethnicity|veteran|disability|sexual\s+orientation|marital\s+status|religion|pronouns?|date\s+of\s+birth|birth\s+date|social\s+security|ssn|national\s+id|work\s+authorization|citizenship|visa\s+status)\b/i.test(context ? context.innerText || '' : '');
    return {
      type,
      tag,
      id: element.id || '',
      name: element.getAttribute('name') || '',
      role: element.getAttribute('role') || '',
      aria_label: element.getAttribute('aria-label') || '',
      sensitive_context,
      text: sensitive ? '' : (element.innerText || element.textContent || '').slice(0, 160)
    };
  };
  for (const type of ['click','change']) {
    window.addEventListener(type, (event) => {
      const payload = describe(event.target, type);
      if (payload && window.mjobsFeedbackEvent) window.mjobsFeedbackEvent(payload);
    }, true);
  }
})();
"""
def run_session(base_url, visuals=False, diagnostic_text=False):
    base_url = safe_url(base_url)
    session_dir = new_session_directory()
    recorder = Recorder(session_dir, diagnostic_text=diagnostic_text)
    started = utc_now()

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.expose_function(
                "mjobsFeedbackEvent",
                lambda payload: recorder.record("interaction", sanitize_interaction(payload)),
            )
            page.add_init_script(INIT_SCRIPT)
            page.on("framenavigated", lambda frame: recorder.record(
                "navigation", {"url": redact_url(frame.url)}
            ) if frame == page.main_frame and frame.url.startswith("http") else None)
            page.on("console", lambda message: recorder.diagnostic_message(
                "console", message.type, message.text
            ) if message.type in {"warning", "error"} else None)
            page.on("pageerror", lambda error: recorder.diagnostic_message(
                "page_error", "error", str(error)
            ))
            page.on("requestfailed", lambda request: recorder.record(
                "request_failed",
                {"method": request.method, "url": safe_url(request.url),
                 "failure": compact_text(request.failure, 120)},
            ) if request.url.startswith("http://127.0.0.1") or request.url.startswith("http://localhost") else None)
            page.on("response", lambda response: recorder.record(
                "http_error",
                {"status": response.status, "method": response.request.method,
                 "url": safe_url(response.url)},
            ) if response.status >= 400 and (
                response.url.startswith("http://127.0.0.1") or response.url.startswith("http://localhost")
            ) else None)

            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            print(f"Live feedback session: {session_dir}")
            print("Use the headed browser normally. Typed field values are not recorded.")
            if visuals:
                print(
                    "Redacted screenshot capture is ON; form controls, visible page text, "
                    "and embedded media are hidden. Review the private local image before sharing."
                )
            print("Press ENTER in this terminal when you finish the test flow.")
            try:
                input()
            except EOFError:
                print("No interactive stdin; close the browser or terminate the task after your flow.")

            final_url = redact_url(page.url)
            title = compact_text(page.title(), 200)
            if visuals:
                capture_redacted_screenshot(page, session_dir / "final.png")
            browser.close()

        ended = utc_now()
        summary = {
            "schema_version": 1,
            "started_at": started,
            "ended_at": ended,
            "base_url": base_url,
            "final_url": final_url,
            "page_title": title,
            "visuals": visuals,
            "diagnostic_text_requested": bool(diagnostic_text),
            "diagnostic_storage": "fingerprint_only",
            "event_count": len(recorder.events),
            "events": recorder.events,
        }
        (session_dir / "session.json").write_text(
            json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (session_dir / "feedback.md").write_text(
            "# MJobs Live Feedback\n\n"
            "## What I tried\n\n"
            "Describe the workflow you performed.\n\n"
            "## What I expected\n\n"
            "Describe the expected behavior.\n\n"
            "## What actually happened\n\n"
            "Describe what happened, including anything confusing or missing.\n\n"
            "## Requested change\n\n"
            "Describe the change you want Codex to make.\n",
            encoding="utf-8",
        )
        write_latest(session_dir)
        print(f"Saved session: {session_dir / 'session.json'}")
        print(f"Add your notes: {session_dir / 'feedback.md'}")
        return session_dir
    finally:
        recorder.close()


def summarize(session_dir=None):
    session_dir = Path(session_dir).resolve() if session_dir else latest_session_directory()
    data = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    counts = {}
    for event in data.get("events", []):
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    print(json.dumps({
        "session_dir": str(session_dir),
        "started_at": data.get("started_at"),
        "final_url": data.get("final_url"),
        "event_count": data.get("event_count"),
        "event_types": counts,
        "feedback_file": str(session_dir / "feedback.md"),
        "visuals": data.get("visuals"),
    }, indent=2))
def build_parser():
    parser = argparse.ArgumentParser(description="MJobs local live-feedback browser harness")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Start a headed browser and record safe interaction diagnostics")
    run.add_argument("--base-url", default="http://127.0.0.1:18700")
    run.add_argument("--visuals", action="store_true",
                     help=("Opt in to a layout screenshot with page text and embedded "
                           "media hidden and form controls masked"))
    run.add_argument("--diagnostic-text", action="store_true",
                     help="Legacy compatibility flag; raw console/page-error text is never stored")
    report = sub.add_parser("summarize", help="Summarize the latest or selected feedback session")
    report.add_argument("--session-dir")
    return parser


def main():
    args = build_parser().parse_args()
    if args.command == "run":
        run_session(args.base_url, visuals=args.visuals, diagnostic_text=args.diagnostic_text)
    else:
        summarize(args.session_dir)


if __name__ == "__main__":
    main()
