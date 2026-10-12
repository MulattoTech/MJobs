"""Cooperative disposable child; no credentials, network or process killing."""
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.applications.browser_process import capture_owner_identity


def write(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def wait(directory, seconds=25):
    deadline = time.monotonic() + seconds
    while not (directory / "release").exists() and time.monotonic() < deadline:
        time.sleep(.05)


def main():
    role, location = sys.argv[1:]
    directory = Path(location)
    if role == "descendant":
        write(directory / "descendant.json", capture_owner_identity())
        wait(directory)
    elif role == "root":
        subprocess.Popen([sys.executable, __file__, "descendant", location],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while not (directory / "descendant.json").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Descendant readiness unavailable")
            time.sleep(.05)
        write(directory / "ready.json", {"host": capture_owner_identity(),
              "descendant": json.loads((directory / "descendant.json").read_text())})
    elif role == "browser":
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(directory / "synthetic-profile"), headless=True,
                args=["--enable-automation"], timeout=15000)
            try:
                context.route("**/*", lambda route: route.abort())
                page = context.pages[0]
                page.goto("about:blank")
                session = context.browser.new_browser_cdp_session()
                try:
                    members = [item["id"] for item in session.send("SystemInfo.getProcessInfo")["processInfo"]]
                finally:
                    session.detach()
                write(directory / "ready.json", {"host": capture_owner_identity(), "members": members})
                deadline = time.monotonic() + 25
                while not (directory / "release").exists() and time.monotonic() < deadline:
                    page.wait_for_timeout(50)
            finally:
                context.close()


if __name__ == "__main__":
    main()
