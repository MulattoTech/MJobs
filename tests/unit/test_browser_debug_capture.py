import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from services.applications import debug_capture as debug_capture_module
from services.applications.debug_capture import (
    append_diagnostic_event,
    append_console_error,
    capture_browser_failure,
    prune_debug_captures,
    sanitize_debug_url,
)


class BrowserDebugCaptureTests(unittest.TestCase):
    def test_duplicate_attempt_preserves_existing_capture_package(self):
        attempt_id = "e" * 32
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "debug-captures"
            existing = root / attempt_id
            existing.mkdir(parents=True)
            marker = existing / "metadata.json"
            marker.write_text('{"status":"existing"}\n', encoding="utf-8")

            capture = capture_browser_failure(
                None,
                root,
                attempt_id=attempt_id,
                step="DETECT_FORM",
                configured_url="https://example.test/apply/existing",
                error=RuntimeError("SYNTHETIC_DUPLICATE_ATTEMPT"),
            )

            self.assertIsNone(capture)
            self.assertEqual(
                marker.read_text(encoding="utf-8"),
                '{"status":"existing"}\n',
            )

    def test_failed_metadata_publication_removes_incomplete_capture(self):
        attempt_id = "f" * 32
        original_atomic_json = debug_capture_module._atomic_json

        def fail_metadata_publication(path, value):
            if path.name == "metadata.json":
                raise OSError("synthetic metadata publication failure")
            return original_atomic_json(path, value)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "debug-captures"
            with mock.patch.object(
                debug_capture_module,
                "_atomic_json",
                side_effect=fail_metadata_publication,
            ):
                capture = capture_browser_failure(
                    None,
                    root,
                    attempt_id=attempt_id,
                    step="DETECT_FORM",
                    configured_url="https://example.test/apply/private-id?token=secret",
                    error=RuntimeError("PRIVATE_EXCEPTION_MESSAGE"),
                )

            self.assertIsNone(capture)
            self.assertFalse((root / attempt_id).exists())

    def test_fixed_application_trace_phases_are_allowed_in_failure_metadata(self):
        phases = ("FILL_LOCATION", "ANSWER_STANDARD", "ANSWER_CUSTOM")
        with tempfile.TemporaryDirectory() as directory:
            for index, phase in enumerate(phases):
                with self.subTest(phase=phase):
                    capture = capture_browser_failure(
                        None,
                        Path(directory) / phase,
                        attempt_id=f"{index:032x}",
                        step=phase,
                        configured_url="http://127.0.0.1:8090/ats/apply/B",
                        error=RuntimeError("SYNTHETIC_FAILURE"),
                    )
                    self.assertIsNotNone(capture)
                    metadata = json.loads(
                        (capture / "metadata.json").read_text(encoding="utf-8"))
                    self.assertEqual(metadata["current_step"], phase)

    def test_failure_capture_contains_only_bounded_structural_and_hashed_data(self):
        class Page:
            url = "http://user:password@127.0.0.1:8090/ats/apply/private-id?token=secret#answer"

            def __init__(self):
                self.screenshots = []
                self.evaluations = 0

            def evaluate(self, script):
                self.evaluations += 1
                raise AssertionError("Unbounded diagnostic evaluation is forbidden")

            def locator(self, selector):
                return selector

            def screenshot(self, **kwargs):
                self.screenshots.append(kwargs)
                Path(kwargs["path"]).write_bytes(b"synthetic-redacted-png")

        now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
        page = Page()
        events = []
        append_diagnostic_event(events, "console", page.url, "PRIVATE_CONSOLE_MESSAGE")
        append_diagnostic_event(events, "requestfailed", page.url, "PRIVATE_NETWORK_ERROR")
        with tempfile.TemporaryDirectory() as directory:
            capture = capture_browser_failure(
                page,
                Path(directory) / "debug-captures",
                attempt_id="a" * 32,
                step="DETECT_FORM",
                configured_url=page.url,
                error=RuntimeError("PRIVATE_EXCEPTION_MESSAGE"),
                events=events,
                now=now,
            )

            self.assertIsNotNone(capture)
            metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
            dom = json.loads((capture / "dom.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["current_step"], "DETECT_FORM")
            self.assertEqual(metadata["current_url"], "http://127.0.0.1:8090/:redacted/:redacted/:redacted")
            self.assertEqual(metadata["error"]["type"], "RuntimeError")
            self.assertNotIn("message", metadata["error"])
            self.assertEqual(len(metadata["diagnostics"]), 2)
            self.assertTrue(all("message_fingerprint" in event for event in metadata["diagnostics"]))
            self.assertTrue(all("message_sha256" not in event for event in metadata["diagnostics"]))
            self.assertEqual(page.evaluations, 0)
            self.assertEqual(dom["element_counts"], {})
            self.assertEqual(dom["role_counts"], {})
            self.assertEqual(dom["status"], "OMITTED_UNSUPPORTED_TRANSPORT")
            self.assertEqual(metadata["dom_snapshot"]["status"], dom["status"])
            for flag in ("text_omitted", "attributes_omitted", "control_values_omitted"):
                self.assertTrue(dom[flag])
            self.assertFalse((capture / "screenshot.png").read_bytes().find(b"PRIVATE") >= 0)
            serialized = (capture / "metadata.json").read_text(encoding="utf-8")
            serialized += (capture / "dom.json").read_text(encoding="utf-8")
            self.assertNotIn("PRIVATE", serialized)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("message_sha256", serialized)
            self.assertIn("message_fingerprint", metadata["error"])
            self.assertTrue((capture / "screenshot.png").is_file())
            self.assertEqual(page.screenshots[0]["full_page"], False)
            self.assertLessEqual(page.screenshots[0]["timeout"], 2000)

    def test_debug_url_drops_credentials_query_fragment_and_identifiers(self):
        self.assertEqual(
            sanitize_debug_url("https://user:pass@example.test/jobs/secret-id?token=private#section"),
            "https://example.test/:redacted/:redacted",
        )
        self.assertEqual(sanitize_debug_url("file:///private/resume.pdf"), "")

    def test_console_capture_ignores_non_error_messages(self):
        class ConsoleMessage:
            def __init__(self, message_type, text):
                self.type = message_type
                self.text = text

        events = []
        append_console_error(events, ConsoleMessage("log", "PRIVATE_NORMAL_LOG"), "https://example.test")
        append_console_error(events, ConsoleMessage("error", "PRIVATE_CONSOLE_ERROR"), "https://example.test")

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "console")
        self.assertNotIn("PRIVATE", json.dumps(events))

    def test_event_buffer_and_capture_retention_are_bounded(self):
        now = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)
        events = []
        for index in range(75):
            append_diagnostic_event(events, "console", "https://example.test/form?token=x", str(index))
        self.assertEqual(len(events), 50)
        self.assertNotIn("token=x", json.dumps(events))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "debug-captures"
            root.mkdir()
            for index in range(25):
                child = root / f"{index:032x}"
                child.mkdir()
                (child / "marker").write_text("synthetic", encoding="utf-8")
                timestamp = (now - timedelta(days=40) if index == 0
                             else now - timedelta(minutes=25-index))
                epoch = timestamp.timestamp()
                os.utime(child, (epoch, epoch))
            unknown = root / "manual-notes"
            unknown.mkdir()
            (unknown / "keep.txt").write_text("preserve", encoding="utf-8")

            removed = prune_debug_captures(root, now=now)

            self.assertIn("00000000000000000000000000000000", removed)
            self.assertEqual(len([path for path in root.iterdir() if path.name != "manual-notes"]), 20)
            self.assertEqual((unknown / "keep.txt").read_text(encoding="utf-8"), "preserve")


