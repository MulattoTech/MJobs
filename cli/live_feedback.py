"""Privacy-conscious headed-browser feedback recorder for local MJobs development."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = ROOT / "outputs" / "live-feedback"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
MAX_METADATA_TEXT = 160


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def redact_url(value):
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, f"{parsed.hostname}{port}", parsed.path or "/", "", ""))


def safe_url(value):
    parsed = urlsplit(str(value or ""))
    if parsed.hostname not in ALLOWED_HOSTS or parsed.scheme != "http":
        raise ValueError("Live feedback is restricted to local HTTP MJobs instances")
    return redact_url(value)
def compact_text(value, limit=MAX_METADATA_TEXT):
    text = " ".join(str(value or "").split())
    return text[:limit]


def text_fingerprint(value):
    raw = str(value or "").encode("utf-8", errors="replace")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}


def sanitize_interaction(payload):
    if not isinstance(payload, dict):
        return {"type": "invalid"}
    kind = compact_text(payload.get("type"), 32) or "unknown"
    tag = compact_text(payload.get("tag"), 24).lower()
    result = {
        "type": kind,
        "tag": tag,
        "id": compact_text(payload.get("id"), 80),
        "name": compact_text(payload.get("name"), 80),
        "role": compact_text(payload.get("role"), 80),
        "aria_label": compact_text(payload.get("aria_label"), 120),
    }
    if tag not in {"input", "textarea", "select", "option"}:
        result["text"] = compact_text(payload.get("text"))
    return result


def new_session_directory():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = OUTPUT_ROOT / stamp
    suffix = 1
    while path.exists():
        path = OUTPUT_ROOT / f"{stamp}-{suffix:02d}"
        suffix += 1
    path.mkdir(parents=True)
    return path


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
        payload = {"level": level, **text_fingerprint(value)}
        if self.diagnostic_text:
            payload["text"] = compact_text(value, 500)
        self.record(kind, payload)


INIT_SCRIPT = r"""
(() => {
  const describe = (node, type) => {
    const element = node && node.closest ? node.closest('button,a,input,textarea,select,[role]') : null;
    if (!element) return null;
    const tag = (element.tagName || '').toLowerCase();
    const sensitive = ['input','textarea','select','option'].includes(tag);
    return {
      type,
      tag,
      id: element.id || '',
      name: element.getAttribute('name') || '',
      role: element.getAttribute('role') || '',
      aria_label: element.getAttribute('aria-label') || '',
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
    trace_path = session_dir / "trace.zip"

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            if visuals:
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
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
                print("Visual tracing is ON and may contain personal page content.")
            print("Press ENTER in this terminal when you finish the test flow.")
            try:
                input()
            except EOFError:
                print("No interactive stdin; close the browser or terminate the task after your flow.")

            final_url = redact_url(page.url)
            title = compact_text(page.title(), 200)
            if visuals:
                page.screenshot(path=str(session_dir / "final.png"), full_page=True)
                context.tracing.stop(path=str(trace_path))
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
            "diagnostic_text": diagnostic_text,
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
                     help="Opt in to screenshots/Playwright trace; may capture personal page content")
    run.add_argument("--diagnostic-text", action="store_true",
                     help="Opt in to console/page-error text instead of hashes only")
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
