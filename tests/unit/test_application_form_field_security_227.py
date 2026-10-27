"""Synthetic security contract for application field values (MJ-REQ-226).

The controlled dry-run browser boundary is exercised with an in-memory
Playwright double. No browser, network, employer page, service, or process is
started by these tests.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from services.applications import controlled_dry_run


SPECIAL_VALUES = [
    'Riley "RJ" Chen',
    "Zoë 李",
    "Research & Development",
    "Design - Systems",
    "O'Neil",
    "R&D — \"O'Neil\" 東京; $(touch never-run)",
]


def _process_launch_apis():
    subprocess_names = (
        "Popen", "run", "call", "check_call", "check_output", "getoutput",
        "getstatusoutput",
    )
    apis = [
        (subprocess, name) for name in subprocess_names
        if callable(getattr(subprocess, name, None))
    ]
    os_names = {"system", "popen", "startfile", "fork", "forkpty"}
    os_names.update(
        name for name in dir(os)
        if name.startswith(("spawn", "exec", "posix_spawn"))
        and callable(getattr(os, name, None))
    )
    apis.extend(
        (os, name) for name in sorted(os_names)
        if callable(getattr(os, name, None))
    )
    return tuple(apis)


def _install_process_launch_sentinels(monkeypatch):
    attempts = []

    def forbidden_process(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("application input reached a process API")

    apis = _process_launch_apis()
    for module, name in apis:
        monkeypatch.setattr(module, name, forbidden_process)
    return apis, attempts, forbidden_process


class _Route:
    def __init__(self, page, url):
        self.request = SimpleNamespace(
            url=url,
            method="GET",
            is_navigation_request=lambda: True,
            frame=page.main_frame,
            redirected_from=None,
        )
        self.fulfilled = False
        self.aborted = False

    def fulfill(self, **_kwargs):
        self.fulfilled = True

    def abort(self):
        self.aborted = True


class _Locator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector
        self.key = None
        if selector.startswith("form [name=\""):
            self.key = selector[len('form [name="'):-2]

    def count(self):
        if self.selector == "form":
            return 1
        if self.selector == "input, textarea, select":
            return len(self.page.current_fields)
        if self.selector == "button, iframe, frame, object, embed, script, base":
            return 0
        if self.selector in {"a", "a[data-controlled-next]"}:
            return int(self.page.page_id == "contact")
        if self.key is not None:
            return int(self.key in self.page.fields_by_key)
        raise AssertionError(f"Unexpected locator selector: {self.selector}")

    def get_attribute(self, name):
        if self.selector == "form":
            if name == "data-controlled-page":
                return self.page.page_id
            if name == "action":
                return None
        if self.selector in {"a", "a[data-controlled-next]"}:
            if name == "href":
                return controlled_dry_run.ORIGIN + "/documents"
            return None
        raise AssertionError(f"Unexpected get_attribute({name!r}) on {self.selector!r}")

    def evaluate(self, expression):
        if self.key is not None and expression.startswith("node =>"):
            field = self.page.fields_by_key[self.key]
            return {
                "name": field["key"],
                "type": field["type"],
                "required": field["required"],
            }
        if self.selector == "form" and expression.startswith("form =>"):
            return True
        raise AssertionError(f"Unexpected evaluate on {self.selector!r}")

    def fill(self, value):
        assert self.key is not None
        self.page.filled.append((self.key, value))
        self.page.values[self.key] = value

    def input_value(self):
        assert self.key is not None
        return self.page.values.get(self.key, "")

    def click(self, **_kwargs):
        self.page.goto(controlled_dry_run.ORIGIN + "/documents")


class _Page:
    def __init__(self, context):
        self.context = context
        self.main_frame = object()
        self.frames = [self.main_frame]
        self.url = "about:blank"
        self.page_id = None
        self.current_fields = []
        self.fields_by_key = {}
        self.values = {}
        self.filled = []

    def set_default_timeout(self, _timeout):
        pass

    def set_default_navigation_timeout(self, _timeout):
        pass

    def goto(self, url, **_kwargs):
        route = _Route(self, url)
        assert self.context.route_handler is not None
        self.context.route_handler(route)
        assert route.fulfilled and not route.aborted
        self.url = url
        self.page_id = "documents" if url.endswith("/documents") else "contact"
        fixture = controlled_dry_run.template("greenhouse")
        page_plan = next(page for page in fixture["pages"] if page["id"] == self.page_id)
        self.current_fields = [
            field for field in self.context.current["fields"]
            if field["key"] in page_plan["field_keys"]
        ]
        self.fields_by_key = {field["key"]: field for field in self.current_fields}

    def locator(self, selector):
        return _Locator(self, selector)

    def expect_navigation(self, **_kwargs):
        class _ExpectedNavigation:
            def __enter__(self):
                return None

            def __exit__(self, *_exc):
                return False

        return _ExpectedNavigation()


class _Context:
    def __init__(self, current):
        self.current = current
        self.pages = []
        self.route_handler = None

    def new_page(self):
        page = _Page(self)
        self.pages = [page]
        return page

    def route(self, _pattern, handler):
        self.route_handler = handler


class _Browser:
    def __init__(self, current):
        self.current = current
        self.context = None
        self.closed = False

    def new_context(self, **kwargs):
        assert kwargs == {"service_workers": "block"}
        self.context = _Context(self.current)
        return self.context

    def close(self):
        self.closed = True


class _Playwright:
    def __init__(self, current):
        self.current = current
        self.browser = _Browser(current)
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def launch(self, *, headless, timeout):
        assert headless is True
        assert timeout > 0
        return self.browser


def _current(value):
    fixture = controlled_dry_run.template("greenhouse")
    fields = [
        {"key": "full_name", "type": "text", "required": True,
         "source": "CONTACT", "value": value},
        {"key": "email", "type": "email", "required": True,
         "source": "CONTACT", "value": "fixture@example.invalid"},
        {"key": "weekend_availability", "type": "textarea", "required": True,
         "source": "VERIFIED_ANSWER", "value": "Fictional fixture response"},
        {"key": "resume", "type": "file", "required": True,
         "source": "DOCUMENT", "value": ""},
        {"key": "cover_letter", "type": "file", "required": True,
         "source": "DOCUMENT", "value": ""},
        {"key": "supporting_documents", "type": "file", "required": False,
         "source": "DOCUMENT", "value": ""},
    ]
    return {"provider": "greenhouse", "template_sha256": fixture["template_sha256"],
            "fields": fields}


def test_process_launch_sentinels_block_all_available_apis_without_child_start(monkeypatch):
    apis, attempts, forbidden_process = _install_process_launch_sentinels(monkeypatch)
    os_names = {name for module, name in apis if module is os}
    assert "system" in os_names
    assert "popen" in os_names
    assert any(name.startswith(("spawn", "exec", "posix_spawn")) for name in os_names)

    for module, name in apis:
        assert getattr(module, name) is forbidden_process
        with pytest.raises(AssertionError, match="application input reached a process API"):
            getattr(module, name)(SPECIAL_VALUES[-1])
    assert len(attempts) == len(apis)


@pytest.mark.parametrize("value", SPECIAL_VALUES)
def test_special_application_values_remain_literal_field_data(value, monkeypatch):
    current = _current(value)
    playwright = _Playwright(current)
    package = ModuleType("playwright")
    package.__path__ = []
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: playwright
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)

    # Fail closed on process APIs, including the OS-level exec/spawn wrappers.
    _, process_attempts, _ = _install_process_launch_sentinels(monkeypatch)

    result = controlled_dry_run.run_browser(current, [], [], check=lambda: None)
    assert process_attempts == []

    page = playwright.browser.context.pages[0]
    assert ("full_name", value) in page.filled
    assert page.values["full_name"] == value
    assert result["pages"] == [
        {"id": "contact", "control_count": 3, "validation": "PASS"},
        {"id": "documents", "control_count": 3, "validation": "PASS"},
    ]
    assert playwright.browser.closed


def test_negative_control_detects_unsafe_shell_interpolation_without_execution():
    # These are parser-only controls: no command is run. A naïve unquoted
    # interpolation splits ampersands into shell operators and breaks on the
    # user's apostrophe, demonstrating why values must stay in form fields.
    assert shlex.split("apply --name North & South") == [
        "apply", "--name", "North", "&", "South",
    ]
    with pytest.raises(ValueError, match="quotation"):
        shlex.split("apply --name O'Neil")