class _CdpPage:
    url = "https://fixture.invalid/apply?token=PRIVATE"

    def __init__(self, root, *, stall=None, phase_hook=None):
        self.root = root
        self.stall = stall
        self.phase_hook = phase_hook
        self.calls = []
        self.cancelled = []
        self.pending_tasks = None
        self._impl_obj = SimpleNamespace(context=SimpleNamespace(new_cdp_session=self.create))

    async def phase(self, name):
        self.calls.append(name)
        if self.phase_hook:
            self.phase_hook(name)
        if name == self.stall:
            try:
                await asyncio.Future()
            finally:
                self.cancelled.append(name)

    async def create(self, implementation):
        assert implementation is self._impl_obj
        await self.phase("create")
        return self

    async def send(self, method, params):
        assert method == "DOM.getDocument"
        assert params == {"depth": debug_capture_module.MAX_DOM_DEPTH, "pierce": False}
        await self.phase("send")
        return {"root": self.root}

    async def detach(self):
        await self.phase("detach")

    def _sync(self, coroutine):
        async def run():
            try:
                return await coroutine
            finally:
                self.pending_tasks = len(asyncio.all_tasks() - {asyncio.current_task()})
        return asyncio.run(run())

    def evaluate(self, *_args):
        raise AssertionError("Page JavaScript must not be evaluated")


