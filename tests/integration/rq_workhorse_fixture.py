"""Narrow disposable-fixture admission and Linux process identity fencing."""
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from rq import Worker


def admit_redis(url):
    if not isinstance(url, str) or not url:
        raise ValueError("Explicit disposable CI Redis URL required")
    target = urlsplit(url)
    if (target.scheme != "redis" or target.hostname != "127.0.0.1"
            or target.port != 6379 or target.path != "/0" or target.username
            or target.password or target.query or target.fragment):
        raise ValueError("Requires disposable CI Redis at 127.0.0.1:6379/0")
    return url


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    group: int
    session: int
    started: int


def process_identity(pid, *, proc=Path("/proc")):
    # comm can contain spaces and parentheses; fields after its final ')' are
    # state (3), ppid (4), pgrp (5), session (6), ... starttime (22).
    fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return ProcessIdentity(pid, int(fields[1]), int(fields[2]), int(fields[3]), int(fields[19]))


def signal_owned(owned, sig):
    try:
        descriptor = os.pidfd_open(owned.pid)
    except ProcessLookupError:
        return False
    try:
        try:
            current = process_identity(owned.pid)
        except FileNotFoundError:
            return False
        # Parent changes after our parent worker dies. Session, group and
        # starttime must retain their verified values. pidfd binds this signal
        # even if the process dies and its PID is reused after the check.
        if ((current.pid, current.group, current.session, current.started)
                != (owned.pid, owned.group, owned.session, owned.started)):
            raise RuntimeError("Owned test process identity changed; refusing signal")
        try:
            signal.pidfd_send_signal(descriptor, sig)
        except ProcessLookupError:
            return False
        return True
    finally:
        os.close(descriptor)


def until(predicate, message, *, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError(message)


def descendants(parent):
    """Snapshot only a verified child's descendants, including thread forks."""
    found = []
    pending = [parent]
    while pending:
        pid = pending.pop()
        children = set()
        for path in Path(f"/proc/{pid}/task").glob("*/children"):
            try:
                children.update(int(value) for value in path.read_text().split())
            except FileNotFoundError:
                continue
        for child in children:
            try:
                owned = process_identity(child)
            except FileNotFoundError:
                continue
            if owned.parent != pid:
                raise RuntimeError("Descendant ancestry changed during fixture snapshot")
            found.append(owned)
            pending.append(child)
    return found


class OwnedWorker:
    """Actual production entry point, with signals limited to verified PIDs."""

    def __init__(self, store, redis, redis_url, origin, root, suffix, *, queue="mjobs-discovery"):
        self.store, self.redis = store, redis
        self.redis_url, self.origin, self.root = redis_url, origin, root
        self.log_path = root / f"worker-{suffix}.log"
        self.queue = queue
        self.process = None
        self.identity = None
        self.horse = None
        self.worker = None
        self.children = []

    def __enter__(self):
        environment = {key: os.environ[key] for key in (
            "PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
            if key in os.environ}
        environment.update(MJOBS_DATABASE_URL=self.store.url,
                           MJOBS_REDIS_URL=self.redis_url,
                           MJOBS_MOCK_ORIGIN=self.origin,
                           MJOBS_OUTPUT_DIR=str(self.root),
                           MJOBS_OPERATION_MODE="FIXTURE",
                           MJOBS_WORKER_QUEUE=self.queue,
                           MJOBS_OPERATION_TIMEOUT="30", PYTHONUNBUFFERED="1")
        self.log = self.log_path.open("w")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "workers.main"],
                cwd=Path(__file__).resolve().parents[2], env=environment,
                stdout=self.log, stderr=self.log, start_new_session=True)
            self.identity = process_identity(self.process.pid)
            if (self.identity.group, self.identity.session) != (self.process.pid, self.process.pid):
                raise RuntimeError("Fresh worker does not own its process session")
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def registered(self):
        if self.process.poll() is not None:
            raise AssertionError("Owned production worker exited during startup")
        for worker in Worker.all(connection=self.redis):
            if worker.pid == self.process.pid:
                if worker.queue_names() != [self.queue]:
                    raise AssertionError("Owned worker registered an unexpected queue")
                self.worker = worker
                return worker
        return None

    def capture_horse(self, job):
        worker = self.registered()
        if not worker or job.get_status(refresh=True) != "started":
            return None
        job.refresh()
        if job.worker_name != worker.name or worker.get_current_job_id() != job.id:
            raise AssertionError("RQ job is not owned by the spawned production worker")
        executions = job.get_executions()
        if len(executions) != 1 or executions[0].worker_name != worker.name:
            raise AssertionError("Expected one RQ execution bound to the owned worker")
        children = Path(f"/proc/{self.process.pid}/task/{self.process.pid}/children").read_text().split()
        if len(children) != 1:
            raise AssertionError("Expected exactly one production RQ workhorse")
        horse = process_identity(int(children[0]))
        # RQ deliberately creates a new group for the horse with setpgrp().
        if ((horse.parent, horse.group, horse.session)
                != (self.process.pid, horse.pid, self.process.pid)):
            raise AssertionError("Workhorse ancestry/session is not owned by this fixture")
        self.horse = horse
        # A fixture browser may exist only beneath this verified workhorse.
        # Keep exact starttime-bound PIDs before killing their owning ancestor.
        self.children = descendants(horse.pid)
        return executions[0]

    def abandon(self):
        if self.horse is None:
            raise RuntimeError("Capture and verify the owned RQ workhorse before interruption")
        # Parent must die first, otherwise its normal monitor handles the death
        # and this would not test abandoned-execution maintenance.
        signal_owned(self.identity, signal.SIGKILL)
        self.process.wait(timeout=5)
        signal_owned(self.horse, signal.SIGKILL)
        for child in reversed(self.children):
            signal_owned(child, signal.SIGKILL)

    def __exit__(self, *_args):
        try:
            if self.horse is not None:
                signal_owned(self.horse, signal.SIGTERM)
            if self.identity is not None and self.process.poll() is None:
                signal_owned(self.identity, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    signal_owned(self.identity, signal.SIGKILL)
                    self.process.wait(timeout=5)
            if self.horse is not None:
                signal_owned(self.horse, signal.SIGKILL)
            for child in reversed(self.children):
                signal_owned(child, signal.SIGKILL)
            if self.worker is not None:
                self.worker.register_death()
        finally:
            self.log.close()
