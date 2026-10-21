"""Bounded HTTPS reads from public employer pages.

This client deliberately owns the socket and TLS setup so DNS resolution can be
validated once and the resulting numeric address can be used for the connection.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from services.company_discovery.dns_resolver import ResolverFailure, ResolverProcess


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_HEADER_BYTES = 64 * 1024
MAX_HEADER_COUNT = 200
MAX_REDIRECTS = 3
MAX_REQUESTS = 20
REQUEST_TIMEOUT = 10.0
DEADLINE = 30.0
DNS_POLL_INTERVAL = 0.05
MIN_REQUEST_INTERVAL = 1.0
USER_AGENT = "MJobs/1.0 (+public-company-discovery)"
_DNS_SLOTS = threading.BoundedSemaphore(4)


class PublicFetchError(RuntimeError):
    """Safe, stable public-fetch failure code without remote error text."""

    def __init__(self, code: str):
        if not isinstance(code, str) or not code.isidentifier():
            code = "PUBLIC_FETCH_FAILED"
        self.code = code
        super().__init__(code)


class HTTPStatusError(PublicFetchError):
    """A bounded remote HTTP status with no response-body disclosure."""

    def __init__(self, status_code: int, *, final_url=None, response_sha256=None):
        self.final_url = final_url
        self.response_sha256 = response_sha256
        self.status_code = int(status_code)
        code = "HTTP_STATUS_BLOCKED" if self.status_code in {401, 403, 429} else "HTTP_STATUS_ERROR"
        super().__init__(code)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _parse_url(value: str):
    if (not isinstance(value, str) or not value or len(value) > 2000 or _has_control(value)
            or any(character.isspace() for character in value)):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None or parsed.fragment:
            return None
        port = parsed.port
        if port is not None and port != 443:
            return None
        if "\\" in parsed.netloc:
            return None
        try:
            ipaddress.ip_address(parsed.hostname)
            host = parsed.hostname.casefold().rstrip(".")
        except ValueError:
            host = parsed.hostname.encode("idna").decode("ascii").casefold().rstrip(".")
    except (TypeError, ValueError):
        return None
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".localhost", ".internal")):
        return None
    return parsed, host, port or 443


def _origin(parsed, host: str, port: int):
    return (parsed.scheme.casefold(), host, port)


class PublicCompanyClient:
    """Fetch one exact public HTTPS origin with bounded, fail-closed policy."""

    fixture_only = False
    bounded_redirects = True

    def __init__(self, website, check=None, total_timeout=90, *, policy=None, reserve_rate=None):
        parsed = _parse_url(website)
        if parsed is None:
            raise PublicFetchError("URL_NOT_ALLOWED")
        if type(total_timeout) not in (int, float) or not 2 <= total_timeout <= 90:
            raise PublicFetchError("INVALID_CLIENT_TIMEOUT")
        self._deadline = time.monotonic() + total_timeout
        self.website = website
        self.check = check or (lambda: None)
        self._parsed, self._host, self._port = parsed
        self._origin = _origin(self._parsed, self._host, self._port)
        self._policy = policy
        self.concurrency = policy.concurrency if policy is not None else 1
        self._request_timeout = policy.request_timeout if policy is not None else REQUEST_TIMEOUT
        self._reserve_rate = reserve_rate
        self._rate_lock = threading.Lock()
        self._robots_lock = threading.Lock()
        self._resources_lock = threading.RLock()
        self._capacity = threading.BoundedSemaphore(self.concurrency)
        self._requests = 0
        self._last_request = None
        self._robots_loaded = False
        self._robots_allowed = True
        self._robots_error = None
        self._robots = None
        self._robots_delay = 0.0
        self._closed = False
        self._sockets = set()

    def _check(self):
        if self._closed:
            raise PublicFetchError("CLIENT_CLOSED")
        # Preserve the caller's cancellation exception and traceback.
        self.check()
        if time.monotonic() >= self._deadline:
            raise PublicFetchError("FETCH_DEADLINE")

    def allowed(self, url):
        parsed = _parse_url(url)
        if parsed is None:
            return False
        value, host, port = parsed
        return _origin(value, host, port) == self._origin

    def _resolve(self, host, port, timeout=REQUEST_TIMEOUT):
        self._check()
        timeout = max(0.01, min(self._request_timeout, timeout))
        deadline = min(self._deadline, time.monotonic() + timeout)
        slots = _DNS_SLOTS
        self._acquire(slots, deadline, "DNS_BUSY")
        resolver = ResolverProcess()
        try:
            answers = resolver.resolve(host, port, self._check, deadline)
        except ResolverFailure as error:
            raise PublicFetchError(error.code) from error
        finally:
            if resolver.stopped:
                slots.release()
        addresses = []
        seen = set()
        for answer in answers:
            sockaddr = answer[4]
            address = sockaddr[0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                raise PublicFetchError("DNS_INVALID")
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                    or ip.is_unspecified or ip.is_reserved):
                raise PublicFetchError("DNS_PRIVATE")
            if not ip.is_global:
                raise PublicFetchError("DNS_NOT_GLOBAL")
            key = (answer[0], address, sockaddr[1])
            if key not in seen:
                seen.add(key)
                addresses.append((answer[0], sockaddr))
        if not addresses:
            raise PublicFetchError("DNS_EMPTY")
        return addresses

    def _acquire(self, lock, deadline, code="FETCH_DEADLINE"):
        while True:
            self._check()
            remaining = min(deadline, self._deadline) - time.monotonic()
            if remaining <= 0:
                raise PublicFetchError(code)
            if lock.acquire(timeout=min(DNS_POLL_INTERVAL, remaining)):
                try:
                    self._check()
                except BaseException:
                    lock.release()
                    raise
                return

    def _wait(self, deadline):
        self._acquire(self._rate_lock, deadline)
        try:
            delay = max(self._robots_delay, MIN_REQUEST_INTERVAL,
                        60. / self._policy.rate_limit if self._policy else 0.)
            local_wait = max(0., delay - (time.monotonic() - self._last_request)) if self._last_request else 0.
            if self._reserve_rate:
                remaining = self._reserve_rate(delay, deadline, local_wait)
            else:
                remaining = local_wait
            end = time.monotonic() + remaining
            if end >= deadline:
                raise PublicFetchError("FETCH_DEADLINE")
            while True:
                self._check()
                left = end - time.monotonic()
                if left <= 0:
                    break
                time.sleep(min(left, DNS_POLL_INTERVAL))
            self._last_request = time.monotonic()
        finally:
            self._rate_lock.release()

    def _budget(self):
        with self._resources_lock:
            if self._requests >= MAX_REQUESTS:
                raise PublicFetchError("REQUEST_BUDGET")
            self._requests += 1

    def _track(self, sock):
        with self._resources_lock:
            if self._closed:
                sock.close()
                raise PublicFetchError("CLIENT_CLOSED")
            self._sockets.add(sock)

    def _request_once(self, url, deadline):
        parsed = _parse_url(url)
        if parsed is None:
            raise PublicFetchError("URL_NOT_ALLOWED")
        value, host, port = parsed
        self._check()
        self._wait(deadline)
        self._budget()
        deadline = min(deadline, time.monotonic() + self._request_timeout)
        addresses = self._resolve(host, port, timeout=max(0.01, deadline - time.monotonic()))[:1]
        last_error = None
        for family, sockaddr in addresses:
            self._check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PublicFetchError("FETCH_DEADLINE")
            sock = None
            tls = None
            deadline_timer = None
            response = None
            cancellation_stop = threading.Event()
            cancellation_error = []

            def abort_request():
                for active in (tls, sock):
                    if active is not None:
                        try:
                            active.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        try:
                            active.close()
                        except OSError:
                            pass

            def watch_cancellation():
                while not cancellation_stop.wait(DNS_POLL_INTERVAL):
                    try:
                        self._check()
                        if time.monotonic() >= deadline:
                            raise PublicFetchError("FETCH_DEADLINE")
                    except BaseException as error:
                        cancellation_error.append(error)
                        abort_request()
                        return

            cancellation_watcher = threading.Thread(
                target=watch_cancellation,
                name="mjobs-public-fetch-cancellation",
                daemon=True,
            )
            try:
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.settimeout(min(self._request_timeout, remaining))
                self._track(sock)
                cancellation_watcher.start()
                sock.connect(sockaddr)
                context = ssl.create_default_context()
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                if self._policy is None:
                    tls = context.wrap_socket(sock, server_hostname=host)
                else:
                    # Retain the SSLSocket before handshake detaches the raw FD.
                    # The owned cancellation watcher can then interrupt TLS too.
                    tls = context.wrap_socket(sock, server_hostname=host, do_handshake_on_connect=False)
                self._track(tls)
                if self._policy is not None:
                    tls.do_handshake()
                tls_remaining = deadline - time.monotonic()
                if tls_remaining <= 0:
                    raise PublicFetchError("FETCH_DEADLINE")
                tls.settimeout(min(self._request_timeout, tls_remaining))
                def abort_at_deadline():
                    for active in (tls, sock):
                        if active is not None:
                            try:
                                active.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                deadline_timer = threading.Timer(max(0.0, deadline - time.monotonic()), abort_at_deadline)
                deadline_timer.daemon = True
                deadline_timer.start()
                request = httpx.Request("GET", url)
                host_header = f"[{host}]" if ":" in host else host
                target = request.url.raw_path
                wire = (b"GET " + target + b" HTTP/1.1\r\n"
                        + f"Host: {host_header}:{port}\r\n".encode("ascii")
                        + f"User-Agent: {USER_AGENT}\r\nAccept: text/html,application/xhtml+xml,*/*;q=0.1\r\n".encode("ascii")
                        + b"Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
                tls.sendall(wire)
                response = http.client.HTTPResponse(tls, method="GET", url=url)
                response.begin()
                raw_headers = response.getheaders()
                header_size = sum(len(str(key)) + len(str(value)) + 4 for key, value in raw_headers)
                if len(raw_headers) > MAX_HEADER_COUNT or header_size > MAX_HEADER_BYTES:
                    raise PublicFetchError("RESPONSE_HEADERS_TOO_LARGE")
                encodings = [str(value).strip().casefold() for key, value in raw_headers
                             if str(key).casefold() == "content-encoding"]
                if len(encodings) > 1 or (encodings and encodings[0] != "identity"):
                    raise PublicFetchError("CONTENT_ENCODING_UNSUPPORTED")
                lengths = [str(value).strip() for key, value in raw_headers if str(key).casefold() == 'content-length']
                transfers = [str(value).strip().casefold() for key, value in raw_headers if str(key).casefold() == 'transfer-encoding']
                if (len(lengths) > 1 or (lengths and (len(lengths[0]) > 10 or not lengths[0].isascii() or not lengths[0].isdigit()))
                        or (lengths and transfers) or len(transfers) > 1 or (transfers and transfers[0] != 'chunked')):
                    raise PublicFetchError('RESPONSE_LENGTH_INVALID')
                expected_length = int(lengths[0]) if lengths else None
                if expected_length is not None and expected_length > MAX_RESPONSE_BYTES:
                    raise PublicFetchError('RESPONSE_TOO_LARGE')
                body = bytearray()
                while True:
                    self._check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PublicFetchError("FETCH_DEADLINE")
                    tls.settimeout(min(self._request_timeout, remaining))
                    chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise PublicFetchError("RESPONSE_TOO_LARGE")
                self._check()
                if expected_length is not None and len(body) != expected_length:
                    raise PublicFetchError('RESPONSE_LENGTH_INVALID')
                if cancellation_error:
                    raise cancellation_error[0]
                headers = {key: value for key, value in raw_headers}
                return httpx.Response(response.status, headers=headers, content=bytes(body), request=request)
            except PublicFetchError:
                if cancellation_error:
                    raise cancellation_error[0]
                raise
            except (OSError, ssl.SSLError, http.client.HTTPException):
                if cancellation_error:
                    raise cancellation_error[0]
                if time.monotonic() >= deadline:
                    raise PublicFetchError("FETCH_DEADLINE")
                last_error = PublicFetchError("NETWORK_FAILED")
            finally:
                cancellation_stop.set()
                if cancellation_watcher.is_alive():
                    cancellation_watcher.join(timeout=1)
                if deadline_timer is not None:
                    deadline_timer.cancel()
                    deadline_timer.join()
                if self._policy is not None and response is not None:
                    response.close()
                for item in (tls, sock):
                    if item is not None:
                        with self._resources_lock:
                            self._sockets.discard(item)
                        try:
                            item.close()
                        except OSError:
                            pass
        raise last_error or PublicFetchError("NETWORK_FAILED")

    def _load_robots(self, deadline):
        self._acquire(self._robots_lock, deadline)
        try:
            return self._load_robots_locked(deadline)
        finally:
            self._robots_lock.release()

    def _load_robots_locked(self, deadline):
        if self._robots_loaded:
            if not self._robots_allowed:
                raise PublicFetchError(self._robots_error or "ROBOTS_UNAVAILABLE")
            return
        self._robots_loaded = True
        robots_host = f"[{self._host}]" if ":" in self._host else self._host
        robots_url = urlunsplit(("https", robots_host if self._port == 443 else f"{robots_host}:{self._port}", "/robots.txt", "", ""))
        try:
            response = self._request_once(robots_url, deadline)
        except PublicFetchError as error:
            if error.code == "MONITOR_RATE_DEFERRED":
                self._robots_allowed = False
                self._robots_error = error.code
                raise
            self._robots_allowed = False
            self._robots_error = "ROBOTS_UNAVAILABLE"
            raise PublicFetchError("ROBOTS_UNAVAILABLE")
        if response.status_code == 404:
            return
        if response.status_code in {401, 403, 429} or not 200 <= response.status_code < 300:
            self._robots_allowed = False
            self._robots_error = "ROBOTS_DENIED"
            raise PublicFetchError("ROBOTS_DENIED")
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        parser.parse(response.content.decode("utf-8", errors="replace").splitlines())
        self._robots = parser
        self._robots_delay = max(0.0, float(parser.crawl_delay(USER_AGENT) or parser.crawl_delay("*") or 0.0))
        rate = parser.request_rate(USER_AGENT) or parser.request_rate("*")
        if rate and rate.requests:
            self._robots_delay = max(self._robots_delay, float(rate.seconds) / max(1, rate.requests))

    def _robots_for(self, url, deadline):
        """Return robots rules for an allowed URL origin (the base client is same-origin)."""
        if not self.allowed(url):
            raise PublicFetchError("URL_NOT_ALLOWED")
        self._load_robots(deadline)
        return self._robots

    def get(self, url):
        self._acquire(self._capacity, self._deadline)
        try:
            return self._get(url)
        finally:
            self._capacity.release()

    def _get(self, url):
        self._check()
        if not self.allowed(url):
            raise PublicFetchError("URL_NOT_ALLOWED")
        deadline = min(self._deadline, time.monotonic() + DEADLINE)
        current = url
        history = []
        for redirects in range(MAX_REDIRECTS + 1):
            self._check()
            robots = self._robots_for(current, deadline)
            if robots and not robots.can_fetch(USER_AGENT, current):
                raise PublicFetchError("ROBOTS_DENIED")
            response = self._request_once(current, deadline)
            if 300 <= response.status_code < 400 and response.status_code not in {301, 302, 303, 307, 308}:
                raise HTTPStatusError(response.status_code, final_url=str(response.url),
                                      response_sha256=hashlib.sha256(response.content).hexdigest())
            if response.status_code not in {301, 302, 303, 307, 308}:
                response.history = history
                if response.status_code >= 400:
                    raise HTTPStatusError(response.status_code, final_url=str(response.url),
                                      response_sha256=hashlib.sha256(response.content).hexdigest())
                return response
            location = response.headers.get("location")
            if not location:
                raise PublicFetchError("REDIRECT_LOCATION_MISSING")
            if redirects >= MAX_REDIRECTS:
                raise PublicFetchError("REDIRECT_LIMIT")
            target = urljoin(current, location)
            if not self.allowed(target):
                raise PublicFetchError("REDIRECT_BLOCKED")
            history.append(response)
            current = target
        raise PublicFetchError("REDIRECT_LIMIT")

    def close(self):
        with self._resources_lock:
            self._closed = True
            sockets = list(self._sockets)
            self._sockets.clear()
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