def _document(children):
    return {"nodeType": 9, "nodeName": "#document", "children": children,
            "documentURL": "https://PRIVATE.invalid/token=PRIVATE"}


class NativeDomCaptureTests(unittest.TestCase):
    def snapshot(self, page, *, deadline=None, checkpoint=lambda: None):
        return debug_capture_module._dom_snapshot(
            page, deadline=time.monotonic() + 2 if deadline is None else deadline,
            checkpoint=checkpoint)

    def test_native_structure_omits_content_values_urls_and_custom_names(self):
        root = _document([{"nodeType": 1, "nodeName": "FORM", "attributes": ["id", "PRIVATE"],
            "children": [
                {"nodeType": 1, "nodeName": "INPUT", "attributes": [
                    "type", "password", "value", "PRIVATE", "role", "textbox", "name", "PRIVATE"]},
                {"nodeType": 1, "nodeName": "X-PRIVATE", "attributes": ["role", "PRIVATE"]},
                {"nodeType": 3, "nodeValue": "PRIVATE text"},
                {"nodeType": 1, "nodeName": "IFRAME", "attributes": ["src", "https://PRIVATE.invalid"],
                 "contentDocument": _document([{"nodeType": 1, "nodeName": "PRIVATE"}])},
                {"nodeType": 1, "nodeName": "DIV", "attributes": ["role", "PRIVATE" * 100000]},
            ]}])
        page = _CdpPage(root)
        dom = self.snapshot(page)
        self.assertEqual(dom["status"], "CAPTURED")
        self.assertEqual(dom["nodes"], [
            {"parent": None, "tag": "form"}, {"parent": 0, "tag": "input", "role": "textbox"},
            {"parent": 0, "tag": "other"}, {"parent": 0, "tag": "iframe"},
            {"parent": 0, "tag": "div"},
        ])
        self.assertEqual(dom["role_counts"], {"textbox": 1})
        self.assertLessEqual(len((json.dumps(dom, indent=2) + "\n").encode()),
                             debug_capture_module.MAX_DOM_BYTES)
        self.assertNotIn("PRIVATE", json.dumps(dom))
        self.assertNotIn("password", json.dumps(dom))
        self.assertEqual(page.calls, ["create", "send", "detach"])
        self.assertEqual(page.pending_tasks, 0)

    def test_wide_and_deep_documents_obey_retained_node_byte_and_depth_caps(self):
        wide = _document([{"nodeType": 1, "nodeName": "SECTION", "attributes": ["role", "progressbar"]}
                          for _ in range(1000)])
        deep = {"nodeType": 1, "nodeName": "DIV"}
        for _ in range(100):
            deep = {"nodeType": 1, "nodeName": "DIV", "children": [deep]}
        for root in (wide, _document([deep])):
            with self.subTest(kind="wide" if root is wide else "deep"):
                dom = self.snapshot(_CdpPage(root))
                self.assertEqual(dom["status"], "CAPTURED")
                self.assertTrue(dom["truncated"])
                self.assertLessEqual(len(dom["nodes"]), debug_capture_module.MAX_DOM_NODES)
                self.assertLessEqual(len((json.dumps(dom, indent=2) + "\n").encode()),
                                     debug_capture_module.MAX_DOM_BYTES)
                self.assertEqual(sum(dom["element_counts"].values()), len(dom["nodes"]))
                for index, node in enumerate(dom["nodes"]):
                    self.assertTrue(node["parent"] is None or 0 <= node["parent"] < index)
        self.assertLessEqual(len(dom["nodes"]), debug_capture_module.MAX_DOM_DEPTH)

    def test_publication_uses_lf_bytes_within_the_same_serialized_limit(self):
        wide = _document([{"nodeType": 1, "nodeName": "SECTION", "attributes": ["role", "progressbar"]}
                          for _ in range(1000)])
        dom = self.snapshot(_CdpPage(wide))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dom.json"
            debug_capture_module._atomic_json(path, dom)
            written = path.read_bytes()
        self.assertEqual(written, (json.dumps(dom, indent=2) + "\n").encode("utf-8"))
        self.assertNotIn(b"\r\n", written)
        self.assertLessEqual(len(written), debug_capture_module.MAX_DOM_BYTES)

    def test_each_stalled_protocol_phase_is_cancelled_without_background_tasks(self):
        for phase in ("create", "send", "detach"):
            with self.subTest(phase=phase), mock.patch.object(debug_capture_module, "DOM_TIMEOUT_MS", 40):
                page = _CdpPage(_document([]), stall=phase)
                started = time.monotonic()
                dom = self.snapshot(page)
                self.assertEqual(dom["status"], "OMITTED_TIMEOUT")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(page.cancelled, [phase])
                self.assertEqual(page.pending_tasks, 0)
                self.assertLessEqual(page.calls.count("detach"), 1)
                if phase == "create":
                    self.assertEqual(page.calls, ["create"])

    def test_expired_total_budget_starts_no_protocol_operation(self):
        page = _CdpPage(_document([]))
        dom = self.snapshot(page, deadline=time.monotonic() - 1)
        self.assertEqual(dom["status"], "OMITTED_TIMEOUT")
        self.assertEqual(page.calls, [])
        self.assertEqual(page.pending_tasks, 0)

    def test_installed_protocol_abort_wait_is_bounded_in_each_native_phase(self):
        try:
            from playwright._impl._connection import Channel, Connection, ProtocolCallback
        except ModuleNotFoundError:
            self.skipTest("Installed Playwright is required to verify its actual cancellation path")

        class NativeProtocolPage(_CdpPage):
            async def phase(self, name):
                self.calls.append(name)
                if name != self.stall:
                    return
                loop = asyncio.get_running_loop()
                self.sent = []
                self.transport_error = loop.create_future()
                self.callback = ProtocolCallback(loop, 1)
                transport = SimpleNamespace(on_error_future=self.transport_error, send=self.sent.append)
                connection = SimpleNamespace(_error=None, _transport=transport,
                    _send_message_to_server=lambda *_: self.callback)
                connection._abort = lambda *args: Connection._abort(connection, *args)
                channel = SimpleNamespace(_connection=connection, _object=SimpleNamespace(_guid="synthetic-cdp"))

                def release_stalled_callback():
                    if not self.callback.future.done():
                        self.callback.future.cancel()

                # A regression must fail promptly rather than hang the suite.
                guard = loop.call_later(.75, release_stalled_callback)
                try:
                    await Channel._inner_send(channel, "send", None, {}, False)
                finally:
                    guard.cancel()

        for phase in ("create", "send", "detach"):
            with self.subTest(phase=phase), mock.patch.object(debug_capture_module, "DOM_TIMEOUT_MS", 50):
                page = NativeProtocolPage(_document([]), stall=phase)
                started = time.monotonic()
                dom = self.snapshot(page)
                self.assertEqual(dom["status"], "OMITTED_TIMEOUT")
                self.assertLess(time.monotonic() - started, .5)
                self.assertTrue(page.callback.future.cancelled())
                self.assertFalse(page.transport_error.done())
                self.assertEqual([message["method"] for message in page.sent], ["__abort__"])
                self.assertEqual(page.pending_tasks, 0)

    def test_elapsed_create_and_send_share_one_budget_and_cannot_start_late_detach(self):
        clock = [100.0]

        def advance(phase):
            clock[0] += .2 if phase == "create" else .15

        page = _CdpPage(_document([]), phase_hook=advance)
        with mock.patch.object(debug_capture_module.time, "monotonic", lambda: clock[0]):
            dom = self.snapshot(page, deadline=102)
        self.assertEqual(dom["status"], "OMITTED_TIMEOUT")
        self.assertEqual(page.calls, ["create", "send"])
        self.assertEqual(page.pending_tasks, 0)

    def test_one_shot_caller_cancellation_during_dom_stops_capture_without_continuation(self):
        class Check:
            calls = 0
            cancelled_at = None
            pending = False

            def __call__(self):
                self.calls += 1
                if self.pending:
                    self.pending = False
                    self.cancelled_at = self.calls
                    raise RuntimeError("PRIVATE caller cancellation")

        check = Check()
        page = _CdpPage(_document([]), phase_hook=lambda phase: setattr(check, "pending", phase == "send"))
        with tempfile.TemporaryDirectory() as directory:
            result = capture_browser_failure(page, directory, attempt_id="1" * 32,
                step="DETECT_FORM", configured_url=page.url, error=ValueError("primary"), check=check)
            self.assertIsNone(result)
            self.assertFalse((Path(directory) / ("1" * 32)).exists())
        self.assertEqual(page.calls, ["create", "send"])
        self.assertEqual(check.calls, check.cancelled_at)
        self.assertEqual(page.pending_tasks, 0)


if __name__ == "__main__":
    unittest.main()
