"""Chromium proves the API CSP blocks embedded objects without blocking local images."""
import re
import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from playwright.sync_api import expect, sync_playwright
import uvicorn

from apps.api.main import LocalOriginMiddleware
from fixtures.browser_ports import bound_browser_socket


def test_same_origin_object_is_blocked_but_same_origin_image_loads():
    sock = bound_browser_socket()
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    requests = {"object": 0, "image": 0}
    lock = threading.Lock()
    app = FastAPI()
    app.add_middleware(LocalOriginMiddleware)

    @app.get("/probe", response_class=HTMLResponse)
    def probe():
        return ("<!doctype html><img id='control' src='/image-marker'>"
                "<object id='blocked' data='/object-marker' type='text/html'></object>")

    @app.get("/object-marker", response_class=HTMLResponse)
    def object_marker():
        with lock:
            requests["object"] += 1
        return "<p>Object endpoint must not be reached</p>"

    @app.get("/image-marker")
    def image_marker():
        with lock:
            requests["image"] += 1
        return Response(
            content=(b"<svg xmlns='http://www.w3.org/2000/svg' width='1' height='1'>"
                     b"<rect width='1' height='1' fill='green'/></svg>"),
            media_type="image/svg+xml",
        )

    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                raise AssertionError("Isolated CSP probe server did not start")
            time.sleep(.02)

        external = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                context = browser.new_context()
                context.route(
                    re.compile(r"^https?://(?!(?:" + re.escape(urlsplit(origin).netloc) + r")(?:/|$))"),
                    lambda route: (external.append(route.request.url), route.abort()),
                )
                page = context.new_page()
                page.add_init_script("""window.__cspViolations = [];
                    addEventListener('securitypolicyviolation', event =>
                      window.__cspViolations.push({directive: event.effectiveDirective,
                                                   blocked: event.blockedURI}));""")
                page.goto(origin + "/probe", wait_until="load")

                # A same-origin non-object request is allowed by img-src 'self'.
                expect(page.locator("#control")).to_have_js_property("naturalWidth", 1)
                # The object element is present but cannot load its same-origin URL.
                expect(page.locator("#blocked")).to_have_js_property("contentDocument", None)
                violations = page.evaluate("window.__cspViolations")
                assert any(item["directive"] == "object-src" and
                           item["blocked"].endswith("/object-marker") for item in violations), violations
            finally:
                browser.close()

        assert requests == {"object": 0, "image": 1}
        assert external == []
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive(), "Owned CSP probe server must stop"
