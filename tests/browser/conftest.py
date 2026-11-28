"""Browser-test-only transport policy for explicitly owned Windows API sockets."""
import ipaddress
import socket
import sys
import threading
from functools import wraps

import pytest
import uvicorn

from fixtures.browser_ports import run_owned_browser_server


def is_owned_fixture_socket(sock):
    if not isinstance(sock, socket.socket):
        return False
    if sock.family not in (socket.AF_INET, socket.AF_INET6) or sock.type != socket.SOCK_STREAM:
        return False
    try:
        address = sock.getsockname()
        return address[1] > 0 and ipaddress.ip_address(address[0]).is_loopback
    except (OSError, ValueError):
        return False


def browser_fixture_run(original):
    """Preserve normal Uvicorn behavior outside the narrow fixture boundary."""
    @wraps(original)
    def run(server, sockets=None):
        if (threading.current_thread() is not threading.main_thread()
                and server.config.loop == "auto"
                and not server.config.use_subprocess
                and isinstance(sockets, (list, tuple))
                and sockets
                and all(is_owned_fixture_socket(sock) for sock in sockets)):
            return run_owned_browser_server(server, sockets=sockets)
        return original(server, sockets=sockets)
    return run


@pytest.fixture(autouse=True)
def windows_owned_browser_transport(monkeypatch):
    # Scope the adapter to browser pytest cases only. Keep the Linux runner,
    # explicit custom loops, production server startup and all assertions intact.
    if sys.platform == "win32":
        monkeypatch.setattr(uvicorn.Server, "run", browser_fixture_run(uvicorn.Server.run))
