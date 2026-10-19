"""Owned stalled DNS must not exhaust future public-company HTTPS reads."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import ssl
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from services.company_discovery import public_client
from services.company_discovery.monitoring_policy import MonitoringPolicy
from tests.integration import test_public_company_tls_policy as tls_fixture
from workers.operations import OperationCancelled


ANSWERS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]


@contextmanager
def owned_https():
    fixture = tls_fixture.PublicCompanyTlsPolicyTests()
    fixture.setUp()
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(str(fixture.certificate), str(fixture.key))
    client_context = ssl.create_default_context(cafile=str(fixture.certificate))
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append(self.path)
            body = b"User-agent: *\nAllow: /\n" if self.path == "/robots.txt" else b"healthy"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02},
                              name="mjobs-owned-dns-https", daemon=True)
    thread.start()
    original_connect = socket.socket.connect

    def connect(connection, address):
        if address != ("8.8.8.8", 443):
            raise AssertionError("Refusing unexpected test egress")
        return original_connect(connection, server.server_address)

    try:
        with patch.object(socket.socket, "connect", connect), patch.object(
                public_client.ssl, "create_default_context", return_value=client_context):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)
        fixture.doCleanups()
        if thread.is_alive():
            raise AssertionError("Owned HTTPS listener leaked")


def available_slots():
    count = 0
    try:
        while public_client._DNS_SLOTS.acquire(blocking=False):
            count += 1
    finally:
        for _ in range(count):
            public_client._DNS_SLOTS.release()
    return count


class PublicCompanyDnsRecoveryTests(unittest.TestCase):
    def starvation(self, mode):
        processes, callers, clients, errors, started_helpers = [], [], [], [], []
        ready = threading.Condition()
        cancelled = threading.Event()

        def check():
            if cancelled.is_set():
                raise OperationCancelled()

        def lookup(host, port, **_kwargs):
            if host == "fixture.example.com":
                return ANSWERS
            if not host.startswith("stall-") or not host.endswith(".example") or port != 443:
                raise AssertionError("Unowned resolver target")
            process = subprocess.Popen([sys.executable, "-I", "-c",
                "import time; print('READY',flush=True); time.sleep(15)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            with ready:
                processes.append(process)
            if process.stdout.readline().strip() != b"READY":
                raise AssertionError("Owned stalled resolver did not start")
            with ready:
                started_helpers.append(process)
                ready.notify_all()
            process.wait(timeout=16)
            return ANSWERS

        def call(client):
            try:
                client.get(client.website)
            except BaseException as error:
                errors.append(error)

        with owned_https() as requests, patch.object(public_client.socket, "getaddrinfo", lookup):
            try:
                for index in range(4):
                    client = public_client.PublicCompanyClient(f"https://stall-{index}.example/jobs",
                        check=check, policy=MonitoringPolicy(request_timeout=2))
                    clients.append(client)
                    thread = threading.Thread(target=call, args=(client,), daemon=True,
                                              name=f"mjobs-owned-dns-caller-{index}")
                    callers.append(thread)
                    thread.start()
                with ready:
                    self.assertTrue(ready.wait_for(lambda: len(started_helpers) == 4, timeout=2),
                                    "All four owned native lookup owners must actually be running")
                self.assertTrue(all(process.poll() is None for process in processes))
                started = time.monotonic()
                if mode == "cancel":
                    cancelled.set()
                for thread in callers:
                    thread.join(.5 if mode == "cancel" else 3)
                self.assertFalse(any(thread.is_alive() for thread in callers))
                self.assertEqual(len(errors), 4)
                if mode == "cancel":
                    self.assertTrue(all(isinstance(error, OperationCancelled) for error in errors))
                    self.assertLess(time.monotonic() - started, .5)
                slots = available_slots()
                alive = sum(process.poll() is None for process in processes)
                followon = public_client.PublicCompanyClient("https://fixture.example.com/jobs", total_timeout=3)
                try:
                    try:
                        result = followon.get(followon.website).status_code
                    except public_client.PublicFetchError as error:
                        result = error.code
                finally:
                    followon.close()
                print(f"DNS_RECOVERY mode={mode} released_slots={slots} live_owned_helpers={alive} "
                      f"followon={result} HTTP={requests}")
                self.assertEqual(result, 200, f"Abandoned DNS retained capacity: slots={slots}, "
                                 f"owned_helpers={alive}, followon={result}, HTTP={requests}")
                self.assertEqual(slots, 4)
                self.assertEqual(alive, 0)
                self.assertEqual(requests, ["/robots.txt", "/jobs"])
            finally:
                cancelled.set()
                for client in clients:
                    client.close()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=1)
                    process.stdout.close()
                for thread in callers:
                    thread.join(1)
                    self.assertFalse(thread.is_alive(), "Owned caller leaked")
                until = time.monotonic() + 1
                while available_slots() != 4 and time.monotonic() < until:
                    time.sleep(.01)
                self.assertEqual(available_slots(), 4, "Test must restore native slots without resetting them")

    def test_cancelled_dns_owners_release_all_four_slots_for_healthy_https(self):
        self.starvation("cancel")

    def test_timed_out_dns_owners_release_all_four_slots_for_healthy_https(self):
        self.starvation("timeout")
