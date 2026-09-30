"""Single application worker; outbox dispatcher restores queue connectivity."""
import os
import logging
import threading

from redis import Redis
from rq import Worker

from packages.persistence.store import Store
from packages.domain.operation_mode import operation_mode_from_env
from workers.dispatch import dispatch_pending
from workers.operations import QUEUES, timeout, limit


def main():
    operation_mode = operation_mode_from_env()
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    try:
        store.bind_workspace_mode(operation_mode)
    except Exception:
        store.engine.dispose()
        raise
    redis_url = os.environ["MJOBS_REDIS_URL"]
    queue = os.environ.get("MJOBS_WORKER_QUEUE", "mjobs-application")
    if queue not in QUEUES.values():
        raise ValueError("Unsupported worker queue")
    timeout()
    for kind in QUEUES:
        limit(kind)
    stopped = threading.Event()

    def dispatch():
        while not stopped.is_set():
            try:
                dispatch_pending(store, redis_url)
            except Exception as error:
                logging.warning("Outbox dispatch will retry: %s", type(error).__name__)
            stopped.wait(2)

    thread = threading.Thread(target=dispatch, daemon=True)
    thread.start()
    redis = Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=10)
    try:
        Worker([queue], connection=redis).work(logging_level="WARNING")
    finally:
        stopped.set()
        thread.join(timeout=5)
        redis.close()
        store.engine.dispose()


if __name__ == "__main__":
    main()
