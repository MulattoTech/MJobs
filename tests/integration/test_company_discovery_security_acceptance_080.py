"""MJ-REQ-080: the mounted PERSONAL discovery path rejects unsafe destinations.

The worker, discovery service, public client, and persistence are production code.
Only the native resolver child's answer program is synthetic; its process lifecycle
and the parent's complete address validation still run. No external socket opens.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from socketserver import TCPServer
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import select

from apps.api.main import create_app
from packages.persistence import models as m
from packages.persistence.store import Store
from services.company_discovery.public_client import PublicCompanyClient, PublicFetchError
from tests.fixtures.company_dns import owned_resolvers
from tests.integration import test_public_company_tls_policy as tls_fixture
from tests.integration.test_spine import PROFILE
from workers.tasks import execute_operation


def address_answer(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    target = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    return (family, socket.SOCK_STREAM, 6, "", target)


class CompanyDiscoverySecurityAcceptance080Tests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mjobs-company-security-080-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database_url = f"sqlite:///{(self.root / 'company.db').as_posix()}"
        self.store = Store(self.database_url)
        self.addCleanup(self.store.engine.dispose)
        self.store.migrate()
        self.store.bind_workspace_mode("PERSONAL")
        self.store.save_profile(PROFILE)
        self.original_profile = self.store.profile("fixture")
        environment = patch.dict(os.environ, {
            "MJOBS_DATABASE_URL": self.database_url,
            "MJOBS_OPERATION_MODE": "PERSONAL",
            "MJOBS_REDIS_URL": "redis://127.0.0.1:1/0",
            "MJOBS_OUTPUT_DIR": str(self.root / "outputs"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.original_connect = socket.socket.connect
        self.network_guards = []
        for target in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            guard = patch(target, side_effect=AssertionError(f"Unexpected network call: {target}"))
            self.network_guards.append(guard.start())
            self.addCleanup(guard.stop)

    def _app(self, store):
        return create_app(
            store, "http://127.0.0.1:1", self.root / "outputs",
            redis_url="redis://127.0.0.1:1/0", operation_mode="PERSONAL",
        )

    def _assert_no_uncontrolled_network(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def test_api_rejects_unsafe_schemes_hosts_and_ports_without_queueing_or_dns(self):
        targets = (
            "file:///etc/passwd", "ftp://employer.example/careers",
            "http://employer.example/careers", "//employer.example/careers",
            "https:///careers", "https://localhost/", "https://intranet.local/",
            "https://service.internal/", "https://127.0.0.1/", "https://10.0.0.1/",
            "https://169.254.169.254/", "https://[::1]/", "https://[fe80::1]/",
            "https://synthetic:fixture@employer.example/",
            "https://employer.example:80/", "https://employer.example:8443/",
        )
        with TestClient(self._app(self.store)) as api:
            for target in targets:
                with self.subTest(target=target):
                    response = api.post("/companies/discovery", json={
                        "company_name": "Synthetic Employer",
                        "company_website": target,
                        "reason": "USER_ADDED_COMPANY", "profile_id": "fixture",
                    })
                    self.assertEqual(response.status_code, 422, response.text)
                    self.assertEqual(self.store.count(m.operations.name), 0)
        self.assertEqual(self.store.count(m.company_sources.name), 0)
        self.assertEqual(self.store.count(m.jobs.name), 0)
        self.assertEqual(self.store.profile("fixture"), self.original_profile)
        self._assert_no_uncontrolled_network()

    def _assert_private_dns_worker_receipt(self, label, answers=None, *, resolver_code=None,
                                           hostname=None, expected_error="ROBOTS_UNAVAILABLE",
                                           expected_lookups=1):
        hostname = hostname or f"{label}.employer.example"
        request = {
            "company_name": "Synthetic Employer",
            "company_website": f"https://{hostname}/",
            "reason": "USER_ADDED_COMPANY", "profile_id": "fixture",
            "request_id": f"security-080-{label}",
        }
        observed_dns_failures = []
        original_resolve = PublicCompanyClient._resolve

        def observe_resolve(client, host, port, *args, **kwargs):
            # Call through: do not manufacture the error that robots.txt masks.
            try:
                return original_resolve(client, host, port, *args, **kwargs)
            except PublicFetchError as error:
                observed_dns_failures.append((host, port, error.code))
                raise

        with owned_resolvers(answers=answers, code=resolver_code) as resolver, patch.object(
                PublicCompanyClient, "_resolve", observe_resolve):
            with TestClient(self._app(self.store)) as api:
                response = api.post("/companies/discovery", json=request)
                self.assertEqual(response.status_code, 202, response.text)
                operation_id = response.json()["operation_id"]
                with self.store.engine.connect() as connection:
                    queued = dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one())
                self.assertEqual(queued["state"], "QUEUED")
                self.assertEqual(queued["kind"], "COMPANY_DISCOVER")
                self.assertEqual(queued["params"]["fetch_mode"], "PUBLIC_HTTPS")
                self.assertEqual(queued["params"]["profile_id"], "fixture")
                self.assertEqual(queued["params"]["candidate"]["company_website"], request["company_website"])
                self.assertEqual(resolver.processes, [], "API must only persist the discovery intent")
                result = execute_operation(operation_id)
                self.assertEqual(result["status"], "BLOCKED", result)
                # Initial robots failure is translated; later DNS failure stays typed.
                self.assertEqual(result["failure"]["code"], expected_error)
            self.assertEqual(observed_dns_failures, [(hostname, 443, "DNS_PRIVATE")])
            self.assertEqual(len(resolver.processes), expected_lookups)
            resolver.assert_reaped()
            self._assert_no_uncontrolled_network()

            # Drop existing connections and read with an independent Store/app.
            self.store.engine.dispose()
            reopened = Store(self.database_url)
            try:
                with reopened.engine.connect() as connection:
                    durable = dict(connection.execute(select(m.operations).where(
                        m.operations.c.id == operation_id)).mappings().one())
                self.assertEqual(durable["state"], "BLOCKED")
                self.assertEqual(durable["error"], expected_error)
                self.assertEqual(durable["attempt_count"], 1)
                self.assertEqual(durable["retry_count"], 0)
                self.assertIsNone(durable["next_attempt_at"])
                self.assertIsNone(durable["lease_expires_at"])
                self.assertIsNotNone(durable["completed_at"])
                self.assertEqual(durable["recovery_metadata"]["action"], "MANUAL_REVIEW")
                with TestClient(self._app(reopened)) as api:
                    response = api.get(f"/operations/{operation_id}")
                    self.assertEqual(response.status_code, 200, response.text)
                    receipt = response.json()
                    for key in ("id", "state", "error", "attempt_count", "retry_count",
                                "next_attempt_at", "completed_at", "recovery_metadata"):
                        self.assertEqual(receipt[key], durable[key], key)
                    replay = api.post("/companies/discovery", json=request)
                    self.assertEqual(replay.status_code, 202, replay.text)
                    self.assertEqual(replay.json()["operation_id"], operation_id)
                    self.assertEqual(execute_operation(operation_id)["status"],
                                     "ALREADY_CLAIMED_OR_CAPACITY")
                    self.assertEqual(api.get(f"/operations/{operation_id}").json(), receipt)
                self.assertEqual(reopened.count(m.company_sources.name), 0)
                self.assertEqual(reopened.count(m.jobs.name), 0)
                self.assertEqual(reopened.profile("fixture"), self.original_profile)
            finally:
                reopened.engine.dispose()
            self.assertEqual(len(resolver.processes), expected_lookups,
                             "A blocked receipt must not retry DNS")
            self.assertEqual(observed_dns_failures, [(hostname, 443, "DNS_PRIVATE")])
            resolver.assert_reaped()
            self._assert_no_uncontrolled_network()

    def test_private_ipv4_and_ipv6_dns_block_real_worker_and_survive_restart(self):
        addresses = {
            "private-v4": "10.23.45.67",
            "loopback-v4": "127.0.0.1",
            "link-local-v4": "169.254.169.254",
            "private-v6": "fd00::1",
            "loopback-v6": "::1",
            "link-local-v6": "fe80::1",
            "mapped-loopback-v6": "::ffff:127.0.0.1",
        }
        for label, address in addresses.items():
            with self.subTest(address=address):
                self._assert_private_dns_worker_receipt(label, [address_answer(address)])

    def test_mixed_public_and_private_dns_blocks_before_any_socket_and_never_retries(self):
        self._assert_private_dns_worker_receipt("mixed-dns", [
            address_answer("8.8.8.8"), address_answer("1.1.1.1"),
            address_answer("2606:4700:4700::1111"), address_answer("10.23.45.67"),
        ])

    def test_same_origin_redirect_revalidates_changed_dns_before_connecting(self):
        certificate = tls_fixture.PublicCompanyTlsPolicyTests()
        certificate.setUp()
        self.addCleanup(certificate.doCleanups)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.minimum_version = ssl.TLSVersion.TLSv1_2
        server_context.load_cert_chain(str(certificate.certificate), str(certificate.key))
        client_context = ssl.create_default_context(cafile=str(certificate.certificate))
        client_context.minimum_version = ssl.TLSVersion.TLSv1_2
        redirect_seen = self.root / "redirect-seen"
        requested_paths, destinations = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                requested_paths.append(self.path)
                if self.path == "/robots.txt":
                    body = b"User-agent: *\nAllow: /\n"
                    self.send_response(200)
                elif self.path == "/":
                    # The subsequent same-origin request must resolve again.
                    redirect_seen.write_text("redirect issued", encoding="utf-8")
                    body = b""
                    self.send_response(302)
                    self.send_header("Location", "/careers")
                else:
                    body = b"Unexpected redirected request"
                    self.send_response(500)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class OwnedServer(ThreadingHTTPServer):
            daemon_threads = True

            def server_bind(self):
                # Avoid HTTPServer's unrelated reverse-DNS lookup for its name.
                TCPServer.server_bind(self)
                self.server_name = "fixture.example.com"
                self.server_port = self.server_address[1]

        server = OwnedServer(("127.0.0.1", 0), Handler)
        server.socket = server_context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": .02}, daemon=True,
                                  name="mjobs-company-security-080-https")
        thread.start()

        def controlled_connect(connection, address):
            self.assertEqual(address, ("8.8.8.8", 443), "Only the public pinned address may connect")
            destinations.append(address)
            # A numeric destination already validated by production is mapped to
            # this test's owned listener. TLS and HTTP parsing remain real.
            return self.original_connect(connection, server.server_address)

        public_answers = json.dumps([address_answer("8.8.8.8")])
        private_answers = json.dumps([address_answer("127.0.0.1")])
        resolver_code = (
            "from pathlib import Path\nimport sys\n"
            f"sys.stdout.write({private_answers!r} if "
            f"Path({str(redirect_seen)!r}).exists() else {public_answers!r})\n"
        )
        try:
            with patch.object(socket.socket, "connect", controlled_connect), patch(
                    "services.company_discovery.public_client.ssl.create_default_context",
                    return_value=client_context):
                self._assert_private_dns_worker_receipt(
                    "redirect-dns-change", resolver_code=resolver_code,
                    hostname="fixture.example.com", expected_error="DNS_PRIVATE", expected_lookups=3,
                )
            self.assertEqual(requested_paths, ["/robots.txt", "/"])
            self.assertEqual(destinations, [("8.8.8.8", 443)] * 2)
            self.assertTrue(redirect_seen.exists())
            self._assert_no_uncontrolled_network()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(1)
            self.assertFalse(thread.is_alive(), "Owned HTTPS listener must terminate")
