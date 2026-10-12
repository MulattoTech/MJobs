"""Disposable API death and cooperative real-browser close; synthetic data only."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.applications.browser_tree import BrowserTreeJob


def controlled_job(directory):
    directory = Path(directory)
    adapter = '''
from pathlib import Path
from types import SimpleNamespace
import playwright.sync_api as api
original = api.sync_playwright
close_file = Path(CLOSE_PATH)
ready_file = Path(READY_PATH)
class Page:
    def __init__(self, page): self.page = page
    def __getattr__(self, key): return getattr(self.page, key)
    def goto(self, *args, **kwargs):
        result = self.page.goto(*args, **kwargs)
        ready_file.touch()
        return result
    def wait_for_timeout(self, milliseconds):
        if close_file.exists(): self.page.close()
        return self.page.wait_for_timeout(milliseconds)
class Context:
    def __init__(self, context): self.context = context
    def __getattr__(self, key): return getattr(self.context, key)
    @property
    def pages(self): return [Page(page) for page in self.context.pages]
    def new_page(self): return Page(self.context.new_page())
class Manager:
    def __enter__(self):
        self.manager = original()
        self.playwright = self.manager.__enter__()
        chromium = self.playwright.chromium
        def launch(*args, **kwargs):
            kwargs['headless'] = True
            return Context(chromium.launch_persistent_context(*args, **kwargs))
        return SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
    def __exit__(self, *args): return self.manager.__exit__(*args)
api.sync_playwright = Manager
'''.replace('CLOSE_PATH', repr(str(directory / 'close-browser'))).replace(
        'READY_PATH', repr(str(directory / 'host-ready')))

    class ControlledJob(BrowserTreeJob):
        def launch(self, args, **kwargs):
            args = list(args)
            prefix, suffix = args[2].split(';import sys;sys.argv=', 1)
            args[2] = prefix + '\n' + adapter + '\nimport sys;sys.argv=' + suffix
            return super().launch(args, **kwargs)
    return ControlledJob


def main():
    from fastapi.testclient import TestClient
    from apps.api.main import create_app
    from packages.persistence.store import Store

    config = json.loads(sys.stdin.readline())
    root = Path(config['directory'])
    def stop_owned_api():
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if (root / 'exit-api').exists():
                os._exit(0)  # This disposable API only; no finally/lease release.
            time.sleep(.05)
        (root / 'close-browser').touch()
        os._exit(2)
    threading.Thread(target=stop_owned_api, daemon=True).start()
    store = Store(config['url'])
    client = TestClient(create_app(store, config['origin'], root, operation_mode='FIXTURE'))
    with patch('services.applications.manual_auth_isolated.BrowserTreeJob', controlled_job(root)), \
            patch('apps.api.manual_auth.timeout', return_value=20):
        response = client.post(config['path'])
    (root / 'api-returned.json').write_text(json.dumps({'status_code': response.status_code}))


if __name__ == '__main__':
    main()
