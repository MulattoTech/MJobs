"""Disposable private observer bootstrap; all input is synthetic pipe data."""
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sqlalchemy import select, update
from packages.persistence import models as m
from packages.persistence.store import Store
from services.applications.browser_process import capture_owner_identity
from services.applications.browser_tree import BrowserTreeJob
from services.applications.browser_tree_ipc import BrowserTreeObserver


def main():
    config = json.loads(sys.stdin.readline())
    directory = Path(config['directory'])
    store = Store(config['url'])
    operation = config['binding']['operation_id']
    with store.engine.connect() as connection:
        row = connection.execute(select(m.operations).where(m.operations.c.id == operation)).mappings().one()
    metadata = row['recovery_metadata']
    metadata['manual_auth_owner']['api_process'] = capture_owner_identity()
    with store.engine.begin() as connection:
        connection.execute(update(m.operations).where(m.operations.c.id == operation).values(
            recovery_metadata=metadata))
    job = BrowserTreeJob()
    query = job.query_handle()
    observer = BrowserTreeObserver(query, config['binding'], store=store, owner_token=config['token'])
    job.launch([sys.executable, str(Path(__file__).with_name('browser_tree_child.py')),
                'browser', str(directory)], cwd=Path.cwd())
    deadline = time.monotonic() + 20
    while not (directory / 'ready.json').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('Synthetic browser unavailable')
        time.sleep(.05)
    (directory / 'observer.json').write_text(json.dumps(observer.identity))
    os._exit(0)


if __name__ == '__main__':
    main()
