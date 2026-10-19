"""Request-owned native DNS; no executor thread survives a cancelled lookup."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time


_POLL = .02
MAX_ANSWERS = 64
MAX_OUTPUT = 65536
_LOOKUP_CODE = """
import json, socket, sys
answers = socket.getaddrinfo(sys.argv[1], int(sys.argv[2]), type=socket.SOCK_STREAM)
if len(answers) > 64:
    sys.exit(2)
output = json.dumps(answers).encode('ascii')
if len(output) > 65536:
    sys.exit(2)
sys.stdout.buffer.write(output)
"""


class ResolverFailure(RuntimeError):
    def __init__(self, code="DNS_FAILED"):
        self.code = code
        super().__init__(code)


class ResolverProcess:
    def __init__(self):
        self.process = None
        self.output = None

    @property
    def stopped(self):
        return self.process is None or self.process.poll() is not None

    def resolve(self, host, port, check, deadline):
        try:
            check()
            if time.monotonic() >= deadline:
                raise ResolverFailure("DNS_TIMEOUT")
            # A seekable bounded-result file avoids a full pipe blocking the
            # child before exit, and needs no background reader thread.
            try:
                self.output = tempfile.TemporaryFile()
                self.process = subprocess.Popen(
                    [sys.executable, "-I", "-c", _LOOKUP_CODE, host, str(port)],
                    stdin=subprocess.DEVNULL, stdout=self.output, stderr=subprocess.DEVNULL,
                    env={key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
                         if key in os.environ},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except OSError as error:
                raise ResolverFailure() from error
            while True:
                check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ResolverFailure("DNS_TIMEOUT")
                try:
                    size = os.fstat(self.output.fileno()).st_size
                except OSError as error:
                    raise ResolverFailure() from error
                if size > MAX_OUTPUT:
                    raise ResolverFailure("DNS_INVALID")
                if self.process.poll() is not None:
                    break
                time.sleep(min(_POLL, remaining))
            self.process.wait(timeout=0)
            if self.process.returncode:
                raise ResolverFailure()
            try:
                self.output.seek(0)
                encoded = self.output.read(MAX_OUTPUT + 1)
            except OSError as error:
                raise ResolverFailure() from error
            if len(encoded) > MAX_OUTPUT:
                raise ResolverFailure("DNS_INVALID")
            try:
                answers = json.loads(encoded)
            except (ValueError, UnicodeError) as error:
                raise ResolverFailure("DNS_INVALID") from error
            if not isinstance(answers, list) or len(answers) > MAX_ANSWERS:
                raise ResolverFailure("DNS_INVALID")
            result = []
            for answer in answers:
                if not isinstance(answer, list) or len(answer) != 5:
                    raise ResolverFailure("DNS_INVALID")
                family, kind, proto, name, target = answer
                if (type(family) is not int or family not in (socket.AF_INET, socket.AF_INET6)
                        or type(kind) is not int or kind != socket.SOCK_STREAM or type(proto) is not int
                        or not isinstance(name, str) or len(name) > 1024
                        or not isinstance(target, list)
                        or len(target) != (2 if family == socket.AF_INET else 4)
                        or not isinstance(target[0], str) or len(target[0]) > 64
                        or type(target[1]) is not int or target[1] != port
                        or any(type(value) is not int or not 0 <= value <= 2**32 - 1
                               for value in target[2:])):
                    raise ResolverFailure("DNS_INVALID")
                result.append((family, kind, proto, name, tuple(target)))
            check()
            return result
        finally:
            self.close()

    def close(self):
        try:
            if not self.stopped:
                self.process.terminate()
                try:
                    self.process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ResolverFailure() from error
        finally:
            # A failed OS termination must not authorize another resolver slot
            # or discard a still-owned output handle. The caller fails closed.
            if self.stopped and self.output is not None:
                self.output.close()
