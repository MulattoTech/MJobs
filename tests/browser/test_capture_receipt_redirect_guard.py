"""Use two disposable loopback HTTP listeners to prove redirect containment."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

from playwright.sync_api import sync_playwright

from tests.browser.test_manual_capture_review_receipt_safety import fetch_creation_receipt


@contextmanager
def listener(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        if worker.is_alive():
            raise RuntimeError("Owned listener did not stop")


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, location=None):
        self.send_response(status)
        if location:
            self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def consume(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))


class CaptureReceiptRedirectGuardTests(unittest.TestCase):
    def test_confirmation_post_does_not_follow_307_or_308(self):
        sink_requests = []
        class Sink(QuietHandler):
            def do_POST(self):
                self.consume()
                sink_requests.append(self.path)
                self.reply(201)

            def do_GET(self):
                sink_requests.append(self.path)
                self.reply(201)

        with listener(Sink) as sink:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for redirect in (307, 308):
                        with self.subTest(redirect=redirect):
                            source_requests = []
                            failures = []
                            class Source(QuietHandler):
                                def do_GET(self):
                                    self.reply(200)

                                def do_POST(self):
                                    self.consume()
                                    source_requests.append(self.path)
                                    self.reply(redirect, sink+"/redirected-confirmation")

                            with listener(Source) as source:
                                context = browser.new_context()
                                try:
                                    page = context.new_page()
                                    def guard(route):
                                        if route.request.url != source+"/jobs/manual":
                                            if route.request.url.startswith(source+"/") and route.request.method == "GET":
                                                route.continue_()
                                            else:
                                                route.abort()
                                            return
                                        try:
                                            fetch_creation_receipt(self, route)
                                        except AssertionError as exc:
                                            failures.append(str(exc))
                                        finally:
                                            route.abort()
                                    context.route("**/*", guard)
                                    page.goto(source+"/", wait_until="domcontentloaded")
                                    page.evaluate("fetch('/jobs/manual',{method:'POST',body:'synthetic'}).catch(()=>null)")
                                    self.assertEqual(source_requests, ["/jobs/manual"])
                                    self.assertEqual(len(failures), 1)
                                    self.assertIn(f"{redirect} != 201", failures[0])
                                    self.assertEqual(sink_requests, [])
                                finally:
                                    context.close()
                finally:
                    browser.close()
