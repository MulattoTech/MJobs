"""Synthetic, individually owned resolver processes; never query host DNS."""
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

from services.company_discovery import dns_resolver


PUBLIC_ANSWERS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


class OwnedResolvers:
    def __init__(self, root):
        self.root = Path(root)
        self.processes, self.outputs, self.arguments = [], [], []
        self.lock = threading.Lock()
        self.peak = 0
        self._popen = subprocess.Popen

    def spawn(self, arguments, **kwargs):
        process = self._popen(arguments, **kwargs)
        with self.lock:
            self.processes.append(process)
            self.outputs.append(kwargs["stdout"])
            self.arguments.append(arguments)
            self.peak = max(self.peak, sum(item.poll() is None for item in self.processes))
        return process

    def wait_started(self, count, timeout=2):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if len(list(self.root.glob("*.ready"))) >= count:
                return True
            time.sleep(.01)
        return False

    def assert_reaped(self):
        if any(process.poll() is None for process in self.processes):
            raise AssertionError("Owned resolver process survived its caller")
        if any(not output.closed for output in self.outputs):
            raise AssertionError("Owned resolver output survived its caller")

    def cleanup(self):
        leaked = False
        for process in self.processes:
            if process.poll() is None:
                leaked = True
                process.terminate()
                try:
                    process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        for output in self.outputs:
            output.close()
        if leaked:
            raise AssertionError("Test cleanup had to terminate a surviving owned resolver")


@contextmanager
def owned_resolvers(*, answers=None, code=None, stall_prefix=None, ignore_terminate=False):
    """Only replace the native getaddrinfo child's program, not its lifecycle."""
    with tempfile.TemporaryDirectory(prefix="mjobs-owned-public-dns-") as directory:
        fixture = OwnedResolvers(directory)
        if code is None:
            encoded = json.dumps(PUBLIC_ANSWERS if answers is None else answers)
            code = "import os,sys,time\n"
            if ignore_terminate:
                code += "import signal; signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            if stall_prefix is not None:
                code += (f"if sys.argv[1].startswith({stall_prefix!r}):\n"
                         f" open(os.path.join({directory!r},str(os.getpid())+'.ready'),'w').close()\n"
                         " time.sleep(15)\n")
            code += f"sys.stdout.write({encoded!r})\n"
        try:
            with patch.object(dns_resolver, "_LOOKUP_CODE", code), patch.object(
                    dns_resolver.subprocess, "Popen", side_effect=fixture.spawn):
                yield fixture
        finally:
            fixture.cleanup()
