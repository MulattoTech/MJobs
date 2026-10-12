"""Disposable creator transfers QUERY authority to its trusted test parent."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from services.applications.browser_process import capture_owner_identity, _read_process
from services.applications.browser_tree import BrowserTreeJob, _kernel


def main():
    location, interpreter, parent_json = sys.argv[1:]
    directory = Path(location)
    parent = json.loads(parent_json)
    kernel = _kernel()
    target = kernel.OpenProcess(0x40 | 0x1000, False, parent["pid"])
    if not target:
        raise ValueError("Synthetic observer unavailable")
    job = BrowserTreeJob()
    query = job.query_handle()
    try:
        current = _read_process(parent["pid"])
        if current is None or current != (parent["created"], "LIVE"):
            raise ValueError("Synthetic observer changed")
        handle = query.duplicate_to(int(target))
        temporary = directory / "retained.tmp"
        temporary.write_text(json.dumps({"handle": handle, "creator": capture_owner_identity()}))
        temporary.replace(directory / "retained.json")
        deadline = time.monotonic() + 15
        while not (directory / "enrolled").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Synthetic observer enrollment unavailable")
            time.sleep(.05)
        job.launch([interpreter, str(Path(__file__).with_name("browser_tree_child.py")),
                    "browser", location], cwd=Path.cwd())
        deadline = time.monotonic() + 20
        while not (directory / "ready.json").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("Synthetic browser readiness unavailable")
            time.sleep(.05)
        # No Python cleanup: prove actual loss of every creator-owned handle.
        os._exit(0)
    finally:
        kernel.CloseHandle(target)
        query.close()
        job.close()


if __name__ == "__main__":
    main()
