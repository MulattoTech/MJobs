"""Dedicated RQ runner for durable company-discovery operations."""
import os

from workers import main as operation_worker
from workers.operations import QUEUES


def main():
    """Run the shared operation worker pinned to the company-discovery queue."""
    queue = QUEUES["COMPANY_DISCOVER"]
    previous_queue = os.environ.get("MJOBS_WORKER_QUEUE")
    os.environ["MJOBS_WORKER_QUEUE"] = queue
    try:
        operation_worker.main()
    finally:
        if previous_queue is None:
            os.environ.pop("MJOBS_WORKER_QUEUE", None)
        else:
            os.environ["MJOBS_WORKER_QUEUE"] = previous_queue


if __name__ == "__main__":
    main()
