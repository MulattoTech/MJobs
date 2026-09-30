"""Single application worker; outbox dispatcher restores queue connectivity."""
import os
import logging
import threading

from redis import Redis
from rq import Worker

from packages.persistence.store import Store
from workers.dispatch import dispatch_pending


def main():
    store = Store(os.environ["MJOBS_DATABASE_URL"])
    redis_url = os.environ["MJOBS_REDIS_URL"]
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
        Worker(["mjobs"], connection=redis).work(logging_level="WARNING")
    finally:
        stopped.set()
        thread.join(timeout=5)
        redis.close()
        store.engine.dispose()


if __name__ == "__main__":
    main()
